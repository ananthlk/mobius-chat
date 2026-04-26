"""Phase 13.6 — conversation-aware planner.

Tests for the ``transform_previous_answer`` skill and for the planner
prompt's threading of the prior assistant turn. Together these encode
the "do NOT re-retrieve when the user is just reshaping prior content"
contract.

What's mocked
-------------
The skill calls ``app.services.llm_provider.get_llm_provider`` which
hits an external LLM. We replace the provider with a fake whose
``generate_with_usage`` echoes the prompt back so the test asserts on
prompt construction (correct prior-answer interpolation, intent
threading, no-fact-fabrication clause). One additional test exercises
the LLM-failure branch.

What's exercised
----------------
1. Skill registration (name, follow-up flag, manifest visibility).
2. Empty / first-turn behavior — no prior answer => clear refusal.
3. Happy path — prior answer interpolated, transformation honored,
   sources cite the prior turn, signal=system_context.
4. Long prior answer truncation (head-keep, tail-drop with marker).
5. LLM failure path — graceful envelope, signal=no_sources.
6. Empty LLM response — graceful envelope.
7. Skill picks the NEWEST turn with non-empty assistant_content
   (skips empty-assistant turns at the top).
8. Planner prompt: previous assistant_content surfaces in full
   (~3000 char head) for the most recent turn, NOT truncated to 200.
9. Planner prompt: continuation guidance text appears.
10. Manifest includes ``transform_previous_answer`` description.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.skills import registry as skill_registry
from app.skills.builtin import transform_previous as skill_mod
from app.skills.registry import SkillCall


# ── Fakes ────────────────────────────────────────────────────────────


class _FakeProvider:
    """Stand-in for the LLM provider. Captures the prompt and returns
    a configurable response so tests can assert on prompt structure
    without making real LLM calls."""

    def __init__(self, response: str = "TRANSFORMED OUTPUT", raise_exc: Exception | None = None):
        self.response = response
        self.raise_exc = raise_exc
        self.last_prompt: str | None = None

    async def generate_with_usage(self, prompt, **kwargs):  # noqa: D401
        self.last_prompt = prompt
        if self.raise_exc:
            raise self.raise_exc
        return self.response, {"prompt_tokens": 100, "completion_tokens": 50}


def _ctx_with_turns(turns):
    """Build a minimal pipeline-ctx-like object carrying last_turns."""
    return SimpleNamespace(last_turns=turns)


def _call(
    *,
    user_message: str,
    transformation: str = "",
    last_turns=None,
):
    return SkillCall(
        name="transform_previous_answer",
        inputs={"transformation": transformation} if transformation else {},
        question=user_message,
        user_message=user_message,
        thread_id="t1",
        active_context=None,
        mode="copilot",
        emitter=None,
        pipeline_ctx=_ctx_with_turns(last_turns or []),
    )


# ── Registration ─────────────────────────────────────────────────────


def test_spec_registered():
    assert skill_registry.has("transform_previous_answer")
    spec = skill_registry.get("transform_previous_answer")
    assert spec.source == "builtin"
    assert spec.visible_to_planner is True
    assert spec.follow_up_capable is True
    # No jurisdiction merging — this skill does not retrieve.
    assert spec.requires_jurisdiction is False


def test_manifest_includes_skill_block():
    """Planner manifest must describe transform_previous_answer.

    Without this, the planner can't pick the skill even after we
    register it — the manifest is the planner's only source of truth
    for which tools exist.
    """
    from app.pipeline.tool_manifest import get_tool_manifest

    manifest = get_tool_manifest()
    assert "transform_previous_answer" in manifest
    # Spot-check: the skill's purpose phrase must be in the planner
    # context. If someone trims the description these asserts catch it.
    assert "Reshape" in manifest or "reshape" in manifest
    assert "appeal letter" in manifest.lower()


# ── First-turn / empty-history guard ─────────────────────────────────


def test_returns_clear_message_when_no_prior_turn():
    env = skill_mod._run(_call(
        user_message="convert this to an appeal letter",
        last_turns=[],
    ))
    assert env.signal == "no_sources"
    assert "first turn" in env.text.lower() or "no prior" in env.text.lower()
    assert env.extra.get("transform_skipped_reason") == "no_previous_turn"


def test_returns_clear_message_when_prior_turn_assistant_empty():
    """If the only prior turn has empty assistant_content, treat as
    no-prior — don't try to transform an empty string."""
    env = skill_mod._run(_call(
        user_message="rewrite this",
        last_turns=[
            {"user_content": "u1", "assistant_content": ""},
        ],
    ))
    assert env.signal == "no_sources"
    assert env.extra.get("transform_skipped_reason") == "no_previous_turn"


