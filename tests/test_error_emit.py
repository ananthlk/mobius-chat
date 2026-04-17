"""Error classifier unit tests (Phase 0.6b).

Direct regression: the provider JSON that leaked to the UI today (Groq 413/429
with the org ID inside) must NOT appear in ``user_facing_message`` after going
through ``classify_exception``.
"""

from __future__ import annotations

from app.communication.error_emit import (
    classify_exception,
    tool_result_from_exception,
)


# ── The exact production failure strings ────────────────────────────────────


GROQ_413 = (
    'Groq API error 413: {"error":{"message":"Request too large for model '
    '`openai/gpt-oss-20b` in organization `org_01k9ff309tfmx9t836sxw6yrw2` '
    'service tier `on_demand` on tokens per minute (TPM): Limit 8000, '
    'Requested 8907, please reduce your message size and try again. Need '
    'more tokens? Upgrade to Dev Tier today at https://console.groq.com/'
    'settings/billing","type":"tokens","code":"rate_limit_exceeded"}}'
)

GROQ_429 = (
    'Groq API error 429: {"error":{"message":"Rate limit reached for model '
    '`llama-3.3-70b-versatile` in organization `org_01k9ff309tfmx9t836sxw6yrw2` '
    'service tier `on_demand` on tokens per minute (TPM): Limit 12000, '
    'Used 8770, Requested 7272. Please try again in 20.21s. Need more '
    'tokens? Upgrade to Dev Tier today at https://console.groq.com/'
    'settings/billing","type":"tokens","code":"rate_limit_exceeded"}}'
)

SCRAPE_502 = (
    "Scrape failed (502): {\"detail\":\"Failed to fetch page: Client error "
    "'404 Not Found' for url 'https://www.sunshinehealth.com/provider/manual/'\"}"
)


# ── classify_exception ─────────────────────────────────────────────────────


class TestLeakPrevention:
    def test_groq_413_never_leaks_org_id_to_user(self):
        env = classify_exception(RuntimeError(GROQ_413), tool="search_corpus")
        assert "org_01k9" not in env.user_facing_message
        assert "gpt-oss-20b" not in env.user_facing_message
        assert "groq.com" not in env.user_facing_message
        assert env.error_code in ("token_budget", "rate_limit")

    def test_groq_429_never_leaks_org_id_to_user(self):
        env = classify_exception(RuntimeError(GROQ_429), tool="integrator", round=3)
        assert "org_01k9" not in env.user_facing_message
        assert "llama-3.3-70b" not in env.user_facing_message
        assert env.error_code == "rate_limit"
        # Retry-after should be parsed from the message.
        assert env.retry_after_seconds == 20

    def test_groq_429_preserves_internal_detail_for_logs(self):
        env = classify_exception(RuntimeError(GROQ_429))
        # Logs still get everything — that's the whole point of a split model.
        assert "org_01k9" in env.internal_detail
        assert "llama-3.3-70b" in env.internal_detail

    def test_scrape_502_mapped_to_scrape_failed(self):
        env = classify_exception(RuntimeError(SCRAPE_502), tool="web_scrape")
        assert env.error_code == "scrape_failed"
        assert "sunshinehealth.com" not in env.user_facing_message
        assert "404" not in env.user_facing_message


class TestClassification:
    def test_timeout_error_class(self):
        env = classify_exception(TimeoutError("deadline exceeded"))
        assert env.error_code == "timeout"
        assert env.retry_after_seconds == 5

    def test_plain_timeout_string(self):
        env = classify_exception(RuntimeError("request timed out after 30s"))
        assert env.error_code == "timeout"

    def test_provider_500(self):
        env = classify_exception(RuntimeError("Internal server error 500 on vertex"))
        assert env.error_code == "provider_error"
        # No 'for url' in it — must NOT be misclassified as scrape_failed
        assert "hiccup" in env.user_facing_message.lower()

    def test_auth_401(self):
        env = classify_exception(RuntimeError("Unauthorized 401 invalid api key"))
        assert env.error_code == "auth_error"

    def test_validation_error(self):
        class _ValErr(Exception):
            pass
        _ValErr.__name__ = "ValidationError"
        env = classify_exception(_ValErr("field required"))
        assert env.error_code == "validation_error"

    def test_fallback_internal_error(self):
        env = classify_exception(RuntimeError("brand-new never-before-seen failure"))
        assert env.error_code == "internal_error"
        assert env.user_facing_message == "Something went wrong — trying another path."

    def test_provenance_fields_pass_through(self):
        env = classify_exception(
            RuntimeError("timeout"), tool="google_search", round=2
        )
        assert env.tool == "google_search"
        assert env.round == 2


# ── tool_result_from_exception ──────────────────────────────────────────────


class TestToolResultShape:
    def test_shape_matches_react_loop_contract(self):
        """ReAct loop expects dicts with tool, success, result, sources keys."""
        r = tool_result_from_exception(
            RuntimeError(GROQ_429), tool="search_corpus", round=1
        )
        assert r["tool"] == "search_corpus"
        assert r["success"] is False
        assert isinstance(r["result"], str)
        assert r["sources"] == []
        # And carries the typed envelope for Phase 0.7 logic.
        assert r["error"]["schema_name"] == "error_envelope"
        assert r["error"]["error_code"] == "rate_limit"
        assert r["error"]["round"] == 1

    def test_result_field_is_user_safe(self):
        """The ``result`` string is what the ReAct loop may echo to context — it must be clean."""
        r = tool_result_from_exception(RuntimeError(GROQ_413), tool="search_corpus")
        assert "org_01k9" not in r["result"]
        assert "openai/gpt-oss-20b" not in r["result"]
