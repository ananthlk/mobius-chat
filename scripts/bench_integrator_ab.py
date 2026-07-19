#!/usr/bin/env python3
"""Integrator A/B latency benchmark — sequential vs parallel.

Flips MOBIUS_INTEGRATOR_MODE on the Cloud Run service between runs so
both modes hit the same deployment. Measures client-side wall-clock and
extracts per-stage LLM timings from the turn_completed SSE envelope.

Usage (dev):
    python mobius-chat/scripts/bench_integrator_ab.py \\
        --base-url https://mobius-chat-ortabkknqa-uc.a.run.app \\
        --project mobius-os-dev --region us-central1 --service mobius-chat \\
        --n 8 --mint-dev-token

Usage (skip gcloud flipping — e.g. already forced via env or you want
to run one mode only):
    python mobius-chat/scripts/bench_integrator_ab.py \\
        --base-url https://mobius-chat-ortabkknqa-uc.a.run.app \\
        --n 8 --mint-dev-token --no-flip

The script restores the original MOBIUS_INTEGRATOR_MODE value (or clears
it if unset) when done, even on error.

Output: JSON report to --out (default stdout), plus a console summary table.
"""
from __future__ import annotations

import argparse
import json
import logging
import statistics
import subprocess
import sys
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

DEV_URL = "https://mobius-chat-ortabkknqa-uc.a.run.app"

QUESTIONS = [
    {"id": "ab_001", "q": "What is the prior authorization timeline for Sunshine Health?"},
    {"id": "ab_002", "q": "How do I file an appeal for a denied claim?"},
    {"id": "ab_003", "q": "What is the timely filing limit for Medicaid claims in Florida?"},
    {"id": "ab_004", "q": "What services require prior authorization under Florida Medicaid?"},
    {"id": "ab_005", "q": "What is the credentialing process for a new mental-health provider?"},
    {"id": "ab_006", "q": "What does Sunshine Health cover for behavioral health services?"},
    {"id": "ab_007", "q": "How does claim submission work for telehealth services in Florida?"},
    {"id": "ab_008", "q": "What is the NPI requirement for billing mental-health services?"},
]


@dataclass
class TurnResult:
    qid: str
    question: str
    mode: str          # "sequential" | "parallel" | "unknown"
    wall_ms: float
    first_chunk_ms: float | None
    status: str        # "ok" | "error" | "timeout"
    total_tokens: int | None = None
    llm_calls: list[dict] = field(default_factory=list)
    error: str | None = None


# ── Cloud Run env-var flip ────────────────────────────────────────────────────

def _cr_get_env_var(project: str, region: str, service: str, var: str) -> str | None:
    """Return current value of a Cloud Run env var (None if unset)."""
    try:
        out = subprocess.check_output(
            [
                "gcloud", "run", "services", "describe", service,
                "--project", project, "--region", region,
                "--format", "json",
            ],
            stderr=subprocess.DEVNULL,
            timeout=30,
        )
        svc = json.loads(out)
        envs = (
            svc.get("spec", {})
               .get("template", {})
               .get("spec", {})
               .get("containers", [{}])[0]
               .get("env", [])
        )
        for e in envs:
            if e.get("name") == var:
                return e.get("value")
        return None
    except Exception as exc:
        logger.warning("cr_get_env_var failed: %s", exc)
        return None


def _cr_set_env_var(project: str, region: str, service: str, var: str, value: str | None) -> None:
    """Set (or clear) a Cloud Run env var and wait for revision to go live."""
    if value is None:
        args = ["--remove-env-vars", var]
        label = f"clear {var}"
    else:
        args = ["--update-env-vars", f"{var}={value}"]
        label = f"{var}={value}"

    logger.info("Cloud Run: %s on %s …", label, service)
    subprocess.check_call(
        [
            "gcloud", "run", "services", "update", service,
            "--project", project, "--region", region,
        ] + args,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=120,
    )
    # Brief settle — new revision needs a moment to take traffic
    time.sleep(8)
    logger.info("  done")


# ── Chat API helpers ──────────────────────────────────────────────────────────

def _mint_token(client: httpx.Client, base_url: str) -> str | None:
    try:
        r = client.post(f"{base_url}/chat/admin/mint-dev-token", timeout=10)
        r.raise_for_status()
        return r.json().get("access_token")
    except Exception as exc:
        logger.warning("mint-dev-token failed: %s", exc)
        return None


def _post_chat(
    client: httpx.Client,
    base_url: str,
    question: str,
    bearer: str | None,
) -> tuple[str, str]:
    """POST /chat, return (correlation_id, thread_id)."""
    body: dict[str, Any] = {
        "message": question,
        "thread_id": str(uuid.uuid4()),
    }
    headers = {"Content-Type": "application/json"}
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    r = client.post(f"{base_url}/chat", json=body, headers=headers, timeout=20)
    r.raise_for_status()
    data = r.json()
    return data["correlation_id"], data.get("thread_id") or ""


