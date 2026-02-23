#!/usr/bin/env python3
"""Multi-turn conversation demo: user asks question, system responds, user provides input, repeat until closure.

Run from Mobius root:
  QUEUE_TYPE=memory PYTHONPATH=mobius-chat python mobius-chat/scripts/conversation_demo.py

Simulates:
  Turn 1: User asks multi-part question
  Turn 2: User provides info/link when system asks for help
  ... until resolved or max turns
"""
from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path

os.environ.setdefault("QUEUE_TYPE", "memory")
os.environ.setdefault("MOBIUS_DEBUG_PLAN", "1")  # Show master plan pre/post parser and integrator

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
from app.storage import get_response
from app.storage.threads import ensure_thread


def _extract_answer(resp: dict) -> str:
    msg = resp.get("message") or ""
    if msg.strip().startswith("{"):
        try:
            j = json.loads(msg)
            return (j.get("direct_answer") or msg)[:600]
        except Exception:
            pass
    return msg[:600]


def main() -> int:
    thread_id = ensure_thread(None)  # create thread in DB
    turn = 0
    max_turns = 4

    # --- Turn 1: User asks (factual: coverage, prior auth, billing) ---
    messages = [
        "are peer support services covered by fh medicaid? do i need prior authorization with sunshine? how should i bill for these services?",
    ]

    print("=" * 80)
    print("CONVERSATION DEMO: Relentless continuity to closure")
    print("=" * 80)

    while turn < max_turns:
        turn += 1
        msg = messages[-1]
        cid = str(uuid.uuid4())

        print(f"\n--- TURN {turn} (User) ---")
        print(msg[:200] + ("..." if len(msg) > 200 else ""))
        print()

        run_pipeline(cid, msg, thread_id)
        resp = get_response(cid)
        if not resp:
            from app.queue import get_queue
            resp = get_queue().get_response(cid)

        status = resp.get("status", "?")
        obj_status = resp.get("objective_status")
        closure_msg = resp.get("closure_message")
        user_ask = resp.get("user_ask")
        answer = _extract_answer(resp)

        print(f"--- TURN {turn} (Assistant) ---")
        print(f"Status: {status}" + (f" | objective_status: {obj_status}" if obj_status else ""))
        if closure_msg:
            print(f"Closure: {closure_msg}")
        # Show per-subquestion answers and source (integrator resolutions = who set the answer)
        resolutions = resp.get("resolutions") or []
        if resolutions:
            print("Answers (integrator resolutions → source=rag|user_input|planner|google):")
            for r in resolutions:
                sid = r.get("sq_id", "?")
                src = r.get("source", "?")
                q = (r.get("question") or "")[:55]
                res = (r.get("resolution") or "")
                res_disp = res[:120] + ("..." if len(res) > 120 else "")
                print(f"  [{sid}] source={src} | Q: {q} | A: {res_disp}")
        print(f"Answer: {answer[:400]}...")
        if user_ask:
            print(f"\n[System asks user for help]")
            print(user_ask[:400])
            # Simulate user providing input (user_provided_context: codes)
            follow_up = "i think the codes are H0038"
            messages.append(follow_up)
        else:
            print("\n[No user_ask - answer resolved or clarification path]")
            break

    print("\n" + "=" * 80)
    print("CONVERSATION END")
    print("=" * 80)
    return 0


if __name__ == "__main__":
    sys.exit(main())
