"""LLM provider for chat (Vertex AI, Ollama). Same pattern as Mobius RAG."""
from abc import ABC, abstractmethod
import asyncio
import json
import logging
import queue
import threading
import time
import urllib.error
import urllib.request
from typing import Any, AsyncIterator, Callable, Dict

from app.services.usage import LLMUsageDict, zero_usage, usage_dict
from app.trace_log import trace_entered

logger = logging.getLogger(__name__)

_PROVIDER_REGISTRY: Dict[str, Callable[[Dict[str, Any]], "LLMProvider"]] = {}

# Stages that expect structured JSON from the model — do not attach Vertex AI Search grounding in "general" mode.
_VERTEX_SEARCH_GROUNDING_GENERAL_EXCLUDE_EXACT: frozenset[str] = frozenset({
    "planner",
    "integrator",
    "integrator_roster",
    "rag",
    "context",
    "critique",
    "badge",
    "classifier",
    "adjudicator",
    "phi_detector",
})


def expand_vertex_ai_search_datastore_path(
    configured: str,
    *,
    project_id: str,
) -> str:
    """Resolve full Discovery Engine datastore resource name from config or env.

    - If ``configured`` or env ``VERTEX_AI_SEARCH_DATASTORE`` is set, use it as-is.
    - Else if ``VERTEX_AI_SEARCH_DATASTORE_ID`` is set, build:
      ``projects/{project}/locations/{VERTEX_AI_SEARCH_LOCATION}/collections/default_collection/dataStores/{id}``.
    """
    import os

    path = (configured or "").strip()
    if not path:
        path = (os.getenv("VERTEX_AI_SEARCH_DATASTORE") or "").strip()
    if path:
        return path
    store_id = (os.getenv("VERTEX_AI_SEARCH_DATASTORE_ID") or "").strip()
    if not store_id:
        return ""
    loc = (os.getenv("VERTEX_AI_SEARCH_LOCATION") or "global").strip() or "global"
    pid = (project_id or "").strip()
    if not pid:
        return ""
    return (
        f"projects/{pid}/locations/{loc}/collections/default_collection/dataStores/{store_id}"
    )


def should_attach_vertex_search_grounding(stage: str | None) -> bool:
    """Return whether to pass Vertex AI Search retrieval tool for this ``stage``."""
    import os

    mode = (os.getenv("VERTEX_AI_SEARCH_GROUNDING_MODE") or "credentialing").strip().lower()
    if mode in ("0", "off", "false", "no", "none"):
        return False
    st = (stage or "").strip()
    if mode == "credentialing":
        return st.startswith("credentialing_")
    if mode == "general":
        if st == "planner" or st.startswith("react_"):
            return False
        if st in _VERTEX_SEARCH_GROUNDING_GENERAL_EXCLUDE_EXACT:
            return False
        return True
    logger.warning("Unknown VERTEX_AI_SEARCH_GROUNDING_MODE=%r; use credentialing or general", mode)
    return False


def build_vertex_ai_search_tools(datastore_path: str) -> list | None:
    """Build ``Tool`` list for Gemini grounding, or ``None`` if path empty."""
    ds = (datastore_path or "").strip()
    if not ds:
        return None
    from vertexai.generative_models import Tool
    from vertexai.generative_models import grounding

    retrieval = grounding.Retrieval(grounding.VertexAISearch(datastore=ds))
    return [Tool.from_retrieval(retrieval)]


def register_provider(name: str, factory: Callable[[Dict[str, Any]], "LLMProvider"]) -> None:
    name = (name or "").lower().strip()
    if name:
        _PROVIDER_REGISTRY[name] = factory


class LLMProvider(ABC):
    @abstractmethod
    async def stream_generate(self, prompt: str, **kwargs) -> AsyncIterator[str]:
        pass

    @abstractmethod
    async def generate(self, prompt: str, **kwargs) -> str:
        pass

    async def generate_with_usage(self, prompt: str, **kwargs) -> tuple[str, LLMUsageDict]:
        """Generate and return (text, usage). Default: call generate() and return empty usage."""
        text = await self.generate(prompt, **kwargs)
        return (text, zero_usage())


