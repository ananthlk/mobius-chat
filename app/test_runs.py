"""Append-only store for named full-pipeline test runs. File-based: config/test_runs.jsonl."""
import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_CHAT_ROOT = Path(__file__).resolve().parent.parent
_STORE_PATH = _CHAT_ROOT / "config" / "test_runs.jsonl"

_SNIPPET_LEN = 120


def _snippet(text: str | None, max_len: int = _SNIPPET_LEN) -> str:
    if not text or not text.strip():
        return ""
    s = (text or "").strip()
    if len(s) <= max_len:
        return s
    return s[: max_len - 3].rstrip() + "..."


def append_run(
    *,
    name: str = "",
    description: str = "",
    config_sha: str = "",
    message: str = "",
    reply: str = "",
    model_used: str | None = None,
    duration_ms: int | None = None,
    stages: dict[str, Any] | None = None,
) -> str:
    """Append one named test run. Returns run id (uuid)."""
    run_id = str(uuid.uuid4())
    created_at = datetime.now(timezone.utc).isoformat()
    entry: dict[str, Any] = {
        "id": run_id,
        "name": (name or "").strip(),
        "description": (description or "").strip(),
        "config_sha": (config_sha or "").strip(),
        "message": (message or "").strip(),
        "reply": (reply or "").strip(),
        "created_at": created_at,
    }
    if model_used is not None:
        entry["model_used"] = model_used
    if duration_ms is not None:
        entry["duration_ms"] = duration_ms
    if stages is not None:
        entry["stages"] = stages
    _STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(_STORE_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, separators=(",", ":")) + "\n")
    logger.info("Appended test run id=%s name=%r", run_id, entry.get("name"))
    return run_id


def list_runs(limit: int = 50) -> list[dict[str, Any]]:
    """Return list of test runs, newest first: [{ id, name, description, config_sha, message_snippet, reply_snippet, created_at }, ...]."""
    if not _STORE_PATH.exists():
        return []
    out: list[dict[str, Any]] = []
    try:
        lines = _STORE_PATH.read_text(encoding="utf-8").strip().split("\n")
        for line in reversed(lines[-limit:]):
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                out.append({
                    "id": entry.get("id", ""),
                    "name": entry.get("name", ""),
                    "description": entry.get("description", ""),
                    "config_sha": entry.get("config_sha", ""),
                    "message_snippet": _snippet(entry.get("message")),
                    "reply_snippet": _snippet(entry.get("reply")),
                    "created_at": entry.get("created_at", ""),
                })
            except json.JSONDecodeError:
                continue
        return out[:limit]
    except Exception as e:
        logger.warning("Failed to list test runs from %s: %s", _STORE_PATH, e)
        return []


def get_run(run_id: str) -> dict[str, Any] | None:
    """Return full run dict for the given id, or None if not found."""
    if not _STORE_PATH.exists() or not (run_id or "").strip():
        return None
    rid = (run_id or "").strip()
    try:
        for line in _STORE_PATH.read_text(encoding="utf-8").strip().split("\n"):
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                if entry.get("id") == rid:
                    return entry
            except json.JSONDecodeError:
                continue
    except Exception as e:
        logger.warning("Failed to read test runs from %s: %s", _STORE_PATH, e)
    return None
