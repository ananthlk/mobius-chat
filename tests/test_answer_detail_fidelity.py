"""Phase 0.14/0.15/0.16 — answer-detail fidelity.

Regression for the "thin one-liner" UX bug: the integrator's answer card
produced a vague summary even when the reasoning trace had clear
specifics (code definition, standard name, manual page). Root cause was
a combination of:

1. BLENDED system prompt instructing ``direct_answer`` to be 1-2 sentences
   and implying that specifics belong in hidden sections. (Phase 0.14)
2. BLENDED UI visibility rule showing only ``requirements`` sections by
   default — so ``definitions`` (e.g. "H0036 = Community Psychiatric
   Supportive Treatment") was always hidden behind "Show details". (0.14)
3. FACTUAL's direct_answer stayed capped at ONE sentence even after 1/2
   landed — and FACTUAL is the mode real fast/copilot-mode questions
   actually land in (confirmed live, 2026-08-06: "is prior auth required
   for an in-home ventilator" classified FACTUAL, produced a single
   sentence — read as "too short" by the user regardless of which mode
   technically produced it). (Phase 0.16, Ananth directive)

Fixes (prompt + visibility, three levers compose):

A) BLENDED system prompt: direct_answer is 1-2 short paragraphs and must
   include inline specifics when the user asked for them (code meaning,
   criteria values, rule conditions). Includes a worked example so the
   LLM sees the contrast between a good and bad direct_answer. (0.14)
B) UI ``splitSectionsByVisibility`` for BLENDED shows both
   ``requirements`` AND ``definitions`` by default. (0.14)
C) All three modes (FACTUAL/BLENDED/CANONICAL) now target real
   paragraph-length direct_answer content. FACTUAL stays the leanest of
   the three (still leads with the operative fact first) but is no
   longer capped at a single sentence when sources support more.
   CANONICAL — the most detailed mode — moved from a few sentences to
   2-3 full paragraphs. The relative gradient (FACTUAL leanest,
   CANONICAL most expansive) is preserved; the absolute floor for all
   three moved up. (0.16)

These tests assert the prompt contract is intact. UI behavior is harder
to unit-test in Python; the TS/JS change is mirrored and the smoke check
is "re-run Sunshine/H0036 in mstart."
"""

from __future__ import annotations

from app.chat_config import ChatPromptsConfig, get_chat_config


class TestYamlOverrideStaysInSync:
    """2026-08-06: config/prompts_llm.yaml, when present, OVERRIDES these
    Python dataclass defaults entirely (get_chat_config() prefers YAML
    content over ChatPromptsConfig()'s field defaults whenever the YAML
    key is non-empty). Found live: the Phase 0.16 prompt edits below had
    ZERO effect on production because the YAML held its own, even-more-
    stale copy of these three prompts (still "ONE sentence" for FACTUAL,
    no worked examples, 2-4 bullets not 3-6) — the Python-only tests
    above all passed while the live answer stayed a single sentence.

    These tests exercise get_chat_config() (the real merge path a live
    request uses), not ChatPromptsConfig() directly, so a future prompt
    edit that updates the Python default but forgets the YAML fails loudly
    here instead of silently doing nothing in production."""

    def test_factual_yaml_matches_python_default(self):
        py = ChatPromptsConfig().integrator_factual_system
        live = get_chat_config().prompts.integrator_factual_system
        assert "1–2 short paragraphs" in live, (
            "config/prompts_llm.yaml's integrator_factual_system is stale relative "
            "to the Python default — update the YAML, not just chat_config.py"
        )
        assert "SHORTEST of the three modes" in py  # sanity: Python source itself is current

    def test_blended_yaml_matches_python_default(self):
        live = get_chat_config().prompts.integrator_blended_system
        assert "1–2 short paragraphs" in live
        assert "InterQual" in live

    def test_canonical_yaml_matches_python_default(self):
        live = get_chat_config().prompts.integrator_canonical_system
        assert "2–3 paragraphs" in live


