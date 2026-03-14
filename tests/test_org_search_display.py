"""Tests for org-name search confidence display (MCP server formatter)."""
import sys
import os
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../mobius-skills-mcp/app"))

try:
    from server import _format_org_search_results
    _HAS_MCP = True
except Exception:
    _HAS_MCP = False

pytestmark = pytest.mark.skipif(not _HAS_MCP, reason="MCP server not importable")


def _results_three():
    return [
        {"npi": "1033883731", "name": "DAVID LAWRENCE CENTER",
         "source": "nppes", "match_score": 1.0, "match_type": "exact"},
        {"npi": "1982867255", "name": "DAVID LAWRENCE MENTAL HEALTH CENTER INC",
         "source": "nppes", "match_score": 0.68, "match_type": "partial"},
        {"npi": "9999999999", "name": "LAWRENCE BEHAVIORAL SERVICES",
         "source": "nppes", "match_score": 0.22, "match_type": "fuzzy"},
    ]


def test_icons_present():
    out = _format_org_search_results("David Lawrence Center", _results_three(), 10)
    assert "●" in out   # exact
    assert "◐" in out   # partial
    assert "○" in out   # fuzzy


def test_npi_numbers_present():
    out = _format_org_search_results("David Lawrence Center", _results_three(), 10)
    assert "NPI 1033883731" in out
    assert "NPI 1982867255" in out
    assert "NPI 9999999999" in out


def test_confidence_labels_present():
    out = _format_org_search_results("David Lawrence Center", _results_three(), 10)
    assert "Exact match  ✓" in out
    assert "Partial match" in out
    assert "Fuzzy match" in out


def test_percentage_shown_for_partial_and_fuzzy():
    out = _format_org_search_results("David Lawrence Center", _results_three(), 10)
    assert "68%" in out
    assert "22%" in out


def test_no_percentage_on_exact_row():
    out = _format_org_search_results("David Lawrence Center", _results_three(), 10)
    exact_line = [l for l in out.splitlines() if "Exact match" in l][0]
    assert "%" not in exact_line


def test_clarification_prompt_when_multiple():
    out = _format_org_search_results("David Lawrence Center", _results_three(), 10)
    assert "Which one did you mean?" in out


def test_no_clarification_prompt_for_single_result():
    single = [_results_three()[0]]
    out = _format_org_search_results("David Lawrence Center", single, 10)
    assert "Which one did you mean?" not in out


def test_header_shows_query_and_count():
    out = _format_org_search_results("David Lawrence Center", _results_three(), 10)
    assert 'Found 3 possible matches for "David Lawrence Center"' in out


def test_single_result_header_singular():
    single = [_results_three()[0]]
    out = _format_org_search_results("Aspire", single, 10)
    assert "1 possible match" in out
    assert "matches" not in out


def test_results_respect_limit():
    out = _format_org_search_results("David Lawrence Center", _results_three(), 2)
    # Third result (fuzzy) should not appear
    assert "LAWRENCE BEHAVIORAL SERVICES" not in out


def test_missing_match_type_defaults_gracefully():
    results = [{"npi": "1111111111", "name": "TEST ORG", "source": "nppes",
                "match_score": 0.5}]  # no match_type key
    out = _format_org_search_results("Test", results, 10)
    assert "TEST ORG" in out
    assert "NPI 1111111111" in out
