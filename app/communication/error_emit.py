"""Chat-side error boundary: raw exceptions → typed ErrorEnvelope + UI-safe string.

Problem this solves
-------------------
Raw provider exceptions (Groq 413/429, scraper 502, LLM refusals) were being
f-stringed directly into user-visible emit messages. Users saw things like:

    I couldn't answer this part: Groq API error 413: {"error":{"message":
    "Request too large for model `openai/gpt-oss-20b` in organization
    `org_01k9ff309tfmx9t836sxw6yrw2` ..."}}

leaking vendor IDs, org IDs, and quota internals.

This module is the single choke point chat uses to convert exceptions into
the canonical ``mobius_contracts.envelopes.ErrorEnvelope``. Every caller
(non_patient_rag, orchestrator, react_loop, tool_agent, …) funnels through
``classify_exception`` so the UI side and the ReAct loop can rely on a typed
shape instead of string-matching provider text.

Usage:

    from app.communication.error_emit import classify_exception

    try:
        ...
    except Exception as e:
        env = classify_exception(e, tool="search_corpus", round=rn)
        # Log the INTERNAL detail (org IDs, traceback, full JSON) — never emit it.
        logger.warning("tool failed: %s", env.internal_detail, exc_info=e)
        # Emit ONLY the user_facing_message; it's guaranteed to be clean.
        emit(env.user_facing_message)
        # Structured event for the FE / ReAct loop to branch on:
        emit_envelope(env)  # (future — requires an emit adapter that accepts dicts)
"""

from __future__ import annotations

import re
from typing import Any

from mobius_contracts.envelopes import ErrorCode, ErrorEnvelope

# ---- Pattern matchers -------------------------------------------------------

# Matches ``Retry-After: 20`` or ``try again in 20.21s`` formats seen from Groq.
_RETRY_AFTER_PATTERNS = (
    re.compile(r"retry[- ]after[:=]?\s*(\d+)", re.IGNORECASE),
    re.compile(r"try again in\s+(\d+(?:\.\d+)?)\s*s", re.IGNORECASE),
)

# Matches HTTP status codes in any surrounding punctuation: ``(502):``, ``'404 Not
# Found'``, `` 500 ``, ``502.``. Using a regex avoids the fragile "leading-space"
# heuristic of an earlier iteration.
_HTTP_4XX_RE = re.compile(r"(?<!\d)(4\d\d)(?!\d)")
_HTTP_5XX_RE = re.compile(r"(?<!\d)(5\d\d)(?!\d)")


def _parse_retry_after(text: str) -> int | None:
    for pat in _RETRY_AFTER_PATTERNS:
        m = pat.search(text)
        if m:
            try:
                return max(1, min(3600, int(float(m.group(1)))))
            except (TypeError, ValueError):
                continue
    return None


def _safe_truncate(s: str, limit: int) -> str:
    s = (s or "").strip()
    if len(s) <= limit:
        return s
    return s[: limit - 1].rstrip() + "…"


# ---- Classifier -------------------------------------------------------------


