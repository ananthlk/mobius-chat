"""Per-step workflow follow-ups: user notes (co-pilot) vs system hints (autopilot).

Structured for future workflow / task tracking. Each item:
  { "text": str, "source": "user" | "system", "kind"?: str }
"""

from __future__ import annotations

from typing import Any

from app.services.roster_credentialing_orchestrator import OrchestratorState, ROSTER_CREDENTIALING_STEP_IDS, StepState


def _normalize_item(raw: Any, *, source: str) -> dict[str, Any] | None:
    if isinstance(raw, str):
        t = raw.strip()
        if not t:
            return None
        return {"text": t, "source": source}
    if isinstance(raw, dict):
        t = str(raw.get("text") or "").strip()
        if not t:
            return None
        out: dict[str, Any] = {"text": t, "source": str(raw.get("source") or source)}
        k = raw.get("kind")
        if k is not None and str(k).strip():
            out["kind"] = str(k).strip()
        return out
    return None


def merge_user_follow_ups(step: StepState, raw: Any) -> None:
    """Append user follow-ups from validate payload (strings or dicts). Dedupes by text (case-fold)."""
    if raw is None:
        return
    items: list[Any]
    if isinstance(raw, str):
        items = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    elif isinstance(raw, list):
        items = list(raw)
    else:
        return
    existing = {str(x.get("text", "")).strip().casefold() for x in step.workflow_follow_ups if isinstance(x, dict)}
    for it in items:
        n = _normalize_item(it, source="user")
        if not n:
            continue
        key = n["text"].casefold()
        if key in existing:
            continue
        existing.add(key)
        step.workflow_follow_ups.append(n)


def replace_system_follow_ups(step: StepState, items: list[dict[str, Any]]) -> None:
    """Drop prior system lines for this step, then append new system suggestions."""
    step.workflow_follow_ups = [x for x in step.workflow_follow_ups if not (isinstance(x, dict) and x.get("source") == "system")]
    for it in items:
        if isinstance(it, dict) and str(it.get("text") or "").strip():
            row = dict(it)
            row["source"] = "system"
            step.workflow_follow_ups.append(row)


def system_suggest_follow_ups(step_id: str, state: OrchestratorState) -> list[dict[str, Any]]:
    """Heuristic next-step / hygiene lines after a step completes (autopilot)."""
    st = state.step_by_id(step_id)
    status = (st.status if st else "") or ""
    out: list[dict[str, Any]] = []

    if status == "failed":
        return [
            {
                "text": (st.result_summary if st else "") or "Step failed — fix this gate before re-running downstream steps.",
                "kind": "step_failed",
            }
        ]

    if status == "skipped":
        out.append(
            {
                "text": f"Step skipped ({step_id}): confirm prerequisites before relying on downstream results.",
                "kind": "skipped_step",
            }
        )
        return out

    if step_id == "identify_org":
        npis = [x for x in (state.org_npis or []) if str(x).strip()]
        if len(npis) > 1:
            out.append(
                {
                    "text": "Multiple billing NPIs resolved — confirm which entity owns this credentialing scope.",
                    "kind": "multi_npi",
                }
            )
        elif len(npis) == 0:
            out.append({"text": "No org NPI locked — verify organization name / registry match.", "kind": "missing_npi"})

    if step_id == "find_locations":
        locs = state.locations if isinstance(state.locations, list) else []
        if len(locs) == 0:
            out.append({"text": "No practice locations returned — verify org NPI / name if sites are missing.", "kind": "no_locations"})
        elif len(locs) == 1:
            out.append({"text": "Only one site found — spot-check that all practice addresses are captured.", "kind": "single_site"})

    if step_id == "find_associated_providers":
        if not _roster_nonempty(state.active_roster):
            out.append(
                {
                    "text": "Active roster empty or not set — confirm operational panel before benchmarks / PML steps.",
                    "kind": "active_roster",
                }
            )
        else:
            out.append(
                {
                    "text": "Review active panel vs full association list; update NPPES / roster if providers are missing.",
                    "kind": "roster_review",
                }
            )

    if step_id == "step_6":
        flagged = state.pml_flagged if isinstance(state.pml_flagged, list) else []
        if flagged:
            out.append(
                {
                    "text": f"{len(flagged)} provider(s) flagged in PML validation — remediate taxonomy / ZIP / enrollment issues.",
                    "kind": "pml_flagged",
                }
            )
        else:
            out.append({"text": "PML validation clean for reviewed rows — no action required on flagged items.", "kind": "pml_ok"})

    if step_id == "step_7":
        missing = state.missing_enrollment if isinstance(state.missing_enrollment, list) else []
        if missing:
            out.append(
                {
                    "text": f"{len(missing)} enrollment gap(s) — follow up on PML / Medicaid enrollment.",
                    "kind": "missing_enrollment",
                }
            )

    if step_id == "build_report" and status == "done":
        out.append({"text": "Report generated — distribute for operational review and file per policy.", "kind": "report_done"})

    if not out and status == "done":
        out.append(
            {
                "text": f"Step {step_id} completed — no specific follow-up generated; add tasks if the ops team needs them.",
                "kind": "generic_done",
            }
        )

    return out


def _roster_nonempty(roster: Any) -> bool:
    if not isinstance(roster, dict) or not roster:
        return False
    return any(isinstance(v, list) and len(v) > 0 for v in roster.values())


def apply_system_follow_ups_after_step(state: OrchestratorState, step_id: str) -> None:
    if step_id not in ROSTER_CREDENTIALING_STEP_IDS:
        return
    st = state.step_by_id(step_id)
    if not st:
        return
    mode = (getattr(state, "credentialing_run_mode", None) or "copilot").strip().lower()
    if mode != "autopilot":
        return
    suggestions = system_suggest_follow_ups(step_id, state)
    replace_system_follow_ups(st, suggestions)
