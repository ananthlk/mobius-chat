"""Map user-provided context to failed sub-objectives and update the answer set.

Pre-integrate step: when the user shares info (e.g. codes, links, documents),
extract which failed sub-objectives it answers and merge into the answer set.
Also pre-fill from master_objective for prior-turn answers so resolve skips retrieval.
See docs plan: Plan + Answer Set multi-stage updates.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING

from app.state.master_objective import MasterObjective

if TYPE_CHECKING:
    from app.pipeline.context import PipelineContext

logger = logging.getLogger(__name__)


def update_answer_set_from_user_context(ctx: "PipelineContext") -> None:
    """If user_provided_context exists and we have failed/partial sub-objectives,
    ask LLM which subquestions the user's text answers; merge into answer_set, answers, and master_objective."""
    if not getattr(ctx, "user_provided_context", None) or not (ctx.user_provided_context or "").strip():
        return
    obj_raw = (ctx.merged_state or {}).get("master_objective")
    obj = MasterObjective.from_dict(obj_raw) if obj_raw else None
    if not obj:
        return
    failed = [so for so in obj.sub_objectives if so.status in ("failed", "partial")]
    if not failed:
        return
    plan = ctx.plan
    if not plan or not getattr(plan, "subquestions", None):
        return

    sq_by_id = {sq.id: sq for sq in plan.subquestions}
    failed_list = [{"id": so.id, "text": so.text} for so in failed]
    user_ctx = (ctx.user_provided_context or "").strip()[:2000]

    try:
        from app.chat_config import get_chat_config
        from app.services.llm_provider import get_llm_provider

        cfg = get_chat_config()
        prompt = f"""The user provided this information. Treat the user as authoritative—what they say is fact.

User-provided information:
{user_ctx}

These subquestions could not be answered from our materials:
{json.dumps(failed_list, indent=2)}

Map the user's text to subquestions it answers. If the user mentions codes (HCPCS, ICD), prior auth, coverage, or any facts that address a subquestion, include that mapping. Use the EXACT ids from the list above (e.g. "1", "2", "3"). Return JSON: {{"1": "answer text", "3": "answer text"}}. Extract and record the user's facts as answers. If none apply, return {{}}.

Output ONLY valid JSON, no other text."""

        provider = get_llm_provider()
        raw, _ = asyncio.run(provider.generate_with_usage(prompt))
        if not raw or not raw.strip():
            return
        text = raw.strip()
        if "```" in text:
            start = text.find("```")
            if start >= 0:
                start = text.find("\n", start) + 1
                end = text.find("```", start)
                if end > start:
                    text = text[start:end]
        data = json.loads(text)
        if not isinstance(data, dict):
            return
        for sq_id, answer_text in data.items():
            if not sq_id or not answer_text or not isinstance(answer_text, str):
                continue
            sq_id = str(sq_id).strip().lower().replace("sq", "")
            if not sq_id or sq_id not in sq_by_id:
                continue
            answer_text = str(answer_text).strip()
            if not answer_text:
                continue

            # Update answer_set
            answer_set = getattr(ctx, "answer_set", None) or {}
            answer_set[sq_id] = {"answer": answer_text, "source": "user_context", "status": "answered"}
            ctx.answer_set = answer_set

            # Update answers list (by index)
            for i, sq in enumerate(plan.subquestions):
                if sq.id == sq_id and i < len(ctx.answers):
                    answers = list(ctx.answers)
                    answers[i] = answer_text
                    ctx.answers = answers
                    break

            # Update master_objective
            updated_subs = []
            for so in obj.sub_objectives:
                if so.id == sq_id:
                    from app.state.master_objective import SubObjective
                    updated_subs.append(SubObjective(id=so.id, text=so.text, status="answered", answer=answer_text))
                else:
                    updated_subs.append(so)
            from datetime import datetime, timezone
            now = datetime.now(timezone.utc).isoformat()
            updated = MasterObjective(
                id=obj.id,
                created_at=obj.created_at,
                updated_at=now,
                status=obj.status,
                summary=obj.summary,
                sub_objectives=updated_subs,
                attempts=obj.attempts,
                last_user_ask=obj.last_user_ask,
            )
            if all(s.status == "answered" for s in updated_subs):
                updated.status = "solved"
            ctx.master_objective = updated.to_dict()
            ctx.merged_state = {**(ctx.merged_state or {}), "master_objective": ctx.master_objective}
            obj = updated
            logger.info("[user_context_resolution] Marked %s as answered from user-provided context", sq_id)
    except Exception as e:
        logger.warning("[user_context_resolution] Failed to map user context to sub-objectives: %s", e)


def prefill_answer_set_from_master_objective(ctx: "PipelineContext") -> None:
    """Pre-fill answer_set and answers from persisted master_objective (prior-turn answers).
    Enables resolve to skip retrieval for already-answered subquestions."""
    obj_raw = (ctx.merged_state or {}).get("master_objective")
    obj = MasterObjective.from_dict(obj_raw) if obj_raw else None
    if not obj:
        return
    plan = ctx.plan
    if not plan or not getattr(plan, "subquestions", None):
        return
    answered_by_id = {so.id: so for so in obj.sub_objectives if so.status == "answered" and (so.answer or "").strip()}
    if not answered_by_id:
        return
    answer_set = getattr(ctx, "answer_set", None) or {}
    answers = list(getattr(ctx, "answers", None) or [])
    # Ensure answers list has placeholders for each subquestion
    while len(answers) < len(plan.subquestions):
        answers.append("[No answer yet]")
    changed = False
    for i, sq in enumerate(plan.subquestions):
        so = answered_by_id.get(sq.id)
        if not so or sq.id in answer_set:
            continue
        ans = (so.answer or "").strip()
        if not ans:
            continue
        answer_set[sq.id] = {"answer": ans, "source": "master_objective", "status": "answered"}
        if i < len(answers):
            answers[i] = ans
            changed = True
    if changed:
        ctx.answer_set = answer_set
        ctx.answers = answers
