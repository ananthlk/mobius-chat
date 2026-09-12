"""One tool call must not mark every open gap attempted.

THE LIVE DEFECT (2026-09-12, cid 4dc1d236): a single "Molina care management
philosophy" search was appended to all three payer gaps. exhausted() then saw
three attempted-and-empty gaps and returned CAPABILITY -- terminal, no
continuation -- for two payers nobody had searched, with 113 Sunshine chunks
in the corpus. The user saw "not available" for a lookup that never ran.

These tests assert the PROPERTY (an unaimed call cannot exhaust a gap), not
the fingerprint of that question.
"""
import inspect

from app.pipeline.v2.posture import (
    Attempt, Budget, ExitMode, Gap, RoundState, exit_mode, targeted_attempts,
)
from app.pipeline.v2.shadow import _targeting

PAYERS = ["Molina care management philosophy",
          "Sunshine Health care management philosophy",
          "UnitedHealthcare care management philosophy"]


def _gap(text, attempts):
    return Gap(gap_id=text[:6], text=text, opened_round=1,
               attempted_by=tuple(attempts))


def _state(gaps):
    """Bound to the REAL signature: a stub built from a guessed shape has
    passed while the live call raised, three times on this module."""
    sig = inspect.signature(RoundState)
    kw = dict(round_index=2, open_gaps=tuple(gaps),
              gaps_open_history=(len(gaps),),
              budget=Budget(remaining_s=88.0, remaining_c=3.0),
              next_round_cost_s=10.4, acting_cost_s=10.0, validate_cost_s=9.6)
    sig.bind(**{k: v for k, v in kw.items() if k in sig.parameters})
    return RoundState(**{k: v for k, v in kw.items() if k in sig.parameters})


def test_sibling_query_does_not_target_other_gaps():
    aimed = _targeting(PAYERS, "Molina Healthcare care management philosophy")
    assert aimed[PAYERS[0]] is True
    assert aimed[PAYERS[1]] is False and aimed[PAYERS[2]] is False


def test_query_naming_all_payers_targets_all():
    aimed = _targeting(PAYERS, "care management philosophy for Molina, "
                               "Sunshine Health and UnitedHealthcare")
    assert all(aimed[g] for g in PAYERS), "a query naming every gap aims at every gap"


def test_single_gap_is_always_targeted():
    only = ["Molina care management philosophy"]
    assert _targeting(only, "completely unrelated text") == {only[0]: True}


def test_unaimed_attempt_cannot_exhaust_a_gap():
    unaimed = Attempt(round_index=1, tool="rag", query="molina...",
                      returned_payload=False, targeted=False)
    g = _gap(PAYERS[1], [unaimed])
    assert targeted_attempts(g) == (), "an unaimed attempt is not a lever spent"


def test_exit_is_budget_not_capability_when_siblings_were_never_searched():
    """THE REGRESSION. Reintroducing the defect must flip this to CAPABILITY."""
    empty = dict(returned_payload=False, tool="rag", round_index=1)
    gaps = [_gap(PAYERS[0], [Attempt(query="molina", targeted=True, **empty)]),
            _gap(PAYERS[1], [Attempt(query="molina", targeted=False, **empty)]),
            _gap(PAYERS[2], [Attempt(query="molina", targeted=False, **empty)])]
    assert exit_mode(_state(gaps)) is ExitMode.BUDGET

    # MUTATION: attribute the one call to every gap, as the old code did.
    as_before = [_gap(g.text, [Attempt(query="molina", targeted=True, **empty)])
                 for g in gaps]
    assert exit_mode(_state(as_before)) is ExitMode.CAPABILITY, (
        "this assertion IS the defect; if it stops holding, the test above "
        "has stopped proving anything"
    )


def test_targeted_survives_the_write_read_round_trip():
    """A field the decision reads but the record omits is undebuggable.

    The live record showed three gaps each carrying the same one-payer query
    with no way to see why only one of them counted. Write and read are
    asserted TOGETHER: a producer with no consumer is the defect this repo
    has found a dozen times.
    """
    from app.pipeline.v2 import ledger
    from app.pipeline.v2.posture import explain, select

    g = _gap(PAYERS[1], [Attempt(round_index=1, tool="rag", query="molina",
                                 returned_payload=False, targeted=False)])
    state = _state([g])
    written = explain(state, select(state))
    row = written["open_gaps"][0]["attempts"][0]
    assert row["targeted"] is False, "write path dropped `targeted`"

    assert ledger.attempt_from_row(row).targeted is False, "read path dropped it"


def test_rows_written_before_the_fix_replay_as_they_decided():
    """No `targeted` key means a pre-fix row. It must default TRUE -- that is
    what those turns actually did. Defaulting False would rewrite history into
    decisions the governor never made."""
    from app.pipeline.v2 import ledger
    old_row = {"round": 1, "tool": "rag", "query": "q", "returned_payload": False}
    assert ledger.attempt_from_row(old_row).targeted is True
