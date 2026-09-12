"""Persist the v2 contract, and the thread's evidence ledger.

Ananth, 2026-09-12: "this is the most critical thing we can build".

TWO WRITES, TWO LIFETIMES:

  react_v2_rounds   append-only. What react claimed, at a round. A later
                    correction is a NEW row -- the disagreement between rows is
                    the telemetry, so nothing here is ever updated.
  thread_evidence   current state per (thread, source). Updated in place: "we
                    already decided this document is useless" is a fact about
                    now. It is what stops us re-sending -- a fact is ~100 chars
                    to carry, the passage it came from ~9,000.

FAIL-SOFT BUT NEVER SILENT. A telemetry write must not kill a turn; a
telemetry write that fails without a trace is worse than no write, because the
table then reads as "this did not happen". Every failure logs at WARNING with
the correlation id.
"""

from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)


# The same transport v2/ledger.py already uses. A second DB accessor in one
# module family is a second place for the connection story to drift.
# "chat", NOT "mobius_chat". This is db_client's logical db NAME, and
# v2/ledger.py:33 already uses it. I wrote the physical database name first --
# a name chosen from intent rather than from the caller that resolves it, which
# is the exact defect toolreg/db.py's header documents at length.
_DB = "chat"


def save_round(row: dict) -> bool:
    """One react round. Returns whether it was written -- callers that ignore
    this get fail-soft, callers that check it can assert a real write."""
    try:
        from app.db_client import db_execute
        res = db_execute("""
                INSERT INTO react_v2_rounds
                  (correlation_id, round_index, thread_id, contract_version,
                   shape_seen, thought, roles_assumed, facts, not_useful, gaps,
                   running_answer, answer_chars, tool_request, tool_reason,
                   is_complete, complete_why, next_round_worth_it,
                   next_round_why, problems)
                VALUES (%(cid)s,%(rn)s,%(tid)s,%(ver)s,%(shape)s,%(thought)s,
                        %(roles)s::jsonb,%(facts)s::jsonb,%(nu)s::jsonb,
                        %(gaps)s::jsonb,%(ra)s,%(ac)s,%(treq)s,%(trea)s,
                        %(ic)s,%(icw)s,%(nrw)s,%(nrwy)s,%(prob)s::jsonb)
                ON CONFLICT (correlation_id, round_index) DO NOTHING
            """, _DB, params={
                "cid": row.get("correlation_id"), "rn": row.get("round_index"),
                "tid": row.get("thread_id"), "ver": row.get("contract_version"),
                "shape": row.get("shape_seen"), "thought": row.get("thought"),
                # json.dumps, not the list: db_execute serialises its params for
                # the db-agent transport and a raw list arrives as a Postgres
                # array literal, not JSONB. Same lesson as the ledger, one
                # table over.
                "roles": json.dumps(row.get("roles_assumed") or []),
                "facts": json.dumps(row.get("facts") or []),
                "nu": json.dumps(row.get("not_useful") or []),
                "gaps": json.dumps(row.get("gaps") or []),
                "ra": row.get("running_answer"), "ac": row.get("answer_chars"),
                "treq": row.get("tool_request"), "trea": row.get("tool_reason"),
                "ic": row.get("is_complete"), "icw": row.get("complete_why"),
                "nrw": row.get("next_round_worth_it"),
                "nrwy": row.get("next_round_why"),
                "prob": json.dumps(row.get("problems") or []),
            })
        return not (res or {}).get("error")
    except Exception as e:                    # pragma: no cover — real DB path
        logger.warning("[v2.store] round write failed cid=%s round=%s: %s",
                       str(row.get("correlation_id"))[:8],
                       row.get("round_index"), e)
        return False