def _stream_turn(
    client: httpx.Client,
    base_url: str,
    correlation_id: str,
    bearer: str | None,
    t0: float,
    timeout_s: float = 90,
) -> tuple[float | None, str, int | None, list[dict]]:
    """Stream SSE; return (first_chunk_ms, mode_detected, total_tokens, llm_calls)."""
    url = f"{base_url}/chat/stream/{correlation_id}"
    headers = {}
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"

    first_chunk_ms: float | None = None
    mode_detected = "unknown"
    total_tokens: int | None = None
    llm_calls: list[dict] = []

    try:
        with client.stream("GET", url, headers=headers, timeout=timeout_s) as resp:
            resp.raise_for_status()
            for raw in resp.iter_lines():
                if not raw or not raw.startswith("data:"):
                    continue
                if first_chunk_ms is None:
                    first_chunk_ms = (time.perf_counter() - t0) * 1000

                payload_str = raw[5:].strip()
                if not payload_str or payload_str == "[DONE]":
                    continue
                try:
                    event = json.loads(payload_str)
                except json.JSONDecodeError:
                    continue

                # turn_completed is emitted as an entry inside thinking_log,
                # not as a top-level SSE event.
                entries_to_check: list[dict] = []
                if event.get("signal") == "turn_completed":
                    entries_to_check = [event]
                elif "thinking_log" in event:
                    log = event.get("thinking_log") or []
                    entries_to_check = [e for e in log if isinstance(e, dict)]

                for entry in entries_to_check:
                    if entry.get("signal") != "turn_completed":
                        continue
                    data = entry.get("data") or {}
                    if data.get("total_llm_tokens") is not None:
                        total_tokens = int(data["total_llm_tokens"])
                    raw_mode = data.get("integrator_mode")
                    if raw_mode:
                        mode_detected = {"S": "sequential", "P": "parallel"}.get(raw_mode, raw_mode)
                    llm_calls = data.get("llm_calls") or []
    except Exception as exc:
        logger.debug("stream error for %s: %s", correlation_id[:8], exc)

    return first_chunk_ms, mode_detected, total_tokens, llm_calls


def _run_turn(
    client: httpx.Client,
    base_url: str,
    qid: str,
    question: str,
    bearer: str | None,
) -> TurnResult:
    t0 = time.perf_counter()
    try:
        cid, _tid = _post_chat(client, base_url, question, bearer)
    except Exception as exc:
        wall_ms = (time.perf_counter() - t0) * 1000
        return TurnResult(qid=qid, question=question, mode="unknown",
                          wall_ms=wall_ms, first_chunk_ms=None,
                          status="error", error=str(exc))

    first_ms, mode, tokens, calls = _stream_turn(
        client, base_url, cid, bearer, t0
    )
    wall_ms = (time.perf_counter() - t0) * 1000
    return TurnResult(
        qid=qid, question=question, mode=mode,
        wall_ms=wall_ms, first_chunk_ms=first_ms,
        status="ok", total_tokens=tokens, llm_calls=calls,
    )


# ── Stats ─────────────────────────────────────────────────────────────────────

