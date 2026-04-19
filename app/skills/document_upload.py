"""Document upload skill — first-class attachment of files to a chat thread.

Shared by ReAct (_execute_tool), legacy tool path (answer_tool), MCP docs,
and HTTP GET /chat/thread/{thread_id}/uploads.

2026-04-18 disconnect note: the roster_reconciliation purpose was retired
along with the credentialing / roster skill set. Only instant_rag uploads
are accepted today; they chunk + embed the document for retrieval via the
``search_uploaded_document`` skill.
"""

from __future__ import annotations

from typing import Any

DOCUMENT_UPLOAD_SKILL_MARKDOWN = """
## Document upload skill (Mobius Chat)

**What it does:** Attach files to **this chat thread** so other tools can search them. Each upload is chunked + embedded once; afterwards you can ask natural-language questions and the `search_uploaded_document` skill retrieves the relevant passages with page citations. You can upload **multiple documents at different times**; each is stored on the thread with a timestamp and filename.

**End user:** Tap **⋯** next to Send → **Upload file** → pick a **PDF, DOCX, CSV, or XLSX**. The upload runs instant-RAG in the background; a receipt banner confirms when indexing is complete.

**Supported purpose:**
- `instant_rag` — the default. Chunks + embeds the document so `search_uploaded_document` can search inside it.

**HTTP API (integrations / MCP):**
- `POST /chat/roster-upload` — multipart form: `file`, `org_name`, `file_purpose="instant_rag"`, `thread_id` (optional; response returns the thread_id used).
- `GET /chat/thread/{thread_id}/uploads` — list uploads on the thread (newest first), each with filename, purpose, row/chunk count, and timestamp.

**Note:** File bytes cannot be sent inside plain chat text; use the UI button or multipart POST. `file_purpose` values other than `instant_rag` return 400 today — roster / credentialing uploads will come back as their own skill integration.

**Next step:** After uploading, ask a question about the document (e.g. *"what does section 3.2 say about prior auth?"*). Chat will pick `search_uploaded_document` and return scoped chunks with page citations — no separate search command needed.
""".strip()


def format_thread_uploads_markdown(thread_id: str) -> str:
    """Human-readable list of uploads for a thread (matches ReAct / MCP behavior)."""
    from app.storage.threads import get_state

    tid = (thread_id or "").strip()
    if not tid:
        return "No chat thread is available yet. Send a message in Mobius Chat first, then ask what files are attached."

    raw = get_state(tid) or {}
    active: dict[str, Any] = raw.get("active") or {}
    files = [u for u in (active.get("uploaded_files") or []) if isinstance(u, dict)]
    lines = [
        f"**Thread:** `{tid}`",
        f"**Uploads on file:** {len(files)} (newest listed first)",
        "",
    ]
    if not files:
        lines.append("No documents uploaded yet. Use ⋯ → **Upload file**, or `POST /chat/roster-upload`.")
    else:
        lines.append("| # | Purpose | File | Organization | Rows | Uploaded (UTC) |")
        lines.append("|---|---------|------|--------------|------|----------------|")
        for i, u in enumerate(files[:20], 1):
            lines.append(
                f"| {i} | {(u.get('purpose') or '—').replace('|', '/')} | "
                f"{(u.get('filename') or '—').replace('|', '/')} | "
                f"{(u.get('org_name') or '—').replace('|', '/')} | "
                f"{u.get('row_count', '—')} | {(u.get('uploaded_at') or '—').replace('|', '/')} |"
            )
        if len(files) > 20:
            lines.append(f"\n_Showing 20 of {len(files)} uploads._")
    return "\n".join(lines)
