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


# ── Router-owned prose (search_corpus, healthcare_npi_lookup, etc.) ──

_SEARCH_CORPUS_BLOCK = """\
search_corpus(query)
  Search Mobius knowledge base (payer manuals, policy docs).
  Use for: ANY question not requiring structured data.
  This includes: enrollment, PA, appeals, credentialing process,
    timely filing, covered services, claims process.
  Try this FIRST for everything except the tools below.
  Returns: answer with page citations and confidence score."""

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


def _compose_manifest() -> str:
    """Splice router-owned prose with registry-rendered skill blocks."""
    blocks = [
        _SEARCH_CORPUS_BLOCK,
        registry.manifest_text(names=("healthcare_query",)),
        _HEALTHCARE_NPI_LOOKUP_BLOCK,
        registry.manifest_text(names=("document_upload_skill",)),
        registry.manifest_text(names=("list_thread_document_uploads",)),
        _SEARCH_UPLOADED_DOCUMENT_BLOCK,
        registry.manifest_text(names=("google_search",)),
        registry.manifest_text(names=("web_scrape",)),
        _REFUSE_BLOCK,
    ]
    joined = "\n\n".join(b for b in blocks if b.strip())
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


TOOL_MANIFEST = _compose_manifest()


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
