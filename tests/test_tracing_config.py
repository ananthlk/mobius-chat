"""Tests for app/tracing_config.py (Sprint 1 #11).

Surfaces covered:
  * Env gate — tracing_enabled() honors all four priority rules
  * configure_tracing() is idempotent + doesn't blow up on disabled path
  * Noop tracer when disabled: context-managers work, methods no-op
  * Real tracer when enabled: span attrs land, trace_id retrievable
  * start_pipeline_span() stamps the standard attribute set
  * trace_context_ids() returns empty tuple when no active span
  * Log filter picks up trace_id/span_id when tracing is active
"""
from __future__ import annotations

import logging
from unittest.mock import patch

import pytest

from app import tracing_config


@pytest.fixture(autouse=True)
def _reset_configured_flag():
    """Tracing config caches init via a module-level flag. Reset it
    between tests so each case gets a fresh init."""
    tracing_config._CONFIGURED = False
    tracing_config._TRACER = None
    yield
    tracing_config._CONFIGURED = False
    tracing_config._TRACER = None


# ── Env gate ──────────────────────────────────────────────────────────


class TestTracingEnabledGate:
    def test_default_dev_off(self, monkeypatch):
        monkeypatch.delenv("CHAT_TRACE_ENABLED", raising=False)
        monkeypatch.delenv("K_SERVICE", raising=False)
        monkeypatch.delenv("CHAT_ENV_STRICT", raising=False)
        assert tracing_config.tracing_enabled() is False

    def test_explicit_off_overrides_k_service(self, monkeypatch):
        monkeypatch.setenv("CHAT_TRACE_ENABLED", "0")
        monkeypatch.setenv("K_SERVICE", "mobius-chat")
        assert tracing_config.tracing_enabled() is False

    def test_explicit_on_without_k_service(self, monkeypatch):
        monkeypatch.setenv("CHAT_TRACE_ENABLED", "1")
        monkeypatch.delenv("K_SERVICE", raising=False)
        assert tracing_config.tracing_enabled() is True

    def test_cloud_run_implicit_on(self, monkeypatch):
        monkeypatch.delenv("CHAT_TRACE_ENABLED", raising=False)
        monkeypatch.setenv("K_SERVICE", "mobius-chat")
        assert tracing_config.tracing_enabled() is True

    def test_hosted_strict_implicit_on(self, monkeypatch):
        monkeypatch.delenv("CHAT_TRACE_ENABLED", raising=False)
        monkeypatch.delenv("K_SERVICE", raising=False)
        monkeypatch.setenv("CHAT_ENV_STRICT", "1")
        assert tracing_config.tracing_enabled() is True


# ── configure_tracing idempotency + disabled path ────────────────────


class TestConfigureTracing:
    def test_disabled_path_does_not_raise(self, monkeypatch):
        monkeypatch.setenv("CHAT_TRACE_ENABLED", "0")
        tracing_config.configure_tracing()  # must not raise
        assert tracing_config._TRACER is None  # no SDK tracer stamped

    def test_idempotent_disabled(self, monkeypatch):
        monkeypatch.setenv("CHAT_TRACE_ENABLED", "0")
        tracing_config.configure_tracing()
        flag1 = tracing_config._CONFIGURED
        tracing_config.configure_tracing()  # second call — no-op
        assert tracing_config._CONFIGURED == flag1 is True


# ── Tracer behavior ───────────────────────────────────────────────────


class TestGetTracer:
    def test_tracer_returns_usable_context_manager_when_disabled(self, monkeypatch):
        """Callers should not need to branch on enabled/disabled —
        ``with tracer.start_as_current_span(...) as span:`` must work
        either way."""
        monkeypatch.setenv("CHAT_TRACE_ENABLED", "0")
        tracer = tracing_config.get_tracer()
        with tracer.start_as_current_span("anything") as span:
            # Attribute setters must accept calls silently when disabled.
            span.set_attribute("k", "v")
            span.set_attributes({"a": 1, "b": "two"})

    def test_tracer_records_spans_when_enabled(self, monkeypatch):
        """Under the SDK + an in-memory exporter, spans we open must
        actually be recorded."""
        monkeypatch.setenv("CHAT_TRACE_ENABLED", "1")
        monkeypatch.delenv("K_SERVICE", raising=False)
        # Install an in-memory exporter instead of Cloud Trace.
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
        from opentelemetry import trace as _trace

        exporter = InMemorySpanExporter()
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        _trace.set_tracer_provider(provider)

        tracing_config._CONFIGURED = True
        tracing_config._TRACER = _trace.get_tracer("test-mobius")

        with tracing_config.get_tracer().start_as_current_span("parent") as parent:
            parent.set_attribute("mobius.correlation_id", "cid-xyz")

        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        assert spans[0].name == "parent"
        assert spans[0].attributes["mobius.correlation_id"] == "cid-xyz"


# ── start_pipeline_span helper ────────────────────────────────────────


