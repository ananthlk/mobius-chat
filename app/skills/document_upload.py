"""Document upload skill helpers — re-exports from mobius-skills-core.

Shared by ReAct (_execute_tool), legacy tool path (answer_tool), MCP
docs, and HTTP GET /chat/thread/{thread_id}/uploads.

skills-core refactor (2026-04-20, Day 3):
The canonical content (markdown text + upload-table formatter) moved
to ``mobius_skills_core.skills.document_upload`` and
``mobius_skills_core.skills.list_thread_uploads`` so the MCP server
produces byte-identical output for external consumers. This file
preserves its pre-refactor public API (``DOCUMENT_UPLOAD_SKILL_MARKDOWN``
and ``format_thread_uploads_markdown(tid)``) as thin re-exports /
adapters so existing callers (tests, legacy branches, any future code
that imports these symbols) keep working.

2026-04-18 disconnect note: the roster_reconciliation purpose was
retired along with the credentialing / roster skill set. Only
instant_rag uploads are accepted today; they chunk + embed the
document for retrieval via the ``search_uploaded_document`` skill.
"""

from __future__ import annotations

try:
    from mobius_skills_core.skills.document_upload import (
        DOCUMENT_UPLOAD_MARKDOWN as _CORE_MARKDOWN,
    )
    from mobius_skills_core.skills.list_thread_uploads import (
        run_list_thread_uploads as _run_list_thread_uploads,
    )
    _SKILLS_CORE_AVAILABLE = True
except ImportError:
    # mobius-skills-core is a sibling package not present in CI or plain
    # chat-only environments. Provide minimal stubs so the module imports
    # cleanly; full behaviour requires the package installed.
    _CORE_MARKDOWN = "Upload a document to attach it to this conversation."
    _run_list_thread_uploads = None  # type: ignore[assignment]
    _SKILLS_CORE_AVAILABLE = False


def run_list_thread_uploads(thread_id: str, uploaded_files):  # type: ignore[misc]
    if _run_list_thread_uploads is not None:
        return _run_list_thread_uploads(thread_id=thread_id, uploaded_files=uploaded_files)

    class _Stub:
        text = "(No uploads)"
    return _Stub()


# Legacy public name — re-export the shared constant so callers that
# import ``DOCUMENT_UPLOAD_SKILL_MARKDOWN`` keep working. When it's
# time to drop the alias (consumers all migrated), this re-export
# goes and only the core name remains.
DOCUMENT_UPLOAD_SKILL_MARKDOWN = _CORE_MARKDOWN


def format_thread_uploads_markdown(thread_id: str) -> str:
    """Human-readable list of uploads for a thread — chat-side adapter.

    Reads upload records from in-process thread state, hands them to
    the shared formatter. Consumers that want the same markdown from
    outside the chat process (e.g. the MCP server) source records via
    HTTP and call the shared formatter directly.
    """
    from app.storage.threads import get_state

    tid = (thread_id or "").strip()
    if not tid:
        # Short-circuit the "no thread yet" case via the shared skill
        # so the message text matches across consumers.
        return run_list_thread_uploads("", None).text

    raw = get_state(tid) or {}
    active = raw.get("active") or {}
    files = [u for u in (active.get("uploaded_files") or []) if isinstance(u, dict)]
    return run_list_thread_uploads(
        thread_id=tid,
        uploaded_files=files,
    ).text
