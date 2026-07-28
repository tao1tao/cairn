from __future__ import annotations

import logging
import os
import signal
import subprocess
import threading
import time
from contextlib import suppress
from pathlib import Path

from cairn.dispatcher.runtime.process import ProcessResult

LOG = logging.getLogger(__name__)

READ_CHUNK_SIZE = 65536
STREAM_JOIN_TIMEOUT_SECONDS = 5.0
FORCE_KILL_REAP_TIMEOUT_SECONDS = 2.0
PROGRESS_DIR = Path("/tmp/cairn-progress")
_HEARTBEAT_INTERVAL = 15.0


def _write_progress(project_id: str, text: str) -> None:
    """Append text to the per-project progress log."""
    try:
        PROGRESS_DIR.mkdir(parents=True, exist_ok=True)
        log_path = PROGRESS_DIR / f"{project_id}.log"
        with open(log_path, "a", encoding="utf-8", errors="replace") as f:
            f.write(text)
    except Exception:
        pass


def _clear_progress(project_id: str) -> None:
    """Remove the progress file for a project."""
    try:
        log_path = PROGRESS_DIR / f"{project_id}.log"
        log_path.unlink(missing_ok=True)
    except Exception:
        pass


def _script_available() -> bool:
    """Check if the 'script' command is available on this system."""
    try:
        import shutil
        return shutil.which("script") is not None
    except Exception:
        return False


