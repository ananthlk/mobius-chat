"""Builtin skill: healthcare_query — NPI / ICD-10 / CMS coverage lookup.

skills-core refactor (2026-04-20, Day 3):
Previously reached the healthcare microservice via MCP
(chat → :8006 MCP → :8007 healthcare). Now delegates straight to
``mobius_skills_core.skills.healthcare_query.run_healthcare_query``
which calls the healthcare service directly. Saves one HTTP hop and
removes the chat→MCP startup-order dependency. mobius-skills-mcp
continues to expose the same skill for external consumers using the
same shared core function.

Entity extraction (``extract_entity_from_question``) stays in chat
because it's chat-specific logic — it reads active_context,
planner-provided inputs, and threading state that the shared core
shouldn't know about. The chat picks the question, the shared core
does the HTTP call.

Legacy behavior preserved:
  - Jurisdiction isolation: active-thread payer/state NEVER merged
    into the healthcare question (per the tool-isolation invariant
    locked in test_tool_isolation_v11).
  - Success shape: one SourceRef(document_name="Healthcare lookup",
    source_type="external"), signal=no_sources (external API data,
    not RAG corpus).
  - Error shape: graceful fallback text, signal=no_sources.
"""

from __future__ import annotations

import logging

from app.skills.registry import SkillCall, SkillEnvelope, SkillSpec, SourceRef, register

logger = logging.getLogger(__name__)


def _run(call: SkillCall) -> SkillEnvelope:
    # Lazy import — extract_entity_from_question lives in tool_agent
    # which imports the registry → circular at module load time.
    from app.services.tool_agent import extract_entity_from_question
    from mobius_skills_core.skills.healthcare_query import run_healthcare_query

    source_text = (call.user_message or call.question or "").strip()
    entity = extract_entity_from_question(text=source_text)

    npi = entity.get("npi_number")
    icd = entity.get("icd10_code")
    hc_question = npi or icd or entity.get("raw", "")[:120]
    # Planner may pass an explicit ``question`` through tool_inputs.
    # Prefer it when set and non-empty (more precise than the keyword
    # heuristic).
    if isinstance(call.inputs.get("question"), str) and call.inputs["question"].strip():
        hc_question = call.inputs["question"].strip()

    # Bridge the skill's SkillEvents to the legacy string emit channel —
    # same pattern used for google_search / web_scrape. The chat's emit
    # envelope pipeline sees the ``note`` text unchanged; structured
    # envelope wiring (correlation_id / task-manager promotion) comes
    # with the retrieval-skill migration in Days 4-5.
    from app.skills.skill_event_adapter import make_skill_emitter
    emitter = (
        make_skill_emitter(
            on_thinking=call.emitter,
            correlation_id=(
                getattr(call.pipeline_ctx, "correlation_id", "") or ""
            ),
            thread_id=(call.thread_id or None),
            user_id=(
                getattr(call.pipeline_ctx, "user_id", None)
                if call.pipeline_ctx is not None else None
            ),
        )
        if call.emitter
        else None
    )

    result = run_healthcare_query(question=hc_question, emitter=emitter)

    # The shared SkillResult maps cleanly to the chat's SkillEnvelope.
    # On success: signal="no_sources" with a SourceRef. On tool_error
    # (empty question, network fail, HTTP error): surface the error
    # text verbatim with signal="no_sources" so the chat integrator
    # handles the "lookup failed" case consistently.
    if result.signal == "no_sources" and result.sources:
        # success path — answer returned with a SourceRef
        return SkillEnvelope(
            text=result.text,
            sources=[
                SourceRef(
                    document_name="Healthcare lookup",
                    index=1,
                    text=result.text[:300],
                    source_type="external",
                )
            ],
            signal="no_sources",
        )

    # Empty answer / error path. Preserve the legacy fallback text for
    # cases where the shared skill's message isn't friendly enough.
    fallback_text = result.text or (
        "Healthcare lookup failed. Ensure the healthcare service is running."
    )
    return SkillEnvelope(
        text=fallback_text,
        signal="no_sources",
    )


register(
    SkillSpec(
        name="healthcare_query",
        description=(
            "Healthcare data lookup: ICD-10-CM codes (meaning of F32.1, Z00.00, etc.),\n"
            "  Medicare/Medicaid coverage summaries (NCD/LCD), CPT/HCPCS wording, diagnosis/procedure codes.\n"
            "Also: NPI registry facts when the question is a 10-digit NPI number (same backend as registry lookup).\n"
            "Use when: User asks what a code means, ICD-10, HCPCS, coverage, or NPI-by-number without PML context.\n"
            "Do NOT use for: PML enrollment status (skill is being rebuilt — not available in chat currently).\n"
            "Cannot: PML status without credentialing report; org NPI by name."
        ),
        handler=_run,
        inputs_schema={
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "Optional explicit question override. When "
                    "omitted, the handler extracts NPI / ICD / raw text from "
                    "the user message.",
                },
            },
        },
        requires_jurisdiction=False,
        follow_up_capable=False,
        category="healthcare",
        display_name="Healthcare Code Lookup",
    )
)
