"""thread_gaps — the gap ledger. DB at the edges, decisions in posture.py.

The governor mints ids; react never does. A model cannot be relied on to
remember an identifier across rounds, and every round is a fresh call. So the
open list is INJECTED each round and react answers BY REFERENCE.

Read at state_load. Written ONCE at turn end, in the attestation's finally --
not per round. That keeps the round loop free of DB writes, reuses a settling
step already proven, and is correct for ERROR turns: an errored turn did not
finish judging its evidence, so its gap list is not authoritative and must land
marked provisional rather than silently promoting a half-judged list into
thread-scoped state. That risk exists only because the ledger now outlives the
turn.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from app.pipeline.v2.posture import Attempt, Gap

logger = logging.getLogger(__name__)

# db_client's LOGICAL key, NOT the physical database name. promise.py:39 carries
# the same constant with the comment "see turn_spans.py for why not
# 'mobius_chat'" -- a warning written to prevent exactly this error, in the file
# this module was modelled on. I copied the pattern and not the constant, and
# every round write failed with
#   {'code': 'connection_error', 'message': "No fallback URL for database 'mobius_chat'"}
# behind a correct swallow, producing zero rows while the flush logged rows=1.
_DB = "chat"


def mint_gap_id(thread_id: str, existing_count: int) -> str:
    """Deterministic, readable, stable. Pure -- no clock, no counter table."""
    return f"{str(thread_id)[:8]}-G{existing_count + 1}"


@dataclass(frozen=True)
class GapUpdate:
    """What one turn did to the ledger. Applied once, at settle."""
    closed_ids: tuple[str, ...] = ()
    new_texts: tuple[str, ...] = ()
    attempts: tuple[tuple[str, Attempt], ...] = ()   # (gap_id, attempt)
    provisional: bool = False                        # ERROR turns


def load_open(thread_id: str) -> tuple[Gap, ...]:
    """One indexed read on (thread_id, status='open').

    state_load is 412.7ms p50 (down from 1,572.9ms). This read belongs BESIDE
    the existing state read, not after it, and its cost must be MEASURED once it
    lands rather than assumed.
    """
    try:
        from app.db_client import db_query

        res = db_query(
            """
            SELECT gap_id, text, opened_round, importance, attempted_by
              FROM thread_gaps
             WHERE thread_id = :tid AND status = 'open'
             ORDER BY opened_at
            """,
            _DB,
            params={"tid": str(thread_id)},
        )
    except Exception as exc:
        logger.warning("[v2.ledger] load_open failed thread=%s: %s", str(thread_id)[:8], exc)
        return ()

    cols = (res or {}).get("columns") or []
    rows = (res or {}).get("rows") or []
    out: list[Gap] = []
    for row in rows:
        d = dict(zip(cols, row, strict=False))
        raw = d.get("attempted_by") or []
        if isinstance(raw, str):
            import json
            try:
                raw = json.loads(raw)
            except Exception:
                raw = []
        attempts = tuple(
            Attempt(
                round_index=int(a.get("round_index") or 0),
                tool=a.get("tool"),
                model=a.get("model"),
                query=a.get("query"),
                # PAYLOAD check, never the tool's own success flag -- port
                # hazard 9: the MCP adapter attaches a self-citing SourceRef on
                # success, so trusting the flag makes exhaustion structurally
                # unreachable for ~34 tools.
                returned_payload=bool(a.get("returned_payload")),
            )
            for a in raw
            if isinstance(a, dict)
        )
        out.append(
            Gap(
                gap_id=d.get("gap_id"),
                text=d.get("text") or "",
                opened_round=int(d.get("opened_round") or 0),
                importance=d.get("importance") or "normal",
                attempted_by=attempts,
            )
        )
    return tuple(out)


# ── round records ────────────────────────────────────────────────────────────

def write_rounds(correlation_id: str, rows: list[dict]) -> None:
    """Persist the round records for one turn. Called ONCE, at settle.

    NOT per round: a write on the round path adds latency to production for an
    observer's benefit, and this module's whole posture is that an observer must
    not affect the thing it observes. Rows accumulate on ctx during the turn and
    land together here.

    Never raises into the caller. But it logs a failure with the id -- a missing
    row must be traceable to a logged cause rather than reading like a turn that
    simply had no rounds, which is the shape this program has found twelve times.
    """
    if not rows:
        return
    try:
        from app.db_client import db_execute

        for r in rows:
            res = db_execute(
                """
                INSERT INTO turn_rounds (
                    correlation_id, round_index, orchestrator_version,
                    posture, directive, gap_targeted, rationale,
                    v1_directive, v1_reason, v1_maps_to, shadow_verdict,
                    gaps_opened, gaps_closed, overran,
                    tools_offered, tool_called, declared_latency_ms, declared_version
                ) VALUES (
                    :cid, :rn, :ver, :posture, :directive, :gap, :rationale,
                    :v1d, :v1r, :v1m, :verdict,
                    CAST(:opened AS JSONB), CAST(:closed AS JSONB), :overran,
                    CAST(:offered AS JSONB), :tool_called, :decl_ms, :decl_ver
                )
                ON CONFLICT (correlation_id, round_index, orchestrator_version)
                DO NOTHING
                """,
                _DB,
                params={
                    "cid": correlation_id,
                    "rn": int(r.get("round") or 0),
                    "ver": r.get("orchestrator_version") or "v1",
                    "posture": r.get("v2_posture"),
                    "directive": r.get("v2_directive"),
                    "gap": r.get("v2_gap_targeted"),
                    "rationale": r.get("v2_because"),
                    "v1d": r.get("v1_directive"),
                    "v1r": r.get("v1_reason"),
                    "v1m": r.get("v1_maps_to"),
                    "verdict": r.get("verdict"),
                    # json.dumps, not the list: db_execute JSON-serialises its
                    # params for the db-agent transport, and a raw list would
                    # arrive as a Postgres array literal, not JSONB. The
                    # datetime lesson from the attestation, one type over.
                    "opened": json.dumps(r.get("gaps_opened") or []),
                    "closed": json.dumps(r.get("gaps_closed") or []),
                    "overran": bool(r.get("v2_overran")),
                    # What was AVAILABLE that round. On v1 rows this is the
                    # whole manifest, recorded as a marker rather than 57 names.
                    "offered": json.dumps(r.get("tools_offered") or []),
                    # What the round actually RAN -- the join that turns
                    # declared per-tool latencies into measured ones.
                    "tool_called": r.get("tool_called"),
                    "decl_ms": r.get("declared_latency_ms"),
                    "decl_ver": r.get("declared_version"),
                },
            )
            if isinstance(res, dict) and res.get("error"):
                logger.warning("[v2.ledger] round write failed cid=%s r%s: %s",
                               str(correlation_id)[:8], r.get("round"), res.get("error"))
    except Exception as exc:
        logger.warning("[v2.ledger] write_rounds raised cid=%s: %s",
                       str(correlation_id)[:8], exc)
