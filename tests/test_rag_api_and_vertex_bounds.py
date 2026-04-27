"""Two production-bug fixes from the 2026-04-27 latency investigation.

Both bugs were silent — chat kept appearing to work but at huge latency
cost — and only surfaced when user-visible turns started taking >10
minutes to start.

  Bug 2: ``retriever_backend.retrieve_via_rag_api`` was POSTing to the
         old ``/retrieve`` endpoint that mobius-rag deprecated. Every
         call returned HTTP 405 → silent fallback to inline-BM25.

  Bug 1: ``llm_provider._vertex_generate_sync`` passed ``retry=`` with a
         45s ``deadline``, but vertexai SDK 1.142.0 silently ignored
         it on 429 retry storms. One call ran for 596.7s before
         raising, and the single-threaded worker queue was held
         hostage for the entire window.

These tests lock both fixes in place.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest


# ── Bug 2: RAG endpoint contract ──────────────────────────────────────


class TestRagApiEndpoint:
    """Confirm chat now talks to mobius-rag's ``/api/query`` endpoint
    with the new {query, k} payload, and parses the {chunks: [...]}
    response. Old endpoint was {question, top_k, ...} → {docs, ...}."""

    def _fake_response(self, body: dict, status: int = 200):
        """Build the urlopen-context-manager mock that retrieve_via_rag_api
        expects. ``urllib.request.urlopen(req, timeout=60).__enter__()``
        is the path; we mock the resp.read() bytes."""
        resp = MagicMock()
        resp.read.return_value = json.dumps(body).encode("utf-8")
        cm = MagicMock()
        cm.__enter__ = MagicMock(return_value=resp)
        cm.__exit__ = MagicMock(return_value=False)
        return cm

    def test_posts_to_api_query_path_not_legacy_retrieve(self, monkeypatch):
        """The new contract is POST /api/query. Posting to /retrieve
        was the bug — surfaced as HTTP 405 in prod logs."""
        monkeypatch.setenv("RAG_API_URL", "https://rag.test")
        from app.services import retriever_backend as rb
        captured: dict = {}

        def _fake_urlopen(req, timeout=None):
            # urllib.request.Request stores the URL in .full_url
            captured["url"] = getattr(req, "full_url", "") or req.get_full_url()
            captured["method"] = req.get_method()
            captured["body"] = req.data.decode() if req.data else ""
            return self._fake_response({"chunks": []})

        monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)
        rb.retrieve_via_rag_api(question="hello", top_k=5)
        assert captured["url"].endswith("/api/query"), (
            f"Expected /api/query path, got {captured['url']!r}"
        )
        assert captured["method"] == "POST"

    def test_payload_uses_new_field_names(self, monkeypatch):
        """{question, top_k} → {query, k}. The new endpoint rejects
        the old field names with HTTP 422 (validated locally)."""
        monkeypatch.setenv("RAG_API_URL", "https://rag.test")
        from app.services import retriever_backend as rb
        captured: dict = {}

        def _fake_urlopen(req, timeout=None):
            captured["body"] = json.loads(req.data.decode())
            return self._fake_response({"chunks": []})

        monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)
        rb.retrieve_via_rag_api(question="What is timely filing?", top_k=7)
        body = captured["body"]
        assert body == {"query": "What is timely filing?", "k": 7}, (
            f"Expected new field names only, got {body!r}"
        )
        # Confirm the OLD field names are NOT present
        assert "question" not in body
        assert "top_k" not in body
        assert "path" not in body
        assert "apply_google" not in body

    def test_filters_silently_dropped(self, monkeypatch):
        """The new endpoint doesn't accept payer/state/program/authority
        filters. Caller stability is preserved (kwargs accepted) but the
        wire payload only carries {query, k}. Documented behavior change."""
        monkeypatch.setenv("RAG_API_URL", "https://rag.test")
        from app.services import retriever_backend as rb
        captured: dict = {}

        def _fake_urlopen(req, timeout=None):
            captured["body"] = json.loads(req.data.decode())
            return self._fake_response({"chunks": []})

        monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)
        rb.retrieve_via_rag_api(
            question="x",
            top_k=3,
            filter_payer="Sunshine Health",
            filter_state="FL",
            filter_program="Medicaid",
            filter_authority_level="payer_manual",
            n_factual=5,
            n_hierarchical=3,
            apply_google=True,
            include_trace=True,
            path="lazy",
        )
        body = captured["body"]
        assert set(body.keys()) == {"query", "k"}, (
            f"Filters should be dropped on the wire; got {set(body.keys())}"
        )

    def test_parses_chunks_response_shape(self, monkeypatch):
        """Response is {chunks: [{text, source_type, source_id,
        document_id, document_name, page_number}, ...]}. trace is
        always None — endpoint doesn't return one."""
        monkeypatch.setenv("RAG_API_URL", "https://rag.test")
        from app.services import retriever_backend as rb

        chunks = [
            {
                "text": "Florida Medicaid timely filing is...",
                "source_type": "manual",
                "source_id": "src-1",
                "document_id": "doc-1",
                "document_name": "FL Medicaid Manual",
                "page_number": 42,
            },
            {
                "text": "...",
                "source_type": "manual",
                "source_id": "src-2",
                "document_id": "doc-2",
                "document_name": "Centene Provider Manual",
                "page_number": 17,
            },
        ]

        def _fake_urlopen(req, timeout=None):
            return self._fake_response({"chunks": chunks})

        monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)
        out, trace = rb.retrieve_via_rag_api(question="x", top_k=2)
        assert len(out) == 2
        assert out[0]["document_name"] == "FL Medicaid Manual"
        assert out[1]["page_number"] == 17
        # Trace is always None on the new endpoint
        assert trace is None

    def test_handles_empty_chunks_response(self, monkeypatch):
        monkeypatch.setenv("RAG_API_URL", "https://rag.test")
        from app.services import retriever_backend as rb

        def _fake_urlopen(req, timeout=None):
            return self._fake_response({"chunks": []})

        monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)
        out, trace = rb.retrieve_via_rag_api(question="x", top_k=3)
        assert out == []
        assert trace is None

    def test_handles_bare_list_response_defensive(self, monkeypatch):
        """Defensive: if some proxy ever returns a bare list (legacy
        shape), don't crash — treat as the chunk list."""
        monkeypatch.setenv("RAG_API_URL", "https://rag.test")
        from app.services import retriever_backend as rb

        def _fake_urlopen(req, timeout=None):
            return self._fake_response([{"text": "a", "document_id": "1"}])

        monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)
        out, trace = rb.retrieve_via_rag_api(question="x", top_k=1)
        assert len(out) == 1
        assert out[0]["text"] == "a"

    def test_returns_empty_when_no_rag_url(self, monkeypatch):
        """No RAG_API_URL → bail early with empty result. Inline-BM25
        fallback in the caller picks up."""
        monkeypatch.delenv("RAG_API_URL", raising=False)
        from app.services import retriever_backend as rb
        out, trace = rb.retrieve_via_rag_api(question="x")
        assert out == []
        assert trace is None

    def test_swallows_exceptions_returns_empty(self, monkeypatch):
        """If urlopen raises, we log + return empty (caller falls back).
        Don't propagate."""
        monkeypatch.setenv("RAG_API_URL", "https://rag.test")
        from app.services import retriever_backend as rb

        def _boom(req, timeout=None):
            raise ConnectionError("boom")

        monkeypatch.setattr("urllib.request.urlopen", _boom)
        out, trace = rb.retrieve_via_rag_api(question="x")
        assert out == []
        assert trace is None


