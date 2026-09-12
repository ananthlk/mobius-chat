"""A count is not a summary.

Ananth, 2026-09-12: "we need a real good summary from rag.. which should
include its role, what it is trying to solve, what it found and the new gap it
is trying to close".
"""
from app.pipeline.react_loop import _preload_doc_spread, _terms_not_returned

Q = "What is the care management philosophy for Molina, Sunshine and United Healthcare?"


def _chunks(*rows):
    return {"chunks": [{"document_name": d, "page_number": p, "text": t}
                       for d, p, t in rows]}


# ── what it found ───────────────────────────────────────────────────────────

def test_documents_and_pages_not_a_count():
    """"17 passages" tells react nothing it can judge -- not which documents,
    not whether its ask was covered."""
    res = _chunks(("molina_manual.pdf", 108, "care management"),
                  ("molina_manual.pdf", 111, "philosophy"),
                  ("Sunshine.pdf", 52, "whole person"))
    spread = _preload_doc_spread(res)
    assert any("molina_manual.pdf" in s and "108" in s and "111" in s for s in spread)
    assert len(spread) == 2, spread


def test_never_returns_chunk_text():
    """The frame renders ONE LINE per tool; the payload already reaches react
    through its own channel. Putting text here sends the retrieval twice."""
    res = _chunks(("d.pdf", 1, "SENTINEL_BODY_TEXT should never appear"))
    assert "SENTINEL" not in " ".join(_preload_doc_spread(res))


def test_survives_a_result_shape_it_does_not_know():
    for res in ({}, {"chunks": "not-a-list"}, {"chunks": [None, 3]},
                {"chunks": [{"no_name": 1}]}):
        assert _preload_doc_spread(res) == []


# ── the new gap: what the ask did not reach ─────────────────────────────────

def test_names_the_payer_that_came_back_in_nothing():
    """THE THREE-PAYER DEFECT, caught mechanically: passages for two payers,
    a count that reads like success, and nobody notices the third."""
    res = _chunks(("molina_manual.pdf", 108, "Molina care management philosophy"),
                  ("Sunshine.pdf", 52, "Sunshine Health whole person care"))
    missing = _terms_not_returned(Q, res)
    assert "united" in missing


def test_says_nothing_when_everything_was_reached():
    """A false alarm every round is a gate that gets deleted."""
    res = _chunks(("m.pdf", 1, "Molina Sunshine United Healthcare care management philosophy"))
    assert _terms_not_returned(Q, res) == []


def test_stopwords_and_short_words_are_not_gaps():
    res = _chunks(("m.pdf", 1, "molina sunshine united healthcare care management philosophy"))
    assert _terms_not_returned("What is the care management philosophy", res) == []


def test_document_names_count_as_coverage():
    """A payer named in the DOCUMENT but not the body is still reached -- the
    UHC manual is titled FL-Care-Provider-Manual and says "UnitedHealthcare"
    on the cover, not in every passage."""
    res = {"chunks": [{"document_name": "United Healthcare FL manual",
                       "page_number": 5, "text": "care management approach"}]}
    assert "united" not in _terms_not_returned(Q, res)


def test_an_empty_result_reports_nothing_rather_than_everything():
    """With no text to search, EVERY term is 'not mentioned' -- which would
    turn an empty retrieval into a list of invented gaps."""
    assert _terms_not_returned(Q, {"chunks": []}) == []


def test_substring_matching_covers_the_unspaced_form():
    """The check is a SUBSTRING test, so "united" is reached by
    "UnitedHealthcare". This is the direction that matters most here -- the
    payer lexicon defect this week was precisely the unspaced form."""
    res = _chunks(("d.pdf", 1, "UnitedHealthcare care management"))
    assert "united" not in _terms_not_returned("United care management", res)


def test_the_false_lead_runs_the_other_way():
    """An unspaced term in the ASK is NOT reached by spaced text -- a genuine
    false lead. Which is why the summary says "not mentioned in anything
    returned", never "missing from the corpus": could-not-find is not
    checked-absent, and react must be able to tell those apart."""
    res = _chunks(("d.pdf", 1, "United Health Care care management"))
    assert "unitedhealthcare" in _terms_not_returned("UnitedHealthcare philosophy", res)


# ── the payload must survive execute() ──────────────────────────────────────
# It did not. execute() rebuilt its result dict from three fields, so a
# 141,074-character retrieval left the function as a 170-character summary and
# round 1 answered from its own priors with citations to evidence that was
# never in the prompt. Twelfth producer-with-no-consumer of the session, and
# the only one built inside the module whose job is carrying the evidence.

def test_execute_carries_everything_the_runner_returned():
    """THE PROPERTY, not a field list: whatever the runner returns must reach
    the caller. Asserting only "payload" would pass again the next time
    someone adds a field and this dict does not."""
    from app.pipeline.v2.preload import PreloadPlan, execute
    given = {"ok": True, "summary": "15 passage(s)", "payload": "X" * 141074,
             "sources": [{"document_name": "m.pdf"}], "asked": "the ask",
             "future_field": "added later by someone else"}
    out = execute(PreloadPlan(execute=("rag",)), lambda t, i: dict(given), "q")
    assert len(out) == 1
    missing = [k for k in given if k not in out[0] and k != "future_field"]
    assert not missing, f"execute() dropped {missing}"
    assert len(out[0]["payload"]) == 141074, "payload was truncated in transit"


def test_a_raising_tool_still_has_the_payload_keys():
    """The caller reads .get("payload") on every row. A row missing the key
    after an exception is the same absence, arriving later."""
    from app.pipeline.v2.preload import PreloadPlan, execute
    def boom(t, i):
        raise RuntimeError("nope")
    out = execute(PreloadPlan(execute=("rag",)), boom, "q")
    assert out[0]["ok"] is False
    for k in ("payload", "sources", "asked"):
        assert k in out[0], k


def test_the_summary_cap_does_not_cut_a_document_name_in_half():
    """200 chars cut the four-part summary mid-document-name -- silently, in
    the middle of the line react reads to decide whether its ask was covered."""
    from app.pipeline.v2.preload import PreloadPlan, execute
    summ = ("15 passage(s) across 5 doc(s): molina_fl_provider_manual_2026.pdf "
            "p102/103/107; SH-PRO-BH-PSR.pdf p5; Sunshine Provider Manual "
            "p38/45/58; FL-Care-Provider-Manual-Statewide-Medicaid-M p5/39/51")
    out = execute(PreloadPlan(execute=("rag",)),
                  lambda t, i: {"ok": True, "summary": summ, "payload": "x"}, "q")
    assert out[0]["summary"] == summ, "summary truncated mid-name"
