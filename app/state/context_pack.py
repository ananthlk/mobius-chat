"""Build context pack string for parser from route, state, and last turns."""
from typing import Any

from app.state.context_router import Route


def build_context_pack(
    route: Route,
    state: dict[str, Any],
    last_turns: list[dict[str, Any]],
    open_slots: list[str],
) -> str:
    """Return context string to prepend before user message for STANDALONE | LIGHT | STATEFUL."""
    if route == "STANDALONE":
        return ""
    active = (state or {}).get("active") or {}
    payers_list = active.get("payers") or []
    if payers_list and isinstance(payers_list, list):
        payer = ", ".join(str(p).strip() for p in payers_list if p) or "—"
    else:
        payer = (active.get("payer") or "").strip() or "—"
    domain = (active.get("domain") or "").strip() or "—"
    jurisdiction = (active.get("jurisdiction") or "").strip() or "—"
    role = (active.get("user_role") or "").strip() or "—"
    slots_str = ", ".join(open_slots) if open_slots else "none"
    header = f"Context: payer={payer}; domain={domain}; jurisdiction={jurisdiction}; role={role}. Open questions: {slots_str}. Do not use patient-specific details."
    if route == "LIGHT":
        if not last_turns:
            return header + "\n\n"
        t = last_turns[0]
        user_content = (t.get("user_content") or "").strip()
        assistant_content = (t.get("assistant_content") or "").strip()
        if assistant_content and len(assistant_content) > 200:
            assistant_content = assistant_content[:200] + "..."
        return header + "\n\nLast turn:\nUser: " + (user_content or "") + "\nAssistant: " + (assistant_content or "") + "\n\n"
    if route == "STATEFUL":
        parts = [header]
        for i, t in enumerate(last_turns[:2]):
            user_content = (t.get("user_content") or "").strip()
            assistant_content = (t.get("assistant_content") or "").strip()
            if assistant_content and len(assistant_content) > 300:
                assistant_content = assistant_content[:300] + "..."
            parts.append(f"Turn {i+1}:\nUser: {user_content or ''}\nAssistant: {assistant_content or ''}")
        return "\n\n".join(parts) + "\n\n"
    return header + "\n\n"
