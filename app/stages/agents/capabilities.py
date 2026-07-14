"""Path capabilities registry: what each agent path can answer.

Fed to the parser/planner so it decomposes questions into subquestions
that match supported capabilities. Single source of truth.

Each tool has explicit capability declarations so the parser/LLM can
match questions to the right tool. If the first tool fails, ReAct can try another.
"""
from typing import Any

# Per-tool explicit capability declarations (tool_name -> what it can/cannot do)
TOOL_CAPABILITIES: dict[str, dict[str, Any]] = {
    # 2026-04-18 disconnect — ask_credentialing_npi removed along with
    # the other credentialing/roster tools. Capability declaration
    # rebuilds when credentialing ships as a proper skill integration.
    "healthcare_query": {
        "can_answer": [
            "ICD-10-CM code meaning and description (e.g. what is F32.1)",
            "Medicare/Medicaid coverage context (NCD/LCD) from healthcare APIs",
            "10-digit NPI registry facts (name, taxonomy, address) when question is NPI-by-number",
            "Diagnosis/procedure code questions, HCPCS/CPT wording when structured lookup applies",
        ],
        "cannot_answer": "PML status without credentialing report; NPI for an organization by name",
    },
    "healthcare_npi_lookup": {
        "can_answer": [
            "NPPES lookup by 10-digit NPI only (provider name, taxonomy, address from national registry)",
        ],
        "cannot_answer": (
            "ICD-10, diagnosis codes, CPT, HCPCS, coverage/NCD/LCD questions (use healthcare_query); "
            "PML status, Florida Medicaid enrollment, credentialing report data"
        ),
    },
    # 2026-04-18 disconnect — removed:
    #   lookup_npi / find_org_locations /
    #   find_associated_providers_at_locations
    # These reached the provider-roster-credentialing skill server via
    # chat proxies. Capability declarations rebuild with the clean
    # credentialing skill integration.
    "org_npi_lookup": {
        "can_answer": [
            "Organization NPI lookup by name via MCP org_npi_lookup (credentialing API + optional web variant discovery)",
        ],
        "requires": "MCP server; chat passes search_mode from composer (copilot=registry-only path, agentic=full web enrichment)",
        "cannot_answer": "PML status / FL Medicaid enrollment (use check_provider_credentialing with org_slug + npi)",
    },
    "search_org_names": {
        "can_answer": [
            "Org / billing NPI disambiguation by name (NPPES + PML); MCP search_org_names with search_mode copilot vs agentic",
        ],
        "cannot_answer": "10-digit NPI registry row only (use healthcare_query); PML enrollment from report (use ask_credentialing_npi)",
    },
    # 2026-04-18 disconnect — removed:
    #   run_credentialing_report / validate_credentialing_step /
    #   run_roster_reconciliation_report
    # All three reached the credentialing skill server via chat proxies
    # and are being rebuilt as a clean skill integration.
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
    "search_uploaded_document": {
        "can_answer": [
            "What does MY uploaded document say about <topic>",
            "Summarize the PDF I just uploaded",
            "Find <specific thing> in the manual I attached",
        ],
        "requires": "At least one instant_rag upload on this thread; document_id is auto-resolved when only one upload exists",
        "cannot_answer": "Anything not contained in the uploaded document — use search_corpus or google_search for those",
    },
    "search_corpus": {
        "can_answer": ["Policy lookup, appeals, PA, eligibility, claims, enrollment, credentialing process"],
    },
    "transform_previous_answer": {
        "can_answer": [
            "Reshape the previous assistant answer into a new artifact (appeal letter, email, shorter version, plain-English rewrite, counter-argument, bulleted summary)",
            "Continuation requests using pronouns ('this', 'that', 'the above')",
            "Transformation verbs on prior content (convert/rewrite/shorten/lengthen/format-as)",
        ],
        "requires": "A prior assistant turn in this thread (read from ctx.last_turns); first-turn invocations return a clarifying message",
        "cannot_answer": "Fresh substantive questions — even if topically related, those need search_corpus / curator / google_search retrieval",
    },
    "google_search": {"can_answer": ["Web search when corpus misses or user asks"]},
    "web_scrape": {
        "can_answer": [
            "Read a web URL (quick single page, or medium/detailed same-site crawl when scrape_mode is set)",
        ]
    },
    "list_tasks": {
        "can_answer": [
            "Show all open tasks for an org",
            "What tasks are pending for this credentialing run?",
            "List tasks assigned to a specific user",
            "What roster reconciliation tasks are open?",
        ],
        "requires": "CHAT_SKILLS_TASK_MANAGER_URL; org_name recommended",
        "cannot_answer": "Task content that hasn't been created yet",
    },
    "create_task": {
        "can_answer": [
            "Create a follow-up task for a provider issue",
            "Add a manual task to the task list",
            "Log an action item from this conversation",
        ],
        "requires": "CHAT_SKILLS_TASK_MANAGER_URL; org_name and text required",
        "cannot_answer": "Tasks that require pipeline data to populate automatically",
    },
    "resolve_task": {
        "can_answer": [
            "Mark a task as resolved",
            "Close out a follow-up item",
        ],
        "requires": "CHAT_SKILLS_TASK_MANAGER_URL; task_id required",
        "cannot_answer": "Batch resolution of multiple tasks in one call",
    },
    "patch_task": {
        "can_answer": [
            "Edit a task's title, severity, deadline, or status",
            "Add a note/comment to a task",
        ],
        "requires": "CHAT_SKILLS_TASK_MANAGER_URL; task_id required",
        "cannot_answer": "Batch edits across multiple tasks in one call",
    },
    "assign_task": {
        "can_answer": [
            "Assign or reassign a task to a person or team",
            "Hand off a follow-up item",
        ],
        "requires": "CHAT_SKILLS_TASK_MANAGER_URL; task_id and assignee required",
        "cannot_answer": "Assignee directory lookup — caller must name the assignee",
    },
    "dismiss_task": {
        "can_answer": [
            "Dismiss a task as won't-do / not relevant (distinct from resolve = done)",
        ],
        "requires": "CHAT_SKILLS_TASK_MANAGER_URL; task_id required",
        "cannot_answer": "Batch dismissal of multiple tasks in one call",
    },
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
        "healthcare_query: ICD-10, CMS coverage, code lookups; NPI-by-number via registry",
        "healthcare_npi_lookup: NPPES by 10-digit NPI only (fallback label; prefer healthcare_query for codes/coverage)",
        "Provider Roster / Credentialing report",
        "Providers at each practice site (operational roster; Step 4)",
        "Roster reconciliation report (upload vs outside-in)",
        "Document upload skill (attach files to thread; API + UI)",
        "List thread document uploads (what files are already attached)",
        "Task management: list, create, edit, assign, resolve, dismiss tasks (list_tasks, create_task, patch_task, assign_task, resolve_task, dismiss_task)",
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
    # 2026-04-18 disconnect — removed 7 credentialing/roster/strategy
    # tool identifiers from the advertised tool list + the routing rule.
    # The chat pipeline now routes to search_corpus, search_uploaded_document,
    # google_search/web_scrape for web, healthcare_query/_npi_lookup for
    # code + registry facts, and task CRUD.
    return {
        "rag_scopes": ["payer_manuals", "state_contracts", "internal_docs"],
        "tools": [
            "google_search",
            "web_scrape",
            "healthcare_npi_lookup",
            "healthcare_query",
            "document_upload_skill",
            "list_thread_document_uploads",
            "list_tasks",
            "create_task",
            "patch_task",
            "assign_task",
            "resolve_task",
            "dismiss_task",
        ],
        "web_allowed": True,
        "reasoning_allowed": True,
        "tool_capabilities": TOOL_CAPABILITIES,
        "routing_rule": (
            "Match question to tool capabilities. "
            "Policy / process / payer-manual questions → search_corpus. "
            "Questions referring to an attached document → search_uploaded_document. "
            "ICD-10, HCPCS, CPT code meaning, Medicare/Medicaid coverage (NCD/LCD) → healthcare_query. "
            "10-digit NPI registry lookup → healthcare_query or healthcare_npi_lookup. "
            "Web lookups / current information → google_search then web_scrape."
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
    from app.planner.credentialing_flow_intent import credentialing_flow_intent_for_planner

    payload: dict[str, Any] = {
        "user_message": user_message,
        "context": context or "",
        "available_capabilities": available_capabilities_json(),
        "defaults_policy": defaults_policy_json(),
        "credentialing_flow_intent": credentialing_flow_intent_for_planner(user_message),
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
