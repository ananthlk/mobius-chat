"""15-question fork compare: v2 served on anthropic, shadow arm on gemini.

The fork holds the question, the seconds and the corpus constant and varies
only the model -- the one shape that survives a shared dev instance where an
unrelated sweep can add a 3.5x spread to an identical query.
"""
import json, os, sys, time, urllib.request, uuid

BASE = "https://mobius-chat-ortabkknqa-uc.a.run.app"
BANK = "docs/eval/ab-15-question-bank.json"
TERMINAL = ("completed", "failed", "clarification", "refinement", "error")
DEADLINE = int(os.environ.get("FORK_DEADLINE", "600"))
OUT = "/tmp/fork15_results_al.json"

# Product Promise, per tier. The scorecard is meaningless without it: a 40s
# turn is a pass in copilot and a 3x miss in quick.
PROMISE_S = {"quick": 13.0, "copilot": 31.0, "agentic": 95.0}
PROMISE_C = {"quick": 0.16, "copilot": 0.45, "agentic": 0.81}


def fetch(cid):
    try:
        return json.loads(urllib.request.urlopen(f"{BASE}/chat/response/{cid}", timeout=30).read())
    except Exception:
        return {}


def summarise(d, wall):
    perf = d.get("llm_performance") if isinstance(d.get("llm_performance"), dict) else {}
    qc = d.get("qc_audit")
    if isinstance(qc, str):
        try: qc = json.loads(qc)
        except Exception: qc = None
    msg = d.get("message") or ""
    import re
    tl = [(e.get("line") if isinstance(e, dict) else str(e)) or "" for e in (d.get("thinking_log") or [])]
    rs = [int(m.group(1)) for l in tl for m in [re.match(r"Round (\d+)/", l)] if m]
    return {
        "status": d.get("status"), "wall_s": round(wall, 1),
        "pipeline_s": round(perf.get("total_latency_ms", 0) / 1000.0, 1) or None,
        "model": perf.get("primary_model") or d.get("model_used"),
        "cost": d.get("cost_usd"), "rounds": max(rs) if rs else None,
        "score": (qc or {}).get("automated_score"),
        "verdict": (qc or {}).get("adjudication_verdict"),
        "chars": len(msg), "sources": len(d.get("sources") or []),
    }


def main():
    bank = json.load(open(BANK))["questions"]
    only = sys.argv[1:] and [int(x) for x in sys.argv[1].split(",")]
    qs = [q for q in bank if not only or q["id"] in only]
    out = []
    for q in qs:
        body = {"message": q["q"], "chat_mode": q["mode"], "correlation_id": str(uuid.uuid4()),
                "ab_fork": True, "ab_arm": "v2",
                "model_profile": "anthropic",      # SERVED arm
                "ab_shadow_profile": "gemini",     # SHADOW arm
                "cache_assist": False}
        r = urllib.request.Request(BASE + "/chat", data=json.dumps(body).encode(),
                                   headers={"Content-Type": "application/json"})
        t0 = time.time()
        top = json.loads(urllib.request.urlopen(r, timeout=60).read())
        comp = top.get("comparison") or {}
        cids = {"v2": comp.get("v2") or top.get("correlation_id"), "v1": comp.get("v1")}
        if not comp:
            print(f"  Q{q['id']}: NO COMPARISON BLOCK — fork not honoured"); break
        res = {}
        while time.time() - t0 < DEADLINE:
            time.sleep(5)
            done = 0
            for arm, cid in cids.items():
                if not cid: done += 1; continue
                d = fetch(cid)
                if (d.get("status") or "") in TERMINAL:
                    res.setdefault(arm, summarise(d, time.time() - t0)); done += 1
            if done >= len(cids): break
        row = {"id": q["id"], "group": q["group"], "mode": q["mode"],
               "cids": cids, "arms": res}
        out.append(row); json.dump(out, open(OUT, "w"), indent=2, default=str)
        a = res.get("v2", {}); b = res.get("v1", {})
        print(f"  Q{q['id']:<2} {q['mode']:<8} "
              f"ANTH {str(a.get('pipeline_s')):>6}s r={a.get('rounds')} ${a.get('cost')} s={a.get('score')} | "
              f"GEM {str(b.get('pipeline_s')):>6}s r={b.get('rounds')} ${b.get('cost')} s={b.get('score')}")
    # 🔴 SECOND PASS FOR QUALITY. Adjudication is fire-and-forget and lands
    # 30-70s AFTER the turn publishes, so a score read at turn-end is
    # legitimately absent -- and absent is NOT "scored badly". Re-reading is
    # the difference between "not yet scored" and a hole in the scorecard.
    print("\n  waiting 90s for adjudication to land, then re-reading scores...")
    time.sleep(90)
    for row in out:
        for arm, cid in row["cids"].items():
            if not cid or arm not in row["arms"]:
                continue
            d = fetch(cid)
            qc = d.get("qc_audit")
            if isinstance(qc, str):
                try: qc = json.loads(qc)
                except Exception: qc = None
            if isinstance(qc, dict):
                row["arms"][arm]["score"] = qc.get("automated_score")
                row["arms"][arm]["verdict"] = qc.get("adjudication_verdict")
                row["arms"][arm]["passed"] = qc.get("passed")
    json.dump(out, open(OUT, "w"), indent=2, default=str)
    print(f"\nwrote {OUT}")


if __name__ == "__main__":
    main()
