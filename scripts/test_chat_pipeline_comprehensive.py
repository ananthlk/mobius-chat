#!/usr/bin/env python3
"""Day 3 gate: Full pipeline runs without crash; structured response.

Verifies error boundaries and _publish_failed robustness.
Run: PYTHONPATH=mobius-chat python mobius-chat/scripts/test_chat_pipeline_comprehensive.py --scenario 1 --runs 3
"""
from __future__ import annotations

import argparse
import os
import sys
import uuid
from pathlib import Path

os.environ.setdefault("QUEUE_TYPE", "memory")

CHAT_ROOT = Path(__file__).resolve().parent.parent
if str(CHAT_ROOT) not in sys.path:
    sys.path.insert(0, str(CHAT_ROOT))

for env_path in (CHAT_ROOT / ".env", CHAT_ROOT.parent / "mobius-config" / ".env"):
    if env_path.exists():
        try:
            from dotenv import load_dotenv
            load_dotenv(env_path, override=False)
        except Exception:
            pass
        break

# Scenarios from Day 3 plan
SCENARIOS = {
    1: ("What are the qualifications for care management?", {"clarification", "completed"}),
    2: ("What can you do?", {"completed"}),
    3: ("Hello", {"clarification", "completed"}),
    4: ("What is the status for MRN 98765?", {"clarification", "completed"}),
    5: ("Search for Florida Medicaid eligibility requirements", {"completed"}),
    6: (
        "A member has income of $1500/month and needs Medicaid. What are the requirements?",
        {"clarification", "completed"},
    ),
    7: ("How do I file an appeal?", {"clarification", "completed"}),
}


def run_one_scenario(scenario_id: int) -> tuple[bool, str]:
    """Run pipeline for one scenario. Returns (passed, message)."""
    if scenario_id not in SCENARIOS:
        return False, f"Unknown scenario {scenario_id}"
    msg, allowed = SCENARIOS[scenario_id]
    cid = str(uuid.uuid4())
    try:
        from app.pipeline.orchestrator import run_pipeline
        from app.storage import get_response

        run_pipeline(cid, msg, thread_id=None)
        resp = get_response(cid)
        if resp is None:
            from app.queue import get_queue
            resp = get_queue().get_response(cid)
        if resp is None:
            return False, f"No response for scenario {scenario_id}"
        status = resp.get("status")
        if status not in allowed and status != "failed":
            return False, f"Scenario {scenario_id}: status={status} not in {allowed}"
        if status == "failed":
            return False, f"Scenario {scenario_id}: pipeline returned failed"
        return True, f"Scenario {scenario_id} ok (status={status})"
    except Exception as e:
        return False, f"Scenario {scenario_id} CRASH: {e}"


def main() -> int:
    parser = argparse.ArgumentParser(description="Day 3 gate: pipeline comprehensive")
    parser.add_argument("--scenario", type=int, default=None, help="Scenario ID 1-7 (omit for all)")
    parser.add_argument("--runs", type=int, default=1, help="Number of runs per scenario (flakiness check)")
    args = parser.parse_args()
    scenarios_to_run = list(SCENARIOS) if args.scenario is None else [args.scenario]
    failed = 0
    total = 0
    for sid in scenarios_to_run:
        for i in range(args.runs):
            total += 1
            ok, msg = run_one_scenario(sid)
            if ok:
                print(f"[OK] {msg}")
            else:
                print(f"[FAIL] {msg}")
                failed += 1
    if failed:
        print(f"\n{failed}/{total} failed")
        return 1
    print(f"\nAll {total} runs passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
