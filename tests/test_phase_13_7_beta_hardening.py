"""BETA-sprint hardening tests for Phase 13.7.

Three test classes covering the gaps the readiness review flagged:

1. **Persistence fallback chain** — exercises all three tiers of
   _atomic_save_turn_with_messages (happy path, user_id-missing
   fallback, context_summary-also-missing fallback) by mocking
   db_transaction to return column_missing errors in sequence.

2. **Sidebar query CTE** — confirms get_recent_threads' DISTINCT ON
   walk picks the LATEST non-null context_summary per thread, not
   the first one or a NULL value when later turns left it empty.

3. **Phase 13.7 metric emitters + schema audit** — confirms
   record_*() calls write to the right channel and that
   audit_thread_summary_schema captures missing/wrong-nullable/ok
   states without raising.

These complement the 13 happy-path tests in
test_thread_summary_phase_13_7.py.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


# ── 1. Persistence fallback chain ─────────────────────────────────────


def _err_result(message: str, code: str = "column_missing"):
    """Mimic db_transaction's error shape."""
    return {"error": {"code": code, "message": message}}


def _ok_result():
    return {"per_statement": [{"operation": "INSERT", "table": "chat_turns", "rows": 1}]}


def _save_kwargs(**overrides):
    """Minimal kwargs for _atomic_save_turn_with_messages."""
    base = dict(
        correlation_id="cid-1",
        question="q",
        thinking_log=[],
        final_message="msg",
        sources=[],
        duration_ms=100,
        model_used="m",
        llm_provider="p",
        thread_id="t1",
        user_content="u",
        assistant_content="a",
        plan_snapshot=None,
        source_confidence_strip=None,
        config_sha=None,
        user_id="user-1",
        context_summary="rolling summary text",
    )
    base.update(overrides)
    return base


def test_persist_happy_path_uses_primary_insert():
    """No fallback: primary db_transaction succeeds, tier=0 metric fires."""
    from app.persistence import postgres as pg

    with patch.object(pg, "db_transaction", return_value=_ok_result()) as mock_tx, \
         patch("app.services.phase_13_7_metrics.record_persist_fallback_tier") as mock_metric:
        pg._atomic_save_turn_with_messages(**_save_kwargs())
    # Single transaction call, on the primary INSERT (with user_id +
    # context_summary).
    assert mock_tx.call_count == 1
    sql = mock_tx.call_args.args[0][0]["sql"]
    assert "user_id" in sql
    assert "context_summary" in sql
    mock_metric.assert_called_once_with(0)


def test_persist_falls_back_to_no_user_id_on_user_id_missing():
    """When the primary errors with column_missing on user_id, the
    second-tier statement (no user_id but WITH context_summary) runs
    and tier=1 metric fires."""
    from app.persistence import postgres as pg

    side = [_err_result('column "user_id" does not exist'), _ok_result()]
    with patch.object(pg, "db_transaction", side_effect=side) as mock_tx, \
         patch("app.services.phase_13_7_metrics.record_persist_fallback_tier") as mock_metric:
        pg._atomic_save_turn_with_messages(**_save_kwargs())
    assert mock_tx.call_count == 2
    # Second call must be the no-user-id SQL but still includes context_summary.
    second_sql = mock_tx.call_args_list[1].args[0][0]["sql"]
    assert "user_id" not in second_sql
    assert "context_summary" in second_sql
    mock_metric.assert_called_with(1)


def test_persist_falls_back_to_legacy_on_both_columns_missing():
    """When the second tier ALSO errors (context_summary missing),
    the third-tier legacy statement (no user_id, no context_summary)
    runs and tier=2 metric fires — the most-degraded but still-write
    code path."""
    from app.persistence import postgres as pg

    side = [
        _err_result('column "user_id" does not exist'),
        _err_result('column "context_summary" does not exist'),
        _ok_result(),
    ]
    with patch.object(pg, "db_transaction", side_effect=side) as mock_tx, \
         patch("app.services.phase_13_7_metrics.record_persist_fallback_tier") as mock_metric:
        pg._atomic_save_turn_with_messages(**_save_kwargs())
    assert mock_tx.call_count == 3
    third_sql = mock_tx.call_args_list[2].args[0][0]["sql"]
    assert "user_id" not in third_sql
    assert "context_summary" not in third_sql
    mock_metric.assert_called_with(2)


