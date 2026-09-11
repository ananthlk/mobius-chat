"""The A/B harness endpoint — comparisons the human judges.

PRODUCTION ROUTES; THE HARNESS FORKS. A live turn goes to exactly one
orchestrator -- two writers sharing state is the defect this program removed
four times. The harness deliberately runs every arm on identical input because
NOBODY IS BEING SERVED. Nothing here is a precedent for forking a live turn.

WHY THIS EXISTS AT ALL: the fleet has zero golden fixtures, recall has no
measure, tool-exposure AFFECTS.quality is UNMEASURED, and the groundedness floor
never runs on agentic. A person reading two renderings side by side is the only
quality signal that exists -- not a fallback for when a metric is unavailable,
but the absence of any metric for the term the promise calls quality.

Built to the FE seat's render contract (docs/AB_HARNESS_FE_RENDER_CONTRACT.md).
"""

from __future__ import annotations

import json
import logging
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

logger = logging.getLogger(__name__)
router = APIRouter()

_DB = "chat"   # db_client's LOGICAL key -- see promise.py:39
_SETS = Path(__file__).resolve().parent.parent.parent / "eval"


def _q(sql: str, params: dict | None = None) -> list[dict]:
    from app.db_client import db_query
    res = db_query(sql, _DB, params=params or {})
    cols = (res or {}).get("columns") or []
    return [dict(zip(cols, r, strict=False)) for r in ((res or {}).get("rows") or [])]


def _x(sql: str, params: dict | None = None) -> dict:
    from app.db_client import db_execute
    return db_execute(sql, _DB, params=params or {}) or {}


def _load_set(set_id: str) -> dict:
    p = _SETS / f"{set_id}.json"
    if not p.exists():
        raise HTTPException(404, f"question set {set_id!r} not found")
    return json.loads(p.read_text())


# ── create ──────────────────────────────────────────────────────────────────

class CreateRun(BaseModel):
    set_id: str = "ab_question_set_v1"
    arms: list[dict] | None = None      # [{id,label}] -- 1..N, columns = len(arms)
    held_constant: list[str] | None = None
    varied: list[str] | None = None
    created_by: str | None = None


@router.post("/ab/runs")
def create_run(body: CreateRun) -> dict:
    """A run is a RECORD, not a one-off. Without it every comparison is
    unreproducible the day after it is looked at."""
    qset = _load_set(body.set_id)
    arms = body.arms or [{"id": "v1", "label": "v1 orchestrator"}]
    if not arms:
        raise HTTPException(400, "a run needs at least one arm")
    run_id = f"ab-{uuid.uuid4().hex[:10]}"
    held = body.held_constant or ["question", f"mode:{qset.get('mode','copilot')}", "input"]
    varied = body.varied or (["orchestrator"] if len(arms) > 1 else [])

    _x("""INSERT INTO ab_runs (run_id, set_id, arm_a_id, arm_a_label, arm_b_id,
                               arm_b_label, held_constant, varied, created_by)
          VALUES (:r,:s,:aid,:alab,:bid,:blab,CAST(:h AS JSONB),CAST(:v AS JSONB),:by)""",
       {"r": run_id, "s": body.set_id,
        "aid": arms[0]["id"], "alab": arms[0].get("label", arms[0]["id"]),
        "bid": arms[1]["id"] if len(arms) > 1 else "",
        "blab": arms[1].get("label", "") if len(arms) > 1 else "",
        "h": json.dumps(held), "v": json.dumps(varied), "by": body.created_by})

    for q in qset.get("questions", []):
        for a in arms:
            _x("""INSERT INTO ab_run_questions (run_id, question_id, arm_id, status)
                  VALUES (:r,:q,:a,'pending')
                  ON CONFLICT (run_id, question_id, arm_id) DO NOTHING""",
               {"r": run_id, "q": q["id"], "a": a["id"]})
    return {"run_id": run_id, "set_id": body.set_id,
            "arms": arms, "questions": len(qset.get("questions", []))}


# ── read ────────────────────────────────────────────────────────────────────

@router.get("/ab/runs")
def list_runs(limit: int = 25) -> dict:
    return {"runs": _q("""select run_id, set_id, arm_a_id, arm_b_id, held_constant,
                                 varied, created_by, created_at, status
                            from ab_runs order by created_at desc limit :l""",
                       {"l": limit})}


@router.get("/ab/runs/{run_id}")
def get_run(run_id: str) -> dict:
    runs = _q("select * from ab_runs where run_id=:r", {"r": run_id})
    if not runs:
        raise HTTPException(404, f"run {run_id!r} not found")
    run = runs[0]
    qset = _load_set(run["set_id"])
    status = {(r["question_id"], r["arm_id"]): r["status"]
              for r in _q("""select question_id, arm_id, status from ab_run_questions
                              where run_id=:r""", {"r": run_id})}
    return {
        "run_id": run_id, "set_id": run["set_id"], "created": str(run["created_at"]),
        "harness": True,
        "questions": [
            {"id": q["id"], "q": q["q"], "shape": q.get("shape"),
             "status": {a: s for (qq, a), s in status.items() if qq == q["id"]}}
            for q in qset.get("questions", [])
        ],
    }


