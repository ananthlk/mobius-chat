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
    source: str | None = None,
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
    from app.telemetry.spans import classify_source
    _src = classify_source(correlation_id, source)
    for r in rows:
        try:
            result = db_execute(
                """
                INSERT INTO turn_spans (
                    correlation_id, span_id, parent_span_id, module, label, depth,
                    wall_ms, llm_ms, counts, model_mix, rich_evidence, chat_mode,
                    sampled, sample_rate, source
                )
                VALUES (:cid, :span_id, :parent_span_id, :module, :label, :depth,
                        :wall_ms, :llm_ms, CAST(:counts AS jsonb),
                        CAST(:model_mix AS jsonb), :rich_evidence, :chat_mode,
                        :sampled, :sample_rate, :source)
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
                    "source": _src,
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


def list_recent_traces(limit: int = 40, source: str | None = None) -> list[dict[str, Any]]:
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
               MAX(sample_rate)                       AS sample_rate,
               MAX(source)                            AS source
        FROM turn_spans
        WHERE (:src IS NULL OR source = :src)
        GROUP BY correlation_id
        ORDER BY MAX(created_at) DESC
        LIMIT :lim
        """,
        _DB,
        params={"lim": int(limit), "src": source},
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


def _phase_for(node: str) -> str:
    from app.telemetry.spans import phase_for
    return phase_for(node)


def node_rollup(limit_spans: int = 5000, source: str | None = "real") -> dict[str, Any]:
    """Per-node MATRIX: processing · llm · db · tool(external), plus both
    falsifiability directions.

    Ananth's shape: for each node, where did ITS time go. A single duration
    per node says a node is slow; the matrix says WHY, and the four causes
    have four different owners:

      llm         model routing. Not our code — flash ~4.7s vs Pro ~20s is a
                  4x swing that no amount of refactoring touches.
      db          database time, summed from this node's own counts.
      tool        time in CHILD spans that are tool dispatches — work the
                  node handed to something external (RAG over HTTP). Inside
                  the node's wall, but not the node's code.
      processing  wall - llm - db - tool. What the node's OWN code costs.
                  This is the only column a refactor can move, which is why
                  it must not be an unexplained remainder.

    Percentiles are p50/p95 on the node's wall, never a mean — the mean is
    eaten by the tail and the tail is where a loop lives.

    Aggregated in Python rather than SQL: the four-way split depends on the
    parent/child relation AND on JSONB counts, and expressing that as one
    query makes it unreadable for no gain at these volumes. limit_spans
    bounds it; the cap is reported so a truncated rollup is never mistaken
    for a complete one.
    """
    result = db_query(
        """
        SELECT correlation_id, span_id, parent_span_id, module, label,
               depth, wall_ms, llm_ms, counts, created_at, source
        FROM turn_spans
        WHERE (:src IS NULL OR source = :src)
        ORDER BY created_at DESC
        LIMIT :lim
        """,
        _DB,
        params={"lim": int(limit_spans), "src": source},
    )
    if not isinstance(result, dict) or result.get("error"):
        logger.warning("[turn_spans] node rollup failed: %s",
                       (result or {}).get("error") if isinstance(result, dict) else "no result")
        return {"observed": [], "silent": [], "unmodelled": [], "node_count": 0,
                "truncated": False}

    cols = result.get("columns") or []
    rows = [dict(zip(cols, r)) for r in (result.get("rows") or [])]
    for r in rows:
        c = r.get("counts")
        if isinstance(c, str):
            try:
                r["counts"] = json.loads(c)
            except Exception:
                r["counts"] = []
        r["wall_ms"] = float(r.get("wall_ms") or 0.0)
        r["llm_ms"] = float(r.get("llm_ms") or 0.0)

    by_id = {r["span_id"]: r for r in rows}
    # tool time a span delegated to a child dispatch
    tool_child: dict[str, float] = {}
    for r in rows:
        pid = r.get("parent_span_id")
        if pid and str(r.get("label") or "").startswith("tool:"):
            tool_child[pid] = tool_child.get(pid, 0.0) + r["wall_ms"]

    agg: dict[str, dict[str, Any]] = {}
    for r in rows:
        # A child tool span's time belongs to its PARENT's `tool` column, not
        # to a second row of its own — otherwise the node double-counts.
        if str(r.get("label") or "").startswith("tool:") and r.get("parent_span_id") in by_id:
            continue
        node = r["module"]
        a = agg.setdefault(node, {"module": node, "turns": set(), "spans": 0,
                                  "walls": [], "llm": 0.0, "db": 0.0,
                                  "tool": 0.0, "processing": 0.0,
                                  "db_n": 0, "llm_n": 0, "last_seen": ""})
        db_ms = sum(float(c.get("ms") or 0.0) for c in (r.get("counts") or [])
                    if str(c.get("kind", "")).startswith("db."))
        # Per-span mean ms/call, collected so the rollup can report a SPREAD.
        # The stored schema keeps n and total ms per (kind,target), so a true
        # per-call distribution is not recoverable — this is the honest
        # approximation, and the gap is named rather than papered over:
        # averaging cold-start smoke turns (~450ms/call) with real turns
        # (~35ms/call) produced a single number describing neither.
        _dbn = sum(int(c.get("n") or 0) for c in (r.get("counts") or [])
                   if str(c.get("kind", "")).startswith("db."))
        if _dbn:
            a.setdefault("_percall", []).append(db_ms / _dbn)
        db_n = sum(int(c.get("n") or 0) for c in (r.get("counts") or [])
                   if str(c.get("kind", "")).startswith("db."))
        llm_n = sum(int(c.get("n") or 0) for c in (r.get("counts") or [])
                    if c.get("kind") == "llm")
        tool_ms = tool_child.get(r["span_id"], 0.0)
        proc = max(0.0, r["wall_ms"] - r["llm_ms"] - db_ms - tool_ms)

        # A TURN OCCURRENCE, not a correlation_id. The deploy smoke test
        # reuses fixed cids (sel-cid-3/4) and span_id is fresh per run, so
        # those cids accumulate rows on every deploy. Counting distinct cids
        # divided four runs' milliseconds by one "turn" and produced a
        # per-turn figure ~14x the truth. Group by the ROOT span instead.
        a["turns"].add((r["correlation_id"], r.get("parent_span_id") or r["span_id"]))
        a["spans"] += 1
        a["walls"].append(r["wall_ms"])
        a["llm"] += r["llm_ms"]
        a["db"] += db_ms
        a["tool"] += tool_ms
        a["processing"] += proc
        a["db_n"] += db_n
        a["llm_n"] += llm_n
        a["last_seen"] = max(a["last_seen"], str(r.get("created_at") or ""))

    def _pct(vals: list[float], q: float) -> float:
        if not vals:
            return 0.0
        v = sorted(vals)
        k = max(0, min(len(v) - 1, int(round(q * (len(v) - 1)))))
        return v[k]

    observed = []
    for a in agg.values():
        walls = a.pop("walls")
        a["turns"] = len(a["turns"])
        a["p50_wall"] = round(_pct(walls, 0.50), 1)
        a["p95_wall"] = round(_pct(walls, 0.95), 1)
        pc = a.pop("_percall", [])
        a["db_ms_per_call_p50"] = round(_pct(pc, 0.50), 1) if pc else 0.0
        a["db_ms_per_call_p95"] = round(_pct(pc, 0.95), 1) if pc else 0.0
        a["phase"] = _phase_for(a["module"])
        a["total_wall"] = round(sum(walls), 1)
        for k in ("llm", "db", "tool", "processing"):
            a[k] = round(a[k], 1)
        observed.append(a)
    observed.sort(key=lambda x: -x["p95_wall"])

    from app.telemetry.spans import NODE_KEYS, PHASE_ORDER
    seen = {o["module"] for o in observed}

    # Phase tier: preprocessing / react / postprocessing. The three have
    # different levers — preprocessing is nearly all I/O, react is dominated
    # by model routing no refactor touches, postprocessing is our composition
    # code. "The turn is slow" is unanswerable; "preprocessing is 40% of it"
    # points at one of three different bodies of work.
    phases: dict[str, dict[str, Any]] = {}
    for o in observed:
        ph = phases.setdefault(o["phase"], {
            "phase": o["phase"], "processing": 0.0, "llm": 0.0, "db": 0.0,
            "tool": 0.0, "db_n": 0, "llm_n": 0, "nodes": 0, "total_wall": 0.0,
        })
        for k in ("processing", "llm", "db", "tool", "total_wall"):
            ph[k] += o[k]
        ph["db_n"] += o["db_n"]
        ph["llm_n"] += o["llm_n"]
        ph["nodes"] += 1
    for ph in phases.values():
        for k in ("processing", "llm", "db", "tool", "total_wall"):
            ph[k] = round(ph[k], 1)
    phase_list = [phases[p] for p in PHASE_ORDER if p in phases]

    return {
        "phases": phase_list,
        "observed": observed,
        "silent": sorted(NODE_KEYS - seen),
        "unmodelled": sorted(seen - set(NODE_KEYS)),
        "node_count": len(NODE_KEYS),
        "truncated": len(rows) >= limit_spans,
        "source": source or "all",
    }


def turn_matrix(correlation_id: str) -> dict[str, Any]:
    """The per-turn MATRIX: every process row split by where its time went.

    Rows are the span tree — phase, module, and sub-process (round, tool,
    integrate phase) — and every row carries the same four columns so the
    hierarchy never changes units under the reader:

        processing = wall - llm - db - tool(child)   our code, this row only
        llm        model time attributed to this row
        db         database time, with read/write counts split out
        tool       work handed to something external

    `writes` is broken out separately from `db` because a read and a write are
    different problems: a repeated read is usually a caching question, a
    repeated write is usually a correctness one.

    This is a VIEW, not a verdict. It does not rank or flag; it shows the
    decomposition and lets the reader decide what to attack.
    """
    spans = read_spans(correlation_id)
    if not spans:
        return {"correlation_id": correlation_id, "rows": [], "totals": {}}

    from app.telemetry.spans import phase_for
    by_parent: dict[Any, list[dict]] = {}
    for sp in spans:
        by_parent.setdefault(sp.get("parent_span_id"), []).append(sp)

    def _cols(sp: dict) -> dict[str, Any]:
        counts = sp.get("counts") or []
        db_r = sum(float(c["ms"]) for c in counts if c.get("kind") == "db.read")
        db_w = sum(float(c["ms"]) for c in counts if c.get("kind") == "db.write")
        n_r = sum(int(c["n"]) for c in counts if c.get("kind") == "db.read")
        n_w = sum(int(c["n"]) for c in counts if c.get("kind") == "db.write")
        kids = by_parent.get(sp["span_id"], [])
        tool = sum(float(k.get("wall_ms") or 0.0) for k in kids
                   if str(k.get("label") or "").startswith("tool:"))
        kids_wall = sum(float(k.get("wall_ms") or 0.0) for k in kids)
        wall = float(sp.get("wall_ms") or 0.0)

        # llm_ms on the span ROLLS UP from children (a parent's llm includes
        # its children's, so self_ms is honest at every level). For a matrix
        # the rolled-up value double-counts, so `processing` is computed from
        # this span's OWN counts and excludes every child's wall.
        own_llm = sum(float(c["ms"]) for c in counts if c.get("kind") == "llm")

        # A tool:* span is an EXTERNAL call (RAG over HTTP), not our code. As a
        # leaf with no llm/db counts of its own, the generic formula put its
        # whole wall in `processing` — so a 5,770ms RAG call read as 5,770ms of
        # our processing. That is the misattribution that sends someone to
        # optimise the wrong thing, which is the specific harm this view exists
        # to prevent. Its time belongs in `tool` on its own row too, not only
        # on its parent's.
        _is_tool = str(sp.get("label") or "").startswith("tool:")
        if _is_tool:
            tool = wall
        return {
            "wall_ms": round(wall, 1),
            "llm_ms": round(float(sp.get("llm_ms") or 0.0), 1),   # inclusive
            "own_llm_ms": round(own_llm, 1),                       # exclusive
            "db_read_ms": round(db_r, 1), "db_write_ms": round(db_w, 1),
            "db_reads": n_r, "db_writes": n_w, "tool_ms": round(tool, 1),
            # EXCLUSIVE self time: what THIS row cost, children removed. The
            # inclusive version made parent rows look like they held work that
            # actually belonged to a child, and made the column non-additive —
            # a total that does not equal the sum of its parts is a total no
            # one can act on.
            "processing_ms": 0.0 if _is_tool else round(
                max(0.0, wall - kids_wall - own_llm - db_r - db_w), 1),
            "is_external": _is_tool,
        }

    rows: list[dict[str, Any]] = []

    def _walk(sp: dict, depth: int) -> None:
        label = sp.get("label")
        rows.append({
            "depth": depth,
            "phase": phase_for(sp["module"]),
            "module": sp["module"],
            "label": label,
            "name": f"{sp['module']}·{label}" if label else sp["module"],
            **_cols(sp),
        })
        for k in sorted(by_parent.get(sp["span_id"], []),
                        key=lambda x: -(x.get("wall_ms") or 0.0)):
            _walk(k, depth + 1)

    for root in sorted(by_parent.get(None, []),
                       key=lambda x: -(x.get("wall_ms") or 0.0)):
        _walk(root, 0)

    roots = [r for r in rows if r["depth"] == 0]
    totals = {
        "wall_ms": round(sum(r["wall_ms"] for r in roots), 1),
        # Exclusive columns sum across ALL rows; inclusive wall sums roots only.
        "llm_ms": round(sum(r["own_llm_ms"] for r in rows), 1),
        "db_ms": round(sum(r["db_read_ms"] + r["db_write_ms"] for r in rows), 1),
        "db_reads": sum(r["db_reads"] for r in rows),
        "db_writes": sum(r["db_writes"] for r in rows),
        "tool_ms": round(sum(r["tool_ms"] for r in rows if r.get("is_external")), 1),
    }
    totals["processing_ms"] = round(sum(r["processing_ms"] for r in rows), 1)
    return {"correlation_id": correlation_id, "rows": rows, "totals": totals}
