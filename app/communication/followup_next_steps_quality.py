"""Filter noisy next_steps / follow-up questions; decide default expanded vs collapsed UI."""

from __future__ import annotations

import re
from typing import Any

# Normalized item shape for API + envelope: {"text": str, "clickable": bool}

# When we already have RAG hits and the card does not ask for missing user inputs,
# strip meta-requests that ask the user to supply documents (common LLM boilerplate).
_RE_REDUNDANT_DOC_ASK = re.compile(
    r"(?i)\b("
    r"can you (please )?(upload|attach|send|share)|"
    r"could you (please )?(upload|attach|send|share)|"
    r"would you (please )?(upload|attach|send|share)|"
    r"do you have (a |any )?(document|doc|file|pdf|link)|"
    r"please (upload|attach|send|share)|"
    r"upload (a |your )?(document|doc|file|pdf)|"
    r"attach (a |your )?(document|doc|file|pdf)|"
    r"share (a |the )?(document|doc|file|link)|"
    r"provide (a |the )?(document|doc|link|file)|"
    r"send (us )?(a |the )?(document|doc|file)"
    r")\b"
)

# Imperative next_steps that are rarely policy-backed when corpus already answered.
_RE_NEXT_STEP_DOC_BOILERPLATE = re.compile(
    r"(?i)^\s*("
    r"upload (a |your )?(document|doc|file)|"
    r"attach (a |your )?(document|doc|file)|"
    r"send (us )?(a |your )?(document|doc|file)|"
    r"provide (a |the )?(document|doc|link)"
    r")\b"
)


def has_corpus_sources(sources: list[dict[str, Any]] | None) -> bool:
    """True if at least one RAG / manual hit with a document id."""
    for s in sources or []:
        if not isinstance(s, dict):
            continue
        did = s.get("document_id")
        if did is not None and str(did).strip():
            return True
    return False


def normalize_followup_line_item(raw: Any, *, default_clickable: bool) -> dict[str, Any] | None:
    """Coerce integrator output to ``{"text": str, "clickable": bool}``.

    - Plain string → ``text`` trimmed, ``clickable`` = ``default_clickable``.
    - Object → ``text`` from ``text`` | ``label`` | ``line``; ``clickable`` from
      ``clickable`` or ``tap_to_send`` if present, else ``default_clickable``.
    """
    if isinstance(raw, str):
        t = raw.strip()
        if not t:
            return None
        return {"text": t[:500], "clickable": default_clickable}
    if isinstance(raw, dict):
        text = raw.get("text") or raw.get("label") or raw.get("line")
        if not isinstance(text, str) or not text.strip():
            return None
        clickable: bool
        if "clickable" in raw:
            clickable = bool(raw.get("clickable"))
        elif "tap_to_send" in raw:
            clickable = bool(raw.get("tap_to_send"))
        else:
            clickable = default_clickable
        return {"text": text.strip()[:500], "clickable": clickable}
    return None


def normalize_followup_line_list(items: list[Any], *, default_clickable: bool) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for x in items or []:
        n = normalize_followup_line_item(x, default_clickable=default_clickable)
        if n:
            out.append(n)
    return out


def answer_card_needs_user_documents(answer_card: dict[str, Any] | None) -> bool:
    """True when the card explicitly lists missing variables the user must supply."""
    if not isinstance(answer_card, dict):
        return False
    rv = answer_card.get("required_variables")
    if not isinstance(rv, list):
        return False
    return any(isinstance(x, str) and x.strip() for x in rv)


def filter_next_steps_and_questions(
    next_steps: list[dict[str, Any]],
    next_questions_for_user: list[dict[str, Any]],
    *,
    response_sources: list[dict[str, Any]],
    answer_card: dict[str, Any] | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """
    Drop document-upload style suggestions when we already have corpus sources and the
    answer card is not asking for required_variables.

    Each list element is ``{"text": str, "clickable": bool}`` (from
    :func:`normalize_followup_line_list`).
    """
    corpus = has_corpus_sources(response_sources)
    need_docs = answer_card_needs_user_documents(answer_card)
    strip_doc_asks = corpus and not need_docs

    out_steps: list[dict[str, Any]] = []
    for item in next_steps or []:
        if not isinstance(item, dict):
            continue
        t = (item.get("text") or "").strip()
        if not t:
            continue
        if strip_doc_asks and _RE_NEXT_STEP_DOC_BOILERPLATE.search(t):
            continue
        out_steps.append(dict(item))

    out_q: list[dict[str, Any]] = []
    for item in next_questions_for_user or []:
        if not isinstance(item, dict):
            continue
        t = (item.get("text") or "").strip()
        if not t:
            continue
        if strip_doc_asks and _RE_REDUNDANT_DOC_ASK.search(t):
            continue
        out_q.append(dict(item))

    return out_steps, out_q


def followup_blocks_collapsed_default(source_confidence_strip: str) -> bool:
    """
    When True, UI should render next_steps / suggested_questions inside a collapsed <details>.
    Strong corpus badges → expanded (False). Weak / web-only → collapsed (True).
    """
    s = (source_confidence_strip or "").strip().lower()
    if s in ("approved_authoritative", "approved_informational"):
        return False
    return True
