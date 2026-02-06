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


def _vertex_stream_producer(model_name: str, prompt: str, gen_config: dict, out: queue.Queue) -> None:
    """Runs in a thread. Streams Vertex AI (Gemini) response into out. Puts delta text only (Gemini may return cumulative .text). Puts None when done; puts ('error', msg) on failure."""
    try:
        from vertexai.generative_models import GenerativeModel
        model = GenerativeModel(model_name)
        response = model.generate_content(
            prompt,
            generation_config=gen_config,
            stream=True,
        )
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


def _vertex_generate_sync(model_name: str, prompt: str, gen_config: dict) -> tuple[str, LLMUsageDict]:
    import time as _time
    t0 = _time.perf_counter()
    logger.info("[vertex] calling generate_content model=%s prompt_len=%d", model_name, len(prompt))
    from vertexai.generative_models import GenerativeModel
    model = GenerativeModel(model_name)
    try:
        response = model.generate_content(prompt, generation_config=gen_config)
    except Exception as e:
        logger.error("[vertex] generate_content raised: %s (elapsed=%.1fs)", e, _time.perf_counter() - t0)
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
    return (text, usage)


class VertexAIProvider(LLMProvider):
    def __init__(self, project_id: str, location: str = "us-central1", model: str = "gemini-2.0-flash"):
        import os
        trace_entered("services.llm_provider.VertexAIProvider.__init__", project_id=(project_id or "")[:20] or "(empty)")
        # Never pass None/empty to Vertex SDK; resolve at last moment (handles env not loaded yet or wrong run path)
        pid = (project_id or os.getenv("VERTEX_PROJECT_ID") or os.getenv("CHAT_VERTEX_PROJECT_ID") or "mobiusos-new").strip()
        if not pid or pid.lower() == "none":
            pid = (os.getenv("VERTEX_PROJECT_ID") or os.getenv("CHAT_VERTEX_PROJECT_ID") or "mobiusos-new").strip()
        if not pid:
            pid = "mobiusos-new"
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

    def _timeout_seconds(self) -> float:
        import os
        try:
            return float(os.getenv("LLM_TIMEOUT_SECONDS", "60") or 60)
        except (TypeError, ValueError):
            return 60.0

    def _generation_config(self, **kwargs) -> dict:
        return {"temperature": 0.1, **kwargs}

    async def stream_generate(self, prompt: str, **kwargs) -> AsyncIterator[str]:
        gen_config = self._generation_config(**kwargs)
        loop = asyncio.get_running_loop()
        q: queue.Queue = queue.Queue()
        timeout_s = self._timeout_seconds()
        start = time.monotonic()

        def get() -> object:
            return q.get(timeout=1)

        t = threading.Thread(
            target=_vertex_stream_producer,
            args=(self.model_name, prompt, gen_config, q),
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
        gen_config = self._generation_config(**kwargs)
        timeout_s = self._timeout_seconds()
        return await asyncio.wait_for(
            asyncio.to_thread(_vertex_generate_sync, self.model_name, prompt, gen_config),
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
    model = config.get("model") or c.vertex_model
    return VertexAIProvider(project_id=project_id, location=location, model=model)


register_provider("ollama", _ollama_factory)
register_provider("vertex", _vertex_factory)


def get_llm_provider() -> LLMProvider:
    """Get LLM provider from chat config only (CHAT_LLM_*, VERTEX_*, OLLAMA_*). Does not use RAG config.
    Called by: worker (process_one → _answer_for_subquestion → answer_non_patient), planner (parse), responder (final).
    _vertex_factory always resolves project_id from config then os.getenv then default 'mobiusos-new' (never raises)."""
    trace_entered("services.llm_provider.get_llm_provider")
    from app.chat_config import get_chat_config
    c = get_chat_config().llm
    provider_name = (c.provider or "ollama").lower()
    cfg = {
        "provider": provider_name,
        "model": c.ollama_model if provider_name == "ollama" else c.vertex_model,
        "options": {"num_predict": c.ollama_num_predict} if provider_name == "ollama" else {},
        "ollama": {"base_url": c.ollama_base_url},
        "vertex": {"project_id": c.vertex_project_id, "location": c.vertex_location},
    }
    factory = _PROVIDER_REGISTRY.get(provider_name)
    if factory:
        return factory(cfg)
    raise ValueError(f"Unknown LLM provider: {c.provider}. Use vertex or ollama.")
