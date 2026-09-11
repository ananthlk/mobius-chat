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
import os
import threading
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

    # The set is read from disk ONCE, here, and frozen onto the run. Every read
    # after this is from the snapshot: editing eval/<set_id>.json must not
    # retroactively rewrite what an already-created run asked.
    _x("""INSERT INTO ab_runs (run_id, set_id, arm_a_id, arm_a_label, arm_b_id,
                               arm_b_label, held_constant, varied, created_by,
                               question_set)
          VALUES (:r,:s,:aid,:alab,:bid,:blab,CAST(:h AS JSONB),CAST(:v AS JSONB),:by,
                  CAST(:qs AS JSONB))""",
       {"r": run_id, "s": body.set_id, "qs": json.dumps(qset),
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


def _run_set(run: dict) -> dict:
    """The FROZEN set, from the run row.

    Never _load_set() on a read path. A run created before this column existed
    has no snapshot and says so -- it does not silently fall back to whatever
    the file says today, because that is the drift this column exists to stop.
    """
    qs = run.get("question_set")
    if qs is None:
        raise HTTPException(409, f"run {run['run_id']!r} predates the question-set "
                                 "snapshot; its questions are not reproducible")
    return json.loads(qs) if isinstance(qs, str) else qs

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
    qset = _run_set(run)
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
                        overran, round_duration_s, applied_directive,
                        v2_applied, prompt_mismatch, decision_inputs, framing_inputs
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
                        "round_duration_s": r["round_duration_s"],
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
                        # What the executor RAN, distinct from the posture it
                        # would have chosen: on a routed turn the exit mode may
                        # have overridden the posture, and that override is the
                        # interesting row.
                        "applied_directive": r["applied_directive"],
                        "v2_applied": bool(r["v2_applied"]),
                        # PROSE, for a human to read — not a name. The
                        # posture is in `posture`; the executed directive is in
                        # `applied_directive`. A renderer that fills a
                        # "v2 chose ___" slot from this string prints a
                        # paragraph where a word belongs.
                        "prompt_mismatch": r["prompt_mismatch"],
                        # THE THINKING. Every input the decision was made from
                        # — open gaps with age/levers/payload checks, the
                        # budget arithmetic and its shortfall, the branch of
                        # select() that fired, every gating predicate. Without
                        # it the trace shows a verdict nobody can argue with.
                        "decision_inputs": r["decision_inputs"],
                        # What the governor would decide once round N's gaps
                        # exist — the same round, one moment later, before its
                        # tool runs. Null until the framing hook has data.
                        "framing_inputs": r["framing_inputs"],
                        # ...and the two mismatches are NOT the same event,
                        # which the single prose field could not tell anyone:
                        #
                        #   mis_prompted   v2 DID run the round, with v1's
                        #                  remediation prompt instead of a
                        #                  gathering one. Not like-for-like.
                        #   not_generated  v2 chose a posture (ALTERNATIVES)
                        #                  whose content v1 cannot produce, so
                        #                  it shipped without it. The person
                        #                  saw a normal answer.
                        #
                        # Derived here from stored fields rather than stored as
                        # a fourth column: it is one rule over two values, and
                        # it has exactly one author. Asking the page to infer
                        # it from the prose would make the renderer the second.
                        "mismatch_kind": (
                            None if not r["prompt_mismatch"]
                            else "mis_prompted" if r["applied_directive"] == "extend"
                            else "not_generated"
                        ),
                        "tool_called": r["tool_called"],
                        "round_duration_s": r["round_duration_s"],
                        "overran": bool(r["overran"])})
    return out


