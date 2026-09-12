"""The memory manager: one owner for what a turn remembers, and for how long.

Ananth, 2026-09-12: "YOU NEED A MEMORY MANAGER MODULE TO DO THIS UNLESS THERE
IS ALREADY ONE THAT WORKS".

There was not one. Three partial mechanisms existed and did not know about each
other -- ctx._evidence_memory (turn, chunks), persistence/memory.py (worker,
session state), thread_evidence (thread, facts). This module is the policy over
those tiers; store.py is its I/O.
"""
import pytest

from app.pipeline.v2 import memory as M


class _Ctx:
    def __init__(self, thread_id="t1", chunks=0, cid="c1"):
        self.thread_id = thread_id
        self.correlation_id = cid
        self._evidence_memory = [{"call": 1, "chunk": i} for i in range(chunks)]


def _rows(n_useful=1, n_bad=0):
    u = [{"document": f"d{i}.pdf", "page": 10 + i, "fact": f"fact {i}",
          "label": f"d{i}.pdf p{10+i}"} for i in range(n_useful)]
    u += [{"document": "x.pdf", "page": None, "fact": "", "label": "x.pdf"}
          for _ in range(n_bad)]
    return u


# ── tiers stay separate, because each answers a different question ──────────

def test_thread_facts_are_carried_as_text_with_provenance(monkeypatch):
    monkeypatch.setattr(M, "recall", M.recall)
    monkeypatch.setattr("app.pipeline.v2.store.load_evidence",
                        lambda tid: (_rows(2), ["junk.pdf"]))
    r = M.recall(_Ctx())
    assert r.facts[0] == "fact 0 [d0.pdf p10]"
    assert "junk.pdf" in r.not_useful


def test_turn_chunks_are_COUNTED_not_copied(monkeypatch):
    """They are already reachable this turn via recall_evidence and integrate.
    Re-rendering them into the prompt sends the same evidence twice -- the cost
    this whole exercise exists to remove."""
    monkeypatch.setattr("app.pipeline.v2.store.load_evidence",
                        lambda tid: ([], []))
    r = M.recall(_Ctx(chunks=17))
    assert r.turn_chunks == 17
    assert r.facts == () and r.not_useful == ()


def test_a_row_with_no_fact_text_degrades_to_its_label(monkeypatch):
    """It still says "this source was useful". Dropping it would forget a
    verdict we paid for."""
    monkeypatch.setattr("app.pipeline.v2.store.load_evidence",
                        lambda tid: (_rows(0, 1), []))
    assert M.recall(_Ctx()).facts == ("x.pdf",)


# ── unavailable is not empty ────────────────────────────────────────────────

def test_a_store_outage_is_named_not_silently_empty(monkeypatch):
    """An empty memory and an unreachable one produce the same empty list and
    carry opposite advice."""
    def boom(_t):
        raise RuntimeError("db down")
    monkeypatch.setattr("app.pipeline.v2.store.load_evidence", boom)
    r = M.recall(_Ctx())
    assert r.facts == ()
    assert r.unavailable and "unreachable" in r.unavailable[0]
    assert not r.is_cold, "an outage must not read as a new thread"


def test_no_thread_id_says_nothing_can_be_remembered(monkeypatch):
    r = M.recall(_Ctx(thread_id=""))
    assert any("nothing can be remembered" in u for u in r.unavailable)


def test_a_genuinely_new_thread_is_cold(monkeypatch):
    monkeypatch.setattr("app.pipeline.v2.store.load_evidence", lambda tid: ([], []))
    assert M.recall(_Ctx()).is_cold


# ── caps, because an unbounded memory becomes the context problem ───────────

def test_carried_facts_are_capped(monkeypatch):
    monkeypatch.setattr("app.pipeline.v2.store.load_evidence",
                        lambda tid: (_rows(40), [f"n{i}.pdf" for i in range(40)]))
    r = M.recall(_Ctx())
    assert len(r.facts) == M.CARRY_FACTS
    assert len(r.not_useful) == M.CARRY_NOT_USEFUL


# ── writing: grounded only ──────────────────────────────────────────────────

def test_ungrounded_facts_are_refused(monkeypatch):
    """A fact with no document cannot be checked later and would re-enter the
    next turn as an unsourced claim wearing the authority of memory."""
    seen = {}
    monkeypatch.setattr("app.pipeline.v2.store.record_evidence",
                        lambda tid, **kw: seen.update(kw) or len(kw.get("useful") or ()))
    from app.pipeline.v2.contract import Fact
    M.remember(_Ctx(), facts=(Fact("grounded", "d.pdf", 1), Fact("floating", "", None)))
    assert [f["fact"] for f in seen["useful"]] == ["grounded"]


def test_remember_without_a_thread_writes_nothing(monkeypatch):
    called = []
    monkeypatch.setattr("app.pipeline.v2.store.record_evidence",
                        lambda *a, **k: called.append(1))
    from app.pipeline.v2.contract import Fact
    assert M.remember(_Ctx(thread_id=""), facts=(Fact("f", "d.pdf", 1),)) == 0
    assert not called


def test_a_write_outage_does_not_raise(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr("app.pipeline.v2.store.record_evidence", boom)
    from app.pipeline.v2.contract import Fact
    assert M.remember(_Ctx(), facts=(Fact("f", "d.pdf", 1),)) == 0


# ── the manager is the only door ────────────────────────────────────────────

def test_blocks_reads_memory_not_the_store_directly():
    """A second reader of the store is a second policy about what a turn
    remembers -- the defect this module was created to prevent."""
    src = open("app/pipeline/v2/blocks.py").read()
    assert "import memory" in src
    assert "store.load_evidence" not in src
