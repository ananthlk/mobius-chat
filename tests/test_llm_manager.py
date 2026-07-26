"""Unit tests for the LLMManager components (PromptManager / ConfigManager /
CallManager). Pure unit tests — the DB is stubbed via the _fetch seams, so no
`integration` marker. Spec: docs/SPEC_LLM_MANAGER.md.
"""
from __future__ import annotations

import random

import pytest

from app.services.call_manager import CalibrationSnapshot, CallManager, InvokeResult
from app.services.config_manager import ConfigManager, _apply_context_overrides
from app.services.llm_manager_types import TAG_AXES, EngagementContext, LLMConfig
from app.services.prompt_manager import PromptManager, PromptTemplate


def _ctx(**over):
    base = dict(voice="general", format="prose", autonomy="copilot",
               intent="retrieve", caller_mode="real_time")
    base.update(over)
    return EngagementContext(**base)


def _tmpl(id, variant_id, tags, body="B {{ q }}", weight=1.0, role="system"):
    return PromptTemplate(id, "integrator", role, variant_id, tags, 1, body, weight)


def _pm_with(templates):
    pm = PromptManager(rng=random.Random(0))

    async def rows(_mk):
        return [
            {"id": t.id, "module_key": t.module_key, "role": t.role,
             "variant_id": t.variant_id, "variant_tags": t.variant_tags,
             "version": t.version, "template_body": t.template_body, "weight": t.weight}
            for t in templates
        ]

    pm._fetch_rows = rows
    return pm


# ── PromptManager: tag-match tiers ───────────────────────────────────────────

def test_match_exact_beats_partial_and_default():
    ctx = _ctx(voice="clinical")
    default = _tmpl(1, "default", {})
    partial = _tmpl(2, "clin", {"voice": "clinical"})
    exact = _tmpl(3, "exact", {a: getattr(ctx, a) for a in TAG_AXES})
    tier = PromptManager._match_tier([default, partial, exact], ctx)
    assert [t.id for t in tier] == [3]


def test_match_partial_most_specific_wins():
    ctx = _ctx(voice="clinical", format="table")
    one = _tmpl(2, "v1", {"voice": "clinical"})
    two = _tmpl(3, "v2", {"voice": "clinical", "format": "table"})
    tier = PromptManager._match_tier([one, two], ctx)
    assert [t.id for t in tier] == [3]  # 2-axis match beats 1-axis


def test_match_falls_through_to_default():
    ctx = _ctx(voice="clinical")
    default = _tmpl(1, "default", {})
    mismatch = _tmpl(2, "exec", {"voice": "executive"})
    tier = PromptManager._match_tier([default, mismatch], ctx)
    assert [t.id for t in tier] == [1]


def test_unknown_tag_value_never_raises():
    ctx = _ctx(voice="does-not-exist")
    default = _tmpl(1, "default", {})
    tier = PromptManager._match_tier([default], ctx)  # must not raise
    assert [t.id for t in tier] == [1]


# ── PromptManager: A/B + render ──────────────────────────────────────────────

def test_ab_disallowed_serves_default():
    pm = PromptManager(rng=random.Random(0))
    cands = [_tmpl(1, "default", {}), _tmpl(2, "b", {})]
    assert pm._pick(cands, ab_allowed=False).variant_id == "default"


def test_ab_weighted_distribution():
    pm = PromptManager(rng=random.Random(0))
    cands = [_tmpl(1, "a", {}, weight=1.0), _tmpl(2, "b", {}, weight=3.0)]
    counts = {1: 0, 2: 0}
    for _ in range(2000):
        counts[pm._pick(cands, ab_allowed=True).id] += 1
    ratio = counts[2] / counts[1]
    assert 2.3 < ratio < 3.7  # ~3:1


async def test_render_autoescape_off():
    pm = _pm_with([_tmpl(1, "default", {}, body="Q: {{ q }}")])
    rp = await pm.render("integrator", _ctx(), {"q": 'A & "B" <c>'})
    assert rp.system_prompt == 'Q: A & "B" <c>'  # not entity-escaped
    assert rp.variant_id == "default" and rp.template_id == 1


async def test_render_returns_system_and_user_pair():
    pm = _pm_with([
        _tmpl(1, "default", {}, body="SYS {{ q }}", role="system"),
        _tmpl(2, "default", {}, body="USR {{ q }}", role="user"),
    ])
    rp = await pm.render("integrator", _ctx(), {"q": "x"})
    assert rp.system_prompt == "SYS x" and rp.user_prompt == "USR x"
    assert rp.template_id == 1  # tracked identity = system (experimental) variant


async def test_render_no_templates_raises():
    pm = _pm_with([])
    with pytest.raises(LookupError):
        await pm.render("integrator", _ctx(), {})


async def test_default_template_and_frozen_render():
    pm = _pm_with([_tmpl(1, "default", {}, body="D {{ q }}"), _tmpl(2, "b", {}, body="B {{ q }}")])
    default = await pm.default_template("integrator")
    assert default.variant_id == "default"
    rp = await pm.render("integrator", _ctx(), {"q": "x"}, frozen_template=default)
    assert rp.template_id == 1


# ── ConfigManager ────────────────────────────────────────────────────────────

