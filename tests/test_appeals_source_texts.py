"""Tests for the appeals-tool-output -> source_texts structural gap fix
(2026-08-07, Ananth: 'integrator should have access to everything react
collected'). Appeals tools (appeals_find_carc/appeals_lookup_rules) return
sources=[], so their rule text reached sections[] via pre_built_sections but
never source_texts -- meaning citations[] (which only ever draws from
source_texts) was structurally blind to appeals content."""
from __future__ import annotations

from app.pipeline.context import PipelineContext  # noqa: F401 -- import order avoids a circular import (see test_detail_ready.py)
from app.stages.integrate import _appeals_hint_pseudo_sources


def _rule(rule_id="COB.R001", statement="stmt text", argument="arg text"):
    return {"rule_id": rule_id, "rule_name": f"Rule {rule_id}", "rule_statement": statement, "appeal_argument": argument}


def test_extracts_rules_from_find_carc_matches_shape():
    """appeals_find_carc's data.matches[].rules[] shape."""
    hints = [{
        "section_format": "appeals_rules",
        "label": "Appeal rules",
        "data": {"matches": [{"carc": 22, "rules": [_rule("COB.R001"), _rule("COB.R003")]}]},
    }]
    out = _appeals_hint_pseudo_sources(hints)
    assert len(out) == 2
    assert out[0]["document_name"] == "Rule COB.R001"
    assert "stmt text" in out[0]["text"]
    assert "arg text" in out[0]["text"]


def test_extracts_rules_from_lookup_rules_flat_shape():
    """appeals_lookup_rules's data.rules[] shape (no matches wrapper)."""
    hints = [{
        "section_format": "appeals_rules",
        "label": "Appeal rules",
        "data": {"carc": 22, "rules": [_rule("COB.R001")]},
    }]
    out = _appeals_hint_pseudo_sources(hints)
    assert len(out) == 1
    assert out[0]["document_name"] == "Rule COB.R001"


def test_falls_back_to_rule_id_when_no_rule_name():
    hints = [{"data": {"rules": [{"rule_id": "X.1", "rule_statement": "s", "appeal_argument": "a"}]}}]
    out = _appeals_hint_pseudo_sources(hints)
    assert out[0]["document_name"] == "X.1"


def test_skips_rules_with_no_text():
    hints = [{"data": {"rules": [{"rule_id": "X.1"}]}}]
    assert _appeals_hint_pseudo_sources(hints) == []


def test_empty_or_none_hints_return_empty():
    assert _appeals_hint_pseudo_sources(None) == []
    assert _appeals_hint_pseudo_sources([]) == []


def test_non_appeals_hint_without_data_dict_ignored():
    hints = [{"section_format": "table", "label": "Rates", "table_headers": ["a"], "rows": [["1"]]}]
    assert _appeals_hint_pseudo_sources(hints) == []


def test_mixed_hints_only_extracts_appeals_shaped_ones():
    hints = [
        {"section_format": "table", "label": "Rates"},  # no data dict -> skipped
        {"data": {"matches": [{"rules": [_rule("A.1")]}]}},
        {"data": {"rules": [_rule("B.1")]}},
    ]
    out = _appeals_hint_pseudo_sources(hints)
    assert {o["document_name"] for o in out} == {"Rule A.1", "Rule B.1"}
