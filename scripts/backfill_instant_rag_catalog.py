"""One-shot backfill: scan chat_state.state_json.active.uploaded_files[]
and insert any instant_rag rows that aren't already in the new catalog.

Idempotent — uses ON CONFLICT DO NOTHING on document_id, so running twice
won't duplicate. Safe to run against a live DB.

Usage:
    cd /Users/ananth/Mobius/mobius-chat
    python3 scripts/backfill_instant_rag_catalog.py

Prints a per-thread summary and an overall tally. Doesn't touch any
state_json; catalog rows can't corrupt the JSONB because we only write
to instant_rag_uploads.

Context: Phase B.1c introduced the catalog table. Uploads from before
the catalog was wired (Phase B.1 → 2026-04-17 shakedown) live only in
the JSONB blob. This script moves that history forward without
re-uploading the bytes — chunks are still in Chroma+PG from the
original ingest; the catalog just gains visibility.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# Make "app" importable when the script runs from repo root.
_repo_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_repo_root))


def main() -> int:
    from app.storage.instant_rag_catalog import record_upload

    url = (os.environ.get("CHAT_RAG_DATABASE_URL") or "").strip()
    if not url:
        print("CHAT_RAG_DATABASE_URL not set — source mobius-chat/.env first.",
              file=sys.stderr)
        return 1

    import psycopg2
    conn = psycopg2.connect(url, connect_timeout=5)
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT thread_id, state_json->'active'->'uploaded_files' AS uploads "
            "FROM chat_state "
            "WHERE state_json->'active'->'uploaded_files' IS NOT NULL"
        )
        rows = cur.fetchall()
        cur.close()
    finally:
        conn.close()

    total_threads = 0
    total_inserted = 0
    total_skipped = 0

    for thread_id, uploads_raw in rows:
        if not uploads_raw:
            continue
        uploads = uploads_raw if isinstance(uploads_raw, list) else json.loads(uploads_raw)
        if not isinstance(uploads, list):
            continue

        thread_inserted = 0
        thread_skipped = 0
        for u in uploads:
            if not isinstance(u, dict):
                continue
            # Filter for instant_rag uploads with a usable document_id.
            if u.get("purpose") != "instant_rag":
                continue
            doc_id = str(u.get("document_id") or "").strip()
            if not doc_id:
                # Partial upload that never got a document_id — skip.
                thread_skipped += 1
                continue
            inserted = record_upload(
                document_id=doc_id,
                envelope_id=str(u.get("envelope_id") or u.get("upload_id") or ""),
                upload_id=str(u.get("upload_id") or ""),
                thread_id=str(thread_id),
                filename=str(u.get("filename") or "upload"),
                user_id=None,
                content_type=None,
                byte_size=None,
                chunks_count=int(u.get("row_count") or 0),
                # Don't override expires_at here; record_upload defaults to
                # now + TTL, which is correct for the "first time we see
                # this row" semantics. The original upload's actual expiry
                # isn't recoverable from the JSONB anyway.
            )
            if inserted:
                thread_inserted += 1
            else:
                thread_skipped += 1
        if thread_inserted or thread_skipped:
            total_threads += 1
            total_inserted += thread_inserted
            total_skipped += thread_skipped
            print(
                f"thread={thread_id}: inserted={thread_inserted} skipped={thread_skipped}"
            )

    print()
    print(f"Backfill complete: {total_threads} threads scanned, "
          f"{total_inserted} rows inserted, {total_skipped} skipped "
          f"(already in catalog or missing document_id).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
