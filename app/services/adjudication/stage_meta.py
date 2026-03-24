"""Build STAGE_METADATA dict for v2 adjudication from usage + thinking log."""
from __future__ import annotations

from typing import Any


def build_stage_metadata(
    *,
    thinking_log: list[str] | None,
    tool_fired: str = "unknown",
    expected_tool: str | None = None,
    iterations: int = 0,
    legacy_path: bool = False,
    usage_breakdown: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Populate planner/rag/integrator/badge models from usage_breakdown when available."""

    planner_model = "unknown"
    rag_model = "unknown"
    integrator_model = "unknown"
    badge_model = "unknown"

    for r in usage_breakdown or []:
        if not isinstance(r, dict):
            continue
        st = str(r.get("stage") or "").strip()
        m = str(r.get("model") or "").strip() or "unknown"
        if st == "integrator":
            integrator_model = m
        elif st == "rag":
            rag_model = m
        elif st == "planner":
            planner_model = m
        elif st.startswith("react_"):
            planner_model = m
        elif st == "badge":
            badge_model = m

    def _find_jurisdiction(log: list[str]) -> str:
        for line in log or []:
            low = line.lower()
            if "payer=" in low or "jurisdiction" in low:
                return line.strip()[:500]
        return "none detected"

    return {
        "planner_model": planner_model,
        "rag_model": rag_model,
        "integrator_model": integrator_model,
        "badge_model": badge_model,
        "tool_fired": tool_fired,
        "expected_tool": (expected_tool or "unknown"),
        "iterations": iterations,
        "legacy_path": legacy_path,
        "jurisdiction": _find_jurisdiction(thinking_log or []),
    }
