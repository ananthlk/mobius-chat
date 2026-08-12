"""get_prior_resolved_entities (2026-08-12, Chat Master directive, Task #90).

Multi-turn react_draft loading via the gaps_closed index. Both react_draft
and reasoning_trace[].gaps_closed are ALREADY durably persisted per-turn in
chat_turns.final_message (confirmed live, cid 997193e2's follow-up
verification) -- this is a read-only lookup, no new persistence. Reaches
further back (8 turns) than last_turns/last_turn_sources' ~3-turn window,
for a targeted pickup on longer multi-entity comparison threads.
"""
from __future__ import annotations

import json
from unittest.mock import patch

from app.storage.turns import get_prior_resolved_entities


def _db_result(rows: list[tuple]) -> dict:
    return {"columns": ["react_draft", "reasoning_trace"], "rows": rows}


def _rt(entries: list[dict]) -> str:
    """JSON-encode a reasoning_trace list, matching what final_message::
    jsonb->'reasoning_trace' returns (a decoded list, per _decode_jsonb)."""
    return entries  # db_query already decodes jsonb columns to Python objects


class TestGetPriorResolvedEntities:
    def test_empty_thread_id_returns_empty_no_query(self):
        with patch("app.storage.turns.db_query") as mock_query:
            result = get_prior_resolved_entities("")
        assert result == []
        mock_query.assert_not_called()

    def test_extracts_gaps_closed_paired_with_that_turns_react_draft(self):
        rows = [(
            "Sunshine: 180 days. Aetna: 180 days. Molina: 180 days.",
            _rt([
                {"round": 1, "tool": "rag"},
                {"round": 2, "tool": "rag", "gaps_open": ["Molina's timely filing deadlines"]},
                {"round": 3, "tool": None, "gaps_closed": ["Molina's timely filing deadlines"]},
            ]),
        )]
        with patch("app.storage.turns.db_query", return_value=_db_result(rows)):
            result = get_prior_resolved_entities("t1")

        assert len(result) == 1
        assert result[0]["gap_text"] == "Molina's timely filing deadlines"
        assert result[0]["react_draft"] == "Sunshine: 180 days. Aetna: 180 days. Molina: 180 days."
        assert result[0]["turn_index"] == 0

    def test_multiple_gaps_closed_in_one_round_all_extracted(self):
        rows = [(
            "draft text",
            _rt([{"round": 1, "gaps_closed": ["Aetna deadline", "Molina deadline"]}]),
        )]
        with patch("app.storage.turns.db_query", return_value=_db_result(rows)):
            result = get_prior_resolved_entities("t1")
        assert {r["gap_text"] for r in result} == {"Aetna deadline", "Molina deadline"}

    def test_turn_index_increases_with_age_most_recent_first(self):
        rows = [
            ("recent draft", _rt([{"round": 1, "gaps_closed": ["recent gap"]}])),
            ("older draft", _rt([{"round": 1, "gaps_closed": ["older gap"]}])),
        ]
        with patch("app.storage.turns.db_query", return_value=_db_result(rows)):
            result = get_prior_resolved_entities("t1")
        by_gap = {r["gap_text"]: r["turn_index"] for r in result}
        assert by_gap["recent gap"] == 0
        assert by_gap["older gap"] == 1

    def test_not_deduped_across_turns(self):
        """A more recent resolution of the same entity must remain visible
        alongside an older one, not silently collapsed."""
        rows = [
            ("corrected: 90 days", _rt([{"round": 1, "gaps_closed": ["Molina deadline"]}])),
            ("original: 180 days", _rt([{"round": 1, "gaps_closed": ["Molina deadline"]}])),
        ]
        with patch("app.storage.turns.db_query", return_value=_db_result(rows)):
            result = get_prior_resolved_entities("t1")
        assert len(result) == 2
        assert result[0]["react_draft"] == "corrected: 90 days"
        assert result[1]["react_draft"] == "original: 180 days"

    def test_turn_with_no_gaps_closed_contributes_nothing(self):
        rows = [("draft with no resolved gaps", _rt([{"round": 1, "tool": "rag"}]))]
        with patch("app.storage.turns.db_query", return_value=_db_result(rows)):
            result = get_prior_resolved_entities("t1")
        assert result == []

    def test_turn_with_empty_react_draft_skipped(self):
        rows = [("", _rt([{"round": 1, "gaps_closed": ["some gap"]}]))]
        with patch("app.storage.turns.db_query", return_value=_db_result(rows)):
            result = get_prior_resolved_entities("t1")
        assert result == []

    def test_malformed_reasoning_trace_does_not_crash(self):
        rows = [("draft", "not a list at all")]
        with patch("app.storage.turns.db_query", return_value=_db_result(rows)):
            result = get_prior_resolved_entities("t1")
        assert result == []

    def test_db_error_returns_empty_not_raises(self):
        with patch("app.storage.turns.db_query", return_value={"error": {"code": "connection_error", "message": "down"}}):
            result = get_prior_resolved_entities("t1")
        assert result == []

    def test_limit_turns_capped_at_20(self):
        with patch("app.storage.turns.db_query", return_value=_db_result([])) as mock_query:
            get_prior_resolved_entities("t1", limit_turns=999)
        _, kwargs = mock_query.call_args
        assert kwargs["params"]["lim"] == 20

    def test_default_limit_is_8(self):
        with patch("app.storage.turns.db_query", return_value=_db_result([])) as mock_query:
            get_prior_resolved_entities("t1")
        _, kwargs = mock_query.call_args
        assert kwargs["params"]["lim"] == 8
