#!/usr/bin/env python3
"""Run full pipeline and stream thinking output (simulates worker + UI).

Run from Mobius root:
  PYTHONPATH=mobius-chat python mobius-chat/scripts/run_pipeline_stream.py "Can you find the ICD code for Socio Psycho Rehab, see if this is a covered benefit under Medicaid in Florida and see if Sunshine Health requires prior authorization"

Prints every thinking chunk and final outcome (resolvable / clarification / refinement).
Use to verify what the current codebase actually emits.
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


def main() -> int:
    message = " ".join(sys.argv[1:]).strip()
    if not message:
        message = (
            "Can you find the ICD code for Socio Psycho Rehab, see if this is a covered benefit "
            "under Medicaid in Florida and see if Sunshine Health requires prior authorization"
        )

    print("=" * 80)
    print("PIPELINE STREAM (thinking + outcome)")
    print("=" * 80)
    print(f"\nQuery: {message}\n")

    thinking_chunks: list[str] = []

    def on_thinking(chunk: str) -> None:
        if chunk and str(chunk).strip():
            thinking_chunks.append(str(chunk).strip())
            print(f"  [thinking] {chunk.strip()}")

    try:
        from app.pipeline.context import PipelineContext
        from app.stages.state_load import run_state_load
        from app.stages.classify import run_classify
        from app.stages.plan import run_plan
        from app.stages.clarify import run_clarify

        ctx = PipelineContext(correlation_id="stream-test", thread_id=None, message=message)
        ctx.thinking_chunks = []

        # Simulate state_load (no emitter)
        run_state_load(ctx)

        # Classify
        run_classify(ctx, emitter=None)

        # Plan (emits thinking)
        print("--- PLAN stage ---")
        run_plan(ctx, emitter=on_thinking)
        print(f"  subquestions: {len(ctx.plan.subquestions) if ctx.plan else 0}")
        print(f"  has task_plan: {getattr(ctx.plan, 'task_plan', None) is not None}")
        print()

        # Clarify
        print("--- CLARIFY stage ---")
        resolvable = run_clarify(ctx, emitter=on_thinking)
        print(f"  resolvable: {resolvable}")
        if ctx.should_refine:
            print(f"  should_refine: True, suggestions: {ctx.refinement_suggestions[:3]}...")
        if ctx.needs_clarification:
            print(f"  needs_clarification: {ctx.clarification_message[:80]}...")
        print()

        print("=" * 80)
        print("THINKING CHUNKS (in order):")
        for i, c in enumerate(thinking_chunks, 1):
            print(f"  {i}. {c}")
        print()
        print("OUTCOME:", "RESOLVABLE" if resolvable else "CLARIFICATION/REFINEMENT")
        print("=" * 80)
        return 0 if resolvable else 1
    except Exception as e:
        print(f"ERROR: {e}")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