def _ollama_stream_producer(
    base_url: str, model: str, prompt: str, out: queue.Queue, **kwargs
) -> None:
    """Runs in a thread. Reads Ollama stream line-by-line, puts chunks into out. Puts None when done; puts ('error', msg) on failure."""
    req_data = {"model": model, "prompt": prompt, "stream": True, **kwargs}
    data = json.dumps(req_data).encode("utf-8")
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/api/generate",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            for line in resp:
                line = line.decode("utf-8").strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                    if "response" in d:
                        out.put(d["response"])
                    if d.get("done", False):
                        break
                except json.JSONDecodeError:
                    continue
        out.put(None)
    except Exception as e:
        out.put(("error", str(e)))


def _ollama_request(
    base_url: str, model: str, prompt: str, stream: bool, **kwargs
) -> tuple[str | None, list[str] | None, LLMUsageDict | None]:
    """Returns (error, chunks, usage). usage is set only for non-stream."""
    req_data = {"model": model, "prompt": prompt, "stream": stream, **kwargs}
    data = json.dumps(req_data).encode("utf-8")
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/api/generate",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            if stream:
                chunks = []
                for line in resp:
                    line = line.decode("utf-8").strip()
                    if not line:
                        continue
                    try:
                        d = json.loads(line)
                        if "response" in d:
                            chunks.append(d["response"])
                        if d.get("done", False):
                            break
                    except json.JSONDecodeError:
                        continue
                return (None, chunks, None)
            else:
                body = resp.read().decode("utf-8")
                d = json.loads(body)
                usage = usage_dict(
                    provider="ollama",
                    model=model,
                    input_tokens=int(d.get("prompt_eval_count", 0) or 0),
                    output_tokens=int(d.get("eval_count", 0) or 0),
                )
                return (None, [d.get("response", "")], usage)
    except urllib.error.HTTPError as e:
        try:
            err_body = e.read().decode("utf-8")
        except Exception:
            err_body = ""
        return (f"Ollama API error: {e.code} - {err_body}", None, None)
    except Exception as e:
        return (str(e), None, None)


class OllamaProvider(LLMProvider):
    def __init__(self, base_url: str = "http://localhost:11434", model: str = "llama3.1:8b", num_predict: int = 8192):
        self.base_url = base_url
        self.model = model
        self.num_predict = num_predict

    async def stream_generate(self, prompt: str, **kwargs) -> AsyncIterator[str]:
        kwargs.pop("stage", None)
        opts = {"num_predict": self.num_predict}
        if "options" in kwargs:
            opts = {**opts, **kwargs.pop("options")}
        loop = asyncio.get_running_loop()
        q: queue.Queue = queue.Queue()

        def get() -> object:
            return q.get()

        t = threading.Thread(
            target=_ollama_stream_producer,
            args=(self.base_url, self.model, prompt, q),
            kwargs={"options": opts, **{k: v for k, v in kwargs.items() if k != "options"}},
            daemon=True,
        )
        t.start()
        while True:
            item = await loop.run_in_executor(None, get)
            if item is None:
                break
            if isinstance(item, tuple) and item[0] == "error":
                raise Exception(item[1])
            yield item

    async def generate(self, prompt: str, **kwargs) -> str:
        text, _ = await self.generate_with_usage(prompt, **kwargs)
        return text

    async def generate_with_usage(self, prompt: str, **kwargs) -> tuple[str, LLMUsageDict]:
        kwargs.pop("stage", None)
        opts = {"num_predict": self.num_predict}
        if "options" in kwargs:
            opts = {**opts, **kwargs.pop("options")}
        err, chunks, usage = await asyncio.to_thread(
            _ollama_request, self.base_url, self.model, prompt, False, options=opts, **kwargs
        )
        if err:
            raise Exception(err)
        text = (chunks or [""])[0]
        return (text, usage if usage else zero_usage("ollama", self.model))


def _vertex_stream_producer(
    model_name: str,
    prompt: str,
    gen_config: dict,
    out: queue.Queue,
    tools: list | None = None,
) -> None:
    """Runs in a thread. Streams Vertex AI (Gemini) response into out. Puts delta text only (Gemini may return cumulative .text). Puts None when done; puts ('error', msg) on failure."""
    try:
        from vertexai.generative_models import GenerativeModel
        model = GenerativeModel(model_name)
        kwargs: Dict[str, Any] = {
            "generation_config": gen_config,
            "stream": True,
        }
        if tools:
            kwargs["tools"] = tools
        response = model.generate_content(prompt, **kwargs)
        so_far = ""
        for chunk in response:
            text = getattr(chunk, "text", None) if chunk else None
            if not text:
                continue
            # Vertex/Gemini streaming may return cumulative text; send only the new delta
            if text.startswith(so_far):
                delta = text[len(so_far) :]
                so_far = text
                if delta:
                    out.put(delta)
            else:
                # treat as delta if not cumulative
                so_far = text
                out.put(text)
        out.put(None)
    except Exception as e:
        out.put(("error", str(e)))