def test_skips_empty_top_turn_and_uses_first_nonempty():
    """If the most-recent turn assistant_content is empty (e.g. error
    last turn), walk to the next turn that actually has content."""
    fake = _FakeProvider(response="LETTER")
    last_turns = [
        # Most recent — but assistant errored, empty content
        {"user_content": "ignore this", "assistant_content": "   "},
        # Older turn with real content
        {"user_content": "what's the PA timeline?",
         "assistant_content": "Sunshine requires 14 days for standard PA."},
    ]
    with patch.object(skill_mod, "get_llm_provider", create=True), \
         patch("app.services.llm_provider.get_llm_provider", return_value=fake):
        env = skill_mod._run(_call(
            user_message="convert to an appeal letter",
            last_turns=last_turns,
        ))
    assert env.signal == "system_context"
    assert "Sunshine requires 14 days" in (fake.last_prompt or "")


# ── Happy path ───────────────────────────────────────────────────────


def test_happy_path_includes_prior_answer_and_intent_in_prompt():
    fake = _FakeProvider(response="Dear Sunshine Health Appeals Department,\n...")
    prior_answer = (
        "For Sunshine Health Florida Medicaid: days 36–90 are denying "
        "as duplicate. File a Provider Claim Adjustment Request, do "
        "not void the original claim."
    )
    last_turns = [{
        "user_content": "split residential stay billing question",
        "assistant_content": prior_answer,
    }]
    with patch("app.services.llm_provider.get_llm_provider", return_value=fake):
        env = skill_mod._run(_call(
            user_message="can you convert this to an appeal letter",
            transformation="formal appeal letter to Sunshine Health",
            last_turns=last_turns,
        ))
    assert env.signal == "system_context"
    assert env.text.startswith("Dear Sunshine Health")
    # Source must cite the conversation, not a corpus doc
    assert len(env.sources) == 1
    src = env.sources[0].to_dict()
    assert src["document_name"] == "Previous answer in this thread"
    assert src["source_type"] == "conversation"

    # Prompt construction asserts
    prompt = fake.last_prompt or ""
    assert prior_answer in prompt
    assert "formal appeal letter to Sunshine Health" in prompt
    # Anti-hallucination clause is non-negotiable
    assert "do not invent facts" in prompt.lower()


def test_uses_user_message_as_intent_when_transformation_omitted():
    fake = _FakeProvider(response="OUT")
    last_turns = [{"user_content": "q", "assistant_content": "prior content here"}]
    with patch("app.services.llm_provider.get_llm_provider", return_value=fake):
        skill_mod._run(_call(
            user_message="make it shorter please",
            last_turns=last_turns,
        ))
    assert "make it shorter please" in (fake.last_prompt or "")


def test_extra_metadata_records_chars():
    fake = _FakeProvider(response="OK")
    prior = "x" * 500
    last_turns = [{"user_content": "q", "assistant_content": prior}]
    with patch("app.services.llm_provider.get_llm_provider", return_value=fake):
        env = skill_mod._run(_call(
            user_message="rewrite",
            transformation="bullet list",
            last_turns=last_turns,
        ))
    assert env.extra.get("previous_answer_chars") == 500
    assert env.extra.get("transformation") == "bullet list"


