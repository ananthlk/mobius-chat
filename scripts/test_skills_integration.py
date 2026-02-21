#!/usr/bin/env python3
"""Integration test for Google search and web scraper via tool agent.

Requires:
- CHAT_SKILLS_GOOGLE_SEARCH_URL (e.g. http://localhost:8004/search?) and mobius-google-search running
- CHAT_SKILLS_WEB_SCRAPER_URL (e.g. http://localhost:8002/scrape/review) and mobius-web-scraper running

Run from Mobius root:
  PYTHONPATH=mobius-chat python mobius-chat/scripts/test_skills_integration.py
  # With mstart running (or skills started manually):
  CHAT_SKILLS_GOOGLE_SEARCH_URL=http://localhost:8004/search? CHAT_SKILLS_WEB_SCRAPER_URL=http://localhost:8002/scrape/review \\
    PYTHONPATH=mobius-chat python mobius-chat/scripts/test_skills_integration.py
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


def test_google_search() -> bool:
    """Test Google search via tool agent."""
    from app.services.doc_assembly import google_search_via_skills_api

    base = os.environ.get("CHAT_SKILLS_GOOGLE_SEARCH_URL", "").strip()
    if not base:
        print("  SKIP: CHAT_SKILLS_GOOGLE_SEARCH_URL not set")
        return False
    results = google_search_via_skills_api("Florida Medicaid eligibility", max_results=2)
    if not results:
        print("  FAIL: No results returned")
        return False
    print(f"  OK: Got {len(results)} results")
    for i, r in enumerate(results[:2], 1):
        print(f"    [{i}] {r.get('document_name', '')[:50]}...")
    return True


def test_web_scrape() -> bool:
    """Test web scraper via tool agent helper."""
    from app.services.tool_agent import web_scrape_via_skills_api

    base = os.environ.get("CHAT_SKILLS_WEB_SCRAPER_URL", "").strip()
    if not base:
        print("  SKIP: CHAT_SKILLS_WEB_SCRAPER_URL not set")
        return False
    url = "https://www.sunshinehealth.com/providers/utilization-management/clinical-payment-policies.html"
    result = web_scrape_via_skills_api(url, include_summary=False)
    if not result or not result.get("text"):
        print("  FAIL: No content returned")
        return False
    text = result.get("text", "")
    print(f"  OK: Got {len(text)} chars from Sunshine Health clinical policies page")
    print(f"    Preview: {text[:150].replace(chr(10), ' ')}...")
    return True


def test_tool_agent_search() -> bool:
    """Test full tool agent path for search request."""
    from app.planner import parse
    from app.planner.blueprint import build_blueprint
    from app.services.retrieval_calibration import get_retrieval_blend
    from app.stages.resolve import _answer_for_subquestion

    base = os.environ.get("CHAT_SKILLS_GOOGLE_SEARCH_URL", "").strip()
    if not base:
        print("  SKIP: CHAT_SKILLS_GOOGLE_SEARCH_URL not set")
        return False

    question = "Search for Florida Medicaid eligibility requirements"
    plan = parse(question, context="Available: rag, tools (google_search, web_scrape), reasoning.")
    blueprint = build_blueprint(plan, rag_default_k=5)
    if not plan.subquestions or not blueprint:
        print("  FAIL: No plan/blueprint")
        return False
    bp = blueprint[0]
    agent = bp.get("agent", "?")
    if agent != "tool":
        print(f"  FAIL: Expected agent=tool, got {agent}")
        return False
    text = bp.get("reframed_text") or bp.get("text") or plan.subquestions[0].text
    ans, _, _, signal = _answer_for_subquestion(
        correlation_id="test",
        sq_id=plan.subquestions[0].id,
        agent="tool",
        kind="non_patient",
        text=text,
    )
    if not ans or "Florida" not in ans and "Medicaid" not in ans and "eligibility" not in ans:
        print(f"  FAIL: Answer doesn't seem relevant: {ans[:200]}...")
        return False
    print(f"  OK: Tool agent returned answer ({len(ans)} chars), signal={signal}")
    return True


def main() -> int:
    print("Skills Integration Test")
    print("=" * 60)
    print(f"CHAT_SKILLS_GOOGLE_SEARCH_URL: {os.environ.get('CHAT_SKILLS_GOOGLE_SEARCH_URL', '(not set)')}")
    print(f"CHAT_SKILLS_WEB_SCRAPER_URL: {os.environ.get('CHAT_SKILLS_WEB_SCRAPER_URL', '(not set)')}")
    print()

    passed = 0
    total = 0

    print("1. Google search (doc_assembly)")
    total += 1
    if test_google_search():
        passed += 1
    print()

    print("2. Web scrape (tool_agent)")
    total += 1
    if test_web_scrape():
        passed += 1
    print()

    print("3. Tool agent search request (full path)")
    total += 1
    if test_tool_agent_search():
        passed += 1
    print()

    print("=" * 60)
    print(f"Passed: {passed}/{total}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
