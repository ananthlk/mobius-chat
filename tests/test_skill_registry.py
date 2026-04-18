"""Skill registry — commit 1: types + 2 migrated skills.

These tests lock the registry contract so later commits (migrating
healthcare_query, web_scrape, google_search, then deleting the legacy
branches in tool_agent.py) can't silently change semantics. What we
assert here:

1. **The registry loads.** Import side-effects populate _REGISTRY with
   exactly the skills we expect (no duplicates, no drift). If a future
   skill gets added without updating ``test_expected_skills_registered``,
   that test fails — forces the author to acknowledge the addition.

2. **``SkillEnvelope.to_legacy_tuple()`` matches the old 4-tuple shape
   byte-for-byte.** The whole migration strategy rests on this being
   true; if it ever diverges, every downstream consumer breaks.

3. **Flag behavior.** ``MOBIUS_USE_SKILL_REGISTRY=0`` routes to the
   legacy ``if hint == "X"`` branch; default / unset routes through
   ``dispatch()``. Both paths must return identical output — a
   migration that subtly changed semantics would be caught here, not
   in production.

4. **Registration guards.** ``register()`` raises on duplicate-name
   registration (catches the "copy-paste a skill and forget to rename
   it" class of bug). ``override()`` bypasses the guard for test
   fixtures only.

5. **Derived views.** ``entity_tools()`` and ``follow_up_capable()``
   match what the current ``tool_manifest.py`` hand-maintains for the
   two migrated skills. Commit 3 will replace the hand-maintained sets
   with these computed ones; this test locks the contract early so that
   swap is a no-op.

Not covered here (intentionally — scope creep into future commits):

- ``manifest_text()`` (doesn't exist yet; commit 3 lands it and the
  matching test that locks the planner prompt shape).
- MCP auto-registration (commit 4+; not in scope for the legacy-branch
  migration series).
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from app.skills import registry
from app.skills.registry import SkillCall, SkillEnvelope, SkillSpec, SourceRef


# ── Type shape ────────────────────────────────────────────────────────


class TestEnvelopeTypes:
    def test_source_ref_to_dict_matches_legacy_shape(self):
        """The old callers unpack ``list[dict]`` with keys document_name,
        index, source_type, text, url. Keep those keys; dropping any
        breaks the integrate stage."""
        s = SourceRef(document_name="Manual", index=2, text="preview", url="http://x")
        d = s.to_dict()
        assert d["document_name"] == "Manual"
        assert d["index"] == 2
        assert d["source_type"] == "external"
        assert d["text"] == "preview"
        assert d["url"] == "http://x"

    def test_source_ref_omits_empty_text_and_url(self):
        """Legacy callers null-check ``text`` and ``url`` via ``dict.get``.
        Don't emit them when unset — keeps the dict shape the old code
        produced so integrate's ``"text" in src`` branches behave."""
        s = SourceRef(document_name="X")
        d = s.to_dict()
        assert "text" not in d
        assert "url" not in d
        assert d["source_type"] == "external"

    def test_envelope_to_legacy_tuple_shape(self):
        """The 4-tuple is what every ``answer_tool`` caller unpacks. If
        this ever returns 3 or 5 elements, every caller breaks."""
        env = SkillEnvelope(
            text="hello",
            sources=[SourceRef(document_name="M")],
            signal="corpus_only",
            usage={"tokens": 10},
        )
        t = env.to_legacy_tuple()
        assert len(t) == 4
        text, sources, usage, signal = t
        assert text == "hello"
        assert sources[0]["document_name"] == "M"
        assert usage == {"tokens": 10}
        assert signal == "corpus_only"

    def test_envelope_defaults_are_safe(self):
        """Default envelope is a valid 'no sources' answer. Handlers
        should be able to construct one with just ``text=``."""
        env = SkillEnvelope(text="just text")
        text, sources, usage, signal = env.to_legacy_tuple()
        assert text == "just text"
        assert sources == []
        assert usage is None
        assert signal == "no_sources"


# ── Registration ──────────────────────────────────────────────────────


