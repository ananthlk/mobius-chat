"""Task #103 (2026-08-16, Chat Master/Ananth ruling): RAG call-per-turn
ceiling raised from 3 to 6 for chat.thinking (agentic) turns -- Retriever's
chat.thinking retrieval-budget increase (#97/#102's trigger) means a
chat.thinking turn now has real room to profitably use more than 3 corpus
calls before the corpus is actually exhausted, not just the old allowance."""
from __future__ import annotations

from app.pipeline.react_loop import _rag_call_ceiling_for_mode


def test_agentic_gets_raised_ceiling():
    assert _rag_call_ceiling_for_mode("agentic") == 6


def test_copilot_stays_at_default():
    assert _rag_call_ceiling_for_mode("copilot") == 3


def test_quick_stays_at_default():
    assert _rag_call_ceiling_for_mode("quick") == 3


def test_task_stays_at_default():
    assert _rag_call_ceiling_for_mode("task") == 3


def test_none_stays_at_default():
    assert _rag_call_ceiling_for_mode(None) == 3
