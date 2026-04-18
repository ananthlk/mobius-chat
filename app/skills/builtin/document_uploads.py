"""Builtin skills: document_upload_skill + list_thread_document_uploads.

These are the two simplest skills in the dispatcher — no MCP calls, no
external state, no error paths worth speaking of. Migrating them first
is the "hello world" for the skill registry: if the registry can
dispatch these identically to the legacy ``_answer_tool_impl`` branches,
the harder skills (healthcare_query, web_scrape, google_search) follow
the same pattern.

Legacy branches (still live behind MOBIUS_USE_SKILL_REGISTRY=0):

    app/services/tool_agent.py
        if hint == "document_upload_skill": ...
        if hint == "list_thread_document_uploads": ...

After commit 3 of the registry series those branches are deleted and
this file is the only definition.
"""

from __future__ import annotations

from app.skills.document_upload import (
    DOCUMENT_UPLOAD_SKILL_MARKDOWN,
    format_thread_uploads_markdown,
)
from app.skills.registry import SkillCall, SkillEnvelope, SkillSpec, register


def _run_document_upload_skill(call: SkillCall) -> SkillEnvelope:
    """Show the canned 'how to upload' markdown. Pure: no state reads,
    no network, no branching on ``call.inputs``. If the user asks "how
    do I upload a doc", the planner routes here."""
    return SkillEnvelope(
        text=DOCUMENT_UPLOAD_SKILL_MARKDOWN,
        signal="no_sources",
    )


def _run_list_thread_document_uploads(call: SkillCall) -> SkillEnvelope:
    """List the uploads on the current thread. Reads ``ThreadState`` via
    the same ``format_thread_uploads_markdown`` helper the legacy branch
    used — identical semantics, no behavior change on migration."""
    tid = (call.thread_id or "").strip()
    body = format_thread_uploads_markdown(tid)
    return SkillEnvelope(
        text=body,
        signal="no_sources",
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
    )
)
