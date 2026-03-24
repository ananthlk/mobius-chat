"""Persist rows to adjudication_scores (analytics)."""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


async def insert_adjudication_score_row(record: dict[str, Any]) -> None:
    """Insert one adjudication_scores row; no-op if DB unavailable."""
    try:
        from app.services.pg_pool import get_pool

        pool = await get_pool()
        if not pool:
            return

        cols = [
            "correlation_id",
            "eval_run_id",
            "test_id",
            "question",
            "question_category",
            "tool_fired",
            "expected_tool",
            "planner_model",
            "rag_model",
            "integrator_model",
            "badge_model",
            "jurisdiction",
            "iterations",
            "legacy_path",
            "addresses_question",
            "completeness",
            "factual_consistency",
            "clarity",
            "actionability",
            "escalation_quality",
            "language_quality",
            "response_efficiency",
            "json_compliance",
            "grounding",
            "confidence_calibration",
            "phi_boundary",
            "clinical_boundary",
            "npi_accuracy",
            "org_match",
            "code_accuracy",
            "payer_accuracy",
            "policy_currency",
            "enrollment_accuracy",
            "roster_accuracy",
            "data_freshness",
            "source_authority",
            "context_accuracy",
            "pronoun_resolution",
            "overall_score",
            "verdict",
            "rationale",
            "flags",
            "failure_stage",
            "failure_reason",
            "is_planner_fault",
            "is_rag_fault",
            "is_integrator_fault",
            "is_no_fault",
            "adjudicator_model",
            "adjudicator_version",
            "used_llm",
            "used_heuristic",
        ]
        vals = [record.get(c) for c in cols]
        placeholders = ", ".join(f"${i + 1}" for i in range(len(cols)))
        col_list = ", ".join(cols)
        sql = f"INSERT INTO adjudication_scores ({col_list}) VALUES ({placeholders})"
        async with pool.acquire() as conn:
            await conn.execute(sql, *vals)
    except Exception as e:
        err = str(e).lower()
        if "does not exist" in err or "relation" in err:
            logger.debug("insert_adjudication_score_row: table missing: %s", e)
        else:
            logger.warning("insert_adjudication_score_row failed: %s", e)
