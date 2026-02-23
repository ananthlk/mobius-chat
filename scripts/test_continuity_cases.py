#!/usr/bin/env python3
"""Test continuity cases: jurisdiction change, follow-up with "it", through pipeline stages.

Runs classify + plan (with mocked parse to avoid LLM) + blueprint to verify:
- "how about for United" -> jurisdiction_change -> refined_query has United
- "can you search for it" + last turn -> is_followup -> refined_query has prior topic

Usage:
  PYTHONPATH=mobius-chat python mobius-chat/scripts/test_continuity_cases.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

CHAT_ROOT = Path(__file__).resolve().parent.parent
if str(CHAT_ROOT) not in sys.path:
    sys.path.insert(0, str(CHAT_ROOT))

_root = CHAT_ROOT.parent
for env_path in (CHAT_ROOT / ".env", _root / "mobius-config" / ".env", _root / ".env"):
    if env_path.exists():
        try:
            from dotenv import load_dotenv
            load_dotenv(env_path, override=False)
        except Exception:
            pass
        break


def _build_state(payer: str | None = None, refined_query: str | None = None) -> dict:
    active = {"payer": payer} if payer else {}
    if payer:
        active["jurisdiction_obj"] = {"payor": payer}
    return {
        "active": active,
        "open_slots": [],
        "refined_query": refined_query,
    }


def test_jurisdiction_change():
    """'how about for United' after 'what is care management for Sunshine' -> refined_query has United."""
    from app.state.refined_query import classify_message, compute_refined_query

    # Turn 1 state
    refined1 = "what is the care management program for Sunshine Health"
    merged1 = _build_state(payer="Sunshine Health", refined_query=refined1)

    # Turn 2: user says "how about for United Healthcare"
    msg2 = "how about for United Healthcare"
    last_turn = {"user_content": "what is the care management program for Sunshine", "assistant_content": "Sunshine Health has a care management program that..."}
    merged2 = _build_state(payer="United Healthcare", refined_query=refined1)

    class2 = classify_message(msg2, last_turn, [], refined1)
    assert class2 == "jurisdiction_change", f"Expected jurisdiction_change, got {class2}"

    refined2 = compute_refined_query(class2, msg2, refined1, merged2, "what is the care management program for United Healthcare")
    assert "United" in refined2, f"Expected United in refined_query, got {refined2}"
    assert "care management" in refined2, f"Expected care management in refined_query, got {refined2}"
    print("[OK] Jurisdiction change: refined_query has United + care management")


def test_followup_can_you_search_for_it():
    """'can you search the web for it' after income question -> refined_query has prior topic."""
    from app.state.refined_query import classify_message, compute_refined_query

    refined1 = "specific income criteria for Florida Medicaid"
    merged1 = _build_state(payer="Sunshine Health", refined_query=refined1)
    last_turn = {
        "user_content": "A member has income $1500. Do they meet eligibility?",
        "assistant_content": "Eligibility is determined by DCF for Florida Medicaid. You can check income thresholds with the Department of Children and Families.",
    }

    msg2 = "can you search the web for it"
    class2 = classify_message(msg2, last_turn, [], refined1)
    refined2 = compute_refined_query(
        class2, msg2, refined1, merged1, "can you search the web for it", last_turn=last_turn
    )
    assert "income" in refined2 or "Medicaid" in refined2, f"Expected prior topic in refined_query, got {refined2}"
    print("[OK] Follow-up 'can you search for it': refined_query expanded with prior topic")


def test_reframe_with_followup():
    """reframe_for_retrieval with is_followup -> concrete query for RAG/tool."""
    from app.state.query_refinement import reframe_for_retrieval

    out = reframe_for_retrieval(
        "can you search for it",
        intent=None,
        question_intent=None,
        last_refined_query="income eligibility criteria for Florida Medicaid",
        jurisdiction={"payor": "Sunshine Health", "state": "Florida", "program": "Medicaid"},
        is_followup=True,
    )
    assert "income" in out or "eligibility" in out
    assert "Sunshine" in out or "Florida" in out or "Medicaid" in out
    print("[OK] reframe_for_retrieval follow-up: concrete query produced")


def test_full_pipeline_jurisdiction_change():
    """Run classify + plan through pipeline with mocked plan (no LLM)."""
    from app.pipeline.context import PipelineContext
    from app.stages.classify import run_classify
    from app.state.context_pack import build_context_pack
    from app.state.context_router import route_context

    # Turn 2 state: user already asked about Sunshine, now "how about for United"
    merged_state = _build_state(payer="United Healthcare", refined_query="how do I file an appeal for Sunshine Health")
    last_turns = [
        {
            "user_content": "how do I file an appeal for Sunshine Health",
            "assistant_content": "For Sunshine Health, a member may file an appeal orally. See the Provider Manual for details.",
        }
    ]

    ctx = PipelineContext(
        correlation_id="test-continuity",
        thread_id="test-thread",
        message="how about for United Healthcare",
    )
    ctx.merged_state = merged_state
    ctx.last_turns = last_turns
    route = route_context(ctx.message, ctx.merged_state, ctx.last_turns, reset_reason=None)
    ctx.context_pack = build_context_pack(route, ctx.merged_state, ctx.last_turns, [])

    run_classify(ctx, emitter=None)
    assert ctx.classification == "jurisdiction_change"
    assert "United" in (ctx.effective_message or "")
    print("[OK] Full pipeline: classify -> jurisdiction_change, effective_message has United")


def main() -> int:
    print("Continuity cases test")
    print("=" * 60)
    try:
        test_jurisdiction_change()
        test_followup_can_you_search_for_it()
        test_reframe_with_followup()
        test_full_pipeline_jurisdiction_change()
        print("=" * 60)
        print("All continuity cases passed.")
        return 0
    except AssertionError as e:
        print(f"[FAIL] {e}")
        return 1
    except Exception as e:
        print(f"[ERROR] {e}")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
