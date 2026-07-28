from __future__ import annotations

import logging
import time

from cairn.dispatcher.config import DispatchConfig, WorkerConfig
from cairn.dispatcher.contracts import parse_json_output, validate_explore_payload
from cairn.dispatcher.prompting import load_prompt, render_prompt
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
    write_graph_snapshot_reference,
)
from cairn.dispatcher.workers.registry import get_driver
from cairn.server.models import Intent, ProjectDetail

LOG = logging.getLogger(__name__)


def run_explore_task(
    config: DispatchConfig,
    client: CairnClient,
    container_manager: ContainerManager,
    project: ProjectDetail,
    export_yaml: str,
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
            load_prompt(config.runtime.prompt_group, "explore.md"),
            {
                "graph_yaml": write_graph_snapshot_reference(
                    container_manager, container_name, export_yaml.strip(),
                    phase="explore_execute",
                ),
                "intent_id": intent.id,
                "intent_description": intent.description,
            },
        )

        session = driver.prepare_session()
        execute = driver.build_execute(worker, prompt, session)
        session = execute.session
        execute_started = time.perf_counter()
        first = run_worker_process(
            container_manager, container_name, worker, execute.argv,
            phase="explore_execute",
            timeout_seconds=config.tasks.explore.timeout,
            lease=lease, cancellation=cancellation,
        )
        execute_ms = int((time.perf_counter() - execute_started) * 1000)
        session = driver.extract_session(session, first.stdout, first.stderr)

        outcome = check_execution_cancelled(
            first, cancellation, lease,
            project_id=project.project.id,
            worker_name=worker.name,
            task_label="explore",
            release_fn=release_fn,
        )
        if outcome is not None:
            return outcome

        if not did_timeout(first) and first.returncode == 0:
            try:
                model_output = driver.extract_response_text(first.stdout, first.stderr)
                payload = parse_json_output(model_output)
                kind, description = validate_explore_payload(payload)
            except Exception as exc:
                LOG.warning(
                    "explore parse failed project=%s intent=%s worker=%s error=%s "
                    "execute_ms=%s total_ms=%s stdout_preview=%s stderr_preview=%s",
                    project.project.id, intent.id, worker.name, exc,
                    execute_ms, int((time.perf_counter() - task_started) * 1000),
                    preview(first.stdout), preview(first.stderr),
                )
                return _conclude_fallback_for_explore(
                    config, client, container_manager, container_name,
                    worker, driver, project.project.id, intent, export_yaml,
                    session, lease, cancellation,
                )
            if kind == "rejected":
                LOG.warning(
                    "explore rejected project=%s intent=%s worker=%s "
                    "execute_ms=%s total_ms=%s stdout_preview=%s",
                    project.project.id, intent.id, worker.name,
                    execute_ms, int((time.perf_counter() - task_started) * 1000),
                    preview(first.stdout),
                )
                # Save rejection reason as a hint so Reason can avoid repeating
                reject_reason = payload.get("reason") or ""
                if reject_reason:
                    reason_hint = f"[探索失败] 意图 {intent.id} 已拒绝: {reject_reason}"
                    client.create_hint(project.project.id, reason_hint, worker.name)
                    LOG.info(
                        "recorded rejection hint project=%s intent=%s worker=%s reason=%s",
                        project.project.id, intent.id, worker.name, reject_reason,
                    )
                release_fn()
                return "rejected"
            return write_conclude_result(
                client, project.project.id, intent.id, worker.name, description,
                source="explore_execute",
                phase_ms=execute_ms,
                total_ms=int((time.perf_counter() - task_started) * 1000),
            )

        if did_timeout(first):
            LOG.warning(
                "explore timed out project=%s intent=%s worker=%s "
                "execute_ms=%s total_ms=%s stdout_preview=%s stderr_preview=%s",
                project.project.id, intent.id, worker.name,
                execute_ms, int((time.perf_counter() - task_started) * 1000),
                preview(first.stdout), preview(first.stderr),
            )
            return _conclude_fallback_for_explore(
                config, client, container_manager, container_name,
                worker, driver, project.project.id, intent, export_yaml,
                session, lease, cancellation,
            )

        LOG.warning(
            "explore command failed project=%s intent=%s worker=%s code=%s "
            "execute_ms=%s total_ms=%s stdout_preview=%s stderr_preview=%s",
            project.project.id, intent.id, worker.name, first.returncode,
            execute_ms, int((time.perf_counter() - task_started) * 1000),
            preview(first.stdout), preview(first.stderr),
        )
        release_fn()
        return "failed"
    except Exception:
        LOG.exception(
            "explore task crashed project=%s intent=%s worker=%s",
            project.project.id, intent.id, worker.name,
        )
        best_effort_release(client, project.project.id, intent.id, worker.name)
        return "failed"
    finally:
        lease.stop()


def _conclude_fallback_for_explore(
    config: DispatchConfig,
    client: CairnClient,
    container_manager: ContainerManager,
    container_name: str,
    worker: WorkerConfig,
    driver,
    project_id: str,
    intent: Intent,
    export_yaml: str,
    session: str | None,
    lease: HeartbeatLease,
    cancellation: TaskCancellation,
) -> str:
    return run_conclude_fallback(
        config, client, container_manager, container_name, worker, driver,
        project_id, intent, session, lease, cancellation,
        prompt_name="explore_conclude.md",
        conclude_timeout=config.tasks.explore.conclude_timeout,
        source="explore_conclude",
        build_prompt=_explore_prompt_builder(config, intent, export_yaml),
        validate_fn=validate_explore_payload,
    )


def _explore_prompt_builder(
    config: DispatchConfig, intent: Intent, export_yaml: str,
):
    """Return a callable that builds the explore conclude prompt."""
    def builder(container_manager: ContainerManager, container_name: str) -> str:
        return render_prompt(
            load_prompt(config.runtime.prompt_group, "explore_conclude.md"),
            {
                "graph_yaml": write_graph_snapshot_reference(
                    container_manager, container_name, export_yaml.strip(),
                    phase="explore_conclude",
                ),
                "intent_id": intent.id,
                "intent_description": intent.description,
            },
        )
    return builder
