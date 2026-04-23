#!/usr/bin/env python3
"""Combined tests + performance snapshot.

Runs both signals we care about — unit-test pass/fail AND live
benchmark performance — in one invocation, writes a JSON snapshot,
appends to a history file, and prints a delta vs the last snapshot.

Usage:
    # Full: tests + benchmark against dev
    python scripts/ci_health.py --bench-url https://mobius-chat-ortabkknqa-uc.a.run.app

    # Tests only (skip benchmark — fast local check)
    python scripts/ci_health.py --no-bench

    # Override output locations
    python scripts/ci_health.py \\
        --snapshot /tmp/ci_today.json \\
        --history /tmp/ci_history.jsonl \\
        --bench-url https://...

Output contract (snapshot JSON):
    {
        "ts": "2026-04-23T13:45:02Z",
        "git_sha": "552b849...",
        "tests": {
            "passed": 1245, "failed": 0, "skipped": 1, "deselected": 10,
            "duration_s": 30.4
        },
        "bench": {
            "base_url": "...",
            "n_completed": 8, "n_failed": 1,
            "duration_ms_p50": 16700, "duration_ms_p95": 32300,
            "first_chunk_ms_p50": 55, "first_chunk_ms_p95": 75,
            "zero_sources_count": 0
        },
        "delta_vs_last": {...}  (populated when history has priors)
    }

History file is JSONL (one snapshot per line). Easy to diff, easy to
plot, easy to query with jq.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

CHAT_ROOT = Path(__file__).resolve().parent.parent


# ── Tests ────────────────────────────────────────────────────────────


def run_tests(venv_python: str) -> dict[str, Any]:
    """Run the non-integration suite, parse pytest's final summary."""
    t0 = time.perf_counter()
    proc = subprocess.run(
        [
            venv_python, "-m", "pytest", "tests/",
            "-m", "not integration and not requires_rag and not requires_skills",
            "--tb=no", "-q",
        ],
        cwd=str(CHAT_ROOT),
        capture_output=True,
        text=True,
        timeout=600,
    )
    dur = time.perf_counter() - t0

    # Parse the final summary line. Two shapes pytest may emit:
    #   "1245 passed, 1 skipped, 10 deselected, 9 warnings in 30.37s"
    #   "9 failed, 1236 passed, 1 skipped, 10 deselected ..."
    summary = {"passed": 0, "failed": 0, "skipped": 0, "deselected": 0,
               "errors": 0, "duration_s": round(dur, 1)}
    combined = (proc.stdout or "") + "\n" + (proc.stderr or "")
    for line in reversed(combined.strip().splitlines()):
        line = line.strip()
        # Look for the final summary line (contains "passed" or "failed"
        # AND "in Ns").
        if (" passed" in line or " failed" in line) and " in " in line:
            for key, pattern in (
                ("passed",     r"(\d+)\s+passed"),
                ("failed",     r"(\d+)\s+failed"),
                ("skipped",    r"(\d+)\s+skipped"),
                ("deselected", r"(\d+)\s+deselected"),
                ("errors",     r"(\d+)\s+error"),
            ):
                m = re.search(pattern, line)
                if m:
                    summary[key] = int(m.group(1))
            break
    summary["exit_code"] = proc.returncode
    return summary


# ── Benchmark ────────────────────────────────────────────────────────


def run_bench(venv_python: str, base_url: str, chat_mode: str,
              per_turn_timeout_s: int) -> dict[str, Any]:
    """Fire bench_chat_e2e.py against the deployed API and summarize."""
    out_path = f"/tmp/ci_health_bench_{int(time.time())}.json"
    t0 = time.perf_counter()
    proc = subprocess.run(
        [
            venv_python, "scripts/bench_chat_e2e.py",
            "--base-url", base_url,
            "--chat-mode", chat_mode,
            "--per-turn-timeout-s", str(per_turn_timeout_s),
            "--pause-s", "3",
            "--tag", f"ci_health_{int(time.time())}",
            "--out", out_path,
        ],
        cwd=str(CHAT_ROOT),
        capture_output=True,
        text=True,
        timeout=2400,
    )
    dur = time.perf_counter() - t0
    result: dict[str, Any] = {
        "base_url": base_url,
        "chat_mode": chat_mode,
        "wallclock_s": round(dur, 1),
        "exit_code": proc.returncode,
    }
    try:
        with open(out_path) as f:
            report = json.load(f)
        s = report.get("summary") or {}
        result.update({
            "n_total":           s.get("n_total"),
            "n_completed":       s.get("n_completed"),
            "n_failed":          s.get("n_failed"),
            "duration_ms_p50":   s.get("duration_ms_p50"),
            "duration_ms_p95":   s.get("duration_ms_p95"),
            "first_chunk_ms_p50": s.get("first_chunk_ms_p50"),
            "first_chunk_ms_p95": s.get("first_chunk_ms_p95"),
            "rounds_used_avg":   s.get("rounds_used_avg"),
            "zero_sources_count": s.get("zero_sources_count"),
            "retrieval_signals": s.get("retrieval_signal_counts"),
            "snapshot_path":     out_path,
        })
    except Exception as exc:
        result["bench_parse_error"] = str(exc)
    return result


