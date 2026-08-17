"""Task #106 (2026-08-16, Ananth, directly, live finding cid=a337ef54):
fetch_document's real content-attachment flow -- react_loop.py's generic
skill-registry dispatch branch stashes env.extra["attachment"] onto
ctx._pending_attachments for the NEXT round's LLM call, and respects
env.extra["golden"] is False so a document-found-but-content-not-yet-
attached result doesn't prematurely finalize the turn."""
from __future__ import annotations

from unittest.mock import patch

from app.pipeline.context import PipelineContext
from app.pipeline.react_loop import _execute_tool
from app.skills.registry import SkillEnvelope, SourceRef


def _make_ctx():
    ctx = PipelineContext(correlation_id="c1", thread_id="t1", message="find 59G-4.087 codes")
    ctx.merged_state = {}
    return ctx


class TestAttachmentStashing:
    def test_attachment_stashed_on_ctx(self):
        ctx = _make_ctx()
        env = SkillEnvelope(
            text="Found **59G-4.087 Coverage Policy**. Use the card below to download it.",
            signal="ok",
            sources=[SourceRef(document_name="59G-4.087", document_id="doc1", source_type="document", index=1, text="59G-4.087")],
            extra={
                "fetch_intent": True,
                "golden": False,
                "attachment": {"mime_type": "application/pdf", "data_b64": "ZmFrZQ==", "filename": "59G-4.087.pdf"},
            },
        )
        with patch("app.skills.registry.dispatch", return_value=env), \
             patch("app.skills.registry.has", return_value=True):
            result = _execute_tool("fetch_document", {"query": "59G-4.087"}, ctx, emitter=None)

        assert getattr(ctx, "_pending_attachments", None) == [
            {"mime_type": "application/pdf", "data_b64": "ZmFrZQ==", "filename": "59G-4.087.pdf"}
        ]
        assert result["golden"] is False

    def test_no_attachment_key_leaves_ctx_untouched(self):
        ctx = _make_ctx()
        env = SkillEnvelope(
            text="Found **Some Doc**. Use the card below to download it.",
            signal="ok",
            sources=[SourceRef(document_name="Some Doc", document_id="doc2", source_type="document", index=1, text="Some Doc")],
            extra={"fetch_intent": True, "golden": False},
        )
        with patch("app.skills.registry.dispatch", return_value=env), \
             patch("app.skills.registry.has", return_value=True):
            _execute_tool("fetch_document", {"query": "some doc"}, ctx, emitter=None)

        assert getattr(ctx, "_pending_attachments", None) is None

    def test_golden_explicitly_false_suppresses_inference(self):
        """The exact live bug: success + sources + signal='ok' used to
        infer golden=True regardless -- explicit False must win."""
        ctx = _make_ctx()
        env = SkillEnvelope(
            text="Found **Doc**. Use the card below to download it.",
            signal="ok",
            sources=[SourceRef(document_name="Doc", document_id="doc3", source_type="document", index=1, text="Doc")],
            extra={"golden": False},
        )
        with patch("app.skills.registry.dispatch", return_value=env), \
             patch("app.skills.registry.has", return_value=True):
            result = _execute_tool("fetch_document", {"query": "doc"}, ctx, emitter=None)
        assert result["golden"] is False
        assert result["golden_explicit"] is False

    def test_golden_true_still_works_for_other_skills(self):
        """Regression guard: explicit opt-in (golden=True) must still work
        for skills that DO want the auto-finalize (unrelated to this fix)."""
        ctx = _make_ctx()
        env = SkillEnvelope(
            text="Certified fact: yes.",
            signal="ok",
            sources=[SourceRef(document_name="Fact Store", document_id="doc4", source_type="document", index=1, text="fact")],
            extra={"golden": True},
        )
        with patch("app.skills.registry.dispatch", return_value=env), \
             patch("app.skills.registry.has", return_value=True):
            result = _execute_tool("fetch_document", {"query": "doc"}, ctx, emitter=None)
        assert result["golden"] is True


class TestRoundLoopConsumesAttachment:
    def test_pending_attachment_reaches_next_round_and_clears(self):
        """Full run_react() integration: round 1 sets ctx._pending_attachments
        (simulating a fetch_document result from a round-1 tool call), round 2's
        _call_llm_json must receive it via the attachments kwarg, and it must
        be cleared so round 3 (if any) doesn't re-send it."""
        from app.pipeline.react_loop import run_react

        ctx = PipelineContext(
            correlation_id="react-attach-test", thread_id=None,
            message="What are the codes in 59G-4.087?",
        )
        ctx.merged_state = {}
        ctx.last_turns = []
        ctx.effective_message = ctx.message

        captured_attachments = []
        reason_count = 0

        def fake_llm(system, user, max_tokens=800, ctx=None, stage="planner", **kwargs):
            nonlocal reason_count
            reason_count += 1
            captured_attachments.append(kwargs.get("attachments"))
            if reason_count == 1:
                return '{"thought": "Fetch the doc.", "tool": "fetch_document", "inputs": {"query": "59G-4.087"}, "is_complete": false}'
            return (
                '{"thought": "Have the content now.", "tool": null, "is_complete": true, '
                '"answer": "Codes are 99213-99215.", "sources": [], "confidence": "high"}'
            )

        def fake_execute(tool, inputs, ctx, round_num, emit_fn, tool_emitter, skip_retry=False, open_gaps=None):
            # Simulate what the real _execute_tool would do after dispatching
            # fetch_document with an attachment in env.extra.
            ctx._pending_attachments = [
                {"mime_type": "application/pdf", "data_b64": "ZmFrZQ==", "filename": "59G-4.087.pdf"}
            ]
            return {
                "tool": "fetch_document", "success": True,
                "result": "Found 59G-4.087. Use the card below to download it.",
                "signal": "ok", "sources": [], "usage": None, "golden": False,
            }

        with patch("app.pipeline.react.critic.critic_enabled", return_value=False), \
             patch("app.pipeline.react_loop._call_llm_json", side_effect=fake_llm), \
             patch("app.pipeline.react_loop._execute_tool_with_retry", side_effect=fake_execute):
            run_react(ctx, emitter=None)

        assert reason_count == 2
        # Round 1 (the tool-choice call) had nothing pending yet.
        assert captured_attachments[0] is None
        # Round 2 received the attachment stashed after round 1's tool call.
        assert captured_attachments[1] == [
            {"mime_type": "application/pdf", "data_b64": "ZmFrZQ==", "filename": "59G-4.087.pdf"}
        ]
        # Consumed -- cleared off ctx after round 2 read it.
        assert getattr(ctx, "_pending_attachments", None) is None