def record_evidence(thread_id: str, *, correlation_id: str = "",
                    useful=(), not_useful=()) -> int:
    """Upsert this thread's verdicts. Returns rows written.

    A source moving from not_useful to useful OVERWRITES -- the later verdict
    is the one made with more context. The history of the change is in
    react_v2_rounds, which is why this table can be a current-state table
    without losing anything.
    """
    rows = 0
    items = ([(f, "useful") for f in (useful or ())]
             + [(f, "not_useful") for f in (not_useful or ())])
    if not items:
        return 0
    try:
        from app.db_client import db_execute
        for item, verdict in items:
            doc, page, fact = _unpack(item)
            if not doc:
                continue
            db_execute("""
                    INSERT INTO thread_evidence
                      (thread_id, document_name, page_number, verdict, fact,
                       correlation_id)
                    VALUES (%(tid)s,%(doc)s,%(pg)s,%(v)s,%(fact)s,%(cid)s)
                    ON CONFLICT (thread_id, document_name,
                                 COALESCE(page_number, -1))
                    DO UPDATE SET verdict = EXCLUDED.verdict,
                                  fact = COALESCE(NULLIF(EXCLUDED.fact,''),
                                                  thread_evidence.fact),
                                  last_seen = now(),
                                  times_seen = thread_evidence.times_seen + 1,
                                  correlation_id = EXCLUDED.correlation_id
                """, _DB, params={"tid": thread_id, "doc": doc, "pg": page,
                                  "v": verdict, "fact": fact,
                                  "cid": correlation_id})
            rows += 1
    except Exception as e:                    # pragma: no cover — real DB path
        logger.warning("[v2.store] evidence write failed thread=%s: %s",
                       str(thread_id)[:12], e)
    return rows


def _unpack(item) -> tuple[str, int | None, str]:
    """(document, page, fact) from a Fact, a dict, or a 'name p12' string.

    The string form exists because that is what the frame already carries, and
    a store that only accepted the new shape would record nothing until every
    producer migrated.
    """
    if isinstance(item, dict):
        pg = item.get("page")
        try:
            pg = int(pg) if pg is not None else None
        except (TypeError, ValueError):
            pg = None
        return (str(item.get("document") or "").strip(), pg,
                str(item.get("fact") or "").strip())
    for attr in ("document",):
        if hasattr(item, attr):
            return (str(getattr(item, "document") or "").strip(),
                    getattr(item, "page", None),
                    str(getattr(item, "fact", "") or "").strip())
    text = str(item or "").strip()
    if not text:
        return "", None, ""
    # "Sunshine Provider Manual p38" -> ("Sunshine Provider Manual", 38)
    if " p" in text:
        head, _, tail = text.rpartition(" p")
        num = tail.split("/")[0].strip().rstrip(",.;")
        if num.isdigit():
            return head.strip(), int(num), ""
    return text, None, ""


def load_evidence(thread_id: str) -> tuple[list[dict], list[str]]:
    """(useful_facts, not_useful_sources) for a thread.

    THIS IS THE READ THAT MAKES THE WRITE WORTH ANYTHING. Called before a turn
    so known facts are re-sent as text instead of passages, and known-useless
    sources are not retrieved again. A write path with no reader is the defect
    this codebase has produced twelve times this session.
    """
    try:
        from app.db_client import db_query
        res = db_query("""
                SELECT document_name, page_number, verdict, fact
                FROM thread_evidence
                WHERE thread_id = %(tid)s
                ORDER BY last_seen DESC
                LIMIT 200
            """, _DB, params={"tid": thread_id}, max_rows=200)
        rows = (res or {}).get("rows") or []
    except Exception as e:                    # pragma: no cover — real DB path
        logger.warning("[v2.store] evidence read failed thread=%s: %s",
                       str(thread_id)[:12], e)
        return [], []

    useful, not_useful = [], []
    for r in rows:
        if isinstance(r, dict):
            doc, page = r.get("document_name"), r.get("page_number")
            verdict, fact = r.get("verdict"), r.get("fact")
        else:
            doc, page, verdict, fact = r
        label = f"{doc} p{page}" if page is not None else str(doc)
        if verdict == "useful":
            useful.append({"document": doc, "page": page,
                           "fact": fact or "", "label": label})
        else:
            not_useful.append(label)
    return useful, not_useful
