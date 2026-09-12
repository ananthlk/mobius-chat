"""Headline + expandable, not a wall of lines.

Ananth, 2026-09-12: "a top line but an expandable into everything underneath
it" -- said after seeing a flat wall of emits and correctly rejecting it. A
stream where every step prints eight lines is not visibility; it is the same
opacity with more scrolling.
"""
from app.pipeline.v2 import trace as T


class _Recall:
    def __init__(self, facts=(), not_useful=(), turn_chunks=0, unavailable=()):
        self.facts, self.not_useful = facts, not_useful
        self.turn_chunks, self.unavailable = turn_chunks, unavailable


class _Fact:
    def __init__(self, fact, document="", page=None):
        self.fact, self.document, self.page = fact, document, page
    @property
    def grounded(self):
        return bool(self.fact and self.document)


class _Resp:
    def __init__(self, **kw):
        self.shape_seen = kw.get("shape_seen", "v2")
        self.facts = kw.get("facts", ())
        self.gaps = kw.get("gaps", ())
        self.problems = kw.get("problems", ())
        self.is_complete = kw.get("is_complete")
        self.complete_why = kw.get("complete_why", "")
        self.next_round_worth_it = kw.get("next_round_worth_it")
        self.next_round_why = kw.get("next_round_why", "")
        self.tool_request = kw.get("tool_request", "")


# ── the headline must stand alone ───────────────────────────────────────────

def test_headline_carries_the_number_that_changes_your_mind():
    """If a reader must expand it to know whether something is wrong, it is a
    label, not a headline."""
    h = T.memory_step(_Recall(facts=("a", "b"), not_useful=("x",))).headline
    assert "2" in h and "1" in h


def test_a_starved_fan_out_arm_is_in_the_HEADLINE():
    """A starved arm means an entity will be missing from the answer. That
    cannot be one expand away."""
    s = T.fair_share_step("rag", {"f0": {"had": 6, "kept": 6},
                                  "f2": {"had": 1, "kept": 0}}, 6, 7)
    assert "NOTHING" in s.headline and "f2" in s.headline
    assert s.data["starved"] == ["f2"]


def test_no_starvation_reads_as_routine():
    s = T.fair_share_step("rag", {"f0": {"had": 6, "kept": 6}}, 6, 6)
    assert "NOTHING" not in s.headline and s.data["starved"] == []


def test_unsourced_facts_are_in_the_headline():
    """A fact with no document will be refused at the write. The reader learns
    that here, not by noticing the memory count did not move."""
    s = T.reply_step(_Resp(facts=(_Fact("grounded", "d.pdf", 1), _Fact("floating"))))
    assert "UNSOURCED" in s.headline


def test_problems_beat_counts_in_the_headline():
    s = T.reply_step(_Resp(problems=("evidence_review absent",)))
    assert "⚠" in s.headline and "evidence_review absent" in s.headline


# ── absence is said, never implied ──────────────────────────────────────────

def test_a_new_thread_does_not_read_as_a_failure():
    h = T.memory_step(_Recall()).headline
    assert "new thread" in h and "UNAVAILABLE" not in h


def test_an_unreachable_store_does_not_read_as_a_new_thread():
    h = T.memory_step(_Recall(unavailable=("thread store unreachable",))).headline
    assert "UNAVAILABLE" in h


def test_an_empty_preload_says_react_will_search_blind():
    assert "blind" in T.preload_step((), (), ()).headline


def test_nothing_remembered_is_stated():
    assert "nothing learned" in T.remembered_step(0, 0, 0).headline


def test_refusals_are_visible_not_silent():
    assert "REFUSED" in T.remembered_step(1, 2, 0).headline


# ── the detail exists, and never repeats a payload ──────────────────────────

def test_detail_expands_beyond_the_headline():
    s = T.memory_step(_Recall(facts=("a [d.pdf p1]",), turn_chunks=15))
    assert len(s.detail) >= 2 and s.headline not in s.detail


def test_turn_chunks_are_counted_never_reprinted():
    """They are already in the prompt. Reprinting them doubles the cost of the
    thing being traced."""
    s = T.memory_step(_Recall(turn_chunks=15))
    assert "15" in " ".join(s.detail)
    assert not any(len(d) > 400 for d in s.detail)


# ── the emit path degrades instead of vanishing ─────────────────────────────

def test_a_string_only_emitter_still_gets_the_headline():
    """A trace legible in one client and invisible in another gets debugged in
    neither."""
    got = []
    def emitter(x):
        if not isinstance(x, str):
            raise TypeError("this emitter only takes strings")
        got.append(x)
    T.emit_step(emitter, "cid", T.memory_step(_Recall(facts=("a",))))
    assert got and got[0].startswith("🧠")


def test_no_emitter_is_not_a_crash():
    T.emit_step(None, "cid", T.memory_step(_Recall()))


# ── one call must not read as two ───────────────────────────────────────────

def test_the_preload_result_is_past_tense_and_emitted_once():
    """🔴 THE BUG ANANTH CAUGHT. The stream showed:

        ◌ preload: running rag, appeals_get_playbook, healthcare_query
        ... rag's own chatter, 48 candidates, 15 passages ...
        ◌ Pre-loading evidence before reasoning: rag · appeals_get_playbook
        ✓ rag → 15 passage(s)

    One rag call, two reports, and the SECOND one phrased as if starting. He
    read it as rag running twice before react, and that reading was reasonable.
    Intent and result are both worth emitting — a 12s preload with no line
    looks frozen — but they must be tensed so nobody has to count."""
    started = T.preload_step(("rag", "appeals_get_playbook"), (), ())
    done = T.preload_done_step([{"tool": "rag", "ok": True, "summary": "15 passages"}], 12.4)
    assert "running" in started.headline
    assert "preloaded" in done.headline and "running" not in done.headline
    assert "Pre-loading" not in done.headline
    assert started.stage != done.stage


def test_tools_that_returned_nothing_are_counted_in_the_headline():
    """A timed-out tool must not hide inside a success line."""
    s = T.preload_done_step([{"tool": "rag", "ok": True, "summary": "15 passages"},
                             {"tool": "healthcare_query", "ok": False, "summary": ""}])
    assert "1 returned nothing" in s.headline
    assert any("healthcare_query" in d and "returned nothing" in d for d in s.detail)


def test_an_all_empty_preload_says_so_rather_than_claiming_success():
    s = T.preload_done_step([{"tool": "rag", "ok": False, "summary": ""}])
    assert "returned nothing" in s.headline and "✓" not in s.headline


def test_no_preload_at_all_is_distinct_from_an_empty_one():
    assert "react starts with no evidence" in T.preload_done_step([]).headline
