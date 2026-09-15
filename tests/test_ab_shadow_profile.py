"""A fork must be able to vary the MODEL, not only the orchestrator.

The fork copies the served turn's payload to the shadow, so both arms
inherited one `model_profile`. That made a fork structurally unable to compare
models -- and the fork is the only comparison shape that holds the question,
the seconds and the corpus constant, which is what a shared dev instance
requires (measured 2026-09-14: a 3.5x latency spread on an IDENTICAL query
shape, caused by an unrelated maintenance sweep, which is larger than any
model difference we have measured).
"""
import inspect

from app.api import chat as chat_api


def test_the_field_exists_and_is_gated_like_the_other_harness_pins():
    assert "ab_shadow_profile" in chat_api.ChatRequest.model_fields
    src = inspect.getsource(chat_api)
    i = src.index("ab_shadow_profile: str | None")
    doc = src[i:i + 1400]
    assert "MOBIUS_V2_AB_FORK" in doc, (
        "a caller that can pick its model per question can return a selection "
        "dressed as a comparison — it needs the same gate as ab_arm"
    )


def test_the_shadow_profile_is_applied_to_the_SHADOW_not_the_served_turn():
    """🔴 The served turn is a real answer to a real person. A harness may
    never change what they receive."""
    src = inspect.getsource(chat_api)
    fork = src[src.index('_p["ab_shadow"] = True'):]
    apply_idx = fork.index("ab_shadow_profile")
    assert 'model_profile' in fork[:apply_idx + 400]
    # It must be assigned into the SHADOW copy `_p`, never into `payload`.
    window = fork[apply_idx:apply_idx + 400]
    assert '_p["model_profile"]' in window, \
        "the shadow profile must be written to the shadow payload copy"
    assert 'payload["model_profile"]' not in window, \
        "never rewrite the served turn's model"


def test_absent_shadow_profile_leaves_the_fork_unchanged():
    """Default behaviour must be bit-identical: both arms inherit one profile,
    which is what every existing fork caller expects."""
    src = inspect.getsource(chat_api)
    fork = src[src.index('_p["ab_shadow"] = True'):]
    i = fork.index("ab_shadow_profile")
    assert "if body.ab_shadow_profile:" in fork[max(0, i - 200):i + 80], \
        "the shadow profile must be opt-in, not a default"
