"""Context router: decide STANDALONE | LIGHT | STATEFUL for context pack. No embeddings."""
from typing import Any, Literal
import re

Route = Literal["STANDALONE", "LIGHT", "STATEFUL"]

PRONOUN_REF = re.compile(
    r"\b(that|this|above|same|previous|those|it|them)\b",
    re.I,
)
NEW_TOPIC = re.compile(
    r"\b(new question|different topic|different question|new topic|switch to)\b",
    re.I,
)


def route_context(
    user_text: str,
    existing_state: dict[str, Any],
    last_turns: list[dict[str, Any]],
    reset_reason: str | None = None,
) -> Route:
    """Return STANDALONE | LIGHT | STATEFUL so the pipeline can build the context pack."""
    text = (user_text or "").strip().lower()
    active = (existing_state or {}).get("active") or {}
    payer = (active.get("payer") or "").strip()
    domain = (active.get("domain") or "").strip()
    open_slots = (existing_state or {}).get("open_slots") or []

    # STANDALONE if: user explicitly changes payer (reset_reason), new question/different topic, domain changed and no pronoun refs
    if reset_reason == "payer_change":
        return "STANDALONE"
    if NEW_TOPIC.search(text):
        return "STANDALONE"

    # STATEFUL if any: pronoun/ref; payer missing but state has payer; domain missing but state has domain; open_slots not empty
    if PRONOUN_REF.search(text):
        return "STATEFUL"
    if payer and "sunshine" not in text and "united" not in text and "aetna" not in text and "uhc" not in text:
        # Payer in state but user didn't mention a payer name
        pass  # could still be STATEFUL if we want to include state; check next
    if open_slots:
        return "STATEFUL"
    if payer or domain:
        # User didn't say "new question" and state has context -> STATEFUL so we include state header
        return "STATEFUL"

    return "LIGHT"
