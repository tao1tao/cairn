from pathlib import Path
import subprocess

from fastapi import APIRouter, Query

router = APIRouter(tags=["logs"])

LOG_FILE = Path("/tmp/cairn-dispatcher.log")


@router.get("/logs")
def get_logs(lines: int = Query(50, ge=1, le=500)):
    """Return the last N lines from the dispatcher log file using tail (O(1) in file size)."""
    if not LOG_FILE.exists():
        return {"lines": [], "total": 0}

    # Count total lines efficiently without reading the whole file
    try:
        total_result = subprocess.run(
            ["wc", "-l", str(LOG_FILE)],
            capture_output=True, text=True, timeout=5,
        )
        total = int(total_result.stdout.split()[0]) if total_result.returncode == 0 else 0
    except Exception:
        total = 0

    try:
        tail_result = subprocess.run(
            ["tail", "-n", str(lines), str(LOG_FILE)],
            capture_output=True, text=True, timeout=5,
        )
        tail_lines = tail_result.stdout.splitlines() if tail_result.returncode == 0 else []
    except Exception:
        tail_lines = []

    return {
        "lines": tail_lines,
        "total": total,
    }
