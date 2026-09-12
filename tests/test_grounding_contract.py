"""Governor seat, 2026-09-11, Ananth's direct ask: "do not hallucinate or
answer from your knowledge" -- requested MODULAR (a separate block, not an
edit to react.critical_rules) so it composes into v1 and v2 alike.

Confirmed a real gap before writing: critical_rules v8's "NEVER fabricate
specifics" only catches a WRONG specific inside an otherwise-grounded
answer -- it says nothing about a fully ungrounded answer that invents no
specifics to be wrong about. Confirmed live (correlation_id 403d0e59, dev):
a three-payer comparison ran planner->enricher->integrator with ZERO
retrieval tool calls and ZERO sources, and still returned a fluent,
structured comparison naming Sunshine Health as "a subsidiary of Centene" --
recalled, not retrieved.
"""
from __future__ import annotations

from app.pipeline.react.prompts import REACT_GROUNDING_CONTRACT_TEXT
from app.services.react_block_seed import COMPOSITIONS, _react_block_specs


def test_grounding_contract_registered_in_block_specs():
    keys = {s.block_key for s in _react_block_specs()}
    assert "react.grounding_contract" in keys


def test_grounding_contract_member_of_all_three_react_compositions():
    for module_key in ("react_explore", "react_synthesize", "react_draft"):
        assert "react.grounding_contract" in COMPOSITIONS[module_key]["members"]
    # And positioned after critical_rules, before user_profile -- same
    # "substantive rule blocks together" grouping as the rest of the set.
    members = COMPOSITIONS["react_draft"]["members"]
    assert members.index("react.critical_rules") < members.index("react.grounding_contract")
    assert members.index("react.grounding_contract") < members.index("react.user_profile")


def test_partial_grounding_is_the_primary_example_not_refusal():
    # feedback_example_undermines_the_rule: whatever example is shown gets
    # copied. The partial case (some entities found, others not) must
    # appear before the full-miss/refusal case, not after it.
    text = REACT_GROUNDING_CONTRACT_TEXT
    partial_idx = text.index("PARTIAL")
    full_miss_idx = text.index("If NO tool call returned anything usable")
    assert partial_idx < full_miss_idx


def test_carve_outs_present_for_the_three_named_legitimate_paths():
    text = REACT_GROUNDING_CONTRACT_TEXT
    assert "product_help_search" in text  # rule 3b's product-identity path
    assert "clarifying question" in text  # rule 12's ask-the-user path
    assert "Arithmetic or reformatting" in text  # reasoning over kept evidence


def test_does_not_gate_on_whether_a_tool_was_called_this_round():
    # The rule must gate on traceability of the CLAIM, not on "a tool ran
    # this round" -- otherwise it would break the clarifying-question path
    # (no tool call, no claim either) and any final answer built from
    # evidence kept in an EARLIER round (no tool call THIS round, but the
    # claim still traces to something retrieved).
    text = REACT_GROUNDING_CONTRACT_TEXT
    assert "traces to something a tool actually returned" in text.lower() or \
           "trace to something a tool actually returned" in text.lower()
