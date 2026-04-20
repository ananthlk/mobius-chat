"""Persist chat turns (question, thinking_log, final_message, sources, metadata) for history and left panel.

All DB access flows through ``app.db_client`` → mobius-db-agent MCP server.
The agent owns pooling + structured errors; this module keeps the
business-logic shape (column-list, graceful-fallback for missing
context_summary / user_id columns, jsonb defensive decode).

db-agent refactor (2026-04-19)
------------------------------
Swapped psycopg2 for ``db_query`` / ``db_execute``. Preserved:
- Primary INSERT column list (test_user_id_plumbing asserts on source text).
- The retry-on-missing-column branch, keyed on ``"user_id" in err_str``.
- COALESCE on ON CONFLICT so user_id is never nulled out by a later update.
"""
import json
import logging
import re
from typing import Any

from app.db_client import db_execute, db_query

logger = logging.getLogger(__name__)

_DB = "chat"

# -------------------------------------------------------------------
# Improvement 1: Structured turn summaries
# -------------------------------------------------------------------

_OUTCOME_MAP = [
    (r"found\s+(\d+)",           "Found {n} result(s)."),
    (r"no results?|not found",    "No results found."),
    (r"created|generated",        "Generated output."),
    (r"explained|overview",       "Provided explanation."),
    (r"error|failed|unable",      "Request could not be completed."),
]


def _detect_outcome(text: str) -> str:
    for pattern, template in _OUTCOME_MAP:
        m = re.search(pattern, text, re.I)
        if m:
            n = m.group(1) if m.lastindex else ""
            return template.replace("{n}", n)
    return "Responded to query."


def _extract_entity(text: str, sources: list[dict[str, Any]]) -> str:
    """Pull a short entity label from sources or leading text."""
    for s in sources[:3]:
        name = s.get("document_name") or s.get("name") or ""
        if name and len(name) < 80:
            return name
    m = re.search(r"(?:found|returned|for)\s+([A-Z][A-Za-z0-9 &\-]{2,50})", text)
    if m:
        return m.group(1).strip()
    return ""


def _extract_jurisdiction(text: str) -> str:
    """Pull first state or payer mention from answer text."""
    m = re.search(r"\b(Florida|Texas|California|New York|Ohio|Georgia|Sunshine Health|United Healthcare|Aetna|Molina|WellCare|Humana)\b", text, re.I)
    return m.group(1) if m else ""


def build_context_summary(final_message: str, sources: list[dict[str, Any]]) -> str:
    """Produce a ≤150-token planner-facing summary of a completed turn."""
    text = re.sub(r"https?://\S+", "", final_message or "")
    text = re.sub(r"\[.*?\]\(.*?\)", "", text)
    text = re.sub(r"[*_`#>]", "", text)
    outcome = _detect_outcome(text)
    entity = _extract_entity(text, sources or [])
    juris = _extract_jurisdiction(text)
    parts = [outcome]
    if entity:
        parts.append(entity)
    if juris:
        parts.append(f"({juris})")
    return " ".join(parts)[:600]  # hard cap ~150 tokens


# -------------------------------------------------------------------
# Agent-response helpers
# -------------------------------------------------------------------


from app.db_client import _err_code, _err_message  # noqa: E402, F401 — shared helpers


def _rows_as_dicts(result: dict) -> list[dict[str, Any]]:
    """Zip columns + rows into dicts. Empty list on error."""
    if _err_code(result) is not None:
        return []
    cols = result.get("columns") or []
    return [dict(zip(cols, r)) for r in (result.get("rows") or [])]


def _decode_jsonb(raw: Any) -> Any:
    """Jsonb comes back as dict/list from psycopg2, but the contract
    allows str (fallback driver, older paths). Defensive decode."""
    if raw is None:
        return None
    if isinstance(raw, (dict, list)):
        return raw
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return None
    return raw


# -------------------------------------------------------------------
# Writes
# -------------------------------------------------------------------


