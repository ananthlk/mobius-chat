#!/usr/bin/env python3
"""Phase 13.6 latency bench — retrieval turn vs. transform-continuation turn.

Validates the structural premise of the conversation-aware planner:
a continuation request that reshapes the prior answer should be
materially faster than the retrieval turn that produced that answer,
because the transform path skips corpus + curator + synthesis and
makes a single LLM call against the prior text.

Procedure (per scenario):
  1. POST a fresh question, same thread_id reused for turn 2.
  2. Wait for turn 1 to complete; record duration_ms +
     first_chunk_ms + tools_used + retrieval_signals.
  3. POST a continuation in the same thread ("convert this to an
     appeal letter", "make it shorter", etc.).
  4. Wait for turn 2 to complete; record the same metrics.
  5. Print per-scenario delta and an aggregate table.

Pass criteria (informational, not enforced — bench, not test):
  - Turn 2 completes in <50% of turn 1's wall clock for the same
    scenario, OR
  - Turn 2 reports tools_used ⊆ {transform_previous_answer} (i.e. no
    retrieval), regardless of timing.

Usage:
    python scripts/bench_transform_latency.py \\
        --base-url https://mobius-chat-ortabkknqa-uc.a.run.app \\
        --out /tmp/transform_bench.json

    # Local:
    python scripts/bench_transform_latency.py \\
        --base-url http://localhost:8000

If the deployed instance requires auth, pass --bearer-token; the bench
inherits CHAT_DEV_BEARER if set.

Reuses the streaming + polling primitives from bench_chat_e2e.py so
the metric semantics stay identical. We only diverge in the
two-turn-per-scenario shape.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import httpx

# Reuse the proven primitives so timing semantics line up with the
# main e2e bench.
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from bench_chat_e2e import (  # noqa: E402
    TurnResult,
    _extract_metrics_from_completed,
    _poll_fallback,
    _post_chat,
    _stream_turn,
)

logger = logging.getLogger(__name__)


# ── Scenarios: paired (retrieval, continuation) prompts ───────────────
#
# The first turn is a substantive payer-policy question that should
# trip the retrieval cascade (search_corpus +/- curator). The second
# turn is a continuation referencing the prior answer — pronoun /
# transformation verb / artifact request — which Phase 13.6 should
# route through transform_previous_answer.

SCENARIOS: list[dict[str, str]] = [
    {
        "id": "appeal_letter",
        "fresh": (
            "I'm working a Sunshine Health Florida Medicaid claim for a "
            "16-year-old patient who completed a 90-day residential "
            "psychiatric stay. Days 36-90 are denying as duplicate. "
            "What's the right billing approach for a split residential stay?"
        ),
        "continuation": "can you convert this to an appeal letter to be sent to Sunshine Health",
    },
    {
        "id": "shorter",
        "fresh": (
            "What's the prior authorization timeline for Sunshine Health "
            "H0036 community mental-health services in Florida?"
        ),
        "continuation": "make that much shorter — bullet points only",
    },
    {
        "id": "audience_rewrite",
        "fresh": (
            "How do I file an appeal for a denied claim with Sunshine "
            "Health, and what's the timely-filing window?"
        ),
        "continuation": "rewrite the above as an email to the credentialing team",
    },
    {
        "id": "counter_argument",
        "fresh": (
            "What documentation does Sunshine Health require for an "
            "EPSDT medical-necessity exception on a behavioral-health "
            "service?"
        ),
        "continuation": (
            "what would Sunshine's most likely counter-argument be, and "
            "how should I rebut it"
        ),
    },
]


@dataclass
class ScenarioResult:
    scenario_id: str
    thread_id: str = ""
    turn1: TurnResult | None = None
    turn2: TurnResult | None = None
    delta_ms: int | None = None
    speedup_x: float | None = None
    transform_was_used: bool = False
    notes: list[str] = field(default_factory=list)


# ── Per-turn driver ───────────────────────────────────────────────────


def _run_one_turn(
    client: httpx.Client,
    base_url: str,
    question: str,
    thread_id: str | None,
    bearer_token: str | None,
    timeout_s: int,
) -> tuple[TurnResult, str]:
    """Issue one chat turn end-to-end. Returns (TurnResult, thread_id)."""
    turn = TurnResult(
        question_id=str(uuid.uuid4())[:8],
        question=question,
    )
    t_start = time.perf_counter()
    try:
        cid, returned_thread = _post_chat(
            client, base_url, question,
            chat_mode="copilot",
            thread_id=thread_id,
            system_context=None,
            cache_assist=False,  # turn off cache so we measure the real path
            bearer_token=bearer_token,
        )
    except Exception as exc:
        turn.error = f"post failed: {exc}"
        turn.status = "error"
        return turn, thread_id or ""
    turn.correlation_id = cid

    # Stream first; fall back to poll on stream error so a single bad
    # SSE connection doesn't blank the whole bench row.
    completed = _stream_turn(client, base_url, cid, turn, timeout_s=timeout_s)
    if completed is None and not turn.error:
        turn.error = None  # clear so poll has a clean slate
        completed = _poll_fallback(client, base_url, cid, turn, timeout_s=timeout_s)
    if completed is None:
        turn.status = "timeout" if (turn.error or "").endswith("timeout") else "error"
        turn.duration_ms = int((time.perf_counter() - t_start) * 1000)
        return turn, returned_thread or thread_id or ""

    _extract_metrics_from_completed(turn, completed)
    turn.duration_ms = int((time.perf_counter() - t_start) * 1000)
    return turn, returned_thread or thread_id or ""


# ── Scenario driver ───────────────────────────────────────────────────


def _run_scenario(
    client: httpx.Client,
    base_url: str,
    scenario: dict[str, str],
    bearer_token: str | None,
    timeout_s: int,
) -> ScenarioResult:
    res = ScenarioResult(scenario_id=scenario["id"])

    # Turn 1: fresh question — retrieval expected.
    turn1, thread_id = _run_one_turn(
        client, base_url, scenario["fresh"],
        thread_id=None, bearer_token=bearer_token, timeout_s=timeout_s,
    )
    res.turn1 = turn1
    res.thread_id = thread_id

    if turn1.status not in ("completed", "clarification"):
        res.notes.append(f"turn1 not completed (status={turn1.status}); skipping turn2")
        return res
    if not thread_id:
        res.notes.append("no thread_id returned from turn1; cannot run continuation")
        return res

    # Brief settle so persistence of turn1's assistant message is
    # visible to turn2's state-load. 500ms is generous; pipeline writes
    # are sub-100ms typically.
    time.sleep(0.5)

    # Turn 2: continuation in the same thread — transform expected.
    turn2, _ = _run_one_turn(
        client, base_url, scenario["continuation"],
        thread_id=thread_id, bearer_token=bearer_token, timeout_s=timeout_s,
    )
    res.turn2 = turn2

    if turn2.status not in ("completed", "clarification"):
        res.notes.append(f"turn2 not completed (status={turn2.status})")
        return res

    # Compute deltas + transform-routing detection.
    if turn1.duration_ms and turn2.duration_ms:
        res.delta_ms = turn1.duration_ms - turn2.duration_ms
        if turn2.duration_ms > 0:
            res.speedup_x = round(turn1.duration_ms / turn2.duration_ms, 2)

    res.transform_was_used = "transform_previous_answer" in (turn2.tools_used or [])
    if not res.transform_was_used:
        res.notes.append(
            f"turn2 did NOT route through transform_previous_answer "
            f"(tools_used={turn2.tools_used}); planner may need stronger guidance"
        )
    return res


# ── Reporting ─────────────────────────────────────────────────────────


def _print_table(results: list[ScenarioResult]) -> None:
    print()
    print(f"{'scenario':<22} {'turn1_ms':>10} {'turn2_ms':>10} {'delta_ms':>10} {'speedup':>8} {'transform?':>11}")
    print("─" * 78)
    for r in results:
        t1 = (r.turn1.duration_ms if r.turn1 else 0) or 0
        t2 = (r.turn2.duration_ms if r.turn2 else 0) or 0
        d = r.delta_ms if r.delta_ms is not None else 0
        sp = f"{r.speedup_x:.2f}x" if r.speedup_x is not None else "—"
        tx = "yes" if r.transform_was_used else "no"
        print(f"{r.scenario_id:<22} {t1:>10} {t2:>10} {d:>+10} {sp:>8} {tx:>11}")
    print()
    # Aggregate
    paired = [r for r in results if r.turn1 and r.turn2 and r.turn2.duration_ms]
    if paired:
        avg_t1 = sum(r.turn1.duration_ms for r in paired) / len(paired)
        avg_t2 = sum(r.turn2.duration_ms for r in paired) / len(paired)
        transform_rate = sum(1 for r in paired if r.transform_was_used) / len(paired)
        print(
            f"avg turn1 (retrieval): {avg_t1:.0f}ms | "
            f"avg turn2 (continuation): {avg_t2:.0f}ms | "
            f"avg speedup: {avg_t1/avg_t2:.2f}x | "
            f"transform-routed: {transform_rate*100:.0f}%"
        )
    for r in results:
        if r.notes:
            print(f"  [{r.scenario_id}] " + "; ".join(r.notes))


def _serialize(results: list[ScenarioResult]) -> dict[str, Any]:
    out: list[dict[str, Any]] = []
    for r in results:
        out.append({
            "scenario_id": r.scenario_id,
            "thread_id": r.thread_id,
            "turn1": asdict(r.turn1) if r.turn1 else None,
            "turn2": asdict(r.turn2) if r.turn2 else None,
            "delta_ms": r.delta_ms,
            "speedup_x": r.speedup_x,
            "transform_was_used": r.transform_was_used,
            "notes": r.notes,
        })
    return {"scenarios": out, "generated_at": time.time()}


# ── CLI ───────────────────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", required=True, help="Chat API base URL (no trailing slash).")
    parser.add_argument("--out", default=None, help="Write JSON report to this path.")
    parser.add_argument("--bearer-token", default=os.environ.get("CHAT_DEV_BEARER"), help="Optional bearer token.")
    parser.add_argument("--timeout", type=int, default=300, help="Per-turn timeout in seconds (default 300).")
    parser.add_argument("--scenario", default=None, help="Run only the named scenario id (default: all).")
    args = parser.parse_args()

    base_url = args.base_url.rstrip("/")
    scenarios = SCENARIOS
    if args.scenario:
        scenarios = [s for s in SCENARIOS if s["id"] == args.scenario]
        if not scenarios:
            print(f"unknown scenario: {args.scenario}; choices: {[s['id'] for s in SCENARIOS]}", file=sys.stderr)
            return 2

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logger.info("bench_transform_latency: %d scenarios against %s", len(scenarios), base_url)

    results: list[ScenarioResult] = []
    with httpx.Client() as client:
        for scen in scenarios:
            logger.info("scenario=%s", scen["id"])
            res = _run_scenario(client, base_url, scen, args.bearer_token, args.timeout)
            results.append(res)

    _print_table(results)

    if args.out:
        Path(args.out).write_text(json.dumps(_serialize(results), indent=2, default=str))
        logger.info("wrote report -> %s", args.out)

    # Exit non-zero if no scenario routed through transform — that's a
    # signal the planner guidance isn't taking and we shouldn't claim
    # a win.
    if results and not any(r.transform_was_used for r in results):
        logger.warning("no scenario routed through transform_previous_answer; planner guidance is not taking")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
