"""The 15-question A/B run. Both arms, one turn at a time.

THREE THINGS THIS DOES ON PURPOSE:

1. cache_assist=false. The answer cache will serve a repeated question with NO
   pipeline at all -- measured 2026-09-14: status=completed, thinking_entries=0,
   no arm, no rag. This bank has been run three times, so every question is a
   cache candidate and a run without this flag would compare cache hits.
2. STRICTLY SERIAL. mobius-rag is min=max=1; concurrent callers queue and blow
   the 120s cap. Measured: 8 concurrent = 8 timeouts. A parallel harness would
   manufacture the failure it is trying to measure.
3. Records status and error per turn. A failed turn is data, not a gap -- it
   must never be silently dropped from a mean.
"""
import json, os, sys, time, urllib.request, uuid

BASE = "https://mobius-chat-ortabkknqa-uc.a.run.app"
BANK = "docs/eval/ab-15-question-bank.json"
DEADLINE_S = int(os.environ.get('AB15_DEADLINE','420'))
ARMS = ("v1", "v2")
TERMINAL = ("completed", "failed", "clarification", "refinement", "error")
# 🔴 SCOPED BY PROFILE. A fixed output path cost the gemini baseline's
# correlation_ids on 2026-09-14: launching the anthropic run truncated the file
# holding them, so round counts for the comparison arm became unrecoverable.
# The results were still on disk in a summary -- the IDS were what went, and
# they are the only way back to a turn.
OUT = f"/tmp/ab15_results_{os.environ.get('AB15_TAG', os.environ.get('AB15_PROFILE','default'))}_al.json"


def one(q, mode, arm, profile=None):
    cid = str(uuid.uuid4())
    body = {"message": q, "chat_mode": mode, "correlation_id": cid,
            "ab_arm": arm, "cache_assist": False}
    _prof = (profile or os.environ.get("AB15_PROFILE", "")).strip()
    if _prof:
        body["model_profile"] = _prof
    r = urllib.request.Request(BASE + "/chat", data=json.dumps(body).encode(),
                               headers={"Content-Type": "application/json"})
    t0 = time.time()
    try:
        cid = json.loads(urllib.request.urlopen(r, timeout=60).read()).get("correlation_id") or cid
    except Exception as e:
        return {"arm": arm, "status": "post_failed", "error": str(e)[:80],
                "latency_s": round(time.time() - t0, 1)}
    d = {}
    while time.time() - t0 < DEADLINE_S:
        time.sleep(5)
        try:
            d = json.loads(urllib.request.urlopen(f"{BASE}/chat/response/{cid}", timeout=30).read())
        except Exception:
            continue
        # 🔴 EVERY TERMINAL STATE, NOT THE TWO I HAPPENED TO THINK OF.
        # `clarification` is a real ending -- the turn asks which document you
        # meant and stops. Polling it to the deadline recorded 604.7s for a
        # turn the service measured at 10.5s, and I reported that upward as
        # "anthropic is slow". A harness that waits past an answer reports the
        # wait as the answer.
        if (d.get("status") or "") in TERMINAL:
            break
    msg = d.get("message") or ""
    sections = 0
    try:
        sections = len((json.loads(msg) or {}).get("sections") or []) if msg.strip().startswith("{") else 0
    except Exception:
        pass
    # 🔴 ROUNDS, NOT WALL CLOCK. The question is whether a model CLOSES THE
    # LOOP sooner, which wall time cannot answer: a slower model that finishes
    # in 3 rounds has converged better than a fast one that needs 11.
    import re as _re
    _tl = [(e.get("line") if isinstance(e, dict) else str(e)) or ""
           for e in (d.get("thinking_log") or [])]
    _rounds = [int(m.group(1)) for l in _tl
               for m in [_re.match(r"Round (\d+)/(\d+)", l)] if m]
    _postures = [m.group(1) for l in _tl
                 for m in [_re.match(r"Round \d+/\d+ — (\S+)", l)] if m]
    _qc = d.get("qc_audit")
    if isinstance(_qc, str):
        try: _qc = json.loads(_qc)
        except Exception: _qc = None
    _score = _qc.get("automated_score") if isinstance(_qc, dict) else None
    _verdict = _qc.get("adjudication_verdict") if isinstance(_qc, dict) else None
    tok = d.get("tokens_used") or {}
    return {
        "arm": arm, "profile": _prof or "default", "cid": cid,
        "status": d.get("status") or "timeout_local",
        "error": d.get("error"),
        "latency_s": round(time.time() - t0, 1),
        "cost_usd": d.get("cost_usd"),
        "in_tokens": tok.get("input_tokens"), "out_tokens": tok.get("output_tokens"),
        "model": d.get("model_used"),
        "thinking_entries": len(d.get("thinking_log") or []),
        "sources": len(d.get("sources") or []),
        "answer_chars": len(msg), "sections": sections,
        # The service's own measurement. When this and latency_s disagree, the
        # gap is MY harness -- waiting, polling, retrying -- not the product.
        "pipeline_s": (lambda p: round(p/1000.0, 1) if p else None)(
            (d.get("llm_performance") or {}).get("total_latency_ms")
            if isinstance(d.get("llm_performance"), dict) else None),
        "rounds": max(_rounds) if _rounds else None,
        "score": _score, "verdict": _verdict,
        "postures": _postures,
        "answer_head": " ".join(msg.split())[:180],
    }


