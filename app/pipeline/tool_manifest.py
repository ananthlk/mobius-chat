"""TOOL_MANIFEST — the planner prompt's catalog of dispatchable tools.

Hybrid shape after skill-registry commit 3:

  - Five tools are now **registry-owned** (document_upload_skill,
    list_thread_document_uploads, healthcare_query, web_scrape,
    google_search). Their descriptions live on ``SkillSpec.description``
    and we render them here via ``registry.manifest_text(...)``. Adding
    a new answer_tool-dispatched skill is one file — no edit here.

  - Four tools are **router-owned**: ``search_corpus``,
    ``healthcare_npi_lookup``, ``search_uploaded_document``, and
    ``refuse`` dispatch in ``app/pipeline/react_loop.py``, not through
    ``answer_tool``. They don't fit the ``SkillSpec`` contract cleanly
    yet (search_corpus is the RAG pipeline; refuse is a terminal
    short-circuit). Their manifest prose stays here until a future
    refactor unifies react_loop's dispatch behind the same registry.

  - ``ENTITY_TOOLS`` and ``FOLLOW_UP_CAPABLE`` are union sets:
    registry-derived for the five migrated skills, plus hand-listed
    additions for the router-owned ones. When healthcare_npi_lookup
    etc. get their own ``SkillSpec``s, the hand-listed parts shrink.

The planner sees the same text it saw before commit 3 — order
preserved, prose preserved. This is deliberate: any drift in the
planner prompt is a behavior change, and behavior changes aren't what
this refactor is about.
"""

from app.skills import registry
from app.stages.agents.capabilities import tool_capabilities_for_parser


# ── Retrieval methodology primer (read once, applies to all 3 search tools) ──
#
# The LLM reasons better about WHEN to switch tools when it understands
# WHY each tool surfaces different content. This block goes ahead of the
# search-tool descriptions so the per-tool blocks can stay short and the
# behavioral rules (e.g. "switch to recall if BM25 dominates") follow
# from first principles instead of memorized symptoms.

_FL_MEDICAID_DATA_ROUTING_BLOCK = """\
FL MEDICAID BH MARKET DATA — read this FIRST before picking a tool.

These tools query BigQuery directly and return verified numbers. Use them
(NOT search_corpus) whenever the question is about quantitative FL Medicaid
behavioral-health market data:

  • Org rankings, market share, total benes/revenue/claims by org or type
  • New entrant analysis — who entered, when, which codes/service lines
  • Benchmarks — how a specific org compares to peers or the market
  • Rate benchmarks — HCPCS-level rates, gaps, trends
  • Service-line mix, utilization, market retention
  • Market size totals or year-over-year trends (2019–2024)

Quick-pick guide (use the first match):
  ┌─────────────────────────────────────────────────────────────────────┐
  │ Largest / top orgs, who serves the most benes  → get_top_orgs      │
  │ New entrants, who captured CMHC share          → get_entrant_analysis │
  │ Specific org profile (name given)              → get_org_profile    │
  │ How org X compares to peers                    → get_org_benchmark  │
  │ Market share over time (2019-2024)             → get_market_timeseries │
  │ Official FL Medicaid fee schedule (ceiling rate) → get_published_rates │
  │ Rate benchmarks, actual paid P50/P75           → get_rate_benchmarks │
  │ Service line breakdown / mix                   → get_market_decomposition │
  │ Find an org by name / get its slug             → search_orgs        │
  │ All BHPF or FBHA member orgs                   → get_org_universe   │
  └─────────────────────────────────────────────────────────────────────┘

Full parameter details for every get_* tool are in the
"Auto-discovered tools (from MCP)" section below — refer there for
optional filter params (org_type, period_year, service_line, etc.)."""


