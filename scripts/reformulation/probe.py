"""Reformulation harness — turn one-shot probes into a repeatable measurement.

Ananth, 2026-09-13: "we need this locked down.. with the reforumaltion and we
need to find the right way to do it".

🔴 WHY THIS EXISTS. Every reformulation number produced tonight — mine and the
ones I sent two other seats — was a SINGLE SAMPLE. Two near-identical queries
then measured 7.4s / 23 chunks and 108.1s / 34 chunks, and a query that found
H0049/H0050 once did not find them again. A rule chosen from single samples of
an unstable system is a rule fitted to noise.

So this does three things a hand-written probe cannot:
  1. REPEATS each formulation and reports spread, not just a value. If p50 and
     max disagree wildly, no rule built on one run is trustworthy.
  2. Scores the SAME way every time — tag composition, target facts, latency —
     so two formulations are comparable rather than described.
  3. Writes a row per run, so a later change can be compared against today
     rather than re-argued.

WHAT IT DOES NOT DO: decide where reformulation lives. Ananth has directed that
tool execution moves to Tool Manifest's executor; this measures candidate rules
so that seat can choose one on evidence. Building the rule into chat's preload
is the fourth-copy problem this fleet already stopped me from creating once.
"""
from __future__ import annotations

import argparse, json, re, statistics as st, time, urllib.request
from collections import Counter

ENDPOINT = "/api/retriever/answer"


def _flat(d, pre=""):
    out = []
    if isinstance(d, dict):
        for k, v in d.items():
            p = f"{pre}.{k}" if pre else k
            out += _flat(v, p) if isinstance(v, dict) else [p]
    return out


def ask(base_url, query, *, caller_mode="chat.thinking", call_number=None,
        timeout=300):
    """One call. speculative=True ALWAYS -- these rows are never consumed by a
    user and must not train as if they were.

    is_calibration is deliberately NOT sent: dispatch.py checks it first and
    forces single-strategy isolation, which silently measures a different
    system. That flag cost three wrong results across two seats on 2026-09-13.
    """
    body = {"query": query, "caller_mode": caller_mode, "speculative": True}
    if call_number is not None:
        body["call_number"] = int(call_number)
    req = urllib.request.Request(
        base_url.rstrip("/") + ENDPOINT, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json",
                 "X-Caller": "reformulation-probe"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode()), time.time() - t0


def score(payload, secs, targets):
    c = payload.get("contract") or {}
    chunks = c.get("chunks") or []
    raw = " ".join(json.dumps(x) for x in chunks)
    tags = Counter()
    for x in chunks:
        for t in _flat((x or {}).get("tags") or {}):
            tags[t] += 1
    found = [t for t in targets
             if re.search(rf"\b{re.escape(t)}\b", raw, re.I)]
    return {
        "secs": round(secs, 1),
        "chunks": len(chunks),
        "slot": c.get("chosen_slot"),
        "status": c.get("status"),
        "docs": len({(x.get("document_name") or x.get("document_id"))
                     for x in chunks if isinstance(x, dict)}),
        # The composition signal: a presence check reports success when
        # reimbursement-tagged chunks come back, even when they are the WRONG
        # reimbursement chunks. The ratio is what separated the two.
        "proc_code": tags.get("billing_codes.procedure_code", 0),
        "bill_gen": tags.get("billing_codes.general", 0),
        "care_mgmt": tags.get("care_management.general", 0),
        "payors": sum(n for t, n in tags.items() if t.startswith("payor.")),
        "found": found,
        "hit": bool(found),
    }


def run(base_url, formulations, targets, repeats):
    results = {}
    for label, q in formulations.items():
        runs = []
        for i in range(repeats):
            try:
                p, s = ask(base_url, q)
                runs.append(score(p, s, targets))
            except Exception as e:
                runs.append({"error": f"{type(e).__name__}: {e}", "hit": False})
        ok = [r for r in runs if "error" not in r]
        hits = sum(1 for r in runs if r.get("hit"))
        agg = {"query": q, "runs": runs, "n": len(runs),
               "hit_rate": f"{hits}/{len(runs)}"}
        if ok:
            secs = [r["secs"] for r in ok]
            agg["secs_min_med_max"] = [min(secs), round(st.median(secs), 1), max(secs)]
            agg["proc_code_med"] = st.median([r["proc_code"] for r in ok])
            agg["care_mgmt_med"] = st.median([r["care_mgmt"] for r in ok])
            agg["slots"] = sorted({str(r["slot"]) for r in ok})
        results[label] = agg
    return results


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", required=True)
    ap.add_argument("--spec", required=True,
                    help="JSON: {formulations:{label:query}, targets:[str]}")
    ap.add_argument("--repeats", type=int, default=3,
                    help="A single sample of an unstable system is an anecdote.")
    ap.add_argument("--out")
    a = ap.parse_args()
    spec = json.loads(open(a.spec).read())
    res = run(a.base_url, spec["formulations"], spec.get("targets") or [],
              a.repeats)
    print(f"{'formulation':34} {'hits':>6} {'lat min/med/max':>18} "
          f"{'proc':>5} {'care':>5}  slots")
    for label, r in res.items():
        lat = r.get("secs_min_med_max")
        print(f"  {label:32} {r['hit_rate']:>6} "
              f"{str(lat):>18} {str(r.get('proc_code_med','-')):>5} "
              f"{str(r.get('care_mgmt_med','-')):>5}  {','.join(r.get('slots',[]))}")
    if a.out:
        json.dump({"ts": time.time(), "repeats": a.repeats, "results": res},
                  open(a.out, "w"), indent=2)
        print(f"\nwrote {a.out}")


if __name__ == "__main__":
    main()
