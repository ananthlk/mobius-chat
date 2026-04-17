"""Normalize integrator JSON so direct_answer never contains raw nested JSON (JSON bleed)."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

_MAX_RECURSE = 4

# Phase 0.12: the fallback string shown when the integrator's JSON output is
# unparseable. Softer than the prior "Something went wrong. Please try again,
# or start a new chat." — that message conflated a transient formatting issue
# with a "the whole thing is broken" failure mode, and nudged users into a
# destructive action (start over) when rephrasing would usually work.
DEFAULT_BLEED_FALLBACK = (
    "I had trouble formatting the answer. Please try rephrasing your question."
)


def _log_fallback(site: str, raw: str) -> None:
    """Structured log line for every fallback fire — so integrator parse
    failures are debuggable in production instead of silently swallowed.

    We log the first 400 chars of the raw input at WARNING so a grep of
    "integrator_fallback site=" surfaces every incident with enough context
    to reproduce.
    """
    preview = (raw or "")[:400].replace("\n", "\\n")
    logger.warning(
        "integrator_fallback site=%s raw_preview=%r raw_len=%d",
        site,
        preview,
        len(raw or ""),
    )


def _strip_fences_and_json_prefix(raw: str) -> str:
    s = (raw or "").strip()
    if s.lower().startswith("json "):
        s = s[5:].lstrip()
    if s.startswith("```"):
        lines = s.split("\n")
        if lines and lines[0].strip().startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        s = "\n".join(lines).strip()
    return s


def _human_from_parsed(obj: dict[str, Any]) -> str | None:
    if not isinstance(obj, dict):
        return None
    ans = obj.get("answer")
    if isinstance(ans, str) and ans.strip():
        s = sanitize_direct_answer_string(ans)
        if s.strip() and not looks_like_raw_json_bleed(s):
            return s
    da = obj.get("direct_answer")
    if isinstance(da, str) and da.strip():
        s = sanitize_direct_answer_string(da)
        if s.strip() and not looks_like_raw_json_bleed(s):
            return s
    msg = obj.get("message")
    if isinstance(msg, str) and msg.strip():
        s = sanitize_direct_answer_string(msg)
        if s.strip() and not looks_like_raw_json_bleed(s):
            return s
    res = obj.get("resolutions")
    if isinstance(res, list) and res:
        parts: list[str] = []
        for item in res:
            if not isinstance(item, dict):
                continue
            r = item.get("resolution")
            if isinstance(r, str) and r.strip():
                parts.append(r.strip())
                continue
            if isinstance(r, dict):
                inner_da = r.get("direct_answer")
                if isinstance(inner_da, str) and inner_da.strip():
                    parts.append(inner_da.strip())
        if parts:
            return "\n\n".join(parts)
    return None


def sanitize_direct_answer_string(da: str, *, depth: int = 0) -> str:
    """If direct_answer is nested JSON or fenced JSON, extract human text; else return stripped da."""
    if not isinstance(da, str) or not da.strip():
        return (da or "").strip()
    if depth > _MAX_RECURSE:
        logger.warning("sanitize_direct_answer_string: max recurse depth")
        return da.strip()[:5000]
    cleaned = _strip_fences_and_json_prefix(da)
    if not cleaned.startswith("{"):
        return cleaned.strip()
    try:
        parsed = json.loads(cleaned)
    except (json.JSONDecodeError, TypeError, ValueError):
        return da.strip()
    if isinstance(parsed, dict):
        human = _human_from_parsed(parsed)
        if human:
            return sanitize_direct_answer_string(human, depth=depth + 1)
        # Valid JSON object but no safe human field — never return raw JSON
        return ""
    if isinstance(parsed, list):
        parts = [str(x).strip() for x in parsed if isinstance(x, str) and str(x).strip()]
        if parts:
            return sanitize_direct_answer_string("\n".join(parts), depth=depth + 1)
        return ""
    return da.strip()[:5000]


def sanitize_answer_card_dict(card: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of AnswerCard-shaped dict with scrubbed direct_answer."""
    out = dict(card)
    da = out.get("direct_answer")
    if isinstance(da, str):
        out["direct_answer"] = sanitize_direct_answer_string(da)
    return out


