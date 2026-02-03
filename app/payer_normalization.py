"""Load payer normalization from config file. Maps aliases/subsidiary names to canonical payer token for RAG filtering."""
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_chat_root = Path(__file__).resolve().parent.parent
_DEFAULT_CONFIG_PATH = _chat_root / "config" / "payer_normalization.yaml"

# alias (lower) -> canonical
_alias_to_canonical: dict[str, str] | None = None


def _load_config() -> dict[str, str]:
    """Load payer_normalization.yaml and build alias -> canonical map. Returns empty dict on missing/error."""
    global _alias_to_canonical
    if _alias_to_canonical is not None:
        return _alias_to_canonical
    path_str = os.environ.get("PAYER_NORMALIZATION_CONFIG", "").strip()
    path = Path(path_str) if path_str else _DEFAULT_CONFIG_PATH
    if not path.is_file():
        logger.info("Payer normalization config not found at %s; RAG payer filter will use raw state value.", path)
        _alias_to_canonical = {}
        return _alias_to_canonical
    try:
        import yaml
        raw = yaml.safe_load(path.read_text()) or {}
        payers = raw.get("payers") or []
        out: dict[str, str] = {}
        for entry in payers:
            if not isinstance(entry, dict):
                continue
            canonical = (entry.get("canonical") or "").strip()
            if not canonical:
                continue
            aliases = entry.get("aliases") or []
            if not isinstance(aliases, list):
                continue
            for a in aliases:
                key = (a or "").strip().lower()
                if key:
                    out[key] = canonical
            out[canonical.strip().lower()] = canonical
        _alias_to_canonical = out
        logger.info("Loaded payer normalization from %s: %d alias(es) -> %d canonical payers.", path, len(out), len({v for v in out.values()}))
    except Exception as e:
        logger.warning("Failed to load payer normalization from %s: %s", path, e)
        _alias_to_canonical = {}
    return _alias_to_canonical


def normalize_payer_for_rag(rawname: str | None) -> str | None:
    """Map payer name (from state or user) to canonical token for RAG document_payer filter.
    If rawname is None or empty, returns None. If no mapping, returns rawname unchanged."""
    if not rawname or not (rawname := rawname.strip()):
        return None
    mapping = _load_config()
    if not mapping:
        return rawname
    canonical = mapping.get(rawname.lower())
    return canonical if canonical else rawname


def detect_payer_from_text(text: str) -> str | None:
    """Detect payer from user message using config aliases. Longer aliases checked first so 'United Healthcare' matches before 'United'.
    Returns canonical payer name or None if no alias appears in text. Used by state extractor so state and RAG stay in sync."""
    found = detect_all_payers_from_text(text)
    return found[0] if found else None


def detect_all_payers_from_text(text: str) -> list[str]:
    """Detect all payers mentioned in user message. Returns list of canonical names (order preserved, deduped).
    When user says 'compare Sunshine, United and Molina', returns ['Sunshine Health', 'United Healthcare', 'Molina']
    so RAG can filter by all (allow_tokens OR)."""
    if not text or not (text := text.strip()):
        return []
    mapping = _load_config()
    if not mapping:
        return []
    t = text.lower()
    # Build (alias_lower, canonical) and sort by alias length desc so longer matches first
    pairs: list[tuple[str, str]] = [(k, v) for k, v in mapping.items()]
    pairs.sort(key=lambda x: -len(x[0]))
    seen: set[str] = set()
    result: list[str] = []
    for alias_lower, canonical in pairs:
        if alias_lower in t and canonical not in seen:
            seen.add(canonical)
            result.append(canonical)
    return result