@router.get("/ab/runs/{run_id}/q/{qid}")
def get_comparison(run_id: str, qid: str) -> dict:
    runs = _q("select * from ab_runs where run_id=:r", {"r": run_id})
    if not runs:
        raise HTTPException(404, f"run {run_id!r} not found")
    run = runs[0]
    qset = _run_set(run)
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
            # The turn this arm ran. Absent until now, so nothing could link a
            # comparison back to its own telemetry -- I had to join
            # ab_run_questions by hand to read my own decision traces.
            "correlation_id": cid,
            "answer_envelope": a["answer_envelope"],
            "envelope_captured_at": str(a["envelope_captured_at"] or "") or None,
            "decision_trace": _trace(cid, a["arm_id"]) if cid else [],
            "delivered": {
                "latency_ms": int(delivered_s * 1000) if delivered_s is not None else None,
                # CENTS, and the name says so. Chat FE found this reading
                # `cost_usd` off a column whose `_c` suffix means cents: a
                # $0.012 turn would have rendered as "1.2", 100x off, and it is
                # NULL on every row today so nothing would have looked wrong
                # until the first real value.
                #
                # RENAMED rather than converted. Every cost in this system is
                # cents -- promised_cost_c, delivered_cost_c, Budget.remaining_c,
                # RoundCost -- so a single USD field here would be the only
                # place a unit changes, and the next reader would have to know
                # which side of that boundary they were on. One unit, named
                # where it is read. Third name-that-lies defect today, after
                # delivered_latency_s (a start time) and shadow_verdict
                # (two questions in one enum).
                "cost_cents": att.get("delivered_cost_c"),
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


# ── capture ─────────────────────────────────────────────────────────────────

class Capture(BaseModel):
    correlation_id: str


@router.post("/ab/runs/{run_id}/q/{qid}/arm/{arm}")
def capture(run_id: str, qid: str, arm: str, body: Capture) -> dict:
    """Freeze one arm's answer onto the run, at completion time.

    The snapshot exists because /chat/response is a Redis key with a TTL: once
    it expires the same call returns {"status":"processing"} forever, and a
    comparison you cannot re-open is not infrastructure. So the capture is
    SERVER-side and reads the same payload the UI reads, through the same
    function -- a second envelope builder here would drift from the renderer
    the first time either changed.

    A turn that is not `completed` is a 409 and writes NOTHING. A half-written
    row with a null envelope is indistinguishable from a turn that answered
    with nothing, which is the precise confusion the `kept`-is-None rule exists
    to prevent.
    """
    if not _q("select run_id from ab_runs where run_id=:r", {"r": run_id}):
        raise HTTPException(404, f"run {run_id!r} not found")

    from app.api.chat import get_chat_response
    payload = get_chat_response(body.correlation_id)
    if (payload or {}).get("status") != "completed":
        raise HTTPException(409, f"turn {body.correlation_id!r} is "
                                 f"{(payload or {}).get('status')!r}, not completed")
    env = payload.get("assistant_envelope")

    _x("""INSERT INTO ab_run_questions
              (run_id, question_id, arm_id, correlation_id, status,
               answer_envelope, envelope_captured_at, delivered)
          VALUES (:r,:q,:a,:c,'captured',CAST(:e AS JSONB), now(), CAST(:d AS JSONB))
          ON CONFLICT (run_id, question_id, arm_id) DO UPDATE SET
              correlation_id = EXCLUDED.correlation_id,
              status         = EXCLUDED.status,
              answer_envelope= EXCLUDED.answer_envelope,
              envelope_captured_at = EXCLUDED.envelope_captured_at,
              delivered      = EXCLUDED.delivered""",
       {"r": run_id, "q": qid, "a": arm, "c": body.correlation_id,
        "e": json.dumps(env), "d": json.dumps({})})

    # Read back in the same change. A write path with no reader is how the
    # attestation shipped an empty table behind 21 green tests.
    got = _q("""select answer_envelope is not null as has_env, status
                  from ab_run_questions
                 where run_id=:r and question_id=:q and arm_id=:a""",
             {"r": run_id, "q": qid, "a": arm})
    if not got:
        raise HTTPException(500, "capture wrote no row")
    return {"run_id": run_id, "question_id": qid, "arm": arm,
            "correlation_id": body.correlation_id,
            "envelope_captured": bool(got[0]["has_env"]), "status": got[0]["status"]}


# ── the simultaneous fork ───────────────────────────────────────────────────

class Fork(BaseModel):
    mode: str | None = None          # defaults to the question set's mode
    arms: list[str] | None = None    # defaults to the run's arms


@router.post("/ab/runs/{run_id}/q/{qid}/fork")
def fork(run_id: str, qid: str, body: Fork) -> dict:
    """Run every arm on one question AT THE SAME TIME, and return both turn ids
    before either has finished.

    WHY THIS EXISTS, in one screen: q20 of run ab-4640b78180 came back with
    `Divergences · 0` -- the governor made identical decisions on both arms,
    same postures, same rounds, same tools, same durations -- and two
    completely different answers. One cited eight sources and answered; the
    other refused on jurisdiction. The arms had run twenty minutes apart.

    Sequential arms cannot separate "the governor decided better" from "the
    model rolled differently". Every latency number on that row was
    contaminated by a gap I introduced myself by flipping an env var between
    passes. Ananth: "simultaneous and onscreen rendering is important -- that's
    how I would know what works and how."

    WHAT IS ACTUALLY HELD CONSTANT BY RUNNING TOGETHER: the corpus at this
    instant, the tool manifest, the model roster and whatever the bandit is
    currently exploring, cache state, and load. None of those are constant
    across twenty minutes, and none of them were held before.

    WHAT IS STILL NOT: the model's own sampling. Two calls in the same second
    still roll differently. Simultaneity removes the CONFOUND; it does not
    remove the variance, and a single fork remains one sample of two.

    TWO WRITERS -- and why this is not that defect. My charter says no turn is
    ever handled by both orchestrators, because two of them sharing write state
    is the two-writer defect at maximum scale. Here there are TWO TURNS: two
    correlation_ids, two fresh threads, two attestations, two sets of round
    rows. Nothing is shared and nobody is being served twice. The harness forks
    precisely because there is no user on the other end of it.

    FRESH THREAD PER ARM, deliberately: the same thread would make the second
    arm a follow-up rather than a comparison.

    Ids are minted HERE and returned immediately, so the page can open both SSE
    streams before either turn has produced a token -- which is the difference
    between watching an A/B and reading one afterwards.
    """
    runs = _q("select * from ab_runs where run_id=:r", {"r": run_id})
    if not runs:
        raise HTTPException(404, f"run {run_id!r} not found")
    run = runs[0]
    qset = _run_set(run)
    q = next((x for x in qset.get("questions", []) if x["id"] == qid), None)
    if q is None:
        raise HTTPException(404, f"question {qid!r} not in set {run['set_id']!r}")

    if os.environ.get("MOBIUS_V2_AB_FORK", "").strip() != "1":
        # A 409, not a silent single-arm run. A fork that quietly degrades to
        # one arm returns a comparison with one column and no explanation --
        # absence dressed as a result, which this program has now found nine
        # times.
        raise HTTPException(
            409, "MOBIUS_V2_AB_FORK is not enabled on this service; the arm "
                 "pin would be ignored and both arms would run on whatever "
                 "MOBIUS_V2_PCT routes them to")

    arms = body.arms or [a for a in (run["arm_a_id"], run.get("arm_b_id")) if a]
    mode = body.mode or qset.get("mode") or "copilot"
    launched: dict[str, str] = {}
    errors: dict[str, str] = {}

    def _launch(arm: str) -> None:
        cid = str(uuid.uuid4())
        try:
            _post_chat(q["q"], mode, arm, cid)
            launched[arm] = cid
        except Exception as exc:          # pragma: no cover
            errors[arm] = str(exc)[:200]

    # Threads, not a loop: a loop is the sequential design this endpoint exists
    # to replace. They are started as close together as the runtime allows and
    # the POST itself only enqueues, so the two turns land in the same second.
    threads = [threading.Thread(target=_launch, args=(a,), daemon=True) for a in arms]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    for arm, cid in launched.items():
        _x("""INSERT INTO ab_run_questions (run_id, question_id, arm_id,
                                            correlation_id, status)
              VALUES (:r,:q,:a,:c,'running')
              ON CONFLICT (run_id, question_id, arm_id) DO UPDATE SET
                  correlation_id = EXCLUDED.correlation_id,
                  status = 'running'""",
           {"r": run_id, "q": qid, "a": arm, "c": cid})

    return {
        "run_id": run_id, "question_id": qid, "question": q["q"], "mode": mode,
        # {arm: correlation_id} — open /chat/stream/{cid} on each, now.
        "launched": launched,
        "errors": errors or None,
        "simultaneous": True,
        # Stated on the payload so the page can render it rather than the
        # reader having to remember it.
        "held_constant_by_running_together": [
            "corpus state", "tool manifest", "model roster / bandit state",
            "cache state", "service load", "wall-clock time",
        ],
        "still_varied": [
            "the orchestrator decision (the point of the experiment)",
            "LLM sampling — simultaneity removes the confound, not the variance",
        ],
    }


def _post_chat(message: str, mode: str, arm: str, correlation_id: str) -> None:
    """Enqueue one arm's turn. Pins the arm; a FRESH thread each time."""
    import urllib.request

    base = os.environ.get("MOBIUS_SELF_URL", "http://127.0.0.1:8080").rstrip("/")
    token = os.environ.get("MOBIUS_AB_FORK_TOKEN", "")
    payload = {
        "message": message, "chat_mode": mode,
        "ab_arm": arm, "correlation_id": correlation_id,
        # thread_id omitted -> ensure_thread() mints a fresh one per arm.
    }
    req = urllib.request.Request(
        f"{base}/chat", data=json.dumps(payload).encode(), method="POST")
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=30) as resp:
        resp.read()


