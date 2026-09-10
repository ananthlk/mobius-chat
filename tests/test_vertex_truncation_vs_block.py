"""MAX_TOKENS is not a safety block.

`response.text` raises ValueError("...has no parts...") whenever a Vertex
candidate carries no visible content — SAFETY, MAX_TOKENS and RECITATION alike —
and the SDK's own message guesses "likely blocked by the safety filters" in
every case. `finish_reason` distinguishes them (1 STOP, 2 MAX_TOKENS, 3 SAFETY,
4 RECITATION) and was ALREADY being read at the raise site — but only to
decorate the message, never to decide the error.

The harm chain that produced, reported by the deep-research seat 2026-09-10
after a preflight with max_tokens=16 against a thinking model:

  finish_reason=2 (MAX_TOKENS)
    -> raised as VertexBlockedError("blocked")        <- wrong cause
    -> react catches it and retries CONDENSED         <- cannot possibly help;
                                                         shrinking the input does
                                                         not restore output budget
    -> error_emit matches "vertexblockederror"
    -> user is told "blocked by a content safety rule. Try rephrasing."

A wrong cause is bad. A wrong cause that tells the user their content was
refused, for our own token budget, is worse.
"""
from __future__ import annotations

import pytest


def test_truncation_and_block_are_distinct_types():
    from app.services.llm_provider import VertexBlockedError, VertexTruncatedError
    assert VertexTruncatedError is not VertexBlockedError
    # NOT a subclass: react catches VertexBlockedError to retry condensed, and a
    # truncation must not be swept into that retry.
    assert not issubclass(VertexTruncatedError, VertexBlockedError)


def test_truncation_maps_to_token_budget_not_refusal():
    from app.communication.error_emit import classify_exception
    from app.services.llm_provider import VertexTruncatedError
    env = classify_exception(VertexTruncatedError(
        "vertex response truncated: the model exhausted its output budget "
        "(finish_reason=2, max_tokens=16) before emitting a visible token."))
    assert env.error_code == "token_budget"
    msg = env.user_facing_message.lower()
    assert "safety" not in msg, "a token-budget limit was described as a safety block"
    assert "rephras" not in msg, "user told to rephrase for OUR budget limit"
    assert "limit on our side" in msg


def test_a_real_safety_block_still_maps_to_refusal():
    """The guard must not have been bought by breaking the case that was right."""
    from app.communication.error_emit import classify_exception
    from app.services.llm_provider import VertexBlockedError
    env = classify_exception(VertexBlockedError("vertex response blocked (finish_reason=3): ..."))
    assert env.error_code == "refusal"
    assert "safety" in env.user_facing_message.lower()


def test_react_does_not_condense_retry_on_truncation():
    """Condensing the INPUT cannot restore an exhausted OUTPUT budget; it spends
    another full call to fail identically."""
    import inspect
    from app.pipeline.react import prompts
    src = inspect.getsource(prompts)
    i_trunc = src.index("except VertexTruncatedError:")
    i_block = src.index("except VertexBlockedError:")
    assert i_trunc < i_block, "truncation must be caught BEFORE the blocked handler"
    between = src[i_trunc:i_block]
    assert "raise" in between, "truncation must re-raise, not fall through to the retry"
    assert "condensed_prompt" not in between


def test_error_code_is_one_the_contract_allows():
    """`truncated` was the natural name and is NOT in the contract's Literal set;
    token_budget is, and names this exactly."""
    from mobius_contracts.envelopes import ErrorCode
    import typing
    allowed = typing.get_args(ErrorCode)   # ErrorCode is a flat Literal[...]
    assert "token_budget" in allowed
    assert "truncated" not in allowed


# ── the DECISION itself, not the plumbing around it ──────────────────
# The first version of this file tested the exception types, the error mapping
# and the react handler — and a mutation that deleted the raise-site branch
# entirely left all of them GREEN. Testing around a decision is not testing it.

@pytest.mark.parametrize("finish_reason", ["2", 2, "MAX_TOKENS", "FinishReason.MAX_TOKENS"])
def test_max_tokens_yields_truncation(finish_reason):
    from app.services.llm_provider import vertex_no_content_error, VertexTruncatedError
    err = vertex_no_content_error(finish_reason, 16, ValueError("no parts"))
    assert isinstance(err, VertexTruncatedError)
    assert "max_tokens=16" in str(err)
    assert "condensing the prompt will not help" in str(err).lower()


@pytest.mark.parametrize("finish_reason", ["3", "SAFETY", "FinishReason.SAFETY",
                                           "4", "RECITATION", None, "", "unknown"])
def test_everything_else_yields_blocked(finish_reason):
    """Only MAX_TOKENS is reclassified. A genuine safety stop, a recitation stop
    and an unknown reason all keep the conservative 'blocked' reading — the fix
    narrows a wrong claim, it does not widen a permissive one."""
    from app.services.llm_provider import vertex_no_content_error, VertexBlockedError
    assert isinstance(vertex_no_content_error(finish_reason, 16, ValueError("x")),
                      VertexBlockedError)
