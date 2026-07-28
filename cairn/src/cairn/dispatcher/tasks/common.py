from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from cairn.dispatcher.config import DispatchConfig, WorkerConfig
from cairn.dispatcher.contracts import parse_json_output
from cairn.dispatcher.protocol.client import CairnClient
from cairn.dispatcher.runtime.cancellation import TaskCancellation
from cairn.dispatcher.runtime.containers import ContainerManager
from cairn.dispatcher.runtime.heartbeat import HeartbeatLease
from cairn.dispatcher.runtime.process import ProcessResult
from cairn.dispatcher.workers.base import WorkerDriver

PROCESS_COMMUNICATE_GRACE_SECONDS = 15
LOG_PREVIEW_LIMIT = 1200
GRAPH_SNAPSHOT_ROOT = "/tmp/cairn-prompts"
LOG = logging.getLogger(__name__)


@dataclass(slots=True)
class ConcludeWriteResult:
    status: str
    fact_id: str | None = None


def preview(text: str, limit: int = LOG_PREVIEW_LIMIT) -> str:
    compact = " ".join(text.split())
    if len(compact) <= limit:
        return compact
    return compact[:limit] + "..."


def did_timeout(result: ProcessResult) -> bool:
    return not result.cancelled and (result.timed_out or result.returncode in (124, 137))


def cancel_reason(result: ProcessResult, cancellation: TaskCancellation | None = None) -> str | None:
    if result.cancelled:
        return result.cancel_reason or "cancelled"
    if cancellation is not None:
        return cancellation.reason
    return None


def communicate_timeout(timeout_seconds: int, grace_seconds: int = PROCESS_COMMUNICATE_GRACE_SECONDS) -> int:
    return timeout_seconds + grace_seconds


def task_healthcheck_enabled(config: DispatchConfig) -> bool:
    if config.runtime.execution == "local":
        return False
    return config.runtime.worker_healthcheck == "startup_and_task"


def write_graph_snapshot_reference(
    container_manager: ContainerManager,
    container_name: str,
    graph_yaml: str,
    *,
    phase: str,
) -> str:
    path = f"{GRAPH_SNAPSHOT_ROOT}/{phase}-{uuid.uuid4().hex[:12]}/graph.yaml"
    container_manager.write_text_file(container_name, path, graph_yaml)
    return (
        "The graph YAML snapshot is stored in this file inside the current container:\n\n"
        f"{path}\n\n"
        "Before using the graph, read the entire file and treat its contents as the YAML snapshot "
        "for this Graph section."
    )


def run_worker_process(
    container_manager: ContainerManager,
    container_name: str,
    worker: WorkerConfig,
    argv: list[str],
    *,
    phase: str,
    timeout_seconds: int,
    lease: HeartbeatLease | None = None,
    cancellation: TaskCancellation | None = None,
    output_callback: Callable | None = None,
) -> ProcessResult:
    LOG.info(
        "starting container exec container=%s worker=%s phase=%s timeout=%ss",
        container_name,
        worker.name,
        phase,
        timeout_seconds,
    )
    process = container_manager.build_exec_process(
        container_name,
        dict(worker.env),
        argv,
        timeout_seconds=timeout_seconds,
        **({} if output_callback is None else {"output_callback": output_callback}),
    )
    process.start()
    if lease is not None:
        lease.attach_process(process)
    if cancellation is not None:
        cancellation.attach_process(process)
    try:
        return process.communicate(timeout=communicate_timeout(timeout_seconds))
    finally:
        if lease is not None:
            lease.attach_process(None)
        if cancellation is not None:
            cancellation.attach_process(None)


def project_allows_conclude_fallback(client: CairnClient, project_id: str, *, worker_name: str, intent_id: str) -> bool:
    project = client.get_project(project_id)
    if project.project.status == "active":
        return True
    LOG.info(
        "skip conclude fallback because project is no longer active project=%s intent=%s worker=%s status=%s",
        project_id,
        intent_id,
        worker_name,
        project.project.status,
    )
    return False


def best_effort_release_reason(client: CairnClient, project_id: str, worker_name: str) -> None:
    response = client.release_reason(project_id, worker_name)
    if not response.ok and response.status_code not in (403, 409):
        LOG.warning(
            "reason release failed project=%s worker=%s status=%s",
            project_id,
            worker_name,
            response.status_code,
        )
    elif response.ok:
        LOG.info("released reason project=%s worker=%s", project_id, worker_name)
    else:
        LOG.info(
            "reason release skipped project=%s worker=%s status=%s",
            project_id,
            worker_name,
            response.status_code,
        )