# ── ask anything ────────────────────────────────────────────────────────────

class Ask(BaseModel):
    question: str
    mode: str = "copilot"
    arms: list[str] | None = None
    run_id: str | None = None        # append to an existing ad-hoc run


@router.post("/ab/ask")
def ask(body: Ask) -> dict:
    """Type a question, fork it to every arm, watch both.

    Ananth: "where do I see it by posting a question." /fork needed a run_id
    and a qid from the fixed twenty, so there was no path from a typed question
    to a comparison at all. This is that path.

    The run is REAL, not a scratch object: same ab_runs row, same
    ab_run_questions rows, same snapshot rules, so an ad-hoc comparison is
    re-openable tomorrow exactly like a set-based one. The only difference is
    where the question came from, and that is recorded rather than implied --
    `source: "ad_hoc"` on the frozen question set.

    WHY THE QUESTION IS SNAPSHOT AND NOT JUST STORED AS TEXT: the run's
    question_set column is the frozen record of what was asked. A typed
    question that lived only in a log would make the run unreadable the moment
    the log rotated, which is the same failure as reading an envelope back from
    a TTL'd cache.

    NOT a general chat endpoint. It forks -- two turns, nobody served -- and it
    is admin-gated like the rest of the harness. A caller wanting an answer
    should POST /chat.
    """
    q_text = (body.question or "").strip()
    if not q_text:
        raise HTTPException(400, "question is empty")

    run_id = body.run_id
    if run_id:
        runs = _q("select * from ab_runs where run_id=:r", {"r": run_id})
        if not runs:
            raise HTTPException(404, f"run {run_id!r} not found")
        qset = _run_set(runs[0])
        arms = body.arms or [a for a in (runs[0]["arm_a_id"], runs[0].get("arm_b_id")) if a]
    else:
        arms = body.arms or ["v1", "v2"]
        run_id = f"ab-{uuid.uuid4().hex[:10]}"
        qset = {"set_id": "ad_hoc", "source": "ad_hoc",
                "mode": body.mode, "questions": []}
        _x("""INSERT INTO ab_runs (run_id, set_id, arm_a_id, arm_a_label,
                                   arm_b_id, arm_b_label, held_constant, varied,
                                   created_by, question_set)
              VALUES (:r,'ad_hoc',:aid,:alab,:bid,:blab,
                      CAST(:h AS JSONB),CAST(:v AS JSONB),'ad_hoc',
                      CAST(:qs AS JSONB))""",
           {"r": run_id,
            "aid": arms[0], "alab": f"{arms[0]} orchestrator",
            "bid": arms[1] if len(arms) > 1 else "",
            "blab": f"{arms[1]} orchestrator" if len(arms) > 1 else "",
            "h": json.dumps(["question", f"mode:{body.mode}", "input",
                             "tool manifest", "prompt blocks", "model roster",
                             "publish path", "renderer",
                             "corpus + cache + load (simultaneous)",
                             "wall-clock time"]),
            "v": json.dumps(["the orchestrator decision",
                             "LLM sampling — simultaneity removes the "
                             "confound, not the variance"]),
            "qs": json.dumps(qset)})

    qid = f"q{len(qset.get('questions') or []) + 1:02d}"
    qset.setdefault("questions", []).append(
        {"id": qid, "q": q_text, "shape": "ad_hoc"})
    _x("""UPDATE ab_runs SET question_set = CAST(:qs AS JSONB) WHERE run_id=:r""",
       {"qs": json.dumps(qset), "r": run_id})

    out = fork(run_id, qid, Fork(mode=body.mode, arms=arms))
    # The page needs these to build its own URL; fork() answers about a
    # question it was handed and does not know it was just created.
    out["created_run"] = (body.run_id is None)
    out["view"] = f"/ab?run={run_id}&q={qid}"
    return out