_RETRIEVAL_METHODOLOGY_PRIMER = """\
RETRIEVAL — one tool, one index, three modes.

search_corpus is the single entry point for all corpus retrieval.
The ``mode`` parameter selects the scoring arm:

  auto (default) — BM25 + pgvector hybrid via Reciprocal Rank Fusion.
    Best for most questions. Balances keyword precision and semantic
    recall in one call. Always use this (omit mode entirely or pass
    mode="auto"). The corpus search agent selects the optimal internal
    strategy and cascades through fallbacks automatically.

  precision / recall — available but NOT for planner use. The agent's
    internal router already applies these arms when appropriate. Passing
    an explicit mode overrides the agent's cascade and prevents it from
    trying other strategies on your behalf. Do not pass mode="precision"
    or mode="recall" — leave mode unset on every call.

RETRY GUIDANCE:
  • When the first search returns weak results, retry with a BETTER
    QUERY (sharper noun, different phrasing, drop payer-specific terms)
    but NO mode override. The agent handles arm selection internally.
  • After at most two query reformulations, surface the gap honestly —
    rephrasing the same query without new information won't help further."""


# ── Router-owned prose (search_corpus, healthcare_npi_lookup, etc.) ──

_SEARCH_CORPUS_BLOCK = """\
search_corpus(query)
  Corpus search — hybrid BM25 + pgvector by default. Single entry
    point for all curated-corpus retrieval. Always omit the mode
    parameter — the agent selects and cascades through strategies
    internally. See the methodology primer above.
  Use for: Questions about policy, PA rules, appeals, credentialing
    process, timely filing, covered services, claims procedures —
    anything answered from authoritative payer documents or manuals.
  Do NOT use for: FL Medicaid BH market data questions (org rankings,
    market share, new entrants, benchmarks, rate data, utilization).
    Those are quantitative BigQuery data — use the get_* analytics
    tools listed in the FL MEDICAID BH MARKET DATA section above.
  Aliases: corpus, default_search, hybrid_search, precision_search,
    explore_search, recall_search (all route here).
  Returns: numbered passages [1]…[N] with page citations, per-arm
    provenance (retrieval_arms), and confidence label.

  FAILURE MODES:
    F1. PRECISION DEFICIT — chunks on-topic but missing the specific
        code / day count / dollar amount / form name.
        Action: retry with a SHARPER QUERY (pull the exact noun from
          prior chunks as the anchor; drop generic words like "rules"
          or "information"). Do NOT pass mode="precision".

    F2. RECALL DEFICIT — hits dominated by one doc/payer when user
        wanted breadth ("tell me about X across payers").
        Action: retry with a MORE CONCEPTUAL QUERY (drop payer-specific
          phrasing, raise abstraction level). Do NOT pass mode="recall".

    F3. ZERO HITS — retry once with a reformulated query. If still
        zero, surface the coverage gap honestly.

    F4. NO ANSWER AFTER ONE ESCALATION — surface the gap. Do not
        keep searching; both arms are paraphrase-invariant."""


_RECALL_SEARCH_BLOCK = ""  # retired — merged into search_corpus(mode="recall")


_PRECISION_SEARCH_BLOCK = ""  # retired — merged into search_corpus(mode="precision")

_HEALTHCARE_NPI_LOOKUP_BLOCK = """\
healthcare_npi_lookup(question)
  NPPES registry lookup ONLY when the user gives or asks about a 10-digit NPI number
    (name, taxonomy, address from the national registry).
  Do NOT use for: ICD-10, diagnosis codes, CPT, HCPCS, "what is code …", Medicare coverage, NCD/LCD —
    those are healthcare_query.
  Use when: question is specifically NPI-registry lookup by number."""