# Per-call timeout for Vertex generate_content's underlying HTTP request
# (2026-04-22 latency hardening). Without this, the Vertex SDK's internal
# retry-with-exponential-backoff can keep hammering on 429s for 10+ minutes
# — observed 597s elapsed before surfacing a single 429 in dev logs. That
# (a) starves the worker thread long after the async wrapper's deadline
# tripped, and (b) worsens the quota-throttling feedback loop because the
# retries count against quota.
#
# Contract: ``request_options.timeout`` limits ONE HTTP round-trip, not
# the whole generate_content call. The SDK may still retry up to
# ``max_retries`` times, but each retry is capped. We want aggressive
# per-attempt caps so the bandit can swap models rather than wait.
#
# Ops knob: ``VERTEX_HTTP_TIMEOUT_SECONDS`` (default 30). Bump for large
# credentialing-style generations; the per-call async wrapper at
# ``generate_with_usage`` applies its own ``_timeout_seconds`` on top.
def _vertex_request_options():
    """Build ``generate_content`` kwargs that cap both per-attempt wall
    time AND the SDK's internal retry loop.

    Two knobs pass through ``kwargs`` to ``GenerativeModel.generate_content``:

    * ``timeout``  — per HTTP attempt (``VERTEX_HTTP_TIMEOUT_SECONDS``,
      default 30s). Caps one round-trip.
    * ``retry``   — an ``api_core.retry.Retry`` object with a TOTAL
      ``deadline`` (``VERTEX_TOTAL_DEADLINE_SECONDS``, default 45s).
      Caps the sum of all attempts + backoffs.

    Why both:
    2026-04-24 prod incident — the async wrapper at ``generate_with_usage``
    used ``asyncio.wait_for(asyncio.to_thread(...), timeout_s)``, which
    DOES cancel the awaiting coroutine at ``LLM_TIMEOUT_SECONDS`` (default
    60s) but does NOT kill the underlying thread. The Vertex SDK's
    internal retry-with-exponential-backoff then kept retrying a 429 for
    **596 seconds** inside the thread, holding an instance slot hostage
    long enough for Cloud Run to reap the worker as "unresponsive" — which
    orphaned any in-flight SSE streams on that instance.

    Python's threading model doesn't let us interrupt ``generate_content``
    from outside the thread. The fix must therefore live INSIDE the SDK
    call: the ``retry`` object's ``deadline`` is the only total-wall-clock
    cap the SDK honors. With ``deadline=45``, a 429 returns in ~45s
    max (not 600s), the async wrapper's ``LLM_TIMEOUT_SECONDS`` kicks in
    as a secondary safety net, and the bandit can swap models fast.

    Both values are tunable via env so ops can relax them for long
    generations without a redeploy.
    """
    import os as _os
    try:
        per_attempt = float(_os.getenv("VERTEX_HTTP_TIMEOUT_SECONDS", "30") or 30)
    except (TypeError, ValueError):
        per_attempt = 30.0
    try:
        total_deadline = float(_os.getenv("VERTEX_TOTAL_DEADLINE_SECONDS", "45") or 45)
    except (TypeError, ValueError):
        total_deadline = 45.0

    kwargs: dict = {"timeout": per_attempt}
    try:
        from google.api_core import retry as _retry
        # Retry on transient errors (503/timeout) but NOT on 429 — we
        # want the bandit to swap to a different model rather than
        # hammer the same throttled one. The ``deadline`` caps the
        # total wall time including initial attempt + all retries +
        # backoffs. Initial=1s, multiplier=2, max=8s gives roughly
        # attempts at t=0, 1s, 3s, 7s, 15s, 31s — so 3-4 retries fit
        # under a 45s deadline, sufficient for transient blips without
        # the 10-minute prod bug.
        kwargs["retry"] = _retry.Retry(
            initial=1.0,
            maximum=8.0,
            multiplier=2.0,
            deadline=total_deadline,
        )
    except Exception:
        # Old SDK without api_core retry — fall back to timeout-only.
        pass
    return kwargs


