"""Path capabilities registry: what each agent path can answer.

Fed to the parser/planner so it decomposes questions into subquestions
that match supported capabilities. Single source of truth.

Each tool has explicit capability declarations so the parser/LLM can
match questions to the right tool. If the first tool fails, ReAct can try another.
"""
from typing import Any

# Per-tool explicit capability declarations (tool_name -> what it can/cannot do)
TOOL_CAPABILITIES: dict[str, dict[str, Any]] = {
    "ask_credentialing_npi": {
        "can_answer": [
            "Is NPI X set up for PML? (Florida Medicaid Provider Master List)",
            "Is this NPI enrolled in PML?",
            "Why is this NPI ready for PML?",
            "NPI profile from credentialing report (PML status, valid combos, readiness)",
        ],
        "requires": "report_run_id or last_report_org in context (user must have run a credentialing report first)",
        "cannot_answer": "NPPES-only lookup; questions when no report exists",
    },
    "healthcare_npi_lookup": {
        "can_answer": [
            "NPPES lookup by 10-digit NPI (provider name, taxonomy, address)",
            "Basic NPI info from national registry",
            "ICD-10 code lookup and description",
            "Medicare/Medicaid coverage (NCD/LCD)",
        ],
        "cannot_answer": "PML status, Florida Medicaid enrollment, credentialing report data",
    },
    "lookup_npi": {
        "can_answer": ["NPI numbers for an organization by name (what is the NPI of David Lawrence Center?)"],
        "cannot_answer": "Lookup by NPI number; PML status",
    },
    "run_credentialing_report": {
        "can_answer": ["Full credentialing report for an org (11-step pipeline, revenue waterfall)"],
    },
    "run_roster_reconciliation_report": {
        "can_answer": [
            "Roster reconciliation report: compare org upload vs outside-in roster",
            "in_both / external_only / internal_only mismatch analysis",
            "Potential ghost billing (external_only) and validation needed (internal_only)",
        ],
        "requires": "Roster file on this chat thread (upload via chat UI). upload_id and billing NPI org_id are filled from thread state after upload; optional NPI override via user message.",
    },
    "document_upload_skill": {
        "can_answer": [
            "How to attach files to this chat (roster CSV/Excel; future document types)",
            "Upload API contract for integrations (POST /chat/roster-upload, GET thread uploads)",
            "That multiple uploads over time are kept on the thread",
        ],
        "cannot_answer": "Actually receiving or parsing file bytes in chat text — user must use UI or HTTP multipart",
    },
    "list_thread_document_uploads": {
        "can_answer": [
            "What documents are already attached to this conversation",
            "Filenames, purposes, row counts, and upload times for prior uploads",
        ],
        "requires": "An active chat thread (thread_id); filled from context in Mobius Chat",
    },
    "search_corpus": {
        "can_answer": ["Policy lookup, appeals, PA, eligibility, claims, enrollment, credentialing process"],
    },
    "google_search": {"can_answer": ["Web search when corpus misses or user asks"]},
    "web_scrape": {"can_answer": ["Read specific web page from URL"]},
}

# Map: path (rag | patient | clinical | tool | reasoning) -> list of capability descriptions
PATH_CAPABILITIES = {
    "rag": [
        "policy lookup",
        "appeals process",
        "grievances",
        "prior auth",
        "eligibility criteria",
        "contact info",
        "utilization management",
        "claims",
        "benefits",
        "member handbook",
        "Google search fallback when corpus confidence is low",
    ],
    "patient": [],  # stub: "I can't access your records"
    "clinical": [],  # stub: future
    "tool": [
        "Google search",
        "web scrape",
        "NPI lookup by org name (what is the NPI of X)",
        "ask_credentialing_npi: NPI + PML status from credentialing report (requires report in context)",
        "healthcare_npi_lookup: NPPES lookup by NPI number (no PML)",
        "ICD-10 code lookup",
        "Medicare/Medicaid coverage (NCD/LCD)",
        "Provider Roster / Credentialing report",
        "Roster reconciliation report (upload vs outside-in)",
        "Document upload skill (attach files to thread; API + UI)",
        "List thread document uploads (what files are already attached)",
    ],
    "reasoning": [
        "conceptual explanation",
        "rationale",
        "general how-to without corpus",
        "difference between concepts",
        "what does X mean",
    ],
}


def capabilities_for_parser() -> str:
    """Format capabilities for inclusion in parser prompt. Returns human-readable string."""
    parts = []
    for path, caps in PATH_CAPABILITIES.items():
        if caps:
            parts.append(f"{path}: {', '.join(caps)}")
        else:
            parts.append(f"{path}: (stub - not yet implemented)")
    return "; ".join(parts)


def tool_capabilities_for_parser() -> str:
    """Format per-tool capabilities for parser/planner prompt (explicit what each tool can/cannot do)."""
    lines = []
    for tool, caps in TOOL_CAPABILITIES.items():
        can_ = caps.get("can_answer", [])
        cannot_ = caps.get("cannot_answer", "")
        requires_ = caps.get("requires", "")
        parts = [f"  {tool}:"]
        parts.append(f"    Can: {'; '.join(can_) if isinstance(can_, list) else can_}")
        if cannot_:
            parts.append(f"    Cannot: {cannot_}")
        if requires_:
            parts.append(f"    Requires: {requires_}")
        lines.append("\n".join(parts))
    return "\n\n".join(lines)


