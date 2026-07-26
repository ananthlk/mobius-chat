"""Structural enforcement of the bubble-backend contract (docs/bubble-backend-contract.md).

These are the guards that turn the BFF invariants into build-failing tests, not vibes:
  - import-guard: bubble-backend may not import the "brain" (router/planner/enricher/llm/
    retriever). If it ever does, presentation has drifted into enrich -> RED.
  - allowlist-drop: the positive filter must never let a non-allowlisted internal field reach
    the client. If it does, consolidation silently widened the FE leak surface -> RED.
"""
from __future__ import annotations

import ast
from pathlib import Path

from app.ux import bubble_backend

_MODULE_PATH = Path(bubble_backend.__file__)

# Substrings that must never appear in an imported module path. bubble-backend reads produced
# outputs passed as arguments; it must import none of the machinery that produced them.
_FORBIDDEN_IMPORT_SUBSTRINGS = (
    "planner",
    "router",
    "responder",      # final_parallel / the enricher emit lives here
    "retriev",
    "rerank",
    "enrich",
    "agents",         # the ReAct loop
    "model_registry",
    "llm_call",
    "llm_client",
    "openai",
    "anthropic",
)


def _imported_module_paths(py_file: Path) -> list[str]:
    tree = ast.parse(py_file.read_text(), filename=str(py_file))
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                names.append(node.module)
    return names


def test_import_guard_no_brain_imports():
    """bubble-backend imports none of router/planner/enricher/llm/retriever (presentation-only)."""
    imported = _imported_module_paths(_MODULE_PATH)
    offenders = [
        name for name in imported
        if any(bad in name.lower() for bad in _FORBIDDEN_IMPORT_SUBSTRINGS)
    ]
    assert offenders == [], (
        f"bubble-backend imported forbidden 'brain' modules {offenders}; it must stay a "
        f"read-only, presentation-only aggregator (contract invariants 1 & 2)."
    )


def test_import_guard_only_stdlib():
    """Positive form: bubble-backend imports only stdlib (json, typing). The point of the module."""
    imported = _imported_module_paths(_MODULE_PATH)
    non_stdlib = [n for n in imported if n.split(".")[0] not in {"json", "typing", "__future__"}]
    assert non_stdlib == [], f"bubble-backend gained non-stdlib imports {non_stdlib}"


def test_allowlist_drops_internal_fields():
    """A non-allowlisted internal field on the produced card is ABSENT from the client output."""
    produced = {
        "citations": [{"id": "1"}],            # allowlisted -> should pass
        "raw_reasoning": "chain of thought",   # internal -> must be dropped
        "source_confidence_override": "approved_authoritative",  # internal -> must be dropped
        "_debug_scratch": {"x": 1},            # internal -> must be dropped
    }
    card = bubble_backend.shape_answer_card("FACTUAL", "hi", [], extra_from=produced)
    assert card["citations"] == [{"id": "1"}]          # allowlisted survives
    assert "raw_reasoning" not in card                 # internal dropped
    assert "source_confidence_override" not in card    # internal dropped
    assert "_debug_scratch" not in card                # internal dropped


def test_allowlist_is_positive_not_passthrough():
    """Every key on the output is either a base field or explicitly allowlisted — no leakage."""
    produced = {k: "v" for k in bubble_backend.ANSWER_CARD_ENVELOPE_KEYS}
    produced["surprise_internal_field"] = "leak"
    card = bubble_backend.shape_answer_card("FACTUAL", "hi", [], extra_from=produced)
    allowed = set(bubble_backend.ANSWER_CARD_ENVELOPE_KEYS) | {"mode", "direct_answer", "sections"}
    assert set(card).issubset(allowed), f"non-allowlisted keys leaked: {set(card) - allowed}"


def test_base_fields_always_present_and_none_values_dropped():
    card = bubble_backend.shape_answer_card(
        "FACTUAL", "answer", [{"label": "s"}],
        extra_from={"citations": None, "takeaways": ["t"]},
    )
    assert card["mode"] == "FACTUAL"
    assert card["direct_answer"] == "answer"
    assert card["sections"] == [{"label": "s"}]
    assert "citations" not in card   # None values are dropped, not passed as null
    assert card["takeaways"] == ["t"]