# ── Git context ──────────────────────────────────────────────────────


def git_sha() -> str:
    try:
        out = subprocess.check_output(
            ["git", "-C", str(CHAT_ROOT), "rev-parse", "--short=10", "HEAD"],
            stderr=subprocess.DEVNULL, timeout=5,
        ).decode().strip()
        return out
    except Exception:
        return "(nogit)"


def git_branch() -> str:
    try:
        return subprocess.check_output(
            ["git", "-C", str(CHAT_ROOT), "branch", "--show-current"],
            stderr=subprocess.DEVNULL, timeout=5,
        ).decode().strip()
    except Exception:
        return "(unknown)"


# ── Delta computation ────────────────────────────────────────────────


def load_last_snapshot(history_path: str) -> dict[str, Any] | None:
    if not os.path.exists(history_path):
        return None
    try:
        with open(history_path) as f:
            lines = [ln for ln in f.readlines() if ln.strip()]
        if not lines:
            return None
        return json.loads(lines[-1])
    except Exception:
        return None


def compute_delta(current: dict[str, Any], previous: dict[str, Any]) -> dict[str, Any]:
    """Simple flat delta on a handful of tracked metrics. ``None`` means
    'couldn't compute' rather than 'zero change'."""
    out: dict[str, Any] = {
        "previous_sha": previous.get("git_sha"),
        "previous_ts": previous.get("ts"),
    }

    def diff(a, b):
        try:
            return a - b
        except TypeError:
            return None

    c_t = current.get("tests") or {}
    p_t = previous.get("tests") or {}
    out["tests"] = {
        "passed_delta":  diff(c_t.get("passed"),  p_t.get("passed")),
        "failed_delta":  diff(c_t.get("failed"),  p_t.get("failed")),
        "skipped_delta": diff(c_t.get("skipped"), p_t.get("skipped")),
    }

    c_b = current.get("bench") or {}
    p_b = previous.get("bench") or {}
    if c_b and p_b:
        out["bench"] = {
            "n_completed_delta":          diff(c_b.get("n_completed"), p_b.get("n_completed")),
            "duration_ms_p50_delta":      diff(c_b.get("duration_ms_p50"), p_b.get("duration_ms_p50")),
            "duration_ms_p95_delta":      diff(c_b.get("duration_ms_p95"), p_b.get("duration_ms_p95")),
            "first_chunk_ms_p50_delta":   diff(c_b.get("first_chunk_ms_p50"), p_b.get("first_chunk_ms_p50")),
            "zero_sources_count_delta":   diff(c_b.get("zero_sources_count"), p_b.get("zero_sources_count")),
        }
    return out


# ── Rendering ────────────────────────────────────────────────────────


def _fmt_delta(v: Any, *, lower_is_better: bool = False, unit: str = "") -> str:
    if v is None:
        return "—"
    if v == 0:
        return f" 0{unit}"
    sign = "+" if v > 0 else ""
    # Direction arrow: up vs down
    arrow = ""
    if lower_is_better:
        arrow = " ↓" if v < 0 else " ↑"
    else:
        arrow = " ↑" if v > 0 else " ↓"
    return f"{sign}{v}{unit}{arrow}"