class TestBlendedPromptContract:
    def _prompt(self) -> str:
        return ChatPromptsConfig().integrator_blended_system

    def test_direct_answer_permits_short_paragraphs(self):
        """Phase 0.16: BLENDED direct_answer is 1-2 short paragraphs (was
        1-3 sentences in Phase 0.14) — a single sentence, however
        specific, still reads as thin."""
        p = self._prompt()
        assert "1–2 short paragraphs" in p, (
            "BLENDED prompt must allow 1-2 short paragraphs for direct_answer; "
            "a sentence-count cap produces thin answers regardless of the cap size"
        )
        assert "1–2 sentences max" not in p, (
            "old 1-2 sentence rule was the primary cause of thin answers"
        )

    def test_direct_answer_must_include_specifics_when_asked(self):
        """The prompt explicitly directs the LLM to inline specifics."""
        p = self._prompt().lower()
        assert "include specifics inline" in p, (
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
    """FACTUAL stays leanest of the three; CANONICAL is the most expansive
    (Phase 0.15, targets updated in 0.16).

    These tests lock in the mode-gradient the user wanted:
        FACTUAL   = 1-2 short paragraphs (leanest — still leads with the
                    single operative fact, but not artificially one-sentence)
        BLENDED   = 1-2 short paragraphs (middle — specifics inlined)
        CANONICAL = 2-3 paragraphs (longest, most expansive)
    """

    def test_factual_permits_short_paragraphs(self):
        """Phase 0.16: FACTUAL direct_answer is 1-2 short paragraphs — no
        longer capped at one sentence (that was the root cause of the
        "too short" complaint, since FACTUAL is the mode real fast-mode
        questions actually land in)."""
        p = ChatPromptsConfig().integrator_factual_system
        assert "1–2 short paragraphs" in p
        assert "direct_answer is ONE sentence" not in p, (
            "old one-sentence FACTUAL rule must be gone — it's what made "
            "real fast-mode answers read as too thin"
        )

    def test_factual_is_declared_shortest(self):
        """Phase 0.15: prompt explicitly tells the LLM FACTUAL is the shortest mode."""
        p = ChatPromptsConfig().integrator_factual_system
        assert "SHORTEST of the three modes" in p

    def test_canonical_allows_multiple_paragraphs(self):
        """Phase 0.16: CANONICAL permits 2-3 paragraphs (was 3-6 sentences
        in 0.15, one sentence before that).

        Regression: the old "one-sentence summary" rule kept CANONICAL
        direct_answer as thin as FACTUAL, eliminating the mode gradient.
        """
        p = ChatPromptsConfig().integrator_canonical_system
        assert "2–3 paragraphs" in p, (
            "CANONICAL direct_answer must be multiple real paragraphs, not one sentence"
        )
        assert "one-sentence summary" not in p, (
            "old one-sentence CANONICAL rule must be gone"
        )
        assert "most detailed of the three modes" in p, (
            "prompt must signal that CANONICAL is the most-expansive mode"
        )


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


class TestSectionsRequireSubstantiveBullets:
    """Phase 0.15: across ALL three modes, section bullets must carry real content.

    The bug was sections with bullets like "required", "see manual", "applicable" —
    technically valid JSON but useless when the user clicks through to details.
    Every mode's prompt now forbids stub bullets.
    """

    def test_factual_requires_substantive_bullets(self):
        p = ChatPromptsConfig().integrator_factual_system
        assert "substantive bullets" in p
        assert "stub bullets" in p

    def test_blended_requires_substantive_bullets(self):
        p = ChatPromptsConfig().integrator_blended_system
        assert "substantive bullets" in p
        assert "stub bullets" in p

    def test_canonical_requires_substantive_bullets(self):
        p = ChatPromptsConfig().integrator_canonical_system
        assert "substantive bullets" in p
        assert "stub bullets" in p

    def test_factual_sections_must_hold_detail_because_hidden(self):
        """FACTUAL sections are hidden by default — the prompt must call this out."""
        p = ChatPromptsConfig().integrator_factual_system
        assert "hides sections behind" in p.lower() or "show details" in p.lower()

    def test_bullets_per_section_increased(self):
        """Phase 0.15: 3-6 bullets (was 2-4) so sections carry real coverage."""
        for p in (
            ChatPromptsConfig().integrator_factual_system,
            ChatPromptsConfig().integrator_blended_system,
            ChatPromptsConfig().integrator_canonical_system,
        ):
            assert "3–6 substantive bullets" in p
