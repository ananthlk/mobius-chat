"""Build context pack string for parser from route, state, and last turns."""
from typing import Any

from app.state.context_router import Route
from app.state.jurisdiction import get_jurisdiction_from_active, jurisdiction_to_summary

_MAX_RESOLVED_SLOTS = 6  # spec §11 Q4


def _format_resolved_slots(resolved: dict[str, str]) -> str:
    """Format persisted slot values as a compact jurisdiction block (Improvement 3)."""
    if not resolved:
        return ""
    items = list(resolved.items())[:_MAX_RESOLVED_SLOTS]
    lines = ["Resolved context:"]
    for k, v in items:
        lines.append(f"  {k} = {v}")
    return "\n".join(lines) + "\n\n"


def build_context_pack(
    route: Route,
    state: dict[str, Any],
    last_turns: list[dict[str, Any]],
    open_slots: list[str],
    last_turn_sources: list[dict[str, Any]] | None = None,
) -> str:
    """Return context string to prepend before user message for STANDALONE | LIGHT | STATEFUL."""
    if route == "STANDALONE":
        return ""
    active = (state or {}).get("active") or {}
    j = get_jurisdiction_from_active(active)
    jurisdiction_summary = jurisdiction_to_summary(j) or "—"
    payer = (j.get("payor") or "").strip() or "—"
    domain = (active.get("domain") or "").strip() or "—"
    state_val = (j.get("state") or "").strip() or "—"
    program = (j.get("program") or "").strip() or "—"
    perspective = (j.get("perspective") or "").strip() or "—"
    slots_str = ", ".join(open_slots) if open_slots else "none"
    header = (
        f"Context: jurisdiction={jurisdiction_summary} (state={state_val} payor={payer} program={program} perspective={perspective}); "
        f"domain={domain}. Open questions: {slots_str}. Do not use patient-specific details."
    )
    sources_line = ""
    if last_turn_sources:
        names = [s.get("document_name") or "document" for s in last_turn_sources[:10]]
        sources_line = f" Previous turn(s) sources used: {', '.join(names)}."
    header = header.rstrip() + sources_line

    # Improvement 3: prepend resolved slot values so planner always knows jurisdiction
    resolved_slots = (state or {}).get("resolved_slots") or {}
    resolved_block = _format_resolved_slots(resolved_slots)

    if route == "LIGHT":
        if not last_turns:
            return resolved_block + header + "\n\n"
        t = last_turns[0]
        user_content = (t.get("user_content") or "").strip()
        assistant_text = (
            (t.get("context_summary") or "").strip()
            or (t.get("assistant_content") or "")[:200] + ("..." if len(t.get("assistant_content") or "") > 200 else "")
        )
        return resolved_block + header + "\n\nLast turn:\nUser: " + (user_content or "") + "\nAssistant: " + assistant_text + "\n\n"
    if route == "STATEFUL":
        parts = [resolved_block + header] if resolved_block else [header]
        for i, t in enumerate(last_turns[:2]):
            user_content = (t.get("user_content") or "").strip()
            assistant_text = (
                (t.get("context_summary") or "").strip()
                or (t.get("assistant_content") or "")[:300] + ("..." if len(t.get("assistant_content") or "") > 300 else "")
            )
            parts.append(f"Turn {i+1}:\nUser: {user_content or ''}\nAssistant: {assistant_text}")
        return "\n\n".join(parts) + "\n\n"
    return resolved_block + header + "\n\n"
