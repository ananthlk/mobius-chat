"""
TOOL_MANIFEST — the model reads this and picks tools.
Each tool declares what it CAN and CANNOT do. Match the question to the best tool.
If a tool fails (e.g. "no report in context"), try the next-best tool.
"""
from app.stages.agents.capabilities import tool_capabilities_for_parser

TOOL_MANIFEST = f"""
AVAILABLE TOOLS — match the question to the tool whose capabilities fit.
If the first tool fails (e.g. returns "no report"), try the next-best tool.

**Credentialing / NPPES tools** (locations, providers per site, org NPI lookup, reconciliation, credentialing report, report Q&A, registry lookups): outputs follow **Mobius tool output v1** — a **Summary** block (internal / ReAct & consistency) plus **Detail** (user display & user download). The server may also pass the short summary in structured fields; the full markdown is still the user artifact. When summarizing, do not assume an empty truncated view means the tool failed.

**Operational roster (find_associated)** — Same data sources for **autopilot** and **copilot**: NPPES, Florida PML, DOGE servicing, optional roster upload / thread roster. **autopilot** (chat **agentic**): API sets an **active panel** when score ≥ cutoff after registry penalties. **copilot** (chat non-agentic path / credentialing co-pilot step): returns **scores and rationales** with `pending_review`; user confirms active panel via validate step. Plain-language basis labels (e.g. practice address match strong vs historical Medicaid servicing) appear in the tool detail; match technical codes only as secondary.

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

ask_credentialing_npi(question)
  NPI profile and PML status from a credentialing report.
  Can answer: "Is NPI X set up for PML?", "Why is this NPI ready for PML?",
    "Is this NPI enrolled in PML?" — from the report's validation data.
  REQUIRES: User must have run a credentialing report first (report_run_id in context).
  If no report exists: returns failure — try healthcare_npi_lookup next for NPPES info.
  Cannot: NPPES-only lookup; does not have PML data without a report.

healthcare_query(question)
  Healthcare data lookup: ICD-10-CM codes (meaning of F32.1, Z00.00, etc.),
    Medicare/Medicaid coverage summaries (NCD/LCD), CPT/HCPCS wording, diagnosis/procedure codes.
  Also: NPI registry facts when the question is a 10-digit NPI number (same backend as registry lookup).
  Use when: User asks what a code means, ICD-10, HCPCS, coverage, or NPI-by-number without PML context.
  Do NOT use for: PML enrollment status (use ask_credentialing_npi when a report exists),
    or finding an org's NPI by name (use lookup_npi).
  Cannot: PML status without credentialing report; org NPI by name.

healthcare_npi_lookup(question)
  NPPES registry lookup ONLY when the user gives or asks about a 10-digit NPI number
    (name, taxonomy, address from the national registry).
  Do NOT use for: ICD-10, diagnosis codes, CPT, HCPCS, "what is code …", Medicare coverage, NCD/LCD —
    those are healthcare_query.
  Cannot: PML status, Florida Medicaid enrollment, credentialing data.
  Use when: ask_credentialing_npi failed or not applicable AND the question is specifically NPI-registry lookup by number.

lookup_npi(org_name)
  Look up NPI numbers for an organization BY NAME.
  Use for: "What is the NPI for David Lawrence Center?", "NPIs for Aspire Health".
  Cannot: Lookup by NPI number; PML status.
  Returns: NPIs with addresses and confidence tiers. When several billing orgs match, the **UI shows
    server-driven choice chips** (single- or multi-select + Continue); the detailed list is only there — do not rely on prose alone.

find_org_locations(org_name optional, org_npi optional, org_npis optional array, state optional, include_web_enrichment optional)
  Discover **practice / service locations** for one or more **billing organization (Type-2) NPIs** (credentialing Step 2).
  Same capability as MCP tool **find_org_locations**: NPPES + Florida PML + DOGE; composer **agentic** mode may add web enrichment.
  Use for: "Find practice locations for [org]", "Sites for NPI 1234567893", "Addresses tied to these NPIs" (put NPIs in **org_npis** or **org_npi**, or in the user message as 10-digit numbers; after **lookup_npi**, prior candidate text is used to resolve bare NPIs).
  Inputs: **org_npis** (list of strings) and/or **org_npi** (single string) and/or **org_name** (resolves when uniquely matchable).
  Cannot: Replace a full credentialing report; does not run Steps 3–11 alone.
  Requires: Credentialing API configured on the server.

find_associated_providers_at_locations(org_name optional, org_npi optional, org_npis optional, upload_id optional, include_roster_members optional, external_only optional, state optional, include_web_enrichment optional)
  **Operational roster per practice site** (credentialing Step 4 / find_associated_providers). Same MCP skill name.
  Answers "who is tied to this location" using **historic Medicaid servicing (DOGE)**, **NPPES + PML practice-address alignment**, and **optionally** members merged from a roster **upload_id** (or thread reconciliation upload when server fills context).
  Use for: "Who practices at these sites?", "Providers at each location for [org]", "NPIs billing under this practice address".
  **Not** a clinical staffing schedule — billing/enrollment-oriented linkage with confidence scores.
  Cannot: Replace full credentialing report; does not run PML validation / waterfall alone.
  Requires: Credentialing API (POST /find-locations then /find-associated-providers).

run_credentialing_report(org_name, mode optional)
  **Medicaid NPI credentialing report** — the **11-step pipeline** with **Sections A–E revenue waterfall**, readiness, and PML-facing outputs.
  **Data tables** in outputs are **built by code** (step CSVs and deterministic transforms); the LLM only narrates. After the draft, the pipeline runs **deterministic validations** and can **re-compose** when checks fail (same family of guardrails as production credentialing).
  mode: "autopilot" (default) = full report in one run.
  mode: "copilot" = one step at a time with validate_credentialing_step.
  Use when the user wants **credentialing / waterfall / readiness / Section A–E** language — **not** "upload vs outside-in reconciliation".
  **Do not** use for "roster reconciliation", "reconcile upload vs external", or **in_both / external_only / internal_only** buckets — use **run_roster_reconciliation_report** instead when a roster file is (or will be) on the thread.

validate_credentialing_step(step_id optional, validated_output, run_id optional)
  Advance the credentialing co-pilot after the user confirms or edits the pending step.
  run_id defaults to the active thread's credentialing run; step_id defaults to pending step.
  validated_output: JSON object — e.g. {{"org_npis":["1234567890"]}} after identify_org, {{"locations":[...]}} after find_locations.
  Optional workflow_follow_ups: list of strings (one operational follow-up per line) stored on the validated step for tracking.
  For "accept draft as-is", pass the same fields as shown in the draft (or {{}} for benchmark-only steps).
  Use after run_credentialing_report(mode="copilot") when the user says proceed, continue, confirm, or supplies corrections.

run_roster_reconciliation_report(org_name, upload_id, org_id)
  **Phase 1 — Roster alignment with NPPES** (product: roster reconciliation): compares the **operational roster upload** to **NPPES / outside-in** association for the org — **not** the credentialing A–E waterfall and **not** a **PML enrollment** validation report. **Phase 2** (separate / later): **PML validation** after NPPES alignment is triaged.
  Narrative distinguishes: **(1) Aligned** — on roster, in NPPES, org/linkage matches; **(2) Misaligned** — on roster, in NPPES, linkage/credentials don’t match org expectation; **(3) On roster, not in NPPES** — credentialing/registry critical; **(4) Strong NPPES/org tie, not on roster** — billing-integrity / compliance priority. **All CSVs** are **code-generated**; LLM is narrative-only; deterministic post-compose checks can **re-compose** on failure.
  Buckets: **in_both**, **external_only**, **internal_only** (split **not-in-NPPES** vs **in NPPES, weak outside-in** in prose).
  Use when: user says **"roster reconciliation"**, **"reconciliation report"** (with roster context), **reconcile my roster/upload**, or compare **upload vs external** roster.
  Pass org_name from the user message. **org_id** (billing NPI) is required; **upload_id** is optional — the server uses the **latest resolved roster in the provider DB** for that org (source of truth), then thread upload metadata if needed. Uploading via chat only **updates** that master record; the report reads from it. Do not ask the user for raw upload_id.
  Billing NPI (org_id) is usually from NPPES/PML search or upload flow; user can override with "Use billing NPI …".
  One billing NPI per run; multi-entity orgs can run again with another NPI.
  Returns: reconciliation narrative **plus** step CSVs (e.g. reconciliation_review, upload validation) as **server attachments** for download — not new tables invented by the model.

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
ENTITY_TOOLS = frozenset({
    "lookup_npi",
    "find_org_locations",
    "find_associated_providers_at_locations",
    "run_credentialing_report",
    "validate_credentialing_step",
    "run_roster_reconciliation_report",
    "web_scrape",
    "ask_credentialing_npi",
    "healthcare_query",
    "healthcare_npi_lookup",
    "document_upload_skill",
    "list_thread_document_uploads",
})

# Which tools can answer follow-up questions from their output
FOLLOW_UP_CAPABLE = frozenset({
    "run_credentialing_report",
    "validate_credentialing_step",
    "run_roster_reconciliation_report",
    "lookup_npi",
    "find_org_locations",
    "find_associated_providers_at_locations",
    "ask_credentialing_npi",
    "list_thread_document_uploads",
})
