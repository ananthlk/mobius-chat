"""Fast unit tests for v2 adjudication (no LLM)."""
from __future__ import annotations

import pytest

from app.services.adjudication.parse import parse_full_response
from app.services.adjudication.stage_meta import build_stage_metadata
from app.services.adjudication.utils import (
    compute_overall_score,
    determine_verdict,
    detect_category,
    get_active_dimensions,
    get_stage_quality_score,
)


def test_detect_category_npi():
    cats = detect_category("What is NPI 1234567890?", "unknown", [])
    assert "npi_lookup" in cats


def test_get_active_dimensions_includes_universal():
    dims = get_active_dimensions(["general"])
    assert "addresses_question" in dims
    assert "phi_boundary" in dims


def test_compute_overall_score_safety_zero():
    s = compute_overall_score({"phi_boundary": 0.0, "addresses_question": 1.0})
    assert s == 0.0


def test_determine_verdict_phi_flag():
    assert determine_verdict(0.9, ["PHI_BOUNDARY_FAIL"]) == "FAIL"


def test_get_stage_quality_score_integrator_uses_overall():
    q = get_stage_quality_score("integrator", {"grounding": 0.2}, 0.88)
    assert q == 0.88


def test_parse_full_response_minimal_json():
    fb = {"a": None}
    out = parse_full_response('{"sub_scores":{"a":0.8},"overall_score":0.8,"verdict":"PASS","rationale":"ok","flags":[]}', fb)
    assert out.get("verdict") == "PASS"


def test_parse_full_response_json_repair_truncated_sub_scores():
    """Model output cut mid-object (max_tokens / stop) — json_repair can still recover sub_scores."""
    fb = {"addresses_question": None}
    truncated = """{
  "sub_scores": {
    "addresses_question": 1.0,
    "completeness": 0.0,
    "factual_consistency": 1.0,
    "clarity": 1.0,
    "actionability": 0.3,
    "escalation_quality": 0.7,
"""
    out = parse_full_response(truncated, fb)
    ss = out.get("sub_scores") or {}
    assert ss.get("addresses_question") == 1.0
    assert ss.get("escalation_quality") == 0.7
    assert "Adjudicator response parse error" not in (out.get("rationale") or "")


def test_parse_full_response_json_repair_trailing_comma():
    fb = {"x": None}
    text = '{"sub_scores":{"addresses_question":1.0,},"overall_score":0.9,"verdict":"PASS","rationale":"ok","flags":[]}'
    out = parse_full_response(text, fb)
    assert out.get("verdict") == "PASS"
    assert (out.get("sub_scores") or {}).get("addresses_question") == 1.0


def test_build_stage_metadata_from_usage():
    meta = build_stage_metadata(
        thinking_log=["payer=sunshine"],
        tool_fired="search_corpus",
        expected_tool="search_corpus",
        iterations=2,
        legacy_path=False,
        usage_breakdown=[
            {"stage": "integrator", "model": "m-int"},
            {"stage": "rag", "model": "m-rag"},
        ],
    )
    assert meta["integrator_model"] == "m-int"
    assert meta["rag_model"] == "m-rag"


def test_build_full_prompt_includes_long_sources_and_llm_chain():
    from app.services.adjudication.prompt import build_full_prompt

    long = "chunk-" + ("x" * 5000)
    p = build_full_prompt(
        question="What is the policy?",
        categories=["general"],
        active_dims=["addresses_question", "grounding"],
        thinking_log=["classifier: ok", "rag: 3 hits"],
        sources=[
            {
                "document_name": "Manual.pdf",
                "page_number": 12,
                "source_type": "corpus",
                "match_score": 0.88,
                "confidence": 0.9,
                "confidence_label": "approved_authoritative",
                "text": long,
            }
        ],
        answer="The answer shown to the user.",
        stage_metadata={"tool_fired": "search_corpus"},
        usage_breakdown=[
            {
                "stage": "react_1",
                "display_stage": "ReAct 1",
                "model": "m-planner",
                "input_tokens": 100,
                "output_tokens": 50,
                "router_reason": "exploration round",
            }
        ],
    )
    assert "chunk-" in p and "xxxx" in p
    assert "LLM_CHAIN" in p
    assert "ReAct 1" in p or "react_1" in p
    assert "confidence=0.9" in p
    assert "REASONING ROUNDS TO SCORE" in p
    assert "react_1" in p


def test_parse_full_response_includes_stage_scores():
    fb = {"a": None}
    text = '{"sub_scores":{"a":0.8},"overall_score":0.8,"verdict":"PASS","rationale":"ok","flags":[],"stage_scores":{"react_1":0.9,"react_2":0.7,"react_4":0.85}}'
    out = parse_full_response(text, fb)
    ss = out.get("stage_scores") or {}
    assert ss.get("react_1") == 0.9
    assert ss.get("react_2") == 0.7
    assert ss.get("react_4") == 0.85


def test_adjudicate_full_heuristic_only():
    from app.services.adjudication import adjudicate_full

    adj = adjudicate_full(
        question="test",
        answer="A reasonable length assistant answer for the heuristic path here.",
        thinking_log=[],
        sources=[],
        stage_metadata=build_stage_metadata(
            thinking_log=[],
            tool_fired="unknown",
            expected_tool="none",
            iterations=0,
            legacy_path=False,
            usage_breakdown=[],
        ),
        use_chat_llm=False,
    )
    assert adj["used_heuristic"] is True
    assert adj["used_llm"] is False
    assert "sub_scores" in adj
    assert adj["verdict"] in ("PASS", "PARTIAL", "FAIL")


@pytest.mark.asyncio
async def test_adjudicate_full_returns_stage_scores_when_llm_provides():
    from unittest.mock import AsyncMock, patch

    from app.services.adjudication import adjudicate_full_async

    mock_json = (
        '{"sub_scores":{"addresses_question":0.9,"phi_boundary":1.0},'
        '"overall_score":0.9,"verdict":"PASS","rationale":"ok","flags":[],'
        '"stage_scores":{"react_1":0.95,"react_2":0.8,"react_3":0.7,"react_4":0.85}}'
    )
    with patch(
        "app.services.llm_manager.generate",
        new_callable=AsyncMock,
        return_value=(mock_json, {}),
    ):
        adj = await adjudicate_full_async(
            question="Look up ICD-10 F32.1",
            answer="F32.1 is Major depressive disorder, single episode, moderate.",
            thinking_log=["Round 1: search corpus", "Round 2: healthcare_query"],
            sources=[],
            usage_breakdown=[
                {"stage": "react_1", "model": "m"},
                {"stage": "react_2", "model": "m"},
                {"stage": "react_3", "model": "m"},
                {"stage": "react_4", "model": "m"},
            ],
            use_chat_llm=True,
        )
    assert adj["used_llm"] is True
    ss = adj.get("stage_scores") or {}
    assert ss.get("react_1") == 0.95
    assert ss.get("react_2") == 0.8
    assert ss.get("react_3") == 0.7
    assert ss.get("react_4") == 0.85