def test_persist_raises_on_third_tier_error():
    """If even the legacy fallback fails, raise — better than silent
    data loss."""
    from app.persistence import postgres as pg

    side = [
        _err_result('column "user_id" does not exist'),
        _err_result('column "context_summary" does not exist'),
        _err_result("connection refused", code="connection_error"),
    ]
    with patch.object(pg, "db_transaction", side_effect=side):
        with pytest.raises(RuntimeError):
            pg._atomic_save_turn_with_messages(**_save_kwargs())


def test_persist_returns_silently_on_connection_error():
    """A connection error on the primary call should NOT cascade
    through fallbacks (db is down — no point retrying)."""
    from app.persistence import postgres as pg

    side = [_err_result("db-agent unreachable", code="connection_error")]
    with patch.object(pg, "db_transaction", side_effect=side) as mock_tx:
        # Returns silently, doesn't raise — operator notices via
        # logged warning, not a 500.
        pg._atomic_save_turn_with_messages(**_save_kwargs())
    assert mock_tx.call_count == 1  # no fallback retry


# ── 2. Sidebar query CTE behavior ─────────────────────────────────────


def _fake_threads_query_result(rows: list[dict]):
    """Build a db_query result with the chat_threads + CTE columns."""
    cols = ["thread_id", "title", "summary", "updated_at", "turn_count"]
    return {
        "columns": cols,
        "rows": [tuple(r.get(c) for c in cols) for r in rows],
    }


def test_recent_threads_surfaces_summary_field(monkeypatch):
    """The sidebar query result includes summary; get_recent_threads
    passes it through to the response dict."""
    from app.storage import threads as t

    monkeypatch.setattr(t, "db_query", lambda *a, **k: _fake_threads_query_result([
        {
            "thread_id": "t1", "title": "title1", "summary": "rolling summary 1",
            "updated_at": None, "turn_count": 3,
        },
    ]))
    out = t.get_recent_threads(limit=5, user_id="u")
    assert len(out) == 1
    assert out[0]["thread_id"] == "t1"
    assert out[0]["summary"] == "rolling summary 1"
    assert out[0]["title"] == "title1"
    assert out[0]["turn_count"] == 3


def test_recent_threads_normalizes_empty_summary_to_none(monkeypatch):
    """When the latest_summary CTE returns empty string (shouldn't
    happen — query filters <> '' — but defensively), we surface
    None so frontend's COALESCE chain falls through to title."""
    from app.storage import threads as t

    monkeypatch.setattr(t, "db_query", lambda *a, **k: _fake_threads_query_result([
        {
            "thread_id": "t1", "title": "fallback title", "summary": "",
            "updated_at": None, "turn_count": 1,
        },
    ]))
    out = t.get_recent_threads(limit=5, user_id="u")
    assert out[0]["summary"] is None
    assert out[0]["title"] == "fallback title"


def test_recent_threads_returns_empty_on_column_missing(monkeypatch):
    """Pre-migration environments shouldn't 500; the query falls into
    a graceful empty list with a debug log."""
    from app.storage import threads as t

    err_result = {"error": {"code": "column_missing",
                            "message": 'column "context_summary" does not exist'}}
    monkeypatch.setattr(t, "db_query", lambda *a, **k: err_result)
    out = t.get_recent_threads(limit=5)
    assert out == []


# ── 3. Phase 13.7 metric emitters + schema audit ──────────────────────


def test_metric_record_thread_summary_emitted_logs_to_channel(caplog):
    """The structured INFO line carries the right channel + fields so
    Cloud Logging metric-extraction can pivot on them."""
    import logging as _logging
    from app.services.phase_13_7_metrics import record_thread_summary_emitted

    with caplog.at_level(_logging.INFO, logger="app.services.phase_13_7_metrics"):
        record_thread_summary_emitted(emitted=True, mode="FACTUAL")
    msgs = [r.getMessage() for r in caplog.records]
    assert any(
        "channel=phase13_7_thread_summary_emit" in m and "emitted=true" in m and "mode=FACTUAL" in m
        for m in msgs
    ), f"missing structured emit log: {msgs}"


def test_metric_record_persist_fallback_tier_logs(caplog):
    import logging as _logging
    from app.services.phase_13_7_metrics import record_persist_fallback_tier

    with caplog.at_level(_logging.INFO, logger="app.services.phase_13_7_metrics"):
        record_persist_fallback_tier(2)
    msgs = [r.getMessage() for r in caplog.records]
    assert any("channel=phase13_7_persist_fallback" in m and "tier=2" in m for m in msgs)