_SEARCH_UPLOADED_DOCUMENT_BLOCK = """\
search_uploaded_document(upload_id optional, query)
  **Instant-RAG** — search *inside* a user-uploaded document on this thread.
  Use when: the user refers to a document they attached (e.g. "what does my
    uploaded doc say about X", "summarize the PDF I just sent", "find the
    prior-auth rules in this manual") AND there is at least one instant_rag
    upload on the thread (see list_thread_document_uploads).
  upload_id: if omitted and exactly one instant_rag upload exists on the
    thread, the server auto-resolves. If multiple uploads exist, pick the
    one the user's question references (use list_thread_document_uploads
    to see filenames).
  QUERY RULES — the query is a semantic vector search, not a command:
    • For summarization ("summarize", "overview", "what is in this"):
        Use a CONTENT query, not a procedural one. Good: the document's
        filename or apparent topic (e.g. "provider billing manual overview"
        or "behavioral health claims policy"). Bad: "summarize this document"
        (that is a command — it matches nothing in the document's text).
        Call this tool 2-3 times with different topic queries to build
        broad coverage, then synthesize.
    • For specific questions: use the exact terms the user asked about
        (e.g. "timely filing deadline" or "prior authorization H0036").
    • If this tool returns empty and the document was JUST uploaded:
        The document may still be indexing. Tell the user: "Your document
        is still being processed — please wait a few seconds and ask again."
        Do NOT retry with the same query.
  This tool does NOT search the curated corpus — use search_corpus for that.
  Chunks returned are scoped to the one document, no tag filters. Use this
  tool BEFORE search_corpus when the user's question is self-referential
  to an upload; otherwise prefer search_corpus.
  Returns: matched chunks with page citations from the uploaded document."""

_REFUSE_BLOCK = """\
refuse(reason)
  Hard stop — no content returned.
  Use for: any question about a specific patient (PHI),
    any clinical treatment recommendation.
  "Is member 12345 eligible?" → refuse (PHI)
  "What are eligibility rules?" → search_corpus (not PHI)"""

# Registry skills, in the order the legacy manifest listed them so the
# planner prompt byte-diff stays minimal across the refactor.
_REGISTRY_ORDER: tuple[str, ...] = (
    "healthcare_query",
    "document_upload_skill",
    "list_thread_document_uploads",
    "google_search",
    "web_scrape",
)


# ── Curator tools (Phase 13.5) — surface URLs we know about even ─────
# when they aren't in the indexed corpus yet.

_LOOKUP_AUTHORITATIVE_SOURCES_BLOCK = """\
lookup_authoritative_sources(payer?, state?, topic?, authority_level?)
  Search Mobius's curated registry of authoritative URLs for a payer/
    state/topic. Returns URLs Mobius has *seen* — both already-indexed
    docs AND known sources that haven't been pulled into the corpus yet.
    Backed by the discovered_sources table; fed by the curator's
    sitemap parser + scraper link extraction.

  ★ ESCALATION ROLE ★ — This is the **mandatory next step** when
    search_corpus returns weak/no hits on a payer-specific question.
    The curator's URL registry is much more likely to contain the
    answer than google_search. The correct order is:
      search_corpus → lookup_authoritative_sources → (ingest_url if
      a relevant URL has ingested=false) → search_corpus again →
      google_search ONLY if all of that fails.
    Skipping this step and going straight to google_search is wrong
    on payer-specific questions.

  Use when:
    - search_corpus came back with weak/no hits and you suspect the
      answer lives in a doc Mobius knows about but hasn't indexed.
    - You want to enumerate "what does Mobius know exists for X" before
      committing to ingest_url.
    - The user asks "do you have <X>?" — a hit here means yes, even if
      not in the corpus yet.
  Inputs (any combination):
    payer            — canonical payer name, e.g. 'Sunshine Health', 'AHCA'
    state            — 2-letter state code, e.g. 'FL'
    topic            — semantic tag, e.g. 'ECT', 'PA', 'appeals'
    authority_level  — 'payer_manual' | 'payer_policy' | 'member_handbook' | etc.
  Returns: list of {url, host, payer, ingested, last_seen_at, content_kind}.
    The ``ingested: bool`` flag tells you whether the URL is already in
    the corpus (cite it from search_corpus) or not.

  Pairing with downstream tools — pick one of these when a returned
  URL with ``ingested: false`` matches the question:
    • ``ingest_url(url)`` — when the URL is authoritative + likely to
      be cited again (provider manuals, policy PDFs, member handbooks,
      enrollment guides). Adds it to the corpus permanently;
      future search_corpus calls cite it. Costs Vertex tokens; uses
      the rag-admin auth path. Best when the user's question is
      policy/process and the URL is clearly the right source.
    • ``web_scrape(url)`` — when you just need to READ the page right
      now without permanent indexing (one-off lookups, exploratory
      "what's on this page" questions, time-sensitive content like
      news/announcements). No admin auth, fast, content stays in the
      turn only. Best for "go deeper on what this URL says" without
      committing to long-term storage.

  Auto-route guidance: for a single high-confidence URL match (host =
    payer's domain AND path/topic clearly aligns), proceed with the
    appropriate tool above without re-asking. Ask only when multiple
    plausible URLs are returned and the right one is ambiguous, or
    when the user's intent (cite vs. read-once) isn't clear from the
    question.

  Do NOT use for: free-text web search — that's google_search. This
    only knows about Mobius's curated registry."""