# ── which one did you prefer ────────────────────────────────────────────────

class Prefer(BaseModel):
    better: str                      # an arm id that actually ran, or "tie"
    notes: str | None = None
    created_by: str | None = None


@router.post("/ab/runs/{run_id}/q/{qid}/prefer")
def prefer(run_id: str, qid: str, body: Prefer) -> dict:
    """Record which answer the person actually wanted.

    Ananth, 2026-09-11: a "which option do you like" that can be stored.

    🔴 THIS MEASURES PREFERENCE, NOT QUALITY, AND NOT ON A SAMPLE THAT SUPPORTS
    EITHER. It is the human fixture the exit criteria are deliberately NOT made
    of. Twenty comparisons are zero data points for any criterion and twenty
    for human reading, and this endpoint will never say otherwise.

    `notes` is the load-bearing column. WHY one answer was wanted is the thing
    a later reader can act on; WHICH was wanted is a label. A one-click choice
    is cheap on purpose -- an unrecorded preference is worth nothing and a
    reason nobody had time to type is worth nothing either -- but a row with no
    reason is marked as such rather than counted as if it had one. See
    /ab/runs/{run_id}/verdicts.

    The arm is VALIDATED against the arms that ran. A preference for an arm
    that was never executed is not a weak signal, it is a data error, and
    accepting it would put a value in the column that no read could
    distinguish from a real one.
    """
    runs = _q("select * from ab_runs where run_id=:r", {"r": run_id})
    if not runs:
        raise HTTPException(404, f"run {run_id!r} not found")
    run = runs[0]

    ran = {r["arm_id"] for r in _q(
        """select arm_id from ab_run_questions
            where run_id=:r and question_id=:q and correlation_id is not null""",
        {"r": run_id, "q": qid})}
    choice = (body.better or "").strip()
    if choice != "tie" and choice not in ran:
        raise HTTPException(
            400, f"arm {choice!r} did not run on {qid!r}; arms that ran: "
                 f"{sorted(ran) or 'none'}")

    _x("""INSERT INTO ab_verdicts (run_id, question_id, better, notes, created_by)
          VALUES (:r,:q,:b,:n,:by)
          ON CONFLICT (run_id, question_id) DO UPDATE SET
              better     = EXCLUDED.better,
              notes      = EXCLUDED.notes,
              created_by = EXCLUDED.created_by,
              updated_at = now()""",
       {"r": run_id, "q": qid, "b": choice,
        "n": (body.notes or "").strip() or None, "by": body.created_by})

    got = _q("""select better, notes, updated_at from ab_verdicts
                 where run_id=:r and question_id=:q""", {"r": run_id, "q": qid})
    if not got:
        raise HTTPException(500, "preference wrote no row")
    return {"run_id": run_id, "question_id": qid,
            "better": got[0]["better"],
            "reason_given": bool(got[0]["notes"]),
            "recorded_at": str(got[0]["updated_at"])}


