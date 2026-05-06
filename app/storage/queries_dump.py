"""Per-query dump for the admin /chat/admin/queries dashboard.

One row per ``chat_turns`` row, joined with aggregated llm_calls + retrieval_runs
+ chat_feedback. Read-only; no mutation. Routes through ``app.db_client.db_query``
like every other read in this app.

Intentionally narrow: this is the v1 "I have no users, give me a flat dump" view.
Filters/sorting are kept minimal — caller paginates with limit/offset, optionally
scopes by since/user_id/has_feedback/has_error.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from app.db_client import db_query

logger = logging.getLogger(__name__)


_DUMP_SQL = """
SELECT
    t.correlation_id,
    t.created_at,
    t.user_id,
    t.session_id,
    t.thread_id,
    LEFT(t.question, 240)        AS question_preview,
    LENGTH(t.final_message)      AS answer_chars,
    t.duration_ms                AS total_latency_ms,
    t.model_used                 AS turn_model,
    t.llm_provider               AS turn_provider,
    t.cache_mode,
    t.cache_top_similarity,
    t.cache_influence,
    COALESCE(llm.call_count, 0)         AS llm_call_count,
    COALESCE(llm.input_tokens, 0)       AS input_tokens,
    COALESCE(llm.output_tokens, 0)      AS output_tokens,
    COALESCE(llm.cost_usd, 0)           AS cost_usd,
    llm.models_used,
    COALESCE(llm.error_count, 0)        AS llm_error_count,
    llm.last_error_type,
    COALESCE(ret.retrieval_runs_count, 0) AS retrieval_runs_count,
    COALESCE(ret.chunks_assembled, 0)     AS chunks_assembled,
    fb.rating                    AS feedback_rating,
    LEFT(fb.comment, 240)        AS feedback_comment
FROM chat_turns t
LEFT JOIN LATERAL (
    SELECT
        COUNT(*)::bigint                                              AS call_count,
        COALESCE(SUM(input_tokens), 0)::bigint                        AS input_tokens,
        COALESCE(SUM(output_tokens), 0)::bigint                       AS output_tokens,
        COALESCE(SUM(cost_usd), 0)::numeric(14, 6)                    AS cost_usd,
        array_to_string(ARRAY(
            SELECT DISTINCT model FROM llm_calls
            WHERE correlation_id = t.correlation_id
            ORDER BY model
        ), ', ')                                                      AS models_used,
        SUM(CASE WHEN success = FALSE THEN 1 ELSE 0 END)::bigint      AS error_count,
        MAX(error_type) FILTER (WHERE success = FALSE)                AS last_error_type
    FROM llm_calls
    WHERE correlation_id = t.correlation_id
) llm ON TRUE
LEFT JOIN LATERAL (
    SELECT
        COUNT(*)::bigint                          AS retrieval_runs_count,
        COALESCE(SUM(n_assembled), 0)::bigint     AS chunks_assembled
    FROM retrieval_runs
    WHERE correlation_id = t.correlation_id
) ret ON TRUE
LEFT JOIN chat_feedback fb ON fb.correlation_id = t.correlation_id
WHERE (:since IS NULL OR t.created_at >= :since)
  AND (:user_id IS NULL OR t.user_id = :user_id)
  AND (
        :has_feedback IS NULL
        OR (:has_feedback = TRUE  AND fb.correlation_id IS NOT NULL)
        OR (:has_feedback = FALSE AND fb.correlation_id IS NULL)
      )
  AND (
        :has_error IS NULL
        OR (:has_error = TRUE  AND COALESCE(llm.error_count, 0) > 0)
        OR (:has_error = FALSE AND COALESCE(llm.error_count, 0) = 0)
      )
ORDER BY t.created_at DESC
LIMIT :limit OFFSET :offset
"""


def fetch_query_dump(
    *,
    limit: int = 100,
    offset: int = 0,
    since: datetime | None = None,
    user_id: str | None = None,
    has_feedback: bool | None = None,
    has_error: bool | None = None,
) -> dict[str, Any]:
    """Return ``{rows: [...], count: N, warning: str | None}``.

    On DB error returns an empty dump with the error message in ``warning``
    rather than raising — same pattern as ``fetch_llm_router_report``.
    """
    limit_clamped = max(1, min(int(limit), 1000))
    offset_clamped = max(0, int(offset))

    params = {
        "limit": limit_clamped,
        "offset": offset_clamped,
        "since": since.astimezone(timezone.utc).isoformat() if since else None,
        "user_id": user_id,
        "has_feedback": has_feedback,
        "has_error": has_error,
    }

    result = db_query(_DUMP_SQL, "chat", params=params, max_rows=limit_clamped)
    err = result.get("error") if isinstance(result, dict) else None
    if err:
        msg = err.get("message", "") if isinstance(err, dict) else str(err)
        logger.warning("fetch_query_dump failed: %s", msg)
        return {"rows": [], "count": 0, "warning": msg[:500]}

    cols = result.get("columns") or []
    raw_rows = result.get("rows") or []
    rows: list[dict[str, Any]] = []
    for row in raw_rows:
        r = dict(zip(cols, row))
        ts = r.get("created_at")
        if hasattr(ts, "isoformat"):
            r["created_at"] = ts.isoformat()
        cost = r.get("cost_usd")
        if cost is not None:
            try:
                r["cost_usd"] = float(cost)
            except (TypeError, ValueError):
                pass
        sim = r.get("cache_top_similarity")
        if sim is not None:
            try:
                r["cache_top_similarity"] = float(sim)
            except (TypeError, ValueError):
                pass
        rows.append(r)

    return {"rows": rows, "count": len(rows), "warning": None}