def classify_exception(
    exc: BaseException,
    *,
    tool: str | None = None,
    round: int | None = None,
) -> ErrorEnvelope:
    """Map a raw exception into a typed ErrorEnvelope with a safe user message.

    The mapping is intentionally conservative — if we can't identify the class
    of failure, we return a generic ``internal_error`` envelope with a bland
    user message. The raw exception *never* reaches ``user_facing_message``;
    it is preserved in ``internal_detail`` for logs / debugging only.
    """
    raw = _safe_truncate(f"{type(exc).__name__}: {exc}", 20_000)
    lower = raw.lower()
    _has_4xx = _HTTP_4XX_RE.search(raw) is not None
    _has_5xx = _HTTP_5XX_RE.search(raw) is not None

    # ── Content filtering (Vertex BLOCK_SAFETY, Anthropic 400 content policy) ─
    # Must come before the 4xx/5xx scans: a 400 content-filter hit would
    # otherwise fall through to internal_error (no URL → not scrape, not 5xx).
    _CF_SIGNALS = (
        "output blocked by content filtering",
        "content filtering policy",
        "vertexblockederror",  # VertexBlockedError re-raised after condensed-prompt retry
    )
    if any(sig in lower for sig in _CF_SIGNALS):
        return ErrorEnvelope(
            error_code="refusal",
            user_facing_message=(
                "This response was blocked by a content safety rule. "
                "Try rephrasing or asking a more specific question."
            ),
            internal_detail=raw,
            tool=tool,
            round=round,
        )

    # ── Rate limit (Groq 429, Anthropic TPM, generic 429) ────────────────────
    if (
        "rate_limit_exceeded" in lower
        or "rate limit" in lower
        or ("tokens per minute" in lower and ("limit" in lower and "used" in lower))
        or "429" in raw  # any 429 token — conservative but correct
    ):
        retry_s = _parse_retry_after(raw)
        msg = (
            f"The model is temporarily busy. Retrying in ~{retry_s}s."
            if retry_s
            else "The model is temporarily busy — trying another option."
        )
        return ErrorEnvelope(
            error_code="rate_limit",
            user_facing_message=msg,
            internal_detail=raw,
            retry_after_seconds=retry_s,
            tool=tool,
            round=round,
        )

    # ── Token/context budget (413 "Request too large") ───────────────────────
    if (
        "request too large" in lower
        or ("413" in raw and ("tokens" in lower or "context" in lower))
        or "context_length_exceeded" in lower
        or ("max_tokens" in lower and "exceed" in lower)
    ):
        return ErrorEnvelope(
            error_code="token_budget",
            user_facing_message=(
                "This question needs a larger context than the selected model "
                "can handle. Switching to a higher-capacity model."
            ),
            internal_detail=raw,
            tool=tool,
            round=round,
        )

    # ── Auth (401/403) ───────────────────────────────────────────────────────
    if (
        "unauthorized" in lower
        or "forbidden" in lower
        or "401" in raw
        or "invalid api key" in lower
    ) and "404" not in raw:  # 404 is scrape territory, not auth
        return ErrorEnvelope(
            error_code="auth_error",
            user_facing_message="A service is mis-configured. The team has been notified.",
            internal_detail=raw,
            tool=tool,
            round=round,
        )

    # ── Scrape / HTTP client errors ──────────────────────────────────────────
    # Characterized by HTTP status mentions + a URL, or an explicit "scrape"
    # keyword. Distinguished from provider 5xx further down by the presence of
    # "for url", "http", or "scrape".
    if (
        ("for url" in lower or "http://" in lower or "https://" in lower or "scrape" in lower)
        and (_has_4xx or _has_5xx)
    ):
        return ErrorEnvelope(
            error_code="scrape_failed",
            user_facing_message="Couldn't reach that web page — trying another source.",
            internal_detail=raw,
            tool=tool,
            round=round,
        )

    # ── Timeout ──────────────────────────────────────────────────────────────
    if "timeout" in lower or "timed out" in lower or isinstance(exc, TimeoutError):
        return ErrorEnvelope(
            error_code="timeout",
            user_facing_message="That step took too long — trying a different approach.",
            internal_detail=raw,
            retry_after_seconds=5,
            tool=tool,
            round=round,
        )

    # ── Provider 5xx (post-filter: didn't match scrape pattern above) ────────
    if _has_5xx:
        return ErrorEnvelope(
            error_code="provider_error",
            user_facing_message="The model service had a hiccup — retrying.",
            internal_detail=raw,
            retry_after_seconds=3,
            tool=tool,
            round=round,
        )

    # ── Validation (Pydantic / our own checks) ───────────────────────────────
    if type(exc).__name__ == "ValidationError" or "validation error" in lower:
        return ErrorEnvelope(
            error_code="validation_error",
            user_facing_message="That input didn't match what the tool expected.",
            internal_detail=raw,
            tool=tool,
            round=round,
        )

    # ── Fallback: something unclassified ─────────────────────────────────────
    return ErrorEnvelope(
        error_code="internal_error",
        user_facing_message="Something went wrong — trying another path.",
        internal_detail=raw,
        tool=tool,
        round=round,
    )


# ---- Emit-safe strings ------------------------------------------------------


def emit_line_for(env: ErrorEnvelope) -> str:
    """Short, user-safe emit line derived from an ErrorEnvelope.

    Used where callers previously did ``emit(f"I couldn't answer this part: {e}")``.
    Keeps the ``I couldn't answer this part:`` phrasing when helpful but never
    includes the raw exception body.
    """
    return env.user_facing_message


def tool_result_from_exception(
    exc: BaseException,
    *,
    tool: str,
    round: int | None = None,
) -> dict[str, Any]:
    """Normalized failed-tool result dict for the ReAct loop.

    ReAct's ``_execute_tool`` currently returns a dict shaped
    ``{"tool": str, "success": bool, "result": str, "sources": list, ...}``.
    This helper produces a matching dict for a failed attempt with a typed
    envelope attached at ``"error"`` so Phase 0.7 can reason over it without
    scraping the ``"result"`` string.
    """
    env = classify_exception(exc, tool=tool, round=round)
    return {
        "tool": tool,
        "success": False,
        "result": env.user_facing_message,  # safe to render
        "error": env.model_dump(),          # typed, for ReAct logic & telemetry
        "sources": [],
    }