def _stats(values: list[float]) -> dict:
    if not values:
        return {}
    s = sorted(values)
    n = len(s)
    return {
        "n": n,
        "mean_ms": round(statistics.mean(s), 1),
        "median_ms": round(statistics.median(s), 1),
        "p75_ms": round(s[int(n * 0.75)], 1),
        "p95_ms": round(s[min(int(n * 0.95), n - 1)], 1),
        "min_ms": round(s[0], 1),
        "max_ms": round(s[-1], 1),
    }


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Integrator A/B latency benchmark")
    parser.add_argument("--base-url", default=DEV_URL)
    parser.add_argument("--project", default="mobius-os-dev")
    parser.add_argument("--region", default="us-central1")
    parser.add_argument("--service", default="mobius-chat")
    parser.add_argument("--n", type=int, default=8,
                        help="Questions per mode (repeats question list if n > len)")
    parser.add_argument("--mint-dev-token", action="store_true")
    parser.add_argument("--bearer", default=None)
    parser.add_argument("--out", default=None, help="Write JSON report here")
    parser.add_argument("--no-flip", action="store_true",
                        help="Skip Cloud Run env-var flipping (already forced externally)")
    parser.add_argument("--mode-order", default="sequential,parallel",
                        help="Comma-separated mode order")
    args = parser.parse_args()

    modes = [m.strip() for m in args.mode_order.split(",")]
    questions = (QUESTIONS * ((args.n // len(QUESTIONS)) + 1))[: args.n]

    with httpx.Client(timeout=120) as client:
        bearer = args.bearer
        if not bearer and args.mint_dev_token:
            bearer = _mint_token(client, args.base_url)
            if bearer:
                logger.info("dev token minted")
            else:
                logger.warning("no auth token — continuing unauthenticated")

        # Save original env-var value so we can restore it
        original_mode: str | None = None
        if not args.no_flip:
            original_mode = _cr_get_env_var(
                args.project, args.region, args.service, "MOBIUS_INTEGRATOR_MODE"
            )

        results_by_mode: dict[str, list[TurnResult]] = {}

        try:
            for mode in modes:
                if not args.no_flip:
                    _cr_set_env_var(
                        args.project, args.region, args.service,
                        "MOBIUS_INTEGRATOR_MODE", mode,
                    )

                logger.info("── Running %d turns in %s mode ──", args.n, mode)
                turns: list[TurnResult] = []
                for q in questions:
                    logger.info("  [%s] %s …", q["id"], q["q"][:60])
                    t = _run_turn(client, args.base_url, q["id"], q["q"], bearer)
                    turns.append(t)
                    logger.info(
                        "    wall=%.0fms first_chunk=%.0fms mode=%s tokens=%s status=%s",
                        t.wall_ms,
                        t.first_chunk_ms or -1,
                        t.mode,
                        t.total_tokens or "?",
                        t.status,
                    )
                results_by_mode[mode] = turns

        finally:
            # Restore original value regardless of errors
            if not args.no_flip:
                logger.info("Restoring MOBIUS_INTEGRATOR_MODE → %s", original_mode)
                _cr_set_env_var(
                    args.project, args.region, args.service,
                    "MOBIUS_INTEGRATOR_MODE", original_mode,
                )

    # ── Build report ──────────────────────────────────────────────────────────
    report: dict[str, Any] = {"modes": {}}
    for mode, turns in results_by_mode.items():
        ok = [t for t in turns if t.status == "ok"]
        walls = [t.wall_ms for t in ok]
        first_chunks = [t.first_chunk_ms for t in ok if t.first_chunk_ms is not None]
        tokens = [t.total_tokens for t in ok if t.total_tokens is not None]
        # Verify mode was actually honoured by the server
        confirmed = [t for t in ok if t.mode == mode[0].upper()]

        report["modes"][mode] = {
            "turns_total": len(turns),
            "turns_ok": len(ok),
            "mode_confirmed_by_server": len(confirmed),
            "wall_clock": _stats(walls),
            "first_chunk": _stats(first_chunks),
            "avg_tokens": round(sum(tokens) / len(tokens), 1) if tokens else None,
            "turns": [asdict(t) for t in turns],
        }

    # Delta summary (sequential → parallel)
    if "sequential" in report["modes"] and "parallel" in report["modes"]:
        seq_mean = report["modes"]["sequential"]["wall_clock"].get("mean_ms")
        par_mean = report["modes"]["parallel"]["wall_clock"].get("mean_ms")
        if seq_mean and par_mean:
            report["delta"] = {
                "wall_mean_ms": round(par_mean - seq_mean, 1),
                "wall_pct_change": round((par_mean - seq_mean) / seq_mean * 100, 1),
                "interpretation": "negative = parallel is faster",
            }

    # ── Console summary ───────────────────────────────────────────────────────
    print("\n══ Integrator A/B latency summary ══\n")
    for mode, data in report["modes"].items():
        wc = data["wall_clock"]
        fc = data["first_chunk"]
        print(f"  {mode.upper()} (n={data['turns_ok']}, server-confirmed={data['mode_confirmed_by_server']})")
        print(f"    wall:        mean={wc.get('mean_ms','?'):.0f}ms  p50={wc.get('median_ms','?'):.0f}ms  p95={wc.get('p95_ms','?'):.0f}ms")
        if fc:
            print(f"    first-chunk: mean={fc.get('mean_ms','?'):.0f}ms  p50={fc.get('median_ms','?'):.0f}ms")
        print(f"    avg tokens:  {data['avg_tokens'] or '?'}")
        print()

    if "delta" in report:
        d = report["delta"]
        sign = "+" if d["wall_mean_ms"] >= 0 else ""
        print(f"  Δ wall-clock mean (parallel - sequential): {sign}{d['wall_mean_ms']}ms ({sign}{d['wall_pct_change']}%)")
        print(f"  ({d['interpretation']})")
    print()

    if args.out:
        Path(args.out).write_text(json.dumps(report, indent=2))
        print(f"Report written to {args.out}")
    else:
        print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