def finalize_answer_card_json_for_client(
    display_message: str,
    *,
    fallback_text: str = DEFAULT_BLEED_FALLBACK,
) -> str:
    """Scrub AnswerCard JSON for API clients: nested JSON in direct_answer removed; bleed → fallback."""
    if not (display_message or "").strip():
        return display_message
    try:
        o = json.loads(display_message)
        if not isinstance(o, dict):
            return display_message
        if o.get("mode") not in ("FACTUAL", "CANONICAL", "BLENDED"):
            return display_message
        if "direct_answer" not in o or "sections" not in o:
            return display_message
        fixed = sanitize_answer_card_dict(o)
        da = fixed.get("direct_answer")
        if not isinstance(da, str) or not da.strip():
            logger.warning("finalize_answer_card: empty direct_answer after sanitize; using fallback")
            fixed["direct_answer"] = fallback_text
            return json.dumps(fixed)
        if looks_like_raw_json_bleed(da):
            logger.warning("finalize_answer_card: direct_answer still looks like JSON bleed; using fallback")
            fixed["direct_answer"] = fallback_text
            return json.dumps(fixed)
        return json.dumps(fixed)
    except (json.JSONDecodeError, TypeError, ValueError):
        return display_message


def display_text_for_parsed_answer_card(parsed: dict[str, Any]) -> str:
    """Plain text to stream/store for a validated AnswerCard dict (scrub DA; else resolutions/message)."""
    if not isinstance(parsed, dict):
        _log_fallback("display_text_for_parsed_answer_card.not_dict", repr(parsed)[:400])
        return DEFAULT_BLEED_FALLBACK
    raw = str(parsed.get("direct_answer") or "")
    clean = sanitize_direct_answer_string(raw)
    if clean.strip() and not looks_like_raw_json_bleed(clean):
        return clean.strip()[:8000]
    alt = _human_from_parsed(parsed)
    if alt:
        c2 = sanitize_direct_answer_string(alt)
        if c2.strip() and not looks_like_raw_json_bleed(c2):
            return c2.strip()[:8000]
        if alt.strip() and not looks_like_raw_json_bleed(alt):
            return alt.strip()[:8000]
    if not raw.strip():
        return ""
    _log_fallback("display_text_for_parsed_answer_card.bleed", raw)
    return DEFAULT_BLEED_FALLBACK


def parse_loose_integrator_json(raw: str) -> dict[str, Any] | None:
    """Parse integrator output as a JSON object when strict AnswerCard validation failed."""
    s = _strip_fences_and_json_prefix(raw or "")
    if not s.startswith("{"):
        return None
    try:
        o = json.loads(s)
        if isinstance(o, dict):
            return o
    except (json.JSONDecodeError, TypeError, ValueError):
        pass
    try:
        import json_repair

        o = json_repair.loads(s)
        return o if isinstance(o, dict) else None
    except Exception:
        return None


def build_minimal_answer_card_preserving_metadata(
    visible_direct_answer: str,
    raw_integrator_text: str,
) -> dict[str, Any]:
    """Minimal AnswerCard for invalid strict parse; keep sections, follow-ups, resolutions from raw JSON."""
    extra = parse_loose_integrator_json(raw_integrator_text) or {}
    mode = extra.get("mode") if extra.get("mode") in ("FACTUAL", "CANONICAL", "BLENDED") else "FACTUAL"
    sections_out: list[dict[str, Any]] = []
    secs = extra.get("sections")
    if isinstance(secs, list):
        for item in secs:
            sec = dict(item) if isinstance(item, dict) else {}
            if not sec.get("label") and sec.get("title"):
                sec["label"] = str(sec.get("title", ""))
            sections_out.append(sec)
    out: dict[str, Any] = {
        "mode": mode,
        "direct_answer": visible_direct_answer,
        "sections": sections_out,
    }
    if isinstance(extra.get("resolutions"), list):
        out["resolutions"] = extra["resolutions"]
    for key in ("closed_task_ids", "open_task_ids", "next_steps", "next_questions_for_user", "cited_source_indices"):
        v = extra.get(key)
        if isinstance(v, list):
            out[key] = v
    if isinstance(extra.get("ui_blocks"), list):
        out["ui_blocks"] = extra["ui_blocks"]
    ov = extra.get("source_confidence_override")
    if isinstance(ov, str) and ov.strip():
        out["source_confidence_override"] = ov.strip()
    return out


