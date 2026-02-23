"""Format execution plan for user display and emit step progress (✓/✗).

Gives users confidence by showing:
1. The plan upfront: "First I'll do X, if that fails I'll do Y, then combine..."
2. Progress as steps complete: ✓ Step 1 done, ✓ Step 2 done (fallback: web search), etc.
"""
from app.services.doc_assembly import (
    RETRIEVAL_SIGNAL_CORPUS_PLUS_GOOGLE,
    RETRIEVAL_SIGNAL_GOOGLE_ONLY,
)


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


def format_execution_plan(plan, blueprint: list[dict]) -> list[str]:
    """Format the execution plan for user display. Returns lines to emit as thinking."""
    if not plan or not getattr(plan, "subquestions", None):
        return []

    lines: list[str] = []
    lines.append("My plan:")

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