def _vertex_generate_sync(
    model_name: str,
    prompt: str,
    gen_config: dict,
    tools: list | None = None,
) -> tuple[str, LLMUsageDict]:
    import time as _time
    t0 = _time.perf_counter()
    logger.info(
        "[vertex] calling generate_content model=%s prompt_len=%d grounded=%s",
        model_name,
        len(prompt),
        bool(tools),
    )
    # Tracing: one span per Vertex call. The parent is the current
    # pipeline/round span via the OTel context. Attributes match the
    # GenAI semantic-convention draft so Cloud Trace's standard
    # "LLM call" view populates automatically.
    try:
        from app.tracing_config import get_tracer
        _tracer = get_tracer()
        _span_cm = _tracer.start_as_current_span("llm.vertex.generate_content")
        _vertex_span = _span_cm.__enter__()
        try:
            _vertex_span.set_attributes({
                "gen_ai.system": "vertex_ai",
                "gen_ai.request.model": model_name,
                "gen_ai.request.prompt_chars": len(prompt),
                "gen_ai.request.grounded": bool(tools),
            })
        except Exception:
            pass
    except Exception:
        _span_cm = None
        _vertex_span = None
    from vertexai.generative_models import GenerativeModel
    model = GenerativeModel(model_name)
    req_opts = _vertex_request_options()
    try:
        # Defensive kwargs: pass ``timeout`` positionally via kwargs so
        # SDK versions that don't recognize it raise cleanly (caught
        # below) rather than silently ignoring the cap.
        if tools:
            try:
                response = model.generate_content(
                    prompt, generation_config=gen_config, tools=tools, **req_opts
                )
            except TypeError:
                # SDK version doesn't accept timeout; fall back without it.
                response = model.generate_content(
                    prompt, generation_config=gen_config, tools=tools
                )
        else:
            try:
                response = model.generate_content(
                    prompt, generation_config=gen_config, **req_opts
                )
            except TypeError:
                response = model.generate_content(
                    prompt, generation_config=gen_config
                )
    except Exception as e:
        logger.error("[vertex] generate_content raised: %s (elapsed=%.1fs)", e, _time.perf_counter() - t0)
        # Record the exception on the span so Cloud Trace's error
        # overlay picks it up, then close the span before re-raising.
        if _span_cm is not None:
            try:
                if _vertex_span is not None:
                    _vertex_span.record_exception(e)
                _span_cm.__exit__(type(e), e, e.__traceback__)
                _span_cm = None
            except Exception:
                pass
        raise
    logger.info("[vertex] generate_content returned (elapsed=%.1fs)", _time.perf_counter() - t0)
    text = response.text or ""
    usage = zero_usage("vertex", model_name)
    if getattr(response, "usage_metadata", None) is not None:
        um = response.usage_metadata
        usage = usage_dict(
            provider="vertex",
            model=model_name,
            input_tokens=int(getattr(um, "prompt_token_count", 0) or 0),
            output_tokens=int(
                getattr(um, "candidates_token_count", 0)
                or getattr(um, "total_token_count", 0)
                - getattr(um, "prompt_token_count", 0)
                or 0
            ),
        )
    # Decorate the span with response attributes on the happy path, then
    # close it. The attribute writes are best-effort — never let tracing
    # churn break the LLM call.
    if _span_cm is not None:
        try:
            if _vertex_span is not None:
                _vertex_span.set_attributes({
                    "gen_ai.response.input_tokens": int(usage.get("input_tokens") or 0),
                    "gen_ai.response.output_tokens": int(usage.get("output_tokens") or 0),
                    "gen_ai.response.chars": len(text or ""),
                    "gen_ai.response.latency_ms": int((_time.perf_counter() - t0) * 1000),
                })
            _span_cm.__exit__(None, None, None)
        except Exception:
            pass
    return (text, usage)