def test_clinical_voice_lowers_temperature():
    base = LLMConfig("integrator", None, 0.7, 1000, None, None, 30000, "fb")
    assert _apply_context_overrides(base, _ctx(voice="clinical")).temperature == 0.1
    assert _apply_context_overrides(base, _ctx(voice="general")).temperature == 0.7


def test_default_config_lets_bandit_own_model():
    assert ConfigManager._default_config("x").model_id is None


# ── CallManager: derivation + DEP-1 ──────────────────────────────────────────

def _callmgr(model_id=None, invoke=None):
    pm = _pm_with([_tmpl(1, "default", {}), _tmpl(2, "b", {})])
    cm = ConfigManager()

    async def row(mk):
        return {"module_key": mk, "model_id": model_id, "temperature": 0.2,
                "max_tokens": 500, "top_p": None, "stop_sequences": None,
                "timeout_ms": 30000, "fallback_model": "fb"}

    cm._fetch_row = row
    return CallManager(pm, cm, invoke_fn=invoke, current_best_fn=lambda _mk: "best-model")


@pytest.mark.parametrize("axis,cfg_pin,exp_forced,exp_pinned,exp_ab", [
    ("none", None, None, False, False),
    ("model", None, None, False, False),
    ("prompt", None, "best-model", True, True),
    ("none", "pin-x", "pin-x", True, False),
    ("prompt", "pin-x", "pin-x", True, True),
])
async def test_derivation_truth_table(axis, cfg_pin, exp_forced, exp_pinned, exp_ab):
    captured = {}

    async def invoke(plan):
        captured["plan"] = plan
        return InvokeResult("ANS", plan.forced_model or "bandit", "c1", 42, 10, 5, 0.001, False)

    mgr = _callmgr(model_id=cfg_pin, invoke=invoke)
    resp = await mgr.call("integrator", _ctx(), template_vars={"q": "hi"},
                          turn_id="corr-1", experiment_axis=axis)
    p = captured["plan"]
    assert p.forced_model == exp_forced
    assert p.is_hard_pinned is exp_pinned
    assert (axis == "prompt") is exp_ab
    assert resp.tokens_used == 15  # 10 + 5


async def test_temperature_flows_into_plan_for_logging():
    """AC-21: the resolved temperature reaches the invoke layer so it can be
    logged per call. Config temp 0.2, general voice → no override → 0.2."""
    captured = {}

    async def invoke(plan):
        captured["plan"] = plan
        return InvokeResult("A", "m", "c", 1, 1, 1, 0.0, False)

    mgr = _callmgr(invoke=invoke)
    await mgr.call("integrator", _ctx(), template_vars={"q": "x"}, turn_id="t1")
    assert captured["plan"].temperature == 0.2


async def test_calibration_snapshot_freezes_temp_and_hard_pins():
    """AC-22 + C2b: calibration snapshot pins (template, temperature); the turn
    is hard-pinned and serves the default variant."""
    captured = {}

    async def invoke(plan):
        captured["plan"] = plan
        return InvokeResult("A", "m", "c", 1, 1, 1, 0.0, False)

    mgr = _callmgr(invoke=invoke)  # config temp 0.2
    snap = await mgr.calibration_snapshot("integrator", _ctx())
    assert isinstance(snap, CalibrationSnapshot)
    assert snap.temperature == 0.2 and snap.template.variant_id == "default"
    await mgr.call("integrator", _ctx(), template_vars={"q": "x"}, turn_id="t2", snapshot=snap)
    p = captured["plan"]
    assert p.temperature == 0.2          # frozen temperature used
    assert p.is_hard_pinned is True       # C2b: calibration hard-pinned
    assert p.variant_id == "default"      # frozen default template


async def test_dep1_requires_turn_id():
    mgr = _callmgr()
    with pytest.raises(ValueError):
        await mgr.call("integrator", _ctx(), template_vars={"q": "x"}, turn_id="")


async def test_typed_error_contract():
    """§10: typed errors that subclass the builtin they logically are, so
    existing except-clauses keep working while callers can branch specifically."""
    from app.services.llm_manager_errors import (
        LLMManagerError, PromptNotFoundError, TurnIdRequiredError,
    )
    assert issubclass(TurnIdRequiredError, ValueError)
    assert issubclass(PromptNotFoundError, LookupError)
    assert issubclass(TurnIdRequiredError, LLMManagerError)

    mgr = _callmgr()
    with pytest.raises(TurnIdRequiredError):
        await mgr.call("integrator", _ctx(), template_vars={}, turn_id="")

    with pytest.raises(PromptNotFoundError):
        await _pm_with([]).render("integrator", _ctx(), {})


async def test_b2f_emitted_on_fallback():
    events = []

    async def invoke(plan):
        return InvokeResult("A", "fb-model", "c2", 10, 1, 1, 0.0, True, fallback_from="primary")

    pm = _pm_with([_tmpl(1, "default", {})])
    cm = ConfigManager()

    async def row(mk):
        return {"module_key": mk, "model_id": None, "temperature": 0.2, "max_tokens": 500,
                "top_p": None, "stop_sequences": None, "timeout_ms": 30000, "fallback_model": "fb"}

    cm._fetch_row = row
    mgr = CallManager(pm, cm, invoke_fn=invoke, emit_fn=lambda e: events.append(e))
    await mgr.call("integrator", _ctx(), template_vars={}, turn_id="corr-9")
    assert len(events) == 1 and events[0]["event"] == "model_fallback"
    assert events[0]["fallback_from"] == "primary"
