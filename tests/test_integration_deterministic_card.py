"""INTEGRATION: a real draft, through run_integrate, to the published card.

The layer the render test could not reach. tests/test_no_enricher_formatting
proves the formatter's output is right, and the frontend's
formatter-integration.test.ts proves the renderer draws it — but both call the
formatter directly. Neither proves a TURN reaches it.

This drives run_integrate itself, so what is exercised is the pipeline's own
decision: the sufficiency gate, the v2 grounding wiring, the pre-built-section
guarantee, and the payload the worker actually publishes.

NO MODEL RUNS. format_response_parallel is asserted NOT CALLED on every
deterministic case — that assertion is the point of the file, not a detail of
it. Mocking it and never checking would prove nothing.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from app.pipeline.context import PipelineContext
from app.planner.schemas import Plan, SubQuestion
from app.stages.integrate import run_integrate

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from render_corpus import CORPUS  # noqa: E402

_DETERMINISTIC_ENV = {
    "MOBIUS_INTEGRATOR_MODE": "parallel",
    "MOBIUS_DYNAMIC_ENRICHMENT_PCT": "100",
}


def _ctx(draft: str, question: str = "What is X?", **extra) -> PipelineContext:
    ctx = PipelineContext(
        correlation_id="integration-cid", thread_id="integration-thread",
        message=question,
        plan=Plan(subquestions=[SubQuestion(id="sq1", text=question, kind="non_patient")]),
        answers=["An answer."], sources=[], usages=[], retrieval_signals=[],
    )
    ctx.chat_mode = "copilot"
    ctx.react_rounds_used = 2
    ctx.react_draft = draft
    ctx.react_unfinished_reason = None
    for k, v in extra.items():
        setattr(ctx, k, v)
    return ctx


def _run(ctx) -> dict:
    """run_integrate with every model path mocked AND asserted unused."""
    with (
        patch.dict(os.environ, _DETERMINISTIC_ENV),
        patch("app.stages.integrate.format_response_parallel") as parallel,
        patch("app.stages.integrate.run_bc_background"),
    ):
        # A real return shape, so a turn that DOES take the LLM path fails on
        # the assertion below rather than on an unpack error that hides it.
        parallel.return_value = ('{"mode":"FACTUAL","direct_answer":"llm","sections":[]}', [])
        run_integrate(ctx)
        assert not parallel.called, "an LLM integrator call ran on the deterministic path"
    return json.loads(ctx.response_payload["message"])


# ── every real draft, end to end ────────────────────────────────────────────

#: The thin-evidence hedge is 112 characters, and the sufficiency gate has a
#: 200-char floor — so it never reaches the deterministic path at all. That is
#: correct pipeline behaviour, not a formatter result, and a test that expected
#: otherwise would be asserting the gate away.
_BELOW_THE_LENGTH_FLOOR = "Thin-evidence hedge"

_REACHES_THE_FORMATTER = [c for c in CORPUS if not c[2].startswith(_BELOW_THE_LENGTH_FLOOR)]


@pytest.mark.parametrize(
    "question,draft,note",
    _REACHES_THE_FORMATTER,
    ids=[note[:38].replace(" ", "_") for _, _, note in _REACHES_THE_FORMATTER],
)
def test_every_real_draft_publishes_a_card_with_no_model_call(question, draft, note):
    card = _run(_ctx(draft, question))
    assert "direct_answer" in card
    assert isinstance(card.get("sections"), list)


def test_a_draft_below_the_length_floor_goes_to_the_llm_path():
    """The other half of the same rule, asserted rather than skipped. A short
    draft is exactly the turn the deterministic pass should NOT take."""
    short = next(d for _q, d, n in CORPUS if n.startswith(_BELOW_THE_LENGTH_FLOOR))
    ctx = _ctx(short)
    with (
        patch.dict(os.environ, _DETERMINISTIC_ENV),
        patch("app.stages.integrate.format_response_parallel") as parallel,
        patch("app.stages.integrate.run_bc_background"),
    ):
        parallel.return_value = ('{"mode":"FACTUAL","direct_answer":"llm","sections":[]}', [])
        run_integrate(ctx)
        assert parallel.called


def test_the_published_card_matches_what_the_formatter_produced():
    """The pipeline must not reshape the card on its way out. If these ever
    diverge, everything the formatter tests assert is about a card the user
    never sees."""
    from app.responder.deterministic_format import deterministic_format

    for question, draft, _note in _REACHES_THE_FORMATTER:
        published = _run(_ctx(draft, question))
        direct = deterministic_format(draft)
        assert [s["format"] for s in published["sections"]] == \
               [s["format"] for s in direct["sections"]], question


def test_a_long_multi_shape_answer_keeps_its_sections_through_the_pipeline():
    long_draft = next(d for _q, d, n in CORPUS if n.startswith("Long multi-shape"))
    card = _run(_ctx(long_draft))
    assert [s["format"] for s in card["sections"]] == ["table", "steps", "bullets", "bullets"]


def test_a_pure_table_draft_publishes_no_raw_markdown():
    """The bug the render harness found, asserted where the payload is built."""
    table_draft = next(d for _q, d, n in CORPUS if n.startswith("cid 65ed12e2"))
    card = _run(_ctx(table_draft))
    assert not card["direct_answer"].strip().startswith("|")
    assert card["sections"][0]["format"] == "table"


# ── the gate, which is what actually decides whether any of this runs ───────

class TestTheSufficiencyGate:
    """These document the production blocker rather than a behaviour I want.
    The formatter is only reached when the pipeline lets a turn through."""

    DRAFT = next(d for _q, d, n in CORPUS if n.startswith("Long multi-shape"))

    def _took_llm_path(self, **extra) -> bool:
        ctx = _ctx(self.DRAFT, **extra)
        with (
            patch.dict(os.environ, _DETERMINISTIC_ENV),
            patch("app.stages.integrate.format_response_parallel") as parallel,
            patch("app.stages.integrate.run_bc_background"),
        ):
            parallel.return_value = (
                '{"mode":"FACTUAL","direct_answer":"llm","sections":[]}', [])
            run_integrate(ctx)
            return parallel.called

    def test_three_rounds_reaches_the_deterministic_path(self):
        assert self._took_llm_path(react_rounds_used=3) is False

    def test_FOUR_rounds_does_not(self):
        """`rounds_used > 3` in _is_sufficient_for_deterministic_pass. The
        live Molina turn used five, which is the whole reason its answer went
        through the LLM integrator despite the formatter being ready."""
        assert self._took_llm_path(react_rounds_used=5) is True

    def test_an_unfinished_turn_does_not(self):
        assert self._took_llm_path(react_unfinished_reason="budget") is True


# ── the v2 grounding wiring, at the call site rather than in the adapter ────

class TestV2GroundingReachesTheCard:
    DRAFT = next(d for _q, d, n in CORPUS if n.startswith("cid 9c825ca5"))

    def test_todays_v1_shape_traffic_still_formats(self):
        """Every part `unobservable` because facts is empty. Reading that as
        "not grounded" suppressed every card on live traffic; the pipeline
        must not reintroduce it."""
        v2 = {"coverage": [{"part": "X", "status": "unobservable", "evidence": []}],
              "citations": [], "ran": {"assemble": "ok"}}
        card = _run(_ctx(self.DRAFT, v2_integration=v2))
        assert card["sections"], "an inconclusive check suppressed the card"

    def test_the_abstain_reason_survives_to_the_published_card(self):
        """_ANSWER_CARD_ENVELOPE_KEYS rebuilds the client card from an
        allowlist. `presentation` was not on it, so the reason the formatter
        declined to build a section had been dropped for two days — through
        every unit test and through the cross-boundary render test, both of
        which feed the formatter's output straight to the renderer and never
        pass through this rebuild."""
        v2 = {"coverage": [{"part": "X", "status": "unsupported", "evidence": []}],
              "citations": [], "ran": {"assemble": "ok"}}
        card = _run(_ctx(self.DRAFT, v2_integration=v2))
        assert "presentation" in card, "the allowlist dropped it again"
        assert card["presentation"]["rule_id"] == "abstain.thin_evidence"

    def test_a_conclusively_ungrounded_turn_is_suppressed(self):
        v2 = {"coverage": [{"part": "X", "status": "unsupported", "evidence": []}],
              "citations": [], "ran": {"assemble": "ok"}}
        card = _run(_ctx(self.DRAFT, v2_integration=v2))
        assert card["sections"] == []

    def test_a_turn_v2_never_touched_is_unaffected(self):
        assert _run(_ctx(self.DRAFT))["sections"]


# ── the guarantee that predates this work and must survive it ──────────────

def test_a_typed_tool_section_survives_the_deterministic_path():
    """ensure_pre_built_sections: typed sections must never depend on the
    LLM's mood (final.py, traced cid=2803928f). The deterministic path is a
    path they must survive too."""
    hints = [{"section_format": "table", "label": "Appeal rules",
              "table_headers": ["Level", "Deadline"],
              "rows": [["Level 1", "90 days"], ["Level 2", "60 days"]]}]
    card = _run(_ctx("Sunshine Health requires claims within 180 days of service. " * 4,
                     tool_section_hints=hints))
    labels = [s.get("label") for s in card["sections"]]
    assert "Appeal rules" in labels
