from pathlib import Path

from fastapi import APIRouter, Query

router = APIRouter(tags=["logs"])

LOG_FILE = Path("/tmp/cairn-dispatcher.log")


@router.get("/logs")
def get_logs(lines: int = Query(50, ge=1, le=500)):
    """Return the last N lines from the dispatcher log file."""
    if not LOG_FILE.exists():
        return {"lines": [], "total": 0}

    try:
        text = LOG_FILE.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return {"lines": [], "total": 0}

    all_lines = text.splitlines()
    total = len(all_lines)
    tail = all_lines[-lines:] if lines < total else all_lines

    return {
        "lines": tail,
        "total": total,
    }
