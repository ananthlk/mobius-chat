"""Router chat ``mode`` (copilot vs agentic) restricts Thompson candidate pool."""
from app.services.model_registry import (
    COPILOT_EXCLUDED_THOMPSON_BENCHMARK_CATEGORIES,
    ModelRouter,
)


def test_apply_router_mode_agentic_no_trim():
    r = ModelRouter()
    cands = r._get_candidates("planner", phi_detected=False)
    if not cands:
        return
    out, note = r._apply_router_mode_filter(cands, "agentic")
    assert len(out) == len(cands)
    assert note is None


def test_apply_router_mode_copilot_drops_heavy_benchmark_categories():
    r = ModelRouter()
    cands = r._get_candidates("planner", phi_detected=False)
    if not cands:
        return
    out, _note = r._apply_router_mode_filter(cands, "copilot")
    assert out
    for spec in out:
        assert spec.benchmark_category not in COPILOT_EXCLUDED_THOMPSON_BENCHMARK_CATEGORIES


def test_apply_router_mode_none_same_as_agentic():
    r = ModelRouter()
    cands = r._get_candidates("planner", phi_detected=False)
    if not cands:
        return
    a, _ = r._apply_router_mode_filter(cands, None)
    assert len(a) == len(cands)
