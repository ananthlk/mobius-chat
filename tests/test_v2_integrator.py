"""The v2 integrator: deterministic assembly + critic + next steps.

Ananth, 2026-09-12: "lets also get a new integrator. which is really the v2 of
deterministic enricher, + critic (llm) + next steps (llm) for now" and "you can
reuse the existing models if they work which should for critic and next steps".
"""
from app.pipeline.v2.contract import Fact
from app.pipeline.v2.enrich import should_enrich
from app.pipeline.v2.integrator import (
    _distinguishing_tokens, _loads, assemble, run,
)

GAPS = ("Molina care management philosophy",
        "Sunshine care management philosophy",
        "United Healthcare care management philosophy")
FACTS = (Fact("Molina uses an Integrated Care Management program", "molina_fl.pdf", 102),
         Fact("Sunshine runs a quality improvement program", "Sunshine_Manual.pdf", 38))


def _decision(**kw):
    base = dict(answer="an answer", facts=FACTS, open_gaps=GAPS,
                elapsed_s=20.0, promise_s=31.0, rounds_left=2, round_cost_s=5.0)
    base.update(kw)
    return should_enrich(**base)


# ── the deterministic half needs no model, and is therefore checkable ───────

def test_coverage_is_the_three_payer_check_mechanised():
    """17 passages covering two payers and 17 covering three read identically
    to a human and differently here."""
    cov = {c.part: c.status for c in
           assemble(question="q", answer="a", facts=FACTS, open_gaps=GAPS).coverage}
    assert cov[GAPS[0]] == "supported"
    assert cov[GAPS[1]] == "supported"
    assert cov[GAPS[2]] == "not_attempted"


def test_matching_uses_the_tokens_that_DISTINGUISH_the_parts():
    """Matching the whole gap text finds nothing -- "Molina care management
    philosophy" is not a substring of "Molina uses an Integrated Care
    Management program", and every part came back not_attempted with its
    evidence sitting right there."""
    keys = _distinguishing_tokens(GAPS)
    assert "molina" in keys[GAPS[0]]
    # Shared words cannot separate the parts and must be excluded, or any fact
    # marks every part supported.
    for part in GAPS:
        assert "management" not in keys[part]
        assert "philosophy" not in keys[part]


def test_a_single_part_question_has_no_other_part_to_contrast_with():
    one = ("timely filing deadline for Sunshine Health",)
    toks = _distinguishing_tokens(one)[one[0]]
    assert "sunshine" in toks and toks


def test_an_answered_part_with_no_grounded_fact_is_UNOBSERVABLE_not_unsupported():
    """"Unsupported" would claim we checked the corpus. We checked the FACTS."""
    cov = assemble(question="q", answer="a", facts=(),
                   open_gaps=()).coverage
    assert all(c.status in ("unobservable", "not_attempted") for c in cov)


def test_supported_verdicts_carry_their_citation():
    cov = {c.part: c for c in assemble(question="q", answer="a", facts=FACTS,
                                       open_gaps=GAPS).coverage}
    assert "molina_fl.pdf p102" in cov[GAPS[0]].evidence


def test_parts_come_from_reacts_own_gaps_not_a_second_decomposition():
    """Re-deriving parts by parsing the question would make the integrator a
    second author of a decomposition this system already made."""
    out = assemble(question="a totally different question", answer="a",
                   facts=FACTS, open_gaps=GAPS)
    assert {c.part for c in out.coverage} == set(GAPS)


# ── the model half: degrade, never fabricate ───────────────────────────────

def _runner(critic=None, steps=None, fail=None):
    def r(system, user, *, max_tokens, stage=None):
        if fail and fail in (stage or ("v2_critic" if "checking" in system else "v2_next_steps")):
            raise RuntimeError("model down")
        if "checking an answer" in system:
            return critic if critic is not None else '{"summary":"ok","parts":[]}'
        return steps if steps is not None else '{"next_steps":[]}'
    return r


def test_next_steps_still_runs_with_no_llm_critic():
    """The LLM critic is gone; next_steps is the only model call left here."""
    out = run(question="q", answer="a", facts=FACTS, open_gaps=GAPS,
              decision=_decision(),
              runner=_runner(steps='{"next_steps":["Search the UHC manual"]}'))
    assert out.next_steps == ("Search the UHC manual",)
    assert out.ran["next_steps"] == "ok"
    assert out.ran["critique"].startswith("deterministic")


def test_no_llm_critic_is_ever_called():
    """🔴 THE POINT OF THIS FILE NOW.

    Ananth, 2026-09-12: "take it out.. critic is a dynamic thing as part of
    react.. we dont need it here.. we will bake in a deterministic critique
    which we already have". It was removed from the per-round path then and
    NOT from finalisation, so it kept running: measured 2026-09-14 across 15
    A/B questions, v2_critic fired on 10 of 15 turns at 13.0s average.

    Asserts on the STAGES the runner was asked for, so a critic reintroduced
    under any prompt or name fails this.
    """
    seen = []

    def _spy(system, user, max_tokens, stage):
        seen.append(stage)
        return '{"next_steps":[]}'

    run(question="q", answer="a", facts=FACTS, open_gaps=GAPS,
        decision=_decision(), runner=_spy)
    assert "v2_critic" not in seen, f"an LLM critic ran: {seen}"
    assert seen == ["v2_next_steps"], seen


