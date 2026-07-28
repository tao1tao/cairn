from __future__ import annotations

import logging
import time

from cairn.dispatcher.config import DispatchConfig, WorkerConfig
from cairn.dispatcher.contracts import (
    parse_json_output,
    validate_bootstrap_conclude_payload,
    validate_bootstrap_execute_payload,
)
from cairn.dispatcher.prompting import format_hints, load_prompt, render_prompt
from cairn.dispatcher.protocol.client import CairnClient
from cairn.dispatcher.runtime.cancellation import TaskCancellation
from cairn.dispatcher.runtime.containers import ContainerManager
from cairn.dispatcher.runtime.heartbeat import HeartbeatLease
from cairn.dispatcher.tasks.common import (
    best_effort_release,
    check_execution_cancelled,
    did_timeout,
    preview,
    run_conclude_fallback,
    run_healthcheck,
    run_worker_process,
    task_healthcheck_enabled,
    write_conclude_result,
    write_conclude_result_with_fact_id,
)
from cairn.dispatcher.workers.registry import get_driver
from cairn.server.models import Intent, ProjectDetail

LOG = logging.getLogger(__name__)


def run_bootstrap_task(
    config: DispatchConfig,
    client: CairnClient,
    container_manager: ContainerManager,
    project: ProjectDetail,
    intent: Intent,
    worker: WorkerConfig,
    cancellation: TaskCancellation,
) -> str:
    driver = get_driver(worker.type, config.runtime.execution)
    task_started = time.perf_counter()
    healthcheck_timeout = config.runtime.healthcheck_timeout
    lease = HeartbeatLease.for_intent(
        client, project.project.id, intent.id, worker.name, config.runtime.interval
    )
    lease.start()
    release_fn = lambda: best_effort_release(
        client, project.project.id, intent.id, worker.name
    )
    try:
        container_name = container_manager.ensure_running(project.project.id)

        if task_healthcheck_enabled(config):
            outcome = run_healthcheck(
                driver, worker, lease, cancellation,
                project_id=project.project.id,
                healthcheck_timeout=healthcheck_timeout,
                release_fn=release_fn,
            )
            if outcome is not None:
                return outcome

        prompt = render_prompt(
            load_prompt(config.runtime.prompt_group, "bootstrap.md"),
            _bootstrap_prompt_replacements(project),
        )

        session = driver.prepare_session()
        execute = driver.build_execute(worker, prompt, session)
        session = execute.session
        execute_started = time.perf_counter()
        first = run_worker_process(
            container_manager, container_name, worker, execute.argv,
            phase="bootstrap",
            timeout_seconds=config.tasks.bootstrap.timeout,
            lease=lease, cancellation=cancellation,
        )
        execute_ms = int((time.perf_counter() - execute_started) * 1000)
        session = driver.extract_session(session, first.stdout, first.stderr)

        outcome = check_execution_cancelled(
            first, cancellation, lease,
            project_id=project.project.id,
            worker_name=worker.name,
            task_label="bootstrap",
            release_fn=release_fn,
        )
        if outcome is not None:
            return outcome

        if not did_timeout(first) and first.returncode == 0:
            try:
                model_output = driver.extract_response_text(first.stdout, first.stderr)
                payload = parse_json_output(model_output)
                kind, data = validate_bootstrap_execute_payload(payload)
            except Exception as exc:
                LOG.warning(
                    "bootstrap parse failed project=%s intent=%s worker=%s error=%s "
                    "execute_ms=%s total_ms=%s stdout_preview=%s stderr_preview=%s",
                    project.project.id, intent.id, worker.name, exc,
                    execute_ms, int((time.perf_counter() - task_started) * 1000),
                    preview(first.stdout), preview(first.stderr),
                )
                return _conclude_fallback_for_bootstrap(
                    config, client, container_manager, container_name,
                    worker, driver, project, intent, session, lease, cancellation,
                )
            if kind == "rejected":
                LOG.warning(
                    "bootstrap rejected project=%s intent=%s worker=%s "
                    "execute_ms=%s total_ms=%s stdout_preview=%s",
                    project.project.id, intent.id, worker.name,
                    execute_ms, int((time.perf_counter() - task_started) * 1000),
                    preview(first.stdout),
                )
                release_fn()
                return "rejected"
            if kind == "complete":
                return _write_bootstrap_complete_result(
                    client, project.project.id, intent.id, worker.name,
                    data["fact_description"], data["complete_description"],
                    source="bootstrap", phase_ms=execute_ms,
                    total_ms=int((time.perf_counter() - task_started) * 1000),
                )
            # kind == "fact": only a fact, no complete — just conclude the intent
            return write_conclude_result(
                client, project.project.id, intent.id, worker.name,
                data["fact_description"],
                source="bootstrap", phase_ms=execute_ms,
                total_ms=int((time.perf_counter() - task_started) * 1000),
            )
        if did_timeout(first):
            LOG.warning(
                "bootstrap timed out project=%s intent=%s worker=%s "
                "execute_ms=%s total_ms=%s stdout_preview=%s stderr_preview=%s",
                project.project.id, intent.id, worker.name,
                execute_ms, int((time.perf_counter() - task_started) * 1000),
                preview(first.stdout), preview(first.stderr),
            )
            return _conclude_fallback_for_bootstrap(
                config, client, container_manager, container_name,
                worker, driver, project, intent, session, lease, cancellation,
            )
        LOG.warning(
            "bootstrap command failed project=%s intent=%s worker=%s code=%s "
            "execute_ms=%s total_ms=%s stdout_preview=%s stderr_preview=%s",
            project.project.id, intent.id, worker.name, first.returncode,
            execute_ms, int((time.perf_counter() - task_started) * 1000),
            preview(first.stdout), preview(first.stderr),
        )
        release_fn()
        return "failed"
    except Exception:
        LOG.exception(
            "bootstrap task crashed project=%s intent=%s worker=%s",
            project.project.id, intent.id, worker.name,
        )
        best_effort_release(client, project.project.id, intent.id, worker.name)
        return "failed"
    finally:
        lease.stop()


