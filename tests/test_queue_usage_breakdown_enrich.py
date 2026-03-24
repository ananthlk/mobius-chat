"""patch_response_merge: usage_breakdown_enrich merges per-call QA into stored rows."""
from __future__ import annotations

from app.queue.memory import MemoryQueue


def test_usage_breakdown_enrich_then_append_order() -> None:
    q = MemoryQueue()
    cid = "test-corr-1"
    q.publish_response(
        cid,
        {
            "status": "completed",
            "usage_breakdown": [
                {
                    "stage": "integrator",
                    "llm_call_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                    "model": "m1",
                },
            ],
        },
    )
    q.patch_response_merge(
        cid,
        {
            "usage_breakdown_enrich": {
                "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa": {
                    "quality_score": 0.812,
                    "quality_source": "post_run_adjudicator_v2",
                },
            },
            "usage_breakdown_append": [
                {
                    "stage": "adjudicator",
                    "llm_call_id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
                    "model": "judge",
                    "quality_score": 0.9,
                    "quality_source": "post_run_adjudicator_v2",
                },
            ],
        },
    )
    out = q.get_response(cid)
    assert out is not None
    rows = out.get("usage_breakdown") or []
    assert len(rows) == 2
    assert rows[0].get("quality_score") == 0.812
    assert rows[0].get("quality_source") == "post_run_adjudicator_v2"
    assert rows[1].get("stage") == "adjudicator"
    assert rows[1].get("quality_score") == 0.9
