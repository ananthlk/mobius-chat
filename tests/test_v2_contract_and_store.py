"""The v2 react contract and the thread evidence ledger.

Ananth, 2026-09-12: "lets get the contract for react structured.. over time
this is gold.. we also need it to store the facts/things it found useful vs not
useful (so that we dont have to send it again)" / "this is the most critical
thing we can build" / "you need to create your version for v2".
"""
from app.pipeline.v2 import store as S
from app.pipeline.v2.contract import CONTRACT_VERSION, parse, to_row


# ── v2's shape, not a typed copy of v1's ────────────────────────────────────

def test_facts_carry_provenance_not_chunk_numbers():
    """v1's keep=[1,4,7] is positional against the last tool result: it
    mis-attributes the moment a round makes two tool calls, and means nothing
    once the chunks are gone. A fact survives them and can be re-sent."""
    r = parse({"facts": [{"fact": "Molina uses ICM",
                          "document": "molina.pdf", "page": 102}]})
    assert r.facts[0].grounded
    assert r.facts[0].document == "molina.pdf" and r.facts[0].page == 102


def test_a_fact_without_a_document_is_kept_and_flagged():
    """Dropping it would hide that react answered ungrounded -- which is the
    thing worth finding. Keeping it silently would let it re-enter the next
    turn as a sourced claim."""
    r = parse({"facts": ["Molina uses ICM"]})
    assert r.facts[0].fact and not r.facts[0].grounded
    assert any("no document" in p for p in r.problems)


def test_v1_chunk_numbers_are_not_converted_into_facts():
    """A number is not a statement. Inventing fact text for it would fabricate
    provenance -- the failure mode this contract exists to end."""
    r = parse({"evidence_review": {"keep": [1, 4], "running_answer": "ra"}})
    assert r.facts == ()
    assert any("chunk numbers cannot become facts" in p for p in r.problems)
    assert r.running_answer == "ra"     # what CAN be carried, is


# ── which shape arrived is recorded, never assumed ──────────────────────────

def test_shape_seen_distinguishes_v1_v2_and_mixed():
    assert parse({"facts": []}).shape_seen == "v2"
    assert parse({"evidence_review": {"keep": []}}).shape_seen == "v1"
    assert parse({"facts": [], "ack": {"parts": []}}).shape_seen == "mixed"
    assert parse({"thought": "t"}).shape_seen == "none"


def test_a_v1_response_is_reported_as_carrying_nothing_forward():
    """The live prompt still asks for v1's shape. A contract that only
    accepted v2 would report every real turn as malformed and teach us
    nothing; one that accepted it silently would hide the migration."""
    assert any("nothing to carry to the next turn" in p
               for p in parse({"evidence_review": {}}).problems)


def test_parse_never_raises():
    for bad in (None, [], "text", 3, {"facts": "nope"}, {"ack": 7},
                {"facts": [{"page": "x"}]}, {"gaps": "nope"}):
        parse(bad)          # must not raise


def test_not_stated_is_not_false():
    assert parse({"thought": "t"}).is_complete is None
    assert parse({"is_complete": False}).is_complete is False
    assert parse({"next_round_worth_it": "yes"}).next_round_worth_it is None


# ── the stored row is the asset ─────────────────────────────────────────────

def test_the_row_stores_the_claims_and_what_was_unreadable():
    row = to_row(parse({"facts": ["ungrounded"], "evidence_review": {}}),
                 correlation_id="c", thread_id="t", round_index=2)
    assert row["contract_version"] == CONTRACT_VERSION
    assert row["problems"], "a turn with an unreadable shape is the one worth finding"
    assert row["shape_seen"] == "mixed"


# ── the ledger's identity parsing ───────────────────────────────────────────

def test_unpack_reads_the_label_form_the_frame_already_uses():
    """A store that only accepted the new shape would record nothing until
    every producer migrated."""
    assert S._unpack("Sunshine Provider Manual p38") == ("Sunshine Provider Manual", 38, "")
    assert S._unpack("Exhibit_II-A.pdf") == ("Exhibit_II-A.pdf", None, "")
    assert S._unpack({"document": "m.pdf", "page": 102, "fact": "f"}) == ("m.pdf", 102, "f")


def test_unpack_does_not_invent_a_page_from_arbitrary_text():
    doc, page, _ = S._unpack("Some Manual pXX")
    assert page is None and doc == "Some Manual pXX"


def test_unpack_on_empty_is_empty_not_a_crash():
    assert S._unpack("") == ("", None, "")
    assert S._unpack(None) == ("", None, "")


# ── the read that makes the write worth anything ────────────────────────────

def test_facts_from_reads_the_thread_ledger(monkeypatch):
    """Without this read the ledger is a table nobody opens -- the
    producer-with-no-consumer defect, with a schema."""
    from app.pipeline.v2 import blocks as B
    monkeypatch.setattr(
        "app.pipeline.v2.store.load_evidence",
        lambda tid: ([{"document": "m.pdf", "page": 102,
                       "fact": "Molina uses ICM", "label": "m.pdf p102"}],
                     ["Exhibit_II-A.pdf"]))

    class _C:
        message = "q"; thread_id = "t1"; react_trace_rounds = []
        user_profile = {}; org_name = ""
    class _S:
        open_gaps = ()
    f = B.facts_from(_C(), _S())
    assert any("Molina uses ICM" in u and "m.pdf p102" in u for u in f.useful), f.useful
    assert "Exhibit_II-A.pdf" in f.discarded


def test_a_store_outage_does_not_cost_the_turn_its_own_memory(monkeypatch):
    def boom(_tid):
        raise RuntimeError("db down")
    monkeypatch.setattr("app.pipeline.v2.store.load_evidence", boom)
    from app.pipeline.v2 import blocks as B
    class _C:
        message = "q"; thread_id = "t1"; user_profile = {}; org_name = ""
        react_trace_rounds = [{"enrichment": {"kept_docs": ["in-turn.pdf p1"]}}]
    class _S:
        open_gaps = ()
    f = B.facts_from(_C(), _S())
    assert "in-turn.pdf p1" in f.useful