def test_critique_comes_from_the_deterministic_findings():
    """verify_claims compared each claim to the page it cites. That verdict --
    not a model's second opinion -- is what reaches the trace."""
    out = run(question="q", answer="a", facts=FACTS, open_gaps=GAPS,
              decision=_decision(),
              verified_findings=("Sunshine 180 days: cited page says 365",),
              runner=_runner(steps='{"next_steps":[]}'))
    assert len(out.critique) == 1
    assert out.critique[0].part == "Sunshine 180 days"
    assert "365" in out.critique[0].why
    assert out.critique[0].status == "unsupported"
    assert "1 claim(s)" in out.critique_summary


def test_no_findings_means_no_critique_and_says_so():
    """Nothing flagged and nothing checked must not read the same. ran[] states
    which it was."""
    out = run(question="q", answer="a", facts=FACTS, open_gaps=GAPS,
              decision=_decision(), runner=_runner(steps='{"next_steps":[]}'))
    assert out.critique == () and out.critique_summary == ""
    assert "nothing flagged" in out.ran["critique"]


def test_a_failed_next_steps_call_is_recorded_never_invented():
    """An integrator that invents next steps the model did not return is worse
    than one returning none -- the invention reads as advice that was reasoned."""
    out = run(question="q", answer="a", facts=FACTS, open_gaps=GAPS,
              decision=_decision(), runner=_runner(fail="v2_next_steps"))
    assert out.next_steps == ()
    assert out.ran["next_steps"] == "failed"
    assert any("next_steps call failed" in p for p in out.problems)


def test_fenced_json_parses():
    """The first fence-stripper mangled ```json blocks and the critique came
    back empty on a perfectly good response."""
    assert _loads('```json\n{"a": 1}\n```') == {"a": 1}
    assert _loads('```\n{"a": 1}\n```') == {"a": 1}
    assert _loads('here you go {"a": 1} hope that helps') == {"a": 1}
    assert _loads("not json at all") is None


def test_skipped_is_not_failed_and_keeps_the_deterministic_half():
    out = run(question="q", answer="", facts=FACTS, open_gaps=GAPS,
              decision=should_enrich(answer=""), runner=_runner())
    assert out.ran["critique"] == "skipped"
    assert out.coverage, "the deterministic half still ran"


def test_next_steps_are_capped():
    out = run(question="q", answer="a", facts=FACTS, open_gaps=GAPS,
              decision=_decision(),
              runner=_runner(steps='{"next_steps":["a","b","c","d","e"]}'))
    assert len(out.next_steps) == 3


# ── the prompt source must be decided on SOURCE, not truthiness ─────────────

def test_a_missing_prompt_block_falls_back_instead_of_shipping_the_placeholder():
    """🔴 resolve() returns a TRUTHY placeholder for a block the DB lacks:
    "[missing prompt block: ...]", source="missing". `text or fallback` picks
    the placeholder and the model receives THAT as its entire system prompt --
    a critic returning confident nonsense with no error anywhere.

    Same shape as an honest-empty dressed as evidence: the value is present so
    every falsiness check passes, and the thing it represents is absent.

    Retargeted 2026-09-14 from _critic_prompt, which was deleted with the LLM
    critic. The defect is a property of _prompt_text, not of which block uses
    it, so the coverage moves to the prompt builder that survives -- deleting
    the test with the function would have retired a guard that still has
    something to guard.
    """
    from app.pipeline.v2.integrator import _next_steps_prompt
    system, _ = _next_steps_prompt("q", "a", ())
    assert "missing prompt block" not in system
    assert system.strip()


def test_the_prompt_source_is_recorded_not_inferred():
    """"The DB had it" and "we fell back" produce identical prompts to a reader
    and different provenance for a wording change."""
    out = run(question="q", answer="a", facts=FACTS, open_gaps=GAPS,
              decision=_decision(), runner=_runner())
    assert out.prompt_sources
    assert all(v.startswith(("db", "fallback")) for v in out.prompt_sources.values())


