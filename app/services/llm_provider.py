"""LLM provider for chat (Vertex AI, Ollama). Same pattern as Mobius RAG."""
from abc import ABC, abstractmethod
import asyncio
import json
import logging
import queue
import threading
import urllib.error
import urllib.request
from typing import Any, AsyncIterator, Callable, Dict

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


def _ollama_request(base_url: str, model: str, prompt: str, stream: bool, **kwargs) -> tuple[str | None, list[str] | None]:
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
                return (None, chunks)
            else:
                body = resp.read().decode("utf-8")
                d = json.loads(body)
                return (None, [d.get("response", "")])
    except urllib.error.HTTPError as e:
        try:
            err_body = e.read().decode("utf-8")
        except Exception:
            err_body = ""
        return (f"Ollama API error: {e.code} - {err_body}", None)
    except Exception as e:
        return (str(e), None)


class OllamaProvider(LLMProvider):
    def __init__(self, base_url: str = "http://localhost:11434", model: str = "llama3.1:8b", num_predict: int = 8192):
        self.base_url = base_url
        self.model = model
        self.num_predict = num_predict

    async def stream_generate(self, prompt: str, **kwargs) -> AsyncIterator[str]:
        opts = {"num_predict": self.num_predict}
        if "options" in kwargs:
            opts = {**opts, **kwargs.pop("options")}
        err, chunks = await asyncio.to_thread(
            _ollama_request, self.base_url, self.model, prompt, True, options=opts, **kwargs
        )
        if err:
            raise Exception(err)
        for c in chunks or []:
            yield c

    async def generate(self, prompt: str, **kwargs) -> str:
        opts = {"num_predict": self.num_predict}
        if "options" in kwargs:
            opts = {**opts, **kwargs.pop("options")}
        err, chunks = await asyncio.to_thread(
            _ollama_request, self.base_url, self.model, prompt, False, options=opts, **kwargs
        )
        if err:
            raise Exception(err)
        return (chunks or [""])[0]


def _vertex_generate_sync(model_name: str, prompt: str, gen_config: dict) -> str:
    from vertexai.generative_models import GenerativeModel
    model = GenerativeModel(model_name)
    response = model.generate_content(prompt, generation_config=gen_config)
    return response.text or ""


class VertexAIProvider(LLMProvider):
    def __init__(self, project_id: str, location: str = "us-central1", model: str = "gemini-2.5-flash"):
        try:
            import vertexai
            vertexai.init(project=project_id, location=location)
            self.model_name = model
        except ImportError:
            raise ImportError(
                "Vertex AI requires: pip install google-cloud-aiplatform"
            ) from None
        except Exception as e:
            raise Exception(f"Failed to initialize Vertex AI: {e}") from e

    def _generation_config(self, **kwargs) -> dict:
        return {"temperature": 0.1, **kwargs}

    async def stream_generate(self, prompt: str, **kwargs) -> AsyncIterator[str]:
        gen_config = self._generation_config(**kwargs)
        # Simple non-streaming fallback for parity; can add stream later
        text = await asyncio.to_thread(
            _vertex_generate_sync, self.model_name, prompt, gen_config
        )
        yield text

    async def generate(self, prompt: str, **kwargs) -> str:
        gen_config = self._generation_config(**kwargs)
        return await asyncio.to_thread(
            _vertex_generate_sync, self.model_name, prompt, gen_config
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
    vertex = config.get("vertex") or {}
    from app.chat_config import get_chat_config
    c = get_chat_config().llm
    project_id = vertex.get("project_id") or c.vertex_project_id
    if not project_id:
        raise ValueError("Vertex AI requires CHAT_VERTEX_PROJECT_ID or VERTEX_PROJECT_ID")
    location = vertex.get("location") or c.vertex_location
    model = config.get("model") or c.vertex_model
    return VertexAIProvider(project_id=project_id, location=location, model=model)


register_provider("ollama", _ollama_factory)
register_provider("vertex", _vertex_factory)


def get_llm_provider() -> LLMProvider:
    """Get LLM provider from chat config only (CHAT_LLM_*, VERTEX_*, OLLAMA_*). Does not use RAG config."""
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