@router.get("/ab/runs/{run_id}/verdicts")
def verdicts(run_id: str) -> dict:
    """Every recorded preference on this run, with its population stated.

    🔴 NO BARE RATE. Ever. "v2 better: 13/20" is the sentence this whole table
    was designed to make impossible -- the moment a score exists it is quoted
    as an exit criterion and the distinction between "zero data points for the
    criteria, twenty for judgement" stops being observed.

    So the counts here always arrive with what they are counts OF: how many
    questions were compared at all, how many drew a preference, and how many of
    those carried a REASON. A reasonless preference is a real signal about what
    a person wanted and a poor one about why -- and Tool Selection's finding
    from tonight is the reason it is separated rather than folded in: a
    correction can land further from the truth than the original when the
    denominator quietly carries rows that were never in scope.
    """
    runs = _q("select * from ab_runs where run_id=:r", {"r": run_id})
    if not runs:
        raise HTTPException(404, f"run {run_id!r} not found")
    qset = _run_set(runs[0])
    rows = _q("""select question_id, better, notes, created_by, updated_at
                   from ab_verdicts where run_id=:r order by question_id""",
              {"r": run_id})
    compared = {r["question_id"] for r in _q(
        """select question_id from ab_run_questions
            where run_id=:r and correlation_id is not null
            group by question_id having count(distinct arm_id) > 1""",
        {"r": run_id})}
    with_reason = sum(1 for r in rows if (r["notes"] or "").strip())
    return {
        "run_id": run_id,
        "population": "questions on which MORE THAN ONE arm actually ran",
        "questions_in_set": len(qset.get("questions") or []),
        "compared": len(compared),
        "preferences_recorded": len(rows),
        "of_those_with_a_reason": with_reason,
        "of_those_reason_free": len(rows) - with_reason,
        "not_yet_judged": len(compared) - len(rows),
        # The rows themselves, never a rate derived from them.
        "verdicts": [
            {"question_id": r["question_id"], "better": r["better"],
             "notes": r["notes"], "by": r["created_by"],
             "at": str(r["updated_at"])}
            for r in rows
        ],
        "read_this_as": (
            "preference, not quality — and on this sample, evidence for human "
            "reading rather than for any exit criterion"
        ),
    }


