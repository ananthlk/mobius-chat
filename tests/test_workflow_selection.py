"""Tests for server-authored workflow selection groups (clarification_options)."""
from __future__ import annotations

from app.communication.workflow_selection import (
    build_npi_org_disambiguation_groups,
    merge_clarification_option_lists,
    normalize_selection_group,
    workflow_selection_group,
)


def test_normalize_selection_group_requires_choices():
    assert normalize_selection_group({}) is None
    assert normalize_selection_group({"slot": "x", "choices": []}) is None
    g = normalize_selection_group(
        {
            "slot": "route",
            "label": "Pick",
            "selection_mode": "single",
            "choices": [{"value": "rag", "label": "Policy docs"}],
        }
    )
    assert g is not None
    assert g["slot"] == "route"
    assert g["choices"][0]["value"] == "rag"


def test_normalize_multiple_sets_defaults():
    g = normalize_selection_group(
        {
            "slot": "locs",
            "label": "Locations",
            "selection_mode": "multiple",
            "choices": [
                {"value": "a", "label": "A"},
                {"value": "b", "label": "B"},
            ],
        }
    )
    assert g is not None
    assert g["selection_mode"] == "multiple"
    assert g["min_choices"] == 1
    assert g["max_choices"] == 2


def test_merge_clarification_option_lists_appends():
    a = [{"slot": "jurisdiction.payor", "label": "Payer", "selection_mode": "single", "choices": [{"value": "x", "label": "X"}]}]
    b = [{"slot": "npi_disambiguation", "label": "NPI", "selection_mode": "single", "choices": [{"value": "y", "label": "Y"}]}]
    out = merge_clarification_option_lists(a, b)
    assert len(out) == 2
    assert out[0]["slot"] == "jurisdiction.payor"
    assert out[1]["slot"] == "npi_disambiguation"


def test_build_npi_org_disambiguation_groups_empty_or_single():
    assert build_npi_org_disambiguation_groups([], "Acme") == []
    assert (
        build_npi_org_disambiguation_groups(
            [{"npi": "1", "name": "A", "match_type": "exact"}],
            "Acme",
        )
        == []
    )


def test_build_npi_org_disambiguation_groups_multi():
    rows = [
        {"npi": "1111111111", "name": "Acme East", "match_type": "exact"},
        {"npi": "2222222222", "name": "Acme West", "match_type": "partial"},
    ]
    groups = build_npi_org_disambiguation_groups(rows, "Acme")
    assert len(groups) == 1
    assert groups[0]["slot"] == "npi_disambiguation"
    assert groups[0]["selection_mode"] == "multiple"
    assert groups[0].get("min_choices") == 1
    assert len(groups[0]["choices"]) == 2
    assert groups[0]["choices"][0]["choice_id"] == "1111111111"


def test_workflow_selection_group_helper():
    g = workflow_selection_group(
        slot="test",
        label="T",
        choices=[{"value": "v", "label": "L"}],
        selection_mode="single",
    )
    assert g is not None
    assert g["choices"][0]["value"] == "v"
