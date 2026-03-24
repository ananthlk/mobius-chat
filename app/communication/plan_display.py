"""Format execution plan for user display and emit step progress (✓/✗).

Gives users confidence by showing:
1. The plan upfront: "First I'll do X, if that fails I'll do Y, then combine..."
2. Progress as steps complete: ✓ Step 1 done, ✓ Step 2 done (fallback: web search), etc.
"""
import urllib.parse

from app.services.doc_assembly import (
    RETRIEVAL_SIGNAL_CORPUS_PLUS_GOOGLE,
    RETRIEVAL_SIGNAL_GOOGLE_ONLY,
)


# ---------------------------------------------------------------------------
# Jurisdiction emit helpers
# ---------------------------------------------------------------------------

def jurisdiction_summary(active: dict | None) -> str:
    """Convert active state dict to plain-language jurisdiction string.
    Returns 'Payer · Program · State' or subsets thereof.
    Returns empty string if nothing is established.
    """
    if not active:
        return ""
    parts = []
    payer = (active.get("payer") or "").strip()
    program = (active.get("program") or "").strip()
    state = (active.get("jurisdiction") or "").strip()
    if payer:
        parts.append(payer)
    if program:
        parts.append(program)
    if state:
        parts.append(state)
    return " · ".join(parts)


def emit_jurisdiction_context(
    active: dict | None,
    reset_reason: str | None,
    emitter,
) -> None:
    """Emit jurisdiction context at start of turn.
    Called from run_resolve() before the subquestion loop.
    """
    if not emitter:
        return

    summary = jurisdiction_summary(active)

    # Multi-payer
    payers = (active or {}).get("payers") if active else None
    if payers and len(payers) > 1:
        names = ", ".join(str(p) for p in payers[:3])
        emitter(f"⚠ Multiple payers detected: {names}")
        emitter("  I'll answer generally — specify one payer for policy-specific details.")
        return

    # Payer change
    if reset_reason == "payer_change":
        prior_payer = (active or {}).get("_prior_payer", "")
        current_payer = (active or {}).get("payer", "")
        if prior_payer and current_payer:
            emitter(f"⟳ Payer change: {prior_payer} → {current_payer}")
            if summary:
                emitter(f"  Starting fresh for {summary}")
        elif summary:
            emitter(f"⟳ Context reset: {summary}")
        return

    # No jurisdiction at all
    if not summary:
        emitter("? Payer not identified — I'll search broadly.")
        emitter("  Mention a specific payer for a more precise answer.")
        return

    # Normal: established this turn vs carried forward
    if (active or {}).get("_jurisdiction_new"):
        emitter(f"✓ Confirmed: {summary}")
    else:
        emitter(f"↺ Carrying forward: {summary}")


# Layer labels for emits
_LAYER_LABELS = {
    "RAG": "our materials",
    "google_search": "the web",
    "web_scrape": "page",
    "npi_lookup": "NPPES registry",
    "healthcare_query": "healthcare database",
    "roster_report": "credentialing pipeline",
    "reasoning": "general knowledge",
}


def emit_layer_attempt(agent: str, tool_hint: str | None, url: str | None, emitter) -> None:
    """Emit ◌ line before a layer attempt."""
    if not emitter:
        return
    if agent == "RAG":
        emitter("◌ Searching our materials...")
    elif agent == "tool" and tool_hint == "web_scrape" and url:
        domain = urllib.parse.urlparse(url).netloc or url[:40]
        emitter(f"◌ Reading page: {domain}")
    elif agent == "tool" and tool_hint == "google_search":
        emitter("◌ Searching the web...")
    elif agent == "tool" and tool_hint in ("npi_lookup", "search_org_names"):
        emitter("◌ Looking up provider in NPPES registry...")
    elif agent == "tool" and tool_hint == "healthcare_query":
        emitter("◌ Querying healthcare database...")
    elif agent == "tool" and tool_hint == "roster_report":
        pass  # roster_agent emits its own step-by-step progress
    elif agent == "reasoning":
        emitter("◌ Reasoning from general knowledge...")


def emit_fallback(from_agent: str, to_agent: str, emitter) -> None:
    """Emit ⬇ line when falling back to next layer."""
    if not emitter:
        return
    if from_agent == "RAG":
        emitter("⬇ Not in our materials — searching the web")
    elif from_agent == "tool" and to_agent == "reasoning":
        emitter("⬇ Nothing specific found — answering from general knowledge")
    elif from_agent == "tool":
        emitter("⬇ Tool returned no result — trying next approach")
    else:
        emitter("⬇ No result — trying next approach")


