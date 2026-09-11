"""Migration 066 / Governor seat's estimate() correction (2026-09-10):
exact per-tokenizer token counts on prompt_blocks, computed once at publish
time since blocks are immutable per-version. See
app/services/prompt_token_counting.py's module docstring for the full
reasoning (chars//4's error is content-and-tokenizer-dependent, not a fixed
correction factor -- measured both directions on real block content).
"""
from __future__ import annotations

from unittest.mock import patch

from app.services.prompt_token_counting import (
    _vertex_project_and_region,
    compute_token_counts,
    fallback_estimate_tokens,
)


class TestFallbackEstimate:
    def test_returns_chars_over_4_with_provenance(self):
        n, provenance = fallback_estimate_tokens("abcdefgh")  # 8 chars
        assert n == 2
        assert provenance == "chars_over_4_fallback"

    def test_provenance_is_always_present_even_for_empty_text(self):
        n, provenance = fallback_estimate_tokens("")
        assert n == 0
        assert provenance == "chars_over_4_fallback"


class TestComputeTokenCounts:
    def test_gemini_count_included_when_available(self):
        with patch("app.services.prompt_token_counting.count_tokens_gemini", return_value=3093):
            counts = compute_token_counts("some block text")
        assert counts == {"gemini": 3093}

    def test_gemini_absent_key_when_unavailable(self):
        # A failed/unreachable tokenizer must leave the key ABSENT, not
        # write a zero or a wrong value -- migration 066's documented
        # contract: "absent key means no stored count for that tokenizer."
        with patch("app.services.prompt_token_counting.count_tokens_gemini", return_value=None):
            counts = compute_token_counts("some block text")
        assert counts == {}
        assert "gemini" not in counts


class TestVertexProjectResolution:
    def test_prefers_vertex_project_id_env(self, monkeypatch):
        monkeypatch.setenv("VERTEX_PROJECT_ID", "explicit-project")
        monkeypatch.delenv("CHAT_VERTEX_PROJECT_ID", raising=False)
        project, _ = _vertex_project_and_region()
        assert project == "explicit-project"

    def test_falls_back_to_chat_vertex_project_id(self, monkeypatch):
        monkeypatch.delenv("VERTEX_PROJECT_ID", raising=False)
        monkeypatch.setenv("CHAT_VERTEX_PROJECT_ID", "chat-project")
        project, _ = _vertex_project_and_region()
        assert project == "chat-project"

    def test_default_project_matches_vertex_factorys_default(self, monkeypatch):
        # Same resolution order as llm_provider._vertex_factory -- this
        # module must never talk to a different project than the one
        # actually serving requests.
        monkeypatch.delenv("VERTEX_PROJECT_ID", raising=False)
        monkeypatch.delenv("CHAT_VERTEX_PROJECT_ID", raising=False)
        project, _ = _vertex_project_and_region()
        assert project == "mobiusos-new"
