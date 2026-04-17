"""Thread title generation (Phase 2.3).

Regression tests for the UI bug where the sidebar rendered every
``chat_turns.question`` verbatim — raw URLs, lone HCPCS codes, tool
invocation strings — instead of readable thread titles.
"""

from __future__ import annotations

from app.storage.thread_title import (
    MAX_TITLE_CHARS,
    generate_thread_title,
    is_noise,
)


# ── is_noise ────────────────────────────────────────────────────────────────


class TestIsNoise:
    def test_empty_is_noise(self):
        assert is_noise("") is True
        assert is_noise("   ") is True
        assert is_noise(None) is True  # type: ignore[arg-type]

    def test_lone_hcpcs_code_is_noise(self):
        assert is_noise("H0036") is True
        assert is_noise("F32.1") is True

    def test_short_blurb_is_noise(self):
        assert is_noise("hi") is True
        assert is_noise("?") is True

    def test_very_long_is_noise(self):
        """A 3kB user message is almost certainly a tool-invocation dump."""
        assert is_noise("x" * 3000) is True

    def test_real_question_is_not_noise(self):
        assert is_noise("What is timely filing for Sunshine Health?") is False
        assert is_noise("List the credentialing requirements for BCBS Florida.") is False


# ── generate_thread_title — real questions ──────────────────────────────────


class TestTitleFromRealQuestions:
    def test_short_question_kept(self):
        t = generate_thread_title("What is timely filing for Sunshine Health?")
        assert t == "What is timely filing for Sunshine Health?"

    def test_long_question_truncated_on_word_boundary(self):
        long_q = (
            "What are Sunshine Health's medical necessity criteria for H0036 "
            "and how does it interact with the InterQual prior authorization "
            "workflow for Florida Medicaid members?"
        )
        t = generate_thread_title(long_q)
        assert len(t) <= MAX_TITLE_CHARS + 1  # +1 for the ellipsis
        assert t.endswith("…")
        # Should cut on a word boundary, not mid-word.
        assert not t[:-1].endswith(" ")
        assert " " in t  # still contains words, not one huge token

    def test_capitalizes_first_letter(self):
        t = generate_thread_title("timely filing rules?")
        assert t[0] == "T"

    def test_quotes_stripped(self):
        t = generate_thread_title('"What is PA 123?"')
        assert not t.startswith('"')
        assert not t.endswith('"')

    def test_curly_quotes_stripped(self):
        t = generate_thread_title("“prior auth for X”")
        assert "“" not in t
        assert "”" not in t


# ── generate_thread_title — noisy inputs ────────────────────────────────────


class TestTitleNoiseHandling:
    def test_empty_input_returns_placeholder(self):
        assert generate_thread_title("") == "Untitled chat"
        assert generate_thread_title("   ") == "Untitled chat"

    def test_lone_hcpcs_becomes_lookup_label(self):
        assert generate_thread_title("H0036") == "Lookup: H0036"
        assert generate_thread_title("F32.1") == "Lookup: F32.1"

    def test_lone_url_becomes_domain_lookup(self):
        t = generate_thread_title("https://www.sunshinehealth.com/provider/manual/")
        # Should collapse the URL to a readable "Lookup: domain" form.
        assert "Lookup:" in t
        assert "sunshinehealth.com" in t
        assert "https://" not in t
        assert "/provider" not in t

    def test_question_with_embedded_url_strips_url(self):
        """A real question that references a URL should still produce a readable title."""
        q = "What is the policy at https://www.sunshinehealth.com/provider/manual/ ?"
        t = generate_thread_title(q)
        assert "https://" not in t
        assert "What is" in t

    def test_markdown_link_text_preserved(self):
        t = generate_thread_title("Check [Sunshine Manual](https://sh.com) for H0036")
        assert "Sunshine Manual" in t
        assert "https://" not in t
        assert "(" not in t  # markdown syntax gone


class TestTitleLengthCap:
    def test_never_exceeds_cap_plus_ellipsis(self):
        """Regression — any input must produce a title ≤ MAX_TITLE_CHARS + 1."""
        cases = [
            "x" * 200,
            "word " * 50,
            "",
            "H0036",
            "https://example.com/" + "a" * 100,
            "What is the " + ("very long " * 20) + "question?",
        ]
        for q in cases:
            t = generate_thread_title(q)
            assert len(t) <= MAX_TITLE_CHARS + 1, f"Title too long for input: {q[:50]!r}"


class TestIdempotence:
    def test_deterministic(self):
        q = "What is timely filing for Sunshine Health participating providers?"
        assert generate_thread_title(q) == generate_thread_title(q)
