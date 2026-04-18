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
            "Explain how to upload a document (PDF / DOCX / CSV) into this "
            "thread for retrieval or roster reconciliation. Use when the "
            "user asks how to attach a file or what formats are supported."
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
            "List the documents the user has uploaded to the current thread, "
            "with filename, purpose, and upload time. Use when the user asks "
            "'what files have I uploaded?' or similar."
        ),
        handler=_run_list_thread_document_uploads,
        requires_jurisdiction=False,
        follow_up_capable=True,
    )
)