_INGEST_URL_BLOCK = """\
ingest_url(url)
  Fetch a single URL and add it to the indexed corpus right now. Goes
    through the same chunking + embedding + lexicon + publish pipeline
    that scraped PDFs go through; the new content is queryable in chat
    within minutes.
  Use ONLY when:
    - lookup_authoritative_sources surfaced a non-ingested URL the
      user explicitly asked you to fetch, OR
    - The user pasted a specific authoritative URL they want indexed
      (provider manual, policy PDF, criteria doc, etc.).
  Do NOT use for:
    - Arbitrary URLs the user hasn't approved (ingestion costs Vertex
      tokens + storage; require explicit "yes, fetch it").
    - Bulk URL lists — call repeatedly, one per turn, with confirmation.
    - URLs that lookup_authoritative_sources reported as
      curation_status='blocked' or 'needs_auth' — those won't fetch
      cleanly; ask the user to upload the PDF manually instead.
  Inputs:
    url — the canonical URL to fetch + index. PDFs and HTML pages both
          work; the inlet is auto-detected.
  Returns: {document_id, status, sections}. After this returns ok,
    immediately call search_corpus with the original question — the
    new doc is now available to retrieve."""


_AUTO_DISCOVERED_HEADER = """\
── Auto-discovered tools (from MCP) ─────────────────────────────────────
These tools are published by a remote MCP server and auto-registered at
chat startup. The descriptions below come from each tool's ``description``
field in its MCP ``list_tools`` response — if a description reads vague or
incomplete, fix it on the MCP server side so this section stays useful to
the planner."""


def _auto_discovered_block(allowed: frozenset[str] | None = None) -> str:
    """Render the MCP-sourced, planner-visible skills that aren't in the
    curated builtin ordering.

    An MCP tool gets auto-appended to the planner manifest when:
      - ``SkillSpec.source == "mcp"`` (set by ``mcp_adapter`` when the
        skill was registered from ``list_mcp_tools()``), AND
      - ``SkillSpec.visible_to_planner == True`` (the default; operators
        can flip this to hide experimental tools), AND
      - the name is NOT already rendered in the curated ``_REGISTRY_ORDER``
        block above (which would mean a builtin and an MCP tool share a
        name — "builtins win" collision policy already prevented the
        register, so this is belt-and-suspenders), AND
      - when ``allowed`` is provided, the name is in the allowed set.

    Returns an empty string when no MCP tools are registered so callers
    can skip the section header.
    """
    mcp_names = registry.names_by_source("mcp")
    visible = registry.planner_visible_names()
    curated = frozenset(_REGISTRY_ORDER)
    render = tuple(sorted(
        n for n in mcp_names
        if n in visible and n not in curated
        and (allowed is None or n in allowed)
    ))
    if not render:
        return ""
    body = registry.manifest_text(names=render)
    if not body.strip():
        return ""
    return f"{_AUTO_DISCOVERED_HEADER}\n\n{body}"


