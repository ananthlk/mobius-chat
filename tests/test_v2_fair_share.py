"""Per-arm retrieval budget: 6k per fan-out, total not to exceed.

Ananth, 2026-09-12: "it is not enforced so we need a knob for it.. per fan out
and total not to exceed".

MEASURED FIRST: rag ignores token_budget_for_retrieval -- identical results
with and without it, both caller modes, n=2 each. So the cap is applied here,
on what came back, and it is applied PER ARM because a global top-K makes the
arms compete and the smallest entity loses outright.
"""
from app.pipeline.v2.preload import CHARS_PER_TOKEN, _arm_of, fair_share


def _src(payer, n, chars=3000, doc=None, page=1):
    return [{"payer": payer, "document_name": doc or f"{payer}.pdf",
             "page_number": page + i, "text": payer[0].upper() * chars}
            for i in range(n)]


# ── the defect this exists to prevent ───────────────────────────────────────

def test_the_one_chunk_payer_survives():
    """THE THREE-PAYER DEFECT. UHC has ONE chunk against Sunshine's ten, so any
    rank-ordered global trim removes UHC first and the answer reads complete
    with a payer silently absent."""
    srcs = _src("sunshine", 10) + _src("molina", 6) + _src("uhc", 1)
    _, kept, report = fair_share(srcs, per_arm_tokens=6000, total_tokens=24000)
    assert report["payer:uhc"]["kept"] == 1, report
    assert {a.split(":")[1] for a in report} == {"sunshine", "molina", "uhc"}


def test_round_robin_gives_every_arm_its_first_chunk_first():
    """Not "fewer chunks" -- FAIRLY fewer. Under a tight total, every arm must
    still be represented rather than the largest arm consuming the budget."""
    srcs = _src("sunshine", 10) + _src("molina", 6) + _src("uhc", 1)
    # Room for exactly three chunks INCLUDING their provenance headers. With a
    # tight total the danger is the biggest arm eating the budget before the
    # smallest arm is reached, which is how UHC disappears.
    one = 3000 + len("[sunshine.pdf p1]\n") + 2
    _, kept, report = fair_share(srcs, per_arm_tokens=6000,
                                 total_tokens=(one * 3) // CHARS_PER_TOKEN + 1)
    arms = {_arm_of(k) for k in kept}
    assert len(arms) == 3, f"an arm was starved: {report}"
    assert all(r["kept"] == 1 for r in report.values()), report


def test_an_arm_that_gets_nothing_is_reported_as_zero_not_omitted():
    """When the total genuinely cannot cover one chunk per arm, something must
    give -- but the starved arm must still appear, with kept=0. An arm missing
    from the report is indistinguishable from an arm that was never retrieved,
    and that is the never-searched / searched-empty collapse again."""
    srcs = _src("sunshine", 10) + _src("molina", 6) + _src("uhc", 1)
    _, _, report = fair_share(srcs, per_arm_tokens=6000, total_tokens=1600)
    assert "payer:uhc" in report, "starved arm vanished from the report"
    assert report["payer:uhc"]["had"] == 1
    assert report["payer:uhc"]["kept"] == 0


def test_per_arm_cap_binds_the_big_arm_only():
    """THE INVARIANT, not the arithmetic: the oversized arm is trimmed to at
    most its cap, the small arm is untouched. Pinned to an exact count this
    broke the moment provenance headers started counting toward the cap --
    which was a correctness FIX, and the test called it a regression."""
    srcs = _src("sunshine", 10) + _src("uhc", 1)
    text, kept, report = fair_share(srcs, per_arm_tokens=6000, total_tokens=100000)
    assert 0 < report["payer:sunshine"]["kept"] < 10
    assert report["payer:uhc"]["kept"] == 1
    sun = sum(len(_arm_of(k)) * 0 + len(str(k["text"])) for k in kept
              if _arm_of(k) == "payer:sunshine")
    assert sun <= 6000 * CHARS_PER_TOKEN


def test_total_cap_binds_across_arms():
    srcs = _src("a", 5) + _src("b", 5)
    text, kept, _ = fair_share(srcs, per_arm_tokens=100000, total_tokens=3000)
    assert len(text) <= 3000 * CHARS_PER_TOKEN


# ── the report distinguishes trimmed from empty ─────────────────────────────

def test_report_separates_trimmed_from_returned_nothing():
    """"kept 15 of 56" cannot tell an arm that was trimmed from an arm that
    came back empty -- and it is the second that loses a payer."""
    srcs = _src("sunshine", 10) + _src("uhc", 1)
    _, _, report = fair_share(srcs, per_arm_tokens=6000, total_tokens=100000)
    assert report["payer:sunshine"]["had"] == 10
    assert report["payer:sunshine"]["kept"] < 10, "big arm was not trimmed"
    # The small arm is NOT trimmed and is NOT missing -- the two facts the
    # report exists to keep apart.
    assert report["payer:uhc"] == {"had": 1, "kept": 1}


# ── arm identity ────────────────────────────────────────────────────────────

def test_arm_is_ragss_own_slot_id_before_anything_inferred():
    """slot_id is rag's identifier for the arm. Re-deriving arms by parsing the
    question would make chat a second author of rag's decomposition."""
    assert _arm_of({"slot_id": "fanout_0", "payer": "molina"}) == "slot_id:fanout_0"
    assert _arm_of({"payer": "molina"}) == "payer:molina"
    assert _arm_of({"document_name": "m.pdf"}) == "doc:m.pdf"


def test_unknown_is_not_an_arm_identity():
    """Grouping every unlabelled chunk under "unknown" would merge distinct
    entities into one budget -- the collision this removes."""
    assert _arm_of({"payer": "unknown", "document_name": "m.pdf"}) == "doc:m.pdf"
    assert _arm_of({"payer": "", "document_name": "m.pdf"}) == "doc:m.pdf"


# ── degenerate inputs ───────────────────────────────────────────────────────

def test_no_caps_means_keep_everything():
    """0 must mean "no cap", never "zero budget" -- an off switch that reads as
    a setting is how a knob becomes an outage."""
    srcs = _src("a", 5)
    _, kept, _ = fair_share(srcs, per_arm_tokens=0, total_tokens=0)
    assert len(kept) == 5


def test_empty_and_textless_sources_return_nothing_not_a_crash():
    for bad in ([], None, [{"payer": "a"}], [{"payer": "a", "text": "  "}], ["x", 3]):
        text, kept, report = fair_share(bad, per_arm_tokens=6000, total_tokens=24000)
        assert (text, kept, report) == ("", [], {})


def test_kept_chunks_carry_their_document_and_page():
    """React cites what it reads. Text with no provenance is how a citation
    marker ends up pointing at nothing."""
    text, _, _ = fair_share(_src("molina", 1, doc="molina_manual.pdf", page=108),
                            per_arm_tokens=6000, total_tokens=24000)
    assert "[molina_manual.pdf p108]" in text
