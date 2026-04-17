"""Phase 0.14 — answer-detail fidelity.

Regression for the "thin one-liner" UX bug: the integrator's BLENDED-mode
answer card produced a vague summary even when the reasoning trace had
clear specifics (code definition, standard name, manual page). Root cause
was a combination of:

1. BLENDED system prompt instructing ``direct_answer`` to be 1-2 sentences
   and implying that specifics belong in hidden sections.
2. BLENDED UI visibility rule showing only ``requirements`` sections by
   default — so ``definitions`` (e.g. "H0036 = Community Psychiatric
   Supportive Treatment") was always hidden behind "Show details".

Fixes (prompt + visibility, two levers compose):

A) BLENDED system prompt: direct_answer is now 1–3 sentences AND must
   include inline specifics when the user asked for them (code meaning,
   criteria values, rule conditions). Includes a worked example so the
   LLM sees the contrast between a good and bad direct_answer.
B) UI ``splitSectionsByVisibility`` for BLENDED now shows both
   ``requirements`` AND ``definitions`` by default.

These tests assert the prompt contract is intact. UI behavior is harder
to unit-test in Python; the TS/JS change is mirrored and the smoke check
is "re-run Sunshine/H0036 in mstart."
"""

from __future__ import annotations

from app.chat_config import ChatPromptsConfig


class TestBlendedPromptContract:
    def _prompt(self) -> str:
        return ChatPromptsConfig().integrator_blended_system

    def test_direct_answer_permits_up_to_three_sentences(self):
        """The new contract allows 1–3 sentences for direct_answer (was 1–2)."""
        p = self._prompt()
        assert "1–3 sentences" in p, (
            "BLENDED prompt must allow 1–3 sentences for direct_answer; "
            "tighter than this produces thin one-liners on detail questions"
        )
        assert "1–2 sentences max" not in p, (
            "old 1-2 sentence rule was the primary cause of thin answers"
        )

    def test_direct_answer_must_include_specifics_when_asked(self):
        """The prompt explicitly directs the LLM to inline specifics."""
        p = self._prompt().lower()
        assert "include those specifics inline" in p, (
            "prompt must push specifics into direct_answer, not only sections"
        )
        # The worked example mentions H0036 — validates the example survived edits.
        assert "h0036" in p

    def test_prompt_has_good_and_bad_example_contrast(self):
        """Contrastive example is the most teachable form for LLM instruction."""
        p = self._prompt().lower()
        assert "good direct_answer" in p or "bad direct_answer" in p, (
            "prompt should contrast good vs bad patterns so the model "
            "learns what 'includes specifics' means"
        )

    def test_visibility_contract_matches_frontend(self):
        """Prompt tells the LLM that requirements + definitions are visible by default.

        The FE's ``splitSectionsByVisibility`` (both app.ts and app.js) is updated
        to match. If this test fails, the prompt and UI have drifted — users will
        see hidden content the prompt claimed was visible or vice versa.
        """
        p = self._prompt()
        assert "requirements AND definitions" in p, (
            "prompt must tell the LLM that BOTH requirements and definitions "
            "sections are visible by default"
        )
        # Old single-section-visible contract must be gone.
        assert "will show direct_answer and requirements sections;" not in p

    def test_definitions_placement_directive_present(self):
        """Prompt specifies which intent should carry code/term content."""
        p = self._prompt().lower()
        assert "code definitions" in p and "definitions" in p
        assert "term meanings" in p or "standard names" in p


class TestOtherModesUnchanged:
    """Sanity — we only changed BLENDED; FACTUAL and CANONICAL prompts are unaffected."""

    def test_factual_still_one_sentence_operational(self):
        p = ChatPromptsConfig().integrator_factual_system
        assert "one sentence, operational" in p

    def test_canonical_still_one_sentence_summary(self):
        p = ChatPromptsConfig().integrator_canonical_system
        assert "one-sentence summary" in p


class TestDocumentedWorkedExample:
    """The BLENDED prompt carries a worked example because prompt-engineering
    research shows contrastive exemplars move model output more reliably than
    abstract rules alone. If the example is dropped, regression risk spikes.
    """

    def test_example_shows_inline_specifics(self):
        p = ChatPromptsConfig().integrator_blended_system
        # The good example should include (code = definition; standard; page).
        assert "Community Psychiatric Supportive Treatment" in p
        assert "InterQual" in p
        assert "Provider Manual" in p

    def test_example_shows_bad_counterexample(self):
        p = ChatPromptsConfig().integrator_blended_system
        # The bad example is the shape we actually saw in production.
        assert "uses InterQual criteria to evaluate H0036" in p