class VertexAIProvider(LLMProvider):
    def __init__(
        self,
        project_id: str,
        location: str = "us-central1",
        model: str = "gemini-2.5-flash",
        vertex_ai_search_datastore: str = "",
    ):
        import os
        trace_entered("services.llm_provider.VertexAIProvider.__init__", project_id=(project_id or "")[:20] or "(empty)")
        # Never pass None/empty to Vertex SDK; resolve at last moment (handles env not loaded yet or wrong run path)
        pid = (project_id or os.getenv("VERTEX_PROJECT_ID") or os.getenv("CHAT_VERTEX_PROJECT_ID") or "mobiusos-new").strip()
        if not pid or pid.lower() == "none":
            pid = (os.getenv("VERTEX_PROJECT_ID") or os.getenv("CHAT_VERTEX_PROJECT_ID") or "mobiusos-new").strip()
        if not pid:
            pid = "mobiusos-new"
        self._vertex_ai_search_datastore = expand_vertex_ai_search_datastore_path(
            vertex_ai_search_datastore,
            project_id=pid,
        )
        self._vertex_search_tools: list | None = None
        try:
            import vertexai
            vertexai.init(project=pid, location=location)
            self.model_name = model
        except ImportError:
            raise ImportError(
                "Vertex AI requires: pip install google-cloud-aiplatform"
            ) from None
        except Exception as e:
            raise Exception(f"Failed to initialize Vertex AI: {e}") from e

    def _tools_for_vertex_search(self, stage: str | None) -> list | None:
        if not self._vertex_ai_search_datastore:
            return None
        if not should_attach_vertex_search_grounding(stage):
            return None
        if self._vertex_search_tools is None:
            try:
                self._vertex_search_tools = build_vertex_ai_search_tools(
                    self._vertex_ai_search_datastore
                )
            except Exception as e:
                logger.warning("Vertex AI Search tools unavailable: %s", e)
                self._vertex_search_tools = []
        if not self._vertex_search_tools:
            return None
        return self._vertex_search_tools

    def _timeout_seconds(
        self,
        *,
        stage: str | None = None,
        max_tokens: int | None = None,
    ) -> float:
        """Wall-clock cap for Vertex generate_content (asyncio.wait_for).

        Credentialing skill calls (stage credentialing_*) and large max_output_tokens
        need far more than the default 60s — otherwise /internal/skill-llm returns 500
        mid-compose while the model is still generating.
        """
        import os

        try:
            base = float(os.getenv("LLM_TIMEOUT_SECONDS", "60") or 60)
        except (TypeError, ValueError):
            base = 60.0
        st = (stage or "").strip()
        if st.startswith("credentialing_") or st == "integrator_roster":
            try:
                cred = float(os.getenv("CREDENTIALING_LLM_TIMEOUT_SECONDS", "900") or 900)
            except (TypeError, ValueError):
                cred = 900.0
            return max(base, cred)
        try:
            mt = int(max_tokens) if max_tokens is not None else 0
        except (TypeError, ValueError):
            mt = 0
        if mt > 8192:
            scaled = min(900.0, base + mt / 100.0)
            return max(base, scaled)
        return base

    def _generation_config(self, **kwargs) -> dict:
        # Gemini GenerationConfig uses max_output_tokens, not max_tokens
        # llm_manager passes stage for Groq JSON mode — not a valid GenerationConfig field
        kwargs.pop("stage", None)
        cfg: dict = {"temperature": 0.1}
        mt = kwargs.pop("max_tokens", None)
        if mt is not None:
            cfg["max_output_tokens"] = int(mt)
        cfg.update(kwargs)
        return cfg

    async def stream_generate(self, prompt: str, **kwargs) -> AsyncIterator[str]:
        kw = dict(kwargs)
        stage_kw = kw.get("stage")
        max_kw = kw.get("max_tokens")
        gen_config = self._generation_config(**kw)
        tools = self._tools_for_vertex_search(stage_kw)
        loop = asyncio.get_running_loop()
        q: queue.Queue = queue.Queue()
        timeout_s = self._timeout_seconds(stage=stage_kw, max_tokens=max_kw)
        start = time.monotonic()

        def get() -> object:
            return q.get(timeout=1)

        t = threading.Thread(
            target=_vertex_stream_producer,
            args=(self.model_name, prompt, gen_config, q, tools),
            daemon=True,
        )
        t.start()
        while True:
            try:
                item = await loop.run_in_executor(None, get)
            except queue.Empty:
                if time.monotonic() - start > timeout_s:
                    raise Exception(f"LLM stream timed out after {timeout_s:.0f}s")
                continue
            if item is None:
                break
            if isinstance(item, tuple) and item[0] == "error":
                raise Exception(item[1])
            yield item

    async def generate(self, prompt: str, **kwargs) -> str:
        text, _ = await self.generate_with_usage(prompt, **kwargs)
        return text

    async def generate_with_usage(self, prompt: str, **kwargs) -> tuple[str, LLMUsageDict]:
        kw = dict(kwargs)
        stage_kw = kw.get("stage")
        max_kw = kw.get("max_tokens")
        gen_config = self._generation_config(**kw)
        tools = self._tools_for_vertex_search(stage_kw)
        timeout_s = self._timeout_seconds(stage=stage_kw, max_tokens=max_kw)
        return await asyncio.wait_for(
            asyncio.to_thread(
                _vertex_generate_sync, self.model_name, prompt, gen_config, tools
            ),
            timeout=timeout_s,
        )


