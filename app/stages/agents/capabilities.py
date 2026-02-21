"""Path capabilities registry: what each agent path can answer.

Fed to the parser/planner so it decomposes questions into subquestions
that match supported capabilities. Single source of truth.
"""
from typing import Any

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
        "NPI lookup (NPPES/Medicaid)",
        "provider data",
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


def available_capabilities_json() -> dict[str, Any]:
    """Build structured available_capabilities for Mobius Planner input (JSON)."""
    return {
        "rag_scopes": ["payer_manuals", "state_contracts", "internal_docs"],
        "tools": ["google_search", "web_scrape", "npi_lookup", "bigquery_templates", "internal_api"],
        "web_allowed": True,
        "reasoning_allowed": True,
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


def planner_input_json(user_message: str, context: str = "") -> dict[str, Any]:
    """Build full planner input payload (user_message, available_capabilities, defaults_policy)."""
    return {
        "user_message": user_message,
        "context": context or "",
        "available_capabilities": available_capabilities_json(),
        "defaults_policy": defaults_policy_json(),
    }


# Answers for capability questions ("can you search Google?", "what can you do?")
CAPABILITY_ANSWERS: dict[str, str] = {
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
