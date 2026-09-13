"""Facts must keep the source's wording, and its document name.

Ananth, 2026-09-12: "now in the instructions for facts, ask the llm to retain
as much of the exact verbiage as possible".

Wording fidelity IS verifiability. Deep Research's deterministic verifier scores
a claim by similarity to the text at the cited page, so a fact that stays close
to the source is confirmed and a paraphrase of the same true thing can fall
below the bar and be dropped as unsupported.
"""
from app.pipeline.v2.prompts import system_suffix


class _V2:
    orchestrator_version = "v2"


class _V1:
    orchestrator_version = "v1"


def test_the_instruction_asks_for_the_sources_own_words():
    t = system_suffix(_V2())
    assert "KEEP THE SOURCE'S OWN WORDING" in t
    assert "trim, do not rewrite" in t


def test_it_shows_a_good_fact_and_a_bad_one():
    """A rule without an example is followed loosely; the example is what gets
    copied, so the contrast has to be in the prompt."""
    t = system_suffix(_V2())
    assert "is a good fact" in t and "is a bad one" in t


def test_it_says_WHY_so_the_rule_survives_paraphrase_pressure():
    """A model told only "keep the wording" will still tidy. Told that tidying
    can get a true claim dropped, it has a reason not to."""
    t = system_suffix(_V2())
    assert "checked against the cited page" in t
    assert "dropped as unsupported" in t


def test_the_document_name_must_be_copied_exactly():
    """🔴 MEASURED IN THE STORED FACTS: 41 carried
    "FL-Care-Provider-Manual-Statewide-Medicaid-Managed-Care.pdf" and 7 carried
    "FL-Care-Provider-Manual-Statewide-Medicaid-M.pdf" — a document that does
    not exist. My own summary truncated the name to 44 chars and react copied
    the truncation. A fact citing a document not in the corpus is unverifiable
    however true it is."""
    t = system_suffix(_V2())
    assert "EXACTLY as it appears" in t
    assert "shortened or tidied filename" in t


def test_v1_gets_none_of_this():
    assert system_suffix(_V1()) == ""