def _ollama_factory(config: Dict[str, Any]) -> LLMProvider:
    ollama = config.get("ollama") or {}
    from app.chat_config import get_chat_config
    c = get_chat_config().llm
    base_url = ollama.get("base_url") or c.ollama_base_url
    model = config.get("model") or c.ollama_model
    options = config.get("options") or {}
    num_predict = options.get("num_predict", c.ollama_num_predict)
    return OllamaProvider(base_url=base_url, model=model, num_predict=int(num_predict))


def _vertex_factory(config: Dict[str, Any]) -> LLMProvider:
    import os
    trace_entered("services.llm_provider._vertex_factory")
    vertex = config.get("vertex") or {}
    from app.chat_config import get_chat_config
    c = get_chat_config().llm
    # Never raise: always resolve to env or default so worker/planner work even if config built before env loaded
    project_id = (vertex.get("project_id") or c.vertex_project_id or os.getenv("VERTEX_PROJECT_ID") or os.getenv("CHAT_VERTEX_PROJECT_ID") or "mobiusos-new")
    if project_id is not None:
        project_id = str(project_id).strip()
    if not project_id:
        project_id = "mobiusos-new"
    location = vertex.get("location") or c.vertex_location
    model = vertex.get("model") or config.get("model") or c.vertex_model
    v_store = (
        (vertex.get("vertex_ai_search_datastore") or "").strip()
        or (getattr(c, "vertex_ai_search_datastore", None) or "")
    )
    return VertexAIProvider(
        project_id=project_id,
        location=location,
        model=model,
        vertex_ai_search_datastore=v_store,
    )


register_provider("ollama", _ollama_factory)
register_provider("vertex", _vertex_factory)


# ── Groq / Anthropic / Together (Sprint -1) ─────────────────────────────────

# Reasoning models require max_completion_tokens (not max_tokens); see Groq reasoning docs.
_GROQ_REASONING_MODEL_IDS = frozenset({
    "openai/gpt-oss-120b",
    "openai/gpt-oss-20b",
    "openai/gpt-oss-safeguard-20b",
    "qwen/qwen3-32b",
})


def _groq_is_reasoning_model(model_id: str) -> bool:
    mid = (model_id or "").strip()
    if mid in _GROQ_REASONING_MODEL_IDS:
        return True
    if mid.startswith("openai/gpt-oss-"):
        return True
    return False


