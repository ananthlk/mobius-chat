"""Run ONE live turn on the governor's own loop, and print the trace.

    .venv/bin/python scripts/eval/v2_loop_turn.py ["question"] [mode]

WHY A PIN AND NOT THE FLAG. MOBIUS_V2_OWN_LOOP is service-wide: turning it on
routes EVERY v2 turn through code that has never executed a live turn. That is
a cutover, not a test. `ab_loop` pins THIS turn only, and is itself gated on
MOBIUS_V2_AB_FORK, so a payload carrying one is a harness turn by construction.

Prints the v2_trace steps as the FE receives them — headline plus the detail
that expands under it — because the point of the exercise is whether a person
can follow the turn, not whether it returns 200.
"""

import json
import sys
import time
import urllib.request
import uuid

BASE = "https://mobius-chat-ortabkknqa-uc.a.run.app"
TERMINAL = {"completed", "failed", "refused"}
DEADLINE = 240

#: A SPREAD, not a set of favourites. One the corpus should answer outright,
#: one that needs a named playbook, one comparison across two payers, and one
#: we expect to come back thin — a trace is only worth reading if it is legible
#: when the turn goes badly, and picking four easy questions would hide that.
QUESTIONS = [
    ("What is the timely filing deadline for Sunshine Health?", "copilot"),
    ("How do I appeal a CARC 22 denial for Sunshine Health?", "copilot"),
    ("Which has the longer claims appeal window in Florida Medicaid — "
     "Aetna Better Health or Sunshine Health?", "copilot"),
    ("What is Molina Healthcare's timely filing deadline for corrected "
     "claims?", "quick"),
]


def fetch(cid):
    with urllib.request.urlopen(f"{BASE}/chat/response/{cid}", timeout=30) as r:
        return json.loads(r.read())


def run_one(Q, MODE) -> int:
    cid = str(uuid.uuid4())
    body = {"message": Q, "chat_mode": MODE, "correlation_id": cid,
            "ab_fork": True, "ab_loop": "v2", "cache_assist": False}
    req = urllib.request.Request(BASE + "/chat", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    t0 = time.time()
    top = json.loads(urllib.request.urlopen(req, timeout=60).read())
    cid = top.get("correlation_id") or cid
    print(f"cid={cid}  mode={MODE}\nQ: {Q}\n")

    data = None
    while time.time() - t0 < DEADLINE:
        time.sleep(5)
        data = fetch(cid)
        if (data.get("status") or "") in TERMINAL:
            break
    wall = time.time() - t0
    data = data or {}

    for entry in (data.get("thinking_log") or []):
        if not isinstance(entry, dict):
            print(f"· {entry}")
            continue
        d = entry.get("data") or {}
        if entry.get("signal") == "v2_trace" or d.get("stage"):
            state = d.get("state", "")
            mark = "◌" if state == "running" else "✓"
            print(f"\n{mark} [{d.get('stage', '')}] {entry.get('note', '')}")
            for line in (d.get("detail") or []):
                print("   " + line)
        else:
            line = entry.get("line") or entry.get("note") or ""
            if line:
                print(f"· {line}")

    print(f"\n{'─' * 60}")
    print(f"status={data.get('status')}  wall={wall:.1f}s  "
          f"rounds={len(data.get('react_trace_rounds') or [])}  "
          f"sources={len(data.get('sources') or [])}")
    print(f"exit={data.get('v2_exit_mode')}  stopped_by={data.get('v2_stopped_by')}")
    ans = (data.get("message") or "").strip()
    print(f"\nANSWER ({len(ans)} chars):\n{ans[:1200]}")
    return 0 if ans else 1


def main() -> int:
    if len(sys.argv) > 1:
        return run_one(sys.argv[1], sys.argv[2] if len(sys.argv) > 2
                       else "copilot")
    bad = 0
    for q, mode in QUESTIONS:
        print("\n" + "=" * 72)
        try:
            bad += run_one(q, mode)
        except Exception as exc:              # one bad turn must not end the run
            print(f"!! {type(exc).__name__}: {exc}")
            bad += 1
    print(f"\n{len(QUESTIONS) - bad}/{len(QUESTIONS)} produced an answer")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
