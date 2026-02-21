#!/usr/bin/env python3
"""Run test questions through chat pipeline to verify agent routing: RAG, tool, reasoning.

Tests:
- Capability: "Can you search Google?" -> tool agent, capability answer
- Reasoning: "What does prior authorization mean?" -> reasoning agent
- RAG: "How do I file an appeal?" (with payer) -> RAG agent
- Search request: "Search for Florida Medicaid eligibility" -> tool agent, Google search
- Web scrape: "Can you scrape web pages?" -> tool agent, capability answer

Run from Mobius root:
  PYTHONPATH=mobius-chat python mobius-chat/scripts/test_agent_routing.py
  PYTHONPATH=mobius-chat python mobius-chat/scripts/test_agent_routing.py --payer "Sunshine Health"
"""
from __future__ import annotations

import os
import sys
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

TEST_QUESTIONS = [
    {"q": "Can you search Google?", "expect_agent": "tool", "expect_in": ["Yes", "search", "web"]},
    {"q": "What can you do?", "expect_agent": "tool", "expect_in": ["policy", "search", "appeals"]},
    {"q": "Can you scrape web pages?", "expect_agent": "tool", "expect_in": ["Yes", "scrape"]},
    {"q": "Scrape https://www.sunshinehealth.com/providers/utilization-management/clinical-payment-policies.html", "expect_agent": "tool", "expect_in": ["Clinical", "Payment", "Policies"]},
    {"q": "What does prior authorization mean in general?", "expect_agent": "reasoning", "expect_in": []},
    {"q": "Explain the difference between a grievance and an appeal.", "expect_agent": "reasoning", "expect_in": []},
    {"q": "How do I file an appeal?", "expect_agent": "RAG", "expect_in": [], "payer": "Sunshine Health"},
    {"q": "Search for Florida Medicaid eligibility requirements", "expect_agent": "tool", "expect_in": []},
]


def run_question(question: str, payer: str | None = None):
    """Run one question; return (agent, answer_preview, full_answer)."""
    from app.planner import parse
    from app.planner.blueprint import build_blueprint
    from app.chat_config import get_chat_config
    from app.stages.resolve import _answer_for_subquestion
    from app.services.retrieval_calibration import get_retrieval_blend, intent_to_score
    from app.state.jurisdiction import rag_filters_from_active

    rag_filter_overrides = {}
    if payer:
        try:
            from app.payer_normalization import normalize_payer_for_rag
            canonical = normalize_payer_for_rag(payer)
            rag_filter_overrides["filter_payer"] = canonical or payer
        except Exception:
            rag_filter_overrides["filter_payer"] = payer

    plan = parse(question, context=f"Available: rag, tools (google_search, web_scrape), reasoning.")
    blueprint = build_blueprint(plan, rag_default_k=get_chat_config().rag.top_k)

    if not plan.subquestions:
        return ("none", "", "No subquestions")
    sq = plan.subquestions[0]
    bp = blueprint[0] if blueprint else {}
    agent = bp.get("agent", "?")
    question_text = bp.get("reframed_text") or bp.get("text") or sq.text
    retrieval_params = get_retrieval_blend(sq.intent_score or 0.5) if agent == "RAG" else None

    ans, _, _, _ = _answer_for_subquestion(
        correlation_id="test",
        sq_id=sq.id,
        agent=agent,
        kind=sq.kind,
        text=question_text,
        retrieval_params=retrieval_params,
        rag_filter_overrides=rag_filter_overrides or None,
        on_rag_fail=bp.get("on_rag_fail"),
    )
    preview = (ans or "")[:200].replace("\n", " ")
    return (agent, preview, ans or "")


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--payer", default="Sunshine Health", help="Payer for RAG questions")
    ap.add_argument("--q", help="Single question to run")
    args = ap.parse_args()
    payer = (args.payer or "").strip() or None

    questions = TEST_QUESTIONS if not args.q else [{"q": args.q, "expect_agent": "?", "expect_in": []}]
    print("Agent Routing Test")
    print("=" * 70)
    has_google = bool(os.environ.get("CHAT_SKILLS_GOOGLE_SEARCH_URL", "").strip())
    has_scraper = bool(os.environ.get("CHAT_SKILLS_WEB_SCRAPER_URL", "").strip())
    print(f"CHAT_SKILLS_GOOGLE_SEARCH_URL: {'set' if has_google else 'not set'}")
    print(f"CHAT_SKILLS_WEB_SCRAPER_URL: {'set' if has_scraper else 'not set'}")
    print()

    passed = 0
    failed = 0
    for t in questions:
        q = t["q"]
        expect_agent = t.get("expect_agent", "?")
        expect_in = t.get("expect_in", [])
        use_payer = t.get("payer") or payer
        print(f"Q: {q}")
        print(f"  Expected agent: {expect_agent}")
        try:
            agent, preview, full = run_question(q, payer=use_payer)
            print(f"  Actual agent:   {agent}")
            print(f"  Answer:         {preview}...")
            if expect_agent != "?" and agent == expect_agent:
                passed += 1
                for kw in expect_in:
                    if kw.lower() in (full or "").lower():
                        print(f"  [OK] Answer contains '{kw}'")
            elif expect_agent != "?":
                failed += 1
                print(f"  [MISMATCH] Expected agent={expect_agent}")
        except Exception as e:
            failed += 1
            print(f"  [ERROR] {e}")
        print()
    if not args.q:
        print("=" * 70)
        print(f"Passed: {passed}  Failed: {failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
