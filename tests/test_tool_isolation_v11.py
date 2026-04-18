"""Spec v1.1 Part 4 — Regression tests: Tool Isolation + Entity Extraction + Auto-Scrape.

Tests cover the three observed failure classes:
  Class A — Jurisdiction bleed into entity queries
  Class B — Query construction ignores question content
  Class C — Web search returns snippets, not content (auto-scrape)
"""
import pytest
from unittest.mock import call, patch, MagicMock

from app.services.doc_assembly import RETRIEVAL_SIGNAL_NO_SOURCES, RETRIEVAL_SIGNAL_GOOGLE_ONLY
from app.services.tool_agent import (
    answer_tool,
    build_search_query,
    extract_entity_from_question,
    score_and_scrape_top_result,
    _parse_search_result_urls,
    TOOL_GOOGLE_SEARCH,
    TOOL_HEALTHCARE_QUERY,
    TOOL_WEB_SCRAPE_REVIEW,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SEARCH_RESULT_TEXT = (
    "[1] David Lawrence Center Provider Info\n"
    "    Behavioral health services in Naples FL\n"
    "    URL: https://www.davidlawrencecenter.org/providers\n\n"
    "[2] NPI lookup David Lawrence\n"
    "    NPI registry entry\n"
    "    URL: https://npiregistry.cms.hhs.gov/search\n"
)

_SEARCH_RESULT_SUNSHINE = (
    "[1] Sunshine Health Provider Enrollment\n"
    "    How to enroll as a Sunshine Health provider in Florida\n"
    "    URL: https://www.sunshinehealth.com/providers/enroll\n\n"
    "[2] Sunshine Health Network FAQ\n"
    "    FAQ for joining Sunshine Health network Florida Medicaid\n"
    "    URL: https://www.sunshinehealth.com/faq\n"
)

_SCRAPE_PROVIDER_CONTENT = "Provider Enrollment Process\n" + "Step 1: Complete the credentialing application. " * 30


# ---------------------------------------------------------------------------
# Unit: extract_entity_from_question
# ---------------------------------------------------------------------------

class TestExtractEntity:
    def test_npi_lookup_extracts_org_not_payer(self):
        """Class A+B: entity comes from question, not from active payer."""
        e = extract_entity_from_question("What is the NPI for David Lawrence Center?")
        assert e.get("org_name") == "David Lawrence Center"

    def test_npi_lookup_aspire_health(self):
        """Class B: org name disambiguation — active payer never substitutes."""
        e = extract_entity_from_question("Find the NPI for Aspire Health")
        assert "Aspire Health" in (e.get("org_name") or "")

    def test_npi_by_number_no_contamination(self):
        """Class A: 10-digit NPI number detected directly."""
        e = extract_entity_from_question("Look up NPI 1234567890")
        assert e.get("npi_number") == "1234567890"

    def test_enrollment_query_extracts_payer_from_question(self):
        """Class B: enrollment query extracts payer from question text."""
        e = extract_entity_from_question("How does a provider enroll with Sunshine Health?")
        assert "Sunshine Health" in (e.get("org_name") or "")

    def test_timely_filing_extracts_named_payer(self):
        """Class B: question names Molina — should NOT be replaced with active payer."""
        e = extract_entity_from_question("What is the timely filing deadline for Molina Healthcare?")
        assert "Molina" in (e.get("org_name") or "")

    def test_address_extraction(self):
        """Class A+B: street address extracted from question text."""
        e = extract_entity_from_question("Find providers at 1234 Main St Naples FL")
        assert e.get("address") is not None
        assert "1234" in e["address"]


# ---------------------------------------------------------------------------
# Unit: build_search_query — jurisdiction as qualifier, never as subject
# ---------------------------------------------------------------------------

class TestBuildSearchQuery:
    _active = {"jurisdiction": "Florida", "program": "Medicaid", "payer": "Sunshine Health"}

    def test_npi_query_uses_extracted_entity(self):
        """Class A+B: search query uses entity, not active payer."""
        entity = extract_entity_from_question("What is the NPI for David Lawrence Center?")
        query = build_search_query(entity, self._active, intent=None)
        assert "David Lawrence" in query
        assert "Sunshine Health" not in query

    def test_npi_number_returns_bare_lookup(self):
        """Class A: NPI number queries have no jurisdiction qualifiers."""
        entity = extract_entity_from_question("Look up NPI 1234567890")
        query = build_search_query(entity, self._active, intent=None)
        assert "1234567890" in query
        assert "Sunshine Health" not in query
        assert "Florida" not in query  # NPI lookups need no qualifiers

    def test_timely_filing_uses_molina_not_sunshine(self):
        """Class B: query built from question, not active payer."""
        entity = extract_entity_from_question("What is the timely filing deadline for Molina Healthcare?")
        query = build_search_query(entity, self._active, intent="timely filing deadline")
        assert "Molina" in query
        assert "Sunshine Health" not in query
        assert "Florida" in query  # state is a valid qualifier
        assert "Medicaid" in query

    def test_enrollment_query_has_entity_plus_qualifiers(self):
        """Class B correct: entity + state + program qualifiers."""
        entity = extract_entity_from_question("How does a provider enroll with Sunshine Health?")
        query = build_search_query(entity, self._active, intent="provider enrollment")
        assert "Sunshine Health" in query
        assert "Florida" in query
        assert "Medicaid" in query


# ---------------------------------------------------------------------------
# Unit: _parse_search_result_urls
# ---------------------------------------------------------------------------

class TestParseSearchResultUrls:
    def test_parses_urls_from_mcp_text(self):
        results = _parse_search_result_urls(_SEARCH_RESULT_TEXT)
        assert len(results) == 2
        assert results[0]["url"] == "https://www.davidlawrencecenter.org/providers"
        assert results[1]["url"] == "https://npiregistry.cms.hhs.gov/search"
        assert "David Lawrence" in results[0]["title"]

    def test_empty_text_returns_empty(self):
        assert _parse_search_result_urls("") == []
        assert _parse_search_result_urls("No search results found.") == []

    def test_skips_entries_without_url(self):
        text = "[1] Title\n    Snippet with no URL line\n\n[2] Other\n    Snippet\n    URL: https://example.com"
        results = _parse_search_result_urls(text)
        assert len(results) == 1
        assert results[0]["url"] == "https://example.com"


# ---------------------------------------------------------------------------
# Unit: score_and_scrape_top_result
# ---------------------------------------------------------------------------

class TestScoreAndScrape:
    def test_scrapes_best_url(self):
        """Class C: scrapes the best-scored URL from search results."""
        results = [
            {"title": "Sunshine Health Provider Enrollment", "url": "https://www.sunshinehealth.com/providers/enroll", "snippet": ""},
            {"title": "Reddit", "url": "https://www.reddit.com/sunshine", "snippet": ""},
        ]
        # New v1.2 signature: org_name / state (not entity / active dicts)
        with patch("app.services.tool_agent._scrape_url_simple") as mock_scrape:
            def fake_scrape(url):
                if "sunshinehealth" in url:
                    return (_SCRAPE_PROVIDER_CONTENT, True)
                return ("", False)
            mock_scrape.side_effect = fake_scrape
            content, source_url, ok = score_and_scrape_top_result(
                results, org_name="Sunshine Health", state="FL"
            )
        assert ok is True
        assert "sunshinehealth" in source_url
        assert "Provider Enrollment" in content

    def test_skips_login_wall(self):
        """Class C: login wall content → _scrape_url_simple returns ('', False), fallback to next URL."""
        results = [
            {"title": "Login Required", "url": "https://portal.sunshinehealth.com/login", "snippet": ""},
            {"title": "Public page", "url": "https://www.sunshinehealth.com/providers", "snippet": ""},
        ]
        # In v1.2, login wall detection happens inside _scrape_direct / _scrape_via_mcp.
        # Simulate: login URL returns ('', False), public page returns content.
        with patch("app.services.tool_agent._scrape_url_simple") as mock_scrape:
            def fake_scrape(url):
                if "login" in url:
                    return ("", False)  # login wall rejected inside _scrape_direct
                return (_SCRAPE_PROVIDER_CONTENT, True)
            mock_scrape.side_effect = fake_scrape
            content, source_url, ok = score_and_scrape_top_result(
                results, org_name="Sunshine Health"
            )
        assert ok is True
        assert "login" not in source_url
        assert "Provider Enrollment" in content

    def test_all_scrapes_fail_returns_none(self):
        """Class C: all scrapes fail → returns (None, None, False) for snippet fallback."""
        results = [{"title": "Page", "url": "https://example.com", "snippet": ""}]
        with patch("app.services.tool_agent._scrape_url_simple", return_value=("", False)):
            content, source_url, ok = score_and_scrape_top_result(results)
        assert ok is False
        assert content is None
        assert source_url is None

    def test_skips_noise_domains(self):
        """Noise domains (reddit, linkedin, etc.) are scored -1.0 and never scraped."""
        results = [
            {"title": "Reddit post", "url": "https://www.reddit.com/r/medicaid/123", "snippet": ""},
            {"title": "Real page", "url": "https://cms.gov/provider-enrollment", "snippet": ""},
        ]
        scraped_urls: list = []
        with patch("app.services.tool_agent._scrape_url_simple") as mock_scrape:
            def tracking_scrape(url):
                scraped_urls.append(url)
                return (_SCRAPE_PROVIDER_CONTENT, True)
            mock_scrape.side_effect = tracking_scrape
            score_and_scrape_top_result(results)
        assert not any("reddit.com" in u for u in scraped_urls)


# ---------------------------------------------------------------------------
# Integration: answer_tool with tool_hint_override — entity isolation
# ---------------------------------------------------------------------------

class TestAnswerToolEntityIsolation:
    """Verifies that active payer never leaks into entity tool search targets."""

    _active = {"jurisdiction": "Florida", "program": "Medicaid", "payer": "Sunshine Health"}

    # Phase 2a (2026-04-18): tests for tool_hint_override="npi_lookup" /
    # "search_org_names" were removed along with those dispatch branches
    # — the credentialing disconnect retired org_npi_lookup /
    # search_org_names as chat-reachable tools. Entity-isolation for
    # NPI-by-number and address lookups is still covered below.

    def test_npi_by_number_no_payer_contamination(self):
        """Class A: 'Look up NPI 1234567890' → healthcare_query with NPI number, no payer passed.

        Patches both MCP import sites: the legacy branch in tool_agent
        uses ``app.services.tool_agent.call_mcp_tool``; the registry
        handler (commit 2 of skill-registry migration) lazy-imports from
        ``app.services.mcp_manager``. Patching both means the test works
        whether the registry flag is on or off."""
        with patch("app.services.tool_agent.call_mcp_tool") as mock_tool, \
             patch("app.services.mcp_manager.call_mcp_tool") as mock_mgr:
            mock_tool.return_value = ("Provider: Jane Doe, NPI: 1234567890, Specialty: Psychiatry", True)
            mock_mgr.return_value = ("Provider: Jane Doe, NPI: 1234567890, Specialty: Psychiatry", True)
            answer, sources, _, signal = answer_tool(
                "Look up NPI 1234567890",
                tool_hint_override="healthcare_query",
                active_context=self._active,
            )
        calls = list(mock_tool.call_args_list) + list(mock_mgr.call_args_list)
        hc_call = next((c for c in calls if c[0][0] == TOOL_HEALTHCARE_QUERY), None)
        assert hc_call is not None, "healthcare_query was never called on either import path"
        question_arg = hc_call[0][1].get("question", "")
        assert "1234567890" in question_arg
        assert "Sunshine" not in question_arg

    # Phase 2a: test_address_lookup_uses_address_from_question removed
    # with the search_org_by_address dispatch branch.


class TestAnswerToolAutoScrape:
    """Class C: google_search → auto-scrape the best URL."""

    _active = {"jurisdiction": "Florida", "program": "Medicaid", "payer": "Sunshine Health"}

    def _mock_search_then_scrape(self, mock_mcp, scrape_content: str = _SCRAPE_PROVIDER_CONTENT):
        def side_effect(tool_name, args):
            if tool_name == TOOL_GOOGLE_SEARCH:
                return (_SEARCH_RESULT_SUNSHINE, True)
            if tool_name == TOOL_WEB_SCRAPE_REVIEW:
                return (scrape_content, True)
            return ("", False)
        mock_mcp.side_effect = side_effect

    def test_enrollment_query_auto_scrapes_top_result(self):
        """Class C: google_search returns URLs → auto-scrapes the best one."""
        with patch("app.services.tool_agent.call_mcp_tool") as mock_mcp:
            self._mock_search_then_scrape(mock_mcp)
            answer, sources, _, signal = answer_tool(
                "How does a provider enroll with Sunshine Health?",
                tool_hint_override="google_search",
                question_intent="provider enrollment",
                active_context=self._active,
            )
        calls = mock_mcp.call_args_list
        tool_names = [c[0][0] for c in calls]
        # Both google_search and web_scrape_review should be called
        assert TOOL_GOOGLE_SEARCH in tool_names
        assert TOOL_WEB_SCRAPE_REVIEW in tool_names
        # Scraped content or search result is in the answer
        assert "provider enrollment" in answer.lower() or "credentialing application" in answer.lower() or "enroll" in answer.lower()
        assert signal == RETRIEVAL_SIGNAL_GOOGLE_ONLY

    def test_enrollment_login_wall_falls_back_to_snippets(self):
        """Class C: login wall on scrape → falls back to snippet summarisation."""
        login_wall = "Please sign in to access this page. Create an account to continue."

        def side_effect(tool_name, args):
            if tool_name == TOOL_GOOGLE_SEARCH:
                return (_SEARCH_RESULT_SUNSHINE, True)
            if tool_name == TOOL_WEB_SCRAPE_REVIEW:
                return (login_wall, True)  # always returns login wall
            return ("", False)

        with patch("app.services.tool_agent.call_mcp_tool") as mock_mcp:
            mock_mcp.side_effect = side_effect
            with patch("app.services.tool_agent.asyncio.run") as mock_run:
                mock_run.return_value = ("Sunshine Health requires providers to complete the enrollment form.", MagicMock())
                answer, sources, _, signal = answer_tool(
                    "How does a provider enroll with Sunshine Health?",
                    tool_hint_override="google_search",
                    question_intent="provider enrollment",
                    active_context=self._active,
                )
        # Should return snippet-based answer when all scrapes are login walls
        assert signal == RETRIEVAL_SIGNAL_GOOGLE_ONLY
        assert answer  # non-empty

    def test_google_search_query_uses_question_entity_not_active_payer(self):
        """Class B: timely filing for Molina — query must NOT use active payer (Sunshine Health)."""
        captured_queries: list = []

        def side_effect(tool_name, args):
            if tool_name == TOOL_GOOGLE_SEARCH:
                captured_queries.append(args.get("query", ""))
                return ("[1] Molina Filing Info\n    Snippet\n    URL: https://www.molinahealthcare.com/timely-filing", True)
            if tool_name == TOOL_WEB_SCRAPE_REVIEW:
                return ("Molina Healthcare timely filing deadline is 180 days from date of service. " * 10, True)
            return ("", False)

        with patch("app.services.tool_agent.call_mcp_tool") as mock_mcp:
            mock_mcp.side_effect = side_effect
            answer_tool(
                "What is the timely filing deadline for Molina Healthcare?",
                tool_hint_override="google_search",
                question_intent="timely filing deadline",
                active_context=self._active,
            )

        assert len(captured_queries) >= 1
        query = captured_queries[0]
        assert "Molina" in query, f"Expected 'Molina' in search query, got: {query!r}"
        assert "Sunshine Health" not in query, f"Active payer leaked into search query: {query!r}"


# ---------------------------------------------------------------------------
# Regression: RAG jurisdiction context is unchanged
# ---------------------------------------------------------------------------

class TestRagJurisdictionUnchanged:
    """Regression: RAG path must still receive rag_filter_overrides from active jurisdiction."""

    def test_rag_filter_overrides_passed_correctly(self):
        """When agent=RAG, rag_filter_overrides still flow from active jurisdiction (not broken)."""
        from app.stages.resolve import _answer_for_subquestion
        from app.services.doc_assembly import RETRIEVAL_SIGNAL_NO_SOURCES

        with patch("app.stages.resolve.answer_non_patient") as mock_rag:
            mock_rag.return_value = (
                "PA criteria for H0036 requires diagnosis X and documentation Y. " * 5,
                [{"document_name": "Sunshine PA Policy", "source_type": "internal"}],
                None,
                "approved_authoritative",
            )
            ans, usage, sources, signal, layer = _answer_for_subquestion(
                correlation_id="test-corr",
                sq_id="t1",
                agent="RAG",
                kind="non_patient",
                text="What is Sunshine Health's PA criteria for H0036?",
                rag_filter_overrides={"payer": "Sunshine Health"},
                active_context={"jurisdiction": "Florida", "payer": "Sunshine Health"},
            )

        assert layer == 1  # RAG layer
        assert "PA criteria" in ans
        # Verify rag_filter_overrides was passed through to answer_non_patient
        call_kwargs = mock_rag.call_args[1]
        assert call_kwargs.get("rag_filter_overrides") == {"payer": "Sunshine Health"}
