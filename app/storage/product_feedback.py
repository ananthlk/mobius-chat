"""Persist product feedback (open + satisfaction-survey instruments) and drive
the per-user prompt cadence.

Complements ``app/storage/feedback.py`` (turn-scoped thumbs). All DB access flows
through ``app.db_client`` → mobius-db-agent MCP, same fail-closed policy as
feedback.py: dev degrades to a log line, hosted raises so nothing vanishes.

The cadence *decision* is a pure function (:func:`evaluate_cadence`) so it can be
unit-tested without a DB or a clock. The DB-backed wrappers read/write
``feedback_prompt_state`` and ``feedback_prompt_events``. See
``docs/feedback-agent-spec.md`` §4B for the three policies.
"""
from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from typing import Any

from app.db_client import _err_code, _err_message, db_execute, db_query

logger = logging.getLogger(__name__)

_DB = "chat"
_MIGRATION = "037"


# ── config knobs (docs/feedback-agent-spec.md §11) ──────────────────────────

def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


CADENCE_THREADS = _int("FEEDBACK_CADENCE_THREADS", 5)
CADENCE_TURNS = _int("FEEDBACK_CADENCE_TURNS", 25)
SNOOZE_ON_DISMISS = _int("FEEDBACK_SNOOZE_ON_DISMISS", 3)
CSAT_SAMPLE_RATE = _float("FEEDBACK_CSAT_SAMPLE_RATE", 0.25)
CSAT_MIN_TURNS = _int("FEEDBACK_CSAT_MIN_TURNS", 3)
NPS_INTERVAL_DAYS = _int("FEEDBACK_NPS_INTERVAL_DAYS", 45)
NPS_SAMPLE_RATE = _float("FEEDBACK_NPS_SAMPLE_RATE", 1.0)

SCORE_SCALES = {"nps": "nps_0_10", "csat": "csat_1_5", "ces": "ces_1_5"}

# Category → downstream routing (mirrors the classifier service's policy so the
# direct-submit API path, which skips classification, routes consistently).
ROUTING = {
    "accuracy_trust": "triage_queue",
    "bug": "triage_queue",
    "coverage_gap": "corpus_backlog",
    "speed": "product_backlog",
    "usability": "product_backlog",
    "feature_request": "product_backlog",
    "other": "product_backlog",
    "praise": "none",
    # docs_gap: filed programmatically by the product-awareness skill when
    # product_help_search misses (no doc above threshold). Routes to the docs
    # curation backlog. See docs/product-awareness-feedback-contract.md.
    "docs_gap": "docs_backlog",
    # doc_stale: the supply side of doc freshness — a module agent (or git hook)
    # ships a user-facing change and files "this doc is now behind." Drained by a
    # weekly refresh sweep. Mirror of docs_gap. trigger="agent_signal".
    "doc_stale": "docs_refresh",
}

# Canonical module slugs for area_tags — the shared vocabulary product-awareness
# and feedback both use (area_tag == module). Slugs are conceptual (what the user
# thinks in); the slug→doc-file map lives in the integration contract.
MODULE_SLUGS = (
    "chat", "rag", "lexicon", "skills", "strategy",      # in-scope corpus
    "os", "credentialing", "roster", "auth",             # valid, corpus pending
    "document-viewer", "infra",
)


def route_for(category: str) -> str:
    return ROUTING.get(category, "product_backlog")


class ProductFeedbackError(RuntimeError):
    """Raised in hosted envs when a write can't reach its target."""


def _env_is_hosted() -> bool:
    env = (os.environ.get("CHAT_ENV") or "dev").strip().lower()
    return env not in ("dev", "development", "local")


def _handle_err(result: dict, kind: str) -> bool:
    """Return True if the caller should treat the op as done (no error).

    Mirrors feedback.py: connection_error / relation_missing degrade in dev,
    raise in hosted. Any other error raises.
    """
    code = _err_code(result)
    if code is None:
        return True
    msg = _err_message(result)
    if code in ("connection_error", "relation_missing"):
        detail = (
            f"{kind} not persisted — db-agent unreachable"
            if code == "connection_error"
            else f"{kind} table missing — run chat DB migration {_MIGRATION}"
        )
        if _env_is_hosted():
            logger.error("[fail-closed] %s: %s", detail, msg)
            raise ProductFeedbackError(detail)
        logger.warning("%s: %s", detail, msg)
        return True
    logger.error("Failed to persist %s: %s", kind, msg)
    raise ProductFeedbackError(msg)