class GroqProvider(LLMProvider):
    """Groq inference — OpenAI-compatible REST API. No SDK needed."""

    def __init__(self, model: str = "llama-3.3-70b-versatile"):
        import os
        from app.secrets_loader import get_secret
        self.model = model
        # Routes through secrets_loader: env var in dev/tests (unchanged
        # behavior), Secret Manager in hosted envs. See app/secrets_loader.py.
        self.api_key = (get_secret("GROQ_API_KEY") or "").strip()
        if not self.api_key:
            raise ValueError(
                "GROQ_API_KEY not set. Add to .env (dev) or populate "
                "Secret Manager secret 'groq-api-key' (hosted): GROQ_API_KEY=gsk_..."
            )
        self.base_url = "https://api.groq.com/openai/v1"
        self.timeout = float(os.environ.get("LLM_TIMEOUT_SECONDS", "60"))

    async def generate(self, prompt: str, **kwargs) -> str:
        text, _ = await self.generate_with_usage(prompt, **kwargs)
        return text

    async def stream_generate(self, prompt: str, **kwargs):
        text = await self.generate(prompt, **kwargs)
        yield text

    async def generate_with_usage(
        self, prompt: str, **kwargs
    ) -> tuple[str, LLMUsageDict]:
        max_out = int(kwargs.get("max_tokens", 4096))
        temp = float(kwargs.get("temperature", 0.1))
        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": temp,
        }
        if _groq_is_reasoning_model(self.model):
            # Groq reasoning models reject/ignore max_tokens for generation cap; use completion limit.
            payload["max_completion_tokens"] = max_out
            # Put final answer in message.content (not only in message.reasoning).
            payload["reasoning_format"] = str(kwargs.get("reasoning_format") or "hidden")
        else:
            payload["max_tokens"] = max_out

        # ReAct + planner expect a JSON object in message.content. Some Groq chat models otherwise emit
        # OpenAI-style tool_calls; with no `tools` in the request Groq implies tool_choice none and returns
        # 400 "Tool choice is none, but model called a tool". JSON mode keeps output in content.
        # Groq reasoning models (gpt-oss, etc.) are excluded from planner/react in model_registry instead.
        stage = str(kwargs.get("stage") or "")
        if (
            (stage == "planner" or stage.startswith("react_"))
            and not _groq_is_reasoning_model(self.model)
        ):
            payload["response_format"] = {"type": "json_object"}

        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
                "User-Agent": "MobiusChat/1.0",
            },
            method="POST",
        )

        def _call() -> tuple[str, LLMUsageDict]:
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                body_text = ""
                try:
                    body_text = e.read().decode("utf-8")
                except Exception:
                    pass
                raise Exception(f"Groq API error {e.code}: {body_text}") from e
            msg = (data.get("choices", [{}])[0].get("message") or {})
            if not isinstance(msg, dict):
                msg = {}
            text = (msg.get("content") or "").strip()
            if not text and msg.get("tool_calls"):
                # Defensive: native tool_calls without JSON mode (or partial failure)
                try:
                    tc = msg.get("tool_calls")
                    if isinstance(tc, list) and tc:
                        first = tc[0] if isinstance(tc[0], dict) else {}
                        fn = first.get("function") if isinstance(first.get("function"), dict) else {}
                        name = (fn.get("name") or "").strip()
                        raw_args = fn.get("arguments") or "{}"
                        if isinstance(raw_args, str):
                            args_obj = json.loads(raw_args) if raw_args.strip() else {}
                        else:
                            args_obj = raw_args if isinstance(raw_args, dict) else {}
                        text = json.dumps(
                            {
                                "thought": "Native tool call mapped to ReAct shape",
                                "tool": name or None,
                                "inputs": args_obj if isinstance(args_obj, dict) else {},
                                "is_complete": False,
                            }
                        )
                except Exception:
                    text = ""
            if not text and msg.get("reasoning"):
                text = str(msg.get("reasoning") or "").strip()
            u = data.get("usage", {}) or {}
            return text, usage_dict(
                provider="groq",
                model=self.model,
                input_tokens=int(u.get("prompt_tokens") or u.get("input_tokens") or 0),
                output_tokens=int(
                    u.get("completion_tokens") or u.get("output_tokens") or 0
                ),
            )

        return await asyncio.to_thread(_call)


def _groq_factory(config: Dict[str, Any]) -> LLMProvider:
    return GroqProvider(model=config.get("model") or "llama-3.3-70b-versatile")


register_provider("groq", _groq_factory)


class AnthropicProvider(LLMProvider):
    """Anthropic Messages API — urllib, no SDK."""

    def __init__(self, model: str = "claude-haiku-4-5-20251001"):
        import os
        from app.secrets_loader import get_secret
        self.model = model
        # Same pattern as GroqProvider — see app/secrets_loader.py.
        self.api_key = (get_secret("ANTHROPIC_API_KEY") or "").strip()
        if not self.api_key:
            raise ValueError(
                "ANTHROPIC_API_KEY not set. Add to .env (dev) or populate "
                "Secret Manager secret 'anthropic-api-key' (hosted): "
                "ANTHROPIC_API_KEY=sk-ant-..."
            )
        self.base_url = "https://api.anthropic.com/v1"
        self.timeout = float(os.environ.get("LLM_TIMEOUT_SECONDS", "60"))

    async def generate(self, prompt: str, **kwargs) -> str:
        text, _ = await self.generate_with_usage(prompt, **kwargs)
        return text

    async def stream_generate(self, prompt: str, **kwargs):
        text = await self.generate(prompt, **kwargs)
        yield text

    async def generate_with_usage(
        self, prompt: str, **kwargs
    ) -> tuple[str, LLMUsageDict]:
        body = json.dumps({
            "model": self.model,
            "max_tokens": int(kwargs.get("max_tokens", 4096)),
            "messages": [{"role": "user", "content": prompt}],
        }).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}/messages",
            data=body,
            headers={
                "Content-Type": "application/json",
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
            },
            method="POST",
        )

        def _call() -> tuple[str, LLMUsageDict]:
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                body_text = ""
                try:
                    body_text = e.read().decode("utf-8")
                except Exception:
                    pass
                raise Exception(
                    f"Anthropic API error {e.code}: {body_text}"
                ) from e
            text = ""
            for block in data.get("content", []):
                if block.get("type") == "text":
                    text += block.get("text", "")
            u = data.get("usage", {})
            return text, usage_dict(
                provider="anthropic",
                model=self.model,
                input_tokens=int(u.get("input_tokens", 0)),
                output_tokens=int(u.get("output_tokens", 0)),
            )

        return await asyncio.to_thread(_call)