def main():
    only = sys.argv[1] if len(sys.argv) > 1 else None   # "controls" | "fans" | None
    arms = (sys.argv[2],) if len(sys.argv) > 2 else ARMS
    bank = json.load(open(BANK))
    qs = bank["questions"]
    if only == "controls":
        qs = [q for q in qs if q["id"] in (1, 2, 3, 10, 11, 12)]
    elif only == "fans":
        qs = [q for q in qs if q["id"] in (4, 5, 6, 7, 8, 9, 13, 14, 15)]
    print(f"{len(qs)} question(s) x {len(arms)} arm(s) = {len(qs)*len(arms)} turns, serial")
    out = []
    for q in qs:
        # 🔴 ALTERNATE WHICH ARM GOES FIRST.
        #
        # The pair is the unit of comparison: v1 and v2 run back to back on the
        # same question, seconds apart, so whatever is loading the shared dev
        # instance hits both members of a pair about equally and cancels in the
        # within-pair difference. That is the ONLY defence available against a
        # confound we do not control -- mobius-lexicon-maintenance sweeps the
        # embeddings table on a schedule nobody here owns, 15+ queries running
        # 3-9 minutes, found by Retriever 2026-09-14.
        #
        # But a FIXED order breaks that defence: if load is rising through a
        # pair, the arm that always runs second always pays for it, and a real
        # systematic bias appears in the mean. Alternating by question id makes
        # second-position cost fall on v1 and v2 about equally instead.
        # PROFILE COMPARE: the pair is the unit. Same question, seconds
        # apart, so whatever is loading the shared instance hits both and
        # cancels in the within-pair difference -- and the order alternates so
        # neither profile systematically owns the costlier second slot.
        profiles = [x.strip() for x in os.environ.get("AB15_PROFILES", "").split(",") if x.strip()]
        if profiles:
            order = profiles if (q["id"] % 2) else list(reversed(profiles))
            pairs = [(arms[0], pf) for pf in order]
        else:
            order = arms if (q["id"] % 2) else tuple(reversed(arms))
            pairs = [(a, None) for a in order]
        for arm, prof in pairs:
            r = one(q["q"], q["mode"], arm, profile=prof)
            r.update(id=q["id"], group=q["group"], mode=q["mode"])
            out.append(r)
            print(f"  Q{q['id']:<2} {q['group']} {q['mode']:<8} {r['profile']:<9}: "
                  f"{r['latency_s']:6.1f}s {str(r['status']):<10} "
                  f"pipe={r.get('pipeline_s')}s cost={r.get('cost_usd')} rounds={r.get('rounds')} "
                  f"score={r.get('score')} src={r['sources']}"
                  + (f" ERR={r['error']}" if r.get("error") else ""))
            json.dump(out, open(OUT, "w"), indent=2, default=str)
    print(f"\nwrote {OUT}")


if __name__ == "__main__":
    main()
