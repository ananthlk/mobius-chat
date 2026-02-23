#!/usr/bin/env python3
"""Trace the Medicaid patient scenario query through plan + clarify to verify tweaks.

Run from Mobius root:
  PYTHONPATH=mobius-chat python mobius-chat/scripts/trace_medicaid_clarify.py

Verifies:
1. Plan decomposes into 3+ subquestions
2. Clarify skips refinement (concrete scenario: age, income, location)
3. Blueprint adds on_rag_fail: search_google for eligibility subquestions
"""
from __future__ import annotations

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

MEDICAID_QUERY = (
    "I have a patient, female who is 35 years old and makes $1200 per month "
    "with a 10 year old kid. She lives in Tampa Florida, I wanted to see if "
    "she may qualify for medicaid and if so what health plans serve that region "
    "and how to get her enrolled"
)


def main() -> int:
    print("=" * 80)
    print("TRACE: Medicaid patient scenario (plan + clarify + blueprint)")
    print("=" * 80)
    print(f"\nQuery: {MEDICAID_QUERY[:120]}...")
    print()

    # --- Stage 1: Plan ---
    print("-" * 60)
    print("STAGE 1: PLANNER")
    print("-" * 60)
    from app.planner import parse

    plan = parse(MEDICAID_QUERY, thinking_emitter=None)
    print(f"  subquestions: {len(plan.subquestions)}")
    for sq in plan.subquestions:
        print(f"    {sq.id}: kind={sq.kind} text={sq.text[:60]}...")
    print()

    # --- Stage 2: Clarify (with our tweak: concrete scenario skips refinement) ---
    print("-" * 60)
    print("STAGE 2: CLARIFY")
    print("-" * 60)
    from app.pipeline.context import PipelineContext
    from app.stages.plan import run_plan
    from app.stages.clarify import run_clarify
    from app.state.query_refinement import _has_concrete_scenario

    ctx = PipelineContext(correlation_id="test", thread_id=None, message=MEDICAID_QUERY)
    ctx.plan = plan
    ctx.merged_state = {}  # No prior jurisdiction selection
    ctx.effective_message = MEDICAID_QUERY
    ctx.classification = {"intent": "new_question", "slots": {}}

    has_scenario = _has_concrete_scenario(MEDICAID_QUERY)
    print(f"  _has_concrete_scenario: {has_scenario}")

    resolvable = run_clarify(ctx, emitter=None)
    print(f"  resolvable: {resolvable}")
    if ctx.should_refine:
        print(f"  would ask REFINEMENT: {ctx.refinement_suggestions}")
    elif ctx.needs_clarification:
        print(f"  would ask CLARIFICATION: {ctx.clarification_message}")
    else:
        print("  -> PROCEEDING (no refinement, no jurisdiction clarification)")
    print()

    # --- Stage 3: Blueprint (with on_rag_fail for eligibility) ---
    if resolvable:
        print("-" * 60)
        print("STAGE 3: BLUEPRINT")
        print("-" * 60)
        from app.planner.blueprint import build_blueprint
        from app.chat_config import get_chat_config

        ctx.blueprint = build_blueprint(
            plan,
            rag_default_k=get_chat_config().rag.top_k,
            retrieval_ctx={"user_message": MEDICAID_QUERY},
        )
        for i, entry in enumerate(ctx.blueprint):
            on_fail = entry.get("on_rag_fail") or []
            has_google = "search_google" in [str(x).lower() for x in on_fail]
            print(f"  {entry['sq_id']}: agent={entry['agent']} on_rag_fail={on_fail} (has search_google={has_google})")
    else:
        print("(Skipping blueprint: not resolvable)")

    print()
    print("=" * 80)
    print("EXPECTED with tweaks: resolvable=True, eligibility sq has on_rag_fail=['search_google']")
    print("=" * 80)
    return 0


if __name__ == "__main__":
    sys.exit(main())
