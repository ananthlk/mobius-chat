"""Builtin skills: document_upload_skill + list_thread_document_uploads.

skills-core refactor (2026-04-20, Day 3):
Both skills now delegate to ``mobius_skills_core.skills.*``. The chat-
specific parts — reading thread state for uploaded_files, routing the
planner's call — stay here. The rendering (canned upload markdown,
upload-table markdown) lives in the shared package so the MCP server
produces byte-identical output for external consumers.

Legacy branches deleted in commit 3 of the registry series; this file
is the only definition. Post-refactor the dispatcher still behaves
identically from the planner's perspective.
"""

from __future__ import annotations

from app.skills.registry import SkillCall, SkillEnvelope, SkillSpec, register


def _run_document_upload_skill(call: SkillCall) -> SkillEnvelope:
    """Show the canned 'how to upload' markdown.

    The markdown is defined once in
    ``mobius_skills_core.skills.document_upload.DOCUMENT_UPLOAD_MARKDOWN``
    so the MCP server's equivalent tool returns the same bytes. No
    state reads, no network. If the user asks "how do I upload a
    doc", the planner routes here.
    """
    from mobius_skills_core.skills.document_upload import run_document_upload_info

    emitter = _make_emitter(call)
    result = run_document_upload_info(emitter=emitter)
    return SkillEnvelope(
        text=result.text,
        signal="no_sources",
        extra={"demo": {"script_id": "chat:upload-a-document", "title": "Upload a document"}},
    )


def _run_list_thread_document_uploads(call: SkillCall) -> SkillEnvelope:
    """List the uploads on the current thread.

    Chat fetches the upload records from in-process thread state; the
    shared skill handles formatting. Split chosen so the MCP server
    can source records over HTTP without this file caring.
    """
    from app.storage.threads import get_state
    from mobius_skills_core.skills.list_thread_uploads import run_list_thread_uploads

    tid = (call.thread_id or "").strip()
    # Pull uploads from thread state (same read the legacy helper did).
    uploaded_files: list = []
    if tid:
        raw = get_state(tid) or {}
        active: dict = raw.get("active") or {}
        uploaded_files = [
            u for u in (active.get("uploaded_files") or []) if isinstance(u, dict)
        ]

    emitter = _make_emitter(call)
    result = run_list_thread_uploads(
        thread_id=tid,
        uploaded_files=uploaded_files,
        emitter=emitter,
    )
    return SkillEnvelope(text=result.text, signal="no_sources")


def _make_emitter(call: SkillCall):
    """Build a SkillEvent → chat EmitEnvelope translator bound to this
    call's context, if the caller supplied a thinking emitter."""
    if not call.emitter:
        return None
    from app.skills.skill_event_adapter import make_skill_emitter
    return make_skill_emitter(
        on_thinking=call.emitter,
        correlation_id=(
            getattr(call.pipeline_ctx, "correlation_id", "") or ""
        ),
        thread_id=(call.thread_id or None),
        user_id=(
            getattr(call.pipeline_ctx, "user_id", None)
            if call.pipeline_ctx is not None
            else None
        ),
    )


# ── Registrations ────────────────────────────────────────────────────

register(
    SkillSpec(
        name="document_upload_skill",
        description=(
            "First-class **document upload skill**: how to attach files to this chat thread for downstream tools.\n"
            "Use when: user asks how to upload, attach a roster, send a file, supported formats, API/MCP integration,\n"
            "  or what the upload flow does. Multiple documents may be uploaded over time on the same thread.\n"
            "Does NOT transfer bytes — returns instructions (UI: ⋯ → Upload file; HTTP: POST /chat/roster-upload).\n"
            "Returns: Markdown with purposes, endpoints, and relation to roster reconciliation."
        ),
        handler=_run_document_upload_skill,
        requires_jurisdiction=False,
        follow_up_capable=False,
        category="documents",
        display_name="Document Upload",
    )
)

register(
    SkillSpec(
        name="list_thread_document_uploads",
        description=(
            "List documents already attached to the chat thread (purpose, filename, org, rows, time).\n"
            "Use when: user asks what they uploaded, what's on file, or to confirm prior uploads.\n"
            "thread_id defaults to the current conversation when omitted (server fills from context).\n"
            "Returns: Markdown table of uploads + reconciliation defaults if set."
        ),
        inputs_schema={
            "type": "object",
            "properties": {
                "thread_id": {
                    "type": "string",
                    "description": "Optional; server fills from context when omitted.",
                },
            },
        },
        handler=_run_list_thread_document_uploads,
        requires_jurisdiction=False,
        follow_up_capable=True,
        category="documents",
        display_name="List Thread Uploads",
    )
)
