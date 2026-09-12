"""Two-letter state codes that are also English words.

Found live 2026-09-11: "approval for **in** home ventilators" resolved to
Indiana, scoping every search on the thread to a state the corpus holds
nothing for. The turn burned its whole round budget and answered "our
materials didn't cover this" — indistinguishable from a genuine corpus gap,
on a question the corpus CAN answer (the same wording returns HCPCS E0466
once the jurisdiction is right).
"""

import pytest

from app.state.state_extractor import STATE_ABBREVS, _detect_jurisdiction


@pytest.mark.parametrize("text", [
    "I want to know if i need approval for in home ventilators",
    "do we cover services or supplies",
    "tell me about prior authorization",
    "is this ok for outpatient therapy",
    "what services are covered at hi acuity",
    "submit the claim as a corrected claim",
])
def test_english_words_are_not_state_codes(text):
    """The patterns run under re.I so surrounding words match in any case —
    which also made `[A-Z]{2}` match LOWERCASE, and `.upper()` then laundered
    an English word into a state code.

    The function's own docstring already warned about this exact list — "OR",
    "IN", "ME", "OK", "HI" appear frequently as normal words — and said the
    token is "matched and normalized". It was normalized; it was never matched
    as an abbreviation. THE GUARD WAS DOCUMENTED, NOT WRITTEN.
    """
    assert _detect_jurisdiction(text) is None, text


@pytest.mark.parametrize("text,expected", [
    ("what is the rate in FL", "FL"),
    ("Miami, FL timely filing", "FL"),
    ("state of TX rules", "TX"),
    ("billing for CA providers", "CA"),
])
def test_real_abbreviations_still_resolve(text, expected):
    """An abbreviation WRITTEN as one still works. The fix requires the token
    to be uppercase as typed, not merely spellable as a state code."""
    assert _detect_jurisdiction(text) == expected


@pytest.mark.parametrize("text,expected", [
    ("prior auth in Florida", "Florida"),
    ("coverage in florida for therapy", "Florida"),
    ("TEXAS medicaid rules", "Texas"),
])
def test_full_state_names_stay_case_insensitive(text, expected):
    """The cost of the fix, stated: lowercase "coverage in fl" no longer
    resolves. Full NAMES are unaffected, which is the common way a person
    actually writes it."""
    assert _detect_jurisdiction(text) == expected


def test_every_ambiguous_abbreviation_is_covered_not_just_IN():
    """Fixing the instance would leave the class. Each of these is a real
    state code AND a common English word; a lowercase occurrence of any must
    not resolve."""
    words = {"in": "IN", "or": "OR", "me": "ME", "ok": "OK", "hi": "HI",
             "de": "DE", "la": "LA", "pa": "PA", "ma": "MA", "mo": "MO"}
    for word, code in words.items():
        assert code in STATE_ABBREVS, f"{code} left the abbreviation list"
        # "for <word>" and "in <word>" are the two context patterns that
        # accept a bare token
        assert _detect_jurisdiction(f"approval for {word} home services") is None
        assert _detect_jurisdiction(f"is it covered in {word} settings") is None
