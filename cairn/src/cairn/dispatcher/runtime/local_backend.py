from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

from cairn.dispatcher.config import LocalConfig
from cairn.dispatcher.runtime.local_process import LocalProcess

LOG = logging.getLogger(__name__)


class LocalBackend:
    """Runs workers directly on the dispatcher host instead of in per-project containers.

    Each project gets an isolated working directory under ``workspace_root`` (defaulting
    to the directory the dispatcher was started in). Worker processes inherit the host
    environment so the pre-configured ``claude`` / ``codex`` / ``pi`` CLIs and their
    credentials are used as-is; no API keys are injected.

    Spawned processes are tracked by project so they can be killed when a project is
    stopped or completed.
    """

    def __init__(self, config: LocalConfig):
        self._config = config
        root = config.workspace_root
        self._root = Path(root).expanduser() if root else Path.cwd()
        self._processes: dict[str, list[LocalProcess]] = {}

    def close(self) -> None:
        for project_id in list(self._processes):
            self._kill_project_processes(project_id)
        return None

    def container_name(self, project_id: str) -> str:
        return str(self._project_dir(project_id))

    def ensure_running(self, project_id: str) -> str:
        project_dir = self._project_dir(project_id)
        project_dir.mkdir(parents=True, exist_ok=True)
        LOG.debug("local project workdir ready project=%s dir=%s", project_id, project_dir)
        return str(project_dir)

    def build_exec_process(
        self,
        container_name: str,
        env: dict[str, str],
        command: list[str],
        timeout_seconds: int | None = None,
        kill_after_seconds: int = 5,
        output_callback: callable | None = None,
    ) -> LocalProcess:
        merged_env = {**os.environ, **(env or {})}
        project_id = Path(container_name).name
        process = LocalProcess(
            command,
            cwd=container_name,
            env=merged_env,
            timeout_seconds=timeout_seconds,
            term_grace_seconds=kill_after_seconds,
            project_id=project_id,
            output_callback=output_callback,
        )
        self._processes.setdefault(project_id, []).append(process)
        return process

    def write_text_file(self, container_name: str, path: str, content: str) -> None:
        target = Path(path)
        if not target.is_absolute():
            raise ValueError(f"local file path must be absolute: {path}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    def needs_completed_cleanup(self, project_id: str) -> bool:
        if self._config.completed_action == "remove":
            return self._project_dir(project_id).exists()
        return project_id in self._processes

    def needs_stopped_cleanup(self, project_id: str) -> bool:
        return project_id in self._processes

    def cleanup_completed(self, project_id: str) -> bool:
        self._kill_project_processes(project_id)
        if self._config.completed_action == "remove":
            project_dir = self._project_dir(project_id)
            LOG.info("removing completed project workdir project=%s dir=%s", project_id, project_dir)
            shutil.rmtree(project_dir, ignore_errors=True)
        return True

    def cleanup_stopped(self, project_id: str) -> bool:
        self._kill_project_processes(project_id)
        return True

    def managed_container_names(self) -> list[str]:
        return []

    def _project_dir(self, project_id: str) -> Path:
        return self._root / project_id.replace("/", "-")

    def _kill_project_processes(self, project_id: str) -> None:
        processes = self._processes.pop(project_id, [])
        if not processes:
            return
        LOG.info("killing %d tracked process(es) for project=%s", len(processes), project_id)
        for process in processes:
            try:
                process.kill()
            except Exception:
                LOG.exception("failed to kill process for project=%s", project_id)
