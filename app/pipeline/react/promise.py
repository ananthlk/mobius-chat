"""The Product Promise — opened at POST, closed at PUBLISH.

Owned by the GOVERNOR seat (work order ``docs/work-order-promise-step1.md``).
Chat wires the three call sites; the promise's shape and rules live here, so
step 2 (levers/targets) has a single object to read.

WHAT STEP 1 IS: a boundary, not a measurement. It succeeds when a turn can be
asked "what did you promise?" and answer, and when that promise is closed
exactly once. Whether the promise was KEPT is step 3 — which is why
``delivered_cost_c`` and ``delivered_quality`` are written as explicit NULLs
here rather than as zeros. A cost of 0.0 would be indistinguishable from a
cheap turn; a NULL is a declared absence and a step-3 backlog item.

WRITER AND READER LIVE IN ONE MODULE, deliberately — same rule as
``app/storage/turn_spans.py``, and for the reason given there: a read-back of
the wrong artifact is indistinguishable from a successful one. ``read()`` reads
the TABLE, so acceptance evidence cannot be satisfied by a log line that merely
carries the right-looking fields.

TWO CLOCKS (work order §3). ``posted_at``/``published_at`` are wall-clock UTC
and span two processes (API and worker). ``t0_start``/``duration_ms`` are a
``perf_counter`` and are process-local — NOT replaced here, joined by a second
clock. Their difference is the queue wait, which nothing has ever measured.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

# Bump when the §7 numbers change; NEVER edit a version's values in place. An
# attestation is only interpretable against the promise version in force when
# it was made.
PROMISE_VERSION = "v1"

_DB = "chat"  # db_client's LOGICAL key — see turn_spans.py for why not "mobius_chat"

# chat_mode -> tier. The §7 table names three tiers; ``task`` is a fourth legal
# chat_mode (orchestrator._normalize_chat_mode, governor._MODE_DEFAULTS) with
# NO §7 row, so it is deliberately absent rather than mapped to a neighbour.
_TIER_BY_MODE: dict[str, str] = {
    "quick": "fast",
    "copilot": "normal",
    "agentic": "thinking",
}

# §7, verbatim. latency_s is the COMMITMENT (not the measured p50 beside it);
# cost_c is the exploration bound at 15x headroom; quality is the ordinal
# posture. Hard-coded per the work order: these are the PROMISE, while
# _MODE_DEFAULTS are LEVERS. A config surface for them is P5's job.
_TERMS: dict[str, tuple[float, float, str]] = {
    "fast": (13.0, 16.0, "none"),
    "normal": (31.0, 45.0, "some"),
    "thinking": (95.0, 81.0, "best"),
}

_UNPROMISED_NO_TIER = "no section-7 tier for chat_mode={mode!r}"
_UNPROMISED_NO_MODE = (
    "chat_mode absent at POST; the tier is resolved in the worker from thread "
    "state (orchestrator.py:708-712), so POST cannot state one"
)


@dataclass(frozen=True)
class Promise:
    """What was promised, fixed at POST.

    ``latency_s``/``cost_c``/``quality`` are Optional and carry
    ``unpromised_reason`` when absent. That is NOT the same as having no
    promise at all: a turn whose payload predates this feature has
    ``ctx.promise is None``, while a ``task``-mode turn has a Promise that
    explicitly states no terms were promised and why. Collapsing those two
    into ``None`` would hide the gap this work order exists to expose.
    """

    version: str
    tier: str | None
    posted_at: datetime
    latency_s: float | None = None
    cost_c: float | None = None
    quality: str | None = None
    unpromised_reason: str | None = None


def open_promise(chat_mode: str | None, now: datetime) -> Promise:
    """Make the promise. Called once, at POST, and never afterwards.

    Always returns a Promise — never None. A mode with no section-7 tier yields
    a Promise with null terms and a reason, because "we promised nothing, and
    here is why" is a fact worth carrying, and is distinguishable from a
    payload that has no promise key at all.
    """
    raw = (chat_mode or "").strip().lower()
    if not raw:
        return Promise(
            version=PROMISE_VERSION, tier=None, posted_at=now,
            unpromised_reason=_UNPROMISED_NO_MODE,
        )
    tier = _TIER_BY_MODE.get(raw)
    if tier is None:
        return Promise(
            version=PROMISE_VERSION, tier=None, posted_at=now,
            unpromised_reason=_UNPROMISED_NO_TIER.format(mode=raw),
        )
    latency_s, cost_c, quality = _TERMS[tier]
    return Promise(
        version=PROMISE_VERSION, tier=tier, posted_at=now,
        latency_s=latency_s, cost_c=cost_c, quality=quality,
    )


def to_payload(p: Promise) -> dict[str, Any]:
    """Serialise for the queue. One key on the request payload, additive."""
    return {
        "version": p.version,
        "tier": p.tier,
        "posted_at": p.posted_at.astimezone(UTC).isoformat(),
        "latency_s": p.latency_s,
        "cost_c": p.cost_c,
        "quality": p.quality,
        "unpromised_reason": p.unpromised_reason,
    }


def _as_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def from_payload(d: dict | None) -> Promise | None:
    """Rehydrate at the worker. Tolerant of absence — returns None.

    None means "enqueued before the promise existed"; the turn still runs and
    still attests, with a null promise. A promise is NEVER synthesised here:
    one invented after POST is not a promise, and would silently backfill the
    exact gap being measured.
    """
    if not isinstance(d, dict):
        return None
    raw_posted = d.get("posted_at")
    try:
        posted = datetime.fromisoformat(str(raw_posted))
    except (TypeError, ValueError):
        # Loud, not silent: a promise with no readable posted_at cannot anchor
        # the clock. That is a serialisation bug, not an absent promise.
        logger.warning("[promise] unparseable posted_at=%r; treating as absent", raw_posted)
        return None
    if posted.tzinfo is None:
        posted = posted.replace(tzinfo=UTC)
    return Promise(
        version=str(d.get("version") or ""),
        tier=(d.get("tier") or None),
        posted_at=posted,
        latency_s=_as_float(d.get("latency_s")),
        cost_c=_as_float(d.get("cost_c")),
        quality=(d.get("quality") or None),
        unpromised_reason=(d.get("unpromised_reason") or None),
    )


@dataclass(frozen=True)
class Attestation:
    """What was delivered against the promise. One row per turn."""

    correlation_id: str
    promise_version: str | None
    tier: str | None
    posted_at: datetime | None
    published_at: datetime
    outcome: str
    delivered_latency_s: float | None
    worker_latency_s: float | None
    delivered_cost_c: float | None = None      # step 3 — NULL in step 1
    delivered_quality: str | None = None       # step 3 — NULL in step 1
    excludes: list[str] | None = None          # step 3
    notes: str | None = None


def close_promise(
    p: Promise | None,
    *,
    correlation_id: str,
    outcome: str,
    now: datetime,
    worker_latency_s: float | None = None,
) -> Attestation | None:
    """Close the promise. Called once, from run_pipeline's outermost finally.

    Returns an Attestation even when ``p`` is None — a turn with no promise
    still has an outcome, and a missing row would be indistinguishable from a
    turn that never ran.
    """
    if not correlation_id:
        return None
    posted = p.posted_at if p is not None else None
    delivered: float | None = None
    notes: str | None = None
    if posted is not None:
        delta = (now - posted).total_seconds()
        if delta < 0:
            # Wall clocks across two machines skew and can go backwards. A
            # clamped zero is indistinguishable from a fast turn, so record the
            # absence and the reason instead of inventing a number.
            notes = f"clock skew: published_at < posted_at by {abs(delta):.3f}s"
            logger.warning("[promise] %s cid=%s", notes, correlation_id[:8])
        else:
            delivered = delta
    else:
        notes = "no promise on payload (enqueued before promise existed)"
    if p is not None and p.unpromised_reason:
        notes = p.unpromised_reason if notes is None else f"{notes}; {p.unpromised_reason}"
    return Attestation(
        correlation_id=correlation_id,
        promise_version=(p.version if p is not None else None),
        tier=(p.tier if p is not None else None),
        posted_at=posted,
        published_at=now,
        outcome=outcome or "unknown",
        delivered_latency_s=delivered,
        worker_latency_s=worker_latency_s,
        notes=notes,
    )


def write(a: Attestation) -> None:
    """Persist and emit. MUST NEVER raise into the caller.

    Never silent either: a failed write logs at WARNING with the
    correlation_id, so a missing row is traceable to a logged cause rather than
    looking like a turn that simply did not close.
    """
    try:
        from app.db_client import db_execute

        result = db_execute(
            """
            INSERT INTO turn_attestations (
                correlation_id, promise_version, tier, posted_at, published_at,
                outcome, delivered_latency_s, worker_latency_s,
                delivered_cost_c, delivered_quality, notes
            )
            VALUES (:cid, :ver, :tier, :posted_at, :published_at,
                    :outcome, :delivered, :worker, :cost, :quality, :notes)
            ON CONFLICT (correlation_id) DO NOTHING
            """,
            _DB,
            params={
                "cid": a.correlation_id,
                "ver": a.promise_version,
                "tier": a.tier,
                # ISO strings, NOT datetime objects. db_execute JSON-serialises
                # its params for the db-agent transport, and a datetime is not
                # JSON-serialisable -- passing one makes every INSERT raise,
                # which write() then swallows, producing zero rows behind a
                # green test suite. Postgres casts ISO 8601 text to TIMESTAMPTZ.
                # Found by a real write against dev; 21 unit tests missed it
                # because they mocked the writer.
                "posted_at": a.posted_at.isoformat() if a.posted_at else None,
                "published_at": a.published_at.isoformat(),
                "outcome": a.outcome,
                "delivered": a.delivered_latency_s,
                "worker": a.worker_latency_s,
                "cost": a.delivered_cost_c,
                "quality": a.delivered_quality,
                "notes": a.notes,
            },
        )
        if isinstance(result, dict) and result.get("error"):
            logger.warning("[promise] attestation write failed cid=%s: %s",
                           a.correlation_id[:8], result.get("error"))
    except Exception as exc:
        logger.warning("[promise] attestation write raised cid=%s: %s",
                       a.correlation_id[:8], exc)

    # The emit is separate from the persist ON PURPOSE: if the DB write fails,
    # the structured record must still reach Cloud Logging, and vice versa.
    # Wiring them together would let one failure hide the other.
    try:
        logger.info(
            "[promise] attestation cid=%s outcome=%s tier=%s",
            a.correlation_id[:8], a.outcome, a.tier,
            extra={
                "event": "turn_attestation",
                "correlation_id": a.correlation_id,
                "promise_version": a.promise_version,
                "tier": a.tier,
                "outcome": a.outcome,
                "posted_at": a.posted_at.isoformat() if a.posted_at else None,
                "published_at": a.published_at.isoformat(),
                "delivered_latency_s": a.delivered_latency_s,
                "worker_latency_s": a.worker_latency_s,
                "queue_wait_s": (
                    a.delivered_latency_s - a.worker_latency_s
                    if a.delivered_latency_s is not None and a.worker_latency_s is not None
                    else None
                ),
                "notes": a.notes,
            },
        )
    except Exception as exc:
        logger.warning("[promise] attestation emit failed cid=%s: %s",
                       a.correlation_id[:8], exc)


def read(correlation_id: str) -> dict | None:
    """Read one attestation back FROM THE TABLE.

    Exists so acceptance evidence is a read of the destination of record, not
    of an emitter log or an in-memory object that merely has the right fields.
    """
    try:
        from app.db_client import db_query

        res = db_query(
            """
            SELECT correlation_id, promise_version, tier, posted_at, published_at,
                   outcome, delivered_latency_s, worker_latency_s,
                   delivered_cost_c, delivered_quality, notes
            FROM turn_attestations WHERE correlation_id = :cid
            """,
            _DB,
            params={"cid": correlation_id},
        )
        # db_query returns {columns: [...], rows: [[...]]} -- positional rows,
        # not dicts. Zipped here so callers get a mapping and cannot silently
        # index the wrong column after a SELECT-list edit.
        cols = (res or {}).get("columns") or []
        rows = (res or {}).get("rows") or []
        if not rows:
            return None
        return dict(zip(cols, rows[0], strict=False))
    except Exception as exc:
        logger.warning("[promise] attestation read failed cid=%s: %s",
                       correlation_id[:8], exc)
        return None
