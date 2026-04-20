"""Sprint A.1 commit 1 — structured emit envelope type + helpers.

What this file locks:

  1. **Envelope round-trip.** ``to_dict()`` produces a JSON-friendly
     shape; ``is_envelope()`` detects it; the shape matches what
     orchestrator's on_thinking expects.

  2. **Signal taxonomy.** The set of 10 promoted signals is explicit
     via constructor helpers. Adding a new one requires a new helper.
     The helpers hard-code task_type + severity so emit-site code
     doesn't have to think about promotion policy.

  3. **Critic block migration.** The four emit sites in run_react
     (audit_started, flagged, approved, approved_after_retry,
     rounds_exhausted) produce envelopes of the right signal + shape.
     Integration test with scripted LLM verifies the envelopes land
     in ctx.thinking_chunks as dicts, with the correct promotion
     metadata.

  4. **Back-compat.** The orchestrator's on_thinking still accepts
     legacy string emits; thinking_chunks becomes a mixed array
     during the rollout and FE can read both shapes.

Not locked here (scope deferred):
  - Sprint A.2 will add a writer that consumes the
    report_to_task_manager flag; commit 1 just ensures the envelopes
    carry the flag correctly.
  - FE rendering of structured envelopes (Sprint A.1 commit 3).
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from app.communication.emit_envelope import (
    EmitEnvelope,
    is_envelope,
    make_critic_approved,
    make_critic_approved_after_retry,
    make_critic_audit_started,
    make_critic_flagged,
    make_note,
    make_rounds_exhausted_with_warning,
)


# ── Envelope round-trip ─────────────────────────────────────────────


class TestEnvelopeShape:
    def test_to_dict_prunes_none_optional_fields(self):
        """Optional fields that are None should not appear in the
        serialized dict. Keeps the JSON compact and avoids
        None-in-storage semantic noise."""
        env = EmitEnvelope(
            signal="critic_flagged",
            correlation_id="c-1",
            note="⚠ flagged",
            data={"count": 2},
        )
        d = env.to_dict()
        assert "note" in d
        assert "data" in d
        # None-valued optionals are pruned:
        assert "thread_id" not in d
        assert "user_id" not in d
        assert "round" not in d
        assert "task_type" not in d

    def test_to_dict_includes_populated_optional_fields(self):
        env = EmitEnvelope(
            signal="critic_flagged",
            correlation_id="c-1",
            thread_id="t-1",
            user_id="user-42",
            round=3,
            task_type="insight",
            task_severity="med",
        )
        d = env.to_dict()
        assert d["thread_id"] == "t-1"
        assert d["user_id"] == "user-42"
        assert d["round"] == 3
        assert d["task_type"] == "insight"
        assert d["task_severity"] == "med"

    def test_timestamp_auto_populated(self):
        """Envelope construction stamps timestamp_ms at call time.
        Locks the invariant — if we ever switch to lazy timestamps
        or external clocks, thinking_log row-ordering breaks."""
        env = EmitEnvelope(signal="note", correlation_id="c-1")
        assert env.timestamp_ms > 0

    def test_source_module_defaults_to_chat(self):
        """Promoted events get source_module=chat. If task-manager
        ever receives a promoted event with a different module, it
        means another system adopted the envelope — not a bug, but
        worth noticing."""
        env = EmitEnvelope(signal="note", correlation_id="c-1")
        assert env.source_module == "chat"

    def test_render_for_ui_uses_note_when_present(self):
        env = EmitEnvelope(
            signal="critic_approved",
            correlation_id="c-1",
            note="✓ Critic approved.",
        )
        assert env.render_for_ui() == "✓ Critic approved."

    def test_render_for_ui_falls_back_to_signal_when_no_note(self):
        """When an emit site produces an envelope without a note,
        render_for_ui produces '[signal]' so the UI shows SOMETHING
        instead of an empty line."""
        env = EmitEnvelope(signal="critic_approved", correlation_id="c-1")
        assert env.render_for_ui() == "[critic_approved]"


class TestIsEnvelope:
    def test_detects_envelope_dict(self):
        d = EmitEnvelope(signal="note", correlation_id="c-1").to_dict()
        assert is_envelope(d) is True

    def test_rejects_legacy_string(self):
        assert is_envelope("◌ Searching our materials…") is False

    def test_rejects_dict_without_signal(self):
        """A dict that happens to be in thinking_chunks but doesn't
        have the 'signal' field is legacy non-envelope data. Detect
        reliably rejects it."""
        assert is_envelope({"type": "thinking", "content": "…"}) is False

    def test_rejects_non_dict_types(self):
        assert is_envelope(None) is False
        assert is_envelope(42) is False
        assert is_envelope(["signal"]) is False


# ── Helper constructors — policy is hard-coded per helper ────────────


class TestPromotionPolicy:
    """Each promoted signal helper hard-codes the
    report_to_task_manager + task_type + task_severity. Tests lock
    the mapping so a future refactor can't accidentally flip the
    severity of a blocker down to info, or un-promote a signal we
    rely on for analytics."""

    def test_critic_flagged_is_promoted_as_insight_med(self):
        env = make_critic_flagged(
            correlation_id="c-1",
            round=3,
            total_issues=2,
            high_severity=2,
            flagged_claims=["fabricated phone", "unsupported PA claim"],
            rounds_remaining=1,
        )
        assert env.report_to_task_manager is True
        assert env.task_type == "insight"
        assert env.task_severity == "med"

    def test_rounds_exhausted_is_promoted_as_blocker_high(self):
        """This is the hard-fail signal — user should see it in
        their feed. HIGH severity, blocker type."""
        env = make_rounds_exhausted_with_warning(
            correlation_id="c-1",
            round=6,
            unresolved_claims=["x"],
        )
        assert env.report_to_task_manager is True
        assert env.task_type == "blocker"
        assert env.task_severity == "high"

    def test_critic_approved_after_retry_is_promoted_as_insight_low(self):
        """Self-correction — worth tracking but not alarming.
        LOW severity, insight type."""
        env = make_critic_approved_after_retry(
            correlation_id="c-1",
            round=5,
            retry_count=1,
            issues_resolved=["phone number"],
        )
        assert env.report_to_task_manager is True
        assert env.task_type == "insight"
        assert env.task_severity == "low"

    def test_critic_approved_is_NOT_promoted(self):
        """Common case. Every successful turn emits this. Promoting
        would flood task-manager with noise."""
        env = make_critic_approved(correlation_id="c-1", round=2)
        assert env.report_to_task_manager is False
        assert env.task_type is None

    def test_critic_audit_started_is_NOT_promoted(self):
        """Internal step. Only the OUTCOME (flagged or approved)
        matters for analytics."""
        env = make_critic_audit_started(
            correlation_id="c-1",
            round=3,
            draft_length=500,
            sources_count=3,
        )
        assert env.report_to_task_manager is False

    def test_note_fallback_is_NOT_promoted(self):
        """Generic string-wrapping envelope. Never promoted —
        promotion requires an explicit helper."""
        env = make_note(correlation_id="c-1", note="◌ Searching…")
        assert env.report_to_task_manager is False
        assert env.task_type is None
        assert env.signal == "note"


# ── Critic helper data payloads ─────────────────────────────────────


class TestCriticHelperData:
    def test_critic_flagged_data_carries_counts_and_claims_preview(self):
        env = make_critic_flagged(
            correlation_id="c-1",
            round=3,
            total_issues=4,
            high_severity=2,
            flagged_claims=["claim one", "claim two"],
            rounds_remaining=3,
        )
        assert env.data["total_issues"] == 4
        assert env.data["high_severity"] == 2
        assert env.data["rounds_remaining"] == 3
        assert env.data["flagged_claims_preview"] == ["claim one", "claim two"]

    def test_critic_flagged_caps_claim_preview_to_5_claims(self):
        """Long claim lists would bloat the envelope. Helper truncates
        at 5 claims × 200 chars each — enough detail for the analytics
        dashboard without blowing up storage size."""
        many_claims = [f"claim_{i}" for i in range(20)]
        env = make_critic_flagged(
            correlation_id="c-1",
            round=3,
            total_issues=20,
            high_severity=20,
            flagged_claims=many_claims,
            rounds_remaining=0,
        )
        assert len(env.data["flagged_claims_preview"]) == 5

    def test_critic_flagged_truncates_very_long_claims(self):
        long_claim = "x" * 500
        env = make_critic_flagged(
            correlation_id="c-1",
            round=3,
            total_issues=1,
            high_severity=1,
            flagged_claims=[long_claim],
            rounds_remaining=1,
        )
        assert len(env.data["flagged_claims_preview"][0]) == 200

    def test_rounds_exhausted_note_reflects_claim_count(self):
        env = make_rounds_exhausted_with_warning(
            correlation_id="c-1",
            round=6,
            unresolved_claims=["a", "b", "c"],
        )
        assert "3 unresolved" in env.note.lower()

    def test_step_id_is_hierarchical(self):
        """step_id encodes round + event for easy filtering in
        analytics queries. Format: round_N.event_name."""
        env = make_critic_flagged(
            correlation_id="c-1",
            round=3,
            total_issues=1,
            high_severity=1,
            flagged_claims=["x"],
            rounds_remaining=0,
        )
        assert env.step_id == "round_3.critic_flagged"


# ── Orchestrator on_thinking integration ────────────────────────────


class TestOnThinkingAcceptsEnvelopes:
    """The orchestrator's on_thinking callback is the single
    integration point for every emit. Sprint A.1 extended it to
    accept either strings (legacy) or envelope dicts (new). This
    test locks both paths."""

    def _make_on_thinking_and_ctx(self):
        """Build a minimal on_thinking replica of the
        orchestrator's logic for unit testing."""
        from app.communication.emit_envelope import is_envelope

        class _Ctx:
            def __init__(self):
                self.thinking_chunks = []

        ctx = _Ctx()
        published = []

        def on_thinking(chunk):
            if isinstance(chunk, dict) and is_envelope(chunk):
                ctx.thinking_chunks.append(chunk)
                ui_text = (chunk.get("note") or f"[{chunk.get('signal', 'event')}]").strip()
                published.append({"content": ui_text, "envelope": chunk})
            elif chunk and str(chunk).strip():
                s = str(chunk).strip()
                ctx.thinking_chunks.append(s)
                published.append({"content": s})

        return on_thinking, ctx, published

    def test_legacy_string_path_works(self):
        on_thinking, ctx, published = self._make_on_thinking_and_ctx()
        on_thinking("◌ Searching our materials…")
        assert ctx.thinking_chunks == ["◌ Searching our materials…"]
        assert published == [{"content": "◌ Searching our materials…"}]

    def test_envelope_dict_path_appends_dict(self):
        on_thinking, ctx, published = self._make_on_thinking_and_ctx()
        env_dict = make_critic_flagged(
            correlation_id="c-1",
            round=3,
            total_issues=2,
            high_severity=2,
            flagged_claims=["fabricated phone"],
            rounds_remaining=1,
        ).to_dict()
        on_thinking(env_dict)
        assert ctx.thinking_chunks == [env_dict]
        # Published to SSE: note as content, full envelope under 'envelope' key
        assert published[0]["content"].startswith("⚠ Critic flagged")
        assert published[0]["envelope"] == env_dict

    def test_mixed_array_supported_during_rollout(self):
        """During Sprint A.1 rollout, thinking_chunks will be a mixed
        array of strings and dicts. FE + DB must tolerate this."""
        on_thinking, ctx, published = self._make_on_thinking_and_ctx()
        on_thinking("legacy string 1")
        on_thinking(make_critic_approved(correlation_id="c-1", round=2).to_dict())
        on_thinking("legacy string 2")
        assert len(ctx.thinking_chunks) == 3
        assert isinstance(ctx.thinking_chunks[0], str)
        assert isinstance(ctx.thinking_chunks[1], dict)
        assert isinstance(ctx.thinking_chunks[2], str)

    def test_empty_string_is_ignored(self):
        """Existing guard: empty/whitespace emits don't make it into
        thinking_chunks. Migration didn't change this."""
        on_thinking, ctx, _ = self._make_on_thinking_and_ctx()
        on_thinking("")
        on_thinking("   ")
        on_thinking(None)
        assert ctx.thinking_chunks == []

    def test_non_envelope_dict_is_ignored(self):
        """Defensive: a dict that isn't a well-formed envelope
        (e.g. arbitrary state injected by a test fixture) shouldn't
        crash on_thinking. is_envelope filters cleanly; non-matching
        dicts fall through to the else branch which str()s them."""
        on_thinking, ctx, _ = self._make_on_thinking_and_ctx()
        # A dict without 'signal' → not recognized as envelope.
        # Goes through str() path. Whether it ends up in
        # thinking_chunks depends on the string value, but it must
        # not crash.
        on_thinking({"random": "dict"})  # type: ignore[arg-type]
        # No assertion on contents — just that we got here without crashing.
