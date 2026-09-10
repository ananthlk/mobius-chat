# The production tool catalogue — observed, not rendered

**How this was established, because that is the point of it.** Not by reading
`tool_manifest.py` and not by rendering it locally. These are the names the
DEPLOYED service actually emitted, read back from `turn_spans` where
`kind='tool.offered'`, on revision `mobius-chat-00977-8bl` after 2026-09-10
14:10:30Z. Every row is a thing production did, not a thing the source says it
would do.

**58 entries: 28 static · 29 MCP-registered at runtime · 1 sentinel.**

**A local render sees 28 of 58.** `get_manifest_tool_names(None)` on a dev
machine returns the static block only, because MCP tools register at FastAPI
startup against services a laptop has no connection to. So **any reading of the
catalogue done from source or from a local render is missing half of it** — and
missing it silently, because the local render succeeds and returns a plausible
list.

That matters for the schematic: a document describing 28 tools would be
describing the half that is easiest to see.

## Static — declared in `tool_manifest.py` (28)

```
appeals_assemble_letter    appeals_find_carc         appeals_get_playbook
appeals_lookup_rules       appeals_validate_claim    document_upload_skill
fetch_document             healthcare_npi_lookup     healthcare_query
ingest_url                 list_thread_document_uploads
payor_readiness            product_feedback          product_help_search
rag                        recall_evidence           refuse
search_uploaded_document   service_line_code_lookup  service_line_coverage
service_line_detail        service_line_gaps         service_line_limits
service_line_requirements  service_line_search       transform_previous_answer
vibe                       web_scrape
```

## MCP — registered at runtime, invisible to source reading (29)

```
check_provider_credentialing
get_benchmark_dimensions   get_churn_benchmark        get_entrant_analysis
get_fact_pack              get_market_decomposition   get_market_retention
get_market_share_timeseries get_market_size           get_market_timeseries
get_msa_map                get_org_benchmark          get_org_leakage
get_org_profile            get_org_rate_gap           get_org_service_line_profile
get_org_type_stats         get_org_universe           get_published_rates
get_rate_benchmarks        get_rate_trends            get_service_line_code_map
get_service_line_opportunity                          get_service_mix
get_top_orgs               get_valid_filters          lookup_npi
search_clinician_by_name   search_orgs
```

**26 of those 29 are market/benchmark analytics** — a coherent block from one
service, offered on every turn including turns about appeal deadlines.

## `__unfiltered__` (1)

Not a tool. The sentinel meaning *no subscription or policy filter was applied*,
kept deliberately alongside the names: "no filter ran" and "the filter permitted
everything" are different facts about tool policy and only this separates them.

## Two caveats a reader should not have to take on trust

1. **This is what was offered, not what exists.** A tool registered but never
   offered in the observed window would be absent here. The window is one
   revision and a modest number of turns.
2. **Source classification is heuristic.** A name is "static" if it appears as a
   string literal in `tool_manifest.py`; otherwise "MCP". That is right for all
   58 as far as I can check, but it is inference from the source, not from the
   registry — the only part of this document not established by observation.

## What this changes about the P6 baseline

The manifest token measurement — **8,137 tokens, 35.8% of a react prompt** — was
taken against a **local render of 28 tools**. Production sends 58. **That figure
is a floor and should not be restated as the number** until it is re-measured
against a production render. I would expect materially higher.
