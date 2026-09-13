"""Quality control is a deterministic check, not an LLM opinion.

Ananth, 2026-09-13: "critic is a dynamic thing as part of react.. we dont need
it here.. we will bake in a deterministic critique which we already have (the
tool that manifest developed for check a fact).. and if the critique is really
found anything then we go back to react, else we move on".

🔴 WHAT IT REPLACES, measured on cid ab19a39a — a 10-second turn:
    +5.70s  [react] v2 composition prompt module=critic_audit
    +5.80s  [vertex] generate_content prompt_len=4738
    +9.46s  returned (elapsed=3.7s)
    +9.56s  [governor] complete ... critic=None
3.7s — 37% of the turn — and the governor used NOTHING from it.
"""
import pytest

from app.pipeline.v2 import verify as V


class _F:
    def __init__(self, **kw):
        self.fact = kw.get("fact", "a claim")
        self.document = kw.get("document", "m.pdf")
        self.page = kw.get("page", 11)
        self.document_id = kw.get("document_id", "d1")


def _runner(results, **extra):
    def run(tool, inputs):
        assert tool == "verify_claims"
        run.seen = inputs
        return {"outcome": "evidence", "payload": {"results": results}, **extra}
    run.seen = None
    return run


def test_a_supported_answer_moves_on():
    r = V.verify([_F()], _runner([{"verdict": "supported", "score": 0.9}]))
    assert r.reopen is False and r.supported == 1 and not r.findings


def test_a_refuted_claim_goes_back_to_react():
    r = V.verify([_F()], _runner([{"verdict": "not_supported", "score": 0.4}]))
    assert r.reopen is True and len(r.findings) == 1


def test_UNVERIFIABLE_IS_NOT_A_FINDING():
    """🔴 THE LINE THIS WHOLE SESSION KEEPS CROSSING. A claim the verifier
    could not check is not a claim it refuted — reopening on one spends a round
    on the verifier's blind spot, and deleting on one is could-not-check
    rendered as checked-false."""
    r = V.verify([_F()], _runner([{"verdict": "unverifiable", "score": None}]))
    assert r.reopen is False
    assert r.unverifiable == 1
    assert not r.findings


def test_a_wrong_NUMBER_asks_for_a_repair_not_a_deletion():
    """Deep Research measured their first version scoring "six years" at 0.943
    against a source saying FIVE, and passing it. not_supported at 0.94 WITH
    numbers_missing is a near-quote with the figure changed — "fix the number"
    and "drop the claim" are different instructions to react."""
    r = V.verify([_F()], _runner([{"verdict": "not_supported", "score": 0.94,
                                   "numbers_missing": ["6 (six)"]}]))
    repair = r.findings[0].repair()
    assert "WRONG FIGURE" in repair and "6 (six)" in repair
    assert "drop the claim" in repair          # offered, not commanded


def test_a_wrong_PAGE_keeps_the_claim():
    r = V.verify([_F()], _runner([{"verdict": "not_supported", "score": 0.88,
                                   "citation_wrong": "p113"}]))
    repair = r.findings[0].repair()
    assert "WRONG PAGE" in repair and "the claim itself stands" in repair


def test_unscoped_facts_are_not_sent_at_all():
    """Their ceiling is 144,000ms UNSCOPED and ~1.1s scoped. A fact with no
    document_id is therefore NOT CHECKED rather than checked slowly — and the
    result says so instead of implying it passed."""
    run = _runner([{"verdict": "supported"}])
    r = V.verify([_F(document_id=""), _F()], run)
    assert len(run.seen["facts"]) == 1
    assert any("document_id" in p for p in r.problems)


def test_the_bar_is_passed_explicitly_never_inherited():
    """Deep Research: pass `bar` explicitly so the choice is recorded rather
    than assumed — the default is tuned for a caller that DELETES, which is
    us; a publishing caller's safe direction is the opposite."""
    run = _runner([{"verdict": "supported"}])
    V.verify([_F()], run)
    assert run.seen["bar"] == V.BAR
    assert 0.0 < V.BAR < 1.0


def test_document_ids_are_always_sent():
    run = _runner([{"verdict": "supported"}])
    V.verify([_F(document="m.pdf", document_id="d1")], run)
    assert run.seen["document_ids"] == {"m.pdf": "d1"}


@pytest.mark.parametrize("bad", [
    {"could_not_run": True, "reason": "no route declared"},
    {"outcome": "could_not_run", "reason": "refused"},
    {"outcome": "evidence", "payload": {"results": "not a list"}},
])
def test_a_verifier_that_could_not_run_never_becomes_a_finding(bad):
    """The failure mode that would be worst: the checker breaks, and its
    silence is read as the answer being wrong."""
    r = V.verify([_F()], lambda t, i: bad)
    assert r.reopen is False and not r.findings and r.skipped


def test_it_never_raises():
    def boom(tool, inputs):
        raise RuntimeError("network")
    r = V.verify([_F()], boom)
    assert r.reopen is False and "raised" in r.skipped


# ── scoping is a cliff, not a slope ──────────────────────────────────────────
#
# 🔴 MEASURED against the live service, 9 identical facts:
#       with document_ids        550ms
#       without document_ids  180,157ms  ->  HTTP 504 Gateway Timeout
#       empty document_ids    181,716ms  ->  HTTP 504 Gateway Timeout
#
# An unscoped fact does not make the call slower — it destroys it, and takes
# the facts that COULD have been checked down with it. Our own live turn cost
# 11.5s (cid 4033e5cf), which is neither number: partial scoping.

