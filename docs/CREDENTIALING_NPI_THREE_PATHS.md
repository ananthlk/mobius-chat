# NPI / Credentialing: Three Paths

The parser and blueprint route NPI/credentialing traffic into three behaviors:

## (a) Answer from previous response

When **active_skill** (or **report_run_id** / **last_report_org**) is in context from the last turn:

- **Blueprint** can force `agent=reasoning` when the message refers to the same org or is a generic follow-up (e.g. “how many NPIs have PML issues?”).
- **Clarify** skips jurisdiction (no “which health plan?”).
- **Resolve** either uses **reasoning** with `active_skill_context` (report summary in context pack) or the **credentialing_qa** path with `report_run_id` → `POST /report-runs/{id}/ask`.

So the system answers from the existing report and does **not** re-run the 11-step pipeline.

## (b) NPI Q&A tool (credentialing_qa)

**Generic** credentialing/NPI questions that are **not** “look up NPI” or “build report”:

- Examples: “explain section E”, “what is section E”, “i meant section E of the credentialing report”, “can you explain section E for me”.
- **Blueprint** sets `tool_hint=credentialing_qa` when the message matches follow-up phrases (section E, explain, “i meant”, etc.).
- **tool_agent** with `credentialing_qa`:
  - If **report in context** → answer via `_ask_credentialing_report` (same as (a)).
  - If **no report** → return **CREDENTIALING_QA_NO_REPORT** (generic explanation of Section E + “Create a credentialing report for [org]” to generate one).

So generic questions never trigger the full report build; they use the NPI Q&A path (answer from report or generic text).

## (c) Specific tools for specific requests

- **Build report:** “Create a credentialing report for [Org]” / “Medicaid NPI report for [Org]” with a **plausible org name** → `tool_hint=roster_report` → 11-step orchestrator.  
  If the “org” is not plausible (e.g. “i meant section E of the credentialing report”), `_is_plausible_org_name` rejects it and we treat as (b) instead.
- **Look up NPI:** “find NPI for X”, “NPIs for X” → npi_lookup / org search.
- **Other:** web search, scrape, healthcare lookup, etc., as defined by route triggers and planner.

So only explicit “create/build report for [real org]” runs the full report; everything else uses (a) or (b).

---

**Tests:** `tests/test_credentialing_three_paths.py` and `tests/test_blueprint_active_skill.py`.
