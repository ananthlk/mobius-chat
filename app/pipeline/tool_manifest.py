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

_RETRIEVAL_METHODOLOGY_PRIMER = """\
RETRIEVAL METHODOLOGY — three search tools, one diagnostic model.

The three corpus search tools (search_corpus, precision_search,
explore_search) share one index but score candidates by
FUNDAMENTALLY DIFFERENT mechanisms:

  • BM25 — bag-of-words token-frequency scoring. Used by
    precision_search and as one arm of search_corpus. Rewards
    documents with high literal-token overlap with the query.
    Strong on exact codes, IDs, verbatim phrases. Weak when the
    user's question uses different vocabulary than the corpus.
    Paraphrase-invariant: rewording the same query → same chunks.

  • Vector / pgvector ANN — cosine similarity between query and
    chunk embeddings in 1536-D space. Used by explore_search and
    as the other arm of search_corpus. Rewards semantic neighbors,
    not literal token match. Strong on conceptual / paraphrased
    questions, surfaces docs that don't share vocabulary with the
    query. Also paraphrase-invariant on the SAME concept.

  • RRF fusion — search_corpus runs BM25 and vector arms in
    parallel, fuses by reciprocal-rank, and applies a healthcare
    reranker. Balances precision (BM25) and recall (vector) in
    one call.

DECISION MODEL — one default, two diagnostic escalations.

  (1) DEFAULT: search_corpus. It tries to balance both signals.
      Right for almost every first call.

  (2) AFTER search_corpus, classify what the answer was LACKING:

      ┌──────────────────────────┬──────────────────────────────┐
      │ LACKING PRECISION        │ LACKING RECALL               │
      │ ───────────────────      │ ───────────────              │
      │ Chunks are roughly       │ Chunks are all from one doc  │
      │ on-topic but didn't pin  │ or one source; question      │
      │ down the specific code,  │ wanted breadth across docs/  │
      │ ID, day count, dollar    │ payers/topics; we surfaced   │
      │ amount, form name, or    │ a narrow slice when the user │
      │ verbatim policy text     │ asked for a survey or scan   │
      │ the user wanted.         │ of what the corpus knows.    │
      │                          │                              │
      │ Action: precision_search │ Action: explore_search        │
      │ with a SHARPER query —   │ with a REFRAMED query —      │
      │ go DEEPER on the         │ broaden conceptually, drop   │
      │ specific thing that's    │ payer/scope constraints,     │
      │ missing. Use the names,  │ shift to higher-level        │
      │ codes, document titles,  │ vocabulary that captures     │
      │ or exact phrases the     │ the wider topic the user     │
      │ corpus chunks already    │ is exploring.                │
      │ surfaced as anchors.     │                              │
      └──────────────────────────┴──────────────────────────────┘

  (3) After at most two retrieval rounds total, prefer surfacing
      the gap honestly to a third round. Both arms are paraphrase-
      invariant — paraphrasing the same conceptual query is wasted.

QUERY REFORMULATION RULES — when escalating, the query MUST change
in a meaningful way, not just word order:

  • Going to PRECISION → make the query MORE specific. Pull the
    exact noun the user wants pinned down ("PA criteria",
    "appeal deadline", "fee schedule"), or a doc/form name the
    prior chunks mentioned ("eAUTH form", "MH-RTC-PHP Initial
    Request"), or a code/ID. Drop generic words ("rules",
    "requirements", "information"). Goal: hit BM25 on the
    literal thing the user wants.

  • Going to RECALL → make the query MORE conceptual. Replace
    payer-specific phrasing with the topic ("Sunshine Health BH
    PA" → "psychiatric residential treatment authorization").
    Drop scope constraints. Goal: cast a wider semantic net so
    the vector arm reaches docs the prior call missed.

  • Word reordering, synonym substitution, or adding generic
    qualifiers ("rules", "policy", "guidelines") does NOT
    count as reformulation — both arms ignore those changes."""


# ── Router-owned prose (search_corpus, healthcare_npi_lookup, etc.) ──

