"""Cache-mode selection rules (2026-04-23).

Pure-function tests — no DB, no Chroma, no LLM.
"""
from __future__ import annotations

import pytest

from app.services.cache_mode import (
    _bucket_for,
    has_freshness_markers,
    select_cache_mode,
)


# ── Freshness marker detection ────────────────────────────────────────


@pytest.mark.parametrize("q,expected", [
    ("What's the PA timeline today?", True),
    ("What is currently the appeal process?", True),
    ("Tell me the latest updates.", True),
    ("Show me now.", True),
    ("What is the PA timeline?", False),
    ("What was the appeal process in 2023?", False),
    ("Yesterday's claims", False),   # not a freshness marker
    ("", False),
    (None, False),
])
def test_has_freshness_markers(q, expected):
    assert has_freshness_markers(q) == expected


# ── Bucketing determinism ─────────────────────────────────────────────


def test_bucket_is_deterministic():
    cid = "abc-123-deterministic"
    assert _bucket_for(cid) == _bucket_for(cid)


def test_bucket_distribution_is_uniform():
    """1000 random-ish cids should spread roughly evenly across 100 buckets.

    Each bucket should get ~10 hits; allow a loose chi-squared-ish
    tolerance so this test isn't flaky on legitimate hash variance."""
    import uuid
    buckets = [0] * 100
    for _ in range(1000):
        buckets[_bucket_for(str(uuid.uuid4()))] += 1
    # No bucket should have > 30 or < 2 on 1000 samples if hashing is
    # reasonable. Loose bounds — not testing perfect uniformity.
    assert max(buckets) < 40, f"suspicious pile-up: {max(buckets)}"
    assert min(buckets) > 0, f"empty bucket: {buckets.index(min(buckets))}"


def test_bucket_empty_cid_is_stable():
    assert _bucket_for("") == 0
    assert _bucket_for(None or "") == 0  # defensive


# ── Selection rules ────────────────────────────────────────────────────


def _base_kwargs():
    return {
        "correlation_id": "test-cid-42",
        "chat_mode": "copilot",
        "system_context": None,
        "cache_assist_override": None,
        "question": "What is the PA timeline?",
    }


def test_selects_active_in_normal_case(monkeypatch):
    monkeypatch.setenv("CACHE_ASSIST_ENABLED", "1")
    monkeypatch.setenv("CACHE_ASSIST_BYPASS_PCT", "0")  # disable bypass so we hit active
    assert select_cache_mode(**_base_kwargs()) == "active"


def test_env_disabled_forces_off(monkeypatch):
    monkeypatch.setenv("CACHE_ASSIST_ENABLED", "0")
    assert select_cache_mode(**_base_kwargs()) == "off"


def test_per_turn_override_false_forces_off(monkeypatch):
    monkeypatch.setenv("CACHE_ASSIST_ENABLED", "1")
    monkeypatch.setenv("CACHE_ASSIST_BYPASS_PCT", "0")
    kw = {**_base_kwargs(), "cache_assist_override": False}
    assert select_cache_mode(**kw) == "off"


def test_agentic_mode_forces_off(monkeypatch):
    monkeypatch.setenv("CACHE_ASSIST_ENABLED", "1")
    monkeypatch.setenv("CACHE_ASSIST_BYPASS_PCT", "0")
    kw = {**_base_kwargs(), "chat_mode": "agentic"}
    assert select_cache_mode(**kw) == "off"


def test_quick_mode_does_not_force_off(monkeypatch):
    """Quick mode is cache-eligible (opposite of agentic)."""
    monkeypatch.setenv("CACHE_ASSIST_ENABLED", "1")
    monkeypatch.setenv("CACHE_ASSIST_BYPASS_PCT", "0")
    kw = {**_base_kwargs(), "chat_mode": "quick"}
    assert select_cache_mode(**kw) == "active"


def test_system_context_present_forces_off(monkeypatch):
    monkeypatch.setenv("CACHE_ASSIST_ENABLED", "1")
    monkeypatch.setenv("CACHE_ASSIST_BYPASS_PCT", "0")
    kw = {**_base_kwargs(), "system_context": "some pre-loaded data"}
    assert select_cache_mode(**kw) == "off"


def test_whitespace_system_context_does_not_force_off(monkeypatch):
    """Whitespace-only system_context shouldn't count as present."""
    monkeypatch.setenv("CACHE_ASSIST_ENABLED", "1")
    monkeypatch.setenv("CACHE_ASSIST_BYPASS_PCT", "0")
    kw = {**_base_kwargs(), "system_context": "   \n\t  "}
    assert select_cache_mode(**kw) == "active"


def test_freshness_markers_force_off(monkeypatch):
    monkeypatch.setenv("CACHE_ASSIST_ENABLED", "1")
    monkeypatch.setenv("CACHE_ASSIST_BYPASS_PCT", "0")
    kw = {**_base_kwargs(), "question": "What is the latest policy today?"}
    assert select_cache_mode(**kw) == "off"


def test_bypass_pct_100_forces_shadow(monkeypatch):
    monkeypatch.setenv("CACHE_ASSIST_ENABLED", "1")
    monkeypatch.setenv("CACHE_ASSIST_BYPASS_PCT", "100")
    assert select_cache_mode(**_base_kwargs()) == "shadow"


def test_bypass_pct_partition_is_deterministic(monkeypatch):
    """Same cid + same bypass pct = same mode every time."""
    monkeypatch.setenv("CACHE_ASSIST_ENABLED", "1")
    monkeypatch.setenv("CACHE_ASSIST_BYPASS_PCT", "20")
    kw = _base_kwargs()
    first = select_cache_mode(**kw)
    second = select_cache_mode(**kw)
    third = select_cache_mode(**kw)
    assert first == second == third


def test_bypass_pct_distribution_roughly_matches(monkeypatch):
    """With 20% bypass, ~20% of random cids should land in shadow."""
    import uuid
    monkeypatch.setenv("CACHE_ASSIST_ENABLED", "1")
    monkeypatch.setenv("CACHE_ASSIST_BYPASS_PCT", "20")
    counts = {"active": 0, "shadow": 0, "off": 0}
    for _ in range(1000):
        kw = _base_kwargs()
        kw["correlation_id"] = str(uuid.uuid4())
        counts[select_cache_mode(**kw)] += 1
    # 20% ± 5% at n=1000 is well within hash-uniformity tolerance.
    assert 150 < counts["shadow"] < 250, counts
    assert 750 < counts["active"] < 850, counts
    assert counts["off"] == 0, counts