def test_every_sent_fact_is_scopable_by_the_map_that_travels_with_it():
    run = _runner([{"verdict": "supported"}] * 2)
    V.verify([_F(document="a.pdf", document_id="1"),
              _F(document="b.pdf", document_id="2")], run)
    keys = set(run.seen["document_ids"])
    for f in run.seen["facts"]:
        assert f["document"] in keys


def test_a_name_that_differs_only_by_case_or_space_still_scopes():
    """The id map is keyed by NAME. A fact whose name differs by case or
    whitespace would be sent with a map that cannot scope it — which is the
    44-char truncation defect arriving from the other direction."""
    run = _runner([{"verdict": "supported"}])
    V.verify([_F(document="  Molina FL  Manual.pdf ", document_id="d1")], run)
    assert len(run.seen["facts"]) == 1
    assert run.seen["document_ids"]


def test_a_fact_with_no_document_name_is_dropped_not_sent():
    run = _runner([{"verdict": "supported"}])
    r = V.verify([_F(document="", document_id="d1"), _F()], run)
    assert len(run.seen["facts"]) == 1
    assert any("no document name" in p for p in r.problems)


def test_the_refusal_is_stated_not_silent():
    """A batch we declined to send must say so — "could not check" is a
    finding for the trace, never an implied pass."""
    r = V.verify([_F(document_id="")], lambda t, i: {})
    assert r.skipped and not r.findings and r.reopen is False


def test_verification_hangs_off_COMPLETION_not_off_finalise():
    """🔴 FOURTH INSTANCE TODAY of keying on how the turn ENDS.

    The verify block lived inside the finalise branch, so it ran only on turns
    that BUY a communicate round. Measured: neither live question verified
    anything. cid 3ee0fcdd communicated on round 1 and stopped — no finalise,
    no verification. cid 4033e5cf showed a verify_claims call that was REACT
    calling it as a tool, not this code; the [v2.verify] line never appeared on
    either turn because the block never executed.

    Asserted on the parsed source: the guard that admits verification must test
    react's completion, and must NOT be nested inside the finalise branch.
    """
    import ast
    import pathlib
    src = pathlib.Path("app/pipeline/react_loop.py").read_text()
    tree = ast.parse(src)

    found = None
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        if "_v2_verified_once" not in ast.dump(node.test):
            continue
        found = node
        break
    assert found is not None, "verification is not gated on a once-per-turn flag"

    test_src = ast.dump(found.test)
    assert "is_complete" in test_src, (
        "verification does not key on react proposing complete")
    assert "orchestrator_version" in test_src, "not scoped to the v2 arm"
    assert "_v2_finalised" not in test_src and "finalising" not in test_src, (
        "verification is keyed on the finalise path again — a turn that "
        "communicates and stops would never be checked")

    # ...and the call itself must be INSIDE that guard, not merely near it.
    body = "\n".join(ast.dump(st) for st in found.body)
    assert "verify" in body, "the guard admits nothing"


def test_it_runs_once_per_turn_not_once_per_proposal():
    """react can propose complete on several rounds and the facts are
    cumulative; paying the round trip per proposal taxes reconsidering."""
    import pathlib
    src = pathlib.Path("app/pipeline/react_loop.py").read_text()
    i = src.index("_v2_verified_once = True")
    assert "not getattr(ctx, \"_v2_verified_once\", False)" in src[:i]


def test_fact_store_provenance_becomes_a_usable_source():
    """🔴 payor_fact CITES A REAL DOCUMENT AND WE THREW THE CITATION AWAY.

    Measured, cid a87898fa — all four facts came back
        document "Sunshine Provider Manual"  page null  document_id ""
    while the envelope carried
        source.source  = "Sunshine Provider Manual"
        source.locator = "page 121, 'Timely Claim Submission' table"
    The page was in the response and nothing parsed it.
    """
    from app.pipeline.react_loop import _fact_store_sources
    out = _fact_store_sources({
        "authority": "fact_store", "as_of": "2026-09-12T03:27:26Z",
        "value": {"text": "Initial claims: 180 days."},
        "source": {"source": "Sunshine Provider Manual",
                   "locator": "page 121, 'Timely Claim Submission' table"}})
    assert out and out[0]["document_name"] == "Sunshine Provider Manual"
    assert out[0]["page_number"] == 121
    assert out[0]["authority"] == "fact_store"
    # NOT a corpus id and not pretending to be one.
    assert out[0]["document_id"] == ""


def test_it_does_not_invent_provenance_it_was_not_given():
    from app.pipeline.react_loop import _fact_store_sources
    assert _fact_store_sources({"value": {"text": "x"}}) == []
    assert _fact_store_sources({"source": {"locator": "page 3"}}) == []
    assert _fact_store_sources("not a dict") == []


def test_the_skip_reason_separates_a_defect_from_a_design_boundary():
    """A corpus fact with no id is OUR id resolution failing. A certified
    fact-store answer has no corpus id BY DESIGN. Reporting them identically
    turns a known boundary into an unexplained gap."""
    r = V.verify([_F(document_id="")], lambda t, i: {})
    assert "certified" in r.skipped and "fact-store" in r.skipped