def write_conclude_result(
    client: CairnClient,
    project_id: str,
    intent_id: str,
    worker_name: str,
    description: str,
    *,
    source: str,
    phase_ms: int,
    total_ms: int | None = None,
) -> str:
    return write_conclude_result_with_fact_id(
        client,
        project_id,
        intent_id,
        worker_name,
        description,
        source=source,
        phase_ms=phase_ms,
        total_ms=total_ms,
    ).status


def write_conclude_result_with_fact_id(
    client: CairnClient,
    project_id: str,
    intent_id: str,
    worker_name: str,
    description: str,
    *,
    source: str,
    phase_ms: int,
    total_ms: int | None = None,
) -> ConcludeWriteResult:
    response = client.conclude(project_id, intent_id, worker_name, description)
    if response.ok:
        fact_id: str | None = None
        if isinstance(response.data, dict):
            fact = response.data.get("fact")
            if isinstance(fact, dict):
                candidate = fact.get("id")
                if isinstance(candidate, str) and candidate:
                    fact_id = candidate
        if total_ms is None:
            LOG.info(
                "intent concluded project=%s intent=%s worker=%s source=%s phase_ms=%s",
                project_id,
                intent_id,
                worker_name,
                source,
                phase_ms,
            )
        else:
            LOG.info(
                "intent concluded project=%s intent=%s worker=%s source=%s phase_ms=%s total_ms=%s",
                project_id,
                intent_id,
                worker_name,
                source,
                phase_ms,
                total_ms,
            )
        return ConcludeWriteResult(status="success", fact_id=fact_id)
    if response.status_code == 403:
        LOG.info(
            "project became inactive during conclude project=%s intent=%s worker=%s",
            project_id,
            intent_id,
            worker_name,
        )
    else:
        LOG.warning(
            "conclude write failed project=%s intent=%s worker=%s status=%s body=%s",
            project_id,
            intent_id,
            worker_name,
            response.status_code,
            response.text,
        )
    best_effort_release(client, project_id, intent_id, worker_name)
    return ConcludeWriteResult(status="failed", fact_id=None)


def best_effort_release(client: CairnClient, project_id: str, intent_id: str, worker_name: str) -> None:
    response = client.release(project_id, intent_id, worker_name)
    if not response.ok and response.status_code not in (403, 409):
        LOG.warning(
            "release failed project=%s intent=%s worker=%s status=%s",
            project_id,
            intent_id,
            worker_name,
            response.status_code,
        )
    elif response.ok:
        LOG.info("released intent project=%s intent=%s worker=%s", project_id, intent_id, worker_name)
    else:
        LOG.info(
            "release skipped project=%s intent=%s worker=%s status=%s",
            project_id,
            intent_id,
            worker_name,
            response.status_code,
        )


# ---------------------------------------------------------------------------
# Shared helpers for task implementations (bootstrap / reason / explore)
# ---------------------------------------------------------------------------


def run_healthcheck(
    driver: WorkerDriver,
    worker: WorkerConfig,
    lease: HeartbeatLease,
    cancellation: TaskCancellation,
    *,
    project_id: str,
    healthcheck_timeout: int,
    release_fn: Callable[[], None] | None = None,
) -> str | None:
    """Run a pre-task healthcheck and return an outcome string on failure, or None on success.

    The caller should immediately return the non-None outcome string.
    """
    LOG.info(
        "checking worker health project=%s worker=%s timeout=%ss",
        project_id,
        worker.name,
        healthcheck_timeout,
    )
    health = driver.check_health(worker, timeout=healthcheck_timeout)
    if cancellation.is_cancelled:
        LOG.info("task cancelled during healthcheck project=%s worker=%s reason=%s", project_id, worker.name, cancellation.reason)
        if release_fn:
            release_fn()
        return "cancelled"
    if lease.failure is not None:
        LOG.warning(
            "heartbeat lost during healthcheck project=%s worker=%s status=%s",
            project_id,
            worker.name,
            lease.failure.status_code,
        )
        if release_fn:
            release_fn()
        return "failed"
    if not health.ok:
        LOG.warning("worker unhealthy project=%s worker=%s status=%s detail=%s", project_id, worker.name, health.status, health.detail)
        if release_fn:
            release_fn()
        return "unhealthy"
    return None


def check_execution_cancelled(
    result: ProcessResult,
    cancellation: TaskCancellation,
    lease: HeartbeatLease,
    *,
    project_id: str,
    worker_name: str,
    task_label: str,
    release_fn: Callable[[], None] | None = None,
) -> str | None:
    """Check for cancellation / heartbeat-loss after execution and return outcome on failure.

    The caller should immediately return the non-None outcome string.
    """
    cancelled = cancel_reason(result, cancellation)
    if cancelled is not None:
        LOG.info(
            "%s cancelled project=%s worker=%s reason=%s",
            task_label,
            project_id,
            worker_name,
            cancelled,
        )
        if release_fn:
            release_fn()
        return "cancelled"
    if lease.failure is not None:
        LOG.warning(
            "heartbeat lost during %s project=%s worker=%s status=%s",
            task_label,
            project_id,
            worker_name,
            lease.failure.status_code,
        )
        if release_fn:
            release_fn()
        return "failed"
    return None