_SEARCH_CORPUS_BLOCK = """\
search_corpus(query)
  Default corpus search — HYBRID BM25 ⊕ vector via Reciprocal Rank
    Fusion. Combines keyword precision with semantic recall in one
    ranked list. Honors the canonical (n_hierarchical) vs factual
    (n_factual) blend so paragraph-level policy answers surface above
    sentence-level fragments.
  Use for: ANY question not requiring structured data, when citations
    and confidence matter. This is the right default for everything
    except specific code/ID lookups (precision_search) or pure
    exploratory passes (explore_search).
  This includes: enrollment, PA, appeals, credentialing process,
    timely filing, covered services, claims process, policy questions.
  Try this FIRST for everything except the tools below.
  Aliases the planner / ReAct can use interchangeably:
    corpus, default_search, hybrid_search
  Returns: answer with page citations, per-arm provenance
    (retrieval_arms = ["bm25"], ["vector"], or both), and confidence.
  FAILURE MODES — read your prior chunks, classify the deficit,
  pick the matching escalation. See the methodology primer above
  for the diagnostic model and query-reformulation rules.

    F1. PRECISION DEFICIT — chunks are roughly on-topic but the
        SPECIFIC thing the user asked for (code, ID, day count,
        dollar amount, deadline, form name, verbatim policy
        text) is not in them.
        Examples:
          • "What is Sunshine BH PA window?" — chunks mention BH
            and PA but no day count.
          • "Show me the FL.UM.02 policy" — chunks reference
            FL.UM.02 in lists but don't include the policy body.
          • "What's the appeal fee?" — chunks discuss appeals
            but no dollar amount.
        Action: precision_search with a SHARPER query. Pull the
          exact noun the user wants ("PA window days", "FL.UM.02
          policy text", "appeal fee schedule"); use any document
          or form name the prior chunks mentioned as an anchor;
          drop generic words ("rules", "information"). Goal: BM25
          hit on the literal thing the user wants.

    F2. RECALL DEFICIT — chunks are all from one doc / one
        source / one payer when the user wanted a broader scan.
        Symptom: top hits dominated by one document, OR the
        user's question was exploratory ("tell me about X",
        "what do we know about Y", "summarize Z across payers")
        and we surfaced a narrow slice.
        Examples:
          • "Tell me about behavioral health policies" — 9 of 10
            hits from the Sunshine Provider Manual.
          • "What do we know about PA across payers?" — only one
            payer's docs surfaced.
          • "Summarize credentialing requirements" — chunks all
            from one section of one manual.
        Action: explore_search with a REFRAMED query. Replace
          payer-specific phrasing with the topic, drop scope
          constraints, shift to higher-level vocabulary. Goal:
          wider semantic net across the corpus.

    F3. ZERO HITS — arm_hits both 0, chunks empty.
        Action: search_corpus once with tag_mode="none". If still
          zero, surface the coverage gap honestly — we likely
          lack docs for the payer or topic.

    F4. STILL NO ANSWER AFTER ONE ESCALATION — you've already
        called search_corpus + (precision_search OR explore_search)
        and the answer still isn't in the chunks.
        Action: surface the gap honestly. Tell the user what was
          found, what specifically wasn't, and where they could
          verify externally. Do NOT keep searching — both arms
          are paraphrase-invariant and a third round won't change
          which chunks come back."""