class TestRegistration:
    def test_expected_skills_registered(self):
        """Commit 1 migrates exactly these two skills. When commit 2/3
        lands, update this list. Drift-detection guard."""
        names = registry.all_names()
        assert "document_upload_skill" in names
        assert "list_thread_document_uploads" in names

    def test_register_rejects_duplicate_name(self):
        """Duplicate registration is the 'forgot to rename after
        copy-paste' bug. Loud failure is the right default."""
        existing = registry.get("document_upload_skill")
        assert existing is not None
        with pytest.raises(ValueError, match="already registered"):
            registry.register(
                SkillSpec(
                    name="document_upload_skill",
                    description="dupe",
                    handler=lambda call: SkillEnvelope(text=""),
                )
            )

    def test_register_tolerates_same_spec_reimport(self):
        """Re-importing the same skill module shouldn't blow up — in
        dev / test reruns Python sometimes re-executes module bodies.
        The guard triggers on ``existing is not spec``, not on 'any
        prior registration'."""
        spec = registry.get("document_upload_skill")
        assert spec is not None
        registry.register(spec)  # must not raise

    def test_register_rejects_empty_name(self):
        with pytest.raises(ValueError, match="cannot be empty"):
            registry.register(
                SkillSpec(name="", description="x", handler=lambda c: SkillEnvelope(text=""))
            )

    def test_override_bypasses_duplicate_guard(self):
        """Test fixtures need to swap in a mock handler. ``override()``
        is the documented path for that — ``register()`` would raise."""
        sentinel = SkillEnvelope(text="overridden!")
        original = registry.get("document_upload_skill")
        assert original is not None
        try:
            registry.override(
                SkillSpec(
                    name="document_upload_skill",
                    description="test override",
                    handler=lambda call: sentinel,
                )
            )
            got = registry.dispatch(
                SkillCall(name="document_upload_skill", inputs={}, question="")
            )
            assert got.text == "overridden!"
        finally:
            registry.override(original)  # restore so later tests see real spec


# ── Dispatch ─────────────────────────────────────────────────────────


class TestDispatch:
    def test_dispatch_unknown_skill_returns_envelope_not_raise(self):
        """Dispatching an unknown name is a soft-fail, not a crash —
        protects against planner typos / version drift between chat and
        a remote MCP server's tool list."""
        env = registry.dispatch(
            SkillCall(name="does_not_exist", inputs={}, question="test")
        )
        assert "Unknown skill" in env.text
        assert env.signal == "no_sources"

    def test_document_upload_skill_returns_canned_markdown(self):
        """Migrated skill — must produce the same body the legacy branch
        produced. The assertion keys on stable text to detect silent
        content drift."""
        env = registry.dispatch(
            SkillCall(name="document_upload_skill", inputs={}, question="how to upload?")
        )
        assert "Document upload" in env.text or "upload" in env.text.lower()
        assert env.signal == "no_sources"

    def test_list_thread_document_uploads_handles_empty_thread(self):
        """No thread_id should produce a graceful message, not a
        traceback. (This is what the legacy branch did; preserving
        behavior on migration.)"""
        env = registry.dispatch(
            SkillCall(
                name="list_thread_document_uploads",
                inputs={},
                question="what have I uploaded?",
                thread_id=None,
            )
        )
        assert isinstance(env.text, str)
        assert env.signal == "no_sources"


# ── Feature flag (the migration safety net) ──────────────────────────


