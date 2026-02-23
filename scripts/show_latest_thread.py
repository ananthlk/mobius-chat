#!/usr/bin/env python3
"""Show the latest conversation thread and confirm why RAG ran.

Run from Mobius root:
  PYTHONPATH=mobius-chat python mobius-chat/scripts/show_latest_thread.py
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

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


def _get_db_url() -> str:
    from app.chat_config import get_chat_config
    return (get_chat_config().rag.database_url or "").strip()


def main() -> int:
    url = _get_db_url()
    if not url:
        print("CHAT_RAG_DATABASE_URL not set. Cannot fetch thread.")
        return 1

    import psycopg2
    import psycopg2.extras

    conn = psycopg2.connect(url)
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    # Latest thread by updated_at
    cur.execute(
        """
        SELECT thread_id, created_at, updated_at
        FROM chat_threads
        ORDER BY updated_at DESC NULLS LAST, created_at DESC
        LIMIT 1
        """
    )
    row = cur.fetchone()
    if not row:
        print("No threads found.")
        cur.close()
        conn.close()
        return 0

    thread_id = row["thread_id"]
    print("=" * 80)
    print(f"LATEST THREAD: {thread_id}")
    print(f"  created_at: {row['created_at']}")
    print(f"  updated_at: {row['updated_at']}")
    print("=" * 80)

    # Turn messages (user + assistant pairs)
    cur.execute(
        """
        WITH pairs AS (
            SELECT turn_id,
                   max(created_at) AS created_at,
                   max(CASE WHEN role = 'user' THEN content END) AS user_content,
                   max(CASE WHEN role = 'assistant' THEN content END) AS assistant_content
            FROM chat_turn_messages
            WHERE thread_id = %s
            GROUP BY turn_id
        )
        SELECT turn_id, user_content, assistant_content, created_at
        FROM pairs
        WHERE user_content IS NOT NULL AND assistant_content IS NOT NULL
        ORDER BY created_at DESC
        LIMIT 5
        """,
        (thread_id,),
    )
    turns = cur.fetchall()
    print("\n--- TURN MESSAGES (newest first) ---")
    for i, t in enumerate(turns, 1):
        print(f"\nTurn {i} ({t['turn_id']}) @ {t['created_at']}")
        print(f"  User: {(t['user_content'] or '')[:200]}...")
        ac = (t['assistant_content'] or '')[:300]
        if ac.startswith("{"):
            try:
                j = json.loads(ac)
                ac = j.get("direct_answer", ac) or ac
            except Exception:
                pass
        print(f"  Assistant: {ac}...")

    # State (master_objective, etc.)
    cur.execute("SELECT state_json FROM chat_state WHERE thread_id = %s", (thread_id,))
    state_row = cur.fetchone()
    if state_row:
        state = state_row["state_json"]
        if isinstance(state, str):
            state = json.loads(state)
        mo = state.get("master_objective") if state else None
        if mo:
            print("\n--- MASTER OBJECTIVE ---")
            print(f"  status: {mo.get('status')}")
            print(f"  summary: {(mo.get('summary') or '')[:100]}...")
            subs = mo.get("sub_objectives") or []
            for so in subs:
                ans = (so.get("answer") or "")[:60]
                print(f"  - [{so.get('id')}] {so.get('status')} | Q: {(so.get('text') or '')[:50]}... | answer: {ans}...")

    # Latest turns with sources (why RAG ran)
    cur.execute(
        """
        SELECT correlation_id, question, final_message, sources, plan_snapshot,
               source_confidence_strip, created_at
        FROM chat_turns
        WHERE thread_id = %s
        ORDER BY created_at DESC
        LIMIT 3
        """,
        (thread_id,),
    )
    turn_rows = cur.fetchall()
    print("\n--- WHY RAG RAN (latest turns) ---")
    for tr in turn_rows:
        print(f"\nTurn {tr['correlation_id'][:8]}... @ {tr['created_at']}")
        print(f"  question: {(tr['question'] or '')[:100]}...")
        sources = tr.get("sources")
        if isinstance(sources, str):
            try:
                sources = json.loads(sources)
            except Exception:
                sources = []
        n_sources = len(sources) if isinstance(sources, list) else 0
        print(f"  sources: {n_sources} docs → RAG {'ran' if n_sources > 0 else 'returned 0 or skipped'}")
        if n_sources > 0:
            for s in (sources or [])[:3]:
                doc = s.get("document_name") or s.get("document_id") or "?"
                print(f"    - {doc}")
        print(f"  source_confidence_strip: {tr.get('source_confidence_strip') or 'n/a'}")
        plan = tr.get("plan_snapshot")
        if plan and isinstance(plan, dict):
            sqs = plan.get("subquestions") or []
            print(f"  plan subquestions: {len(sqs)}")
            for sq in sqs[:5]:
                print(f"    - [{sq.get('id')}] {(sq.get('text') or '')[:50]}...")

    cur.close()
    conn.close()
    print("\n" + "=" * 80)
    return 0


if __name__ == "__main__":
    sys.exit(main())
