"""
TOOL_MANIFEST — the model reads this and picks tools.
Each tool declares what it CAN and CANNOT do. Match the question to the best tool.
If a tool fails (e.g. "no report in context"), try the next-best tool.
"""
from app.stages.agents.capabilities import tool_capabilities_for_parser

TOOL_MANIFEST = f"""
AVAILABLE TOOLS — match the question to the tool whose capabilities fit.
If the first tool fails (e.g. returns no results), try the next-best tool.

WORKFLOW SELECTION (chat UI) — The server may attach **clarification_options** on the assistant
response: clickable choices (single- or multi-select). Users may also type a normal message instead
(or in addition, depending on UI); the next turn is still plain user text for you to interpret.
When choice chips appear, keep your summary short and point the user to the buttons; do not invent
a separate prose-only list as the only way to proceed.

════════════════════════════════════════════
search_corpus(query)
  Search Mobius knowledge base (payer manuals, policy docs).
  Use for: ANY question not requiring structured data.
  This includes: enrollment, PA, appeals, credentialing process,
    timely filing, covered services, claims process.
  Try this FIRST for everything except the tools below.
  Returns: answer with page citations and confidence score.

healthcare_query(question)
  Healthcare data lookup: ICD-10-CM codes (meaning of F32.1, Z00.00, etc.),
    Medicare/Medicaid coverage summaries (NCD/LCD), CPT/HCPCS wording, diagnosis/procedure codes.
  Also: NPI registry facts when the question is a 10-digit NPI number (same backend as registry lookup).
  Use when: User asks what a code means, ICD-10, HCPCS, coverage, or NPI-by-number without PML context.
  Do NOT use for: PML enrollment status (skill is being rebuilt — not available in chat currently).
  Cannot: PML status without credentialing report; org NPI by name.

healthcare_npi_lookup(question)
  NPPES registry lookup ONLY when the user gives or asks about a 10-digit NPI number
    (name, taxonomy, address from the national registry).
  Do NOT use for: ICD-10, diagnosis codes, CPT, HCPCS, "what is code …", Medicare coverage, NCD/LCD —
    those are healthcare_query.
  Use when: question is specifically NPI-registry lookup by number.

document_upload_skill()
  First-class **document upload skill**: how to attach files to this chat thread for downstream tools.
  Use when: user asks how to upload, attach a roster, send a file, supported formats, API/MCP integration,
    or what the upload flow does. Multiple documents may be uploaded over time on the same thread.
  Does NOT transfer bytes — returns instructions (UI: ⋯ → Upload file; HTTP: POST /chat/roster-upload).
  Returns: Markdown with purposes, endpoints, and relation to roster reconciliation.

list_thread_document_uploads(thread_id optional)
  List documents already attached to the chat thread (purpose, filename, org, rows, time).
  Use when: user asks what they uploaded, what's on file, or to confirm prior uploads.
  thread_id defaults to the current conversation when omitted (server fills from context).
  Returns: Markdown table of uploads + reconciliation defaults if set.

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
  Returns: matched chunks with page citations from the uploaded document.

google_search(query)
  Search the web for current information.
  Use for: corpus misses, or user explicitly asks to search web.
  Do NOT use as primary route — corpus goes first.
  Returns: URLs and snippets, then auto-scrapes top result.

web_scrape(url, scrape_mode optional)
  Read the web: **quick** (default), **medium**, or **detailed** crawl from the seed URL.
  scrape_mode:
    **quick** — single page, fastest (default when unsure).
    **medium** — same-site tree crawl: depth up to **3**, up to **6** HTML pages (no doc download quota).
    **detailed** — deeper crawl: depth up to **5**, up to **50** pages, up to **10** linked document downloads (e.g. PDFs) when the scraper supports it.
  Use **quick** for one policy page or a direct link; **medium** for a small site section; **detailed** when the user needs broad coverage or many linked files and latency is acceptable.
  Omit scrape_mode or use **quick** unless the user (or agentic mode) clearly needs more coverage.
  Returns: extracted text (combined across pages when crawling).

refuse(reason)
  Hard stop — no content returned.
  Use for: any question about a specific patient (PHI),
    any clinical treatment recommendation.
  "Is member 12345 eligible?" → refuse (PHI)
  "What are eligibility rules?" → search_corpus (not PHI)

PER-TOOL CAPABILITIES (explicit):
{tool_capabilities_for_parser()}
════════════════════════════════════════════
"""

# Which tools are entity tools (never receive jurisdiction context)
# 2026-04-18 disconnect: credentialing/roster tools removed from the set
# (their dispatch branches in react_loop.py are gone too).
ENTITY_TOOLS = frozenset({
    "web_scrape",
    "healthcare_query",
    "healthcare_npi_lookup",
    "document_upload_skill",
    "list_thread_document_uploads",
})

# Which tools can answer follow-up questions from their output
FOLLOW_UP_CAPABLE = frozenset({
    "list_thread_document_uploads",
})
