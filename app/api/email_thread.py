"""Thread-level email — proxy from chat to mobius-skills/email.

Endpoint:
    POST /chat/thread/{thread_id}/email

Why this lives in mobius-chat (not the email skill directly):
- Auth: derive ``actor`` from the chat session, never trust the browser.
- DB access: assemble the transcript server-side from chat_turns.
- Idempotency: deterministic key from server state so a double-click is a
  replay, not a duplicate send.
- LLM: summary mode calls llm_manager.generate(stage="email_draft") in-process
  so the bandit + llm_calls analytics correlate with the chat thread.

The email skill on http://localhost:8013 enforces validation, suppression,
rate limits, persistence, and audit.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from typing import Any, Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.api.front_door import require_user
from app.db_client import db_query
from app.services import llm_manager

logger = logging.getLogger(__name__)

router = APIRouter(tags=["email"])

EMAIL_SKILL_URL = (os.environ.get("CHAT_SKILLS_EMAIL_URL") or "http://localhost:8013").rstrip("/")
EMAIL_HTTP_TIMEOUT_SEC = float(os.environ.get("CHAT_EMAIL_HTTP_TIMEOUT_SEC", "60"))


class EmailThreadRequest(BaseModel):
    to: list[str] = Field(..., min_length=1, description="Recipient addresses")
    scope: Literal["thread", "last"] = Field(
        "thread",
        description="'thread' = whole conversation; 'last' = last user+assistant exchange",
    )
    mode: Literal["summary", "full"] = Field(
        "summary",
        description="'summary' = LLM drafts a short email; 'full' = raw transcript in body",
    )
    confirm_before_send: bool = True


class EmailThreadResponse(BaseModel):
    sent: bool = False
    requires_confirmation: bool = False
    status: str | None = None
    message_id: str | None = None
    provider_message_id: str | None = None
    draft: dict[str, Any] | None = None
    error: str | None = None
    idempotent_replay: bool | None = None


# ─── transcript assembly ────────────────────────────────────────────────────


def _fetch_turns(thread_id: str) -> list[dict[str, Any]]:
    """Ordered turns for a thread. Each: {question, final_message, created_at, correlation_id}."""
    result = db_query(
        """
        SELECT correlation_id, question, final_message, created_at
        FROM chat_turns
        WHERE thread_id = :thread_id
        ORDER BY created_at ASC
        """,
        "chat",
        params={"thread_id": thread_id.strip()},
    )
    # db_query returns {"columns": [...], "rows": [tuple, ...]} — must zip
    # columns to keys (mirrors mobius-chat/app/storage/turns._rows_as_dicts).
    cols = result.get("columns") or []
    rows = result.get("rows") or []
    return [dict(zip(cols, r)) for r in rows]


def _format_transcript(turns: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for t in turns:
        ts = t.get("created_at")
        ts_str = ts.isoformat() if hasattr(ts, "isoformat") else str(ts or "")
        q = (t.get("question") or "").strip()
        a = (t.get("final_message") or "").strip()
        if q:
            parts.append(f"You [{ts_str}]:\n{q}")
        if a:
            parts.append(f"Mobius:\n{a}")
        parts.append("")
    return "\n".join(parts).strip()


def _last_exchange(turns: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Last user+assistant pair. We only have one row per turn (question+final_message),
    so 'last exchange' is just the most-recent turn that has a final_message."""
    for t in reversed(turns):
        if (t.get("final_message") or "").strip():
            return [t]
    return turns[-1:] if turns else []


# ─── summary drafting via in-process LLM manager ────────────────────────────


_SUMMARY_SYSTEM = """You draft professional emails summarizing a Mobius chat conversation.

Output ONLY a JSON object with exactly two fields:
{"subject": "...", "body": "..."}

Rules:
- Subject: under 120 chars, specific, no clickbait, no exclamation marks.
- Body: plain text, 2-5 short paragraphs, friendly-professional, no marketing language.
- Open with one sentence stating the topic, then the key findings/decisions, then a one-line closing.
- Do NOT invent facts not present in the transcript.
- Do NOT include any PHI (names+DOB, MRNs, SSNs, diagnoses).
- No markdown fences, no commentary outside the JSON object.
"""


async def _draft_summary(
    transcript: str,
    *,
    correlation_id: str,
    thread_id: str,
) -> dict[str, str]:
    """Call llm_manager.generate(stage='email_draft'). Returns {'subject', 'body'} or raises."""
    user_prompt = (
        "Summarize the following Mobius chat conversation as a professional email.\n\n"
        "=== CONVERSATION ===\n"
        f"{transcript}\n"
        "=== END ===\n\n"
        "Return JSON: {\"subject\": \"...\", \"body\": \"...\"}"
    )
    full_prompt = _SUMMARY_SYSTEM + "\n\n" + user_prompt
    try:
        text, _usage = await llm_manager.generate(
            full_prompt,
            stage="email_draft",
            max_tokens=1200,
            correlation_id=correlation_id,
            thread_id=thread_id,
        )
    except Exception as exc:
        logger.warning("email_thread llm_manager.generate failed: %s", exc)
        raise HTTPException(status_code=502, detail=f"LLM draft failed: {exc}") from exc

    parsed = _parse_json(text or "")
    if not parsed:
        logger.warning("email_thread LLM returned unparseable JSON; first 200 chars: %s",
                       (text or "")[:200])
        raise HTTPException(status_code=502, detail="LLM did not return parseable subject/body JSON")
    return parsed


