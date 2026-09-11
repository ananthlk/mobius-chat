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

import logging
from dataclasses import dataclass

from app.pipeline.v2.posture import Attempt, Gap

logger = logging.getLogger(__name__)

_DB = "mobius_chat"


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
