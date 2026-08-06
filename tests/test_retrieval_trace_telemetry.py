"""Task: make_retrieval_trace telemetry-shape bug (2026-08-06, Chat Master).

Post-RAG-endpoint-cutover, the new pipeline's reduced telemetry dict
({chosen_slot, score, status, attempt_count, latency_ms, dispatch_path,
allocator_override, authority_requirement, n_chunks}) has no
arm_hits/arms/chunks -- the old fallback chain for `returned` always
evaluated to 0, so every new-pipeline turn rendered "BM25=0 vec=0 --
no matches" regardless of how many chunks were actually found.
"""
from __future__ import annotations

from app.communication.emit_envelope import make_retrieval_trace

_NEW_PIPELINE_TELEMETRY = {
    "chosen_slot": "b",
    "score": 0.82,
    "status": "ok",
    "attempt_count": 1,
    "latency_ms": 340,
    "dispatch_path": "direct",
    "allocator_override": None,
    "authority_requirement": None,
    "n_chunks": 19,
}


def _make(telemetry: dict) -> str:
    env = make_retrieval_trace(
        "cid", search_id="s1", query="q", mode="auto", k=10, telemetry=telemetry,
    )
    return env.note or ""


def test_new_pipeline_uses_n_chunks_for_returned_count():
    note = _make(_NEW_PIPELINE_TELEMETRY)
    assert "19 chunks" in note
    assert "no matches" not in note


def test_new_pipeline_omits_bm25_vec_when_arm_data_absent():
    """The core bug: BM25=0 vec=0 must not appear when arm_hits/arms
    were never in the telemetry dict at all -- that's a real capability
    gap in the new pipeline, not "zero hits found"."""
    note = _make(_NEW_PIPELINE_TELEMETRY)
    assert "BM25" not in note
    assert "vec=" not in note


def test_new_pipeline_zero_chunks_still_reports_no_matches_without_bm25():
    telemetry = {**_NEW_PIPELINE_TELEMETRY, "n_chunks": 0, "total_ms": 50}
    note = _make(telemetry)
    assert "no matches" in note
    assert "BM25" not in note


def test_old_pipeline_shape_still_computes_bm25_vec():
    """Regression guard: legacy telemetry (arm_hits present) must keep
    showing real BM25/vec numbers -- this fix must not blind old-shape
    turns to data they actually have."""
    telemetry = {
        "arm_hits": {"bm25": 12, "vector": 7},
        "chunks": [{"id": i} for i in range(9)],
        "total_ms": 220,
    }
    note = _make(telemetry)
    assert "BM25=12" in note
    assert "vec=7" in note
    assert "9 chunk" in note


def test_old_pipeline_zero_hits_still_shows_bm25_vec_as_zero():
    """A genuinely old-shape turn with zero hits (arm_hits present but
    empty/zero-valued) should still show BM25=0 vec=0 -- that IS real
    data for that shape, distinct from the new pipeline's absence."""
    telemetry = {"arm_hits": {"bm25": 0, "vector": 0}, "chunks": [], "total_ms": 40}
    note = _make(telemetry)
    assert "BM25=0 vec=0" in note
    assert "no matches" in note


def test_legacy_arms_returned_field_still_works():
    telemetry = {"arms": {"returned": 5, "bm25_hits": 3, "vec_hits": 2}, "timing": {"total_ms": 100}}
    note = _make(telemetry)
    assert "5 chunk" in note
    assert "BM25=3" in note
    assert "vec=2" in note


def test_fail_fast_path_unaffected():
    telemetry = {
        "assembler": {"strategy_used": "e"},
        "gate": {"fail_fast_reason": "no_domain_match"},
        "total_ms": 5,
    }
    note = _make(telemetry)
    assert "Fail-fast" in note
