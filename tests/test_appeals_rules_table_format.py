"""Tests for the appeals-rules table-format fix (Task #60, 2026-08-07).

Appeals rule section_hints (from appeals_find_carc/appeals_lookup_rules) used
to pass through as a custom format="appeals_rules" string with the data blob
untouched, relying on the enricher LLM to preserve that non-standard format
verbatim per a schema-instruction carve-out. Confirmed via a live card
(a796845a) that instruction doesn't reliably hold -- the model wrote
format="bullets" anyway, with the data blob orphaned underneath and nothing
rendered. Fixed by converting to format="table" (a real schema-enum value)
with a clean row structure, removing the reliance on model fidelity entirely.
"""
from __future__ import annotations

import json

from app.planner.schemas import Plan
from app.responder.final import _appeals_rules_table_data, _build_consolidator_input_json


def _rule(rule_id="COB.R001", name="Medicaid Payor of Last Resort", statement="stmt", argument="arg", authority=None):
    return {
        "rule_id": rule_id,
        "rule_name": name,
        "rule_statement": statement,
        "appeal_argument": argument,
        "authority": authority or {},
        "sub_rules": [{"type": "universal", "statement": "noise", "authority_tags": [], "facts": []}],
    }


class TestAppealsRulesTableData:
    def test_extracts_from_find_carc_matches_shape(self):
        # Rules carry an authority so the Authority column survives: since
        # 2026-08-10 a column that is empty for EVERY row is dropped to save
        # width on the card (see _appeals_rules_table_data). Without this the
        # table would legitimately come back with 3 headers, which would test
        # the drop rule rather than the find_carc shape extraction this case
        # is about. The drop rule has its own coverage below.
        data = {"matches": [{"carc": 22, "rules": [
            _rule("COB.R001", authority={"state": "fl_409_910_fs"}),
            _rule("COB.R003", name="TPL", authority={"state": "fl_409_910_fs"}),
        ]}]}
        out = _appeals_rules_table_data(data)
        assert out["headers"] == ["Rule", "Statement", "Appeal Argument", "Authority"]
        assert len(out["rows"]) == 2
        assert out["rows"][0][0] == "Medicaid Payor of Last Resort"

    def test_extracts_from_lookup_rules_flat_shape(self):
        data = {"carc": 22, "rules": [_rule("COB.R001")]}
        out = _appeals_rules_table_data(data)
        assert len(out["rows"]) == 1

    def test_row_content_matches_rule_fields(self):
        data = {"rules": [_rule(statement="Medicaid is last resort.", argument="No other coverage existed.")]}
        out = _appeals_rules_table_data(data)
        row = out["rows"][0]
        assert row[1] == "Medicaid is last resort."
        assert row[2] == "No other coverage existed."

    def test_authority_bits_joined(self):
        data = {"rules": [_rule(authority={"state": "fl_409_910_fs", "federal": None, "clinical": None})]}
        out = _appeals_rules_table_data(data)
        assert out["rows"][0][3] == "fl_409_910_fs"

    def test_no_authority_shows_em_dash(self):
        """A rule with no authority renders "—" — but only while the column
        survives. Pair it with a rule that HAS an authority; an all-empty
        Authority column is dropped entirely (2026-08-10), which is covered
        by test_all_empty_authority_column_is_dropped below."""
        data = {"rules": [
            _rule("COB.R001", authority={}),
            _rule("COB.R003", name="TPL", authority={"state": "fl_409_910_fs"}),
        ]}
        out = _appeals_rules_table_data(data)
        assert out["rows"][0][3] == "—"
        assert out["rows"][1][3] == "fl_409_910_fs"

    def test_all_empty_authority_column_is_dropped(self):
        """When EVERY row lacks an authority the column is removed rather than
        rendering a column of em-dashes — pure width cost on a card that wraps
        badly (Ananth screenshot review 2026-08-10, CARC 197 x FL Medicaid)."""
        data = {"rules": [_rule(authority={})]}
        out = _appeals_rules_table_data(data)
        assert out["headers"] == ["Rule", "Statement", "Appeal Argument"]
        assert len(out["rows"][0]) == 3

    def test_sub_rules_noise_not_in_row(self):
        """The nested sub_rules/authority_tags/facts structure that made the
        original data blob heavy must not leak into the clean table row."""
        data = {"rules": [_rule()]}
        out = _appeals_rules_table_data(data)
        row_text = " ".join(out["rows"][0])
        assert "authority_tags" not in row_text
        assert "sub_rules" not in row_text

    def test_falls_back_to_rule_id_when_no_name(self):
        data = {"rules": [{"rule_id": "X.1", "rule_statement": "s", "appeal_argument": "a"}]}
        out = _appeals_rules_table_data(data)
        assert out["rows"][0][0] == "X.1"

    def test_no_rules_returns_none(self):
        assert _appeals_rules_table_data({"matches": [{"carc": 22, "rules": []}]}) is None
        assert _appeals_rules_table_data({}) is None


class TestPreBuiltSectionConversion:
    def test_appeals_rules_hint_becomes_table_format(self):
        plan = Plan(subquestions=[])
        hints = [{
            "section_format": "appeals_rules",
            "label": "Appeal rules",
            "data": {"matches": [{"carc": 22, "rules": [_rule()]}]},
        }]
        raw = _build_consolidator_input_json(plan, [], "how do I appeal?", tool_section_hints=hints)
        payload = json.loads(raw)
        sections = payload["pre_built_sections"]
        assert len(sections) == 1
        assert sections[0]["format"] == "table"
        assert sections[0]["label"] == "Appeal rules"
        assert "headers" in sections[0]["data"]
        assert "rows" in sections[0]["data"]

    def test_appeals_rules_hint_with_no_rules_produces_no_section(self):
        plan = Plan(subquestions=[])
        hints = [{"section_format": "appeals_rules", "label": "Appeal rules", "data": {"matches": []}}]
        raw = _build_consolidator_input_json(plan, [], "q", tool_section_hints=hints)
        payload = json.loads(raw)
        assert "pre_built_sections" not in payload

    def test_other_custom_format_still_generic_passthrough(self):
        """appeals_playbook (not appeals_rules) must still use the generic
        pass-through path, unaffected by the appeals_rules-specific fix."""
        plan = Plan(subquestions=[])
        hints = [{"section_format": "appeals_playbook", "label": "Playbook", "data": {"deadline_appeal_days": 30}}]
        raw = _build_consolidator_input_json(plan, [], "q", tool_section_hints=hints)
        payload = json.loads(raw)
        sections = payload["pre_built_sections"]
        assert sections[0]["format"] == "appeals_playbook"
        assert sections[0]["data"] == {"deadline_appeal_days": 30}

    def test_table_rows_capped_at_max_rows(self):
        plan = Plan(subquestions=[])
        many_rules = [_rule(rule_id=f"R.{i}", name=f"Rule {i}") for i in range(250)]
        hints = [{"section_format": "appeals_rules", "label": "Appeal rules", "data": {"rules": many_rules}}]
        raw = _build_consolidator_input_json(plan, [], "q", tool_section_hints=hints)
        payload = json.loads(raw)
        data = payload["pre_built_sections"][0]["data"]
        assert len(data["rows"]) == 200
        assert data["truncated"] == 50