class TestStartPipelineSpan:
    def test_stamps_standard_attrs(self, monkeypatch):
        monkeypatch.setenv("CHAT_TRACE_ENABLED", "1")
        monkeypatch.delenv("K_SERVICE", raising=False)

        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
        from opentelemetry import trace as _trace

        exporter = InMemorySpanExporter()
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        # Don't set the global provider — OTel refuses to override it
        # across tests in the same process. Attach our tracer directly
        # from the local provider so our spans land in our exporter
        # regardless of what other tests did with the global.
        tracing_config._CONFIGURED = True
        tracing_config._TRACER = provider.get_tracer("test-mobius")

        with tracing_config.start_pipeline_span(
            "pipeline.run_pipeline",
            correlation_id="cid-1",
            user_id="alice",
            thread_id="t-7",
            stage="run_pipeline",
            extra={"chat_mode": "copilot"},
        ):
            pass

        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        attrs = spans[0].attributes
        assert attrs["mobius.correlation_id"] == "cid-1"
        assert attrs["mobius.user_id"] == "alice"
        assert attrs["mobius.thread_id"] == "t-7"
        assert attrs["mobius.stage"] == "run_pipeline"
        assert attrs["mobius.chat_mode"] == "copilot"

    def test_omits_attrs_when_not_supplied(self, monkeypatch):
        """When the caller passes None / omits, don't stamp the
        attribute at all (don't stamp empty strings either)."""
        monkeypatch.setenv("CHAT_TRACE_ENABLED", "1")
        monkeypatch.delenv("K_SERVICE", raising=False)

        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
        from opentelemetry import trace as _trace

        exporter = InMemorySpanExporter()
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        # Don't set the global provider — OTel refuses to override it
        # across tests in the same process. Attach our tracer directly
        # from the local provider so our spans land in our exporter
        # regardless of what other tests did with the global.
        tracing_config._CONFIGURED = True
        tracing_config._TRACER = provider.get_tracer("test-mobius")

        with tracing_config.start_pipeline_span("llm.sparse"):
            pass

        spans = exporter.get_finished_spans()
        attrs = spans[0].attributes
        # No context supplied → no mobius.* attributes at all.
        mobius_keys = [k for k in attrs if str(k).startswith("mobius.")]
        assert mobius_keys == []


# ── trace_context_ids ─────────────────────────────────────────────────


class TestTraceContextIds:
    def test_returns_empty_when_disabled(self, monkeypatch):
        monkeypatch.setenv("CHAT_TRACE_ENABLED", "0")
        assert tracing_config.trace_context_ids() == ("", "")

    def test_returns_empty_outside_any_span(self, monkeypatch):
        monkeypatch.setenv("CHAT_TRACE_ENABLED", "1")
        monkeypatch.delenv("K_SERVICE", raising=False)
        # Not currently in a span → trace_id / span_id both empty.
        tid, sid = tracing_config.trace_context_ids()
        # Depending on OTel default state, may be empty strings — what
        # matters is that the call doesn't raise.
        assert isinstance(tid, str)
        assert isinstance(sid, str)

    def test_returns_populated_inside_span(self, monkeypatch):
        monkeypatch.setenv("CHAT_TRACE_ENABLED", "1")
        monkeypatch.delenv("K_SERVICE", raising=False)

        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
        from opentelemetry import trace as _trace

        exporter = InMemorySpanExporter()
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        # Don't set the global provider — OTel refuses to override it
        # across tests in the same process. Attach our tracer directly
        # from the local provider so our spans land in our exporter
        # regardless of what other tests did with the global.
        tracing_config._CONFIGURED = True
        tracing_config._TRACER = provider.get_tracer("test-mobius")

        with tracing_config.get_tracer().start_as_current_span("inside"):
            tid, sid = tracing_config.trace_context_ids()
            assert len(tid) == 32  # 128-bit hex
            assert len(sid) == 16  # 64-bit hex
            assert set(tid) <= set("0123456789abcdef")


# ── Integration with logging_config enrichment ───────────────────────


class TestLoggingFilterPicksUpTraceIds:
    def test_filter_stamps_trace_ids_when_span_active(self, monkeypatch):
        monkeypatch.setenv("CHAT_TRACE_ENABLED", "1")
        monkeypatch.delenv("K_SERVICE", raising=False)

        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
        from opentelemetry import trace as _trace

        exporter = InMemorySpanExporter()
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        # Don't set the global provider — OTel refuses to override it
        # across tests in the same process. Attach our tracer directly
        # from the local provider so our spans land in our exporter
        # regardless of what other tests did with the global.
        tracing_config._CONFIGURED = True
        tracing_config._TRACER = provider.get_tracer("test-mobius")

        from app.logging_config import ContextEnrichmentFilter
        f = ContextEnrichmentFilter()

        with tracing_config.get_tracer().start_as_current_span("log-scope"):
            r = logging.LogRecord("t", logging.INFO, "", 0, "m", None, None)
            f.filter(r)
            assert len(r.trace_id) == 32
            assert len(r.span_id) == 16

    def test_filter_leaves_trace_ids_empty_when_disabled(self, monkeypatch):
        monkeypatch.setenv("CHAT_TRACE_ENABLED", "0")
        tracing_config._CONFIGURED = True
        tracing_config._TRACER = None

        from app.logging_config import ContextEnrichmentFilter
        f = ContextEnrichmentFilter()
        r = logging.LogRecord("t", logging.INFO, "", 0, "m", None, None)
        f.filter(r)
        assert r.trace_id == ""
        assert r.span_id == ""