def _fallback_desc(on_rag_fail: list[str] | None) -> str | None:
    """Human-readable fallback description from on_rag_fail list."""
    if not on_rag_fail:
        return None
    for fb in on_rag_fail:
        fb_lower = (fb or "").strip().lower()
        if "google" in fb_lower or "web" in fb_lower or "search" in fb_lower:
            return "search the web"
        if "tool" in fb_lower:
            return "use tools"
        if "reason" in fb_lower:
            return "reason through it"
    return "use fallback"


def _is_roster_request(text: str) -> bool:
    """True if message matches roster/credentialing triggers."""
    t = (text or "").strip().lower()
    triggers = (
        "provider roster", "credentialing report", "roster report", "medicaid roster",
        "roster for", "medicaid npi report", "create a medicaid npi report",
        "create medicaid npi report", "create a credentialing report", "create credentialing report",
        "i want to create a medicaid npi report", "i want to create a credentialing report",
    )
    return any(tr in t for tr in triggers)


def _extract_org_for_roster(text: str) -> str:
    """Extract org name from roster request (e.g. 'Create a Medicaid NPI report for Aspire' -> 'Aspire')."""
    t = (text or "").strip()
    tl = t.lower()
    for prefix in (
        "provider roster for", "credentialing report for", "roster report for",
        "medicaid roster for", "roster for", "create a medicaid npi report for",
        "create medicaid npi report for", "create a credentialing report for",
        "create credentialing report for", "i want to create a medicaid npi report for",
        "i want to create a credentialing report for", "medicaid npi report for",
    ):
        if prefix in tl:
            return t[tl.find(prefix) + len(prefix) :].strip()
    return t[:80].strip() if t else ""


def format_execution_plan(plan, blueprint: list[dict], user_message: str | None = None) -> list[str]:
    """Format the execution plan for user display. Returns lines to emit as thinking."""
    if not plan or not getattr(plan, "subquestions", None):
        return []

    lines: list[str] = []
    lines.append("My plan:")

    # Roster: single tool subquestion — use roster-specific wording
    if user_message and _is_roster_request(user_message) and len(plan.subquestions) == 1:
        org = _extract_org_for_roster(user_message)
        lines.append(f"  1. I'll run the Medicaid NPI / Credentialing report plan for {org or '[org]'} (11 steps).")
        return lines

    for i, sq in enumerate(plan.subquestions):
        bp = blueprint[i] if i < len(blueprint) else {}
        agent = bp.get("agent") or "RAG"
        text = (bp.get("text") or sq.text or "").strip()
        snippet = (text[:70] + "…") if len(text) > 70 else text
        on_rag_fail = bp.get("on_rag_fail")
        fallback = _fallback_desc(on_rag_fail) if isinstance(on_rag_fail, list) else None

        if agent == "patient_stub":
            lines.append(f"  {i + 1}. This part is about your own info—I can't access that yet.")
        elif agent == "tool":
            lines.append(f"  {i + 1}. I'll search the web / use tools for: \"{snippet}\"")
        elif agent == "reasoning":
            lines.append(f"  {i + 1}. I'll reason through: \"{snippet}\"")
        else:
            # RAG
            if fallback:
                lines.append(
                    f"  {i + 1}. I'll look up \"{snippet}\" in our materials. "
                    f"If nothing relevant: {fallback}."
                )
            else:
                lines.append(f"  {i + 1}. I'll look up \"{snippet}\" in our materials.")

    if len(plan.subquestions) > 1:
        lines.append("  Then I'll combine the results and format the answer.")

    return lines


def format_step_done(
    step_num: int,
    total: int,
    success: bool,
    used_fallback: str | None = None,
) -> str:
    """Format step completion status for user: ✓ or ✗ with optional fallback note."""
    marker = "✓" if success else "✗"
    label = f"Step {step_num}"
    if total > 1:
        label = f"Step {step_num}/{total}"
    if success and used_fallback:
        return f"{marker} {label} done ({used_fallback})"
    if success:
        return f"{marker} {label} done"
    return f"{marker} {label} failed"


def retrieval_signal_to_fallback_note(signal: str | None) -> str | None:
    """Map retrieval_signal to user-facing fallback note, or None if no fallback used."""
    if not signal:
        return None
    if signal == RETRIEVAL_SIGNAL_GOOGLE_ONLY:
        return "used web search — nothing relevant in our materials"
    if signal == RETRIEVAL_SIGNAL_CORPUS_PLUS_GOOGLE:
        return "added web search to complement our materials"
    # corpus_only, no_sources: no fallback; success without mentioning fallback
    return None
