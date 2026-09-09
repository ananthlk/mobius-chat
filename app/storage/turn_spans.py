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

_DB = "mobius_chat"


def save_spans(
    correlation_id: str,
    rows: list[dict[str, Any]],
    *,
    chat_mode: str | None = None,
    model_mix: list[dict[str, Any]] | None = None,
    rich_evidence: bool | None = None,
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
    for r in rows:
        try:
            result = db_execute(
                """
                INSERT INTO turn_spans (
                    correlation_id, span_id, parent_span_id, module, depth,
                    wall_ms, llm_ms, counts, model_mix, rich_evidence, chat_mode
                )
                VALUES (:cid, :span_id, :parent_span_id, :module, :depth,
                        :wall_ms, :llm_ms, CAST(:counts AS jsonb),
                        CAST(:model_mix AS jsonb), :rich_evidence, :chat_mode)
                ON CONFLICT (correlation_id, span_id) DO NOTHING
                """,
                _DB,
                params={
                    "cid": correlation_id,
                    "span_id": r.get("span_id"),
                    "parent_span_id": r.get("parent_span_id"),
                    "module": r.get("module"),
                    "depth": int(r.get("depth") or 0),
                    "wall_ms": float(r.get("wall_ms") or 0.0),
                    "llm_ms": float(r.get("llm_ms") or 0.0),
                    "counts": json.dumps(r.get("counts") or []),
                    "model_mix": _mix,
                    "rich_evidence": rich_evidence,
                    "chat_mode": chat_mode,
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
        SELECT span_id, parent_span_id, module, depth,
               wall_ms, llm_ms, counts, model_mix, rich_evidence, chat_mode
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
    return {
        "wall_ms": wall,
        "llm_ms": llm,
        "self_ms": round(max(0.0, wall - llm), 2),
        "span_count": len(spans),
        "counts": counts,
        "model_mix": (roots[0].get("model_mix") if roots else []) or [],
        "rich_evidence": (roots[0].get("rich_evidence") if roots else None),
        "chat_mode": (roots[0].get("chat_mode") if roots else None),
    }
