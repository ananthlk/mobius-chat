"""The preload plan: what runs before react speaks, and what react may ask for.

Spec: docs/preload-loop-spec-v1.md
Ananth, 2026-09-12: "preload the top 2 + rag ... load the tool outputs + leave
a generalized list of the next 3 tool choices for react to suggest."
"""
from app.pipeline.v2.preload import (
    ALWAYS_PRELOAD, EXECUTE_RANKED, NEVER_PRELOAD, SUGGEST_N, plan,
)

# The real offer for the three-payer question, in estimate()'s rank order.
OFFER = ["refuse", "appeals_get_playbook", "healthcare_query",
         "appeals_lookup_rules", "document_upload_skill", "fetch_document",
         "ingest_url", "list_thread_document_uploads", "recall_evidence",
         "search_uploaded_document", "transform_previous_answer", "web_scrape",
         "rag"]


def test_rag_is_always_executed_whatever_it_ranks():
    """Ananth: "no we will always do rag". It sits 12th of 13 in this offer --
    tier=default, offered unconditionally, never ranked -- and it is the only
    tool that finds the answer to a payer-policy question."""
    p = plan(OFFER)
    assert "rag" in p.execute
    assert OFFER.index("rag") > 10, "fixture no longer reproduces the low rank"


def test_top_two_ranked_plus_rag():
    p = plan(OFFER)
    assert p.execute == ("rag", "appeals_get_playbook", "healthcare_query")
    assert len(p.execute) == EXECUTE_RANKED + len(ALWAYS_PRELOAD)


def test_the_next_three_are_offered_for_react_to_request():
    """WITHOUT THIS, PRELOAD IS A CAPABILITY REMOVAL. Today react may call
    anything in the manifest; under preload it gets what we chose. This session
    already shipped one capability removal -- the governor's unilateral stop,
    0 additions against 50 subtractions in 48h."""
    p = plan(OFFER)
    assert len(p.suggest) == SUGGEST_N
    assert p.suggest == ("appeals_lookup_rules", "fetch_document",
                         "list_thread_document_uploads")
    assert not set(p.suggest) & set(p.execute), "a tool cannot be both"


def test_side_effect_tools_are_never_preloaded():
    """Speculative execution must not WRITE. ingest_url pulls a URL into the
    corpus; document_upload_skill and transform_previous_answer act on state.
    Running them on a question nobody asked them to act on is a different class
    of mistake from a wasted retrieval."""
    p = plan(OFFER)
    for key in NEVER_PRELOAD:
        assert key not in p.execute, key
        assert key not in p.suggest, key


def test_the_gate_token_is_not_a_tool():
    """`refuse` is offered first by estimate() as a gate -- "a refusal is an
    answer". Executing it would be meaningless."""
    assert "refuse" not in plan(OFFER).execute


def test_every_excluded_tool_carries_its_reason():
    """A preload that ran the wrong tools and one that ran nothing look
    identical in an answer. "We never asked the right tool" must be a visible
    verdict, not an absence."""
    p = plan(OFFER)
    assert p.excluded
    for key, why in p.excluded:
        assert why and isinstance(why, str), key
    covered = set(p.execute) | set(p.suggest) | {k for k, _ in p.excluded}
    assert set(OFFER) <= covered, set(OFFER) - covered


def test_rank_order_is_estimates_and_is_not_recomputed():
    """estimate() ranks; the governor supplies gaps and budget. Two rankers
    would be two authors -- and tonight produced one silent disagreement
    between a decision and a re-derivation of it."""
    shuffled = ["rag", "healthcare_query", "appeals_get_playbook"]
    p = plan(shuffled)
    assert p.execute == ("rag", "healthcare_query", "appeals_get_playbook"), (
        "plan() reordered the offer instead of honouring it"
    )


def test_an_empty_offer_plans_nothing_and_says_so():
    p = plan([])
    assert p.is_empty and p.execute == () and p.suggest == ()


def test_an_offer_of_only_rag_still_executes_it():
    p = plan(["rag"])
    assert p.execute == ("rag",) and p.suggest == ()


def test_rag_is_not_executed_when_the_offer_withheld_it():
    """ALWAYS means "whatever it ranks", not "whether or not it was offered".

    estimate() withholds rag when the budget cannot carry it -- measured at
    budget_ms=18000: "rag withheld: worst case 20s against a 18s envelope".
    That refusal is BINDING. Executing it anyway would be the governor
    overruling Tool Manifest's budget arithmetic with a constant.
    """
    p = plan(["appeals_get_playbook", "healthcare_query", "fetch_document"])
    assert "rag" not in p.execute
    assert p.execute == ("appeals_get_playbook", "healthcare_query")


# ── the frame sections ──────────────────────────────────────────────────────

def test_a_tool_that_ran_and_found_nothing_is_reported_as_such():
    """"Ran and found nothing" and "was never run" are different facts and
    carry opposite advice. Omitting the empty one lets react assume it was
    never tried -- the never-searched / searched-and-empty collapse this whole
    contract exists to end."""
    from app.pipeline.v2.frame import preload_sections
    txt = "\n".join(preload_sections(
        [{"tool": "rag", "ok": True, "summary": "17 chunks"},
         {"tool": "healthcare_query", "ok": False, "summary": ""}], ()))
    assert "healthcare_query -> ran, returned nothing" in txt
    assert "rag -> 17 chunks" in txt


def test_the_suggestion_list_tells_react_how_to_ask():
    """A list of names react cannot act on is decoration."""
    from app.pipeline.v2.frame import preload_sections
    txt = "\n".join(preload_sections([], ("web_scrape", "fetch_document")))
    assert "web_scrape" in txt
    assert "Name one in your gap report" in txt
    assert "not choosing a tool this round" in txt


def test_no_preload_renders_no_sections():
    """Absent preload must leave the frame exactly as it was -- this ships dark
    and must be a no-op until it is switched on."""
    from app.pipeline.v2.frame import preload_sections
    assert preload_sections([], ()) == []
