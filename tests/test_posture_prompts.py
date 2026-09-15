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


def test_communicate_has_TWO_prompts_and_the_failure_one_asks_for_no_structure():
    """🔴 The Deterministic UX seat's reason is mechanical, not stylistic.

    Their formatter treats a thin-evidence turn as a REFUSAL and will not fill
    it — a cited bullet list beside an ungrounded answer is the most
    confident-looking thing on a screen. So a failure answer that arrives
    SHAPED like a success fights the gate that keeps it honest.

    Load-bearing since v2's fallback to v1 was removed: a failed turn publishes
    its own words now, so those words are the product.
    """
    from app.pipeline.v2.posture_prompts import COMMUNICATE, COMMUNICATE_FAILURE

    assert COMMUNICATE and COMMUNICATE_FAILURE
    assert COMMUNICATE is not COMMUNICATE_FAILURE

    # 🔴 INTENT, NOT VOCABULARY. My first version banned the WORD "label" and
    # failed on "Do not write labels" — a prohibition, which is the opposite of
    # what it was checking for. A gate that matches a word matches the denial
    # of that word too.
    # 🔴 NORMALISE WHITESPACE BEFORE MATCHING. The prompt wraps as
    # "Do not write\nlabels, lines or sections", so a substring spanning the
    # break does not match. Second time in one test: first I matched a word
    # and caught its denial, then I matched a phrase and caught a line break.
    # A prompt is prose — any gate over it has to read it as prose.
    f = " ".join(COMMUNICATE_FAILURE.lower().split())
    assert "do not write labels" in f, (
        "the failure prompt must explicitly forbid structure, not merely omit "
        "asking for it — omission leaves the model to guess"
    )
    # and it must not carry the success prompt's positive demands
    for demand in ("one assertion per line", "a peer set", "mark what things are"):
        assert demand not in f, (
            f"the failure prompt asks for {demand!r} — structure is what makes "
            f"a failure read as confident"
        )


def test_the_four_failures_are_named_separately():
    """could_not_run / no_sources / unobservable / unsupported are tracked
    apart everywhere else in this system and collapse in the ANSWER into
    "I could not find...", which is true of all four and useful for none."""
    from app.pipeline.v2.posture_prompts import COMMUNICATE_FAILURE
    t = COMMUNICATE_FAILURE.lower()
    for distinction in ("did not look", "not there", "could not check",
                        "did not hold up"):
        assert distinction in t, f"the failure prompt collapses {distinction!r}"


def test_the_answer_prompt_never_names_a_FORMAT():
    """Since 26facd1 `format` is discarded on every v2 section and re-decided
    from content, so a shape named in the prompt is dead on arrival. Measured
    why it must be: the model called a six-item set "stats" into a renderer
    that draws four tiles, losing two rows silently every time."""
    from app.pipeline.v2.posture_prompts import COMMUNICATE
    t = COMMUNICATE.lower()
    for fmt in ("use a table", "use bullets", "bullet points", "heading"):
        assert fmt not in t, f"the prompt names a format ({fmt!r})"