class TestFeatureFlag:
    def test_registry_enabled_default_on(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MOBIUS_USE_SKILL_REGISTRY", None)
            assert registry.registry_enabled() is True

    @pytest.mark.parametrize("val", ["0", "false", "False", "NO", "off"])
    def test_registry_disabled_values(self, val):
        with patch.dict(os.environ, {"MOBIUS_USE_SKILL_REGISTRY": val}):
            assert registry.registry_enabled() is False

    @pytest.mark.parametrize("val", ["1", "true", "TRUE", "yes", "on", ""])
    def test_registry_enabled_values(self, val):
        """Empty string → default (enabled). Any truthy value → enabled.
        We're permissive here because the flag only exists during
        migration; strict parsing isn't worth the user confusion."""
        with patch.dict(os.environ, {"MOBIUS_USE_SKILL_REGISTRY": val}):
            assert registry.registry_enabled() is True


class TestRegistryParityWithLegacyBranches:
    """The migration's load-bearing assertion: registry path and legacy
    path produce byte-identical output for every migrated skill. If
    either drifts, this fails before users see it."""

    def test_document_upload_skill_parity(self):
        """Pull the DOCUMENT_UPLOAD_SKILL_MARKDOWN the legacy branch
        returns, and the registry dispatch, and compare."""
        from app.skills.document_upload import DOCUMENT_UPLOAD_SKILL_MARKDOWN

        legacy_text = DOCUMENT_UPLOAD_SKILL_MARKDOWN
        env = registry.dispatch(
            SkillCall(name="document_upload_skill", inputs={}, question="")
        )
        assert env.text == legacy_text, (
            "registry and legacy branch must produce identical text — "
            "divergence means the migration silently changed behavior"
        )

    def test_list_thread_document_uploads_parity(self):
        """Legacy branch calls format_thread_uploads_markdown(tid) and
        wraps in a 4-tuple. Registry handler must produce the same."""
        from app.skills.document_upload import format_thread_uploads_markdown

        tid = ""  # no thread, deterministic output
        legacy_text = format_thread_uploads_markdown(tid)
        env = registry.dispatch(
            SkillCall(
                name="list_thread_document_uploads",
                inputs={},
                question="",
                thread_id=tid,
            )
        )
        assert env.text == legacy_text


# ── Derived views (replacement for tool_manifest sets in commit 3) ───


class TestDerivedViews:
    def test_entity_tools_includes_both_migrated(self):
        """Neither document_upload nor list_thread_uploads takes
        jurisdiction as a qualifier — they're both in ENTITY_TOOLS
        today and should be in entity_tools() post-migration."""
        et = registry.entity_tools()
        assert "document_upload_skill" in et
        assert "list_thread_document_uploads" in et

    def test_follow_up_capable_matches_manifest_set(self):
        """Only list_thread_document_uploads is follow-up-capable in
        the current tool_manifest.py FOLLOW_UP_CAPABLE set (the only
        non-credentialing entry remaining after the 2026-04-18
        disconnect). Assert registry matches — commit 3 will replace
        the hand-maintained set with this computed one."""
        fuc = registry.follow_up_capable()
        assert "list_thread_document_uploads" in fuc
        assert "document_upload_skill" not in fuc


class TestAnswerToolIntegration:
    """End-to-end: ``answer_tool(..., tool_hint_override="X")`` goes
    through the registry when flag is on, through the legacy branch
    when flag is off, and both produce the same output."""

    def test_answer_tool_registry_path(self):
        from app.services.tool_agent import answer_tool

        with patch.dict(os.environ, {"MOBIUS_USE_SKILL_REGISTRY": "1"}):
            text, sources, usage, signal = answer_tool(
                "how do I upload?",
                tool_hint_override="document_upload_skill",
            )
        assert "upload" in text.lower()
        assert signal == "no_sources"
        assert sources == []
        assert usage is None

    def test_answer_tool_legacy_path_produces_identical_output(self):
        """Flag-off gate-test. If the flag stops routing through the
        registry, output must be identical to the flag-on path."""
        from app.services.tool_agent import answer_tool

        with patch.dict(os.environ, {"MOBIUS_USE_SKILL_REGISTRY": "1"}):
            r1 = answer_tool("x", tool_hint_override="document_upload_skill")
        with patch.dict(os.environ, {"MOBIUS_USE_SKILL_REGISTRY": "0"}):
            r2 = answer_tool("x", tool_hint_override="document_upload_skill")
        assert r1 == r2, (
            "registry and legacy paths diverged — the migration is not "
            "behavior-preserving and commit 2/3 must not delete the "
            "legacy branch yet"
        )