def run_conclude_fallback(
    config: DispatchConfig,
    client: CairnClient,
    container_manager: ContainerManager,
    container_name: str,
    worker: WorkerConfig,
    driver: WorkerDriver,
    project_id: str,
    intent: Intent,
    session: str | None,
    lease: HeartbeatLease,
    cancellation: TaskCancellation,
    *,
    prompt_name: str,
    conclude_timeout: int,
    source: str,
    build_prompt: Callable[[ContainerManager, str], str],
    validate_fn: Callable[[dict[str, Any]], tuple[str, str | None]],
) -> str:
    """Run a phase-2 conclude when the main execute timed out or failed to parse.

    Each task (bootstrap vs explore) provides its own prompt builder, validator,
    and timeout via the keyword arguments.
    """
    if not driver.supports_conclude() or not session:
        LOG.info(
            "conclude fallback unavailable project=%s intent=%s worker=%s supports_conclude=%s has_session=%s",
            project_id, intent.id, worker.name, driver.supports_conclude(), bool(session),
        )
        best_effort_release(client, project_id, intent.id, worker.name)
        return "failed"

    if lease.failure is not None:
        LOG.warning(
            "conclude fallback skipped because heartbeat already lost project=%s intent=%s worker=%s",
            project_id, intent.id, worker.name,
        )
        best_effort_release(client, project_id, intent.id, worker.name)
        return "failed"

    if cancellation.is_cancelled:
        LOG.info(
            "conclude fallback skipped because task was cancelled project=%s intent=%s worker=%s reason=%s",
            project_id, intent.id, worker.name, cancellation.reason,
        )
        best_effort_release(client, project_id, intent.id, worker.name)
        return "cancelled"

    if not project_allows_conclude_fallback(client, project_id, worker_name=worker.name, intent_id=intent.id):
        best_effort_release(client, project_id, intent.id, worker.name)
        return "failed"

    container_name = container_manager.ensure_running(project_id)

    prompt = build_prompt(container_manager, container_name)
    conclude_argv = driver.build_conclude(worker, prompt, session)
    LOG.info("starting conclude fallback project=%s intent=%s worker=%s phase=%s", project_id, intent.id, worker.name, source)
    conclude_started = time.perf_counter()
    result = run_worker_process(
        container_manager, container_name, worker, conclude_argv,
        phase=source.replace("/", "_"),
        timeout_seconds=conclude_timeout, lease=lease, cancellation=cancellation,
    )
    conclude_ms = int((time.perf_counter() - conclude_started) * 1000)

    cancelled = cancel_reason(result, cancellation)
    if cancelled is not None:
        LOG.info(
            "conclude cancelled project=%s intent=%s worker=%s reason=%s conclude_ms=%s",
            project_id, intent.id, worker.name, cancelled, conclude_ms,
        )
        best_effort_release(client, project_id, intent.id, worker.name)
        return "cancelled"
    if lease.failure is not None:
        best_effort_release(client, project_id, intent.id, worker.name)
        return "failed"
    if result.timed_out or result.returncode != 0:
        LOG.warning(
            "conclude failed project=%s intent=%s worker=%s code=%s timed_out=%s conclude_ms=%s",
            project_id, intent.id, worker.name, result.returncode, result.timed_out, conclude_ms,
        )
        best_effort_release(client, project_id, intent.id, worker.name)
        return "failed"

    try:
        model_output = driver.extract_response_text(result.stdout, result.stderr)
        payload = parse_json_output(model_output)
        kind, description = validate_fn(payload)
    except Exception as exc:
        LOG.warning(
            "conclude parse failed project=%s intent=%s worker=%s error=%s conclude_ms=%s",
            project_id, intent.id, worker.name, exc, conclude_ms,
        )
        best_effort_release(client, project_id, intent.id, worker.name)
        return "failed"

    if kind == "rejected":
        LOG.warning(
            "conclude rejected project=%s intent=%s worker=%s conclude_ms=%s",
            project_id, intent.id, worker.name, conclude_ms,
        )
        best_effort_release(client, project_id, intent.id, worker.name)
        return "rejected"

    return write_conclude_result(client, project_id, intent.id, worker.name, description, source=source, phase_ms=conclude_ms)
