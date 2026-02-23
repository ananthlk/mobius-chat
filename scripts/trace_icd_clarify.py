#!/usr/bin/env python3
"""Trace the ICD + coverage + prior auth query through plan + clarify (prompt-driven test).

Run from Mobius root:
  PYTHONPATH=mobius-chat python mobius-chat/scripts/trace_icd_clarify.py

Verifies prompt-driven clarification:
1. Plan decomposes into 3 subquestions (ICD code, coverage, prior auth)
2. Clarify skips refinement (Mobius plan with task_plan)
3. Proceeds to resolve all parts
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

ICD_QUERY = (
    "Can find the ICD code for Socio Psycho Rehab, see if this is a covered benefit "
    "under Medicaid in Florida and see if Sunshine Health requires prior authorization"
)


def main() -> int:
    print("=" * 80)
    print("TRACE: ICD + coverage + prior auth (plan + clarify)")
    print("=" * 80)
    print(f"\nQuery: {ICD_QUERY}")
    print()

    # --- Stage 1: Plan ---
    print("-" * 60)
    print("STAGE 1: PLANNER")
    print("-" * 60)
    from app.planner import parse

    plan = parse(ICD_QUERY, thinking_emitter=None)
    print(f"  subquestions: {len(plan.subquestions)}")
    for sq in plan.subquestions:
        print(f"    {sq.id}: kind={sq.kind} text={sq.text[:65]}...")
    has_task_plan = getattr(plan, "task_plan", None) is not None
    print(f"  has task_plan (Mobius): {has_task_plan}")
    print()

    # --- Stage 2: Clarify ---
    print("-" * 60)
    print("STAGE 2: CLARIFY")
    print("-" * 60)
    from app.pipeline.context import PipelineContext
    from app.stages.clarify import run_clarify

    ctx = PipelineContext(correlation_id="test", thread_id=None, message=ICD_QUERY)
    ctx.plan = plan
    ctx.merged_state = {}
    ctx.effective_message = ICD_QUERY
    ctx.classification = {"intent": "new_question", "slots": {}}

    resolvable = run_clarify(ctx, emitter=None)
    print(f"  resolvable: {resolvable}")
    if ctx.should_refine:
        print(f"  would ask REFINEMENT: {ctx.refinement_suggestions}")
    elif ctx.needs_clarification:
        print(f"  would ask CLARIFICATION: {ctx.clarification_message}")
    elif ctx.needs_route_clarification:
        print("  would ask ROUTE clarification")
    else:
        print("  -> PROCEEDING (no refinement, no clarification)")
    print()

    print("=" * 80)
    print("EXPECTED (prompt-driven): resolvable=True, no refinement for multi-part query")
    print("=" * 80)
    return 0 if resolvable else 1


if __name__ == "__main__":
    sys.exit(main())