def test_a_resolver_that_raises_still_yields_a_usable_prompt(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("prompt service down")
    monkeypatch.setattr("app.pipeline.v2.statement_text.resolve", boom)
    from app.pipeline.v2.integrator import _next_steps_prompt
    system, _ = _next_steps_prompt("q", "a", ())
    assert "close what is still open" in system


def test_coverage_checks_every_part_the_turn_named_not_just_the_open_ones():
    """MEASURED LIVE: react closed all three payer gaps and reported none
    open, so parts fell back to the whole question, coverage collapsed to ONE
    part, and a single Molina fact marked it "supported". The three-payer check
    stopped checking at the exact moment the answer claimed to be complete.

    Closed gaps ARE the decomposition; open gaps are only its unfinished tail.
    """
    out = assemble(question="q", answer="a",
                   facts=(Fact("Molina uses ICM", "molina.pdf", 111),),
                   open_gaps=(), all_parts=GAPS)
    got = {c.part: c.status for c in out.coverage}
    assert got[GAPS[0]] == "supported"
    # Not "supported by association" and not "not_attempted" (we did search) --
    # nothing grounds them, and that is a different claim from either.
    assert got[GAPS[1]] == "unobservable"
    assert got[GAPS[2]] == "unobservable"


def test_a_single_part_question_still_falls_back_to_the_question():
    out = assemble(question="timely filing for Sunshine", answer="a",
                   facts=(), open_gaps=(), all_parts=())
    assert len(out.coverage) == 1


# ── truncated is not malformed, and they need opposite fixes ────────────────

def test_a_truncated_reply_says_TRUNCATED_and_names_the_budget():
    """MEASURED against gemini-2.5-flash with the real prompt:
         max_tokens=900  -> 166 chars, cut mid-word
         max_tokens=3000 -> 936 chars, cut mid-word
         max_tokens=8000 -> complete, parses
    The budget includes THINKING tokens, so sizing it to the visible ~900-char
    reply truncates. The first live run reported "was not JSON" for a reply
    that was perfectly good JSON with its tail missing, and that wrong
    diagnosis sent me to the prompt."""
    out = run(question="q", answer="a", facts=FACTS, open_gaps=GAPS,
              decision=_decision(),
              runner=_runner(steps='```json\n{"summary": "the answer accur'))
    assert any("TRUNCATED" in p and "NEXT_STEPS_MAX_TOKENS" in p for p in out.problems)
    assert not any("was not JSON" in p for p in out.problems)


def test_genuinely_malformed_still_says_not_json():
    out = run(question="q", answer="a", facts=FACTS, open_gaps=GAPS,
              decision=_decision(), runner=_runner(steps="I think it looks fine!"))
    assert any("was not JSON" in p for p in out.problems)


# test_empty_string_evidence_does_not_count_as_evidence REMOVED 2026-09-14,
# with the code it guarded. It asserted that an LLM critic returning
# status="supported" with evidence [""] was downgraded to "unobservable" -- a
# real live defect, and a property of asking a model to grade our own answer.
#
# The deterministic verifier cannot produce that failure: it compares a claim
# to the page it cites, so it has no way to assert support it did not find.
# Keeping the test would mean keeping _parse_critique alive to satisfy it.


# ── an empty fact list cannot make a claim unsupported ─────────────────────

def test_next_steps_still_runs_without_facts():
    """"What would close what is still open" is answerable from the gaps alone
    and needs no facts. Skipping it too would lose a section for an unrelated
    reason."""
    out = run(question="q", answer="a", facts=(), open_gaps=GAPS,
              decision=_decision(facts=()),
              runner=_runner(steps='{"next_steps":["Search the UHC manual"]}'))
    assert out.ran["next_steps"] == "ok"
    assert out.next_steps == ("Search the UHC manual",)


def test_an_ungrounded_fact_does_not_count_as_something_to_check_against():
    """A fact with no document cannot support anything, so a list of only
    those is still nothing to check against."""
    called = []

    def runner(system, user, *, max_tokens, stage=None):
        called.append(stage)
        return "{}"

    run(question="q", answer="a", facts=(Fact("floating", "", None),),
        open_gaps=GAPS, decision=_decision(facts=()), runner=runner)
    assert "v2_critic" not in called


def test_a_token_inside_another_part_cannot_distinguish():
    """🔴 MEASURED LIVE, and it is the three-payer defect INVERTED.

    "Sunshine Health care management philosophy" kept `health` as a
    distinguishing token — it is not a token of "UnitedHealthcare care
    management philosophy" — and matching is a SUBSTRING test, so it then
    matched Molina's "health management programs" AND the string
    "UnitedHealthcare". Coverage reported Sunshine SUPPORTED, citing Molina's
    and UHC's documents.

    A payer silently credited with someone else's evidence is worse than one
    silently missing: it reads as verified."""
    keys = _distinguishing_tokens(GAPS)
    assert keys[GAPS[1]] == ("sunshine",), keys[GAPS[1]]
    assert "health" not in keys[GAPS[1]]


def test_another_payers_document_does_not_support_this_part():
    facts = (Fact("Molina offers health management programs", "molina.pdf", 108),
             Fact("UnitedHealthcare Care Model empowers members", "FL-Care.pdf", 5))
    cov = {c.part: c for c in assemble(question="q", answer="a", facts=facts,
                                       all_parts=GAPS).coverage}
    assert cov[GAPS[1]].status == "unobservable"
    assert cov[GAPS[1]].evidence == ()
    assert cov[GAPS[0]].status == "supported"