def test_shape_matches_legacy_integrate_output():
    """bubble-backend's JSON output is identical to integrate.py's _answer_card_json_for_client,
    so the cutover (integrate delegates here) is a pure move with no output-shape change."""
    import json
    mode, da, sections = "CANONICAL", "the answer", [{"label": "a", "bullets": ["b"]}]
    extra = {"citations": [{"id": "1"}], "takeaways": ["t"], "internal_only": "x"}
    out = json.loads(bubble_backend.shape_answer_card_json(mode, da, sections, extra_from=extra))
    # Replicate the legacy allowlist copy exactly:
    expected = {"mode": mode, "direct_answer": da, "sections": sections}
    for k in bubble_backend.ANSWER_CARD_ENVELOPE_KEYS:
        v = extra.get(k)
        if v is not None:
            expected[k] = v
    assert out == expected


# --- B/C typed-block builders (task #12): shape parity with assistant_envelope --------------

def _card():
    return {
        # assistant_envelope gates takeaways/gaps/action_chips behind a direct_answer (line 453);
        # the gating stays in the caller — these per-block builders just shape the field.
        "direct_answer": "the answer",
        "correction": {"original": "old", "corrected": "new"},
        "takeaways": ["  keep this  ", "", "and this", None],
        "gaps": ["rate table stale", ""],
        "suggested_actions": [
            {"type": "external_link", "label": "Appeals", "url": "https://x"},
            {"type": "other", "label": "skip", "url": "https://y"},  # filtered
        ],
        "next_steps": ["confirm units", {"text": "check plan", "clickable": True}],
        "next_questions_for_user": ["what ceiling?"],
    }


def test_bc_block_shapes_unit():
    c = _card()
    assert bubble_backend.build_correction_block(c) == {"type": "correction", "original": "old", "corrected": "new"}
    assert bubble_backend.build_takeaways_block(c) == {"type": "takeaways", "items": ["keep this", "and this"]}
    gap = bubble_backend.build_gaps_callout_block(c)
    assert gap["type"] == "callout" and gap["variant"] == "info" and "rate table stale" in gap["body"]
    chips = bubble_backend.build_action_chips_block(c)
    assert chips == {"type": "action_chips", "chips": [{"type": "external_link", "label": "Appeals", "url": "https://x"}]}
    ns = bubble_backend.build_next_steps_block(c["next_steps"], collapsed_default=False)
    assert ns == {"type": "next_steps", "items": [{"text": "confirm units", "clickable": False}, {"text": "check plan", "clickable": True}], "collapsed_default": False}
    nq = bubble_backend.build_next_questions_block(c["next_questions_for_user"], collapsed_default=True)
    assert nq == {"type": "suggested_questions", "items": [{"text": "what ceiling?", "clickable": True}], "collapsed_default": True}


def test_bc_block_none_when_absent():
    empty = {}
    assert bubble_backend.build_correction_block(empty) is None
    assert bubble_backend.build_takeaways_block(empty) is None
    assert bubble_backend.build_gaps_callout_block(empty) is None
    assert bubble_backend.build_action_chips_block(empty) is None
    assert bubble_backend.build_next_steps_block([], collapsed_default=False) is None


def test_bc_block_parity_with_assistant_envelope():
    """bubble-backend's per-block output is byte-identical to assistant_envelope's inline build,
    so the cutover (assistant_envelope delegates to these) is a pure move — one builder."""
    from app.communication.assistant_envelope import build_assistant_envelope_v1
    from app.communication.followup_next_steps_quality import followup_blocks_collapsed_default
    c = _card()
    env = build_assistant_envelope_v1(
        answer_card=c, ui_blocks_raw=None, tool_fired="", response_sources=[],
        next_steps=c["next_steps"], next_questions_for_user=c["next_questions_for_user"],
        roster_report_final_md=None, has_roster_pdf=False, source_confidence_strip="",
    )
    by_type = {b["type"]: b for b in env["blocks"]}
    collapsed = followup_blocks_collapsed_default("")
    assert by_type["correction"] == bubble_backend.build_correction_block(c)
    assert by_type["takeaways"] == bubble_backend.build_takeaways_block(c)
    assert by_type["callout"] == bubble_backend.build_gaps_callout_block(c)
    assert by_type["action_chips"] == bubble_backend.build_action_chips_block(c)
    assert by_type["next_steps"] == bubble_backend.build_next_steps_block(c["next_steps"], collapsed_default=collapsed)
    assert by_type["suggested_questions"] == bubble_backend.build_next_questions_block(c["next_questions_for_user"], collapsed_default=collapsed)
