#!/usr/bin/env python3
"""Run full pipeline and print complete response (multi-turn style output)."""
import json
import os
import sys
import uuid
from pathlib import Path

os.environ.setdefault("QUEUE_TYPE", "memory")

CHAT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(CHAT_ROOT))
for env_path in (CHAT_ROOT / ".env", CHAT_ROOT.parent / "mobius-config" / ".env"):
    if env_path.exists():
        try:
            from dotenv import load_dotenv
            load_dotenv(env_path, override=False)
        except Exception:
            pass
        break

from app.pipeline.orchestrator import run_pipeline
from app.queue import get_queue

def main():
    msg = (
        "Can you find the ICD code for Socio Psycho Rehab, see if this is a covered benefit "
        "under Medicaid in Florida and see if Sunshine Health requires prior authorization"
    )
    thread_id = None  # no DB; use None to avoid UUID error when chat_state expects uuid
    cid = str(uuid.uuid4())
    print("Running pipeline with thread_id=%s..." % thread_id)
    run_pipeline(cid, msg, thread_id)

    from app.storage import get_response
    resp = get_response(cid)
    if not resp:
        resp = get_queue().get_response(cid)

    print("\n" + "=" * 80)
    print("FULL MULTI-TURN RESPONSE")
    print("=" * 80)
    print("Status:", resp.get("status"))
    print("\nPlan (subquestions):")
    for sq in (resp.get("plan") or {}).get("subquestions", [])[:8]:
        print("  ", sq.get("id"), ":", (sq.get("text") or "")[:70])
    print("\nuser_ask (relentless continuity):")
    ua = resp.get("user_ask")
    if ua:
        print(" ", ua[:400])
    else:
        print("  (none)")
    msg_text = resp.get("message") or ""
    if msg_text.strip().startswith("{"):
        try:
            j = json.loads(msg_text)
            print("\nAnswer (direct_answer):", (j.get("direct_answer") or "")[:500])
        except Exception:
            print("\nMessage:", msg_text[:500])
    else:
        print("\nMessage:", msg_text[:500])
    chunks = resp.get("thinking_log") or []
    print("\nThinking chunks (%d):" % len(chunks))
    for i, t in enumerate(chunks[:10]):
        print("  %d. %s" % (i + 1, (t or "")[:75]))
    if len(chunks) > 10:
        print("  ... (%d more)" % (len(chunks) - 10))
    print("=" * 80)

if __name__ == "__main__":
    main()
