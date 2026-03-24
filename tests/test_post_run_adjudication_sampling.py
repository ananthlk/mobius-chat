"""MOBIUS_POST_RUN_ADJUDICATE_EVERY_N sampling for post-run LLM adjudication."""
from __future__ import annotations

import uuid

import pytest

from app.services.post_run_adjudication import _should_post_run_adjudicate_this_turn


@pytest.fixture(autouse=True)
def _clear_every_n(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MOBIUS_POST_RUN_ADJUDICATE_EVERY_N", raising=False)


def test_every_n_default_runs_every_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MOBIUS_POST_RUN_ADJUDICATE_EVERY_N", raising=False)
    cid = str(uuid.uuid4())
    assert _should_post_run_adjudicate_this_turn(cid) is True


def test_every_n_invalid_falls_back_to_every_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MOBIUS_POST_RUN_ADJUDICATE_EVERY_N", "not-a-number")
    cid = str(uuid.uuid4())
    assert _should_post_run_adjudicate_this_turn(cid) is True


def _uuid_str_with_int_mod(n: int, remainder: int) -> str:
    """Build a UUID whose int % n == remainder (for deterministic sampling tests)."""
    assert 0 <= remainder < n
    for _ in range(10000):
        u = uuid.uuid4()
        if u.int % n == remainder:
            return str(u)
    raise RuntimeError("could not sample UUID")  # pragma: no cover


def test_every_n_twenty_matches_uuid_int_mod(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MOBIUS_POST_RUN_ADJUDICATE_EVERY_N", "20")
    assert _should_post_run_adjudicate_this_turn(_uuid_str_with_int_mod(20, 0)) is True
    assert _should_post_run_adjudicate_this_turn(_uuid_str_with_int_mod(20, 1)) is False


def test_bad_correlation_id_fails_open(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MOBIUS_POST_RUN_ADJUDICATE_EVERY_N", "999")
    assert _should_post_run_adjudicate_this_turn("not-a-uuid") is True
