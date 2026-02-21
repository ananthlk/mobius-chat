"""Unit tests for mobius_parse: TaskPlan JSON parsing, fallbacks, modality mapping."""
import pytest

from app.planner.mobius_parse import parse_task_plan_from_json


VALID_MINIMAL = '''
{
  "message_summary": "User asks about eligibility",
  "subquestions": [
    {"id": "sq1", "text": "What are the eligibility criteria for care management?"}
  ],
  "tasks": [],
  "clarifications": [],
  "retry_policy": {},
  "safety": {}
}
'''


def test_parse_valid_minimal_json():
    """Valid minimal TaskPlan JSON returns TaskPlan with subquestions."""
    result = parse_task_plan_from_json(VALID_MINIMAL)
    assert result is not None
    assert len(result.subquestions) == 1
    assert result.subquestions[0].id == "sq1"
    assert result.subquestions[0].text == "What are the eligibility criteria for care management?"
    assert result.subquestions[0].kind == "non_patient"


def test_parse_fallback_dict_in_capabilities():
    """Fallbacks with {"if": "no_evidence", "then": "web"} are parsed correctly."""
    json_str = '''
    {
      "message_summary": "test",
      "subquestions": [
        {"id": "sq1", "text": "Look this up", "capabilities_needed": {"primary": "rag", "fallbacks": [{"if": "no_evidence", "then": "web"}]}}
      ],
      "tasks": [{"id": "t1", "subquestion_id": "sq1", "modality": "rag", "fallbacks": [{"if": "no_evidence", "then": "web"}]}],
      "clarifications": [],
      "retry_policy": {},
      "safety": {}
    }
    '''
    result = parse_task_plan_from_json(json_str)
    assert result is not None
    assert len(result.subquestions) == 1
    caps = result.subquestions[0].capabilities_needed
    assert caps is not None
    assert "web" in caps.fallbacks


def test_parse_modality_web_scrape():
    """Modality 'web_scrape' maps to 'web' in task."""
    json_str = '''
    {
      "message_summary": "scrape request",
      "subquestions": [{"id": "sq1", "text": "Scrape https://example.com"}],
      "tasks": [{"id": "t1", "subquestion_id": "sq1", "modality": "web_scrape"}],
      "clarifications": [],
      "retry_policy": {},
      "safety": {}
    }
    '''
    result = parse_task_plan_from_json(json_str)
    assert result is not None
    assert len(result.tasks) == 1
    assert result.tasks[0].modality == "web"


def test_parse_modality_google_search():
    """Modality 'google_search' maps to 'web' in task."""
    json_str = '''
    {
      "message_summary": "search",
      "subquestions": [{"id": "sq1", "text": "Search for X"}],
      "tasks": [{"id": "t1", "subquestion_id": "sq1", "modality": "google_search"}],
      "clarifications": [],
      "retry_policy": {},
      "safety": {}
    }
    '''
    result = parse_task_plan_from_json(json_str)
    assert result is not None
    assert result.tasks[0].modality == "web"


def test_parse_malformed_json_returns_none():
    """Invalid JSON returns None, no crash."""
    result = parse_task_plan_from_json("{ invalid json")
    assert result is None

    result = parse_task_plan_from_json("not json at all")
    assert result is None


def test_parse_empty_returns_none():
    """Empty or whitespace input returns None."""
    assert parse_task_plan_from_json("") is None
    assert parse_task_plan_from_json("   ") is None


def test_parse_missing_subquestions_returns_none():
    """JSON without 'subquestions' key or with empty list returns None."""
    assert parse_task_plan_from_json('{"message_summary": "x"}') is None
    assert parse_task_plan_from_json('{"subquestions": [], "message_summary": "x"}') is None


def test_parse_subquestion_missing_text_skipped():
    """Subquestion with no text is skipped; others retained."""
    json_str = '''
    {
      "message_summary": "mixed",
      "subquestions": [
        {"id": "sq1", "text": "Valid question"},
        {"id": "sq2"},
        {"id": "sq3", "text": ""},
        {"id": "sq4", "text": "Another valid"}
      ],
      "tasks": [],
      "clarifications": [],
      "retry_policy": {},
      "safety": {}
    }
    '''
    result = parse_task_plan_from_json(json_str)
    assert result is not None
    assert len(result.subquestions) == 2
    assert result.subquestions[0].text == "Valid question"
    assert result.subquestions[1].text == "Another valid"


def test_parse_wrapped_in_markdown_code_block():
    """JSON wrapped in ``` code block is extracted and parsed."""
    json_str = '''Some preamble
```json
{
  "message_summary": "x",
  "subquestions": [{"id": "sq1", "text": "Hello"}],
  "tasks": [],
  "clarifications": [],
  "retry_policy": {},
  "safety": {}
}
```
Some trailing text'''
    result = parse_task_plan_from_json(json_str)
    assert result is not None
    assert len(result.subquestions) == 1
    assert result.subquestions[0].text == "Hello"