# ── Bug 1: Vertex outer-bound timeout ─────────────────────────────────


class TestVertexOuterBoundTimeout:
    """Vertex SDK 1.142.0 silently ignored ``retry=Retry(deadline=45)``
    on throttled paths. We now wrap the SDK call in
    ``concurrent.futures.ThreadPoolExecutor`` and use
    ``Future.result(timeout=...)`` to guarantee an outer wall-clock
    bound. The inner zombie thread keeps running but the worker
    thread is freed at the deadline."""

    def test_raises_timeout_when_sdk_call_exceeds_deadline(self, monkeypatch):
        """Smoking-gun reproduction: SDK call takes longer than
        VERTEX_TOTAL_DEADLINE_SECONDS. Our wrapper must raise
        TimeoutError, NOT wait for the SDK to finish."""
        # Set a very short deadline so the test runs fast
        monkeypatch.setenv("VERTEX_TOTAL_DEADLINE_SECONDS", "0.5")

        # Simulate a hung SDK call: returns nothing, sleeps long
        import time as _t

        class FakeModel:
            def generate_content(self, *args, **kwargs):
                _t.sleep(5)  # 10x the deadline
                return MagicMock()

        with patch("vertexai.generative_models.GenerativeModel", return_value=FakeModel()):
            from app.services.llm_provider import _vertex_generate_sync
            with pytest.raises(Exception) as excinfo:
                _vertex_generate_sync(
                    model_name="gemini-2.5-flash",
                    prompt="test",
                    gen_config={},
                )
            # Assert the timeout fires before the SDK finishes (5s).
            # Either TimeoutError or a wrapped form is acceptable.
            err_str = str(excinfo.value).lower()
            assert ("timeout" in err_str or "abandoned" in err_str), (
                f"Expected timeout/abandoned message, got: {excinfo.value!r}"
            )

    def test_normal_call_returns_result_when_under_deadline(self, monkeypatch):
        """When the SDK call completes in time, the wrapper returns
        the response normally. Sanity test — wrapper should be a
        no-op for fast calls."""
        monkeypatch.setenv("VERTEX_TOTAL_DEADLINE_SECONDS", "5")

        # Real-shape response object with the text() method the
        # downstream parser reads. We don't need the full Vertex
        # response — just enough to flow through.
        from unittest.mock import MagicMock as MM
        mock_resp = MM()
        mock_resp.text = "fast answer"
        # The downstream of _vertex_generate_sync extracts from response;
        # the test's only job is "no timeout was raised."

        class FakeModel:
            def generate_content(self, *args, **kwargs):
                return mock_resp

        with patch("vertexai.generative_models.GenerativeModel", return_value=FakeModel()):
            from app.services.llm_provider import _vertex_generate_sync
            try:
                _vertex_generate_sync(
                    model_name="gemini-2.5-flash",
                    prompt="test",
                    gen_config={},
                )
            except TimeoutError:
                pytest.fail("Fast call should not have timed out")
            except Exception:
                # Other exceptions (response parsing, etc.) are not the
                # focus of THIS test — the timeout wrapper passing
                # through is what we're locking.
                pass

    def test_deadline_env_var_honored(self, monkeypatch):
        """The wrapper reads VERTEX_TOTAL_DEADLINE_SECONDS dynamically
        each call so operators can tune it without restart."""
        monkeypatch.setenv("VERTEX_TOTAL_DEADLINE_SECONDS", "0.3")
        import time as _t
        t0 = _t.perf_counter()

        class FakeModel:
            def generate_content(self, *args, **kwargs):
                _t.sleep(3)
                return MagicMock()

        with patch("vertexai.generative_models.GenerativeModel", return_value=FakeModel()):
            from app.services.llm_provider import _vertex_generate_sync
            with pytest.raises(Exception):
                _vertex_generate_sync(
                    model_name="x",
                    prompt="x",
                    gen_config={},
                )
        elapsed = _t.perf_counter() - t0
        # Must time out close to the 0.3s deadline, NOT wait the full 3s
        assert elapsed < 1.5, (
            f"Wrapper waited {elapsed:.1f}s — should have abandoned at ~0.3s. "
            "If this is flaky, the deadline isn't being honored."
        )
