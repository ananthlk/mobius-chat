"""react's stated need must reach a consumer as STRUCTURE, not display text.

2026-09-13: the emit carried `gaps: 5` and rendered the sentences into detail
strings. Retriever needed the sentences, could not get them, and reconstructed
a worse signal from tag diffs -- which surfaced an incontinence-supplies policy
and a member-reimbursement-for-swimming-lessons doc, both correctly tagged and
both irrelevant.

react had already written the thing that would have excluded them:

    "Whether Sunshine Health has a general reimbursement policy for care
     management OUTSIDE OF TCM."
    "Whether Aetna reimburses providers for THEIR OWN care management."

No tag code expresses provider-delivered-not-member-reimbursed. That sentence
does.

TEXT ONLY, NEVER A CODE: the consumer resolves these through Gate's lexicon
matcher, which can only return codes that exist -- so a hallucinated identifier
has no path in. These tests assert the text travels and that no code does.
"""
from app.pipeline.v2.contract import Fact, GapState, ReactV2Response
from app.pipeline.v2 import trace as T


def _resp(**kw):
    base = dict(
        facts=(Fact("a fact", "Doc", 4, "doc-id"),),
        gaps=(GapState(text="Whether Aetna reimburses providers for their own "
                            "care management activities.", status="open"),
              GapState(text="already answered", status="closed")),
        tool_request="rag",
        tool_reason="need a fee schedule, not a member benefits page",
        shape_seen="mixed", is_complete=False,
    )
    base.update(kw)
    return ReactV2Response(**base)


def _data(step):
    return step.data


def test_open_gap_sentences_travel_as_structure():
    d = _data(T.llm_step(_resp(), provider="vertex", model="m", round_index=1)
              if hasattr(T, "llm_step") else T.reply_step(_resp()))
    assert "gaps_open" in d, f"gap sentences not emitted: {sorted(d)}"
    assert any("their own care management" in g for g in d["gaps_open"]), d["gaps_open"]


def test_closed_gaps_are_excluded_from_gaps_open():
    d = _data(T.reply_step(_resp()))
    assert all("already answered" not in g for g in d["gaps_open"])
    # ...but remain available with their status, for anyone who wants them.
    assert any(g["status"] == "closed" for g in d["gaps_all"])


def test_the_count_field_is_unchanged_for_existing_consumers():
    """`gaps` was an int. Renaming it would make two authors of one field."""
    d = _data(T.reply_step(_resp()))
    assert d["gaps"] == 2 and isinstance(d["gaps"], int)


def test_tool_request_and_reason_travel():
    d = _data(T.reply_step(_resp()))
    assert d["tool_request"] == "rag"
    assert "fee schedule" in d["tool_reason"]


def test_empty_response_produces_empty_lists_not_missing_keys():
    """A consumer must be able to tell 'react stated nothing' from 'the field
    does not exist' -- could-not-check vs checked-false."""
    d = _data(T.reply_step(ReactV2Response()))
    assert d["gaps_open"] == [] and d["gaps_all"] == []
    assert d["tool_request"] == "" and d["tool_reason"] == ""


def test_no_corpus_code_is_ever_emitted_from_reacts_text():
    """The consumer resolves text through the lexicon; it must never receive an
    identifier react produced. Guard the PROPERTY: the emitted need-fields
    carry only the strings react wrote."""
    r = _resp(gaps=(GapState(text="need code T1099 for this", status="open"),))
    d = _data(T.reply_step(r))
    # The sentence travels verbatim -- including a token react invented.
    assert d["gaps_open"] == ["need code T1099 for this"]
    # But nothing in the emit presents it AS a code/identifier field.
    assert not any(k for k in d if "code" in k.lower()), sorted(d)
