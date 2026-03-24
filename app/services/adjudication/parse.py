"""Parse v2 adjudicator JSON from model output."""
from __future__ import annotations

import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)


def _parse_adjudicator_json_object(blob: str) -> dict[str, Any] | None:
    """Try stdlib json.loads, then json_repair (truncated / trailing commas / minor LLM corruption)."""
    t = (blob or "").strip()
    if not t:
        return None
    try:
        o = json.loads(t)
        if isinstance(o, dict):
            return o
    except json.JSONDecodeError:
        pass
    try:
        import json_repair

        o = json_repair.loads(t)
        if isinstance(o, dict):
            logger.debug("Adjudicator JSON parsed via json_repair")
            return o
    except Exception as e:
        logger.debug("Adjudicator json_repair failed: %s", e)
    return None


def parse_full_response(text: str, sub_scores_fallback: dict[str, Any]) -> dict[str, Any]:
    """Parse comprehensive adjudicator JSON response."""
    raw = (text or "").strip()
    raw = re.sub(r"```json\s*", "", raw)
    raw = re.sub(r"```\s*", "", raw)
    raw = raw.strip()

    data = _parse_adjudicator_json_object(raw)
    if data is not None:
        return data

    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if m:
        data = _parse_adjudicator_json_object(m.group())
        if data is not None:
            return data

    logger.warning("Could not parse adjudicator response: %s", raw[:200])
    return {
        "sub_scores": sub_scores_fallback,
        "overall_score": 0.5,
        "verdict": "PARTIAL",
        "rationale": "Adjudicator response parse error",
        "attribution": {
            "failure_stage": None,
            "failure_reason": None,
            "is_planner_fault": False,
            "is_rag_fault": False,
            "is_integrator_fault": False,
            "is_no_fault": True,
        },
        "flags": [],
    }
