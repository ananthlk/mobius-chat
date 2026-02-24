"""Pytest configuration and marker registration for mobius-chat tests.

Markers (Day 5 regression suite):
  - integration: needs external service (DB, MCP, Google); exclude with -m "not integration"
  - requires_rag: needs RAG DB (CHAT_RAG_DATABASE_URL)
  - requires_skills: needs skills/MCP (e.g. CHAT_SKILLS_GOOGLE_SEARCH_URL)

Gate: pytest mobius-chat/tests/ -v -m "not integration"
"""
from __future__ import annotations

import pytest


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "integration: tests that require external services (DB, MCP, Google). Exclude with -m 'not integration'.",
    )
    config.addinivalue_line(
        "markers",
        "requires_rag: tests that require RAG database (CHAT_RAG_DATABASE_URL).",
    )
    config.addinivalue_line(
        "markers",
        "requires_skills: tests that require skills/MCP (e.g. CHAT_SKILLS_GOOGLE_SEARCH_URL).",
    )
