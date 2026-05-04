#!/usr/bin/env python3
"""Concurrent stress test for chat — validates cold-start and consistency fixes.

Goal: prove that after the eager-import + Vertex warmup fixes, p95 turn
latency stays bounded even when traffic spikes force the autoscaler to
spawn fresh instances.

Strategy:

  Round 1 (warm-up only the warm pool): N concurrent turns, expect
  all to land on the 4 warm instances. Establishes baseline p50/p95.

  Round 2 (force autoscale): 2*N concurrent turns. Forces autoscaler
  to spawn new instances. Pre-fix this saw 60-300s gaps from cold-import
  inside the user budget. Post-fix should match Round 1 closely.

  Round 3 (sustained): N concurrent every 30s for K rounds. Tests
  that no slow drift creeps in — Redis connection pool, conn churn,
  worker queue depth.

Per-turn metrics captured:
  - cid, question_id
  - elapsed_s_total      = post → final response
  - elapsed_s_first_log  = post → first thinking line (instance ready signal)
  - status               = completed | failed | timeout | error
  - rounds_used          = ReAct rounds
  - error                = if any

Reports:
  - per-round p50/p90/p95/p99 + error rate
  - cold-instance signal: turns whose elapsed_s_first_log > 5s
    (indicates instance was still spinning up)
  - "stuck cid" detection: turns whose stall-bailout triggered
    (>90s since last progress) — should be 0 after FE fix is deployed

Usage:
  python mobius-chat/scripts/stress_chat_concurrent.py \\
      --base-url https://mobius-chat-ortabkknqa-uc.a.run.app \\
      --token "$(./scripts/mint-dev-token.sh)" \\
      --concurrency 10 \\
      --out /tmp/stress_$(date +%s).json
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, asdict
from typing import Any

try:
    import httpx
except ImportError:
    print("Need httpx. pip install httpx", file=sys.stderr)
    sys.exit(1)


# Diverse question set covering the main pipeline paths so we're not
# all hitting the same cache/dedup path (we have CACHE_ASSIST_ENABLED=0
# but better to be safe).
QUESTIONS: list[str] = [
    "What is the timely filing limit for Sunshine Health?",
    "What documentation is required for a Florida Medicaid behavioral health prior auth?",
    "How do I appeal a denied claim with United Healthcare?",
    "What are the credentialing requirements for a CMHC in Florida?",
    "What modifiers apply to H0001 for Medicaid billing?",
    "Explain the prior auth process for outpatient mental health services.",
    "What is the daily rate for partial hospitalization (H0035) in Florida Medicaid?",
    "How long does Molina take to process a credentialing application?",
    "Are telehealth services covered under Florida Medicaid managed care?",
    "What are the documentation requirements for residential SUD treatment?",
]

STALL_THRESHOLD_S = 90.0
MAX_TOTAL_S = 320.0


@dataclass
class TurnResult:
    round_id: int
    question_id: int
    question: str
    cid: str
    started_at: float = 0.0
    elapsed_s_total: float = 0.0
    elapsed_s_first_log: float | None = None
    elapsed_s_first_message: float | None = None
    status: str = "pending"
    rounds_used: int | None = None
    error: str | None = None
    thinking_lines: int = 0


def _post_chat(
    client: httpx.Client,
    base_url: str,
    token: str | None,
    question: str,
    thread_id: str,
) -> str:
    """Returns correlation_id of accepted turn."""
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    payload = {"message": question, "thread_id": thread_id}
    r = client.post(f"{base_url}/chat", json=payload, headers=headers, timeout=15.0)
    r.raise_for_status()
    data = r.json()
    return data.get("correlation_id") or ""


def _poll_until_done(
    client: httpx.Client,
    base_url: str,
    token: str | None,
    cid: str,
    started_at: float,
    result: TurnResult,
) -> None:
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    last_progress_t = time.time()
    last_thinking_count = 0
    last_msg_len = 0
    last_status = ""
    while True:
        try:
            r = client.get(
                f"{base_url}/chat/response/{cid}",
                headers=headers,
                timeout=10.0,
            )
            data = r.json()
        except Exception as e:
            result.status = "error"
            result.error = f"poll_error: {type(e).__name__}: {e}"
            result.elapsed_s_total = time.time() - started_at
            return
        progressed = False
        thinking = data.get("thinking_log") or []
        if len(thinking) > last_thinking_count:
            if result.elapsed_s_first_log is None:
                result.elapsed_s_first_log = time.time() - started_at
            last_thinking_count = len(thinking)
            result.thinking_lines = last_thinking_count
            progressed = True
        msg = data.get("message") or ""
        if len(msg) > last_msg_len:
            if result.elapsed_s_first_message is None:
                result.elapsed_s_first_message = time.time() - started_at
            last_msg_len = len(msg)
            progressed = True
        st = data.get("status") or ""
        if st != last_status:
            last_status = st
            progressed = True
        if progressed:
            last_progress_t = time.time()
        if st in ("completed", "clarification", "refinement_ask", "failed"):
            result.status = st
            result.elapsed_s_total = time.time() - started_at
            usage = (data.get("usage_breakdown") or [])
            result.rounds_used = len(usage) if isinstance(usage, list) else None
            return
        now = time.time()
        if now - last_progress_t > STALL_THRESHOLD_S:
            result.status = "stalled"
            result.error = f"no progress for {STALL_THRESHOLD_S:.0f}s (lost-job signal)"
            result.elapsed_s_total = now - started_at
            return
        if now - started_at > MAX_TOTAL_S:
            result.status = "timeout"
            result.error = f"total>{MAX_TOTAL_S:.0f}s"
            result.elapsed_s_total = now - started_at
            return
        time.sleep(0.4)


def run_one(
    base_url: str,
    token: str | None,
    round_id: int,
    question_id: int,
    question: str,
) -> TurnResult:
    res = TurnResult(
        round_id=round_id,
        question_id=question_id,
        question=question,
        cid="",
    )
    # thread_id MUST be a UUID — chat's ensure_thread() casts to pg uuid.
    # Round/question metadata captured on TurnResult, not in the id.
    thread_id = str(uuid.uuid4())
    started_at = time.time()
    res.started_at = started_at
    with httpx.Client() as client:
        try:
            cid = _post_chat(client, base_url, token, question, thread_id)
            res.cid = cid
            if not cid:
                res.status = "error"
                res.error = "no cid in /chat response"
                res.elapsed_s_total = time.time() - started_at
                return res
        except Exception as e:
            res.status = "error"
            res.error = f"post_error: {type(e).__name__}: {e}"
            res.elapsed_s_total = time.time() - started_at
            return res
        _poll_until_done(client, base_url, token, cid, started_at, res)
    return res


def _pcts(values: list[float]) -> dict[str, float]:
    if not values:
        return {"n": 0}
    s = sorted(values)
    n = len(s)
    def q(p: float) -> float:
        i = max(0, min(n - 1, int(p * (n - 1))))
        return s[i]
    return {
        "n": n,
        "min": s[0],
        "p50": q(0.5),
        "p90": q(0.9),
        "p95": q(0.95),
        "p99": q(0.99),
        "max": s[-1],
        "mean": statistics.fmean(s),
    }


def run_round(
    base_url: str,
    token: str | None,
    round_id: int,
    concurrency: int,
) -> list[TurnResult]:
    print(f"\n=== Round {round_id} — {concurrency} concurrent turns ===")
    t0 = time.time()
    results: list[TurnResult] = []
    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        futures = []
        for i in range(concurrency):
            q = QUESTIONS[i % len(QUESTIONS)]
            futures.append(ex.submit(run_one, base_url, token, round_id, i, q))
        for f in as_completed(futures):
            r = f.result()
            results.append(r)
            print(
                f"  cid={r.cid[:8] or 'n/a':<8}  status={r.status:<11}  "
                f"total={r.elapsed_s_total:6.1f}s  first_log="
                f"{r.elapsed_s_first_log if r.elapsed_s_first_log is not None else float('nan'):5.1f}s"
                f"{('  ' + r.error) if r.error else ''}"
            )
    elapsed = time.time() - t0
    completed = [r for r in results if r.status == "completed"]
    print(f"  round wall={elapsed:.1f}s  completed={len(completed)}/{len(results)}")
    return results


def summarize(round_id: int, results: list[TurnResult]) -> dict[str, Any]:
    completed = [r for r in results if r.status == "completed"]
    statuses: dict[str, int] = {}
    for r in results:
        statuses[r.status] = statuses.get(r.status, 0) + 1
    totals = [r.elapsed_s_total for r in completed]
    first_logs = [r.elapsed_s_first_log for r in completed if r.elapsed_s_first_log is not None]
    cold_instance_count = sum(1 for x in first_logs if x > 5.0)
    return {
        "round_id": round_id,
        "n": len(results),
        "statuses": statuses,
        "elapsed_s_total": _pcts(totals),
        "elapsed_s_first_log": _pcts(first_logs),
        "cold_instance_count": cold_instance_count,
        "cold_instance_pct": (cold_instance_count / len(results) * 100.0) if results else 0.0,
        "stalled_count": statuses.get("stalled", 0),
        "error_count": statuses.get("error", 0) + statuses.get("timeout", 0),
    }


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--base-url", required=True)
    p.add_argument("--token", default=None)
    p.add_argument("--concurrency", type=int, default=10)
    p.add_argument("--rounds", type=int, default=3)
    p.add_argument("--cooldown-s", type=float, default=15.0)
    p.add_argument("--out", default=None)
    args = p.parse_args()

    base_url = args.base_url.rstrip("/")
    print(f"stress: target={base_url}  concurrency={args.concurrency}  rounds={args.rounds}")
    print(f"        rule of thumb: cold-instance turns have first_log >5s; warm <2s")

    all_results: list[TurnResult] = []
    summaries: list[dict[str, Any]] = []
    for round_id in range(1, args.rounds + 1):
        # Round 2 doubles concurrency to force autoscale.
        c = args.concurrency * 2 if round_id == 2 else args.concurrency
        rs = run_round(base_url, args.token, round_id, c)
        all_results.extend(rs)
        summaries.append(summarize(round_id, rs))
        if round_id < args.rounds:
            print(f"  cooldown {args.cooldown_s:.0f}s ...")
            time.sleep(args.cooldown_s)

    print("\n=== SUMMARY ===")
    for s in summaries:
        et = s["elapsed_s_total"]
        fl = s["elapsed_s_first_log"]
        print(
            f"  round {s['round_id']}: n={s['n']}  "
            f"total p50={et.get('p50', 0):.1f}s p95={et.get('p95', 0):.1f}s "
            f"max={et.get('max', 0):.1f}s | "
            f"first_log p50={fl.get('p50', 0):.2f}s p95={fl.get('p95', 0):.2f}s | "
            f"cold={s['cold_instance_count']}/{s['n']} "
            f"stalled={s['stalled_count']} errors={s['error_count']}"
        )

    if args.out:
        out = {
            "target": base_url,
            "concurrency": args.concurrency,
            "rounds": args.rounds,
            "summaries": summaries,
            "turns": [asdict(r) for r in all_results],
        }
        with open(args.out, "w") as f:
            json.dump(out, f, indent=2)
        print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