# Tools that are router-owned (not in the SkillSpec registry) but still
# need to be filterable via ``allowed``.  Each maps to the prose block
# variable that carries its manifest text.
_ROUTER_OWNED_BLOCKS: dict[str, str] = {
    "search_corpus": "_SEARCH_CORPUS_BLOCK",
    "healthcare_npi_lookup": "_HEALTHCARE_NPI_LOOKUP_BLOCK",
    "search_uploaded_document": "_SEARCH_UPLOADED_DOCUMENT_BLOCK",
    "refuse": "_REFUSE_BLOCK",
    # Curator tools aren't in the registry either
    "lookup_authoritative_sources": "_LOOKUP_AUTHORITATIVE_SOURCES_BLOCK",
    "ingest_url": "_INGEST_URL_BLOCK",
}


def _compose_manifest(allowed: frozenset[str] | None = None) -> str:
    """Splice router-owned prose with registry-rendered skill blocks.

    Block order (deliberate — planner reads top-down):
      1. Curated router-owned + builtin skills (stable, hand-tuned prose).
      2. Auto-discovered MCP skills (dynamic, rebuilt each call).
      3. Per-tool capabilities footer (structured JSON for parser use).

    Args:
        allowed: When provided, only tools whose names are in this set are
            rendered into the manifest. ``None`` means "render everything"
            (the normal path for all modes except task mode or when a user
            has restricted their tool list).
    """
    def _allow(name: str) -> bool:
        return allowed is None or name in allowed

    def _registry_block(name: str) -> str:
        if not _allow(name):
            return ""
        return registry.manifest_text(names=(name,))

    def _router_block(name: str, prose: str) -> str:
        if not _allow(name):
            return ""
        return prose

    curated_blocks = [
        # FL Medicaid data routing gate — must come FIRST so the planner
        # sees the quick-pick guide before it reaches search_corpus.
        # Without this, the planner over-selects search_corpus for
        # quantitative market data questions (which aren't in the corpus).
        _FL_MEDICAID_DATA_ROUTING_BLOCK if _allow("get_top_orgs") or allowed is None else "",
        # Methodology primer — the search tools below reference its
        # concepts (BM25 / vector / RRF). Without this the LLM has to
        # infer mechanism from per-tool symptom lists.
        _RETRIEVAL_METHODOLOGY_PRIMER if _allow("search_corpus") else "",
        _router_block("search_corpus", _SEARCH_CORPUS_BLOCK),
        _RECALL_SEARCH_BLOCK,   # retired prose — always empty
        _PRECISION_SEARCH_BLOCK,  # retired prose — always empty
        # fetch_document — registered via SkillSpec; rendered by registry.
        # Distinct from search_corpus: returns a download URL, not an answer.
        _registry_block("fetch_document"),
        _registry_block("healthcare_query"),
        _router_block("healthcare_npi_lookup", _HEALTHCARE_NPI_LOOKUP_BLOCK),
        _registry_block("document_upload_skill"),
        _registry_block("list_thread_document_uploads"),
        _router_block("search_uploaded_document", _SEARCH_UPLOADED_DOCUMENT_BLOCK),
        _registry_block("google_search"),
        _registry_block("web_scrape"),
        # vibe: short, work-adjacent vibe lines (toast/empathy/dry obs/etc.)
        # Registered but was missing from the planner manifest until 2026-04-25.
        _registry_block("vibe"),
        # product_feedback: capture open product feedback + CSAT/NPS surveys.
        # Same lesson as vibe — registered + visible_to_planner but the manifest
        # is an explicit list, so it must be named here or the planner never
        # sees it (2026-07-02).
        _registry_block("product_feedback"),
        # Phase 13.6 — conversation-aware planner. Continuation/
        # transformation requests ("convert this to an appeal letter",
        # "make it shorter", "rewrite for X") MUST route here, NOT to
        # search_corpus / lookup_authoritative_sources. Placed before
        # the curator/google blocks so the planner sees it as a first-
        # class option for follow-up turns.
        _registry_block("transform_previous_answer"),
        # Curator tools (Phase 13.5) — registry of URLs Mobius knows
        # about, including non-ingested ones. Ordered after the search
        # tools so the planner reaches for search_corpus first; only
        # falls through to lookup_authoritative_sources when corpus
        # comes up empty.
        _router_block("lookup_authoritative_sources", _LOOKUP_AUTHORITATIVE_SOURCES_BLOCK),
        _router_block("ingest_url", _INGEST_URL_BLOCK),
        _router_block("refuse", _REFUSE_BLOCK),
    ]
    auto_block = _auto_discovered_block(allowed=allowed)
    if auto_block:
        curated_blocks.append(auto_block)
    joined = "\n\n".join(b for b in curated_blocks if b.strip())
    if not joined.strip():
        # No tools rendered (allowed=[]) — return a minimal no-tools notice
        # so the system prompt is valid but the planner doesn't hallucinate tools.
        return "\nNo tools available for this request.\n"
    return f"""
AVAILABLE TOOLS — match the question to the tool whose capabilities fit.
If the first tool fails (e.g. returns no results), try the next-best tool.

WORKFLOW SELECTION (chat UI) — The server may attach **clarification_options** on the assistant
response: clickable choices (single- or multi-select). Users may also type a normal message instead
(or in addition, depending on UI); the next turn is still plain user text for you to interpret.
When choice chips appear, keep your summary short and point the user to the buttons; do not invent
a separate prose-only list as the only way to proceed.

════════════════════════════════════════════
{joined}

PER-TOOL CAPABILITIES (explicit):
{tool_capabilities_for_parser()}
════════════════════════════════════════════
"""


