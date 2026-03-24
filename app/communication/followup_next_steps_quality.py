"""Filter noisy next_steps / follow-up questions; decide default expanded vs collapsed UI."""

from __future__ import annotations

import re
from typing import Any

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


def answer_card_needs_user_documents(answer_card: dict[str, Any] | None) -> bool:
    """True when the card explicitly lists missing variables the user must supply."""
    if not isinstance(answer_card, dict):
        return False
    rv = answer_card.get("required_variables")
    if not isinstance(rv, list):
        return False
    return any(isinstance(x, str) and x.strip() for x in rv)


def filter_next_steps_and_questions(
    next_steps: list[str],
    next_questions_for_user: list[str],
    *,
    response_sources: list[dict[str, Any]],
    answer_card: dict[str, Any] | None,
) -> tuple[list[str], list[str]]:
    """
    Drop document-upload style suggestions when we already have corpus sources and the
    answer card is not asking for required_variables.
    """
    corpus = has_corpus_sources(response_sources)
    need_docs = answer_card_needs_user_documents(answer_card)
    strip_doc_asks = corpus and not need_docs

    out_steps: list[str] = []
    for x in next_steps or []:
        if not isinstance(x, str) or not x.strip():
            continue
        t = x.strip()
        if strip_doc_asks and _RE_NEXT_STEP_DOC_BOILERPLATE.search(t):
            continue
        out_steps.append(t)

    out_q: list[str] = []
    for x in next_questions_for_user or []:
        if not isinstance(x, str) or not x.strip():
            continue
        t = x.strip()
        if strip_doc_asks and _RE_REDUNDANT_DOC_ASK.search(t):
            continue
        out_q.append(t)

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
