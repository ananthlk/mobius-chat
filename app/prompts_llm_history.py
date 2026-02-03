"""Append-only history for prompts+LLM config. File-based: config/prompts_llm_history.jsonl."""
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.prompts_llm_config import compute_config_sha

logger = logging.getLogger(__name__)

_CHAT_ROOT = Path(__file__).resolve().parent.parent
_HISTORY_PATH = _CHAT_ROOT / "config" / "prompts_llm_history.jsonl"


def append_entry(config: dict[str, Any]) -> None:
    """Append one config snapshot to history (one JSON line per entry)."""
    sha = compute_config_sha(config)
    created_at = datetime.now(timezone.utc).isoformat()
    entry = {"config_sha": sha, "config_json": config, "created_at": created_at}
    _HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(_HISTORY_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, separators=(",", ":")) + "\n")
    logger.info("Appended config history entry config_sha=%s", sha)


def list_entries(limit: int = 50) -> list[dict[str, Any]]:
    """Return list of history entries, newest first: [{ config_sha, created_at }, ...]."""
    if not _HISTORY_PATH.exists():
        return []
    out: list[dict[str, Any]] = []
    try:
        lines = _HISTORY_PATH.read_text(encoding="utf-8").strip().split("\n")
        for line in reversed(lines[-limit:]):
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                out.append({
                    "config_sha": entry.get("config_sha", ""),
                    "created_at": entry.get("created_at", ""),
                })
            except json.JSONDecodeError:
                continue
        return out[:limit]
    except Exception as e:
        logger.warning("Failed to list config history from %s: %s", _HISTORY_PATH, e)
        return []


def get_by_sha(config_sha: str) -> dict[str, Any] | None:
    """Return full config dict for the given config_sha, or None if not found."""
    if not _HISTORY_PATH.exists() or not (config_sha or "").strip():
        return None
    sha = (config_sha or "").strip()
    try:
        for line in _HISTORY_PATH.read_text(encoding="utf-8").strip().split("\n"):
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                if entry.get("config_sha") == sha:
                    return entry.get("config_json")
            except json.JSONDecodeError:
                continue
    except Exception as e:
        logger.warning("Failed to read config history from %s: %s", _HISTORY_PATH, e)
    return None
