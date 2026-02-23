"""Tests for continuity checks: end pursuit, user-provided context."""
import pytest

from app.state.continuity_checks import (
    user_wants_to_end_pursuit,
    extract_user_provided_context,
)


def test_user_wants_to_end_pursuit() -> None:
    assert user_wants_to_end_pursuit("never mind") is True
    assert user_wants_to_end_pursuit("That's enough, thanks") is True
    assert user_wants_to_end_pursuit("Stop") is True
    assert user_wants_to_end_pursuit("I'm done") is True
    assert user_wants_to_end_pursuit("no thanks") is True
    assert user_wants_to_end_pursuit("forget it") is True
    assert user_wants_to_end_pursuit("don't bother") is True
    assert user_wants_to_end_pursuit("") is False
    assert user_wants_to_end_pursuit("Can you find the ICD code?") is False
    assert user_wants_to_end_pursuit("Here's what I found") is False


def test_extract_user_provided_context() -> None:
    assert extract_user_provided_context("never mind") is None
    assert extract_user_provided_context("Here's what I found: prior auth is required") is not None
    assert extract_user_provided_context("I found that Sunshine Health requires prior auth") is not None
    assert extract_user_provided_context("For your reference, the ICD code is Z55.9") is not None
    assert "https://example.com/doc" in (extract_user_provided_context("Check https://example.com/doc for details") or "")
    assert extract_user_provided_context("What is the prior auth requirement?") is None