def insert_turn(
    correlation_id: str,
    question: str,
    thinking_log: list[str],
    final_message: str,
    sources: list[dict[str, Any]],
    duration_ms: int | None,
    model_used: str | None,
    llm_provider: str | None,
    session_id: str | None = None,
    thread_id: str | None = None,
    plan_snapshot: dict[str, Any] | None = None,
    blueprint_snapshot: dict[str, Any] | None = None,
    agent_cards: list[dict[str, Any]] | None = None,
    source_confidence_strip: str | None = None,
    config_sha: str | None = None,
    user_id: str | None = None,
) -> None:
    """Insert one turn row. Called by worker when response is complete.

    config_sha ties run to prompts+LLM config version.

    user_id (Phase 2d, added 2026-04-19): the authenticated user_id
    from ``require_user`` when auth is enabled. None in dev / when
    auth is disabled. Stamped onto the row so audit trails, per-user
    rate limiting, and per-user analytics have the attribution they
    need. Requires the ``user_id`` column on ``chat_turns``; when
    missing, the function falls back to the column-less insert path
    (same pattern as the context_summary migration). That way this
    change ships without requiring an immediate DB migration — the
    column can be added at operator's convenience.
    """
    thread_val = (thread_id or "").strip() or None
    if thread_val:
        from app.storage.threads import ensure_thread, set_thread_title_if_empty

        ensure_thread(thread_val)
        set_thread_title_if_empty(thread_val, question or "")

    strip_val = (source_confidence_strip or "").strip() or None
    config_sha_val = (config_sha or "").strip() or None
    user_id_val = (user_id or "").strip() or None
    context_summary_val = build_context_summary(final_message or "", sources or [])

    base_params = {
        "cid": correlation_id,
        "question": (question or "").strip() or "",
        "thinking_log": json.dumps(thinking_log or []),
        "final_message": (final_message or "").strip() or None,
        "sources": json.dumps(sources or []),
        "duration_ms": duration_ms,
        "model_used": (model_used or "").strip() or None,
        "llm_provider": (llm_provider or "").strip() or None,
        "session_id": (session_id or "").strip() or None,
        "thread_id": thread_val,
        "plan_snapshot": json.dumps(plan_snapshot) if plan_snapshot is not None else None,
        "blueprint_snapshot": json.dumps(blueprint_snapshot) if blueprint_snapshot is not None else None,
        "agent_cards": json.dumps(agent_cards) if agent_cards is not None else None,
        "strip": strip_val,
        "config_sha": config_sha_val,
        "context_summary": context_summary_val or None,
        "user_id": user_id_val,
    }

    # Primary INSERT — writes all columns including context_summary + user_id.
    # Column list text is asserted on by test_user_id_plumbing; do NOT
    # reformat without updating those tests:
    #   "source_confidence_strip, config_sha,\n                context_summary, user_id"
    result = db_execute(
        """
        INSERT INTO chat_turns (
            correlation_id, question, thinking_log, final_message, sources,
            duration_ms, model_used, llm_provider, session_id, thread_id,
            plan_snapshot, blueprint_snapshot, agent_cards, source_confidence_strip, config_sha,
                context_summary, user_id
        )
        VALUES (:cid, :question, :thinking_log, :final_message, :sources,
                :duration_ms, :model_used, :llm_provider, :session_id, :thread_id,
                :plan_snapshot, :blueprint_snapshot, :agent_cards, :strip, :config_sha,
                :context_summary, :user_id)
        ON CONFLICT (correlation_id) DO UPDATE SET
            question = EXCLUDED.question,
            thinking_log = EXCLUDED.thinking_log,
            final_message = EXCLUDED.final_message,
            sources = EXCLUDED.sources,
            duration_ms = EXCLUDED.duration_ms,
            model_used = EXCLUDED.model_used,
            llm_provider = EXCLUDED.llm_provider,
            session_id = EXCLUDED.session_id,
            thread_id = EXCLUDED.thread_id,
            plan_snapshot = EXCLUDED.plan_snapshot,
            blueprint_snapshot = EXCLUDED.blueprint_snapshot,
            agent_cards = EXCLUDED.agent_cards,
            source_confidence_strip = EXCLUDED.source_confidence_strip,
            config_sha = EXCLUDED.config_sha,
            context_summary = EXCLUDED.context_summary,
            user_id = COALESCE(EXCLUDED.user_id, chat_turns.user_id)
        """,
        _DB,
        params=base_params,
    )

    code = _err_code(result)
    if code is None:
        return

    # Retry branch: strip context_summary + user_id when those columns
    # haven't been added yet. Keyed on the column-missing error code
    # (or legacy text match — ``"user_id" in err_str`` below is also
    # asserted on by test_user_id_plumbing). Do NOT reformat.
    err_str = _err_message(result).lower()
    if (
        code == "column_missing"
        or "context_summary" in err_str
        or "user_id" in err_str
        or ("column" in err_str and "does not exist" in err_str)
    ):
        fallback_params = {k: v for k, v in base_params.items()
                           if k not in ("context_summary", "user_id")}
        result2 = db_execute(
            """
            INSERT INTO chat_turns (
                correlation_id, question, thinking_log, final_message, sources,
                duration_ms, model_used, llm_provider, session_id, thread_id,
                plan_snapshot, blueprint_snapshot, agent_cards, source_confidence_strip, config_sha
            )
            VALUES (:cid, :question, :thinking_log, :final_message, :sources,
                    :duration_ms, :model_used, :llm_provider, :session_id, :thread_id,
                    :plan_snapshot, :blueprint_snapshot, :agent_cards, :strip, :config_sha)
            ON CONFLICT (correlation_id) DO UPDATE SET
                question = EXCLUDED.question,
                thinking_log = EXCLUDED.thinking_log,
                final_message = EXCLUDED.final_message,
                sources = EXCLUDED.sources,
                duration_ms = EXCLUDED.duration_ms,
                model_used = EXCLUDED.model_used,
                llm_provider = EXCLUDED.llm_provider,
                session_id = EXCLUDED.session_id,
                thread_id = EXCLUDED.thread_id,
                plan_snapshot = EXCLUDED.plan_snapshot,
                blueprint_snapshot = EXCLUDED.blueprint_snapshot,
                agent_cards = EXCLUDED.agent_cards,
                source_confidence_strip = EXCLUDED.source_confidence_strip,
                config_sha = EXCLUDED.config_sha
            """,
            _DB,
            params=fallback_params,
        )
        if _err_code(result2) is None:
            return
        logger.warning(
            "Retry insert_turn without context_summary/user_id failed: %s",
            _err_message(result2),
        )

    logger.exception("Failed to persist turn: %s", _err_message(result))
    raise RuntimeError(_err_message(result))