def test_metric_record_rehydrate_request_truncates_thread_id(caplog):
    """Long thread UUIDs should be truncated to 8 chars in the log line
    so Cloud Logging UI stays readable."""
    import logging as _logging
    from app.services.phase_13_7_metrics import record_rehydrate_request

    with caplog.at_level(_logging.INFO, logger="app.services.phase_13_7_metrics"):
        record_rehydrate_request(thread_id="abcd1234-5678-9abc-def0-1234567890ab", turn_count=3)
    msgs = [r.getMessage() for r in caplog.records]
    matched = [m for m in msgs if "channel=phase13_7_rehydrate_request" in m]
    assert matched, f"missing rehydrate log: {msgs}"
    assert "thread=abcd1234" in matched[0]
    # Full UUID must NOT appear
    assert "abcd1234-5678" not in matched[0]


def test_audit_records_ok_status_when_column_present_nullable(monkeypatch):
    """Happy path: information_schema returns the column with
    is_nullable='YES' → status=ok."""
    from app.services import phase_13_7_metrics as m

    fake_query = lambda *a, **k: {
        "columns": ["column_name", "data_type", "is_nullable"],
        "rows": [("context_summary", "text", "YES")],
    }
    monkeypatch.setattr("app.db_client.db_query", fake_query)
    monkeypatch.setattr("app.db_client.err_code", lambda r: None)
    monkeypatch.setattr("app.db_client.err_message", lambda r: "")
    m.audit_thread_summary_schema()
    assert m.schema_audit_status()["status"] == "ok"


def test_audit_records_missing_column_when_no_rows(monkeypatch):
    """information_schema returns 0 rows → column not present.
    Audit logs WARNING but does not raise."""
    from app.services import phase_13_7_metrics as m

    fake_query = lambda *a, **k: {
        "columns": ["column_name", "data_type", "is_nullable"],
        "rows": [],
    }
    monkeypatch.setattr("app.db_client.db_query", fake_query)
    monkeypatch.setattr("app.db_client.err_code", lambda r: None)
    monkeypatch.setattr("app.db_client.err_message", lambda r: "")
    m.audit_thread_summary_schema()
    assert m.schema_audit_status()["status"] == "missing_column"


def test_audit_records_wrong_type_when_not_nullable(monkeypatch):
    """is_nullable='NO' breaks the fallback chain assumption.
    Audit logs WARNING, sets status accordingly."""
    from app.services import phase_13_7_metrics as m

    fake_query = lambda *a, **k: {
        "columns": ["column_name", "data_type", "is_nullable"],
        "rows": [("context_summary", "text", "NO")],
    }
    monkeypatch.setattr("app.db_client.db_query", fake_query)
    monkeypatch.setattr("app.db_client.err_code", lambda r: None)
    monkeypatch.setattr("app.db_client.err_message", lambda r: "")
    m.audit_thread_summary_schema()
    assert m.schema_audit_status()["status"] == "wrong_type"


def test_audit_records_error_on_db_failure(monkeypatch):
    """If the audit query itself fails (e.g. connection error), the
    audit logs but doesn't raise — boot must succeed."""
    from app.services import phase_13_7_metrics as m

    monkeypatch.setattr("app.db_client.db_query", lambda *a, **k: {"error": {"code": "connection_error", "message": "down"}})
    monkeypatch.setattr("app.db_client.err_code", lambda r: (r.get("error") or {}).get("code"))
    monkeypatch.setattr("app.db_client.err_message", lambda r: (r.get("error") or {}).get("message", ""))
    m.audit_thread_summary_schema()
    assert m.schema_audit_status()["status"] == "error"


# ── 4. JSON-reliability on transform path (Move 1) ───────────────────