# ── Truncation ───────────────────────────────────────────────────────


def test_truncates_very_long_prior_answer():
    """Prior answers larger than the budget get clipped with a marker
    so we don't blow the LLM's input window."""
    fake = _FakeProvider(response="OUT")
    huge = "A" * (skill_mod._PREVIOUS_ANSWER_CHAR_BUDGET + 5000)
    last_turns = [{"user_content": "q", "assistant_content": huge}]
    with patch("app.services.llm_provider.get_llm_provider", return_value=fake):
        skill_mod._run(_call(
            user_message="shorten this",
            last_turns=last_turns,
        ))
    prompt = fake.last_prompt or ""
    # Marker present
    assert "[... truncated ...]" in prompt
    # And the prompt is bounded; full huge string is NOT inside
    assert prompt.count("A") <= skill_mod._PREVIOUS_ANSWER_CHAR_BUDGET + 100


def test_does_not_truncate_short_prior_answer():
    fake = _FakeProvider(response="OUT")
    last_turns = [{"user_content": "q", "assistant_content": "short answer"}]
    with patch("app.services.llm_provider.get_llm_provider", return_value=fake):
        skill_mod._run(_call(user_message="rewrite", last_turns=last_turns))
    assert "[... truncated ...]" not in (fake.last_prompt or "")


# ── Failure paths ────────────────────────────────────────────────────


def test_handles_llm_exception_gracefully():
    fake = _FakeProvider(raise_exc=RuntimeError("LLM 503"))
    last_turns = [{"user_content": "q", "assistant_content": "prior"}]
    with patch("app.services.llm_provider.get_llm_provider", return_value=fake):
        env = skill_mod._run(_call(user_message="rewrite", last_turns=last_turns))
    assert env.signal == "no_sources"
    assert env.extra.get("transform_skipped_reason") == "llm_exception"
    assert "LLM 503" in env.extra.get("error", "")


def test_handles_empty_llm_response():
    fake = _FakeProvider(response="   ")
    last_turns = [{"user_content": "q", "assistant_content": "prior"}]
    with patch("app.services.llm_provider.get_llm_provider", return_value=fake):
        env = skill_mod._run(_call(user_message="rewrite", last_turns=last_turns))
    assert env.signal == "no_sources"
    assert env.extra.get("transform_skipped_reason") == "empty_llm_response"


# ── Defensive: malformed last_turns shapes ───────────────────────────


def test_handles_non_dict_turn_entries():
    """If something upstream pushes a malformed entry into last_turns,
    we shouldn't crash — just skip the bad entry and try the next."""
    fake = _FakeProvider(response="OK")
    last_turns = [
        "not-a-dict",  # garbage
        None,          # garbage
        {"user_content": "q", "assistant_content": "real prior"},
    ]
    with patch("app.services.llm_provider.get_llm_provider", return_value=fake):
        env = skill_mod._run(_call(user_message="rewrite", last_turns=last_turns))
    assert env.signal == "system_context"
    assert "real prior" in (fake.last_prompt or "")


def test_handles_missing_pipeline_ctx():
    """If pipeline_ctx is None entirely (legacy callers), behave as
    no-prior-turn rather than crashing."""
    call = SkillCall(
        name="transform_previous_answer",
        inputs={},
        question="rewrite this",
        user_message="rewrite this",
        thread_id="t",
        active_context=None,
        mode="copilot",
        emitter=None,
        pipeline_ctx=None,
    )
    env = skill_mod._run(call)
    assert env.signal == "no_sources"
    assert env.extra.get("transform_skipped_reason") == "no_previous_turn"


