#!/usr/bin/env python3
"""Test org search, healthcare, and multi-part query via chat pipeline simulation.

Run from Mobius root:
  PYTHONPATH=mobius-chat python mobius-chat/scripts/test_tool_skills.py

Requires (for full pass):
  - mobius-skills-mcp (port 8006) — restart after adding healthcare_query tool
  - mobius-skills/provider-roster-credentialing (CHAT_SKILLS_PROVIDER_ROSTER_CREDENTIALING_URL)
  - mobius-skills/healthcare (CHAT_SKILLS_HEALTHCARE_URL, port 8007)
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


def _trunc(s: str, n: int = 60) -> str:
    s = (s or "").replace("\n", " ")
    return (s[: n - 3] + "...") if len(s) > n else s


def test_answer_tool(question: str, label: str, *, user_message: str | None = None) -> tuple[bool, str]:
    """Call answer_tool and return (success, message)."""
    from app.services.tool_agent import answer_tool

    def emit(msg: str) -> None:
        print(f"    [emit] {msg[:70]}")

    try:
        answer, sources, _, signal = answer_tool(
            question,
            emitter=emit,
            invoke_google_for_search_request=True,
            user_message=user_message or question,
        )
        ans = answer or ""
        # Fail on explicit errors or MCP/tool not available
        fail_markers = ("Error:", "Unknown tool", "MCP client not available", "MCP call failed")
        ok = bool(ans) and not any(m in ans for m in fail_markers)
        return ok, ans or "(empty)"
    except Exception as e:
        return False, str(e)


def test_parse_multi(text: str) -> tuple[bool, list[str]]:
    """Parse multi-part query; return (success, list of subquestion texts)."""
    from app.planner import parse

    try:
        plan = parse(text)
        sq_texts = [sq.text or "" for sq in plan.subquestions]
        return True, sq_texts
    except Exception as e:
        return False, [str(e)]


def test_full_pipeline(question: str) -> tuple[bool, str]:
    """Run full trace (parse → blueprint → answer) for one question."""
    import uuid
    from app.planner import parse
    from app.planner.blueprint import build_blueprint
    from app.chat_config import get_chat_config
    from app.stages.resolve import _answer_for_subquestion

    try:
        plan = parse(question)
        blueprint = build_blueprint(plan, rag_default_k=get_chat_config().rag.top_k)
        answers = []
        for i, sq in enumerate(plan.subquestions):
            bp = blueprint[i] if i < len(blueprint) else {}
            agent = bp.get("agent") or "RAG"
            qtext = bp.get("reframed_text") or bp.get("text") or sq.text

            ans, _, _, _ = _answer_for_subquestion(
                correlation_id=str(uuid.uuid4()),
                sq_id=sq.id,
                agent=agent,
                kind=sq.kind,
                text=qtext,
                emitter=lambda m: None,
                user_message=question,
            )
            answers.append(ans)
        combined = "\n\n".join(answers)
        ok = bool(combined) and "Error:" not in combined[:200]
        return ok, combined
    except Exception as e:
        return False, str(e)


def main() -> int:
    print("=" * 70)
    print("Tool skills test: org search, healthcare, multi-part query")
    print("=" * 70)

    # Check env
    roster_url = os.environ.get("CHAT_SKILLS_PROVIDER_ROSTER_CREDENTIALING_URL", "").strip()
    healthcare_url = os.environ.get("CHAT_SKILLS_HEALTHCARE_URL", "").strip()
    mcp_url = os.environ.get("MCP_SERVER_URL", "http://localhost:8006/mcp").strip()
    print(f"\n  CHAT_SKILLS_PROVIDER_ROSTER_CREDENTIALING_URL: {roster_url or '(not set)'}")
    print(f"  CHAT_SKILLS_HEALTHCARE_URL: {healthcare_url or '(not set)'}")
    print(f"  MCP_SERVER_URL: {mcp_url}")

    passed = 0
    failed = 0

    # --- 1. Org search ---
    print("\n" + "-" * 60)
    print("1. Org search: 'What is the NPI of Circles of Care?'")
    print("-" * 60)
    ok, msg = test_answer_tool("What is the NPI of Circles of Care?", "org_search")
    if ok:
        print(f"  PASS: {_trunc(msg, 120)}")
        passed += 1
    else:
        print(f"  FAIL: {_trunc(msg, 120)}")
        failed += 1

    # --- 2. Healthcare (ICD-10) ---
    print("\n" + "-" * 60)
    print("2. Healthcare ICD-10: 'What does ICD-10 Z00.00 mean?'")
    print("-" * 60)
    ok, msg = test_answer_tool("What does ICD-10 Z00.00 mean?", "healthcare_icd10")
    if ok:
        print(f"  PASS: {_trunc(msg, 120)}")
        passed += 1
    else:
        print(f"  FAIL: {_trunc(msg, 120)}")
        failed += 1

    # --- 3. Healthcare (NPI number) ---
    print("\n" + "-" * 60)
    print("3. Healthcare NPI lookup: 'Look up NPI 1234567890'")
    print("-" * 60)
    ok, msg = test_answer_tool("Look up NPI 1234567890", "healthcare_npi")
    if ok:
        print(f"  PASS: {_trunc(msg, 120)}")
        passed += 1
    else:
        print(f"  FAIL: {_trunc(msg, 120)}")
        failed += 1

    # --- 4. Parser multi-part decomposition ---
    print("\n" + "-" * 60)
    print("4. Parser: multi-part 'NPI of Circles of Care AND ICD-10 Z00.00'")
    print("-" * 60)
    multi = "What is the NPI of Circles of Care and what does ICD-10 Z00.00 mean?"
    ok, sq_texts = test_parse_multi(multi)
    if ok and len(sq_texts) >= 2:
        print(f"  PASS: Decomposed into {len(sq_texts)} subquestion(s)")
        for i, t in enumerate(sq_texts, 1):
            print(f"    {i}. {_trunc(t, 55)}")
        passed += 1
    else:
        print(f"  FAIL: {len(sq_texts)} subquestion(s) - expected >=2. Texts: {sq_texts}")
        failed += 1

    # --- 5. Full pipeline multi-part ---
    print("\n" + "-" * 60)
    print("5. Full pipeline: multi-part query (both subquestions answered)")
    print("-" * 60)
    ok, msg = test_full_pipeline(multi)
    # Check we got two distinct answers; no bad extraction; no tool errors in either part
    has_bad_extraction = "Circles of Care and what does" in (msg or "")
    has_tool_error = any(m in (msg or "") for m in ("Unknown tool", "Error:", "MCP client not available"))
    if ok and not has_bad_extraction and not has_tool_error:
        print(f"  PASS: {_trunc(msg, 200)}")
        passed += 1
    else:
        reason = " (bad org extraction)" if has_bad_extraction else ""
        print(f"  FAIL{reason}: {_trunc(msg, 200)}")
        failed += 1

    # --- Summary ---
    print("\n" + "=" * 70)
    print(f"Result: {passed} passed, {failed} failed")
    if failed > 0 and not healthcare_url:
        print("\nNote: Set CHAT_SKILLS_HEALTHCARE_URL and restart mobius-skills-mcp for healthcare_query.")
    print("=" * 70)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