# -------------------------------------------------------------------
# Reads
# -------------------------------------------------------------------


def get_last_turn_sources(thread_id: str, limit_turns: int = 2) -> list[dict[str, Any]]:
    """Return sources (document_id, document_name) from last N turns in thread for continuity.
    Dedupes by document_id. Used by planner context and retriever include_document_ids."""
    if not (thread_id or "").strip():
        return []
    result = db_query(
        """
        SELECT sources
        FROM chat_turns
        WHERE thread_id = :thread_id AND sources IS NOT NULL AND sources != '[]'::jsonb
        ORDER BY created_at DESC
        LIMIT :lim
        """,
        _DB,
        params={"thread_id": thread_id.strip(), "lim": max(1, min(limit_turns, 5))},
    )
    if _err_code(result) is not None:
        logger.warning("Failed to get last turn sources: %s", _err_message(result))
        return []

    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for row in _rows_as_dicts(result):
        raw = _decode_jsonb(row.get("sources"))
        if not isinstance(raw, list):
            continue
        for elem in raw:
            if not isinstance(elem, dict):
                continue
            doc_id = elem.get("document_id")
            if not doc_id or not str(doc_id).strip():
                continue
            doc_id_str = str(doc_id).strip()
            if doc_id_str in seen:
                continue
            seen.add(doc_id_str)
            out.append({
                "document_id": doc_id_str,
                "document_name": (elem.get("document_name") or "document") or "document",
            })
    return out


def get_recent_turns(limit: int = 10) -> list[dict[str, Any]]:
    """Return list of recent turns: { correlation_id, question, created_at }."""
    result = db_query(
        """
        SELECT correlation_id, question, created_at
        FROM chat_turns
        ORDER BY created_at DESC
        LIMIT :lim
        """,
        _DB,
        params={"lim": max(1, min(limit, 100))},
    )
    if _err_code(result) is not None:
        logger.warning("Failed to get recent turns: %s", _err_message(result))
        return []
    return [
        {
            "correlation_id": r["correlation_id"],
            "question": r.get("question") or "",
            "created_at": _iso(r.get("created_at")),
        }
        for r in _rows_as_dicts(result)
    ]


