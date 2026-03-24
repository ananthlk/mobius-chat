"""Document upload skill — first-class attachment of files to a chat thread.

Shared by ReAct (_execute_tool), legacy tool path (answer_tool), MCP docs, and HTTP GET /chat/thread/.../uploads.
"""

from __future__ import annotations

from typing import Any

DOCUMENT_UPLOAD_SKILL_MARKDOWN = """
## Document upload skill (Mobius Chat)

**What it does:** Attach files to **this chat thread** so other tools can use them. You may upload **different documents at different times**; each upload is stored on the thread (timestamp, purpose, filename, row counts).

**End user:** Tap **⋯** next to Send → **Upload file** → choose **file purpose** (e.g. roster for reconciliation) → organization name when required → pick **CSV or Excel**.

**Purposes today:**
- `roster_reconciliation` — provider roster for upload vs outside-in reconciliation.

**HTTP API (integrations / MCP):**
- `POST /chat/roster-upload` — multipart: `file`, `org_name`, `file_purpose` (optional), `thread_id` (optional; response returns `thread_id`).
- `GET /chat/thread/{thread_id}/uploads` — list uploads on that thread (newest first), plus reconciliation pointers.

**Note:** File bytes cannot be sent inside plain chat text; use the UI or multipart POST.

**Next step:** After a roster upload, ask to run the **roster reconciliation report** for the same org; the server fills `upload_id` and billing NPI from thread state.
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
    rec = (active.get("reconciliation_upload_id") or "").strip()
    if rec:
        lines.append("")
        lines.append(
            f"**Reconciliation default:** upload `{rec[:12]}…`, billing NPI "
            f"`{(active.get('reconciliation_org_id') or '').strip() or '—'}`, org "
            f"`{(active.get('reconciliation_org_name') or '').strip() or '—'}`."
        )
    return "\n".join(lines)