def _deterministic_fraction(*keys: str) -> float:
    """Stable [0,1) value from the keys — sampling without a RNG, so tests and
    reruns agree and a user isn't re-rolled on every eligible turn."""
    h = sha256("|".join(k or "" for k in keys).encode()).hexdigest()
    return int(h[:8], 16) / 0xFFFFFFFF


# ── writes: feedback items ──────────────────────────────────────────────────

def insert_open_feedback(
    *,
    trigger: str,
    category: str,
    verbatim: str,
    tidied: str = "",
    summary: str = "",
    sentiment: str = "neutral",
    severity: str = "low",
    area_tags: list[str] | None = None,
    routed_to: str | None = None,
    user_id: str | None = None,
    thread_id: str | None = None,
    correlation_id: str | None = None,
    org_slug: str | None = None,
    parent_feedback_id: str | None = None,
    config_sha: str | None = None,
    linked_task_id: str | None = None,
) -> str | None:
    """Insert one open-instrument feedback row. Returns feedback_id (or None in
    dev when the DB is unavailable)."""
    import json

    fid = str(uuid.uuid4())
    result = db_execute(
        """
        INSERT INTO product_feedback
            (feedback_id, trigger, kind, category, verbatim, tidied, summary,
             sentiment, severity, area_tags, routed_to, user_id, thread_id,
             correlation_id, org_slug, parent_feedback_id, config_sha, linked_task_id)
        VALUES
            (:fid, :trigger, 'open', :category, :verbatim, :tidied, :summary,
             :sentiment, :severity, CAST(:area_tags AS JSONB), :routed_to, :user_id, :thread_id,
             :correlation_id, :org_slug, :parent, :config_sha, :task_id)
        """,
        _DB,
        params={
            "fid": fid,
            "trigger": trigger,
            "category": category,
            "verbatim": (verbatim or "")[:4000],
            "tidied": (tidied or "")[:4000],
            "summary": (summary or "")[:200],
            "sentiment": sentiment,
            "severity": severity,
            "area_tags": json.dumps(area_tags or []),
            "routed_to": routed_to,
            "user_id": user_id,
            "thread_id": thread_id,
            "correlation_id": correlation_id,
            "org_slug": org_slug,
            "parent": parent_feedback_id,
            "config_sha": config_sha,
            "task_id": linked_task_id,
        },
    )
    if _err_code(result) is not None:
        _handle_err(result, "open feedback")
        return None
    return fid


def insert_survey_score(
    *,
    survey_type: str,
    score: float,
    trigger: str = "periodic",
    user_id: str | None = None,
    thread_id: str | None = None,
    correlation_id: str | None = None,
    org_slug: str | None = None,
) -> str | None:
    """Insert a survey score row (kind='survey'). Returns feedback_id."""
    survey_type = survey_type if survey_type in SCORE_SCALES else "csat"
    fid = str(uuid.uuid4())
    result = db_execute(
        """
        INSERT INTO product_feedback
            (feedback_id, trigger, kind, survey_type, score, score_scale,
             user_id, thread_id, correlation_id, org_slug)
        VALUES
            (:fid, :trigger, 'survey', :stype, :score, :scale,
             :user_id, :thread_id, :correlation_id, :org_slug)
        """,
        _DB,
        params={
            "fid": fid,
            "trigger": trigger,
            "stype": survey_type,
            "score": score,
            "scale": SCORE_SCALES[survey_type],
            "user_id": user_id,
            "thread_id": thread_id,
            "correlation_id": correlation_id,
            "org_slug": org_slug,
        },
    )
    if _err_code(result) is not None:
        _handle_err(result, "survey score")
        return None
    return fid


