"""Day 1 gate: Search and capability questions route to tool.

V1 plan: test_agent_routing.py — "Search for X" and "What can you do?" route to tool.
Uses same routing logic as pipeline (detect_route from route_triggers).
"""
import pytest

from app.planner.route_triggers import detect_route


def test_search_for_x_routes_to_tool():
    """'Search for X' must route to tool (web/search)."""
    agent, conf, _ = detect_route("Search for Florida Medicaid eligibility requirements")
    assert agent == "tool", f"Expected tool, got {agent}"
    assert conf >= 1.0


def test_what_can_you_do_routes_to_tool():
    """'What can you do?' must route to tool (capability question)."""
    agent, conf, _ = detect_route("What can you do?")
    assert agent == "tool", f"Expected tool, got {agent}"
    assert conf >= 1.0
