"""Load/save prompts+LLM config from config/prompts_llm.yaml and compute deterministic SHA for audit."""
import hashlib
import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_CHAT_ROOT = Path(__file__).resolve().parent.parent
_CONFIG_PATH = _CHAT_ROOT / "config" / "prompts_llm.yaml"

# Top-level keys we care about for SHA (order for canonical serialization)
_TOP_KEYS = ("llm", "parser", "prompts")


def _deep_sort(obj: Any) -> Any:
    """Return a copy with all dict keys sorted for deterministic serialization."""
    if isinstance(obj, dict):
        return {k: _deep_sort(obj[k]) for k in sorted(obj.keys())}
    if isinstance(obj, list):
        return [_deep_sort(x) for x in obj]
    return obj


def _canonical_bytes(config: dict[str, Any]) -> bytes:
    """Serialize config to canonical JSON (sorted keys, no extra whitespace) for hashing."""
    trimmed: dict[str, Any] = {}
    for key in _TOP_KEYS:
        if key in config:
            trimmed[key] = config[key]
    return json.dumps(_deep_sort(trimmed), sort_keys=True, separators=(",", ":")).encode("utf-8")


def compute_config_sha(config: dict[str, Any]) -> str:
    """Compute SHA-256 of canonical config; return first 12 hex chars for readability."""
    digest = hashlib.sha256(_canonical_bytes(config)).hexdigest()
    return digest[:12]


def load_prompts_llm_config() -> tuple[dict[str, Any], str]:
    """Load config from config/prompts_llm.yaml. Returns (config_dict, config_sha).
    If file is missing, returns ({}, '').
    """
    if not _CONFIG_PATH.exists():
        logger.debug("prompts_llm config not found at %s", _CONFIG_PATH)
        return {}, ""

    try:
        import yaml
        raw = _CONFIG_PATH.read_text(encoding="utf-8")
        data = yaml.safe_load(raw)
        if not isinstance(data, dict):
            logger.warning("prompts_llm.yaml root is not a dict: %s", type(data))
            return {}, ""
        config = {k: data[k] for k in _TOP_KEYS if k in data}
        sha = compute_config_sha(config)
        return config, sha
    except Exception as e:
        logger.warning("Failed to load prompts_llm config from %s: %s", _CONFIG_PATH, e)
        return {}, ""


def save_prompts_llm_config(config: dict[str, Any]) -> str:
    """Merge config with existing (if any), write to config/prompts_llm.yaml, return new config_sha."""
    existing, _ = load_prompts_llm_config()
    merged: dict[str, Any] = {}
    for key in _TOP_KEYS:
        base = existing.get(key) or {}
        if isinstance(base, dict) and isinstance(config.get(key), dict):
            merged[key] = {**base, **config[key]}
        elif config.get(key) is not None:
            merged[key] = config[key]
        elif key in existing:
            merged[key] = existing[key]

    try:
        import yaml
        _CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(_CONFIG_PATH, "w", encoding="utf-8") as f:
            yaml.safe_dump(merged, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
        sha = compute_config_sha(merged)
        logger.info("Saved prompts_llm config to %s config_sha=%s", _CONFIG_PATH, sha)
        return sha
    except Exception as e:
        logger.exception("Failed to save prompts_llm config to %s: %s", _CONFIG_PATH, e)
        raise