def get_tool_manifest(allowed: list[str] | None = None) -> str:
    """Fresh manifest, composed at call time.

    Args:
        allowed: Optional list of tool names to include. When None (the
            normal path), the full manifest is rendered. When an empty
            list, the manifest contains "No tools available." When a
            non-empty list, only the named tools appear — this is the
            tool-policy filter path used when a user has disabled some
            tools or when the pipeline runs in task mode with a restricted
            tool set.

    Lazy composition matters because ``register_mcp_skills()`` runs at
    FastAPI startup — AFTER modules that depend on the manifest may have
    been imported. If the planner's reasoning-system prompt captured
    the manifest at import time, MCP tools registered during startup
    would be invisible to the planner until the next process restart.
    Re-rendering each call costs one registry scan (<1ms for <100 skills)
    which is negligible compared to the LLM call it feeds into.
    """
    allowed_set: frozenset[str] | None = (
        None if allowed is None else frozenset(allowed)
    )
    return _compose_manifest(allowed=allowed_set)


# Back-compat: modules that still do ``from ... import TOOL_MANIFEST``
# get the manifest as-of-import. Most callers should switch to
# ``get_tool_manifest()`` so MCP-registered tools show up without a
# restart. Kept as a module-level __getattr__ so each read re-renders,
# matching the semantics operators expect from a "current manifest"
# symbol.
def __getattr__(name: str) -> str:
    if name == "TOOL_MANIFEST":
        return get_tool_manifest()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# ── Sets: union of registry-derived + router-owned ────────────────────
#
# ``healthcare_npi_lookup`` dispatches in react_loop and never takes
# jurisdiction; it belongs to ENTITY_TOOLS until it gets its own
# SkillSpec. search_corpus / refuse / search_uploaded_document aren't
# entity tools so they don't appear here.

_NON_REGISTRY_ENTITY_TOOLS = frozenset({
    "healthcare_npi_lookup",
})

ENTITY_TOOLS = registry.entity_tools() | _NON_REGISTRY_ENTITY_TOOLS

# Post-disconnect, only list_thread_document_uploads is follow-up-capable.
# That comes from the registry via the SkillSpec.follow_up_capable flag.
FOLLOW_UP_CAPABLE = registry.follow_up_capable()