def _anthropic_factory(config: Dict[str, Any]) -> LLMProvider:
    return AnthropicProvider(
        model=config.get("model") or "claude-haiku-4-5-20251001"
    )


register_provider("anthropic", _anthropic_factory)


class TogetherProvider(LLMProvider):
    """Together.ai — OpenAI-compatible REST API."""

    def __init__(
        self,
        model: str = "meta-llama/Meta-Llama-3.1-405B-Instruct-Turbo",
    ):
        import os
        self.model = model
        self.api_key = os.environ.get("TOGETHER_API_KEY", "").strip()
        if not self.api_key:
            raise ValueError(
                "TOGETHER_API_KEY not set. Add to .env: TOGETHER_API_KEY=..."
            )
        self.base_url = "https://api.together.xyz/v1"
        self.timeout = float(os.environ.get("LLM_TIMEOUT_SECONDS", "90"))

    async def generate(self, prompt: str, **kwargs) -> str:
        text, _ = await self.generate_with_usage(prompt, **kwargs)
        return text

    async def stream_generate(self, prompt: str, **kwargs):
        text = await self.generate(prompt, **kwargs)
        yield text

    async def generate_with_usage(
        self, prompt: str, **kwargs
    ) -> tuple[str, LLMUsageDict]:
        body = json.dumps({
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.1,
            "max_tokens": int(kwargs.get("max_tokens", 4096)),
        }).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )

        def _call() -> tuple[str, LLMUsageDict]:
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                body_text = ""
                try:
                    body_text = e.read().decode("utf-8")
                except Exception:
                    pass
                raise Exception(
                    f"Together API error {e.code}: {body_text}"
                ) from e
            text = (
                (data.get("choices", [{}])[0].get("message", {}).get("content", ""))
                or ""
            )
            u = data.get("usage", {})
            return text, usage_dict(
                provider="together",
                model=self.model,
                input_tokens=int(u.get("prompt_tokens", 0)),
                output_tokens=int(u.get("completion_tokens", 0)),
            )

        return await asyncio.to_thread(_call)


def _together_factory(config: Dict[str, Any]) -> LLMProvider:
    return TogetherProvider(
        model=config.get("model")
        or "meta-llama/Meta-Llama-3.1-405B-Instruct-Turbo"
    )


register_provider("together", _together_factory)


def get_llm_provider(parser: bool = False) -> LLMProvider:
    """Get LLM provider from chat config only (CHAT_LLM_*, VERTEX_*, OLLAMA_*). Does not use RAG config.
    Called by: worker (process_one → _answer_for_subquestion → answer_non_patient), planner (parse), responder (final).
    When parser=True, uses parser_vertex_model (default gemini-2.5-pro) for Vertex so parser and rest of chat can use different rate limits.
    _vertex_factory always resolves project_id from config then os.getenv then default 'mobiusos-new' (never raises)."""
    trace_entered("services.llm_provider.get_llm_provider")
    from app.chat_config import get_chat_config
    c = get_chat_config().llm
    parser_cfg = get_chat_config().parser
    provider_name = (c.provider or "ollama").lower()
    vertex_model = getattr(parser_cfg, "parser_vertex_model", None) or c.vertex_model if parser and provider_name == "vertex" else c.vertex_model
    cfg = {
        "provider": provider_name,
        "model": c.ollama_model if provider_name == "ollama" else vertex_model,
        "options": {"num_predict": c.ollama_num_predict} if provider_name == "ollama" else {},
        "ollama": {"base_url": c.ollama_base_url},
        "vertex": {
            "project_id": c.vertex_project_id,
            "location": c.vertex_location,
            "model": vertex_model,
            "vertex_ai_search_datastore": getattr(
                c, "vertex_ai_search_datastore", ""
            )
            or "",
        },
    }
    factory = _PROVIDER_REGISTRY.get(provider_name)
    if factory:
        return factory(cfg)
    raise ValueError(f"Unknown LLM provider: {c.provider}. Use vertex or ollama.")