def get_most_helpful_turns(limit: int = 10) -> list[dict[str, Any]]:
    """Return turns that have feedback rating = 'up', same shape as get_recent_turns."""
    result = db_query(
        """
        SELECT t.correlation_id, t.question, t.created_at
        FROM chat_turns t
        INNER JOIN chat_feedback f ON f.correlation_id = t.correlation_id AND f.rating = 'up'
        ORDER BY t.created_at DESC
        LIMIT :lim
        """,
        _DB,
        params={"lim": max(1, min(limit, 100))},
    )
    if _err_code(result) is not None:
        logger.warning("Failed to get most helpful turns: %s", _err_message(result))
        return []
    return [
        {
            "correlation_id": r["correlation_id"],
            "question": r.get("question") or "",
            "created_at": _iso(r.get("created_at")),
        }
        for r in _rows_as_dicts(result)
    ]


def get_most_helpful_documents(limit: int = 10) -> list[dict[str, Any]]:
    """From turns with feedback up, list documents by how many distinct liked turns featured them."""
    result = db_query(
        """
        WITH liked_docs AS (
            SELECT DISTINCT t.correlation_id, elem->>'document_name' AS document_name, elem->>'document_id' AS document_id
            FROM chat_turns t
            INNER JOIN chat_feedback f ON f.correlation_id = t.correlation_id AND f.rating = 'up'
            CROSS JOIN LATERAL jsonb_array_elements(COALESCE(t.sources, '[]'::jsonb)) AS elem
            WHERE elem->>'document_name' IS NOT NULL AND (elem->>'document_name') != ''
        )
        SELECT document_name, MAX(NULLIF(TRIM(document_id), '')) AS document_id, COUNT(*) AS cited_in_count
        FROM liked_docs
        GROUP BY document_name
        ORDER BY COUNT(*) DESC, document_name
        LIMIT :lim
        """,
        _DB,
        params={"lim": max(1, min(limit, 100))},
    )
    if _err_code(result) is not None:
        logger.warning("Failed to get most helpful documents: %s", _err_message(result))
        return []
    out: list[dict[str, Any]] = []
    for r in _rows_as_dicts(result):
        cnt = r.get("cited_in_count")
        out.append({
            "document_name": r.get("document_name") or "",
            "document_id": r["document_id"] if r.get("document_id") else None,
            "cited_in_count": int(cnt) if cnt is not None else 0,
        })
    return out


def fetch_turn_qc_audit(correlation_id: str) -> dict[str, Any] | None:
    """Return qc_audit JSON from chat_turns, or None if missing / no DB."""
    if not correlation_id:
        return None
    result = db_query(
        "SELECT qc_audit FROM chat_turns WHERE correlation_id = :cid",
        _DB,
        params={"cid": correlation_id},
    )
    if _err_code(result) is not None:
        logger.debug("fetch_turn_qc_audit failed: %s", _err_message(result))
        return None
    rows = _rows_as_dicts(result)
    if not rows:
        return None
    decoded = _decode_jsonb(rows[0].get("qc_audit"))
    if isinstance(decoded, dict):
        return dict(decoded)
    return None


def update_turn_qc_audit(correlation_id: str, qc_audit: dict[str, Any]) -> None:
    """Merge qc_audit JSON into chat_turns (requires migration 023). No-op if DB unavailable.

    JSONB merge uses CAST(:patch AS jsonb) per contract guidance — the
    ``:param::jsonb`` suffix is not reliable through SQLAlchemy's text binder.
    """
    if not correlation_id:
        return
    result = db_execute(
        """
        UPDATE chat_turns
        SET qc_audit = COALESCE(qc_audit, '{}'::jsonb) || CAST(:patch AS jsonb)
        WHERE correlation_id = :cid
        """,
        _DB,
        params={"patch": json.dumps(qc_audit), "cid": correlation_id},
    )
    code = _err_code(result)
    if code is None:
        return
    err = _err_message(result).lower()
    if code == "column_missing" or "qc_audit" in err or ("column" in err and "does not exist" in err):
        logger.debug("update_turn_qc_audit: column missing (run migration 023): %s", _err_message(result))
        return
    logger.warning("update_turn_qc_audit failed: %s", _err_message(result))


# -------------------------------------------------------------------
# Utilities
# -------------------------------------------------------------------


def _iso(val: Any) -> str | None:
    """Coerce a timestamp value to ISO-8601 string, or None.

    The db-agent contract says timestamptz comes back as ISO-8601 str already,
    but the psycopg2 fallback path returns native ``datetime`` — accept both.
    """
    if val is None:
        return None
    if isinstance(val, str):
        return val
    iso = getattr(val, "isoformat", None)
    if callable(iso):
        return iso()
    return str(val)