def _conclude_fallback_for_bootstrap(
    config: DispatchConfig,
    client: CairnClient,
    container_manager: ContainerManager,
    container_name: str,
    worker: WorkerConfig,
    driver,
    project: ProjectDetail,
    intent: Intent,
    session: str | None,
    lease: HeartbeatLease,
    cancellation: TaskCancellation,
) -> str:
    return run_conclude_fallback(
        config, client, container_manager, container_name, worker, driver,
        project.project.id, intent, session, lease, cancellation,
        prompt_name="bootstrap_conclude.md",
        conclude_timeout=config.tasks.bootstrap.conclude_timeout,
        source="bootstrap_conclude",
        build_prompt=_bootstrap_prompt_builder(config, project),
        validate_fn=validate_bootstrap_conclude_payload,
    )


def _bootstrap_prompt_builder(config: DispatchConfig, project: ProjectDetail):
    """Return a callable that builds the bootstrap conclude prompt."""
    def builder(container_manager: ContainerManager, container_name: str) -> str:
        return render_prompt(
            load_prompt(config.runtime.prompt_group, "bootstrap_conclude.md"),
            _bootstrap_prompt_replacements(project),
        )
    return builder


def _bootstrap_prompt_replacements(project: ProjectDetail) -> dict[str, str]:
    facts = {fact.id: fact.description for fact in project.facts}
    hints = [
        {"id": hint.id, "content": hint.content, "creator": hint.creator, "created_at": hint.created_at}
        for hint in project.hints
    ]
    return {
        "origin": facts.get("origin", ""),
        "goal": facts.get("goal", ""),
        "hints": format_hints(hints),
    }


def _write_bootstrap_complete_result(
    client: CairnClient,
    project_id: str,
    intent_id: str,
    worker_name: str,
    fact_description: str,
    complete_description: str,
    *,
    source: str,
    phase_ms: int,
    total_ms: int | None = None,
) -> str:
    conclude = write_conclude_result_with_fact_id(
        client, project_id, intent_id, worker_name, fact_description,
        source=source, phase_ms=phase_ms, total_ms=total_ms,
    )
    if conclude.status != "success":
        return "failed"
    if conclude.fact_id is None:
        LOG.warning(
            "bootstrap complete deferred because conclude response omitted fact id "
            "project=%s intent=%s worker=%s source=%s",
            project_id, intent_id, worker_name, source,
        )
        return "success"

    response = client.complete(
        project_id, [conclude.fact_id], complete_description, worker_name,
    )
    if response.status_code in (403, 409):
        LOG.info(
            "bootstrap complete deferred project=%s intent=%s worker=%s source=%s status=%s fact_id=%s",
            project_id, intent_id, worker_name, source,
            response.status_code, conclude.fact_id,
        )
        return "success"
    if not response.ok:
        LOG.warning(
            "bootstrap complete write failed project=%s intent=%s worker=%s "
            "source=%s fact_id=%s status=%s body=%s",
            project_id, intent_id, worker_name, source,
            conclude.fact_id, response.status_code, response.text,
        )
        return "failed"
    LOG.info(
        "bootstrap completed project=%s intent=%s worker=%s source=%s from=%s phase_ms=%s%s",
        project_id, intent_id, worker_name, source, [conclude.fact_id], phase_ms,
        f" total_ms={total_ms}" if total_ms is not None else "",
    )
    return "success"
