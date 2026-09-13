"""The communicate round asks for the answer the user reads.

Ananth, 2026-09-13: "this feels too short and succinct without facts.. this
feels like the summary, in which case are we even passing the expanded answer
to the integrate and front end".

Measured, cid 3fc33ed1: round 1 answer 1012 chars; round 2 -- the communicate
round -- 847. v1's REACT_FORMAT_RULES_TEXT caps "answer" at 2-4 bullets of
10-25 words, and that cap beat the communicate role because the cap is in the
system prompt and the role is in the user prompt.
"""
import re

from app.pipeline.v2 import prompts


class _Ctx:
    def __init__(self, version="v2", communicates=False, finalising=False):
        self.orchestrator_version = version
        self._v2_round_communicates = communicates
        self._v2_finalising = finalising


def test_communicate_round_supersedes_the_bullet_cap():
    out = prompts.system_suffix(_Ctx(communicates=True))
    assert "THAT CAP DOES NOT APPLY THIS ROUND" in out
    assert "ANSWER EVERY PART THEY ASKED" in out


def test_non_communicate_round_keeps_the_default():
    out = prompts.system_suffix(_Ctx(communicates=False))
    assert "THAT CAP DOES NOT APPLY THIS ROUND" not in out
    # the standing v2 ask is still there
    assert '"facts"' in out


def test_v1_never_sees_any_of_it():
    """v1 stays pure for A/B -- including prompts."""
    for c in (_Ctx("v1", communicates=True, finalising=True),
              _Ctx("v1"), _Ctx(None)):
        assert prompts.system_suffix(c) == ""


def test_prompts_does_not_read_the_flag_that_is_cleared_first():
    """THE ORDERING TRAP, asserted as a property of the source.

    _v2_finalising is cleared at react_loop:5579; system_suffix is called at
    :5968. A gate here reading _v2_finalising sees False on the exact round it
    exists for and fires never -- silently, because a missing prompt section
    looks like a model that chose to be brief. The capture is taken while the
    value is still true and is read under a different name; this fails if
    anyone reintroduces the direct read.
    """
    src = open(prompts.__file__).read()
    code = "\n".join(ln for ln in src.splitlines()
                     if not ln.lstrip().startswith("#"))
    assert "_v2_finalising" not in code, (
        "system_suffix must not read _v2_finalising -- it is already cleared "
        "by the time this module runs")
    assert "_v2_round_communicates" in code


def test_user_preferences_still_win_on_length():
    """Superseding the DEFAULT must not silently override the person."""
    out = prompts.system_suffix(_Ctx(communicates=True))
    assert re.search(r"USER PREFERENCES still take final authority", out)