def _trace(correlation_id: str, arm: str) -> list[dict]:
    """One turn's rounds, read two ways.

    v1 and v2 rows are the SAME turn_rounds rows: v1's decision is the
    v1_directive/v1_reason columns, v2's is posture/directive/rationale. Two
    views of one source -- never a second array, which would drift the first
    time someone edited one and not the other.

    `verdict` is read, never recomputed. A posture-vs-posture diff at render
    time would be a SECOND MAPPING, and it would disagree with compare() exactly
    where compare() is most interesting: on `extend`, which resolves by reason
    rather than by name.
    """
    rows = _q("""select round_index, posture, directive, gap_targeted, rationale,
                        gaps_opened, gaps_closed, v1_directive, v1_reason,
                        v1_maps_to, shadow_verdict, tool_called, tools_offered,
                        overran, delivered_latency_s
                   from turn_rounds where correlation_id=:c
                  order by round_index""", {"c": correlation_id})
    out = []
    for r in rows:
        if arm == "v1":
            out.append({"round_n": r["round_index"],
                        "posture": None,                 # v1 has no posture vocabulary
                        "directive": r["v1_directive"],
                        "rationale": r["v1_reason"],
                        "tool_called": r["tool_called"],
                        "gaps_opened": [], "gaps_closed": [],
                        "verdict": None})                # a verdict is ABOUT v2, not v1
        else:
            out.append({"round_n": r["round_index"],
                        "posture": r["posture"], "directive": r["directive"],
                        "gap_targeted": r["gap_targeted"], "rationale": r["rationale"],
                        "gaps_opened": r["gaps_opened"] or [],
                        "gaps_closed": r["gaps_closed"] or [],
                        "v1_directive": r["v1_directive"], "v1_reason": r["v1_reason"],
                        "v1_maps_to": r["v1_maps_to"],
                        "verdict": r["shadow_verdict"],
                        "tool_called": r["tool_called"],
                        "overran": bool(r["overran"])})
    return out


@router.get("/ab/runs/{run_id}/q/{qid}")
def get_comparison(run_id: str, qid: str) -> dict:
    runs = _q("select * from ab_runs where run_id=:r", {"r": run_id})
    if not runs:
        raise HTTPException(404, f"run {run_id!r} not found")
    run = runs[0]
    qset = _load_set(run["set_id"])
    q = next((x for x in qset.get("questions", []) if x["id"] == qid), None)
    if q is None:
        raise HTTPException(404, f"question {qid!r} not in set {run['set_id']!r}")

    per_arm = _q("""select arm_id, correlation_id, status, error, answer_envelope,
                           envelope_captured_at, delivered
                      from ab_run_questions where run_id=:r and question_id=:q""",
                 {"r": run_id, "q": qid})

    arms: dict[str, Any] = {}
    for a in per_arm:
        cid = a["correlation_id"]
        att = _q("""select promise_version, tier, promised_latency_s, delivered_latency_s,
                           delivered_cost_c, outcome, worker_latency_s
                      from turn_attestations where correlation_id=:c""",
                 {"c": cid})[0] if cid else {}
        delivered_s = att.get("delivered_latency_s")
        promised_s = att.get("promised_latency_s")
        arms[a["arm_id"]] = {
            # The REAL assistant_envelope, verbatim {version, blocks}, from the
            # SNAPSHOT -- /chat/response is a Redis key with a TTL and returns
            # {"status":"processing"} forever once it expires. A comparison you
            # cannot re-open is not infrastructure.
            "answer_envelope": a["answer_envelope"],
            "envelope_captured_at": str(a["envelope_captured_at"] or "") or None,
            "decision_trace": _trace(cid, a["arm_id"]) if cid else [],
            "delivered": {
                "latency_ms": int(delivered_s * 1000) if delivered_s is not None else None,
                "cost_usd": att.get("delivered_cost_c"),
                "exit_mode": att.get("outcome"),
                "rounds": len(_trace(cid, a["arm_id"])) if cid else None,
            },
            "promised": {"latency_ms": int(promised_s * 1000) if promised_s is not None else None,
                         "promise_version": att.get("promise_version"),
                         "tier": att.get("tier")},
            # null renders "—", never "false" or "0". Unset is not false.
            "kept": (delivered_s <= promised_s) if (delivered_s is not None and promised_s is not None) else None,
            "status": a["status"], "error": a["error"],
        }

    arm_list = [{"id": run["arm_a_id"], "label": run["arm_a_label"]}]
    if run.get("arm_b_id"):
        arm_list.append({"id": run["arm_b_id"], "label": run["arm_b_label"]})

    return {
        "run_id": run_id,
        "question": {"id": q["id"], "q": q["q"], "shape": q.get("shape"),
                     "mode": qset.get("mode")},
        "harness": True,
        "experiment": {
            "arms": arm_list,
            # Rule 5 as DATA, so the page renders it as the header of every
            # comparison. A comparison without this stated is a shape with no
            # experiment behind it -- two claims were withdrawn in this program
            # for exactly that omission.
            "held_constant": run["held_constant"] or [],
            "varied": run["varied"] or [],
        },
        "arms": arms,
    }
