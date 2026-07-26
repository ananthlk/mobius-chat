"""bubble-backend — the BFF read-only aggregator for the chat-bubble surface.

The single point the chat bubble's frontend (render/bubble -> answer-card + card-render-model)
maps to. It SHAPES the enricher's produced outputs (the final_parallel merged card +
assistant_envelope typed blocks + retriever sources) into what the bubble renders. It does
not produce anything and never touches the "brain".

Contract: docs/bubble-backend-contract.md. Invariants — enforced structurally by
tests/test_bubble_backend.py, not by policy:

  1. Read-only aggregator. Reads produced outputs (passed in as args); imports none of them.
  2. Presentation-only. NO LLM calls, retrieval, re-ranking, or mutation of answer content.
     Enforced by the import-guard test (no router/planner/enricher/llm/retriever imports).
  3. Positive-filter allowlist. Only allowlisted keys reach the client card; every other
     field on the produced card is dropped, so internal enricher state never hits the wire.
     Enforced by the allowlist-drop test.
  4. Semantics (what B/C fields mean) belong to the enricher; presentation (tab mapping,
     envelope selection, phase->slot) belongs here.

This module owns only stdlib imports (json, typing). That is the point.
"""
from __future__ import annotations

import json
from typing import Any

# --- Positive allowlist (MOVED from integrate.py:_ANSWER_CARD_ENVELOPE_KEYS) --------------
# ONLY these optional fields (plus the base mode/direct_answer/sections) are copied onto the
# client card. Everything else on the produced card — raw reasoning, source_confidence_override
# internals, any future internal field — is dropped. This is a security boundary, not a
# convenience: consolidation must never degrade it into "pass through whatever is on the card".
ANSWER_CARD_ENVELOPE_KEYS: tuple[str, ...] = (
    "citations",
    "confidence_note",
    "required_variables",
    "followups",
    "thread_summary",
    "suggested_actions",
    "correction",
    "takeaways",
    "gaps",
    "recital",
    "next_questions_for_user",
)


def shape_answer_card(
    mode: str,
    direct_answer: str,
    sections: list[Any],
    *,
    extra_from: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Shape the produced answer fields into the client answer-card dict.

    POSITIVE FILTER: the result contains exactly ``mode``, ``direct_answer``, ``sections``,
    and any allowlisted key present (and non-None) on ``extra_from``. No other field on
    ``extra_from`` can reach the client. Presentation only — reads no answer meaning, mutates
    nothing.
    """
    card: dict[str, Any] = {"mode": mode, "direct_answer": direct_answer, "sections": sections}
    src = extra_from or {}
    for key in ANSWER_CARD_ENVELOPE_KEYS:
        value = src.get(key)
        if value is not None:
            card[key] = value
    return card


def shape_answer_card_json(
    mode: str,
    direct_answer: str,
    sections: list[Any],
    *,
    extra_from: dict[str, Any] | None = None,
) -> str:
    """JSON form — the drop-in for integrate.py's ``_answer_card_json_for_client`` at cutover.

    The produce/shape cut is a move, not a copy: when this lands, integrate.py delegates its
    allowlist copy here (LLM Agent's atomic edit) so there is one source of truth, not two.
    """
    return json.dumps(shape_answer_card(mode, direct_answer, sections, extra_from=extra_from))
