"""OpenTelemetry tracing — Sprint 1 #11 (2026-04-24).

One span per incoming request (via FastAPI auto-instrumentation),
one per outgoing HTTP call (via httpx auto-instrumentation), plus
manual spans at the pipeline stage + LLM boundaries. Exports to
Google Cloud Trace in hosted envs, console in dev when explicitly
enabled, no-op otherwise.

Why env-gated
-------------
A non-trivial OTel init cost (~100–300ms at import + ~50ms per span
export) is wasted in dev where nobody's looking at Cloud Trace. The
``CHAT_TRACE_ENABLED`` env var gates the entire surface: when off,
the OpenTelemetry SDK imports don't even happen and ``start_span``
is a no-op via the default API's NonRecordingSpan.

Integration with structured logs
--------------------------------
Both modules key off ``correlation_id``. The tracing layer
additionally stamps ``trace_id`` and ``span_id`` onto every
emitted log record (via the enrichment filter extension) so a
Cloud Logging query like ``jsonPayload.correlation_id="abc"`` will
cross-reference to the matching Cloud Trace waterfall.

Correlation semantics
---------------------
Every /chat turn gets a root request span (FastAPI auto). Inside
it we add child spans for run_pipeline → each stage → each
ReAct round → each LLM call → each tool dispatch. The correlation_id
rides as a span attribute at the request boundary; children inherit
it via the context.

Known scope limits
------------------
* Sync handlers that FastAPI runs in a threadpool don't propagate
  the trace context automatically. Most of our slow paths are in
  the async pipeline so this is a minor gap.
* Post-run adjudication fires on a daemon thread — spans emitted
  there are detached from the turn trace unless we explicitly pass
  the context, which isn't done yet (follow-up work).
"""
from __future__ import annotations

import logging
import os
from typing import Any, Callable

logger = logging.getLogger(__name__)

_CONFIGURED = False
_TRACER: Any = None  # opentelemetry.trace.Tracer when configured, None otherwise


def tracing_enabled() -> bool:
    """Master gate. Defaults OFF in dev, ON when CHAT_ENV_STRICT=1
    (hosted) unless explicitly overridden.

    Priority:
      1. ``CHAT_TRACE_ENABLED`` explicit bool override
      2. ``K_SERVICE`` set (Cloud Run) → on
      3. ``CHAT_ENV_STRICT=1`` (hosted) → on
      4. otherwise → off
    """
    raw = (os.environ.get("CHAT_TRACE_ENABLED") or "").strip().lower()
    if raw in ("0", "false", "no", "off"):
        return False
    if raw in ("1", "true", "yes", "on"):
        return True
    if os.environ.get("K_SERVICE"):
        return True
    return (os.environ.get("CHAT_ENV_STRICT") or "").strip() == "1"


def _resolve_sample_ratio() -> float:
    """TRACE_SAMPLE_RATIO env var (float in [0.0, 1.0]).
    Default 1.0 (100% sampling) — fine for beta volumes. Drop to 0.1
    for higher-traffic prod."""
    raw = (os.environ.get("TRACE_SAMPLE_RATIO") or "1.0").strip()
    try:
        r = float(raw)
        return max(0.0, min(1.0, r))
    except ValueError:
        return 1.0


def configure_tracing() -> None:
    """Install the OTel provider + exporter. Idempotent.

    Safe to call before ``app = FastAPI(...)``. Auto-instrumentation
    of the app itself is separate — see :func:`instrument_app`."""
    global _CONFIGURED, _TRACER
    if _CONFIGURED:
        return
    if not tracing_enabled():
        logger.info("tracing disabled (CHAT_TRACE_ENABLED not set)")
        _CONFIGURED = True
        return

    # Lazy imports — we only pay the cost when tracing is on.
    from opentelemetry import trace
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import (
        BatchSpanProcessor,
        ConsoleSpanExporter,
    )
    from opentelemetry.sdk.trace.sampling import TraceIdRatioBased

    service_name = (os.environ.get("OTEL_SERVICE_NAME") or "mobius-chat").strip()
    sample_ratio = _resolve_sample_ratio()

    resource = Resource.create({
        "service.name": service_name,
        "service.version": (os.environ.get("CHAT_RELEASE") or "dev").strip(),
        "deployment.environment": (os.environ.get("CHAT_ENV") or "dev").strip(),
    })
    provider = TracerProvider(
        resource=resource,
        sampler=TraceIdRatioBased(sample_ratio),
    )

    # Exporter selection:
    #   * Cloud Run or K_SERVICE set → Cloud Trace
    #   * CHAT_TRACE_EXPORTER=console → ConsoleSpanExporter (dev diagnostic)
    #   * otherwise → Cloud Trace (assumed hosted)
    exporter_choice = (os.environ.get("CHAT_TRACE_EXPORTER") or "").strip().lower()
    if exporter_choice == "console":
        exporter = ConsoleSpanExporter()
        logger.info("tracing: using ConsoleSpanExporter")
    else:
        try:
            from opentelemetry.exporter.cloud_trace import CloudTraceSpanExporter
            gcp_project = (
                os.environ.get("GOOGLE_CLOUD_PROJECT")
                or os.environ.get("CHAT_GCP_PROJECT")
                or os.environ.get("VERTEX_PROJECT_ID")
            )
            exporter = CloudTraceSpanExporter(project_id=gcp_project) if gcp_project else CloudTraceSpanExporter()
            logger.info("tracing: using CloudTraceSpanExporter (project=%s)", gcp_project or "(default)")
        except Exception as exc:
            logger.warning(
                "tracing: CloudTraceSpanExporter init failed (%s); falling back to console", exc,
            )
            exporter = ConsoleSpanExporter()

    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    _TRACER = trace.get_tracer(service_name)
    _CONFIGURED = True
    logger.info(
        "tracing configured",
        extra={"service_name": service_name, "sample_ratio": sample_ratio},
    )


