#!/usr/bin/env python3
"""CLI smoke check for credentialing step runner (no chat server required).

Usage (from mobius-chat repo root):
  uv run python scripts/validate_credentialing_pipeline.py
  uv run python scripts/validate_credentialing_pipeline.py --org "Some Org"

Exit 0 if invariants hold; non-zero on failure.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Allow `python scripts/validate_credentialing_pipeline.py` without PYTHONPATH
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--org",
        default="",
        help="Optional org name to run full orchestrator (may call provider-roster API if configured).",
    )
    args = parser.parse_args()

    from app.services.roster_credentialing_orchestrator import (
        ROSTER_CREDENTIALING_PLAN,
        ROSTER_CREDENTIALING_STEP_IDS,
        OrchestratorState,
        StepState,
        run_credentialing_step,
        run_orchestrator,
    )

    errors: list[str] = []

    if tuple(s["id"] for s in ROSTER_CREDENTIALING_PLAN) != ROSTER_CREDENTIALING_STEP_IDS:
        errors.append("ROSTER_CREDENTIALING_STEP_IDS does not match ROSTER_CREDENTIALING_PLAN order")

    state = OrchestratorState(steps=[], org_npis=[])
    try:
        run_credentialing_step("x", state, "__invalid__", emitter=None)
        errors.append("expected ValueError for invalid step id")
    except ValueError:
        pass

    text, st = run_orchestrator("", emitter=None)
    if "No organization name provided" not in text:
        errors.append(f"empty org expected no-org message, got: {text[:80]!r}")

    if args.org.strip():
        text2, st2 = run_orchestrator(args.org.strip(), emitter=None)
        print("run_orchestrator sample:", text2[:200].replace("\n", " "), "...")
        for sid in ROSTER_CREDENTIALING_STEP_IDS:
            s = st2.step_by_id(sid)
            if s is None:
                errors.append(f"missing step state for {sid}")
        print("step statuses:", {sid: st2.step_by_id(sid).status for sid in ROSTER_CREDENTIALING_STEP_IDS})

    # Single-step on synthetic state
    st3 = OrchestratorState(
        steps=[StepState(id=s["id"], label=s["label"]) for s in ROSTER_CREDENTIALING_PLAN],
        org_npis=[],
    )
    st3.org_name = "SmokeOrg"
    run_credentialing_step("SmokeOrg", st3, "ensure_benchmarks", emitter=None)
    eb = st3.step_by_id("ensure_benchmarks")
    if eb is None or eb.status not in ("done", "skipped"):
        errors.append(f"ensure_benchmarks expected done/skipped, got {eb}")

    if errors:
        for e in errors:
            print("ERROR:", e, file=sys.stderr)
        return 1

    print("OK: credentialing step list and runner invariants passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
