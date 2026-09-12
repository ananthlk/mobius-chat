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


def attempt_from_row(a: dict) -> Attempt:
    """One persisted attempt -> one Attempt. Extracted so the read path is
    testable without a database: the write side lives in posture.explain and
    the two must not drift."""
    return Attempt(
        round_index=int(a.get("round_index") or a.get("round") or 0),
        tool=a.get("tool"),
        model=a.get("model"),
        query=a.get("query"),
        # PAYLOAD check, never the tool's own success flag -- port hazard 9:
        # the MCP adapter attaches a self-citing SourceRef on success, so
        # trusting the flag makes exhaustion structurally unreachable for ~34
        # tools.
        returned_payload=bool(a.get("returned_payload")),
        # Absent on rows written before 2026-09-12. TRUE is the pre-fix
        # meaning (every attempt counted against every gap), so an old row
        # replays as the governor actually decided rather than being
        # retroactively reinterpreted by a newer default.
        targeted=bool(a.get("targeted", True)),
    )


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
            attempt_from_row(a) for a in raw if isinstance(a, dict)
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
                    tools_offered, tool_called, round_duration_s,
                    applied_directive, v2_applied, prompt_mismatch, decision_inputs,
                    framing_inputs, executor_inputs,
                    declared_latency_ms, declared_version
                ) VALUES (
                    :cid, :rn, :ver, :posture, :directive, :gap, :rationale,
                    :v1d, :v1r, :v1m, :verdict,
                    CAST(:opened AS JSONB), CAST(:closed AS JSONB), :overran,
                    CAST(:offered AS JSONB), :tool_called, :dur_s,
                    :applied, :v2_applied, :mismatch, CAST(:inputs AS JSONB),
                    CAST(:framing AS JSONB), CAST(:execin AS JSONB),
                    :decl_ms, :decl_ver
                )
                ON CONFLICT (correlation_id, round_index, orchestrator_version)
                DO UPDATE SET
                    -- TWO HOOKS WRITE ONE ROUND. The pre-round hook records
                    -- the posture before the round runs; the post-round hook
                    -- records what the round DID and -- on a routed turn --
                    -- what the executor substituted. Same round_index, so
                    -- DO NOTHING silently dropped the second one, and the
                    -- executor's fields never reached the table while the
                    -- logs showed it firing. Found by reading the row against
                    -- the log rather than trusting either alone.
                    --
                    -- COALESCE(EXCLUDED, existing) on every nullable column:
                    -- the later write fills in what it knows and CANNOT erase
                    -- what the earlier one knew. Plain EXCLUDED would let the
                    -- pre-round hook's nulls wipe the post-round hook's values
                    -- on any future reordering.
                    posture           = COALESCE(EXCLUDED.posture, turn_rounds.posture),
                    directive         = COALESCE(EXCLUDED.directive, turn_rounds.directive),
                    gap_targeted      = COALESCE(EXCLUDED.gap_targeted, turn_rounds.gap_targeted),
                    rationale         = COALESCE(EXCLUDED.rationale, turn_rounds.rationale),
                    v1_directive      = COALESCE(EXCLUDED.v1_directive, turn_rounds.v1_directive),
                    v1_reason         = COALESCE(EXCLUDED.v1_reason, turn_rounds.v1_reason),
                    v1_maps_to        = COALESCE(EXCLUDED.v1_maps_to, turn_rounds.v1_maps_to),
                    shadow_verdict    = COALESCE(EXCLUDED.shadow_verdict, turn_rounds.shadow_verdict),
                    tool_called       = COALESCE(EXCLUDED.tool_called, turn_rounds.tool_called),
                    round_duration_s  = COALESCE(EXCLUDED.round_duration_s, turn_rounds.round_duration_s),
                    applied_directive = COALESCE(EXCLUDED.applied_directive, turn_rounds.applied_directive),
                    prompt_mismatch   = COALESCE(EXCLUDED.prompt_mismatch, turn_rounds.prompt_mismatch),
                    decision_inputs   = COALESCE(EXCLUDED.decision_inputs, turn_rounds.decision_inputs),
                    framing_inputs    = COALESCE(EXCLUDED.framing_inputs, turn_rounds.framing_inputs),
                    executor_inputs   = COALESCE(EXCLUDED.executor_inputs, turn_rounds.executor_inputs),
                    -- booleans: OR, never overwrite. A round that overran or
                    -- was executed by v2 cannot become one that wasn't.
                    overran           = turn_rounds.overran OR EXCLUDED.overran,
                    v2_applied        = turn_rounds.v2_applied OR EXCLUDED.v2_applied
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
                    # Successor-differenced at settle. None on the last round:
                    # its span includes publish and is NOT a tool latency.
                    "dur_s": r.get("round_duration_s"),
                    # THE SUBSTITUTION. v1's vocabulary, and the only field on
                    # this row that says what the executor actually did.
                    # NULL on a v1 turn -- a real distinction, not a missing
                    # value: a v1 turn has no applied v2 directive.
                    "applied": r.get("v2_directive_applied"),
                    "v2_applied": bool(r.get("v2_applied")),
                    "mismatch": r.get("v2_prompt_mismatch"),
                    # The decision's INPUTS, not its verdict. json.dumps for
                    # the same reason gaps_opened needs it: db_execute
                    # JSON-serialises params for the transport, and a raw dict
                    # would arrive as a Postgres composite, not JSONB.
                    # None, not json.dumps(None). json.dumps(None) is the
                    # STRING "null", which CASTs to a JSONB null -- a legal
                    # value that count() counts. It told me all 52 rounds had
                    # framing data when 17 had none: absence dressed as a
                    # plausible value, in the ledger that exists to stop
                    # exactly that.
                    "inputs": (json.dumps(r["v2_decision_inputs"])
                               if r.get("v2_decision_inputs") else None),
                    # What the governor WOULD decide with round N's gaps in
                    # hand. Never replaces `inputs` -- the difference is the
                    # finding.
                    "framing": (json.dumps(r["v2_framing_inputs"])
                                if r.get("v2_framing_inputs") else None),
                    "execin": (json.dumps(r["v2_executor_inputs"])
                               if r.get("v2_executor_inputs") else None),
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


