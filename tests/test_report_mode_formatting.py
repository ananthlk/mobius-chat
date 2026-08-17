"""Task #106 (2026-08-16, Chat Master ruling): report-mode formatting,
mode-gated -- chat.thinking gets full structured-report scaffolding
(## headers per sub-question/entity, tables for comparisons, no
duplication between direct_answer and sections), other modes get a
lighter anti-duplication-only variant."""
from __future__ import annotations

from app.responder.final_parallel import (
    _REPORT_MODE_INSTRUCTIONS_CHAT_THINKING,
    _REPORT_MODE_INSTRUCTIONS_DEFAULT,
    _report_mode_instructions,
)


def test_agentic_gets_full_report_scaffolding():
    out = _report_mode_instructions("agentic")
    assert out == _REPORT_MODE_INSTRUCTIONS_CHAT_THINKING
    assert "##" in out
    assert "table" in out.lower()
    assert "exactly ONCE" in out


def test_copilot_gets_lighter_variant():
    out = _report_mode_instructions("copilot")
    assert out == _REPORT_MODE_INSTRUCTIONS_DEFAULT
    assert out != _REPORT_MODE_INSTRUCTIONS_CHAT_THINKING


def test_quick_gets_lighter_variant():
    assert _report_mode_instructions("quick") == _REPORT_MODE_INSTRUCTIONS_DEFAULT


def test_task_gets_lighter_variant():
    assert _report_mode_instructions("task") == _REPORT_MODE_INSTRUCTIONS_DEFAULT


def test_none_gets_lighter_variant():
    assert _report_mode_instructions(None) == _REPORT_MODE_INSTRUCTIONS_DEFAULT


def test_both_variants_state_anti_duplication_rule():
    # The one rule that must hold in EVERY mode, not just chat.thinking.
    assert "exactly once" in _REPORT_MODE_INSTRUCTIONS_DEFAULT.lower()
    assert "exactly once" in _REPORT_MODE_INSTRUCTIONS_CHAT_THINKING.lower()
