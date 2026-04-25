"""Skill registry commit 3: google_search migrated + legacy deleted
+ TOOL_MANIFEST computed from the registry.

What this file guards:

1. **google_search skill registered + dispatches.** The final migration
   in the series. Envelope shape matches the legacy branch for the
   three behavior layers (auto-scrape, LLM snippet fallback, empty
   results).

2. **``registry.manifest_text()`` renders the enriched descriptions.**
   ``SkillSpec.description`` is multi-line now (carries the full
   "Use when / Do NOT use for / Returns" prose formerly hand-maintained
   in tool_manifest.py). Lock the rendered shape so nobody silently
   downgrades it to a one-liner.

3. **``TOOL_MANIFEST`` in tool_manifest.py composes router-owned prose
   with registry-rendered skill blocks.** The rendered manifest must
   still carry every skill the planner expects. Byte-equality against
   a snapshot is too brittle (whitespace, order); we assert every
   required skill name + its key phrases are present.

4. **``ENTITY_TOOLS`` and ``FOLLOW_UP_CAPABLE`` are union views.**
   Registry-derived for the five migrated skills; plus
   ``healthcare_npi_lookup`` explicitly hand-listed in tool_manifest.py
   until it gets its own ``SkillSpec``.

5. **Legacy dispatch branches are gone.** Scan tool_agent.py for the
   old ``if hint == "X"`` cascade fragments — they shouldn't exist
   anymore. If someone re-adds one, this test catches it.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from app.skills import registry
from app.skills.registry import SkillCall


# ── google_search skill ──────────────────────────────────────────────


class TestGoogleSearchSkill:
    def test_registered(self):
        assert registry.has("google_search")

    def test_requires_jurisdiction_true(self):
        """google_search is the one registered skill that takes active
        payer/state into the query — 'provider enrollment' inside a
        Sunshine Health / Florida thread becomes much better with those
        qualifiers merged in via build_search_query."""
        spec = registry.get("google_search")
        assert spec is not None
        assert spec.requires_jurisdiction is True

    def test_not_in_entity_tools_view(self):
        """Because requires_jurisdiction=True, google_search is NOT in
        entity_tools (which is 'skills that don't take jurisdiction').
        Locks the semantic inversion so a future edit can't flip one
        without flipping the other."""
        assert "google_search" not in registry.entity_tools()

    def test_auto_scrape_success_returns_web_source(self):
        """Happy path: auto-scrape hits on attempt 1. Envelope carries
        the scraped content + one web SourceRef with the source_url."""
        with patch(
            "app.services.tool_agent._run_google_search"
        ) as mock_search, patch(
            "app.services.tool_agent.score_and_scrape_top_result"
        ) as mock_scrape:
            mock_search.return_value = (
                [{"title": "Policy", "url": "https://x.com/policy", "snippet": "..."}],
                "[1] Policy ...",
                None,
                "google_only",
            )
            mock_scrape.return_value = ("# Scraped content\n\nDetails.", "https://x.com/policy", True)
            env = registry.dispatch(
                SkillCall(
                    name="google_search",
                    inputs={},
                    question="what's the prior auth policy?",
                    active_context={"payer": "Sunshine Health", "jurisdiction": "Florida"},
                )
            )
        assert "Scraped content" in env.text
        assert env.signal == "google_only"
        assert len(env.sources) == 1
        assert env.sources[0].url == "https://x.com/policy"
        assert env.sources[0].source_type == "web"

    def test_scrape_fails_falls_back_to_snippet_summary(self):
        """All scrape attempts fail, snippets exist → LLM summary path
        with the 'verify with payer' disclaimer appended."""
        with patch(
            "app.services.tool_agent._run_google_search"
        ) as mock_search, patch(
            "app.services.tool_agent.score_and_scrape_top_result"
        ) as mock_scrape, patch(
            "app.services.llm_provider.get_llm_provider"
        ) as mock_prov:
            mock_search.return_value = (
                [{"title": "x", "url": "https://y.com", "snippet": "..."}],
                "[1] X's site says ...",
                None,
                "google_only",
            )
            mock_scrape.return_value = (None, None, False)
            fake_provider = type(
                "P",
                (),
                {
                    "generate_with_usage": staticmethod(
                        lambda prompt: _async_return(("Summarized answer.", {"tokens": 5}))
                    )
                },
            )
            mock_prov.return_value = fake_provider
            env = registry.dispatch(
                SkillCall(
                    name="google_search",
                    inputs={},
                    question="tell me about X",
                )
            )
        assert "Summarized answer" in env.text
        assert "verify details directly with the payer" in env.text.lower()
        assert env.signal == "google_only"
        assert env.usage == {"tokens": 5}

    def test_empty_search_results_returns_no_sources(self):
        """No raw results + 'No search results' snippet → envelope
        carries a 'No relevant information' message with
        signal=no_sources."""
        with patch(
            "app.services.tool_agent._run_google_search"
        ) as mock_search, patch(
            "app.services.tool_agent.score_and_scrape_top_result"
        ) as mock_scrape:
            mock_search.return_value = ([], "No search results found", None, "no_sources")
            mock_scrape.return_value = (None, None, False)
            env = registry.dispatch(
                SkillCall(
                    name="google_search",
                    inputs={},
                    question="nonexistent-topic-8675309",
                )
            )
        assert env.signal == "no_sources"
        assert len(env.sources) == 0


# Small helper to wrap an awaitable value for mocking async functions
# without pulling in asyncio.Future machinery.
async def _awaitable_result(value):
    return value


def _async_return(value):
    return _awaitable_result(value)


# ── Manifest rendering ────────────────────────────────────────────────


class TestManifestRendering:
    def test_manifest_text_contains_full_skill_prose(self):
        """Enriched SkillSpec descriptions carry multi-line 'Use when /
        Do NOT use for / Returns' text. Lock that the rendered manifest
        preserves those phrases — downgrading to a one-liner would
        regress planner quality."""
        body = registry.manifest_text(names=("healthcare_query",))
        assert "Use when:" in body
        assert "Do NOT use for:" in body
        assert "Cannot:" in body

    def test_manifest_text_subset_selection(self):
        """Passing ``names`` restricts output. Used by tool_manifest.py
        to interleave registry blocks with router-owned prose in a
        specific order."""
        body = registry.manifest_text(names=("google_search",))
        assert "google_search" in body
        assert "web_scrape" not in body
        assert "healthcare_query" not in body

    def test_manifest_text_renders_input_signature(self):
        """A skill with ``url`` required + ``scrape_mode`` optional
        must render as ``web_scrape(url, scrape_mode optional)``.
        Matches the pre-refactor manifest signature shape."""
        body = registry.manifest_text(names=("web_scrape",))
        assert "web_scrape(" in body
        # Exact signature varies with dict ordering; just assert both
        # keys are present.
        assert "url" in body
        assert "scrape_mode" in body
        assert "optional" in body

    def test_manifest_text_empty_for_unknown_name(self):
        """Asking for a non-registered name returns empty, not a
        KeyError — tool_manifest.py should be able to request skills
        optimistically without wrapping in has() checks."""
        body = registry.manifest_text(names=("does_not_exist",))
        assert body == ""


# ── TOOL_MANIFEST composition ────────────────────────────────────────


class TestComputedToolManifest:
    def test_manifest_contains_all_router_owned_tools(self):
        """The four non-registry tools (search_corpus, refuse,
        healthcare_npi_lookup, search_uploaded_document) live as
        hand-maintained prose in tool_manifest.py because their
        dispatch is in react_loop, not answer_tool. Lock that they're
        still present after the composition refactor."""
        from app.pipeline.tool_manifest import TOOL_MANIFEST

        assert "search_corpus(query)" in TOOL_MANIFEST
        assert "healthcare_npi_lookup(question)" in TOOL_MANIFEST
        assert "search_uploaded_document(" in TOOL_MANIFEST
        assert "refuse(reason)" in TOOL_MANIFEST

    def test_manifest_contains_all_registry_skills(self):
        from app.pipeline.tool_manifest import TOOL_MANIFEST

        for name in (
            "healthcare_query",
            "document_upload_skill",
            "list_thread_document_uploads",
            "google_search",
            "web_scrape",
        ):
            assert f"{name}" in TOOL_MANIFEST, f"{name} missing from computed manifest"

    def test_manifest_preserves_planner_prompt_headers(self):
        """The 'AVAILABLE TOOLS' header + workflow-selection notice +
        the PER-TOOL CAPABILITIES block at the end must survive the
        composition refactor so the planner prompt doesn't silently
        change shape."""
        from app.pipeline.tool_manifest import TOOL_MANIFEST

        assert "AVAILABLE TOOLS" in TOOL_MANIFEST
        assert "WORKFLOW SELECTION" in TOOL_MANIFEST
        assert "PER-TOOL CAPABILITIES" in TOOL_MANIFEST


# ── Set views ────────────────────────────────────────────────────────


class TestEntityAndFollowUpViews:
    def test_entity_tools_is_union(self):
        """ENTITY_TOOLS = registry.entity_tools() | {healthcare_npi_lookup}.
        Assert the union actually happens — if tool_manifest.py ever
        drops the hand-listed additions, healthcare_npi_lookup would
        silently lose its entity-tool status and the dispatcher would
        start leaking jurisdiction into NPI lookups."""
        from app.pipeline.tool_manifest import ENTITY_TOOLS

        assert "healthcare_npi_lookup" in ENTITY_TOOLS, (
            "healthcare_npi_lookup dropped from ENTITY_TOOLS — the "
            "hand-listed union in tool_manifest.py was broken. "
            "Jurisdiction may now bleed into NPI-number lookups."
        )
        # All 5 registry skills that don't require jurisdiction:
        assert "healthcare_query" in ENTITY_TOOLS
        assert "web_scrape" in ENTITY_TOOLS
        assert "document_upload_skill" in ENTITY_TOOLS
        assert "list_thread_document_uploads" in ENTITY_TOOLS
        # google_search requires jurisdiction — NOT in entity_tools
        assert "google_search" not in ENTITY_TOOLS

    def test_follow_up_capable_derived_from_registry(self):
        """FOLLOW_UP_CAPABLE is derived from the registry. Any skill
        registered with ``follow_up_capable=True`` automatically appears
        here. Currently:
          * list_thread_document_uploads — long-standing
          * vibe — added 2026-04-25 (mobius-skills/vibe)
        """
        from app.pipeline.tool_manifest import FOLLOW_UP_CAPABLE

        assert FOLLOW_UP_CAPABLE == frozenset({
            "list_thread_document_uploads",
            "vibe",
        })


# ── Legacy dispatch branches deleted ─────────────────────────────────


class TestLegacyDispatchDeleted:
    """Scan tool_agent.py source for the specific old-cascade fragments
    to make sure nobody re-adds them. A new branch would duplicate a
    registry skill's behavior and drift silently."""

    def test_no_if_hint_eq_document_upload_skill_branch(self):
        src = Path("app/services/tool_agent.py").read_text()
        # The exact ``if hint == "document_upload_skill":`` branch that
        # was the first thing the registry replaced. Its return path is
        # also removed — no more direct DOCUMENT_UPLOAD_SKILL_MARKDOWN
        # reference in the dispatcher.
        assert "DOCUMENT_UPLOAD_SKILL_MARKDOWN" not in src, (
            "Legacy document_upload_skill dispatch branch reintroduced. "
            "Registry handles this now; adding a duplicate branch means "
            "the planner's hint could reach two different code paths."
        )

    def test_no_legacy_google_search_hint_branch(self):
        """The legacy branch did its own build_search_query + LLM
        summarization inside ``_answer_tool_impl``. Registry handler
        in web_search.py owns that now. Assert none of the legacy
        branch's distinctive strings survive inside the dispatcher."""
        src = Path("app/services/tool_agent.py").read_text()
        # Two fingerprints unique to the deleted branch — if either
        # reappears, the cascade is creeping back in:
        assert "# Auto-scrape: score URLs and read the best page" not in src
        assert '"\\n\\n[Note: Full page content could not be retrieved.' not in src
        # The emit text for the search-query header lived in the
        # deleted branch AND in the google_search skill. It should be
        # in web_search.py, not tool_agent.py:
        search_emit = "'◌ Searching the web for:"
        assert search_emit not in src, (
            "Legacy google_search hint branch emit text reintroduced in "
            "tool_agent.py. Registry handler owns it."
        )

    def test_mobius_use_skill_registry_flag_retired(self):
        """The env flag gated registry vs legacy during commits 1+2.
        Commit 3 retired the flag. A compat stub remains but no live
        code should read the env var."""
        src = Path("app/services/tool_agent.py").read_text()
        assert "MOBIUS_USE_SKILL_REGISTRY" not in src
        # Registry no longer reads the env either:
        registry_src = Path("app/skills/registry.py").read_text()
        assert "os.environ" not in registry_src
