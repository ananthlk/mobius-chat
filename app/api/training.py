"""Training-mode telemetry endpoint.

POST /chat/training-event  — record a training outcome or graduation action.

Event types:
  training_completed         user finished all 5 steps
  training_skipped           user clicked "skip" on the consent screen
  training_dismissed         user clicked × before reaching graduation
  graduation_question_fired  user sent a question from the graduation screen

Metrics queryable via training_outcome_summary and training_graduation_funnel views.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.api.front_door import require_user
from app.services.pg_pool import get_pool

router = APIRouter(tags=["training"])

_VALID_TYPES = frozenset({
    "training_completed",
    "training_skipped",
    "training_dismissed",
    "graduation_question_fired",
})


class TrainingEventBody(BaseModel):
    event_type: str
    source: str | None = None  # chip|typed (graduation_question_fired only)
    text: str | None = None    # question text (graduation_question_fired only)


@router.post("/chat/training-event", status_code=204)
async def record_training_event(
    body: TrainingEventBody,
    user_id: str = Depends(require_user),
) -> None:
    if body.event_type not in _VALID_TYPES:
        return  # silently ignore unknown types for forward-compat
    pool = await get_pool()
    if not pool:
        return
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO training_events (user_id, event_type, source, text)
            VALUES ($1, $2, $3, $4)
            """,
            user_id,
            body.event_type,
            body.source,
            body.text[:2000] if body.text else None,  # guard against runaway text
        )