def close_signals(*, category: str, module: str | None = None, before: str | None = None) -> int:
    """Mark matching open signals as closed — the drain for the weekly refresh
    sweep (and any backlog-clearing). Encapsulates the status flip so callers
    never UPDATE the table directly. Returns rows affected (0 on DB-down in dev).

    e.g. after refreshing the chat doc:
        close_signals(category="doc_stale", module="chat")
    """
    conds = ["category = :cat", "status <> 'closed'"]
    params: dict[str, Any] = {"cat": category}
    if module:
        conds.append("area_tags ? :mod")
        params["mod"] = module
    if before:
        conds.append("created_at <= :before")
        params["before"] = before
    result = db_execute(
        f"UPDATE product_feedback SET status='closed', updated_at=now() WHERE {' AND '.join(conds)}",
        _DB,
        params=params,
    )
    if _err_code(result) is not None:
        _handle_err(result, "close signals")
        return 0
    return int(result.get("rows_affected") or 0)


def log_event(
    *,
    trigger: str,
    action: str,
    user_id: str | None = None,
    thread_id: str | None = None,
    kind: str | None = None,
    category: str | None = None,
    score: float | None = None,
    feedback_id: str | None = None,
) -> None:
    """Append a funnel event (shown/opened/scored/submitted/dismissed/…)."""
    result = db_execute(
        """
        INSERT INTO feedback_prompt_events
            (user_id, thread_id, trigger, kind, action, category, score, feedback_id)
        VALUES (:user_id, :thread_id, :trigger, :kind, :action, :category, :score, :fid)
        """,
        _DB,
        params={
            "user_id": user_id,
            "thread_id": thread_id,
            "trigger": trigger,
            "kind": kind,
            "action": action,
            "category": category,
            "score": score,
            "fid": feedback_id,
        },
    )
    if _err_code(result) is not None:
        _handle_err(result, "feedback event")


# ── prompt state ────────────────────────────────────────────────────────────

def get_prompt_state(user_id: str) -> dict[str, Any]:
    """Return the cadence row for a user, or defaults if none/unavailable."""
    default = {
        "user_id": user_id,
        "threads_since_prompt": 0,
        "turns_since_prompt": 0,
        "last_prompted_at": None,
        "last_captured_at": None,
        "last_csat_at": None,
        "last_nps_at": None,
        "snooze_until": None,
        "opted_out": False,
        "prompt_count": 0,
        "capture_count": 0,
    }
    if not user_id:
        return default
    result = db_query(
        "SELECT * FROM feedback_prompt_state WHERE user_id = :uid",
        _DB,
        params={"uid": user_id},
    )
    if _err_code(result) is not None:
        return default
    rows = result.get("rows") or []
    if not rows:
        return default
    cols = result.get("columns") or []
    return dict(zip(cols, rows[0]))


def bump_counters(user_id: str, *, threads: int = 0, turns: int = 0) -> None:
    """Increment the since-prompt counters, creating the row if needed."""
    if not user_id:
        return
    result = db_execute(
        """
        INSERT INTO feedback_prompt_state (user_id, threads_since_prompt, turns_since_prompt, updated_at)
        VALUES (:uid, :threads, :turns, now())
        ON CONFLICT (user_id) DO UPDATE SET
            threads_since_prompt = feedback_prompt_state.threads_since_prompt + :threads,
            turns_since_prompt   = feedback_prompt_state.turns_since_prompt + :turns,
            updated_at = now()
        """,
        _DB,
        params={"uid": user_id, "threads": threads, "turns": turns},
    )
    if _err_code(result) is not None:
        _handle_err(result, "cadence counters")


def mark_prompted(user_id: str, *, kind: str) -> None:
    """Record that an ask was shown: reset the relevant counters/clocks."""
    if not user_id:
        return
    clock = "last_csat_at = now()," if kind == "csat" else "last_nps_at = now()," if kind == "nps" else ""
    result = db_execute(
        f"""
        INSERT INTO feedback_prompt_state (user_id, last_prompted_at, prompt_count, updated_at)
        VALUES (:uid, now(), 1, now())
        ON CONFLICT (user_id) DO UPDATE SET
            last_prompted_at = now(),
            {clock}
            threads_since_prompt = 0,
            turns_since_prompt = 0,
            prompt_count = feedback_prompt_state.prompt_count + 1,
            updated_at = now()
        """,
        _DB,
        params={"uid": user_id},
    )
    if _err_code(result) is not None:
        _handle_err(result, "mark prompted")


def mark_captured(user_id: str) -> None:
    if not user_id:
        return
    result = db_execute(
        """
        INSERT INTO feedback_prompt_state (user_id, last_captured_at, capture_count, updated_at)
        VALUES (:uid, now(), 1, now())
        ON CONFLICT (user_id) DO UPDATE SET
            last_captured_at = now(),
            capture_count = feedback_prompt_state.capture_count + 1,
            updated_at = now()
        """,
        _DB,
        params={"uid": user_id},
    )
    if _err_code(result) is not None:
        _handle_err(result, "mark captured")


