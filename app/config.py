"""Config from environment. Single place for queue type, storage, LLM, etc."""
import os
from dataclasses import dataclass
from typing import Literal


@dataclass
class Config:
    """App config. Load from env or defaults."""
    queue_type: Literal["memory", "redis", "pubsub"] = "memory"
    redis_url: str = "redis://localhost:6379/0"
    redis_request_key: str = "mobius:chat:requests"
    redis_response_key_prefix: str = "mobius:chat:response:"
    redis_response_ttl_seconds: int = 86400  # 24h
    storage_backend: Literal["memory"] = "memory"
    api_base_url: str = "http://localhost:8000"
    # LLM (same pattern as Mobius RAG: vertex for prod, ollama for local)
    llm_provider: Literal["ollama", "vertex"] = "ollama"
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "llama3.1:8b"
    ollama_num_predict: int = 8192
    vertex_project_id: str | None = None
    vertex_location: str = "us-central1"
    vertex_model: str = "gemini-2.5-flash"


def get_config() -> Config:
    return Config(
        queue_type=os.getenv("QUEUE_TYPE", "memory"),
        redis_url=os.getenv("REDIS_URL", "redis://localhost:6379/0"),
        redis_request_key=os.getenv("REDIS_REQUEST_KEY", "mobius:chat:requests"),
        redis_response_key_prefix=os.getenv("REDIS_RESPONSE_KEY_PREFIX", "mobius:chat:response:"),
        redis_response_ttl_seconds=int(os.getenv("REDIS_RESPONSE_TTL_SECONDS", "86400")),
        storage_backend=os.getenv("STORAGE_BACKEND", "memory"),
        api_base_url=os.getenv("API_BASE_URL", "http://localhost:8000"),
        llm_provider=os.getenv("LLM_PROVIDER", "vertex" if os.getenv("VERTEX_PROJECT_ID") else "ollama") or "ollama",
        ollama_base_url=os.getenv("OLLAMA_BASE_URL", "http://localhost:11434"),
        ollama_model=os.getenv("OLLAMA_MODEL", "llama3.1:8b"),
        ollama_num_predict=int(os.getenv("OLLAMA_NUM_PREDICT", "8192")),
        vertex_project_id=os.getenv("VERTEX_PROJECT_ID"),
        vertex_location=os.getenv("VERTEX_LOCATION", "us-central1"),
        vertex_model=os.getenv("VERTEX_MODEL", "gemini-2.5-flash"),
    )
