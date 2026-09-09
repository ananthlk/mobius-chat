"""Persistence for per-module turn spans (P2b).

WRITER AND READER LIVE IN ONE MODULE ON PURPOSE. The acceptance criterion
for this phase is a same-turn read-back — write a span on a real turn, then
read it back from the destination of record and render it. Splitting the
two across modules is how a write path loses its reader, and this program
has now catalogued a dozen instances of exactly that.

A note on what "read it back" has to mean, learned the expensive way on
2026-09-09: a correlation_id fix was reported verified end-to-end on the
strength of a log line that carried the field — but that log line was a
different record (the PHI diagnostics envelope), while the destination of
record (llm_calls) still wrote NULL. A read-back of the wrong artifact is
indistinguishable from a successful one. So read_spans() reads the TABLE,
and the acceptance test asserts against what read_spans() returns — not an
emitter log, not an in-memory TurnTrace, not the nearest thing with the
right-looking field on it.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from app.db_client import db_execute, db_query

logger = logging.getLogger(__name__)

# The db_client's LOGICAL key, not the physical database name. _get_fallback_url
# maps "chat" -> CHAT_RAG_DATABASE_URL (db_client.py:133); "mobius_chat" is the
# actual Postgres database, and passing it here produced
#   connection_error: No fallback URL for database 'mobius_chat'
# on every write and read. Every other storage module uses "chat"
# (threads.py, turns.py, feedback.py, tool_policy.py, ...).
_DB = "chat"


def save_spans(
    correlation_id: str,
    rows: list[dict[str, Any]],
    *,
    chat_mode: str | None = None,
    model_mix: list[dict[str, Any]] | None = None,
    rich_evidence: bool | None = None,
    sample_rate: float | None = None,
) -> int:
    """Persist one turn's spans. Returns the number written.

    Never raises: a telemetry failure must not fail a turn the user is
    waiting on. But it never fails silently either — a swallowed write here
    would make a gap in the data look like a fast turn, which is the exact
    defect this table exists to surface. Every failure logs at WARNING with
    the correlation_id so a missing row is traceable to a logged cause.
    """
    if not rows:
        return 0
    written = 0
    _mix = json.dumps(model_mix or [])
    _rate = sample_rate
    for r in rows:
        try:
            result = db_execute(
                """
                INSERT INTO turn_spans (
                    correlation_id, span_id, parent_span_id, module, label, depth,
                    wall_ms, llm_ms, counts, model_mix, rich_evidence, chat_mode,
                    sampled, sample_rate
                )
                VALUES (:cid, :span_id, :parent_span_id, :module, :label, :depth,
                        :wall_ms, :llm_ms, CAST(:counts AS jsonb),
                        CAST(:model_mix AS jsonb), :rich_evidence, :chat_mode,
                        :sampled, :sample_rate)
                ON CONFLICT (correlation_id, span_id) DO NOTHING
                """,
                _DB,
                params={
                    "cid": correlation_id,
                    "span_id": r.get("span_id"),
                    "parent_span_id": r.get("parent_span_id"),
                    "module": r.get("module"),
                    "label": r.get("label"),
                    "depth": int(r.get("depth") or 0),
                    "wall_ms": float(r.get("wall_ms") or 0.0),
                    "llm_ms": float(r.get("llm_ms") or 0.0),
                    "counts": json.dumps(r.get("counts") or []),
                    "model_mix": _mix,
                    "rich_evidence": rich_evidence,
                    "chat_mode": chat_mode,
                    "sampled": True,
                    "sample_rate": _rate,
                },
            )
            if isinstance(result, dict) and result.get("error"):
                logger.warning(
                    "[turn_spans] write failed cid=%s module=%s: %s",
                    correlation_id[:8], r.get("module"), result.get("error"),
                )
                continue
            written += 1
        except Exception as exc:
            logger.warning(
                "[turn_spans] write raised cid=%s module=%s: %s",
                correlation_id[:8], r.get("module"), exc,
            )
    if written != len(rows):
        logger.warning(
            "[turn_spans] partial write cid=%s: %d of %d spans stored",
            correlation_id[:8], written, len(rows),
        )
    return written


def read_spans(correlation_id: str) -> list[dict[str, Any]]:
    """Read one turn's spans back from the table. The destination of record.

    Ordered by depth then wall_ms desc so the reader renders a tree with the
    most expensive branch first — the shape someone diagnosing a slow turn
    actually wants to see.
    """
    result = db_query(
        """
        SELECT span_id, parent_span_id, module, label, depth,
               wall_ms, llm_ms, counts, model_mix, rich_evidence, chat_mode,
               sampled, sample_rate
        FROM turn_spans
        WHERE correlation_id = :cid
        ORDER BY depth ASC, wall_ms DESC
        """,
        _DB,
        params={"cid": correlation_id},
    )
    if not isinstance(result, dict) or result.get("error"):
        logger.warning(
            "[turn_spans] read failed cid=%s: %s",
            correlation_id[:8],
            (result or {}).get("error") if isinstance(result, dict) else "no result",
        )
        return []
    cols = result.get("columns") or []
    out: list[dict[str, Any]] = []
    for row in result.get("rows") or []:
        d = dict(zip(cols, row))
        for k in ("counts", "model_mix"):
            v = d.get(k)
            if isinstance(v, str):
                try:
                    d[k] = json.loads(v)
                except Exception:
                    d[k] = []
        for k in ("wall_ms", "llm_ms"):
            if d.get(k) is not None:
                d[k] = float(d[k])
        d["self_ms"] = round(max(0.0, (d.get("wall_ms") or 0.0) - (d.get("llm_ms") or 0.0)), 2)
        out.append(d)
    return out


def summarize(spans: list[dict[str, Any]]) -> dict[str, Any]:
    """Turn-level rollup for the diagnostics panel.

    ``counts`` is aggregated across spans and sorted by n descending, so the
    loop is the first thing on screen rather than something a reader has to
    total up by eye. A panel that renders only a duration would foreclose
    the question even though the data supports it.
    """
    roots = [s for s in spans if int(s.get("depth") or 0) == 0]
    agg: dict[tuple[str, str], dict[str, Any]] = {}
    for s in spans:
        for c in s.get("counts") or []:
            key = (c.get("kind"), c.get("target"))
            a = agg.setdefault(key, {"kind": c.get("kind"), "target": c.get("target"),
                                     "n": 0, "ms": 0.0})
            a["n"] += int(c.get("n") or 0)
            a["ms"] += float(c.get("ms") or 0.0)
    counts = sorted(agg.values(), key=lambda a: a["n"], reverse=True)
    for a in counts:
        a["ms"] = round(a["ms"], 2)
    wall = round(sum(float(s.get("wall_ms") or 0.0) for s in roots), 2)
    llm = round(sum(float(s.get("llm_ms") or 0.0) for s in roots), 2)
    # llm_ms can legitimately EXCEED wall_ms when LLM calls run in parallel
    # (the integrator fans out): llm_ms is total model time, not wall time
    # spent in the model. Clamping self_ms to 0 keeps it meaningful, but the
    # clamp would silently hide the condition — so it is reported. A reader
    # seeing self_ms=0 must be able to tell "no code time" from "we could not
    # compute code time because the calls overlapped".
    _parallel = llm > wall
    return {
        "wall_ms": wall,
        "llm_ms": llm,
        "self_ms": round(max(0.0, wall - llm), 2),
        "llm_exceeds_wall": _parallel,
        "span_count": len(spans),
        "counts": counts,
        "model_mix": (roots[0].get("model_mix") if roots else []) or [],
        "rich_evidence": (roots[0].get("rich_evidence") if roots else None),
        "chat_mode": (roots[0].get("chat_mode") if roots else None),
    }


def list_recent_traces(limit: int = 40) -> list[dict[str, Any]]:
    """Recent turns that produced spans — the index for the trace viewer.

    Rolls up to one row per turn from its depth-0 spans. Ordered newest
    first, and carries enough to triage without opening each trace: total
    wall, llm, the derived self, and the mode. A list that showed only a
    duration would make the reader open every row to find the slow one.
    """
    result = db_query(
        """
        SELECT correlation_id,
               MAX(created_at)                        AS created_at,
               SUM(wall_ms) FILTER (WHERE depth = 0)  AS wall_ms,
               SUM(llm_ms)  FILTER (WHERE depth = 0)  AS llm_ms,
               COUNT(*)                               AS span_count,
               MAX(chat_mode)                         AS chat_mode,
               BOOL_OR(rich_evidence)                 AS rich_evidence,
               MAX(sample_rate)                       AS sample_rate
        FROM turn_spans
        GROUP BY correlation_id
        ORDER BY MAX(created_at) DESC
        LIMIT :lim
        """,
        _DB,
        params={"lim": int(limit)},
    )
    if not isinstance(result, dict) or result.get("error"):
        logger.warning("[turn_spans] recent list failed: %s",
                       (result or {}).get("error") if isinstance(result, dict) else "no result")
        return []
    cols = result.get("columns") or []
    out = []
    for row in result.get("rows") or []:
        d = dict(zip(cols, row))
        for k in ("wall_ms", "llm_ms"):
            d[k] = float(d.get(k) or 0.0)
        d["self_ms"] = round(max(0.0, d["wall_ms"] - d["llm_ms"]), 2)
        d["llm_exceeds_wall"] = d["llm_ms"] > d["wall_ms"]
        d["created_at"] = str(d.get("created_at") or "")
        out.append(d)
    return out