class LocalProcess:
    """Runs a worker command as a host subprocess.
    """

    def __init__(
        self,
        command: list[str],
        cwd: str,
        env: dict[str, str],
        timeout_seconds: int | None = None,
        term_grace_seconds: int = 5,
        project_id: str | None = None,
        output_callback: callable | None = None,
    ):
        self.command = command
        self.env = env
        self._cwd = cwd
        self._timeout_seconds = timeout_seconds
        self._term_grace = max(1.0, float(term_grace_seconds))
        self._process: subprocess.Popen[str] | None = None
        self._stdout_chunks: list[str] = []
        self._stderr_chunks: list[str] = []
        self._stdout_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        self._timed_out = False
        self._cancel_reason: str | None = None
        self._kill_lock = threading.Lock()
        self._project_id = project_id
        self._output_callback = output_callback
        self._heartbeat_stop = threading.Event()
        self._heartbeat_thread: threading.Thread | None = None
        if project_id:
            _clear_progress(project_id)

    def start(self) -> None:
        cmd_label = " ".join(self.command[:3]) + "..." if len(self.command) > 3 else " ".join(self.command)
        if self._project_id:
            _write_progress(self._project_id, f"[START] {cmd_label}\n")

        self._process = subprocess.Popen(
            self.command,
            cwd=self._cwd,
            env=self.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            start_new_session=True,
        )
        self._stdout_thread = threading.Thread(
            target=self._drain_and_write, args=(self._process.stdout, self._stdout_chunks), daemon=True
        )
        self._stderr_thread = threading.Thread(
            target=self._drain, args=(self._process.stderr, self._stderr_chunks), daemon=True
        )
        self._stdout_thread.start()
        self._stderr_thread.start()

        # Start heartbeat thread that writes progress updates
        if self._project_id:
            self._heartbeat_thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
            self._heartbeat_thread.start()

    def communicate(self, timeout: float | None) -> ProcessResult:
        assert self._process is not None
        wait_for = float(self._timeout_seconds) if self._timeout_seconds is not None else timeout
        try:
            self._process.wait(timeout=wait_for)
        except subprocess.TimeoutExpired:
            self._timed_out = True
            self._terminate()
        self._heartbeat_stop.set()
        with suppress(subprocess.TimeoutExpired):
            self._process.wait(timeout=FORCE_KILL_REAP_TIMEOUT_SECONDS)
        if self._stdout_thread is not None:
            self._stdout_thread.join(timeout=STREAM_JOIN_TIMEOUT_SECONDS)
        if self._stderr_thread is not None:
            self._stderr_thread.join(timeout=STREAM_JOIN_TIMEOUT_SECONDS)
        if self._heartbeat_thread is not None:
            self._heartbeat_thread.join(timeout=2.0)
        returncode = self._process.returncode
        if returncode is None:
            returncode = 137 if self._timed_out else 1
        return ProcessResult(
            returncode=returncode,
            stdout="".join(self._stdout_chunks),
            stderr="".join(self._stderr_chunks),
            timed_out=self._timed_out,
            cancelled=self._cancel_reason is not None,
            cancel_reason=self._cancel_reason,
        )

    def kill(self) -> None:
        self._heartbeat_stop.set()
        self._terminate()

    def cancel(self, reason: str) -> None:
        if self._cancel_reason is None:
            self._cancel_reason = reason
        self._heartbeat_stop.set()
        self._terminate()

    def _terminate(self) -> None:
        with self._kill_lock:
            process = self._process
            if process is None or process.poll() is not None:
                return
            self._signal_group(process, signal.SIGTERM)
            try:
                process.wait(timeout=self._term_grace)
                return
            except subprocess.TimeoutExpired:
                pass
            self._signal_group(process, signal.SIGKILL)

    @staticmethod
    def _signal_group(process: subprocess.Popen[str], sig: int) -> None:
        try:
            os.killpg(os.getpgid(process.pid), sig)
        except (ProcessLookupError, PermissionError):
            with suppress(ProcessLookupError, PermissionError, ValueError):
                process.send_signal(sig)

    @staticmethod
    def _drain(pipe, sink: list[str]) -> None:
        try:
            for chunk in iter(lambda: pipe.read(READ_CHUNK_SIZE), ""):
                sink.append(chunk)
        except (ValueError, OSError):
            pass
        finally:
            with suppress(Exception):
                pipe.close()

    def _drain_and_write(self, pipe, sink: list[str]) -> None:
        """Read from pipe, write to progress file too."""
        try:
            for chunk in iter(lambda: pipe.read(READ_CHUNK_SIZE), ""):
                sink.append(chunk)
                if self._project_id:
                    _write_progress(self._project_id, chunk)
                    # 实时回调，用于在线处理发现的事实
                    if self._output_callback:
                        try:
                            self._output_callback(chunk)
                        except Exception:
                            pass
                    # 立即 flush 文件，使前端能看到实时输出
                    try:
                        fpath = PROGRESS_DIR / f"{self._project_id}.log"
                        if fpath.exists():
                            fpath.touch()
                    except Exception:
                        pass
        except (ValueError, OSError) as e:
            if self._project_id:
                _write_progress(self._project_id, '[DRAIN_ERROR] ' + str(e) + chr(10))
        finally:
            with suppress(Exception):
                pipe.close()

    def _heartbeat_loop(self) -> None:
        """Write periodic heartbeat messages so the user can see the process is alive."""
        pid = self._process.pid if self._process else "?"
        timeout = self._timeout_seconds or 0
        start = time.time()
        while not self._heartbeat_stop.is_set():
            elapsed = int(time.time() - start)
            remaining = max(0, timeout - elapsed)
            # Check child processes to show what Claude is doing
            cmds = self._get_child_commands(pid)
            cmd_info = f" | active: {cmds}" if cmds else ""
            msg = f"[HEARTBEAT] PID={pid} running for {elapsed}s, timeout in {remaining}s{cmd_info}\n"
            _write_progress(self._project_id, msg)
            self._heartbeat_stop.wait(_HEARTBEAT_INTERVAL)
        total = int(time.time() - start)
        _write_progress(self._project_id, f"[END] process exited after {total}s\n")

    @staticmethod
    def _get_child_commands(pid: int) -> str:
        """Get a summary of what commands the process is currently running.
        
        Walks the process tree: parent → script → claude → shells → actual commands.
        """
        try:
            import subprocess
            # Walk process tree up to 3 levels deep
            seen = set()
            all_cmds = []
            current_pids = [str(pid)]
            
            for _ in range(4):  # up to 4 levels deep
                if not current_pids:
                    break
                parents = ",".join(current_pids)
                result = subprocess.run(
                    ["ps", "--ppid", parents, "-o", "pid=,cmd=", "--no-headers"],
                    capture_output=True, text=True, timeout=3
                )
                current_pids = []
                for line in result.stdout.strip().split("\n"):
                    line = line.strip()
                    if not line:
                        continue
                    parts = line.split(maxsplit=1)
                    if len(parts) < 2:
                        continue
                    child_pid, cmdline = parts
                    base = cmdline.split()[0] if cmdline else "?"
                    base_name = base.split("/")[-1]  # strip path
                    
                    # Skip claude itself and common shells
                    if base_name in ("claude-bin", "sh", "bash", "zsh", "dash", "script"):
                        current_pids.append(child_pid)
                        continue
                    if child_pid not in seen:
                        seen.add(child_pid)
                        all_cmds.append(base_name)
                        current_pids.append(child_pid)
            
            if all_cmds:
                # Count occurrences and return summary
                from collections import Counter
                counts = Counter(all_cmds)
                return ", ".join(f"{cmd}" for cmd, _ in counts.most_common(6))
        except Exception:
            pass
        return ""
