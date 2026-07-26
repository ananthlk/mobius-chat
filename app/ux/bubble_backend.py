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


# --- B/C typed-block shape (MOVED from assistant_envelope.py:417 build) --------------------
# The presentation shape for the enricher's B (critic) + C (enrichment) fields — takeaways,
# gaps, correction, suggested_actions, next_steps, next_questions. bubble-backend BUILDS these
# blocks from the merged card (per contract invariant 1: one builder). assistant_envelope
# delegates to these per-block builders at cutover, keeping its own block ORDERING while the
# SHAPE lives here once. All pure (stdlib only) — the UX-policy `collapsed_default` is computed
# by the caller and passed in, so this module imports nothing new and stays guard-clean.


def build_correction_block(card: dict[str, Any]) -> dict[str, Any] | None:
    """B: correction {original, corrected} → a correction block, shown before the draft."""
    corr = card.get("correction")
    if isinstance(corr, dict):
        orig = (corr.get("original") or "").strip()
        fixed = (corr.get("corrected") or "").strip()
        if orig and fixed:
            return {"type": "correction", "original": orig[:2000], "corrected": fixed[:2000]}
    return None


def build_takeaways_block(card: dict[str, Any]) -> dict[str, Any] | None:
    """B: takeaways[] → a takeaways block (≤5 distilled bullets)."""
    tw = card.get("takeaways")
    if isinstance(tw, list):
        items = [str(t).strip() for t in tw if t and str(t).strip()][:5]
        if items:
            return {"type": "takeaways", "items": items}
    return None


def build_gaps_callout_block(card: dict[str, Any]) -> dict[str, Any] | None:
    """B: gaps[] → an info callout ("Sources did not cover: …"), ≤4 lines."""
    gaps = card.get("gaps")
    if isinstance(gaps, list):
        lines = [str(g).strip() for g in gaps if g and str(g).strip()][:4]
        if lines:
            return {
                "type": "callout",
                "variant": "info",
                "body": "**Sources did not cover:**\n\n" + "\n".join(f"- {g}" for g in lines),
            }
    return None


def build_action_chips_block(card: dict[str, Any]) -> dict[str, Any] | None:
    """C: suggested_actions[] → an action_chips block (external_link chips only)."""
    sa = card.get("suggested_actions")
    if isinstance(sa, list) and sa:
        chips = [
            a for a in sa
            if isinstance(a, dict)
            and a.get("type") == "external_link"
            and isinstance(a.get("label"), str)
            and isinstance(a.get("url"), str)
        ]
        if chips:
            return {"type": "action_chips", "chips": chips}
    return None


def followup_items(items: list[Any], *, fallback_clickable: bool) -> list[dict[str, Any]]:
    """Normalize follow-up items to [{text, clickable}] (accepts dicts or legacy strings, ≤8)."""
    out: list[dict[str, Any]] = []
    for x in items or []:
        if isinstance(x, dict):
            t = (x.get("text") or "").strip()
            if not t:
                continue
            c = x.get("clickable")
            if c is None:
                c = fallback_clickable
            out.append({"text": t[:500], "clickable": bool(c)})
        elif isinstance(x, str) and x.strip():
            out.append({"text": x.strip()[:500], "clickable": fallback_clickable})
        if len(out) >= 8:
            break
    return out


def build_next_steps_block(next_steps: list[Any], *, collapsed_default: bool) -> dict[str, Any] | None:
    """C: next_steps → a next_steps block. `collapsed_default` is the caller's UX-policy value."""
    items = followup_items(next_steps, fallback_clickable=False)
    if items:
        return {"type": "next_steps", "items": items, "collapsed_default": collapsed_default}
    return None


def build_next_questions_block(next_questions: list[Any], *, collapsed_default: bool) -> dict[str, Any] | None:
    """C: next_questions_for_user → a suggested_questions block."""
    items = followup_items(next_questions, fallback_clickable=True)
    if items:
        return {"type": "suggested_questions", "items": items, "collapsed_default": collapsed_default}
    return None
