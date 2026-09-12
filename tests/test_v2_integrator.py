"""The v2 integrator: deterministic assembly + critic + next steps.

Ananth, 2026-09-12: "lets also get a new integrator. which is really the v2 of
deterministic enricher, + critic (llm) + next steps (llm) for now" and "you can
reuse the existing models if they work which should for critic and next steps".
"""
from app.pipeline.v2.contract import Fact
from app.pipeline.v2.enrich import decide
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
    return decide(**base)


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


def test_critic_and_next_steps_both_run():
    out = run(question="q", answer="a", facts=FACTS, open_gaps=GAPS,
              decision=_decision(),
              runner=_runner(critic='{"summary":"UHC missing","parts":[]}',
                             steps='{"next_steps":["Search the UHC manual"]}'))
    assert out.critique_summary == "UHC missing"
    assert out.next_steps == ("Search the UHC manual",)
    assert out.ran["critique"] == "ok" and out.ran["next_steps"] == "ok"


def test_a_failed_critic_does_not_take_next_steps_with_it():
    out = run(question="q", answer="a", facts=FACTS, open_gaps=GAPS,
              decision=_decision(),
              runner=_runner(steps='{"next_steps":["x"]}', fail="v2_critic"))
    assert out.ran["critique"] == "failed"
    assert out.next_steps == ("x",)
    assert any("critique call failed" in p for p in out.problems)


def test_a_failed_call_is_recorded_never_invented():
    """An integrator that invents a critique when the critic did not answer is
    worse than one that returns nothing -- the invention reads as a check that
    happened."""
    out = run(question="q", answer="a", facts=FACTS, open_gaps=GAPS,
              decision=_decision(), runner=_runner(fail="v2_critic"))
    assert out.critique == () and out.critique_summary == ""
    assert out.ran["critique"] == "failed"


def test_supported_with_no_evidence_is_downgraded():
    """The verdict a lenient judge produces. The point of the critic is not
    taking the model's word for it."""
    out = run(question="q", answer="a", facts=FACTS, open_gaps=GAPS,
              decision=_decision(),
              runner=_runner(critic='{"parts":[{"part":"Sunshine","status":"supported","evidence":[]}]}'))
    assert out.critique[0].status == "unobservable"
    assert any("no evidence" in p for p in out.problems)


def test_supported_with_evidence_survives():
    out = run(question="q", answer="a", facts=FACTS, open_gaps=GAPS,
              decision=_decision(),
              runner=_runner(critic='{"parts":[{"part":"Molina","status":"supported","evidence":["molina_fl.pdf p102"]}]}'))
    assert out.critique[0].status == "supported"


def test_fenced_json_parses():
    """The first fence-stripper mangled ```json blocks and the critique came
    back empty on a perfectly good response."""
    assert _loads('```json\n{"a": 1}\n```') == {"a": 1}
    assert _loads('```\n{"a": 1}\n```') == {"a": 1}
    assert _loads('here you go {"a": 1} hope that helps') == {"a": 1}
    assert _loads("not json at all") is None


def test_skipped_is_not_failed_and_keeps_the_deterministic_half():
    out = run(question="q", answer="", facts=FACTS, open_gaps=GAPS,
              decision=decide(answer=""), runner=_runner())
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
    every falsiness check passes, and the thing it represents is absent."""
    from app.pipeline.v2.integrator import _critic_prompt
    system, _ = _critic_prompt("q", "a", (), ())
    assert "missing prompt block" not in system
    assert "checking an answer" in system


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