def available_capabilities_json() -> dict[str, Any]:
    """Build structured available_capabilities for Mobius Planner input (JSON)."""
    return {
        "rag_scopes": ["payer_manuals", "state_contracts", "internal_docs"],
        "tools": [
            "google_search",
            "web_scrape",
            "org_npi_lookup",
            "search_org_names",
            "ask_credentialing_npi",
            "healthcare_npi_lookup",
            "healthcare_query",
            "npi_lookup",
            "roster_report",
            "roster_reconciliation",
            "document_upload_skill",
            "list_thread_document_uploads",
        ],
        "web_allowed": True,
        "reasoning_allowed": True,
        "tool_capabilities": TOOL_CAPABILITIES,
        "routing_rule": (
            "Match question to tool capabilities. "
            "NPI + PML/enrollment → ask_credentialing_npi (requires report). "
            "NPI number only (no PML) → healthcare_npi_lookup. "
            "NPI for org name → search_org_names."
        ),
    }


def defaults_policy_json() -> dict[str, Any]:
    """Build defaults_policy for Mobius Planner input (JSON)."""
    return {
        "timeframe_default_allowed": True,
        "timeframe_default": "last_90_days",
        "jurisdiction_fields_supported": [
            "state", "payer", "program", "timeframe", "plan",
            "population", "setting", "provider_type",
        ],
    }


def slim_master_plan(plan: dict[str, Any] | None) -> dict[str, Any] | None:
    """
    Reduce last_master_plan to a planner-safe context object.
    Strips routing fields (capabilities_needed, kind, intent_score, fallbacks)
    to prevent the model from inheriting stale routing decisions.
    Keeps only: what the user originally wanted, which tools ran, and jurisdiction used.
    """
    if not plan or not isinstance(plan, dict):
        return None
    tasks = plan.get("tasks") or plan.get("subquestions") or []
    tools_used = []
    jurisdiction: dict[str, Any] = {}
    for t in tasks:
        if not isinstance(t, dict):
            continue
        hint = t.get("tool_hint") or t.get("capabilities_primary")
        if hint and str(hint).lower() not in ("null", "none", ""):
            tools_used.append(str(hint))
        jd = t.get("jurisdiction") or {}
        if isinstance(jd, dict) and not jurisdiction:
            jurisdiction = {k: v for k, v in jd.items() if v and str(v).lower() not in ("null", "none", "")}
    return {
        "original_intent": (plan.get("plan_summary") or plan.get("message_summary") or "").strip(),
        "tools_used": tools_used,
        "jurisdiction": jurisdiction,
    }


def planner_input_json(
    user_message: str, context: str = "", last_master_plan: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Build full planner input payload (user_message, context, available_capabilities, defaults_policy, last_master_plan)."""
    payload: dict[str, Any] = {
        "user_message": user_message,
        "context": context or "",
        "available_capabilities": available_capabilities_json(),
        "defaults_policy": defaults_policy_json(),
    }
    if last_master_plan and isinstance(last_master_plan, dict):
        payload["last_master_plan"] = slim_master_plan(last_master_plan)
    return payload


# Answers for capability questions ("can you search Google?", "what can you do?")
CAPABILITY_ANSWERS: dict[str, str] = {
    "upload file": "You can attach files to this chat with **⋯** (next to Send) → **Upload file**. For rosters, choose **Roster for reconciliation** and pick CSV or Excel; you can upload different files at different times and they stay on this thread. I can also list what’s already attached if you ask.",
    "attach a file": "Use **⋯** → **Upload file** next to the message box. Roster uploads support CSV and Excel for reconciliation; each upload is saved on this chat thread with a timestamp.",
    "upload roster": "Tap **⋯** → **Upload file** → **Roster for reconciliation**, enter the organization name, then select your CSV or Excel file. After it finishes, ask to run the reconciliation report for that org.",
    "google": "Yes, I can search the web when our policy materials don't have the answer. I'll use external search to complement our corpus and cite those sources.",
    "search google": "Yes, I can search the web. When our materials don't cover your question, I can look up information from the internet and cite those sources.",
    "web scrape": "Yes, I can scrape web pages to extract content when you provide a URL. This helps when you need information from a specific page.",
    "scrape": "Yes, I can scrape web pages when you give me a URL. I'll extract the content and summarize it for you.",
    "what can you do": "I can help with: (1) Policy lookups from payer manuals and contracts—appeals, grievances, prior auth, eligibility, claims, benefits. (2) Web search when our materials don't cover your question. (3) Web scraping when you provide a URL. (4) General explanations and reasoning. I don't have access to your personal health records.",
}


def get_capability_answer(question: str) -> str | None:
    """If question asks about our capabilities, return a canned answer; else None."""
    q = (question or "").strip().lower()
    for key, answer in CAPABILITY_ANSWERS.items():
        if key in q:
            return answer
    return None
