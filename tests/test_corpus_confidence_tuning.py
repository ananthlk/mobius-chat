"""MOBIUS_REACT_CORPUS_CONFIDENCE_MIN — tunable knob for the ReAct
loop's confidence_min threshold on search_corpus.

Background. The live 2026-04-19 Sunshine Health H0036 validation
failed because search_corpus's hardcoded confidence_min=0.5 silently
dropped Provider Manual chunks that the RAG backend had actually
retrieved — they scored in the "abstain" band (0.3) on a
specific-code question because the Manual's general
medical-necessity framework pages don't score high against a
specific HCPCS code query. Planner saw zero chunks, burned rounds
searching while the evidence sat unused in ctx.sources.

The operator's call: lower the default to 0.3 (admits abstain
chunks as partial evidence), gate behind an env var so iteration
doesn't require code changes. Guidance mode (commit 545677d)
handles synthesis from this partial-evidence input; the critic
(commit 07ed1f4) keeps the synthesized answers grounded.

This test file locks:

  1. **Default is 0.3.** The commit value. If someone raises it
     back to 0.5 without discussion, the regression catches.

  2. **Env var overrides.** Operators tune per-environment via
     MOBIUS_REACT_CORPUS_CONFIDENCE_MIN — tests, staging, prod
     can carry different values.

  3. **Clamping.** Values outside [0.0, 1.0] clamp to the boundary;
     garbage values fall back to default. Defensive — a malformed
     env var shouldn't break the ReAct loop.

  4. **Call-site uses the helper.** The hardcoded 0.5 is gone from
     react_loop.py. If someone re-hardcodes it, a source-level
     assertion catches it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.pipeline.react_loop import (
    _CORPUS_CONFIDENCE_MIN_DEFAULT,
    _corpus_confidence_min,
)


class TestDefaultConfidenceMin:
    def test_default_is_0_3(self):
        """Lowered from 0.5 → 0.3 on 2026-04-19 to admit abstain
        chunks as partial evidence. If this assertion fails because
        someone raised it back to 0.5, they either need to coordinate
        with guidance-mode design or update this test with a rationale."""
        assert _CORPUS_CONFIDENCE_MIN_DEFAULT == 0.3

    def test_default_admits_abstain_grade_chunks(self):
        """Semantic assertion: the default MUST be low enough that
        chunks labeled 'abstain' (mapped score 0.3 in
        app/services/non_patient_rag.py::_CONFIDENCE_LABEL_SCORE)
        pass the filter. Otherwise guidance mode has nothing to
        synthesize from on hard queries — the bug class this whole
        commit addresses."""
        abstain_score = 0.3  # from _CONFIDENCE_LABEL_SCORE["abstain"]
        assert _CORPUS_CONFIDENCE_MIN_DEFAULT <= abstain_score


class TestEnvVarOverride:
    def test_no_env_uses_default(self, monkeypatch):
        monkeypatch.delenv("MOBIUS_REACT_CORPUS_CONFIDENCE_MIN", raising=False)
        assert _corpus_confidence_min() == _CORPUS_CONFIDENCE_MIN_DEFAULT

    def test_env_override_admitted(self, monkeypatch):
        """Operator tunes per-env. Stricter threshold for prod,
        looser for dev — all without a redeploy."""
        monkeypatch.setenv("MOBIUS_REACT_CORPUS_CONFIDENCE_MIN", "0.45")
        assert _corpus_confidence_min() == 0.45

    def test_env_zero_is_admitted(self, monkeypatch):
        """Zero means 'accept every retrieved chunk no matter how
        low-confidence' — useful for maximum-recall debugging. Must
        not be silently upgraded to the default."""
        monkeypatch.setenv("MOBIUS_REACT_CORPUS_CONFIDENCE_MIN", "0.0")
        assert _corpus_confidence_min() == 0.0

    def test_env_one_is_admitted(self, monkeypatch):
        """One means 'only process_confident chunks (0.9 mapped)
        and higher.' Effectively only the best matches. Valid
        strict configuration."""
        monkeypatch.setenv("MOBIUS_REACT_CORPUS_CONFIDENCE_MIN", "1.0")
        assert _corpus_confidence_min() == 1.0

    def test_whitespace_stripped(self, monkeypatch):
        monkeypatch.setenv("MOBIUS_REACT_CORPUS_CONFIDENCE_MIN", "  0.4  ")
        assert _corpus_confidence_min() == 0.4

    def test_empty_string_uses_default(self, monkeypatch):
        monkeypatch.setenv("MOBIUS_REACT_CORPUS_CONFIDENCE_MIN", "")
        assert _corpus_confidence_min() == _CORPUS_CONFIDENCE_MIN_DEFAULT


class TestClamping:
    def test_above_one_clamps_to_one(self, monkeypatch):
        """Defensive: a typo like '10' (meant 0.10?) clamps to 1.0
        rather than silently raising the threshold to 10 which would
        admit zero chunks ever."""
        monkeypatch.setenv("MOBIUS_REACT_CORPUS_CONFIDENCE_MIN", "10")
        assert _corpus_confidence_min() == 1.0

    def test_negative_clamps_to_zero(self, monkeypatch):
        monkeypatch.setenv("MOBIUS_REACT_CORPUS_CONFIDENCE_MIN", "-0.5")
        assert _corpus_confidence_min() == 0.0


class TestMalformedFallback:
    @pytest.mark.parametrize("bad_value", ["abc", "0.3.5", "NaN", "none", "0,3"])
    def test_unparseable_falls_back_to_default(self, monkeypatch, bad_value):
        """A garbage env var must NOT crash the ReAct loop. Fall
        back to the default. This is production-resilience: an
        operator typo in .env shouldn't break every chat turn."""
        monkeypatch.setenv("MOBIUS_REACT_CORPUS_CONFIDENCE_MIN", bad_value)
        assert _corpus_confidence_min() == _CORPUS_CONFIDENCE_MIN_DEFAULT


class TestCallSiteUsesHelper:
    """Source-level regression guard: the hardcoded 0.5 must stay
    deleted. If someone pastes ``confidence_min=0.5`` back into
    react_loop.py (either the old line or as a new call), this test
    fails. The lesson of the 2026-04-19 validation shouldn't get
    undone by a well-meaning refactor."""

    def test_no_hardcoded_confidence_min_in_react_loop(self):
        src = Path("app/pipeline/react_loop.py").read_text()
        # The fix replaced ``confidence_min=0.5`` with
        # ``confidence_min=_corpus_confidence_min()``.
        # Check no hardcoded numeric value remains at a call site.
        import re

        # Allowlist inside comments + the constant declaration itself.
        hardcoded = re.findall(
            r"confidence_min\s*=\s*0\.\d+",
            src,
        )
        # The only accepted form at a CALL site is
        # confidence_min=_corpus_confidence_min(), which doesn't
        # match this regex (no numeric literal after the =).
        assert not hardcoded, (
            f"react_loop.py has hardcoded confidence_min values: {hardcoded}. "
            "Use _corpus_confidence_min() instead so the threshold is "
            "operator-tunable via MOBIUS_REACT_CORPUS_CONFIDENCE_MIN."
        )

    def test_call_site_uses_helper(self):
        """Positive assertion: the helper IS wired at the call site.
        Defense against someone accidentally deleting the helper
        call and leaving confidence_min unset entirely."""
        src = Path("app/pipeline/react_loop.py").read_text()
        assert "confidence_min=_corpus_confidence_min()" in src
