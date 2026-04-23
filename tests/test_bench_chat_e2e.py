"""Unit tests for the E2E benchmark runner.

We only test the parts that don't need a live server: metric extraction,
summary math, question loading. The HTTP layer is exercised in actual
benchmark runs, not in pytest.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# scripts/ isn't a package; load the module by path.
_HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_HERE / "scripts"))
import bench_chat_e2e as bench  # noqa: E402


# ── Metric extraction ─────────────────────────────────────────────────


def test_extract_metrics_populates_core_fields():
    turn = bench.TurnResult(question_id="t1", question="q?")
    payload = {
        "status": "completed",
        "message": "The timeline is 7 days.",
        "retrieval_signals": ["corpus_only"],
        "sources": [
            {"document_name": "Sunshine Manual", "source_type": "internal"},
            {"document_name": "Policy Doc", "source_type": "internal"},
        ],
        "thinking_log": [
            "◌ Searching our materials…",
            "Found 3 chunks",
            {
                "signal": "turn_completed",
                "data": {
                    "rounds_used": 2,
                    "tools_used": ["search_corpus"],
                    "total_llm_tokens": 4500,
                    "total_cost_usd": 0.012,
                },
            },
        ],
    }
    bench._extract_metrics_from_completed(turn, payload)

    assert turn.status == "completed"
    assert turn.final_message.startswith("The timeline")
    assert turn.retrieval_signals == ["corpus_only"]
    assert turn.sources_count == 2
    assert "Sunshine Manual" in turn.sources_sample
    assert turn.rounds_used == 2
    assert turn.tools_used == ["search_corpus"]
    assert turn.total_llm_tokens == 4500
    assert turn.total_cost_usd == pytest.approx(0.012)


def test_extract_metrics_handles_missing_turn_completed_envelope():
    """Some worker paths never emit turn_completed (legacy, early failures).
    Must not crash; just leave those fields None."""
    turn = bench.TurnResult(question_id="t2", question="q?")
    payload = {
        "status": "completed",
        "message": "Short answer.",
        "retrieval_signals": [],
        "sources": [],
        "thinking_log": ["◌ Thinking…", "I don't know."],
    }
    bench._extract_metrics_from_completed(turn, payload)
    assert turn.rounds_used is None
    assert turn.tools_used == []
    assert turn.total_llm_tokens is None
    assert turn.sources_count == 0


def test_extract_metrics_detects_system_context_short_circuit():
    """answered_from_system_context=True in payload should surface on turn."""
    turn = bench.TurnResult(question_id="t3", question="q?")
    payload = {
        "status": "completed",
        "message": "From cached context.",
        "answered_from_system_context": True,
        "retrieval_signals": ["system_context"],
        "sources": [],
        "thinking_log": [],
    }
    bench._extract_metrics_from_completed(turn, payload)
    assert turn.answered_from_system_context is True
    assert turn.retrieval_signals == ["system_context"]


def test_extract_metrics_truncates_long_final_message():
    """400-char truncation keeps JSON reports manageable."""
    turn = bench.TurnResult(question_id="t4", question="q?")
    payload = {"status": "completed", "message": "X" * 1000, "thinking_log": []}
    bench._extract_metrics_from_completed(turn, payload)
    assert len(turn.final_message) == 400


# ── Summary math ──────────────────────────────────────────────────────


def _mk(id_: str, status: str = "completed", duration_ms: int = 1000, **kw) -> bench.TurnResult:
    return bench.TurnResult(
        question_id=id_, question="q?", status=status, duration_ms=duration_ms, **kw
    )


def test_summarize_basic_counts_and_percentiles():
    turns = [
        _mk("a", duration_ms=1000, first_chunk_ms=100, retrieval_signals=["corpus_only"], rounds_used=2, total_llm_tokens=1000, sources_count=3),
        _mk("b", duration_ms=2000, first_chunk_ms=150, retrieval_signals=["corpus_only"], rounds_used=3, total_llm_tokens=2000, sources_count=5),
        _mk("c", duration_ms=3000, first_chunk_ms=200, retrieval_signals=["google_only"], rounds_used=3, total_llm_tokens=3000, sources_count=2),
        _mk("d", status="timeout", duration_ms=0),
    ]
    s = bench._summarize(turns)
    assert s["n_total"] == 4
    assert s["n_completed"] == 3
    assert s["n_failed"] == 1
    # Percentile helper uses floor-index; with 3 items p50 is middle.
    assert s["duration_ms_p50"] in (2000, 3000)  # tolerate indexing semantics
    assert s["first_chunk_ms_p50"] in (150, 200)
    assert s["retrieval_signal_counts"] == {"corpus_only": 2, "google_only": 1}
    assert s["rounds_used_avg"] == pytest.approx((2 + 3 + 3) / 3)
    assert s["zero_sources_count"] == 0


def test_summarize_handles_all_failed():
    turns = [
        _mk("a", status="error", duration_ms=0),
        _mk("b", status="timeout", duration_ms=0),
    ]
    s = bench._summarize(turns)
    assert s["n_completed"] == 0
    assert s["n_failed"] == 2
    assert s["duration_ms_p50"] is None
    assert s["first_chunk_ms_p50"] is None
    assert s["retrieval_signal_counts"] == {}
    assert s["rounds_used_avg"] is None


def test_summarize_counts_zero_sources_turns():
    turns = [
        _mk("a", sources_count=0, retrieval_signals=["no_sources"]),
        _mk("b", sources_count=3, retrieval_signals=["corpus_only"]),
        _mk("c", sources_count=0, retrieval_signals=["no_sources"]),
    ]
    s = bench._summarize(turns)
    assert s["zero_sources_count"] == 2


# ── Question loader ───────────────────────────────────────────────────


def test_load_questions_returns_fallback_when_path_missing():
    qs = bench._load_questions("/nonexistent/path/does_not_exist.yaml")
    assert qs is bench.FALLBACK_QUESTIONS
    assert len(qs) >= 5
    # Structure sanity
    for q in qs:
        assert "id" in q and "question" in q
        assert q["question"]


def test_load_questions_parses_yaml(tmp_path):
    p = tmp_path / "q.yaml"
    p.write_text(
        "questions:\n"
        "  - id: x_001\n"
        "    question: First question?\n"
        "  - id: x_002\n"
        "    question: Second question?\n"
    )
    qs = bench._load_questions(str(p))
    assert len(qs) == 2
    assert qs[0]["id"] == "x_001"
    assert qs[1]["question"] == "Second question?"


def test_load_questions_falls_back_on_empty_yaml(tmp_path):
    p = tmp_path / "empty.yaml"
    p.write_text("questions: []\n")
    qs = bench._load_questions(str(p))
    # Empty list → fallback so the run still has something to execute.
    assert qs is bench.FALLBACK_QUESTIONS