def instrument_app(app) -> None:
    """Install FastAPI + httpx auto-instrumenters. Called from main.py
    AFTER app construction, before the first request arrives.

    No-op when tracing is disabled."""
    if not tracing_enabled():
        return
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        FastAPIInstrumentor.instrument_app(app, excluded_urls="^/health$,^/ready$")
    except Exception as exc:
        logger.warning("FastAPI instrumentation failed: %s", exc)
    try:
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
        HTTPXClientInstrumentor().instrument()
    except Exception as exc:
        logger.warning("httpx instrumentation failed: %s", exc)


# ── Public tracer access ─────────────────────────────────────────────


def get_tracer():
    """Return the configured tracer, or the default no-op tracer when
    tracing is disabled. Callers never need to branch on ``enabled``
    — ``tracer.start_as_current_span(...)`` is a context manager either
    way."""
    if _TRACER is not None:
        return _TRACER
    try:
        from opentelemetry import trace
        return trace.get_tracer("mobius-chat")
    except ImportError:
        # API not installed — shouldn't happen in a deployed image but
        # keep the caller code safe during hypothetical dep-strip tests.
        return _NoopTracer()


class _NoopSpan:
    """Returned by _NoopTracer when OTel isn't available at all."""

    def __enter__(self) -> "_NoopSpan":
        return self

    def __exit__(self, *_args) -> None:
        pass

    def set_attribute(self, _key: str, _value: Any) -> None:
        pass

    def set_attributes(self, _attrs: dict) -> None:
        pass

    def record_exception(self, _exc: BaseException) -> None:
        pass

    def set_status(self, *_args, **_kwargs) -> None:
        pass


class _NoopTracer:
    def start_as_current_span(self, _name: str, **_kwargs) -> _NoopSpan:
        return _NoopSpan()

    def start_span(self, _name: str, **_kwargs) -> _NoopSpan:
        return _NoopSpan()


# ── Log-record enrichment: trace_id + span_id ────────────────────────


def trace_context_ids() -> tuple[str, str]:
    """Return (trace_id, span_id) as hex strings for the CURRENT span.

    Empty strings when there's no active span (tracing off, or called
    outside any span). The logging_config ContextEnrichmentFilter
    calls this on every LogRecord so Cloud Logging rows carry
    ``trace``/``spanId`` fields that Cloud Trace cross-references.
    """
    if not tracing_enabled():
        return "", ""
    try:
        from opentelemetry import trace
        span = trace.get_current_span()
        ctx = span.get_span_context()
        if not ctx.is_valid:
            return "", ""
        return format(ctx.trace_id, "032x"), format(ctx.span_id, "016x")
    except Exception:
        return "", ""


# ── Convenience: add a span with standard attributes ──────────────────


def start_pipeline_span(
    name: str,
    *,
    correlation_id: str | None = None,
    user_id: str | None = None,
    thread_id: str | None = None,
    stage: str | None = None,
    model: str | None = None,
    extra: dict[str, Any] | None = None,
):
    """Return a context manager that opens a span with the standard
    attribute set stamped on it. Use inside the pipeline to wrap any
    logical unit of work — run_react, per-round, an LLM call.

    Example:
        with start_pipeline_span("react.round", correlation_id=cid,
                                 stage=f"react_{rn}"):
            decision = _call_llm_json(...)

    When tracing is disabled this returns a no-op context manager,
    so call sites don't need to check anything.
    """
    tracer = get_tracer()
    span_cm = tracer.start_as_current_span(name)

    class _Wrapped:
        def __enter__(self):
            self._span = span_cm.__enter__()
            attrs: dict[str, Any] = {}
            if correlation_id:
                attrs["mobius.correlation_id"] = correlation_id
            if user_id:
                attrs["mobius.user_id"] = user_id
            if thread_id:
                attrs["mobius.thread_id"] = thread_id
            if stage:
                attrs["mobius.stage"] = stage
            if model:
                attrs["mobius.model"] = model
            if extra:
                for k, v in extra.items():
                    attrs[f"mobius.{k}"] = v
            if attrs and hasattr(self._span, "set_attributes"):
                try:
                    self._span.set_attributes(attrs)
                except Exception:
                    pass
            return self._span

        def __exit__(self, exc_type, exc, tb):
            return span_cm.__exit__(exc_type, exc, tb)

    return _Wrapped()
