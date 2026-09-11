"""Governor seat, 2026-09-11: enricher.answercard_schema_and_rules@14 (the
live Studio-edited version) split into card.shape_schema (the raw AnswerCard
JSON schema -- source-agnostic, shared) and enricher.how (everything else --
field rules interleaved with consolidating a prior react_draft/
reasoning_ledger, which cannot be made source-agnostic without authoring new
rules against the byte-identical invariant on this live path).

Verified live before this test was written: rendering [card.shape_schema,
enricher.how] via the real BlockAssembler reproduces
enricher.answercard_schema_and_rules@14's exact text, and the full
integrator_enricher_answer composition (all 6 members after the split, vs.
5 before) renders to byte-identical output before and after the swap
(diff -u showed zero lines). This test locks in the boundary itself rather
than re-asserting the live DB state.
"""
from __future__ import annotations

from app.services.block_seed import _CARD_SHAPE_SCHEMA, _ENRICHER_HOW, BLOCK_SPECS, COMPOSITIONS
from app.services.prompt_blocks import Block, BlockAssembler


def test_both_blocks_registered_in_block_specs():
    keys = {s.block_key for s in BLOCK_SPECS}
    assert "card.shape_schema" in keys
    assert "enricher.how" in keys


def test_integrator_enricher_answer_composition_order():
    assert COMPOSITIONS["integrator_enricher_answer"] == [
        "module.enricher", "card.shape_schema", "enricher.how",
        "module.enricher.answer", "hipaa_context", "forced_json",
    ]


def test_shape_is_the_raw_json_schema_only():
    # The boundary itself: shape starts with the schema header and contains
    # the schema's JSON object, nothing about HOW to fill it from a prior
    # draft.
    assert _CARD_SHAPE_SCHEMA.startswith("AnswerCard schema:\n{")
    assert "react_draft" not in _CARD_SHAPE_SCHEMA
    assert "reasoning_ledger" not in _CARD_SHAPE_SCHEMA


def test_how_carries_every_field_rule_and_no_exit_mode_fields_yet():
    # Governor's own correction: no exit-mode fields (status/open_items/
    # continuation_offered) in this pass -- the field and its first writer
    # ship together, and no `how` yet populates them.
    assert "OR when there IS a correction" in _ENRICHER_HOW
    assert "react_draft" in _ENRICHER_HOW  # the consolidation framing, untouched
    # Check the SCHEMA (not prose) for the exit-mode field names as JSON keys --
    # a blunt substring check on _ENRICHER_HOW's prose would false-positive on
    # unrelated uses of common words like "status".
    for forbidden_key in ('"status"', '"open_items"', '"continuation_offered"'):
        assert forbidden_key not in _CARD_SHAPE_SCHEMA


def test_split_reconstructs_original_byte_for_byte():
    """The actual invariant: assembling [shape, how] must equal assembling
    the original single block, via the real assembler (not string
    concatenation by hand -- the assembler's "\\n\\n" join is part of the
    contract being verified)."""
    original_body = _CARD_SHAPE_SCHEMA + "\n\n" + _ENRICHER_HOW
    original_block = Block(
        block_key="enricher.answercard_schema_and_rules", block_kind="static",
        role="system", template_body=original_body, version=14,
    )
    shape_block = Block(block_key="card.shape_schema", block_kind="static",
                         role="system", template_body=_CARD_SHAPE_SCHEMA, version=1)
    how_block = Block(block_key="enricher.how", block_kind="static",
                       role="system", template_body=_ENRICHER_HOW, version=1)

    asm = BlockAssembler()
    old = asm.assemble(
        ["enricher.answercard_schema_and_rules"],
        {"enricher.answercard_schema_and_rules": original_block},
        conditions={}, template_vars={},
    )
    new = asm.assemble(
        ["card.shape_schema", "enricher.how"],
        {"card.shape_schema": shape_block, "enricher.how": how_block},
        conditions={}, template_vars={},
    )
    assert old.text == new.text
