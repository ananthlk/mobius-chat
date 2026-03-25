"""
Server-authored workflow selection groups for the chat UI.

Any pipeline stage (tools, orchestrator, future skills) can attach **authoritative**
choice lists so the user can pick one or many options before the next turn. The
integrator/LLM may summarize in prose; **clickable values always come from the server**
(`clarification_options` on the completed response).

Payload shape — each element of ``clarification_options`` / ``pending_workflow_selection``:

  slot: str
    Stable key, e.g. ``route``, ``jurisdiction.payor``, ``npi_disambiguation``, ``payer_pick``.
  label: str
    Short heading shown above the chips/checkboxes.
  selection_mode: "single" | "multiple"
    single: one click sends that choice as the next user message.
    multiple: user toggles choices, then confirms; min/max enforce workflow rules.
  choices: list of { "value": str, "label": str, "choice_id"?: str }
    ``value`` is sent verbatim as the user message (or combined for multiple).
  min_choices: int | null  (optional; multiple only, default 1)
  max_choices: int | null  (optional; multiple only, default len(choices))
  context_type: str | null (optional hint: npi, route, jurisdiction, generic, …)

Jurisdiction clarification and route clash already use the same ``clarification_options`` key;
this module normalizes and builds additional groups for NPI disambiguation and future flows.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def normalize_selection_group(raw: dict[str, Any] | None) -> dict[str, Any] | None:
    """Validate and return a group dict, or None if invalid."""
    if not isinstance(raw, dict):
        return None
    slot = str(raw.get("slot") or "").strip()
    label = str(raw.get("label") or "").strip()
    mode = str(raw.get("selection_mode") or "single").strip().lower()
    if mode not in ("single", "multiple"):
        mode = "single"
    choices_in = raw.get("choices")
    if not isinstance(choices_in, list):
        return None
    choices: list[dict[str, str]] = []
    for c in choices_in:
        if not isinstance(c, dict):
            continue
        val = str(c.get("value") or "").strip()
        lab = str(c.get("label") or "").strip() or val
        if not val:
            continue
        item: dict[str, str] = {"value": val, "label": lab}
        cid = c.get("choice_id")
        if cid is not None and str(cid).strip():
            item["choice_id"] = str(cid).strip()
        choices.append(item)
    if not choices:
        return None
    if not slot:
        slot = "workflow_selection"
    if not label:
        label = "Choose an option"
    out: dict[str, Any] = {
        "slot": slot[:200],
        "label": label[:500],
        "selection_mode": mode,
        "choices": choices,
    }
    for key in ("min_choices", "max_choices"):
        v = raw.get(key)
        if v is None:
            continue
        try:
            n = int(v)
            if n >= 0:
                out[key] = n
        except (TypeError, ValueError):
            pass
    ct = raw.get("context_type")
    if ct is not None and str(ct).strip():
        out["context_type"] = str(ct).strip()[:120]
    if mode == "multiple":
        if "min_choices" not in out:
            out["min_choices"] = 1
        if "max_choices" not in out:
            out["max_choices"] = len(choices)
    return out


def workflow_selection_group(
    *,
    slot: str,
    label: str,
    choices: list[dict[str, str]],
    selection_mode: str = "single",
    min_choices: int | None = None,
    max_choices: int | None = None,
    context_type: str | None = None,
) -> dict[str, Any] | None:
    """Build one group; returns None if there are no valid choices."""
    raw: dict[str, Any] = {
        "slot": slot,
        "label": label,
        "selection_mode": selection_mode,
        "choices": choices,
    }
    if min_choices is not None:
        raw["min_choices"] = min_choices
    if max_choices is not None:
        raw["max_choices"] = max_choices
    if context_type:
        raw["context_type"] = context_type
    return normalize_selection_group(raw)


def merge_clarification_option_lists(
    existing: list[dict[str, Any]] | None,
    extra: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Append normalized ``extra`` groups to ``existing`` (jurisdiction + tool groups)."""
    out: list[dict[str, Any]] = []
    for g in existing or []:
        ng = normalize_selection_group(g) if isinstance(g, dict) else None
        if ng:
            out.append(ng)
    for g in extra or []:
        ng = normalize_selection_group(g) if isinstance(g, dict) else None
        if ng:
            out.append(ng)
    return out


def attach_workflow_selection(ctx: Any, groups: list[dict[str, Any]] | None) -> None:
    """Extend ``ctx.pending_workflow_selection`` with normalized groups."""
    if not groups:
        return
    merged: list[dict[str, Any]] = []
    for g in groups:
        ng = normalize_selection_group(g) if isinstance(g, dict) else None
        if ng:
            merged.append(ng)
    if not merged:
        return
    prev = getattr(ctx, "pending_workflow_selection", None)
    if not isinstance(prev, list):
        prev = []
    ctx.pending_workflow_selection = list(prev) + merged


