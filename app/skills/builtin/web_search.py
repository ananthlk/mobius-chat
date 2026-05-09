"""Builtin skill: google_search — web search with auto-scrape + LLM fallback.

Final migration in commit 3. Hardest of the five because it has three
behavior layers the handler must preserve exactly:

  1. Search: build a query (jurisdiction-aware), call
     ``_run_google_search(return_raw_results=True)``, get ``raw_results``
     + ``snippets``.
  2. Auto-scrape: ``score_and_scrape_top_result`` tries up to 3 URLs
     from the raw results; if one succeeds, return scraped content as
     the answer with ``signal=google_only``.
  3. LLM fallback: when all scrapes fail but snippets exist, summarize
     snippets via the LLM provider and append a "verify with payer"
     disclaimer.

Unlike healthcare_query / web_scrape, this skill DOES take
jurisdiction — ``build_search_query`` merges active payer/state into
the query so "provider enrollment" in a Florida Sunshine Health thread
becomes "Sunshine Health Florida Medicaid provider enrollment".

Commit 3 of the registry series also deletes the legacy
``if hint == "google_search"`` branch. Parity with that branch is
locked in ``test_skill_registry_commit3.py``.
"""

from __future__ import annotations

import asyncio
import logging

from app.skills.registry import SkillCall, SkillEnvelope, SkillSpec, SourceRef, register

logger = logging.getLogger(__name__)


def _run(call: SkillCall) -> SkillEnvelope:
    # Lazy-import the same helpers the legacy branch used. Keeps parity
    # mechanical and avoids a circular at module load time.
    from app.services.doc_assembly import (
        RETRIEVAL_SIGNAL_GOOGLE_ONLY,
        RETRIEVAL_SIGNAL_NO_SOURCES,
    )
    from app.services.tool_agent import (
        _extract_domain,
        _run_google_search,
        build_search_query,
        extract_entity_from_question,
        score_and_scrape_top_result,
    )

    question = call.question or ""
    user_message = call.user_message or question

    # Entity + query construction — same path the legacy branch took,
    # so the planner prompt / intent continues to drive search shape.
    entity = extract_entity_from_question(text=user_message)
    active = call.active_context or {}
    question_intent = call.inputs.get("question_intent") if isinstance(call.inputs, dict) else None

    query = build_search_query(entity, active, intent=question_intent)
    if not query.strip():
        query = question.strip()

    if call.emitter:
        call.emitter(f"◌ Searching the web for: {query[:70]}")

    # Raw results: list of {title, snippet, url} — needed for auto-scrape.
    raw_results, snippets, usage, signal = _run_google_search(
        query,
        emitter=call.emitter,
        return_raw_results=True,
    )

    # ── Layer 2: auto-scrape the best-scoring URL ────────────────────
    org_name = entity.get("org_name") or active.get("payer") or None
    state = active.get("jurisdiction") or active.get("state") or "FL"

    content, source_url, ok = score_and_scrape_top_result(
        raw_results,
        org_name=org_name,
        state=state,
        max_attempts=3,
        emitter=call.emitter,
    )

    if ok and content:
        domain = _extract_domain(source_url) or (source_url or "")[:40]
        return SkillEnvelope(
            text=content,
            sources=[
                SourceRef(
                    document_name=domain,
                    source_type="web",
                    url=source_url,
                    # ``confidence_label`` was in the legacy dict; we
                    # stash it in extra so consumers that read it still
                    # can. SourceRef doesn't promote it to a field
                    # because no consumer treats it as structured.
                )
            ],
            usage=usage,
            signal=RETRIEVAL_SIGNAL_GOOGLE_ONLY,
            extra={"confidence_label": "process_confident", "source_url": source_url},
        )

    # ── Layer 3: LLM-summarize snippets when scrape fails ────────────
    if snippets and "No search results" not in snippets:
        if call.emitter:
            call.emitter("Summarizing search results...")
        try:
            from app.services.llm_provider import get_llm_provider

            provider = get_llm_provider()
            prompt = (
                "Use the following web search results to answer the user's question. "
                "Cite sources by number [1], [2], etc.\n\n"
                f"Results:\n{snippets}\n\n"
                f"Question: {question}\n\nAnswer:"
            )
            raw_ans, llm_usage = asyncio.run(provider.generate_with_usage(prompt))
            answer = (raw_ans or "").strip()
            disclaimer = (
                "\n\n[Note: Full page content could not be retrieved. "
                "These are search result summaries only — "
                "verify details directly with the payer.]"
            )
            return SkillEnvelope(
                text=answer + disclaimer,
                sources=[SourceRef(document_name="Web search", source_type="external")],
                usage=llm_usage,
                signal=RETRIEVAL_SIGNAL_GOOGLE_ONLY,
            )
        except Exception as e:
            logger.warning("LLM summarization of search snippets failed: %s", e)
            return SkillEnvelope(
                text=(
                    snippets
                    + "\n\n[Note: These are search result summaries only — "
                    "verify directly with the payer.]"
                ),
                sources=[SourceRef(document_name="Web search", source_type="external")],
                usage=None,
                signal=RETRIEVAL_SIGNAL_GOOGLE_ONLY,
            )

    # ── Layer 4: nothing found ───────────────────────────────────────
    return SkillEnvelope(
        text=snippets or "No relevant information found on the web for this query.",
        usage=usage,
        signal=RETRIEVAL_SIGNAL_NO_SOURCES,
    )


register(
    SkillSpec(
        name="google_search",
        description=(
            "Search the web for current information. LAST-RESORT external lookup.\n"
            "Correct fallback order on a payer-specific question is:\n"
            "  1. search_corpus  (always first)\n"
            "  2. lookup_authoritative_sources  (when corpus is weak/empty —\n"
            "     Mobius's curated URL registry knows the answer often lives\n"
            "     in a payer-published page that isn't indexed YET; ingest_url\n"
            "     can pull it in within seconds)\n"
            "  3. google_search  (ONLY when both of the above came back empty)\n"
            "Use for: general/non-payer questions, user explicitly asks to\n"
            "  search the web, or steps 1+2 found nothing relevant.\n"
            "Do NOT use as the immediate fallback after a corpus miss on a\n"
            "  payer-specific question — call lookup_authoritative_sources\n"
            "  FIRST. The curator likely has the URL.\n"
            "Returns: URLs and snippets, then auto-scrapes top result."
        ),
        handler=_run,
        inputs_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "question_intent": {
                    "type": "string",
                    "description": "Optional planner-provided intent used to qualify the search query.",
                },
            },
        },
        # google_search is the one chat skill that SHOULD receive active
        # jurisdiction — "Sunshine Health provider enrollment" in a
        # Florida Sunshine thread becomes a much better query with state
        # + payer merged in via build_search_query.
        requires_jurisdiction=True,
        follow_up_capable=False,
        category="web",
        display_name="Google Search",
    )
)
