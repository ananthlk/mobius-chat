"""Builtin skill: healthcare_query — NPI / ICD-10 / CMS coverage via MCP.

Third migration in the skill-registry refactor (commit 2). Routes to the
mobius-healthcare MCP via ``call_mcp_tool(TOOL_HEALTHCARE_QUERY, ...)``.

Parity with the legacy ``if hint == "healthcare_query"`` branch:

  - Entity extraction: pulls ``npi_number``, ``icd10_code``, or
    ``raw[:120]`` from the user's question text via
    ``extract_entity_from_question`` (same helper the legacy branch
    uses). Active jurisdiction is deliberately NOT merged into the
    query — healthcare_query is an entity-lookup tool; jurisdiction
    leaking in produces wrong NPIs for the wrong state.

  - Error shape: MCP exceptions return "I ran into an issue. {e}.
    Please try again." with no sources, signal=no_sources.

  - Success shape: one source with document_name="Healthcare lookup",
    the first 300 chars of the response as preview, signal=no_sources
    (this signal is correct — healthcare_query data isn't RAG corpus
    and isn't google-scraped web content; it's an external API lookup).

Envelope shape is captured in ``test_skill_registry_commit2.py`` so
commit 3's deletion of the legacy branch can't silently change it.
"""

from __future__ import annotations

import logging

from app.skills.registry import SkillCall, SkillEnvelope, SkillSpec, SourceRef, register

logger = logging.getLogger(__name__)

# Keep in sync with tool_agent.TOOL_HEALTHCARE_QUERY. Duplicated here so
# this module doesn't have to import tool_agent (which itself imports the
# registry — circular). Commit 3 can consolidate.
_TOOL_HEALTHCARE_QUERY = "healthcare_query"


def _run(call: SkillCall) -> SkillEnvelope:
    # Lazy imports: call_mcp_tool is bound at tool_agent import time for
    # the legacy branch; importing it at module level here would bind a
    # SECOND reference that mock-patching ``app.services.mcp_manager.call_mcp_tool``
    # couldn't reach. Pulling it in at call time means patches apply
    # uniformly across both dispatch paths. extract_entity_from_question
    # lives in tool_agent which imports the registry → circular at
    # module load.
    from app.services.mcp_manager import call_mcp_tool
    from app.services.tool_agent import extract_entity_from_question

    source_text = (call.user_message or call.question or "").strip()
    entity = extract_entity_from_question(text=source_text)

    npi = entity.get("npi_number")
    icd = entity.get("icd10_code")
    hc_question = npi or icd or entity.get("raw", "")[:120]
    # Planner may also pass an explicit ``question`` through tool_inputs.
    # Prefer it when set and non-empty (lets the planner be more precise
    # than the keyword heuristic).
    if isinstance(call.inputs.get("question"), str) and call.inputs["question"].strip():
        hc_question = call.inputs["question"].strip()

    try:
        result_text, success = call_mcp_tool(
            _TOOL_HEALTHCARE_QUERY,
            {"question": hc_question},
        )
    except Exception as e:
        logger.warning("call_mcp_tool healthcare_query failed: %s", e, exc_info=True)
        return SkillEnvelope(
            text=f"I ran into an issue. {e}. Please try again.",
            signal="no_sources",
        )

    if success and result_text and "Error:" not in result_text:
        return SkillEnvelope(
            text=result_text,
            sources=[
                SourceRef(
                    document_name="Healthcare lookup",
                    index=1,
                    text=result_text[:300],
                    source_type="external",
                )
            ],
            signal="no_sources",
        )

    return SkillEnvelope(
        text=(
            result_text
            if result_text
            else "Healthcare lookup failed. Ensure mobius-healthcare API is running."
        ),
        signal="no_sources",
    )


register(
    SkillSpec(
        name="healthcare_query",
        description=(
            "Answer healthcare lookup questions: NPI lookup by number, "
            "ICD-10 code meaning, CMS coverage (NCD/LCD), prior-auth status. "
            "Uses the mobius-healthcare MCP (NPPES + CMS data). Does NOT "
            "take jurisdiction — jurisdiction would produce wrong NPIs for "
            "entity lookups."
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
    )
)