def _parse_json(text: str) -> dict[str, str] | None:
    import json
    import re
    s = (text or "").strip()
    s = re.sub(r"^```(?:json)?\s*", "", s)
    s = re.sub(r"\s*```$", "", s)
    start = s.find("{")
    end = s.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        obj = json.loads(s[start : end + 1])
    except Exception:
        return None
    sub = (obj.get("subject") or "").strip()
    body = (obj.get("body") or "").strip()
    if not sub or not body:
        return None
    return {"subject": sub, "body": body}


# ─── endpoint ───────────────────────────────────────────────────────────────


def _idempotency_key(*, thread_id: str, last_correlation_id: str, scope: str,
                     mode: str, to: list[str], actor: str) -> str:
    """Deterministic key — same click yields same key, idempotent on the email skill."""
    parts = "|".join([
        thread_id,
        last_correlation_id or "",
        scope,
        mode,
        ",".join(sorted(to)),
        actor or "",
    ])
    return "thread-email-" + hashlib.sha256(parts.encode()).hexdigest()[:24]


def _post_email_skill(payload: dict) -> dict:
    """Synchronous POST to the email skill /email/send."""
    try:
        with httpx.Client(timeout=EMAIL_HTTP_TIMEOUT_SEC) as c:
            r = c.post(f"{EMAIL_SKILL_URL}/email/send", json=payload)
            if r.status_code >= 400:
                logger.warning("email skill HTTP %s: %s", r.status_code, (r.text or "")[:300])
                # Surface validation errors verbatim so the UI can show them
                detail = r.json() if r.headers.get("content-type", "").startswith("application/json") else r.text
                raise HTTPException(status_code=r.status_code, detail=detail)
            return r.json()
    except httpx.RequestError as exc:
        logger.warning("email skill connection failed: %s", exc)
        raise HTTPException(status_code=502, detail=f"email skill unreachable: {exc}") from exc


@router.post("/chat/thread/{thread_id}/email", response_model=EmailThreadResponse)
def email_thread(
    thread_id: str,
    body: EmailThreadRequest,
    user_id: str | None = Depends(require_user),
):
    """Email a chat thread or its last exchange. Honors confirm_before_send (default true).

    Server-side responsibilities:
      1. Auth → derives actor from session
      2. Loads transcript from chat_turns
      3. Picks scope (whole thread vs. last exchange)
      4. Drafts subject+body (LLM summary or raw transcript)
      5. Generates deterministic idempotency_key
      6. Calls the email skill chokepoint

    The email skill enforces idempotency / suppression / rate limits / audit.
    """
    if not thread_id or not thread_id.strip():
        raise HTTPException(status_code=400, detail="thread_id required")

    turns = _fetch_turns(thread_id)
    if not turns:
        raise HTTPException(status_code=404, detail="thread has no turns")

    selected = turns if body.scope == "thread" else _last_exchange(turns)
    if not selected:
        raise HTTPException(status_code=404, detail="nothing to email in this thread")

    transcript = _format_transcript(selected)
    last_corr = str(turns[-1].get("correlation_id") or "")

    actor = f"user:{user_id}" if user_id else "user:anonymous"

    if body.mode == "summary":
        # Summary uses chat's own llm_manager — same bandit, in-process
        try:
            drafted = asyncio.run(_draft_summary(
                transcript,
                correlation_id=last_corr,
                thread_id=thread_id,
            ))
        except RuntimeError:
            # Already inside a loop (shouldn't happen for sync FastAPI, but defensive)
            loop = asyncio.new_event_loop()
            try:
                drafted = loop.run_until_complete(_draft_summary(
                    transcript, correlation_id=last_corr, thread_id=thread_id,
                ))
            finally:
                loop.close()
        subject = drafted["subject"]
        body_text = drafted["body"]
    else:
        # Full transcript — straightforward
        scope_label = "Whole thread" if body.scope == "thread" else "Last exchange"
        subject = f"Mobius chat — {scope_label} ({len(selected)} turn{'s' if len(selected) != 1 else ''})"
        body_text = transcript

    key = _idempotency_key(
        thread_id=thread_id,
        last_correlation_id=last_corr,
        scope=body.scope,
        mode=body.mode,
        to=body.to,
        actor=actor,
    )

    payload = {
        "to": body.to,
        "subject": subject,
        "body": body_text,
        "sender": "system",
        "mode": "raw",  # always raw to the email skill — we already crafted (or not) here
        "idempotency_key": key,
        "actor": actor,
        "confirm_before_send": bool(body.confirm_before_send),
        "run_id": thread_id,
        "step_id": f"thread-email:{key[-12:]}",
    }
    res = _post_email_skill(payload)

    logger.info(
        "email_thread thread_id=%s scope=%s mode=%s actor=%s status=%s replay=%s",
        thread_id, body.scope, body.mode, actor,
        res.get("status"), res.get("idempotent_replay"),
    )

    return EmailThreadResponse(
        sent=bool(res.get("sent")),
        requires_confirmation=bool(res.get("requires_confirmation")),
        status=res.get("status"),
        message_id=res.get("message_id"),
        provider_message_id=res.get("provider_message_id"),
        draft=res.get("draft"),
        error=res.get("error"),
        idempotent_replay=res.get("idempotent_replay"),
    )
