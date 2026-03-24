#!/usr/bin/env python3
"""Trace credentialing report Q&A through the full pipeline with real parser and LLM.

Shows exactly where parser (LLM) and integrator (LLM) are invoked so you can see where it breaks.

Run from Mobius root:
  PYTHONPATH=mobius-chat uv run python mobius-chat/scripts/trace_credentialing.py "What is the latest report for David Lawrence Center?"
  PYTHONPATH=mobius-chat uv run python mobius-chat/scripts/trace_credentialing.py --mock-skill "What is the latest report for David Lawrence Center?"
  PYTHONPATH=mobius-chat uv run python mobius-chat/scripts/trace_credentialing.py --after-report "How many NPIs have issues with PML?"

  --mock-skill   Use a fake stored report and answer so the flow "works" end-to-end (no real skill needed).
  --after-report Simulate state after a report was just run (active_skill, report_run_id); trace follow-up without re-running.

Requires:
  - CHAT_SKILLS_PROVIDER_ROSTER_CREDENTIALING_URL set (or you get "not configured" from tool)
  - LLM configured (parser + integrator will call get_llm_provider())
"""
from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path
from unittest.mock import patch

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

# Example credentialing questions
DEFAULT_QUESTIONS = [
    "What is the latest report for David Lawrence Center?",
    "What does the report say about at-risk revenue?",
]

# State to simulate "we just ran a report for David Lawrence Center" (for --after-report trace)
AFTER_REPORT_STATE = {
    "active_skill": {
        "skill": "roster_report",
        "org": "David Lawrence Center",
        "data": {
            "section_a_count": 41,
            "section_b_count": 3,
            "section_c_count": 45,
            "section_d_count": 14,
            "readiness_score": 46.07,
            "total_opportunity": 1969457.66,
        },
        "turn": "trace",
    },
    "active": {
        "report_run_id": "trace-run-123",
        "last_report_org": "David Lawrence Center",
    },
}

# When --mock-skill: fake answer so the trace shows a "working" run
MOCK_REPORT_ANSWER = (
    "The credentialing report for David Lawrence Center shows 12 providers in Section A (ready for PML), "
    "$2.1M at-risk revenue in Section B, and 3 providers in Section C needing attention. "
    "Readiness score is 78%."
)
MOCK_SOURCES = [{"index": 1, "document_name": "Credentialing report", "text": MOCK_REPORT_ANSWER[:300], "source_type": "external"}]


def _trunc(s: str, n: int = 72) -> str:
    s = (s or "").replace("\n", " ")
    return (s[: n - 3] + "...") if len(s) > n else s