def register_chat_fork(question: str, mode: str, arms: dict[str, str],
                       thread_arm: str) -> str:
    """Give a chat-surface fork a permalink into the two-column page.

    The kebab fork happens in /chat and produces two correlation_ids. The page
    that renders a comparison is keyed on run_id + question_id, so without this
    a forked chat turn would have no way to be LOOKED at -- and I would have
    had to build a second two-column renderer inside the chat bubble, which is
    exactly what I told the FE seat not to do: a comparison rendered
    differently from the product is measuring the renderer.

    The run is marked `source: "chat_fork"` and carries `thread_arm`, because a
    chat fork is NOT the same object as a lab-bench run: one of its arms was
    served to a person and is in their thread. A reader who cannot tell those
    apart will eventually quote a chat fork as if nobody had been served.
    """
    run_id = f"ab-{uuid.uuid4().hex[:10]}"
    qset = {"set_id": "chat_fork", "source": "chat_fork", "mode": mode,
            "thread_arm": thread_arm,
            "questions": [{"id": "q01", "q": question, "shape": "chat_fork"}]}
    ids = sorted(arms)
    _x("""INSERT INTO ab_runs (run_id, set_id, arm_a_id, arm_a_label, arm_b_id,
                               arm_b_label, held_constant, varied, created_by,
                               question_set)
          VALUES (:r,'chat_fork',:aid,:alab,:bid,:blab,
                  CAST(:h AS JSONB),CAST(:v AS JSONB),'chat_kebab',
                  CAST(:qs AS JSONB))""",
       {"r": run_id,
        "aid": ids[0], "alab": f"{ids[0]}{' — served in the thread' if ids[0] == thread_arm else ' — shadow'}",
        "bid": ids[1] if len(ids) > 1 else "",
        "blab": (f"{ids[1]}{' — served in the thread' if ids[1] == thread_arm else ' — shadow'}"
                 if len(ids) > 1 else ""),
        "h": json.dumps(["question", f"mode:{mode}", "input", "tool manifest",
                         "prompt blocks", "model roster", "publish path",
                         "renderer", "corpus + cache + load (simultaneous)",
                         "wall-clock time"]),
        "v": json.dumps(["the orchestrator decision",
                         "LLM sampling — simultaneity removes the confound, "
                         "not the variance",
                         "thread memory — the shadow arm answers cold, the "
                         "served arm has the conversation"]),
        "qs": json.dumps(qset)})
    for arm, cid in arms.items():
        _x("""INSERT INTO ab_run_questions (run_id, question_id, arm_id,
                                            correlation_id, status)
              VALUES (:r,'q01',:a,:c,'running')
              ON CONFLICT (run_id, question_id, arm_id) DO UPDATE SET
                  correlation_id = EXCLUDED.correlation_id, status='running'""",
           {"r": run_id, "a": arm, "c": cid})
    return f"/ab?run={run_id}&q=q01"