def test_emitter_called_when_supplied():
    fake = _FakeProvider(response="OK")
    emit_calls: list[str] = []
    last_turns = [{"user_content": "q", "assistant_content": "prior"}]
    call = SkillCall(
        name="transform_previous_answer",
        inputs={"transformation": "shorter"},
        question="shorten this",
        user_message="shorten this",
        thread_id="t",
        active_context=None,
        mode="copilot",
        emitter=emit_calls.append,
        pipeline_ctx=_ctx_with_turns(last_turns),
    )
    with patch("app.services.llm_provider.get_llm_provider", return_value=fake):
        skill_mod._run(call)
    assert any("Reshaping previous answer" in m for m in emit_calls)


# ── Planner prompt threading ─────────────────────────────────────────


def test_planner_prompt_threads_full_prior_assistant_turn():
    """The most-recent assistant_content must appear in the planner
    prompt at full length (≤3000 chars), not truncated to 200.

    Phase 13.6 acceptance test — before the fix, the planner saw only
    the first 200 chars of the prior answer and could not meaningfully
    reshape it.
    """
    from app.pipeline.context import PipelineContext
    from app.pipeline.react.prompts import build_reasoning_context

    long_answer = "Sunshine Health requires the following steps: " + ("X " * 600)
    assert len(long_answer) > 1000  # sanity

    ctx = PipelineContext(
        correlation_id="c", thread_id="t",
        message="convert this to an appeal letter",
    )
    ctx.merged_state = {"active": {}}
    ctx.effective_message = ctx.message
    ctx.last_turns = [
        {"user_content": "split stay billing question",
         "assistant_content": long_answer},
    ]
    out = build_reasoning_context(ctx, [], 1)
    # Full prior answer head (>>200 chars) must surface
    assert "Sunshine Health requires the following steps" in out
    # And the body — not just the first 200 chars
    assert out.count("X ") > 100, (
        "expected most of the long assistant_content to reach the planner"
    )


def test_planner_prompt_includes_continuation_guidance():
    """The planner manifest itself plus the recent-conversation block
    must explicitly tell the model: 'use transform_previous_answer for
    continuations, not retrieval'."""
    from app.pipeline.context import PipelineContext
    from app.pipeline.react.prompts import build_reasoning_context

    ctx = PipelineContext(
        correlation_id="c", thread_id="t",
        message="convert this to an appeal letter",
    )
    ctx.merged_state = {"active": {}}
    ctx.effective_message = ctx.message
    ctx.last_turns = [
        {"user_content": "u", "assistant_content": "a prior answer"},
    ]
    out = build_reasoning_context(ctx, [], 1)
    assert "transform_previous_answer" in out
    # Pronoun / transformation cue keywords surface, so the planner has
    # explicit anchors to recognize continuation requests
    assert "pronouns" in out.lower() or "this" in out.lower()


def test_planner_prompt_handles_no_last_turns():
    """First-turn safety — no last_turns should not crash and should
    not mention the continuation block."""
    from app.pipeline.context import PipelineContext
    from app.pipeline.react.prompts import build_reasoning_context

    ctx = PipelineContext(correlation_id="c", thread_id=None, message="hi")
    ctx.merged_state = {"active": {}}
    ctx.effective_message = ctx.message
    ctx.last_turns = []
    out = build_reasoning_context(ctx, [], 1)
    # No "Recent conversation" block when there's no history
    assert "Recent conversation" not in out


def test_planner_prompt_truncates_older_turns_short():
    """The most-recent turn gets a generous head budget; older turns
    stay short to keep the planner context bounded."""
    from app.pipeline.context import PipelineContext
    from app.pipeline.react.prompts import build_reasoning_context

    long_old = "OLD-TURN-CONTENT " * 500  # >>200 chars
    ctx = PipelineContext(correlation_id="c", thread_id="t", message="now")
    ctx.merged_state = {"active": {}}
    ctx.effective_message = ctx.message
    ctx.last_turns = [
        {"user_content": "newest", "assistant_content": "newest answer"},
        {"user_content": "older", "assistant_content": long_old},
    ]
    out = build_reasoning_context(ctx, [], 1)
    # Older turn must be truncated — full string should NOT appear
    assert out.count("OLD-TURN-CONTENT ") < 50
