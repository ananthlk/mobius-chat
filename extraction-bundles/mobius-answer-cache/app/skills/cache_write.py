"""cache_write skill handler — server-side.

Embeds the question, writes (correlation_id, embedding, answer,
metadata) into the backend, returns the candidate_id.

Source: derived from mobius-chat's
``app/services/cache_writer.py``. The "should we cache this?" gate
(retrieval signals, config_sha, source count, quality floor) stays
on the **chat** side — chat decides whether to invoke this skill.
This handler just persists what it's told to. That keeps the
service single-purpose and the gate logic close to the data it
inspects (chat's pipeline context).
"""
from __future__ import annotations

import logging
import time
from typing import Any

from app.backends import get_backend
from app.embedding import embed_question

logger = logging.getLogger(__name__)


def write_handler(
    *,
    correlation_id: str,
    thread_id: str | None,
    question: str,
    answer: str,
    skill_envelope: dict[str, Any],
    config_sha: str | None,
    filters: dict[str, Any],
    domain_tags: list[str],
    qc_passed: bool,
    thumbs_down: bool,
    caller: str,
) -> "CacheWriteResponse":  # type: ignore[name-defined]
    from app.main import CacheWriteResponse

    if not (question or "").strip() or not (answer or "").strip():
        # Defensive: chat should already have gated, but never write
        # an empty pair.
        return CacheWriteResponse(candidate_id="", embed_ms=0, write_ms=0)

    t_embed = time.perf_counter()
    embedding = embed_question(question)
    embed_ms = int((time.perf_counter() - t_embed) * 1000)

    backend = get_backend()
    t_write = time.perf_counter()
    candidate_id = backend.write(
        correlation_id=correlation_id,
        embedding=embedding,
        thread_id=thread_id,
        question=question,
        answer=answer,
        skill_envelope=skill_envelope,
        config_sha=config_sha,
        filters=filters,
        domain_tags=domain_tags,
        qc_passed=qc_passed,
        thumbs_down=thumbs_down,
        caller=caller,
    )
    write_ms = int((time.perf_counter() - t_write) * 1000)

    return CacheWriteResponse(
        candidate_id=candidate_id,
        embed_ms=embed_ms,
        write_ms=write_ms,
    )