def format_npi_org_search_markdown(search_name: str, results: list[dict[str, Any]]) -> str:
    """Readable markdown for tool/ReAct context (matches MCP-style tiers)."""
    if not results:
        return f'No NPIs found for "{search_name}". Try the exact legal name or an address.'
    _ICON = {"exact": "●", "partial": "◐", "fuzzy": "○", "none": "○"}
    _LABEL = {
        "exact": "Exact match",
        "partial": "Partial match",
        "fuzzy": "Fuzzy match",
        "none": "Low confidence",
    }
    lines: list[str] = [
        f"## NPI lookup: {search_name}",
        "",
    ]
    exact = [r for r in results if (r.get("match_type") or "") == "exact"]
    partial = [r for r in results if (r.get("match_type") or "") == "partial"]
    fuzzy = [r for r in results if (r.get("match_type") or "") in ("fuzzy", "none")]
    for tier_label, tier_results in (
        ("Exact matches", exact),
        ("Partial matches", partial),
        ("Other matches", fuzzy),
    ):
        if not tier_results:
            continue
        lines.append(f"### {tier_label}")
        lines.append("")
        for r in tier_results:
            mt = r.get("match_type") or "none"
            icon = _ICON.get(str(mt), "○")
            label = _LABEL.get(str(mt), "Match")
            score = r.get("match_score", 0.0)
            try:
                spct = int(float(score) * 100)
            except (TypeError, ValueError):
                spct = 0
            score_note = f" ({spct}%)" if mt in ("partial", "fuzzy", "none") else ""
            name = (r.get("name") or "").strip()
            npi = (r.get("npi") or "").strip()
            src = (r.get("source") or "").upper()
            lines.append(f"{icon} **{name}** — NPI `{npi}` ({src}, {label}{score_note})")
            lines.append("")
    if len(results) > 1:
        lines.append(
            "**Use the choice buttons below** to confirm which billing organization/NPI you mean, "
            "or type the NPI or name in your own words."
        )
    return "\n".join(lines).strip()


def format_npi_org_search_summary_for_disambiguation(
    search_name: str, results: list[dict[str, Any]]
) -> str:
    """Minimal tool text when the UI shows choice chips — keeps the integrator from re-listing every row."""
    if not results:
        return f'No NPIs found for "{search_name}". Try the exact legal name or an address.'
    exact_n = sum(1 for r in results if (r.get("match_type") or "") == "exact")
    partial_n = sum(1 for r in results if (r.get("match_type") or "") == "partial")
    other_n = sum(1 for r in results if (r.get("match_type") or "") in ("fuzzy", "none"))
    n = len(results)
    return (
        f'NPPES/PML (Florida) organization search for "{search_name}" returned **{n}** billing-organization '
        f"candidate(s): {exact_n} exact, {partial_n} partial, {other_n} other/low-confidence. "
        "**The chat UI lists each candidate as selectable options below** (pick one or more, then Continue). "
        "The registry list is only in those options — do not reproduce names or NPIs as a table or bullet list."
    )


def build_npi_org_disambiguation_groups(
    results: list[dict[str, Any]],
    search_name: str,
    *,
    max_choices: int = 20,
) -> list[dict[str, Any]]:
    """Multi-select group (min 1) so users can confirm one billing org or short-list several."""
    capped = results[:max_choices]
    if len(capped) <= 1:
        return []
    choices: list[dict[str, str]] = []
    for r in capped:
        npi = str(r.get("npi") or "").strip()
        name = str(r.get("name") or "").strip()
        if not npi:
            continue
        mt = str(r.get("match_type") or "none").strip()
        tier = {"exact": "Exact", "partial": "Partial", "fuzzy": "Fuzzy", "none": "Low conf"}.get(mt, mt)
        chip_label = f"{npi} — {name[:46]}{'…' if len(name) > 46 else ''} ({tier})"
        value = f"Use billing NPI {npi} for {name}" if name else f"Use billing NPI {npi}"
        choices.append({"value": value, "label": chip_label, "choice_id": npi})
    if len(choices) < 2:
        return []
    g = workflow_selection_group(
        slot="npi_disambiguation",
        label=f'Select one or more billing organizations for "{search_name[:60]}" (Continue), or type an NPI.',
        choices=choices,
        selection_mode="multiple",
        min_choices=1,
        max_choices=len(choices),
        context_type="npi",
    )
    return [g] if g else []


__all__ = [
    "attach_workflow_selection",
    "build_npi_org_disambiguation_groups",
    "format_npi_org_search_markdown",
    "format_npi_org_search_summary_for_disambiguation",
    "merge_clarification_option_lists",
    "normalize_selection_group",
    "workflow_selection_group",
]
