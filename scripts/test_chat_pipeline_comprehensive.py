#!/usr/bin/env python3
"""Day 3 gate: Pipeline runs without crash; returns structured response.

Runs the full chat pipeline (state_load → classify → plan → clarify → resolve → integrate)
and verifies we get a structured payload (completed, clarification, refinement_ask, or failed).
No unhandled exceptions.

Usage:
  PYTHONPATH=mobius-chat python mobius-chat/scripts/test_chat_pipeline_comprehensive.py
  PYTHONPATH=mobius-chat python mobius-chat/scripts/test_chat_pipeline_comprehensive.py --scenario 1
  PYTHONPATH=mobius-chat python mobius-chat/scripts/test_chat_pipeline_comprehensive.py --runs 3

Gate: Run 3x with 0 crashes (python script --runs 3 or invoke script 3 times).
"""
from __future__ import annotations

import argparse
import os
import sys
import uuid
from pathlib import Path

# Ensure memory queue for in-process test
os.environ.setdefault("QUEUE_TYPE", "memory")

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

# Re-apply after dotenv in case .env overrides
os.environ["QUEUE_TYPE"] = "memory"

VALID_STATUSES = frozenset({"completed", "clarification", "refinement_ask", "failed"})


SCENARIOS: dict[int, str] = {
    1: "What are the qualifications for care management?",
    2: "What can you do?",
    3: "Hello",
    4: "What is the status for MRN 98765?",
    5: "Search for Florida Medicaid eligibility requirements",
    6: "A member has income of $1500/month and two chronic conditions. Do they meet eligibility?",
    7: "How do I file an appeal?",
}


def run_one_scenario(scenario_id: int, scenario_message: str) -> bool:
    """Run pipeline for one message. Returns True if no crash and structured response."""
    from app.pipeline.orchestrator import run_pipeline
    from app.queue import get_queue

    correlation_id = str(uuid.uuid4())
    try:
        run_pipeline(correlation_id, scenario_message, None)
    except Exception as e:
        print(f"[CRASH] Scenario {scenario_id}: {e}")
        return False

    q = get_queue()
    resp = q.get_response(correlation_id)
    if resp is None:
        from app.storage import get_response as storage_get_response
        resp = storage_get_response(correlation_id)

    if resp is None:
        print(f"[FAIL] Scenario {scenario_id}: No response published")
        return False

    status = resp.get("status")
    if status not in VALID_STATUSES:
        print(f"[FAIL] Scenario {scenario_id}: Unexpected status {status!r}")
        return False

    print(f"[OK] Scenario {scenario_id}: status={status}")
    return True


def main():
    parser = argparse.ArgumentParser(description="Comprehensive pipeline gate")
    parser.add_argument("--scenario", type=int, default=None, help="Run specific scenario 1-7")
    parser.add_argument("--runs", type=int, default=1, help="Number of runs (for flakiness check)")
    args = parser.parse_args()

    if args.scenario is not None:
        if args.scenario not in SCENARIOS:
            print(f"Unknown scenario {args.scenario}. Valid: 1-{len(SCENARIOS)}")
            sys.exit(1)
        scenarios_to_run = [(args.scenario, SCENARIOS[args.scenario])]
    else:
        scenarios_to_run = [(i, m) for i, m in SCENARIOS.items()]

    total = len(scenarios_to_run) * args.runs
    passed = 0
    for run in range(args.runs):
        if args.runs > 1:
            print(f"\n--- Run {run + 1}/{args.runs} ---")
        for scenario_id, message in scenarios_to_run:
            if run_one_scenario(scenario_id, message):
                passed += 1

    print(f"\n{passed}/{total} passed")
    if passed < total:
        sys.exit(1)
    print("All pipeline runs completed without crash.")


if __name__ == "__main__":
    main()
