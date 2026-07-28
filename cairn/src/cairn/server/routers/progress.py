from pathlib import Path

from fastapi import APIRouter

router = APIRouter(tags=["progress"])

PROGRESS_DIR = Path("/tmp/cairn-progress")


@router.get("/progress/{project_id}")
def get_project_progress(project_id: str):
    """Return the real-time progress output for a running project."""
    log_path = PROGRESS_DIR / f"{project_id}.log"
    if not log_path.exists():
        return {"lines": []}

    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return {"lines": []}

    lines = text.splitlines()
    # Keep last 100 lines to avoid huge responses
    if len(lines) > 100:
        lines = lines[-100:]

    return {"lines": lines}
