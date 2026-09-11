"""Exact per-tokenizer token counts for prompt_blocks.template_body.

Governor seat's estimate() correction (2026-09-10, docs/governor-prompt-
budget.md): a chars//4 heuristic's error is content-and-tokenizer-dependent
(measured both directions on real block content in the session that
produced this module — 3% over on Gemini for prose-heavy content, 5% under
on Gemini for manifest-shaped text elsewhere), not a fixed correction
factor. Blocks are immutable once versioned (053's append-only design), so
counting once at publish time and storing the result is strictly better
than any heuristic, forever after, in either direction.

Stored as a MAP (tokenizer_id -> count), not a single integer, because a
composition can be served by whichever model the bandit routes to
(Vertex/Anthropic/Groq all live candidates per model_registry's roster) and
a block's real token count differs by tokenizer. Governor's ruling on how
to consume this: budget using MAX across the stored counts for the
candidate pool, not the count for whichever model the bandit eventually
picks — the bound must hold before that choice is made, and an under-count
fails open (permits a prompt that doesn't fit) while an over-count only
fails closed (conservative). This module only produces the stored counts;
the MAX-based budgeting logic is the governor's, not this module's.
"""
from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)

# Only "gemini" is wired today — it's the only tokenizer with a live,
# already-authenticated call path in this codebase (Vertex's countTokens
# REST endpoint, same project/region as every other Vertex call). Adding
# "anthropic" here means wiring its own count-tokens endpoint (beta
# messages.count_tokens); Groq/OpenAI-compatible has no equivalent endpoint
# as of this writing, so it will keep falling back to chars//4 with
# provenance recorded (see fallback_estimate_tokens below) until/unless a
# real tokenizer library is vendored for it. Adding a tokenizer to this set
# is additive — existing stored counts for other tokenizers aren't touched.
LIVE_TOKENIZERS: tuple[str, ...] = ("gemini",)

_COUNT_TOKENS_MODEL = "gemini-2.5-flash"  # any Gemini model shares one tokenizer


def _vertex_project_and_region() -> tuple[str, str]:
    """Same resolution order as llm_provider._vertex_factory, so this module
    never talks to a different project than the one actually serving
    requests. Not imported directly -- duplicated on purpose, matching
    governor.py's own precedent of duplicating rather than importing across
    a process-entrypoint/library boundary."""
    project_id = (
        os.getenv("VERTEX_PROJECT_ID") or os.getenv("CHAT_VERTEX_PROJECT_ID") or "mobiusos-new"
    ).strip()
    try:
        from app.chat_config import get_chat_config
        location = get_chat_config().llm.vertex_location or "us-central1"
    except Exception:
        location = "us-central1"
    return project_id, location


def count_tokens_gemini(text: str) -> int | None:
    """Exact token count via Vertex's countTokens endpoint. None on any
    failure (auth, network, quota) -- callers must treat None as "no count
    available," not zero, and fall back accordingly."""
    project, region = _vertex_project_and_region()
    try:
        import google.auth
        import google.auth.transport.requests

        creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        creds.refresh(google.auth.transport.requests.Request())
        token = creds.token
    except Exception as e:
        logger.warning("count_tokens_gemini: failed to get access token: %s", e)
        return None

    url = (
        f"https://{region}-aiplatform.googleapis.com/v1/projects/{project}"
        f"/locations/{region}/publishers/google/models/{_COUNT_TOKENS_MODEL}:countTokens"
    )
    body = json.dumps({"contents": [{"role": "user", "parts": [{"text": text}]}]}).encode("utf-8")
    request = urllib.request.Request(
        url, data=body,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10.0) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return int(data["totalTokens"])
    except (urllib.error.URLError, KeyError, ValueError, TypeError) as e:
        logger.warning("count_tokens_gemini: countTokens call failed: %s", e)
        return None


def compute_token_counts(text: str) -> dict[str, int]:
    """Compute {tokenizer_id: count} across every LIVE_TOKENIZERS entry.
    Best-effort per tokenizer -- a failure on one doesn't block the others;
    a tokenizer that fails entirely is simply absent from the returned map
    (matches the column's documented "absent key = no stored count"
    contract, see migration 066)."""
    out: dict[str, int] = {}
    if "gemini" in LIVE_TOKENIZERS:
        n = count_tokens_gemini(text)
        if n is not None:
            out["gemini"] = n
    return out


def fallback_estimate_tokens(text: str) -> tuple[int, str]:
    """chars//4 heuristic with explicit provenance -- Governor's ruling:
    "the number carries its provenance, same as everything else on this
    track." Returns (estimate, "chars_over_4_fallback") so a caller can
    never mistake this for a stored exact count."""
    return len(text) // 4, "chars_over_4_fallback"
