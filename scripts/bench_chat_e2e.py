#!/usr/bin/env python3
"""End-to-end chat benchmark runner.

Hits a deployed chat API with a question set, captures per-turn metrics,
writes a structured JSON report. Intended for baselining RAG changes:
run before a RAG swap, run after, diff the JSON files.

Usage (against dev Cloud Run):

    python mobius-chat/scripts/bench_chat_e2e.py \\
        --base-url https://mobius-chat-ortabkknqa-uc.a.run.app \\
        --out /tmp/rag_new.json

Usage (against local):

    python mobius-chat/scripts/bench_chat_e2e.py \\
        --base-url http://localhost:8000 \\
        --out /tmp/rag_local.json

Compare two runs:

    diff <(jq -S '.questions[] | {id, signals: .retrieval_signals, src: .sources_count, ms: .duration_ms}' baseline.json) \\
         <(jq -S '.questions[] | {id, signals: .retrieval_signals, src: .sources_count, ms: .duration_ms}' after.json)

Per-turn fields captured:
  - question, question_id
  - correlation_id
  - status (completed | failed | clarification | timeout | error)
  - duration_ms (POST → final response)
  - first_chunk_ms (POST → first SSE byte; tests G + D latency fixes)
  - final_message (truncated to 400 chars)
  - retrieval_signals
  - sources_count + sources_sample (first 3 document_names)
  - rounds_used (from turn_completed envelope)
  - tools_used
  - total_llm_tokens, total_cost_usd
  - thinking_log_length
  - error (if any)

Streaming is preferred (SSE) so we capture first_chunk_ms — that measures
the perceived-latency fixes. Falls back to poll on SSE error.

Dependencies: httpx (already in requirements.txt). pyyaml if loading the
default question set from mobius-retriever; otherwise the built-in
fallback question set is used.
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

logger = logging.getLogger(__name__)


# ── Fallback question set (used when eval_questions_dev.yaml isn't reachable) ──
#
# Nine questions covering the core RAG retrieval paths — matches the shape
# of the repo's existing eval set. If you edit these, keep them grounded
# in what's actually in the corpus so `retrieval_signals=no_sources` is a
# signal, not an artifact.

FALLBACK_QUESTIONS: list[dict[str, str]] = [
    {"id": "e2e_001", "question": "What is the prior authorization timeline for Sunshine Health H0036?"},
    {"id": "e2e_002", "question": "How do I file an appeal for a denied claim with Sunshine Health?"},
    {"id": "e2e_003", "question": "What is the 72-hour emergency medication supply policy?"},
    {"id": "e2e_004", "question": "What is the timely filing limit for Medicaid claims in Florida?"},
    {"id": "e2e_005", "question": "What services require prior authorization under Florida Medicaid?"},
    {"id": "e2e_006", "question": "What is the credentialing process for a new mental-health provider?"},
    {"id": "e2e_007", "question": "What phone number do pharmacies call for Express Scripts help desk?"},
    {"id": "e2e_008", "question": "How can a provider determine whether a service requires prior authorization?"},
    {"id": "e2e_009", "question": "What are the covered services for outpatient behavioral health in Florida?"},
]


# ── Per-turn result shape ─────────────────────────────────────────────


@dataclass
class TurnResult:
    question_id: str
    question: str
    correlation_id: str = ""
    status: str = "pending"
    duration_ms: int = 0
    first_chunk_ms: int | None = None
    final_message: str = ""
    retrieval_signals: list[str] = field(default_factory=list)
    sources_count: int = 0
    sources_sample: list[str] = field(default_factory=list)
    rounds_used: int | None = None
    tools_used: list[str] = field(default_factory=list)
    total_llm_tokens: int | None = None
    total_cost_usd: float | None = None
    thinking_log_length: int = 0
    answered_from_system_context: bool = False
    error: str | None = None


# ── Question loader ───────────────────────────────────────────────────


def _load_questions(path: str | None) -> list[dict[str, str]]:
    """Load questions from a YAML file if available, else fall back."""
    if path:
        p = Path(path)
    else:
        # Auto-discover the shared eval set from the sibling retriever repo.
        here = Path(__file__).resolve().parent.parent
        p = here.parent / "mobius-retriever" / "eval_questions_dev.yaml"
    if not p.exists():
        logger.info("Question file not found at %s; using built-in fallback set.", p)
        return FALLBACK_QUESTIONS
    try:
        import yaml
    except ImportError:
        logger.warning("PyYAML not installed; using built-in fallback set.")
        return FALLBACK_QUESTIONS
    try:
        data = yaml.safe_load(p.read_text())
    except Exception as exc:
        logger.warning("Failed to parse %s (%s); using fallback.", p, exc)
        return FALLBACK_QUESTIONS
    qs = (data or {}).get("questions") or []
    out: list[dict[str, str]] = []
    for q in qs:
        if not isinstance(q, dict):
            continue
        qid = str(q.get("id") or "").strip()
        text = str(q.get("question") or "").strip()
        if not text:
            continue
        out.append({"id": qid or f"q_{len(out)+1:03d}", "question": text})
    if not out:
        return FALLBACK_QUESTIONS
    return out


# ── Single-turn runner ────────────────────────────────────────────────


def _post_chat(
    client: httpx.Client,
    base_url: str,
    question: str,
    chat_mode: str,
    thread_id: str | None,
    system_context: str | None,
    cache_assist: bool | None = None,
) -> tuple[str, str]:
    """POST /chat, return (correlation_id, thread_id)."""
    body: dict[str, Any] = {"message": question, "chat_mode": chat_mode}
    if thread_id:
        body["thread_id"] = thread_id
    if system_context:
        body["system_context"] = system_context
    if cache_assist is not None:
        body["cache_assist"] = cache_assist
    r = client.post(f"{base_url}/chat", json=body, timeout=30)
    r.raise_for_status()
    data = r.json()
    return (data["correlation_id"], data.get("thread_id") or "")


def _stream_turn(
    client: httpx.Client,
    base_url: str,
    correlation_id: str,
    turn: TurnResult,
    *,
    timeout_s: int,
) -> dict | None:
    """Stream the SSE endpoint; return the completed payload or None on timeout/error.

    Sets turn.first_chunk_ms on the first byte (perception-latency metric).
    Falls back to None on SSE error — caller can poll as a second attempt.
    """
    url = f"{base_url}/chat/stream/{correlation_id}"
    t0 = time.perf_counter()
    completed: dict | None = None
    try:
        with client.stream("GET", url, timeout=timeout_s) as resp:
            if resp.status_code != 200:
                turn.error = f"stream HTTP {resp.status_code}"
                return None
            for raw in resp.iter_lines():
                if turn.first_chunk_ms is None:
                    turn.first_chunk_ms = int((time.perf_counter() - t0) * 1000)
                if not raw:
                    continue
                # SSE comments start with `:` — skip (also confirms our
                # D-fix ": stream-open" is landing, but we don't assert).
                if raw.startswith(":"):
                    continue
                if not raw.startswith("data:"):
                    continue
                payload_txt = raw[len("data:"):].strip()
                if not payload_txt:
                    continue
                try:
                    payload = json.loads(payload_txt)
                except json.JSONDecodeError:
                    continue
                ev = payload.get("event")
                if ev == "completed":
                    completed = payload.get("data") or {}
                    break
                if ev == "error":
                    turn.error = str((payload.get("data") or {}).get("message") or "stream error")
                    return None
    except httpx.ReadTimeout:
        turn.error = "stream read timeout"
        return None
    except Exception as exc:
        turn.error = f"stream exception: {exc}"
        return None
    return completed


def _poll_fallback(
    client: httpx.Client,
    base_url: str,
    correlation_id: str,
    turn: TurnResult,
    *,
    timeout_s: int,
    interval_s: float = 1.0,
) -> dict | None:
    """Poll GET /chat/response/:id until completed, failed, or timeout."""
    url = f"{base_url}/chat/response/{correlation_id}"
    deadline = time.perf_counter() + timeout_s
    while time.perf_counter() < deadline:
        try:
            r = client.get(url, timeout=15)
            if r.status_code != 200:
                turn.error = f"poll HTTP {r.status_code}"
                return None
            data = r.json()
            status = data.get("status")
            if status in ("completed", "failed", "clarification", "refinement_ask"):
                return data
        except Exception as exc:
            turn.error = f"poll exception: {exc}"
            return None
        time.sleep(interval_s)
    turn.error = "poll timeout"
    return None


def _extract_metrics_from_completed(turn: TurnResult, payload: dict) -> None:
    """Populate turn.* from the completed response payload."""
    turn.status = payload.get("status") or "completed"
    msg = payload.get("message") or payload.get("final_message") or ""
    turn.final_message = (msg or "")[:400]
    sig = payload.get("retrieval_signals") or []
    if isinstance(sig, list):
        turn.retrieval_signals = [str(s) for s in sig if s]
    src = payload.get("sources") or []
    if isinstance(src, list):
        turn.sources_count = len(src)
        turn.sources_sample = [
            str((s or {}).get("document_name") or "")
            for s in src[:3]
        ]
    tl = payload.get("thinking_log") or []
    turn.thinking_log_length = len(tl) if isinstance(tl, list) else 0
    if payload.get("answered_from_system_context") is True:
        turn.answered_from_system_context = True
    # turn_completed envelope is usually near the tail of thinking_log.
    for entry in reversed(tl or []):
        if isinstance(entry, dict) and entry.get("signal") == "turn_completed":
            data = entry.get("data") or {}
            if turn.rounds_used is None and data.get("rounds_used") is not None:
                turn.rounds_used = int(data["rounds_used"])
            if not turn.tools_used and data.get("tools_used"):
                turn.tools_used = [str(t) for t in (data["tools_used"] or []) if t]
            if turn.total_llm_tokens is None and data.get("total_llm_tokens") is not None:
                try:
                    turn.total_llm_tokens = int(data["total_llm_tokens"])
                except (TypeError, ValueError):
                    pass
            if turn.total_cost_usd is None and data.get("total_cost_usd") is not None:
                try:
                    turn.total_cost_usd = float(data["total_cost_usd"])
                except (TypeError, ValueError):
                    pass
            break


def run_one(
    client: httpx.Client,
    base_url: str,
    q: dict[str, str],
    *,
    chat_mode: str,
    per_turn_timeout_s: int,
    use_stream: bool,
    cache_assist: bool | None = None,
) -> TurnResult:
    turn = TurnResult(question_id=q["id"], question=q["question"])
    t0 = time.perf_counter()
    try:
        cid, _ = _post_chat(
            client, base_url, q["question"], chat_mode,
            thread_id=None, system_context=None,
            cache_assist=cache_assist,
        )
        turn.correlation_id = cid
    except Exception as exc:
        turn.status = "error"
        turn.error = f"POST /chat failed: {exc}"
        turn.duration_ms = int((time.perf_counter() - t0) * 1000)
        return turn

    completed: dict | None = None
    if use_stream:
        completed = _stream_turn(client, base_url, turn.correlation_id, turn, timeout_s=per_turn_timeout_s)
    if completed is None:
        # Either stream disabled or stream failed — fall back to polling.
        completed = _poll_fallback(client, base_url, turn.correlation_id, turn, timeout_s=per_turn_timeout_s)

    turn.duration_ms = int((time.perf_counter() - t0) * 1000)

    if completed is None:
        if not turn.status or turn.status == "pending":
            turn.status = "timeout"
        if not turn.error:
            turn.error = "no completed payload"
        return turn

    _extract_metrics_from_completed(turn, completed)
    return turn


# ── Summary / reporting ───────────────────────────────────────────────


def _summarize(turns: list[TurnResult]) -> dict[str, Any]:
    """Compute roll-up metrics for the run."""
    n = len(turns)
    completed = [t for t in turns if t.status == "completed"]
    failed = [t for t in turns if t.status in ("failed", "timeout", "error")]
    durs = sorted(t.duration_ms for t in completed)
    first_chunks = sorted(
        t.first_chunk_ms for t in completed if t.first_chunk_ms is not None
    )
    signal_counts: dict[str, int] = {}
    for t in completed:
        for s in t.retrieval_signals or ["(none)"]:
            signal_counts[s] = signal_counts.get(s, 0) + 1
    rounds = [t.rounds_used for t in completed if t.rounds_used is not None]
    tokens = [t.total_llm_tokens for t in completed if t.total_llm_tokens is not None]

    def _p(values, pct):
        if not values:
            return None
        k = max(0, min(len(values) - 1, int(len(values) * pct / 100)))
        return values[k]

    return {
        "n_total": n,
        "n_completed": len(completed),
        "n_failed": len(failed),
        "duration_ms_p50": _p(durs, 50),
        "duration_ms_p95": _p(durs, 95),
        "first_chunk_ms_p50": _p(first_chunks, 50),
        "first_chunk_ms_p95": _p(first_chunks, 95),
        "retrieval_signal_counts": signal_counts,
        "rounds_used_avg": (sum(rounds) / len(rounds)) if rounds else None,
        "total_llm_tokens_avg": (sum(tokens) / len(tokens)) if tokens else None,
        "zero_sources_count": sum(1 for t in completed if t.sources_count == 0),
    }


def _print_table(turns: list[TurnResult]) -> None:
    print("\n" + "=" * 100)
    print(f"{'ID':<10} {'Status':<10} {'ms':>6} {'fcms':>6} {'src':>4} {'rnd':>4} {'tok':>6} {'signals':<22} Q")
    print("-" * 100)
    for t in turns:
        sig = ",".join(t.retrieval_signals or []) or "-"
        q_short = (t.question or "")[:30]
        print(
            f"{t.question_id:<10} {t.status:<10} {t.duration_ms:>6d} "
            f"{(t.first_chunk_ms or 0):>6d} {t.sources_count:>4d} "
            f"{(t.rounds_used or 0):>4d} {(t.total_llm_tokens or 0):>6d} "
            f"{sig[:22]:<22} {q_short}"
        )
    print("=" * 100)


# ── Main ──────────────────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--base-url", required=True,
                        help="Chat API base URL (no trailing slash)")
    parser.add_argument("--questions", default=None,
                        help="Path to YAML question file (defaults to mobius-retriever/eval_questions_dev.yaml, then built-in set)")
    parser.add_argument("--out", default=None,
                        help="Output JSON path (default: /tmp/chat_bench_<run_id>.json)")
    parser.add_argument("--chat-mode", default="copilot",
                        choices=["copilot", "agentic", "quick"])
    parser.add_argument("--per-turn-timeout-s", type=int, default=120)
    parser.add_argument("--pause-s", type=float, default=1.5,
                        help="Seconds between turns (avoid quota spikes)")
    parser.add_argument("--no-stream", action="store_true",
                        help="Skip SSE; poll only. Useful if SSE is failing and you want to isolate.")
    parser.add_argument("--limit", type=int, default=0,
                        help="Run only the first N questions (0 = all)")
    parser.add_argument("--tag", default="",
                        help="Free-form tag saved into the run metadata (e.g. 'rag_v2', 'post_chroma_swap')")
    parser.add_argument("--cache-assist", choices=["on", "off"], default=None,
                        help="Force cache-assist on/off per turn via POST /chat body. "
                             "Omit to let the server apply normal mode-selection rules (recommended). "
                             "Use 'off' to establish a no-cache baseline for A/B comparison.")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    run_id = str(uuid.uuid4())
    base_url = args.base_url.rstrip("/")
    out_path = args.out or f"/tmp/chat_bench_{run_id[:8]}.json"
    questions = _load_questions(args.questions)
    if args.limit > 0:
        questions = questions[: args.limit]

    print(f"run_id={run_id}")
    print(f"base_url={base_url}")
    print(f"chat_mode={args.chat_mode}")
    print(f"questions={len(questions)}  (from {'fallback' if questions is FALLBACK_QUESTIONS else args.questions or 'auto-discovered YAML'})")
    print(f"output={out_path}")
    print(f"tag={args.tag or '(none)'}")

    # Quick health check so we fail fast.
    try:
        r = httpx.get(f"{base_url}/health", timeout=10)
        print(f"health={r.status_code}")
    except Exception as exc:
        print(f"WARNING: /health probe failed ({exc}); continuing anyway", file=sys.stderr)

    results: list[TurnResult] = []
    with httpx.Client(http2=False) as client:
        for i, q in enumerate(questions, start=1):
            print(f"\n[{i}/{len(questions)}] {q['id']}: {q['question'][:80]}")
            cache_override: bool | None = None
            if args.cache_assist == "off":
                cache_override = False
            elif args.cache_assist == "on":
                cache_override = True
            t = run_one(
                client, base_url, q,
                chat_mode=args.chat_mode,
                per_turn_timeout_s=args.per_turn_timeout_s,
                use_stream=not args.no_stream,
                cache_assist=cache_override,
            )
            results.append(t)
            print(
                f"    → status={t.status} "
                f"dur={t.duration_ms}ms fcms={t.first_chunk_ms} "
                f"sources={t.sources_count} signals={t.retrieval_signals} "
                f"rounds={t.rounds_used} tokens={t.total_llm_tokens}"
            )
            if t.error:
                print(f"    ! error: {t.error}")
            if i < len(questions) and args.pause_s > 0:
                time.sleep(args.pause_s)

    summary = _summarize(results)
    report = {
        "run_id": run_id,
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "base_url": base_url,
        "chat_mode": args.chat_mode,
        "tag": args.tag,
        "question_source": args.questions or "auto",
        "summary": summary,
        "questions": [asdict(t) for t in results],
    }
    Path(out_path).write_text(json.dumps(report, indent=2))
    print("\n" + "=" * 100)
    print("SUMMARY")
    print(json.dumps(summary, indent=2))
    _print_table(results)
    print(f"\nWrote report → {out_path}")
    # Exit code: 0 if all completed; 1 if any errored/timed out.
    return 0 if summary["n_failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
