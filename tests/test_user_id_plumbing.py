"""Phase 2d completion — user_id plumbing from POST /chat to chat_turns.

Phase 2d (c3f7327) added ``require_user`` to 10 write endpoints but the
returned user_id was named ``_user_id`` and discarded. This file locks
the end-to-end plumbing that stamps the authenticated user_id onto
chat_turns rows:

  POST /chat (require_user) → payload["user_id"] → queue →
  worker.process_one → run_pipeline(user_id=…) → ctx.user_id →
  persistence.save_turn(user_id=…) → insert_turn(user_id=…) →
  INSERT INTO chat_turns (..., user_id) VALUES (..., %s)

What we test:

  1. **POST /chat includes user_id in the payload** when require_user
     returns a user_id; omits the key entirely when user_id is None
     (so older worker binaries without the new signature still
     deserialize the payload).

  2. **Worker extracts user_id from payload** and forwards to
     run_pipeline. Defensive: non-string values become None.

  3. **run_pipeline stores user_id on ctx.user_id** — the link to
     save_turn call sites.

  4. **persistence.save_turn forwards user_id to insert_turn**
     (both the in-memory and Postgres backends).

  5. **insert_turn has graceful-fallback** when the user_id column
     doesn't exist in the DB (migration not yet run). Turns still
     persist, user_id column write is silently dropped — same pattern
     as context_summary.

Not tested here (intentional scope):

  - Actual Postgres column-level test. That needs a live DB fixture;
    the graceful-fallback is stubbed via the existing retry-without-
    new-column path and verified via SQL string inspection.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


# ── POST /chat forwards user_id into the payload ─────────────────────


class TestPostChatForwardsUserId:
    def _mount(self, monkeypatch, auth_mode: str = "off") -> TestClient:
        from app.api.chat import router

        monkeypatch.setenv("CHAT_AUTH_MODE", auth_mode)
        app = FastAPI()
        app.include_router(router)
        return TestClient(app)

    def test_user_id_in_payload_when_auth_returns_one(self, monkeypatch):
        """When require_user yields a user_id, POST /chat must
        include it in the queue payload under the 'user_id' key.
        Without this, the worker has no way to know who made the
        request."""
        captured: dict = {}

        def fake_publish(cid, payload):
            captured["payload"] = payload

        fake_queue = MagicMock()
        fake_queue.publish_request.side_effect = fake_publish
        client = self._mount(monkeypatch)

        # Mock require_user to return a specific user_id
        with patch("app.api.chat.require_user", return_value="user-42") as _, \
             patch("app.api.chat.get_queue", return_value=fake_queue), \
             patch("app.api.chat.ensure_thread", return_value="thread-xyz"):
            # Need to remount with the patched dependency — FastAPI
            # caches the resolved dependency. Use the actual fixture
            # that sets auth=off, which yields None from require_user.
            # Instead we'll check the "None omitted" contract in the
            # next test and test the "present" contract via auth=
            # off gives None, then we bypass.
            pass

        # Alternative path: directly exercise the post_chat function.
        # This proves the payload contract without FastAPI dependency
        # caching getting in the way.
        from app.api.chat import post_chat, ChatRequest

        body = ChatRequest(message="hi")
        captured.clear()
        with patch("app.api.chat.get_queue", return_value=fake_queue), \
             patch("app.api.chat.ensure_thread", return_value="t"):
            post_chat(body, user_id="user-42")
        assert captured["payload"]["user_id"] == "user-42"
        assert captured["payload"]["message"] == "hi"
        assert captured["payload"]["thread_id"] == "t"

    def test_user_id_absent_from_payload_when_none(self, monkeypatch):
        """When auth is disabled (CHAT_AUTH_MODE=off) require_user
        returns None. The 'user_id' key must NOT be in the payload —
        a null would force every downstream to check-and-skip. Older
        worker binaries that haven't picked up the new signature
        should still deserialize cleanly."""
        captured: dict = {}

        def fake_publish(cid, payload):
            captured["payload"] = payload

        fake_queue = MagicMock()
        fake_queue.publish_request.side_effect = fake_publish

        from app.api.chat import post_chat, ChatRequest

        body = ChatRequest(message="hi")
        with patch("app.api.chat.get_queue", return_value=fake_queue), \
             patch("app.api.chat.ensure_thread", return_value="t"):
            post_chat(body, user_id=None)
        assert "user_id" not in captured["payload"]


# ── Worker extracts user_id and forwards ────────────────────────────


class TestWorkerForwardsUserId:
    def test_process_one_extracts_and_forwards(self):
        """Worker's process_one must pull 'user_id' out of the queue
        payload and pass it to run_pipeline. Without this, even though
        POST /chat put the value in the queue, it stops here and
        chat_turns stays unattributed."""
        from app.worker.run import process_one

        with patch("app.pipeline.orchestrator.run_pipeline") as mock_rp:
            process_one("cid-1", {
                "message": "hi",
                "thread_id": "t-1",
                "user_id": "user-42",
            })
        assert mock_rp.called
        kwargs = mock_rp.call_args.kwargs
        assert kwargs["user_id"] == "user-42"

    def test_process_one_handles_missing_user_id(self):
        """Backward compat: payloads without user_id (from older POST
        /chat code OR dev auth-off) pass None through. Worker must not
        KeyError."""
        from app.worker.run import process_one

        with patch("app.pipeline.orchestrator.run_pipeline") as mock_rp:
            process_one("cid-1", {"message": "hi", "thread_id": "t-1"})
        assert mock_rp.call_args.kwargs["user_id"] is None

    def test_process_one_rejects_non_string_user_id(self):
        """Defensive: if something corrupts the payload and puts a
        non-string in user_id (e.g. a dict from an old credentialing
        ticket), the worker must downgrade to None rather than
        propagate the garbage."""
        from app.worker.run import process_one

        with patch("app.pipeline.orchestrator.run_pipeline") as mock_rp:
            process_one("cid-1", {
                "message": "hi",
                "thread_id": "t-1",
                "user_id": {"not": "a string"},
            })
        assert mock_rp.call_args.kwargs["user_id"] is None


# ── PipelineContext carries user_id ─────────────────────────────────


class TestContextCarriesUserId:
    def test_context_default_is_none(self):
        from app.pipeline.context import PipelineContext

        ctx = PipelineContext(correlation_id="c", thread_id=None, message="m")
        assert ctx.user_id is None

    def test_context_accepts_user_id(self):
        from app.pipeline.context import PipelineContext

        ctx = PipelineContext(
            correlation_id="c",
            thread_id=None,
            message="m",
            user_id="user-42",
        )
        assert ctx.user_id == "user-42"


# ── persistence.save_turn forwards user_id ──────────────────────────


class TestPersistenceForwardsUserId:
    def test_postgres_save_turn_forwards_to_insert(self):
        """Postgres backend must pass user_id to insert_turn. Otherwise
        the column never gets written even when the DB has it."""
        from app.persistence.postgres import PostgresPersistence

        p = PostgresPersistence()
        with patch("app.persistence.postgres.insert_turn") as mock_insert:
            p.save_turn(
                correlation_id="c",
                question="q",
                thinking_log=[],
                final_message="a",
                sources=[],
                duration_ms=100,
                model_used=None,
                llm_provider=None,
                user_id="user-42",
            )
        assert mock_insert.called
        assert mock_insert.call_args.kwargs["user_id"] == "user-42"

    def test_memory_save_turn_accepts_user_id(self):
        """In-memory backend doesn't persist user_id (by design — it's
        ephemeral) but MUST accept the kwarg to stay signature-
        compatible with Postgres. Otherwise save_turn calls from the
        orchestrator would raise TypeError in-memory mode."""
        from app.persistence.memory import MemoryPersistence

        p = MemoryPersistence()
        # Should not raise
        p.save_turn(
            correlation_id="c",
            question="q",
            thinking_log=[],
            final_message="a",
            sources=[],
            duration_ms=100,
            model_used=None,
            llm_provider=None,
            thread_id="t",
            user_id="user-42",
        )

    def test_save_turn_with_messages_forwards_user_id(self):
        """The atomic path used when a thread_id is present. Must also
        forward user_id or threaded requests would lose attribution
        while unthreaded ones kept it."""
        from app.persistence.postgres import PostgresPersistence

        p = PostgresPersistence()
        with patch("app.persistence.postgres._atomic_save_turn_with_messages") as mock_atomic:
            p.save_turn_with_messages(
                correlation_id="c",
                question="q",
                thinking_log=[],
                final_message="a",
                sources=[],
                duration_ms=100,
                model_used=None,
                llm_provider=None,
                thread_id="t",
                user_content="q",
                assistant_content="a",
                user_id="user-42",
            )
        # _atomic_save_turn_with_messages takes user_id as the last
        # positional arg (see signature in postgres.py).
        args = mock_atomic.call_args.args
        # user_id is the 15th positional arg (index 14) after the
        # required 14 fields.
        assert args[14] == "user-42"


# ── insert_turn graceful fallback for missing column ────────────────


class TestInsertTurnGracefulFallback:
    """Locks the two-path DB write. When the user_id column exists,
    the primary INSERT runs and carries user_id. When it doesn't
    exist (migration not yet run), psycopg2 raises 'column does not
    exist' and the retry-without-column path runs so the turn still
    persists. Operators can run the migration at their convenience."""

    def test_sql_contains_user_id_column(self):
        """Positive assertion that the primary INSERT path writes
        user_id. If someone accidentally removes it, the column
        silently stops filling even after migration."""
        from pathlib import Path

        src = Path("app/storage/turns.py").read_text()
        # Primary path:
        assert "user_id" in src
        # Column list in the INSERT contains user_id:
        assert "source_confidence_strip, config_sha,\n                context_summary, user_id" in src

    def test_fallback_path_triggers_on_user_id_column_missing(self):
        """The retry branch must fire when the error message contains
        'user_id'. Otherwise missing-column errors in hosted would
        crash the whole worker."""
        from pathlib import Path

        src = Path("app/storage/turns.py").read_text()
        # The err_str check must include 'user_id':
        assert '"user_id" in err_str' in src

    def test_on_conflict_preserves_existing_user_id(self):
        """COALESCE on the UPSERT means an existing user_id never gets
        overwritten by a NULL (which would happen if a background job
        updated the turn with no user_id). Stable audit trail."""
        from pathlib import Path

        src = Path("app/storage/turns.py").read_text()
        assert "user_id = COALESCE(EXCLUDED.user_id, chat_turns.user_id)" in src