_RECALL_SEARCH_BLOCK = """\
explore_search(query)
  Vector-only semantic search — pgvector ANN, no BM25 keyword
    constraint, no rerank confidence floor. Surfaces semantic
    neighbors regardless of literal token overlap. Returns a
    DIFFERENT set of chunks than search_corpus, not a lighter
    version of the same set.

  Position in the diagnostic model: this is the RECALL-DEFICIT
    escalation. Call it AFTER search_corpus when the prior chunks
    were too narrow — dominated by one document, one source, or
    one payer when the user wanted a broader scan. See the
    methodology primer above for the full diagnostic model and
    the query-reformulation rule (REFRAME conceptually — drop
    payer-specific phrasing, shift to higher-level topic
    vocabulary, drop scope constraints).

  Use when:
    1. RECALL DEFICIT after search_corpus — most common. The
       prior chunks were too narrow for the user's exploratory
       question. Reformulate the query broader and call this.
    2. EXPLORATORY OPENING — if the user's question is so
       open-ended that even search_corpus's BM25 arm would
       waste effort ("what do we know about X across the entire
       corpus?"), explore_search can be the FIRST call. Rare.
    3. SEARCH_CORPUS PARAPHRASE TRAP — you've already called
       search_corpus and are tempted to call it again with
       reworded query. STOP. Both arms of search_corpus are
       paraphrase-invariant. If chunks felt too narrow, switch
       to explore_search (don't re-run search_corpus). If chunks
       felt too vague on the specific answer, switch to
       precision_search.

  Do NOT use for:
    - First retrieval on a normal scoped question — start with
      search_corpus.
    - Specific code / ID / exact-phrase lookups — use
      precision_search.
    - User-uploaded documents — use search_uploaded_document.

  Aliases: lazy_corpus_search (back-compat), broad, explore
  Returns: ranked chunks with doc name + page citation, no synthesis.

  FAILURE MODES — when explore_search ran but the answer wasn't
  in the chunks:

    R1. EMPTY OR <3 HITS — semantic neighborhood is sparse.
        Diagnosis: query embeds far from any chunk. Either the
          query is too abstract OR the corpus lacks the topic.
        Action: if the question has any concrete anchor (a code,
          ID, doc name, proper noun), try precision_search on
          that anchor. Otherwise surface the gap honestly.

    R2. STILL NO PRECISE ANSWER — recall surfaced new docs you
        hadn't seen, but the specific code / day count / dollar
        amount the user wanted still isn't in any chunk.
        Action: precision_search with a sharper query targeting
          the specific noun, using doc names recall surfaced as
          anchors. This is the recall-then-precision sequence:
          recall discovered the right docs, precision pins down
          the literal answer.

    R3. LOW SIMILARITY ACROSS ALL HITS — top similarity below
        ~0.2 cosine, chunks are tangentially related at best.
        Diagnosis: corpus genuinely lacks the topic.
        Action: surface the gap honestly. Do not retry recall
          with a paraphrase (paraphrase-invariant)."""


_PRECISION_SEARCH_BLOCK = """\
precision_search(query)
  BM25-only exact-phrase search — keyword precision, no semantic
    similarity. Strong on literal token overlap; zero contribution
    from vector embeddings.

  Position in the diagnostic model: this is the PRECISION-DEFICIT
    escalation. Call it AFTER search_corpus when the prior chunks
    were on-topic but didn't pin down the specific code / ID /
    day count / dollar amount / verbatim policy text the user
    wanted. See the methodology primer above for the diagnostic
    model and the query-reformulation rule (SHARPEN — pull the
    exact noun the user wants, use doc/form names from prior
    chunks as anchors, drop generic words).

  Use when:
    1. PRECISION DEFICIT after search_corpus — most common. The
       prior chunks mentioned the topic but not the specific
       data point. Reformulate the query sharper and call this.
    2. NEEDLE-IN-HAYSTACK OPENING — the user named a specific
       code, policy ID, form number, or exact phrase that should
       appear verbatim somewhere ("FL.UM.02", "H0019", "eAUTH
       form", "CP.MP.98"). precision_search can be the FIRST
       call here — going through search_corpus first risks the
       vector arm diluting the exact-token signal.
    3. SEARCH_CORPUS PARAPHRASE TRAP (precision side) — you've
       already called search_corpus and are tempted to reword.
       STOP. If chunks were on-topic but missing the specific
       answer, switch to precision_search (don't re-run
       search_corpus).

  Do NOT use for:
    - Conceptual / paraphrased questions — vector wins those, use
      explore_search.
    - Exploratory "what do we know about X" scans — explore_search.
    - User-uploaded documents — search_uploaded_document.

  Aliases: exact, keyword_search, bm25_search, lookup
  Returns: ranked chunks with doc name + page citation, BM25 score.

  FAILURE MODES — when precision_search ran but the answer
  wasn't in the chunks:

    P1. ZERO HITS — exact phrase / code is not in the corpus.
        Diagnosis: either the code/ID doesn't exist, the corpus
          is missing that document, or the user's spelling
          differs from the corpus's spelling.
        Action: try explore_search — semantic match may surface
          adjacent terminology (e.g. user typed "BH PA" and the
          corpus uses "behavioral health authorization request").
          If recall also misses, surface the gap.

    P2. CODE FOUND IN INDEX BUT NOT IN BODY — chunks include the
        code/ID in list pages, table-of-contents, or cross-
        references, but the substantive policy section isn't
        present.
        Action: search_corpus(<code> + "criteria" or "policy"
          or "requirements"). Paragraph-level rerank often
          surfaces the substantive section that BM25-only
          missed because the body section doesn't repeat the
          code on every paragraph."""

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


