"""Builtin skill: transform_previous_answer — no-retrieval text transform.

Phase 13.6 — conversation-aware planner. When the user asks the chat to
*reshape* the previous assistant answer ("convert this to an appeal
letter", "make it shorter", "rewrite for the credentialing team",
"turn this into an email", "what's the counter-argument"), the right
move is to operate on the prior turn's text — NOT to re-run retrieval.

Before this skill existed, those follow-ups fell through to
``search_corpus`` / ``lookup_authoritative_sources``, which returned
generic results unrelated to the actual prior answer, and the bot
either asked the user to re-paste the source content or produced a
context-blind response.

How it works
------------
The handler reads ``pipeline_ctx.last_turns`` (already populated by
the state-load stage) and grabs the most recent assistant message. It
then asks the LLM to apply ``transformation`` (free-text intent) to
that prior answer — no corpus call, no curator call. The returned
envelope carries ``signal=system_context`` because the answer is
synthesized from in-thread material, not from a retrieval source.

Notes for the planner manifest
------------------------------
This skill must be invoked when the user's message:
  - Contains a pronoun referring to prior content ("this", "that", "the
    above", "your last answer").
  - Asks for a transformation verb on existing content ("convert",
    "rewrite", "shorten", "lengthen", "bulletize", "summarize",
    "format as", "draft an X from this", "make it more formal").
  - Asks for a downstream artifact ("appeal letter", "email to provider
    rep", "memo for the credentialing team") that pairs naturally with
    the prior answer's substance.

It must NOT be invoked when the user is asking a fresh question that
happens to share a topic with the prior turn — that's a retrieval call.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.skills.registry import SkillCall, SkillEnvelope, SkillSpec, SourceRef, register

logger = logging.getLogger(__name__)


# Keep the source preview small — most appeal-letter / rewrite intents
# work on the bot's own last answer, which is rarely >8k chars. We cap
# at ~12k chars to leave headroom for the transformation prompt and
# response without blowing the planner-stage context budget.
_PREVIOUS_ANSWER_CHAR_BUDGET = 12_000


def _extract_previous_answer(call: SkillCall) -> tuple[str, str]:
    """Return ``(previous_answer, previous_user_question)``.

    Pulled from ``pipeline_ctx.last_turns`` (newest first per
    ``get_last_turn_messages``). Returns empty strings if no prior
    turn exists — the caller handles that as a recoverable error so
    the planner can re-route to a real retrieval skill.
    """
    ctx = getattr(call, "pipeline_ctx", None)
    last_turns = getattr(ctx, "last_turns", None) if ctx is not None else None
    if not last_turns:
        return "", ""

    # last_turns is newest-first per the storage layer. Walk for the
    # first turn that actually has assistant content (defensive — empty
    # strings appear when the prior turn errored out).
    for turn in last_turns:
        if not isinstance(turn, dict):
            continue
        ans = (turn.get("assistant_content") or "").strip()
        if ans:
            return ans, (turn.get("user_content") or "").strip()
    return "", ""


def _build_prompt(
    previous_answer: str,
    previous_user_question: str,
    transformation: str,
    user_message: str,
) -> str:
    """Compose the LLM prompt that drives the transformation."""
    # Truncate from the END is wrong for letters that need the
    # opening; truncate from the MIDDLE keeps both head and tail. But
    # for healthcare-ops use cases (appeal letter, email, summary),
    # the head carries the operative facts. Keep head-only for now.
    src = previous_answer
    if len(src) > _PREVIOUS_ANSWER_CHAR_BUDGET:
        src = src[:_PREVIOUS_ANSWER_CHAR_BUDGET] + "\n\n[... truncated ...]"

    intent_hint = (transformation or user_message or "").strip()
    if not intent_hint:
        intent_hint = "Reformat the previous answer per the user's request."

    parts = [
        "You are transforming a prior assistant answer into a new artifact "
        "the user has requested. Use ONLY the prior answer as your source — "
        "do not invent facts, citations, claim numbers, dates, or contact "
        "details that are not in the prior answer. If the prior answer says "
        "something is unverified or pending, preserve that uncertainty in "
        "the output.",
        "",
        f"USER REQUEST: {user_message.strip() or intent_hint}",
        "",
        f"TRANSFORMATION INTENT: {intent_hint}",
        "",
    ]
    if previous_user_question:
        parts.append(f"ORIGINAL QUESTION (for context): {previous_user_question}")
        parts.append("")
    parts.append("PRIOR ASSISTANT ANSWER (the source to transform):")
    parts.append(src)
    parts.append("")
    parts.append(
        "Produce the requested artifact. Match the tone and format the user "
        "asked for. If the user asked for a letter or email, include "
        "appropriate salutation and sign-off placeholders (e.g., "
        "[Provider Name], [NPI], [Date]) where the prior answer did not "
        "supply specifics. Do not add a disclaimer about being an AI."
    )
    return "\n".join(parts)


def _run(call: SkillCall) -> SkillEnvelope:
    from app.services.doc_assembly import (
        RETRIEVAL_SIGNAL_NO_SOURCES,
        RETRIEVAL_SIGNAL_SYSTEM_CONTEXT,
    )

    inputs: dict[str, Any] = call.inputs or {}
    transformation = str(inputs.get("transformation") or "").strip()
    user_message = (call.user_message or call.question or "").strip()

    previous_answer, previous_user_question = _extract_previous_answer(call)

    if not previous_answer:
        # No prior turn to transform — surface a clear, short message
        # so the user knows why we couldn't act, and the planner can
        # decide whether to escalate to a real retrieval call instead.
        # Signal=no_sources because we produced nothing grounded.
        return SkillEnvelope(
            text=(
                "I can't transform a previous answer because this looks "
                "like the first turn in the thread — there's no prior "
                "content to reshape. Could you paste the source text "
                "you'd like converted, or ask the underlying question "
                "first so I can produce an answer to transform?"
            ),
            sources=[],
            signal=RETRIEVAL_SIGNAL_NO_SOURCES,
            extra={"transform_skipped_reason": "no_previous_turn"},
        )

    if call.emitter:
        # Short, action-shaped. Mirrors other skills' emitter style.
        intent_for_emit = (transformation or user_message)[:60]
        call.emitter(f"◌ Reshaping previous answer: {intent_for_emit}")

    try:
        from app.services.llm_provider import get_llm_provider

        provider = get_llm_provider()
        prompt = _build_prompt(
            previous_answer=previous_answer,
            previous_user_question=previous_user_question,
            transformation=transformation,
            user_message=user_message,
        )
        raw_ans, llm_usage = asyncio.run(provider.generate_with_usage(prompt))
        answer = (raw_ans or "").strip()
        if not answer:
            return SkillEnvelope(
                text=(
                    "I tried to reshape the previous answer but the "
                    "transformation came back empty. Try rephrasing the "
                    "request (e.g., 'rewrite the above as a formal appeal "
                    "letter to Sunshine Health')."
                ),
                signal=RETRIEVAL_SIGNAL_NO_SOURCES,
                extra={"transform_skipped_reason": "empty_llm_response"},
            )

        # The "source" of this answer is the prior turn itself. We
        # surface that as a SourceRef so consumers (and the user) can
        # see this is grounded in the conversation, not in a fresh
        # retrieval. ``source_type=conversation`` is new but harmless
        # — front-end code that doesn't know it falls through to the
        # generic source pill.
        return SkillEnvelope(
            text=answer,
            sources=[
                SourceRef(
                    document_name="Previous answer in this thread",
                    source_type="conversation",
                )
            ],
            usage=llm_usage,
            signal=RETRIEVAL_SIGNAL_SYSTEM_CONTEXT,
            extra={
                "transformation": transformation,
                "previous_answer_chars": len(previous_answer),
            },
        )

    except Exception as e:
        logger.warning("transform_previous_answer LLM call failed: %s", e)
        return SkillEnvelope(
            text=(
                "I couldn't reshape the previous answer right now "
                "(LLM call failed). Try again, or paste the source "
                "text you'd like converted directly."
            ),
            signal=RETRIEVAL_SIGNAL_NO_SOURCES,
            extra={"transform_skipped_reason": "llm_exception", "error": str(e)[:200]},
        )


register(
    SkillSpec(
        name="transform_previous_answer",
        description=(
            "Reshape the PREVIOUS assistant answer into a new artifact — "
            "no retrieval, no corpus call. The prior turn IS the source.\n"
            "Use when the user's message is a continuation that operates "
            "on prior content:\n"
            "  - Pronouns: 'this', 'that', 'the above', 'your last answer'\n"
            "  - Transformation verbs: convert, rewrite, shorten, lengthen,\n"
            "    bulletize, summarize, format as, draft from this\n"
            "  - Artifact requests pairing with prior substance:\n"
            "    'turn this into an appeal letter', 'email to provider rep',\n"
            "    'memo for credentialing', 'counter-argument', 'shorter\n"
            "    version', 'plain-English version'\n"
            "Do NOT use when the user is asking a fresh substantive\n"
            "  question (even if topically related) — that needs retrieval.\n"
            "Do NOT use on the very first turn of a thread — there is no\n"
            "  prior answer to transform.\n"
            "Returns: the reshaped artifact; sources cite the prior turn."
        ),
        handler=_run,
        inputs_schema={
            "type": "object",
            "properties": {
                "transformation": {
                    "type": "string",
                    "description": (
                        "Free-text description of how to reshape the prior "
                        "answer (e.g. 'formal appeal letter to Sunshine "
                        "Health', 'shorter bulleted version', 'email to "
                        "the provider rep'). If omitted, the user_message "
                        "itself is used as the intent."
                    ),
                },
            },
        },
        requires_jurisdiction=False,
        follow_up_capable=True,
        visible_to_planner=True,
    )
)