def print_summary(snapshot: dict[str, Any]) -> None:
    t = snapshot.get("tests") or {}
    b = snapshot.get("bench") or {}
    d = snapshot.get("delta_vs_last") or {}
    dt = d.get("tests") or {}
    db = d.get("bench") or {}

    print()
    print("══════════════════════════════════════════════════════════════")
    print(f"  CI HEALTH SNAPSHOT")
    print(f"  branch:  {snapshot.get('git_branch')}")
    print(f"  sha:     {snapshot.get('git_sha')}")
    print(f"  ts:      {snapshot.get('ts')}")
    if d.get("previous_sha"):
        print(f"  vs:      {d['previous_sha']} ({d['previous_ts']})")
    print("══════════════════════════════════════════════════════════════")
    print()
    print("  TESTS")
    print(f"    passed    {t.get('passed', 0):>6}   {_fmt_delta(dt.get('passed_delta'))}")
    print(f"    failed    {t.get('failed', 0):>6}   {_fmt_delta(dt.get('failed_delta'), lower_is_better=True)}")
    print(f"    skipped   {t.get('skipped', 0):>6}")
    print(f"    wall      {t.get('duration_s', 0):>6}s")
    print(f"    exit_code {t.get('exit_code', 0):>6}")
    print()
    if b:
        print("  BENCH (9-question suite, copilot mode)")
        print(f"    completed  {b.get('n_completed', 0)}/{b.get('n_total', 0)}  {_fmt_delta(db.get('n_completed_delta'))}")
        if b.get('duration_ms_p50') is not None:
            print(f"    p50 dur    {b['duration_ms_p50']:>5}ms  {_fmt_delta(db.get('duration_ms_p50_delta'), lower_is_better=True, unit='ms')}")
        if b.get('duration_ms_p95') is not None:
            print(f"    p95 dur    {b['duration_ms_p95']:>5}ms  {_fmt_delta(db.get('duration_ms_p95_delta'), lower_is_better=True, unit='ms')}")
        if b.get('first_chunk_ms_p50') is not None:
            print(f"    p50 fcms   {b['first_chunk_ms_p50']:>5}ms  {_fmt_delta(db.get('first_chunk_ms_p50_delta'), lower_is_better=True, unit='ms')}")
        print(f"    zero_src   {b.get('zero_sources_count', 0):>5}     {_fmt_delta(db.get('zero_sources_count_delta'), lower_is_better=True)}")
        rounds = b.get("rounds_used_avg")
        if rounds is not None:
            print(f"    rounds_avg {rounds:>5.2f}")
    else:
        print("  BENCH (skipped)")
    print()
    print("══════════════════════════════════════════════════════════════")


# ── Main ──────────────────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--bench-url", default=None,
                        help="Chat API base URL to benchmark. Omit (or pass --no-bench) to skip.")
    parser.add_argument("--no-bench", action="store_true",
                        help="Skip the live benchmark — tests only. Fast local check.")
    parser.add_argument("--chat-mode", default="copilot",
                        choices=["copilot", "agentic", "quick"])
    parser.add_argument("--per-turn-timeout-s", type=int, default=180)
    parser.add_argument("--venv-python",
                        default=str(CHAT_ROOT / ".venv" / "bin" / "python"),
                        help="Python interpreter to run pytest+bench with")
    parser.add_argument("--snapshot", default=None,
                        help="Where to write this run's JSON snapshot (default: /tmp/ci_health_<sha>.json)")
    parser.add_argument("--history", default="/tmp/ci_health_history.jsonl",
                        help="Append-only history file (JSONL)")
    args = parser.parse_args()

    do_bench = bool(args.bench_url) and not args.no_bench

    sha = git_sha()
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    # ── Tests first (fast, offline) ──────────────────────────────
    print(f"Running test suite…")
    tests = run_tests(args.venv_python)
    print(f"  → passed={tests.get('passed')} failed={tests.get('failed')} "
          f"skipped={tests.get('skipped')} ({tests.get('duration_s')}s)")

    # ── Bench (optional, slow, network) ──────────────────────────
    bench: dict[str, Any] = {}
    if do_bench:
        print(f"Running bench against {args.bench_url}…")
        bench = run_bench(
            args.venv_python, args.bench_url,
            args.chat_mode, args.per_turn_timeout_s,
        )
        print(f"  → completed={bench.get('n_completed')}/{bench.get('n_total')} "
              f"p50={bench.get('duration_ms_p50')}ms "
              f"p95={bench.get('duration_ms_p95')}ms")
    else:
        print("Skipping bench.")

    # ── Snapshot + delta ─────────────────────────────────────────
    snapshot: dict[str, Any] = {
        "ts": ts,
        "git_sha": sha,
        "git_branch": git_branch(),
        "tests": tests,
        "bench": bench,
    }
    last = load_last_snapshot(args.history)
    if last:
        snapshot["delta_vs_last"] = compute_delta(snapshot, last)

    # Persist
    snapshot_path = args.snapshot or f"/tmp/ci_health_{sha}.json"
    Path(snapshot_path).write_text(json.dumps(snapshot, indent=2))
    with open(args.history, "a") as f:
        f.write(json.dumps(snapshot) + "\n")

    print_summary(snapshot)
    print(f"Snapshot → {snapshot_path}")
    print(f"History  → {args.history}")

    # Exit non-zero if tests failed OR bench had failures (configurable
    # later if we want warn-only bench).
    bad_tests = (tests.get("failed", 0) or 0) + (tests.get("errors", 0) or 0)
    bad_bench = bench.get("n_failed", 0) or 0 if do_bench else 0
    return 0 if (bad_tests == 0 and bad_bench == 0) else 1


if __name__ == "__main__":
    sys.exit(main())
