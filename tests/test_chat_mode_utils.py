"""Task (Chat Master 2026-08-15): caller_mode vocabulary translation.

react_loop.py was sending raw chat_mode ("agentic"/"copilot"/"quick"/"task")
straight through as RAG's caller_mode, which only recognizes the chat.*
preset keys -- every dispatch silently fell through to chat.default. This
is the shared translation helper both sides agreed should exist.
"""
from __future__ import annotations

from app.services.chat_mode_utils import (
    DEFAULT_CALLER_MODE,
    translate_chat_mode_to_caller_mode,
)


def test_agentic_maps_to_chat_thinking():
    assert translate_chat_mode_to_caller_mode("agentic") == "chat.thinking"


def test_copilot_maps_to_chat_copilot():
    assert translate_chat_mode_to_caller_mode("copilot") == "chat.copilot"


def test_quick_maps_to_chat_default():
    assert translate_chat_mode_to_caller_mode("quick") == "chat.default"


def test_task_maps_to_chat_default():
    assert translate_chat_mode_to_caller_mode("task") == "chat.default"


def test_none_falls_back_to_default():
    assert translate_chat_mode_to_caller_mode(None) == DEFAULT_CALLER_MODE


def test_unknown_string_falls_back_to_default():
    assert translate_chat_mode_to_caller_mode("bogus_mode") == DEFAULT_CALLER_MODE


def test_case_and_whitespace_insensitive():
    assert translate_chat_mode_to_caller_mode(" Agentic ") == "chat.thinking"