def extract_user_visible_text_from_integrator_raw(raw: str) -> str:
    """When the integrator output is not a valid AnswerCard, still extract prose — never return raw JSON."""
    s = _strip_fences_and_json_prefix(raw or "")
    if not s.startswith("{"):
        return s.strip()[:8000]
    try:
        parsed = json.loads(s)
    except (json.JSONDecodeError, TypeError, ValueError):
        _log_fallback("extract_user_visible_text_from_integrator_raw.invalid_json", s)
        return DEFAULT_BLEED_FALLBACK
    if not isinstance(parsed, dict):
        _log_fallback("extract_user_visible_text_from_integrator_raw.not_dict", s)
        return DEFAULT_BLEED_FALLBACK
    if parsed.get("mode") in ("FACTUAL", "CANONICAL", "BLENDED") and isinstance(parsed.get("direct_answer"), str):
        da = sanitize_direct_answer_string(parsed["direct_answer"])
        if da.strip() and not looks_like_raw_json_bleed(da):
            return da.strip()[:8000]
    h = _human_from_parsed(parsed)
    if h:
        out = sanitize_direct_answer_string(h)
        if out.strip() and not looks_like_raw_json_bleed(out):
            return out.strip()[:8000]
        if h.strip() and not looks_like_raw_json_bleed(h):
            return h.strip()[:8000]
    _log_fallback("extract_user_visible_text_from_integrator_raw.no_usable_text", s)
    return DEFAULT_BLEED_FALLBACK


def looks_like_raw_json_bleed(text: str) -> bool:
    """Heuristic: user-visible string that is mostly a JSON object."""
    t = (text or "").strip()
    if not t.startswith("{"):
        return False
    if len(t) < 30:
        return False
    if re.search(r'"mode"\s*:\s*"(FACTUAL|CANONICAL|BLENDED)"', t):
        return True
    if '"direct_answer"' in t[:800] and '"sections"' in t[:800]:
        return True
    if '"resolutions"' in t[:400]:
        return True
    return False


def plain_text_for_adjudication_from_chat_message(message: str, *, max_chars: int = 12_000) -> str:
    """
    Stored assistant `message` is often AnswerCard JSON (`format_response` returns json.dumps(card)).
    The UI parses that JSON and shows `direct_answer` plus section labels/bullets — not the raw wire object.

    LLM adjudication must receive the same kind of plain text a user sees; otherwise it falsely flags
    json_compliance and claims `sections` is empty when those details live in `direct_answer`.
    """
    s = (message or "").strip()
    if not s:
        return ""
    if not (s.startswith("{") and '"mode"' in s):
        return s[:max_chars]

    try:
        o = json.loads(s)
    except (json.JSONDecodeError, TypeError, ValueError):
        return s[:max_chars]

    if not isinstance(o, dict) or o.get("mode") not in ("FACTUAL", "CANONICAL", "BLENDED"):
        return s[:max_chars]

    parts: list[str] = []
    body = display_text_for_parsed_answer_card(o)
    if body.strip():
        parts.append(body.strip())

    secs = o.get("sections")
    if isinstance(secs, list):
        for sec in secs:
            if not isinstance(sec, dict):
                continue
            label = str(sec.get("label") or sec.get("title") or "").strip()
            bullets = sec.get("bullets")
            block: list[str] = []
            if label:
                block.append(label)
            if isinstance(bullets, list):
                for b in bullets:
                    if isinstance(b, str) and b.strip():
                        block.append("- " + b.strip())
            if block:
                parts.append("\n".join(block))

    out = "\n\n".join(parts).strip()
    if out:
        return out[:max_chars]
    fallback = extract_user_visible_text_from_integrator_raw(s).strip()
    return (fallback or s)[:max_chars]
