"""
TOOL_MANIFEST — the model reads this and picks tools.
Replaces all keyword-based routing rules in prompts_llm.yaml.
"""

TOOL_MANIFEST = """
AVAILABLE TOOLS — read before deciding what to do.

════════════════════════════════════════════
search_corpus(query)
  Search Mobius knowledge base (payer manuals, policy docs).
  Use for: ANY question not requiring structured data.
  This includes: enrollment, PA, appeals, credentialing,
    timely filing, covered services, claims process.
  Try this FIRST for everything except the tools below.
  Returns: answer with page citations and confidence score.

google_search(query)
  Search the web for current information.
  Use for: corpus misses, or user explicitly asks to search web.
  Do NOT use as primary route — corpus goes first.
  Returns: URLs and snippets, then auto-scrapes top result.

web_scrape(url)
  Read a specific web page.
  Use for: URL present in message, or top search result.
  Returns: full page content.

lookup_npi(org_name)
  Look up NPI numbers for an organization.
  Use ONLY when user explicitly asks for NPI or provider number.
  "What is the NPI for X" — use this.
  "How does X handle enrollment" — do NOT use this, use corpus.
  Returns: NPIs with addresses and confidence tiers.

run_credentialing_report(org_name)
  Generate full credentialing and PML enrollment report.
  Use ONLY when user explicitly requests a report or roster.
  Returns: 11-step report with revenue waterfall A-E.

refuse(reason)
  Hard stop — no content returned.
  Use for: any question about a specific patient (PHI),
    any clinical treatment recommendation.
  "Is member 12345 eligible?" → refuse (PHI)
  "What are eligibility rules?" → search_corpus (not PHI)
════════════════════════════════════════════
"""

# Which tools are entity tools (never receive jurisdiction context)
ENTITY_TOOLS = frozenset({
    "lookup_npi",
    "run_credentialing_report",
    "web_scrape",
})

# Which tools can answer follow-up questions from their output
FOLLOW_UP_CAPABLE = frozenset({
    "run_credentialing_report",
    "lookup_npi",
})
