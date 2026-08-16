"""POST /chat/verify-claim — HTTP entry point for the verify_claim scaffold.

Distinct from the download endpoints and from the fetch_document chat skill:
Fact Store's certification sweep is NOT a chat conversation, so verify_claim
is exposed as a plain synchronous endpoint callable server-to-server. Reads
public corpus content only (PHI is refused at ingest per policy), so no auth
gate — the caller identity, when present, is audit-logged.

See app/services/claim_verification.py for the scaffold/judge contract.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.api.front_door import require_user
from app.services.claim_verification import judge_is_wired, verify_claim

logger = logging.getLogger(__name__)

router = APIRouter()


class VerifyClaimRequest(BaseModel):
    document_id: str
    claim: str
    page: Optional[int] = None


@router.post("/chat/verify-claim")
def verify_claim_endpoint(
    body: VerifyClaimRequest,
    user_id: str | None = Depends(require_user),
) -> dict[str, Any]:
    """Verify whether a claim is supported by a specific corpus document/page.

    Returns the §8.4 verdict schema. While Eval's grader is unwired the
    verdict is always low_coverage with status="judge_unwired" (loud — never
    a false agree); the page_text_chars field still proves the resolve +
    page-text substrate ran.
    """
    result = verify_claim(body.document_id, body.claim, body.page)
    logger.info(
        "verify_claim: caller=%s document_id=%s page=%s status=%s verdict=%s judge_wired=%s",
        user_id or "-", body.document_id, body.page,
        result.get("status"), result.get("verdict"), judge_is_wired(),
    )
    return result
