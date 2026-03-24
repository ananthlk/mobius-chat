"""
TOOL_MANIFEST — the model reads this and picks tools.
Each tool declares what it CAN and CANNOT do. Match the question to the best tool.
If a tool fails (e.g. "no report in context"), try the next-best tool.
"""
from app.stages.agents.capabilities import tool_capabilities_for_parser

TOOL_MANIFEST = f"""
AVAILABLE TOOLS — match the question to the tool whose capabilities fit.
If the first tool fails (e.g. returns "no report"), try the next-best tool.

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

healthcare_npi_lookup(question)
  NPPES lookup by 10-digit NPI number (national registry).
  Can answer: Basic provider info (name, taxonomy, address) from NPI number.
  Also: ICD-10 codes, Medicare/Medicaid coverage (NCD/LCD).
  Cannot: PML status, Florida Medicaid enrollment, credentialing data.
  Use when: NPI number in question, and ask_credentialing_npi failed or not applicable.

lookup_npi(org_name)
  Look up NPI numbers for an organization BY NAME.
  Use for: "What is the NPI for David Lawrence Center?", "NPIs for Aspire Health".
  Cannot: Lookup by NPI number; PML status.
  Returns: NPIs with addresses and confidence tiers.

run_credentialing_report(org_name)
  Generate full credentialing and PML enrollment report.
  Use ONLY when user explicitly requests a report or roster.
  Returns: 11-step report with revenue waterfall A-E.

run_roster_reconciliation_report(org_name, upload_id, org_id)
  Roster reconciliation: compare org upload vs outside-in roster.
  Use when: user uploaded a roster and wants to reconcile it (in_both, external_only, internal_only).
  Pass org_name from the user message. upload_id and org_id are OPTIONAL if the user already uploaded
  a roster in this chat thread (server fills from thread state — newest upload wins). Do not ask for raw upload_id.
  Billing NPI (org_id) is auto-selected at upload from NPPES/PML search; user can override with "Use billing NPI …".
  One billing NPI per run; multi-entity orgs can run again with another NPI.
  Returns: reconciliation report with mismatch actions.

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

web_scrape(url)
  Read a specific web page.
  Use for: URL present in message, or top search result.
  Returns: full page content.

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
    "run_credentialing_report",
    "run_roster_reconciliation_report",
    "web_scrape",
    "ask_credentialing_npi",
    "healthcare_npi_lookup",
    "document_upload_skill",
    "list_thread_document_uploads",
})

# Which tools can answer follow-up questions from their output
FOLLOW_UP_CAPABLE = frozenset({
    "run_credentialing_report",
    "run_roster_reconciliation_report",
    "lookup_npi",
    "ask_credentialing_npi",
    "list_thread_document_uploads",
})
