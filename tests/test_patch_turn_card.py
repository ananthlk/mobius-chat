"""Tests for PersistencePort.patch_turn_card (Task #76, 2026-08-08):
Postgres does a single-statement jsonb merge (no read-modify-write race);
memory/default backends no-op. Used by the dynamic-enrichment background
critic/enrichment done-callbacks to update an already-persisted card.
"""
from __future__ import annotations

import json
from unittest.mock import patch


class TestPatchTurnCardPostgres:
    def test_calls_db_execute_with_jsonb_merge(self):
        from app.persistence.postgres import PostgresPersistence

        p = PostgresPersistence()
        with patch("app.persistence.postgres.db_execute") as mock_exec:
            mock_exec.return_value = {}
            p.patch_turn_card("cid-123", {"citations": [{"claim": "x"}]})

        assert mock_exec.called
        sql = mock_exec.call_args.args[0]
        assert "jsonb" in sql.lower()
        assert "chat_turns" in sql
        params = mock_exec.call_args.kwargs["params"]
        assert params["cid"] == "cid-123"
        assert json.loads(params["patch"]) == {"citations": [{"claim": "x"}]}

    def test_empty_patch_is_a_noop(self):
        from app.persistence.postgres import PostgresPersistence

        p = PostgresPersistence()
        with patch("app.persistence.postgres.db_execute") as mock_exec:
            p.patch_turn_card("cid-123", {})

        mock_exec.assert_not_called()

    def test_db_error_does_not_raise(self):
        """Best-effort: a failed patch must not propagate -- this runs
        inside a ThreadPoolExecutor done-callback, which the caller isn't
        watching for exceptions."""
        from app.persistence.postgres import PostgresPersistence

        p = PostgresPersistence()
        with patch("app.persistence.postgres.db_execute") as mock_exec:
            mock_exec.return_value = {"error": {"message": "connection lost"}}
            p.patch_turn_card("cid-123", {"gaps": ["x"]})  # must not raise

    def test_exception_from_db_execute_does_not_raise(self):
        from app.persistence.postgres import PostgresPersistence

        p = PostgresPersistence()
        with patch("app.persistence.postgres.db_execute", side_effect=RuntimeError("boom")):
            p.patch_turn_card("cid-123", {"gaps": ["x"]})  # must not raise


class TestPatchTurnCardMemory:
    def test_memory_backend_noop_does_not_raise(self):
        from app.persistence.memory import MemoryPersistence

        p = MemoryPersistence()
        p.patch_turn_card("cid-123", {"citations": []})  # must not raise
