"""ReAct decision parsing (Phase 1i pass 1 — extracted from react_loop.py).

Pure, context-free helpers for pulling a structured decision dict out of
the reasoning model's free-text response. Each reasoning round emits a
JSON object describing the next tool call (``{thought, tool, inputs,
is_complete}``); real-world LLM output is rarely clean JSON — it may
arrive wrapped in ```json fences, include trailing commas, have
unescaped newlines, or embed the JSON inside markdown prose. This
module is where the tolerance lives.

Four tiers tried in order (see ``_parse_react_decision_json``):
  1. Strip triple-backtick json fence if present.
  2. ``json.loads`` the stripped text verbatim.
  3. ``json_repair.loads`` for common LLM hiccups (trailing commas etc).
  4. Extract the first balanced ``{...}`` block and re-run steps 2-3 on it.

If all four miss, the ReAct loop stops — but for one narrow class of
asks ("NPIs for <org>") we keep a heuristic fallback in
:func:`_react_fallback_org_npi_lookup_decision` so a mangled planner
response still routes to ``lookup_npi`` instead of a blank refusal.

These functions have NO imports from app.pipeline.react_loop to keep
the module independently testable. The only inbound dep is
``PipelineContext`` for the fallback heuristic, which reads the user's
message text.
"""

from __future__ import annotations

import json
import logging
import re

from app.pipeline.context import PipelineContext

logger = logging.getLogger(__name__)


def _strip_markdown_json_fence(s: str) -> str:
    """Remove ```json ... ``` wrapper if present."""
    t = s.strip()
    if not t.startswith("```"):
        return t
    lines = t.split("\n")
    if len(lines) >= 2 and lines[0].strip().startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _extract_balanced_json_object(text: str) -> str | None:
    """
    First top-level `{ ... }` with brace depth outside of JSON strings.
    Avoids greedy ``\\{.*\\}`` which breaks when values contain ``}`` (e.g. markdown).
    """
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escape = False
    i = start
    while i < len(text):
        c = text[i]
        if escape:
            escape = False
            i += 1
            continue
        if in_string:
            if c == "\\":
                escape = True
            elif c == '"':
                in_string = False
            i += 1
            continue
        if c == '"':
            in_string = True
            i += 1
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
        i += 1
    return None


def _parse_react_decision_dict_obj(text: str) -> dict | None:
    """Try stdlib json.loads then json_repair (LLMs often emit trailing commas, etc.)."""
    t = (text or "").strip()
    if not t:
        return None
    try:
        obj = json.loads(t)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass
    try:
        import json_repair

        obj = json_repair.loads(t)
        if isinstance(obj, dict):
            return obj
    except Exception as e:
        logger.debug("ReAct decision json_repair failed: %s", e)
    return None


def _parse_react_decision_json(decision_raw: str) -> dict | None:
    """
    Parse reasoning-round JSON. Returns None if parsing fails (caller may stop the loop).
    """
    raw = (decision_raw or "").strip()
    if not raw:
        return None
    stripped = _strip_markdown_json_fence(raw)
    for candidate in (stripped, raw):
        obj = _parse_react_decision_dict_obj(candidate)
        if obj is not None:
            return obj
        extracted = _extract_balanced_json_object(candidate)
        if extracted:
            obj = _parse_react_decision_dict_obj(extracted)
            if obj is not None:
                return obj
            logger.warning(
                "ReAct decision JSON failed after balanced extract (first 240 chars): %s",
                extracted[:240],
            )
    return None


_ORG_NPI_NAME_LOOKUP_HINT = re.compile(
    r"(?:^|\b)(?:find|look|lookup|list|search|get|show)\s+(?:the\s+)?npis?\s+for\s+",
    re.I,
)


def _react_fallback_org_npi_lookup_decision(ctx: PipelineContext) -> dict | None:
    """If the reasoning model returns unusable text, still route clear 'NPIs for Org' asks to lookup_npi."""
    m = (ctx.effective_message or ctx.message or "").strip()
    if not m:
        return None
    mm = _ORG_NPI_NAME_LOOKUP_HINT.search(m)
    if not mm:
        return None
    if re.search(r"\b\d{10}\b", m):
        return None
    tail = m[mm.end() :].strip().rstrip("?.!")
    tail = re.split(r"\s+and\s+i\s+can\b", tail, maxsplit=1, flags=re.I)[0].strip()
    tail = re.split(r"\s+so\s+(?:that|i)\s+can\b", tail, maxsplit=1, flags=re.I)[0].strip()
    if len(tail) < 2:
        return None
    if len(tail) > 100:
        tail = tail[:100].strip()
    return {
        "thought": "Fallback: user asked for organization NPI(s) by name.",
        "tool": "lookup_npi",
        "inputs": {"org_name": tail},
        "is_complete": False,
    }