def snooze(user_id: str, *, threads: int | None = None) -> None:
    """Push the next possible ask out by N thread-equivalents (dismiss ladder)."""
    if not user_id:
        return
    n = SNOOZE_ON_DISMISS if threads is None else threads
    # Approximate a thread-count snooze as a time snooze so it survives restarts.
    until = datetime.now(timezone.utc) + timedelta(days=max(1, n))
    result = db_execute(
        """
        INSERT INTO feedback_prompt_state (user_id, snooze_until, updated_at)
        VALUES (:uid, :until, now())
        ON CONFLICT (user_id) DO UPDATE SET snooze_until = :until, updated_at = now()
        """,
        _DB,
        params={"uid": user_id, "until": until.isoformat()},
    )
    if _err_code(result) is not None:
        _handle_err(result, "snooze")


def set_opt_out(user_id: str, opted_out: bool) -> None:
    if not user_id:
        return
    result = db_execute(
        """
        INSERT INTO feedback_prompt_state (user_id, opted_out, updated_at)
        VALUES (:uid, :opt, now())
        ON CONFLICT (user_id) DO UPDATE SET opted_out = :opt, updated_at = now()
        """,
        _DB,
        params={"uid": user_id, "opt": opted_out},
    )
    if _err_code(result) is not None:
        _handle_err(result, "opt out")


# ── the cadence decision (pure) ─────────────────────────────────────────────

def _as_dt(v: Any) -> datetime | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    try:
        s = str(v).replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def evaluate_cadence(
    state: dict[str, Any],
    *,
    user_id: str,
    thread_turns: int,
    last_turn_failed: bool,
    nudged_this_thread: bool,
    just_rated: bool,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """Decide whether an ask is *eligible* this turn, and which instrument.

    Pure: no DB, no clock unless injected. Returns None (nothing to offer) or
    ``{"trigger": "periodic", "kind": ..., "reason": ...}``. Priority when
    several are eligible: nps > csat > open (explicit/inline are handled upstream
    by the planner selecting the tool). This is the hard ceiling — the model
    only ever decides *whether* to surface what this function deems eligible.
    """
    now = now or datetime.now(timezone.utc)

    if state.get("opted_out"):
        return None
    snooze_until = _as_dt(state.get("snooze_until"))
    if snooze_until and snooze_until > now:
        return None
    if nudged_this_thread or just_rated:
        return None

    # nps_relationship — time-boxed, sampled, never right after a miss, and
    # never on an opening turn (a relationship survey needs real engagement;
    # requiring a substantive thread also prevents NPS on a brand-new user's
    # first message). Cross-session tenure gating is a follow-up (spec §12).
    last_nps = _as_dt(state.get("last_nps_at"))
    nps_due = last_nps is None or (now - last_nps) >= timedelta(days=NPS_INTERVAL_DAYS)
    if nps_due and not last_turn_failed and thread_turns >= CSAT_MIN_TURNS:
        if _deterministic_fraction(user_id, "nps", now.strftime("%Y-%m")) < NPS_SAMPLE_RATE:
            return {"trigger": "periodic", "kind": "nps",
                    "reason": f"no NPS in {NPS_INTERVAL_DAYS}d"}

    # csat_thread — after a substantive, successful thread; sampled
    if thread_turns >= CSAT_MIN_TURNS and not last_turn_failed:
        if _deterministic_fraction(user_id, "csat", str(thread_turns)) < CSAT_SAMPLE_RATE:
            return {"trigger": "periodic", "kind": "csat",
                    "reason": f"resolved thread ({thread_turns} turns)"}

    # open_periodic — every N threads/turns
    threads_since = int(state.get("threads_since_prompt") or 0)
    turns_since = int(state.get("turns_since_prompt") or 0)
    if threads_since >= CADENCE_THREADS or turns_since >= CADENCE_TURNS:
        kind = "targeted_miss" if last_turn_failed else "generic"
        return {"trigger": "periodic", "kind": kind,
                "reason": f"{threads_since} threads / {turns_since} turns since last ask"}

    return None