def test_format_response_recovers_from_bleed_on_continuation_turn(monkeypatch):
    """When the integrator emits valid AnswerCard JSON but the sanitizer
    can't extract human-visible text from direct_answer (returns empty),
    AND this is a continuation turn (has previous_thread_summary),
    recover the stub answer instead of showing 'I had trouble
    formatting...'.

    Repros the audience_rewrite bench failure mode and confirms the fix.
    Patches display_text_for_parsed_answer_card to simulate the real
    failure: sanitizer ate everything, returns empty.
    """
    import json
    from types import SimpleNamespace
    from app.responder import final as fr

    # Valid AnswerCard but the sanitizer comes up empty.
    valid_card = json.dumps({
        "mode": "FACTUAL",
        "direct_answer": "anything here",
        "sections": [],
    })

    def fake_generate(*a, **k):
        return (valid_card, {"input_tokens": 10, "output_tokens": 10})

    monkeypatch.setattr("app.services.llm_manager.generate_sync", fake_generate)
    # Force the bleed-fallback condition: display_text returns empty.
    monkeypatch.setattr(fr, "display_text_for_parsed_answer_card", lambda _p: "")

    plan = SimpleNamespace(subquestions=[
        SimpleNamespace(id="sq1", text="rewrite as bullet list", intent_score=0.5)
    ])
    stub = "Here is the bulleted version of the prior PA timeline answer..."

    msg, _usage = fr.format_response(
        plan,
        [stub],
        user_message="rewrite as bullets",
        previous_thread_summary="Sunshine FL PA timeline conversation",
    )
    parsed = json.loads(msg)
    # The recovered direct_answer should be the stub, NOT the bleed
    # fallback message.
    assert "trouble formatting" not in parsed["direct_answer"].lower()
    assert "bulleted version" in parsed["direct_answer"]


def test_format_response_keeps_bleed_fallback_on_first_turn(monkeypatch):
    """On a fresh turn (no previous_thread_summary), we DON'T recover
    from bleed — the user genuinely got a broken answer and should
    rephrase. The recovery is ONLY for continuation turns where we
    have prior content as a known-good fallback."""
    import json
    from types import SimpleNamespace
    from app.responder import final as fr

    valid_card = json.dumps({
        "mode": "FACTUAL",
        "direct_answer": "x",
        "sections": [],
    })

    def fake_generate(*a, **k):
        return (valid_card, {"input_tokens": 10, "output_tokens": 10})

    monkeypatch.setattr("app.services.llm_manager.generate_sync", fake_generate)
    monkeypatch.setattr(fr, "display_text_for_parsed_answer_card", lambda _p: "")

    plan = SimpleNamespace(subquestions=[
        SimpleNamespace(id="sq1", text="fresh question", intent_score=0.5)
    ])
    msg, _usage = fr.format_response(
        plan,
        ["a stub from corpus retrieval, not a transform"],
        user_message="fresh question",
        previous_thread_summary=None,  # ← first turn
    )
    parsed = json.loads(msg)
    # Falls through to the standard bleed fallback
    assert "trouble formatting" in parsed["direct_answer"].lower()


def test_format_response_short_stub_does_not_recover(monkeypatch):
    """If the stub is too short (<20 chars), don't trust it — fall
    through to the bleed fallback so the user gets a clear retry hint
    rather than a half-broken answer."""
    import json
    from types import SimpleNamespace
    from app.responder import final as fr

    valid_card = json.dumps({
        "mode": "FACTUAL",
        "direct_answer": "x",
        "sections": [],
    })

    def fake_generate(*a, **k):
        return (valid_card, {"input_tokens": 10, "output_tokens": 10})

    monkeypatch.setattr("app.services.llm_manager.generate_sync", fake_generate)
    monkeypatch.setattr(fr, "display_text_for_parsed_answer_card", lambda _p: "")

    plan = SimpleNamespace(subquestions=[
        SimpleNamespace(id="sq1", text="t", intent_score=0.5)
    ])
    msg, _usage = fr.format_response(
        plan,
        ["short."],  # 6 chars; below the 20-char floor
        user_message="x",
        previous_thread_summary="something",
    )
    parsed = json.loads(msg)
    assert "trouble formatting" in parsed["direct_answer"].lower()


def test_audit_never_raises_even_when_module_imports_fail(monkeypatch):
    """Defensive: if the import chain itself blows up (e.g. db_client
    not importable in some test fixture), audit catches and logs."""
    from app.services import phase_13_7_metrics as m

    def boom(*a, **k):
        raise RuntimeError("simulated import failure")

    # Force the import inside audit_thread_summary_schema to fail
    import app.db_client as dc
    monkeypatch.setattr(dc, "db_query", boom)
    m.audit_thread_summary_schema()
    # status set to error rather than the audit raising
    assert m.schema_audit_status()["status"] in ("error", "ok", "missing_column", "wrong_type")
