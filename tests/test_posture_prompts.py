"""One prompt per posture, and the round must be told which one it is in.

All six postures previously collapsed onto v1's three agent roles
(react_explore / synthesize / draft), so three of the six distinctions died at
the prompt boundary — in the one thing the governor actually decides.

These assert the PROPERTY (every posture is accounted for, the block reaches
the prompt, and the base contract is not restated) rather than any wording,
which is about to be tuned now that it is pluggable.
"""
import inspect

from app.pipeline.v2 import loop as L
from app.pipeline.v2.posture import Posture
from app.pipeline.v2.posture_prompts import POSTURE_PROMPTS


def test_every_posture_is_accounted_for():
    """🔴 Accounted for, not necessarily written. COMMUNICATE is deliberately
    None pending the Deterministic UX seat — a MISSING key is an oversight, an
    explicit None is a decision, and the two must not look alike."""
    assert set(POSTURE_PROMPTS) == set(Posture), (
        f"postures without an entry: {set(Posture) - set(POSTURE_PROMPTS)}"
    )
    written = [p for p, t in POSTURE_PROMPTS.items() if t]
    assert len(written) >= 5, "fewer prompts than postures that have one"


def test_the_posture_block_is_applied_at_a_single_exit():
    """The base builder has THREE returns. Appending the block at each is
    three places to forget it, and a prompt that silently loses the posture
    means the governor decided something the model never learned."""
    src = inspect.getsource(L._v2_system_prompt)
    assert "_v2_system_prompt_base(" in src, "the wrapper does not call the base"
    # ONE lookup, not one mention — the import names it too. What matters is
    # that the block is resolved in a single place, so no exit can resolve a
    # different one.
    assert src.count("POSTURE_PROMPTS.get(") == 1, (
        "the posture block is resolved in more than one place"
    )
    # every return in the wrapper carries the provenance key
    assert src.count("posture_block") >= 2, (
        "not every exit records whether a posture block was applied"
    )


def test_a_missing_block_is_REPORTED_not_silently_skipped():
    """COMMUNICATE has no block yet. That must show up as provenance saying
    so, never as a prompt that looks complete."""
    src = inspect.getsource(L._v2_system_prompt)
    i = src.index("if not block:")
    assert '"posture_block": None' in src[i:i + 400], (
        "a missing block must be recorded as None, not omitted"
    )


def test_the_posture_prompts_do_not_restate_the_shared_contract():
    """🔴 The base composition already carries response shape, format rules,
    the tool manifest and user preferences. A posture block that repeats them
    becomes a second author of the contract and drifts from it — which is how
    the answer stops being parseable while every test still passes."""
    banned = ("FORMAT RULES", "NEVER write prose outside the JSON",
              "is_complete", '"sources"')
    for posture, text in POSTURE_PROMPTS.items():
        if not text:
            continue
        for phrase in banned:
            assert phrase not in text, (
                f"{posture.value} restates the shared contract ({phrase!r}) — "
                f"the composition already carries it"
            )
