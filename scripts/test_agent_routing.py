#!/usr/bin/env python3
"""Day 2 gate: Search and capability questions route to tool agent.

Verifies deterministic route triggers and blueprint agent assignment.
Run: PYTHONPATH=mobius-chat python mobius-chat/scripts/test_agent_routing.py
"""
from __future__ import annotations

import sys
from pathlib import Path

CHAT_ROOT = Path(__file__).resolve().parent.parent
if str(CHAT_ROOT) not in sys.path:
    sys.path.insert(0, str(CHAT_ROOT))


def test_search_routes_to_tool():
    """'Search for X' → tool."""
    from app.planner.route_triggers import detect_route

    agent, conf, _ = detect_route("Search for Florida Medicaid eligibility")
    assert agent == "tool", f"Expected tool, got {agent}"
    assert conf >= 1.0, f"Expected conf>=1.0, got {conf}"
    print("[OK] Search for X → tool")


def test_capability_routes_to_tool():
    """'What can you do?' → tool."""
    from app.planner.route_triggers import detect_route

    agent, conf, _ = detect_route("What can you do?")
    assert agent == "tool", f"Expected tool, got {agent}"
    assert conf >= 1.0, f"Expected conf>=1.0, got {conf}"
    print("[OK] What can you do? → tool")


def test_scrape_routes_to_tool():
    """'Scrape https://...' → tool."""
    from app.planner.route_triggers import detect_route

    agent, conf, _ = detect_route("Can you scrape https://example.com/page?")
    assert agent == "tool", f"Expected tool, got {agent}"
    assert conf >= 1.0, f"Expected conf>=1.0, got {conf}"
    print("[OK] Scrape URL → tool")


def test_blueprint_search_agent_tool():
    """Blueprint: search question → agent=tool."""
    from app.planner.blueprint import build_blueprint
    from app.planner.schemas import Plan, SubQuestion

    plan = Plan(subquestions=[
        SubQuestion(id="sq1", text="Search for Florida Medicaid eligibility", kind="non_patient"),
    ])
    blueprint = build_blueprint(plan, rag_default_k=10, retrieval_ctx={"user_message": "Search for Florida Medicaid eligibility"})
    assert blueprint[0]["agent"] == "tool", f"Expected agent=tool, got {blueprint[0]['agent']}"
    print("[OK] Blueprint: search → agent=tool")


def test_blueprint_capability_agent_tool():
    """Blueprint: capability question → agent=tool."""
    from app.planner.blueprint import build_blueprint
    from app.planner.schemas import Plan, SubQuestion

    plan = Plan(subquestions=[
        SubQuestion(id="sq1", text="What can you do?", kind="non_patient"),
    ])
    blueprint = build_blueprint(plan, rag_default_k=10, retrieval_ctx={"user_message": "What can you do?"})
    assert blueprint[0]["agent"] == "tool", f"Expected agent=tool, got {blueprint[0]['agent']}"
    print("[OK] Blueprint: what can you do → agent=tool")


def main():
    tests = [
        test_search_routes_to_tool,
        test_capability_routes_to_tool,
        test_scrape_routes_to_tool,
        test_blueprint_search_agent_tool,
        test_blueprint_capability_agent_tool,
    ]
    failed = 0
    for t in tests:
        try:
            t()
        except AssertionError as e:
            print(f"[FAIL] {t.__name__}: {e}")
            failed += 1
    if failed:
        print(f"\n{failed}/{len(tests)} failed")
        sys.exit(1)
    print(f"\nAll {len(tests)} agent routing tests passed.")


if __name__ == "__main__":
    main()
