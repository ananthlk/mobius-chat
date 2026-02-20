#!/usr/bin/env python3
"""Trace a user query through the full chat pipeline to find where it fails.

Run from Mobius root:
  PYTHONPATH=mobius-chat python mobius-chat/scripts/trace_query.py "How to file a grievance"
  CHAT_DEBUG_TRACE=1 PYTHONPATH=mobius-chat python mobius-chat/scripts/trace_query.py "How to file a grievance"

Shows each stage: planner → blueprint → answer per subquestion → format response.
On failure: prints the exception and traceback.
"""
from __future__ import annotations

import sys
import traceback
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


def _trunc(s: str, n: int = 70) -> str:
    s = (s or "").replace("\n", " ")
    return (s[: n - 3] + "...") if len(s) > n else s


def main() -> int:
    message = " ".join(sys.argv[1:]).strip()
    if not message:
        print("Usage: python trace_query.py <your question>")
        print('Example: python trace_query.py "How to file a grievance"')
        return 1

    print("=" * 80)
    print("TRACE: Full chat pipeline")
    print("=" * 80)
    print(f"\nQuery: {message}\n")

    # --- Stage 1: Planner (parse) ---
    print("-" * 60)
    print("STAGE 1: PLANNER (parse)")
    print("-" * 60)
    try:
        from app.planner import parse

        thinking: list[str] = []

        def on_thinking(chunk: str) -> None:
            thinking.append(chunk)
            print(f"  [thinking] {chunk[:80]}")

        plan = parse(message, thinking_emitter=on_thinking)
        print(f"  subquestions: {len(plan.subquestions)}")
        for sq in plan.subquestions:
            intent = getattr(sq, "question_intent", None) or "—"
            print(f"    {sq.id}: kind={sq.kind} intent={intent} text={_trunc(sq.text, 55)}")
    except Exception as e:
        print(f"\n>>> FAILED at STAGE 1 (Planner): {e}")
        traceback.print_exc()
        return 1

    # --- Stage 2: Blueprint ---
    print("\n" + "-" * 60)
    print("STAGE 2: BLUEPRINT")
    print("-" * 60)
    try:
        from app.planner.blueprint import build_blueprint
        from app.chat_config import get_chat_config

        rag_k = get_chat_config().rag.top_k
        blueprint = build_blueprint(plan, rag_default_k=rag_k)
        for entry in blueprint:
            print(f"  {entry['sq_id']}: agent={entry['agent']} rag_k={entry['rag_k']} kind={entry['kind']}")
    except Exception as e:
        print(f"\n>>> FAILED at STAGE 2 (Blueprint): {e}")
        traceback.print_exc()
        return 1

    # --- Stage 3: Answer each subquestion ---
    print("\n" + "-" * 60)
    print("STAGE 3: ANSWER SUBQUESTIONS (RAG + LLM)")
    print("-" * 60)
    answers: list[str] = []
    sources: list[dict] = []
    for i, sq in enumerate(plan.subquestions):
        print(f"\n  Subquestion {sq.id}: {_trunc(sq.text, 60)}")
        try:
            from app.services.non_patient_rag import answer_non_patient
            from app.services.retrieval_calibration import get_retrieval_blend, intent_to_score

            score = getattr(sq, "intent_score", None) or intent_to_score(getattr(sq, "question_intent", None))
            params = get_retrieval_blend(score)

            def emit(msg: str) -> None:
                print(f"    [emit] {msg[:80]}")

            ans, srcs, usage, signal = answer_non_patient(
                question=sq.text,
                k=params.get("top_k"),
                confidence_min=params.get("confidence_min"),
                n_hierarchical=params.get("n_hierarchical"),
                n_factual=params.get("n_factual"),
                emitter=emit,
            )
            answers.append(ans)
            sources.extend(srcs or [])
            print(f"    answer_len={len(ans)} sources={len(srcs or [])} retrieval_signal={signal}")
        except Exception as e:
            print(f"\n>>> FAILED at STAGE 3 (Answer {sq.id}): {e}")
            traceback.print_exc()
            return 1

    # --- Stage 4: Format response ---
    print("\n" + "-" * 60)
    print("STAGE 4: FORMAT RESPONSE (Integrator LLM)")
    print("-" * 60)
    try:
        from app.responder import format_response
        from app.services.doc_assembly import RETRIEVAL_SIGNAL_CORPUS_ONLY
        from app.services.cost_model import compute_cost

        retrieval_signals = [RETRIEVAL_SIGNAL_CORPUS_ONLY]  # simplified
        all_sources = sources
        labels = [s.get("confidence_label") for s in all_sources if s.get("confidence_label")]
        default_confidence = "approved_authoritative" if labels else "informational_only"
        retrieval_metadata = {"default_source_confidence": default_confidence}
        sources_summary = [
            {"index": s.get("index", i + 1), "document_name": s.get("document_name") or "document", "confidence_label": s.get("confidence_label")}
            for i, s in enumerate(all_sources)
        ]

        def emit(msg: str) -> None:
            print(f"  [emit] {msg[:80]}")

        final_message, integrator_usage = format_response(
            plan, answers, user_message=message, emitter=emit,
            retrieval_metadata=retrieval_metadata, sources_summary=sources_summary,
        )
        print(f"\n  Final message length: {len(final_message)}")
        print("\n--- Final response (first 500 chars) ---")
        print((final_message or "(empty)")[:500])
    except Exception as e:
        print(f"\n>>> FAILED at STAGE 4 (Format response): {e}")
        traceback.print_exc()
        return 1

    print("\n" + "=" * 80)
    print("SUCCESS: All stages completed")
    print("=" * 80)
    return 0


if __name__ == "__main__":
    sys.exit(main())
