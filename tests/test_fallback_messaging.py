"""Phase 0.12 — fallback messaging hygiene.

Regression for the UX bug: unparseable integrator JSON produced
"Something went wrong. Please try again, or start a new chat." in the chat
UI, which (a) conflated a transient formatting issue with a catastrophic
failure, and (b) nudged users into a destructive action (start over) when
rephrasing usually works.
"""

from __future__ import annotations

import logging

from app.communication.json_display_sanitize import (
    DEFAULT_BLEED_FALLBACK,
    _log_fallback,
    display_text_for_parsed_answer_card,
    extract_user_visible_text_from_integrator_raw,
)


class TestFallbackString:
    def test_does_not_suggest_starting_over(self):
        """Regression: the old string said 'start a new chat' which is way too aggressive."""
        assert "start a new chat" not in DEFAULT_BLEED_FALLBACK.lower()
        assert "new chat" not in DEFAULT_BLEED_FALLBACK.lower()

    def test_is_actionable(self):
        """The fallback should suggest something the user can actually do."""
        lowered = DEFAULT_BLEED_FALLBACK.lower()
        assert any(
            word in lowered for word in ("rephras", "try again", "different")
        ), f"fallback should be actionable; got: {DEFAULT_BLEED_FALLBACK}"

    def test_is_not_scary(self):
        """Avoid 'Something went wrong' which feels broken-state without context."""
        # Regression: previous message started with this catastrophic phrase.
        assert not DEFAULT_BLEED_FALLBACK.startswith("Something went wrong")


class TestFallbackLogging:
    def test_log_fallback_emits_warning_with_site_and_preview(self, caplog):
        """Every fallback fire must be greppable in logs for production debugging."""
        with caplog.at_level(logging.WARNING):
            _log_fallback("unit_test_site", "some raw integrator output")
        records = [
            r for r in caplog.records
            if "integrator_fallback" in r.getMessage()
        ]
        assert records, "fallback must log a WARNING with 'integrator_fallback' prefix"
        msg = records[0].getMessage()
        assert "site=unit_test_site" in msg
        assert "raw_len=" in msg
        assert "some raw integrator" in msg

    def test_log_fallback_redacts_newlines_in_preview(self):
        """Log line should be single-line for easy grep."""
        # The function uses .replace("\n", "\\n"); verify by calling directly.
        # We're really just asserting the implementation is newline-safe;
        # more of a design assertion than a runtime check.
        from app.communication.json_display_sanitize import _log_fallback
        import io
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setLevel(logging.WARNING)
        target_logger = logging.getLogger("app.communication.json_display_sanitize")
        prior_level = target_logger.level
        target_logger.addHandler(handler)
        target_logger.setLevel(logging.WARNING)
        try:
            _log_fallback("nl_test", "first line\nsecond line\nthird")
        finally:
            target_logger.removeHandler(handler)
            target_logger.setLevel(prior_level)
        out = stream.getvalue()
        # Expect raw newlines NOT present inside the preview string. The log line
        # itself ends with a newline from the handler, so we check the preview
        # portion only.
        preview_start = out.find("raw_preview=")
        preview_end = out.find("raw_len=")
        assert preview_start >= 0 and preview_end >= 0
        preview = out[preview_start:preview_end]
        # The preview must not contain a literal newline (would break log grep).
        assert "\n" not in preview, f"preview contained a raw newline: {preview!r}"


class TestDisplayTextForParsedAnswerCard:
    def test_non_dict_input_returns_fallback_and_logs(self, caplog):
        with caplog.at_level(logging.WARNING):
            result = display_text_for_parsed_answer_card(
                "not a dict — oops"  # type: ignore[arg-type]
            )
        assert result == DEFAULT_BLEED_FALLBACK
        assert any(
            "display_text_for_parsed_answer_card.not_dict" in r.getMessage()
            for r in caplog.records
        )

    def test_usable_direct_answer_returned_unchanged(self):
        parsed = {
            "mode": "FACTUAL",
            "direct_answer": "Timely filing is 180 days.",
            "sections": [],
        }
        assert display_text_for_parsed_answer_card(parsed) == "Timely filing is 180 days."

    def test_empty_returns_empty_not_fallback(self):
        """Truly empty output returns '' — that's a distinct signal from a parse failure."""
        parsed = {"mode": "FACTUAL", "direct_answer": "", "sections": []}
        assert display_text_for_parsed_answer_card(parsed) == ""


class TestExtractUserVisibleText:
    def test_invalid_json_returns_fallback_and_logs(self, caplog):
        with caplog.at_level(logging.WARNING):
            result = extract_user_visible_text_from_integrator_raw("{ broken : json ,,,")
        assert result == DEFAULT_BLEED_FALLBACK
        assert any(
            "extract_user_visible_text_from_integrator_raw.invalid_json" in r.getMessage()
            for r in caplog.records
        )

    def test_plain_text_passes_through(self):
        """Non-JSON text is legitimate and should NOT trigger the fallback."""
        out = extract_user_visible_text_from_integrator_raw("plain text answer")
        assert out == "plain text answer"