def _auto_discovered_block() -> str:
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
        register, so this is belt-and-suspenders).

    Returns an empty string when no MCP tools are registered so callers
    can skip the section header.
    """
    mcp_names = registry.names_by_source("mcp")
    visible = registry.planner_visible_names()
    curated = frozenset(_REGISTRY_ORDER)
    render = tuple(sorted(n for n in mcp_names if n in visible and n not in curated))
    if not render:
        return ""
    body = registry.manifest_text(names=render)
    if not body.strip():
        return ""
    return f"{_AUTO_DISCOVERED_HEADER}\n\n{body}"


def _compose_manifest() -> str:
    """Splice router-owned prose with registry-rendered skill blocks.

    Block order (deliberate — planner reads top-down):
      1. Curated router-owned + builtin skills (stable, hand-tuned prose).
      2. Auto-discovered MCP skills (dynamic, rebuilt each call).
      3. Per-tool capabilities footer (structured JSON for parser use).
    """
    curated_blocks = [
        # Methodology primer first — the three search tools below
        # reference its concepts (BM25 / vector / RRF / what
        # actually changes retrieval). Without this primer the LLM
        # would have to infer mechanism from per-tool symptom lists.
        _RETRIEVAL_METHODOLOGY_PRIMER,
        _SEARCH_CORPUS_BLOCK,
        _RECALL_SEARCH_BLOCK,
        _PRECISION_SEARCH_BLOCK,
        # fetch_document — registered via SkillSpec; rendered by registry.
        # Distinct from search_corpus: returns a download URL, not an answer.
        registry.manifest_text(names=("fetch_document",)),
        registry.manifest_text(names=("healthcare_query",)),
        _HEALTHCARE_NPI_LOOKUP_BLOCK,
        registry.manifest_text(names=("document_upload_skill",)),
        registry.manifest_text(names=("list_thread_document_uploads",)),
        _SEARCH_UPLOADED_DOCUMENT_BLOCK,
        registry.manifest_text(names=("google_search",)),
        registry.manifest_text(names=("web_scrape",)),
        # vibe: short, work-adjacent vibe lines (toast/empathy/dry obs/etc.)
        # Registered but was missing from the planner manifest until 2026-04-25.
        registry.manifest_text(names=("vibe",)),
        # Phase 13.6 — conversation-aware planner. Continuation/
        # transformation requests ("convert this to an appeal letter",
        # "make it shorter", "rewrite for X") MUST route here, NOT to
        # search_corpus / lookup_authoritative_sources. Placed before
        # the curator/google blocks so the planner sees it as a first-
        # class option for follow-up turns.
        registry.manifest_text(names=("transform_previous_answer",)),
        # Curator tools (Phase 13.5) — registry of URLs Mobius knows
        # about, including non-ingested ones. Ordered after the search
        # tools so the planner reaches for search_corpus first; only
        # falls through to lookup_authoritative_sources when corpus
        # comes up empty.
        _LOOKUP_AUTHORITATIVE_SOURCES_BLOCK,
        _INGEST_URL_BLOCK,
        _REFUSE_BLOCK,
    ]
    auto_block = _auto_discovered_block()
    if auto_block:
        curated_blocks.append(auto_block)
    joined = "\n\n".join(b for b in curated_blocks if b.strip())
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


def get_tool_manifest() -> str:
    """Fresh manifest, composed at call time.

    Lazy composition matters because ``register_mcp_skills()`` runs at
    FastAPI startup — AFTER modules that depend on the manifest may have
    been imported. If the planner's reasoning-system prompt captured
    the manifest at import time, MCP tools registered during startup
    would be invisible to the planner until the next process restart.
    Re-rendering each call costs one registry scan (<1ms for <100 skills)
    which is negligible compared to the LLM call it feeds into.
    """
    return _compose_manifest()


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