def write_compliance(correlation_id: str, result: dict) -> None:
    """Persist one turn's compliance verdicts. Fail-soft, like write_rounds.

    A telemetry write that can break a turn is not telemetry -- and this one
    runs AFTER the answer is published, so a raise here would fail a turn the
    user has already been served.
    """
    rows: list[tuple] = []
    for s in (result.get("statements") or []):
        rows.append(("statement", int(s.get("round") or 0), s.get("id"),
                     s.get("verdict"), None, None, s.get("basis")))
    for a in (result.get("acks") or []):
        rows.append(("ack", int(a.get("round") or 0), a.get("key"),
                     a.get("verdict"), a.get("claimed"), a.get("observed"),
                     a.get("basis")))
    if not rows:
        # Said out loud. A write that does nothing and logs nothing is
        # indistinguishable from one that never ran.
        logger.info("[v2.compliance] cid=%s nothing to record",
                    str(correlation_id)[:8])
        return
    written = 0
    for kind, rn, item, verdict, claimed, observed, basis in rows:
        if not item or not verdict:
            continue
        try:
            from app.db_client import db_execute

            db_execute(
                """
                INSERT INTO turn_compliance
                    (correlation_id, round_index, kind, item, verdict,
                     claimed, observed, basis)
                VALUES (:cid, :rn, :kind, :item, :verdict, :claimed,
                        :observed, :basis)
                ON CONFLICT (correlation_id, round_index, kind, item)
                DO UPDATE SET
                    verdict  = EXCLUDED.verdict,
                    claimed  = EXCLUDED.claimed,
                    observed = EXCLUDED.observed,
                    basis    = EXCLUDED.basis
                """,
                _DB,
                params={"cid": correlation_id, "rn": rn, "kind": kind,
                        "item": item, "verdict": verdict,
                        "claimed": (claimed or None), "observed": (observed or None),
                        "basis": (basis or None)},
            )
            written += 1
        except Exception as e:                      # pragma: no cover
            logger.warning("[v2.compliance] row failed cid=%s %s/%s: %s",
                           str(correlation_id)[:8], kind, item, e)
    logger.info("[v2.compliance] cid=%s wrote %d/%d verdicts",
                str(correlation_id)[:8], written, len(rows))
