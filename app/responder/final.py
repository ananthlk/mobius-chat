"""Final responder: format plan + stub answers into a chat-style message."""
from app.planner.schemas import Plan


def format_response(plan: Plan, stub_answers: list[str]) -> str:
    """Build chat-style final message from plan and per-subquestion stub answers."""
    lines = ["Here’s what I found based on your question.\n"]
    for i, sq in enumerate(plan.subquestions):
        ans = stub_answers[i] if i < len(stub_answers) else "[No answer yet]"
        kind_label = "Policy/document" if sq.kind == "non_patient" else "Personal (we don’t have access yet)"
        lines.append(f"**{sq.id}** ({kind_label}): {sq.text}")
        lines.append(f"→ {ans}\n")
    return "\n".join(lines)