def main() -> int:
    args = sys.argv[1:]
    mock_skill = "--mock-skill" in args
    after_report = "--after-report" in args
    if mock_skill:
        args = [a for a in args if a != "--mock-skill"]
    if after_report:
        args = [a for a in args if a != "--after-report"]
    if args and not args[0].startswith("-"):
        messages = [" ".join(args)]
    else:
        messages = DEFAULT_QUESTIONS[:1]
    if after_report and messages == DEFAULT_QUESTIONS[:1]:
        messages = ["How many NPIs have issues with PML?"]

    credentialing_url = (os.environ.get("CHAT_SKILLS_PROVIDER_ROSTER_CREDENTIALING_URL") or "").strip()
    if mock_skill:
        os.environ["CHAT_SKILLS_PROVIDER_ROSTER_CREDENTIALING_URL"] = "http://mock:8011"
        credentialing_url = "http://mock:8011 (mocked)"
    print("=" * 80)
    print("TRACE: Credentialing report Q&A (real parser + LLM + integrator)")
    print("=" * 80)
    print(f"\nCHAT_SKILLS_PROVIDER_ROSTER_CREDENTIALING_URL: {credentialing_url}")
    if mock_skill:
        print("  (--mock-skill: tool will return a fake report answer so the flow succeeds)")
    if after_report:
        print("  (--after-report: simulating state AFTER a report was just run for David Lawrence Center)")
        print("  merged_state: active_skill=roster_report, report_run_id, last_report_org")
    print()

    merged_state = AFTER_REPORT_STATE if after_report else None

    for msg_idx, message in enumerate(messages):
        print("\n" + "=" * 80)
        print(f"QUESTION {msg_idx + 1}: {message}")
        print("=" * 80)

        # --- Stage 1: Parser (LLM invoked here) ---
        print("\n--- STAGE 1: PARSER (LLM invoked) ---")
        try:
            from app.planner import parse
            from app.pipeline.message_resolver import build_skill_context_summary
            thinking: list[str] = []
            def on_thinking(chunk: str) -> None:
                thinking.append(chunk)
                print(f"  [thinking] {_trunc(chunk, 60)}")
            parser_context = ""
            if merged_state and merged_state.get("active_skill"):
                parser_context = build_skill_context_summary(merged_state["active_skill"]) + "\n\n"
            parser_context = (parser_context or "") + "Available paths and capabilities: (omitted)"
            plan = parse(message, thinking_emitter=on_thinking, context=parser_context)
            print(f"  → subquestions: {len(plan.subquestions)}")
            for sq in plan.subquestions:
                kind = getattr(sq, "kind", "—")
                intent = getattr(sq, "question_intent", None) or "—"
                caps = getattr(sq, "capabilities_primary", None) or "—"
                req_j = getattr(sq, "requires_jurisdiction", None)
                print(f"     {sq.id}: kind={kind} intent={intent} capabilities_primary={caps} requires_jurisdiction={req_j}")
                print(f"     text: {_trunc(sq.text, 60)}")
        except Exception as e:
            print(f"  >>> FAILED (Parser): {e}")
            import traceback
            traceback.print_exc()
            return 1

        # --- Stage 2: Blueprint (deterministic route + agent) ---
        print("\n--- STAGE 2: BLUEPRINT (detect_route + active_skill pre-check + agent) ---")
        try:
            from app.planner.blueprint import build_blueprint
            from app.planner.route_triggers import detect_route
            from app.chat_config import get_chat_config
            route_agent, route_conf, _ = detect_route(message)
            print(f"  detect_route(message) → agent={route_agent} confidence={route_conf}")
            rag_k = get_chat_config().rag.top_k
            retrieval_ctx = {"user_message": message}
            if merged_state and merged_state.get("active_skill"):
                retrieval_ctx["active_skill"] = merged_state["active_skill"]
                print(f"  retrieval_ctx.active_skill: roster_report org={merged_state['active_skill'].get('org')}")
            blueprint = build_blueprint(plan, rag_default_k=rag_k, retrieval_ctx=retrieval_ctx)
            for i, entry in enumerate(blueprint):
                print(f"  → {entry['sq_id']}: agent={entry['agent']} rag_k={entry['rag_k']} kind={entry['kind']}")
                if entry.get("tool_hint"):
                    print(f"     tool_hint={entry['tool_hint']}")
        except Exception as e:
            print(f"  >>> FAILED (Blueprint): {e}")
            import traceback
            traceback.print_exc()
            return 1

        # --- Stage 2.5: Clarify (would we ask for health plan?) ---
        if after_report and merged_state:
            print("\n--- STAGE 2.5: CLARIFY (jurisdiction check) ---")
            try:
                from app.state.clarification import need_jurisdiction_clarification
                active = merged_state.get("active") or {}
                needs_clar, missing_slots, clarification_message = need_jurisdiction_clarification(
                    plan.subquestions, active, question_text=message, rag_url=""
                )
                print(f"  need_jurisdiction_clarification → needs_clar={needs_clar} missing_slots={missing_slots}")
                if needs_clar and clarification_message:
                    print(f"  clarification_message: {_trunc(clarification_message, 60)}")
                if blueprint and merged_state.get("active_skill") and (blueprint[0].get("agent") in ("reasoning", "tool")):
                    print("  → Override: active_skill=roster_report and agent=reasoning/tool → skip clarification (no payer ask)")
            except Exception as e:
                print(f"  >>> Clarify check error: {e}")

        # --- Stage 3: Answer subquestion (tool path → credentialing block) ---
        print("\n--- STAGE 3: ANSWER (tool agent; credentialing block if intent match) ---")
        answers: list[str] = []
        sources: list[dict] = []
        from app.services.doc_assembly import RETRIEVAL_SIGNAL_ROSTER_COMPLETE

        def _run_stage3():
            nonlocal answers, sources
            from app.stages.resolve import _answer_for_subquestion
            from app.pipeline.message_resolver import build_skill_context_summary
            skill_ctx_str = None
            if after_report and merged_state and merged_state.get("active_skill"):
                skill_ctx_str = build_skill_context_summary(merged_state["active_skill"])
            for i, sq in enumerate(plan.subquestions):
                bp = blueprint[i] if i < len(blueprint) else {}
                agent = bp.get("agent") or ("RAG" if sq.kind == "non_patient" else "patient_stub")
                question_text = bp.get("reframed_text") or bp.get("text") or sq.text
                print(f"  Subq {sq.id}: agent={agent} text={_trunc(question_text, 50)}")
                def emit(m: str) -> None:
                    print(f"    [emit] {_trunc(m, 60)}")
                active_ctx = (merged_state or {}).copy() if after_report else {}
                ans, usage, srcs, signal, layer = _answer_for_subquestion(
                    correlation_id=str(uuid.uuid4()),
                    sq_id=sq.id,
                    agent=agent,
                    kind=sq.kind,
                    text=question_text,
                    retrieval_params=None,
                    emitter=emit,
                    rag_filter_overrides=None,
                    on_rag_fail=bp.get("on_rag_fail"),
                    user_message=message,
                    active_context=active_ctx,
                    tool_hint=bp.get("tool_hint"),
                    question_intent=bp.get("question_intent") or getattr(sq, "question_intent", None),
                    active_skill_context=skill_ctx_str if (after_report and agent == "reasoning") else None,
                )
                answers.append(ans)
                sources.extend(srcs or [])
                print(f"  → answer length={len(ans)} retrieval_signal={signal} layer_used={layer}")
                print(f"  → answer (first 200 chars): {_trunc(ans, 200)}")

        try:
            if mock_skill:
                with patch("app.services.tool_agent._get_latest_run_for_org", return_value={"report_run_id": "mock-run-1", "org_name": "David Lawrence Center"}):
                    with patch(
                        "app.services.tool_agent._ask_credentialing_report",
                        return_value=(MOCK_REPORT_ANSWER, MOCK_SOURCES, None, RETRIEVAL_SIGNAL_ROSTER_COMPLETE),
                    ):
                        _run_stage3()
            else:
                _run_stage3()
        except Exception as e:
            print(f"  >>> FAILED (Answer): {e}")
            import traceback
            traceback.print_exc()
            return 1

        # --- Stage 4: Integrator (LLM invoked here) ---
        print("\n--- STAGE 4: INTEGRATOR (LLM invoked) ---")
        try:
            from app.responder import format_response
            from app.services.doc_assembly import RETRIEVAL_SIGNAL_ROSTER_COMPLETE
            retrieval_signals = [RETRIEVAL_SIGNAL_ROSTER_COMPLETE] if sources else ["corpus_only"]
            default_confidence = "approved_authoritative" if sources else "no_sources"
            retrieval_metadata = {"default_source_confidence": default_confidence}
            sources_summary = [
                {"index": s.get("index", i + 1), "document_name": s.get("document_name") or "document", "confidence_label": s.get("confidence_label")}
                for i, s in enumerate(sources)
            ]
            print("  Input to format_response:")
            print(f"    plan.subquestions: {len(plan.subquestions)}")
            print(f"    answers (stub_answers): {[len(a) for a in answers]} chars each")
            print(f"    user_message: {_trunc(message, 50)}")
            def emit(m: str) -> None:
                print(f"  [emit] {_trunc(m, 60)}")
            final_message, integrator_usage = format_response(
                plan, answers, user_message=message, emitter=emit,
                retrieval_metadata=retrieval_metadata, sources_summary=sources_summary,
            )
            print(f"  → final_message length: {len(final_message or '')}")
            if final_message:
                raw = (final_message or "").strip()
                if raw.startswith("{"):
                    try:
                        parsed = json.loads(raw)
                        da = parsed.get("direct_answer", "")
                        print(f"  → parsed mode={parsed.get('mode')} direct_answer (first 200): {_trunc(da, 200)}")
                    except json.JSONDecodeError:
                        print(f"  → (not JSON) first 300 chars: {raw[:300]}")
                else:
                    print(f"  → first 400 chars: {final_message[:400]}")
        except Exception as e:
            print(f"  >>> FAILED (Integrator): {e}")
            import traceback
            traceback.print_exc()
            return 1

    print("\n" + "=" * 80)
    print("TRACE DONE")
    print("=" * 80)
    return 0


if __name__ == "__main__":
    sys.exit(main())
