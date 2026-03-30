"""Unit tests for credentialing_assertion fact building and hashing."""

from app.storage.credentialing_assertions_pg import (
    assertion_sync_summary_line,
    facts_from_credentialing_state,
    material_hash_for,
)


def test_assertion_sync_summary_line_lists_all_nonzero():
    line = assertion_sync_summary_line(
        step_id="find_associated_providers",
        mode="copilot",
        added=2,
        deleted=1,
        validated=3,
        revised=1,
    )
    assert "2 added" in line
    assert "3 validated" in line
    assert "1 revised (new version)" in line
    assert "1 removed (closed)" in line
    assert "find_associated_providers" in line


def test_assertion_sync_summary_line_empty_changes():
    line = assertion_sync_summary_line(
        step_id="identify_org",
        mode="copilot",
        added=0,
        deleted=0,
        validated=0,
        revised=0,
    )
    assert "no assertion row changes" in line


def test_material_hash_stable():
    h1 = material_hash_for({"a": 1, "b": "x"})
    h2 = material_hash_for({"b": "x", "a": 1})
    assert h1 == h2


def test_facts_identify_org():
    facts = facts_from_credentialing_state(
        "identify_org",
        "Acme Health",
        org_npis=["1234567893"],
        locations=[],
        associated_providers={},
        active_roster={},
    )
    assert len(facts) == 1
    assert facts[0]["fact_kind"] == "org_npi"
    assert facts[0]["payload_json"]["npi"] == "1234567893"


def test_facts_find_locations():
    loc = {
        "location_id": "L1",
        "site_city": "Tampa",
        "site_address_line_1": "1 Main",
        "site_source": "org_nppes",
    }
    facts = facts_from_credentialing_state(
        "find_locations",
        "Acme",
        org_npis=[],
        locations=[loc],
        associated_providers={},
        active_roster={},
    )
    assert len(facts) == 1
    assert facts[0]["fact_kind"] == "location"
    assert facts[0]["payload_json"]["location_id"] == "L1"


def test_facts_provider_active_flag():
    assoc = {"LOC": [{"npi": "1000000001", "name": "Dr X", "association_likelihood": 80}]}
    active = {"LOC": [{"npi": "1000000001"}]}
    facts = facts_from_credentialing_state(
        "find_associated_providers",
        "Acme",
        org_npis=[],
        locations=[],
        associated_providers=assoc,
        active_roster=active,
    )
    assert len(facts) == 1
    assert facts[0]["payload_json"]["in_active_roster"] is True
