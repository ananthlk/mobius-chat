"""
Chat-specific config: LLM, parser, prompts. Self-contained; does not use or change RAG configs.
All factors for this module live here for faster development and separation.
"""
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

# Optional: load from YAML if present (app/chat_config.yaml)
_CHAT_CONFIG_DIR = Path(__file__).resolve().parent
_CHAT_CONFIG_YAML = _CHAT_CONFIG_DIR / "chat_config.yaml"


@dataclass
class ChatLLMConfig:
    """LLM factors for chat (separate from RAG). Default: Vertex AI."""
    provider: Literal["ollama", "vertex"] = "vertex"
    model: str = "gemini-2.5-flash"
    temperature: float = 0.1
    # Vertex
    vertex_project_id: str | None = None
    vertex_location: str = "us-central1"
    vertex_model: str = "gemini-2.5-flash"
    # Ollama
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "llama3.1:8b"
    ollama_num_predict: int = 8192


@dataclass
class ChatParserConfig:
    """Parser/planner factors (patient vs non-patient, decomposition)."""
    # Keywords that classify a subquestion as patient-related (no patient RAG yet)
    patient_keywords: list[str] = field(default_factory=lambda: [
        "my doctor", "my medication", "my visit", "my record", "my care", "what did my doctor"
    ])
    # Split user message on these to get subquestions
    decomposition_separators: list[str] = field(default_factory=lambda: [" and ", " also ", " then "])


@dataclass
class ChatPromptsConfig:
    """Prompt templates used by the chat worker (first-gen LLM, etc.)."""
    first_gen_system: str = (
        "You are a helpful assistant. Provide a concise, accurate response. "
        "Do not make up facts; if you don't know, say so."
    )
    first_gen_user_template: str = (
        "The user asked the following question. Provide a helpful, concise response.\n\n"
        "User question: {message}\n\n"
        "Plan: {plan_summary}\n\n"
        "Response:"
    )


@dataclass
class ChatConfig:
    """All chat-specific factors. Env overrides: CHAT_LLM_*, CHAT_PARSER_*, CHAT_PROMPT_*."""
    llm: ChatLLMConfig = field(default_factory=ChatLLMConfig)
    parser: ChatParserConfig = field(default_factory=ChatParserConfig)
    prompts: ChatPromptsConfig = field(default_factory=ChatPromptsConfig)


def _env(key: str, default: str = "") -> str:
    return (os.getenv(key) or default).strip()


def _env_float(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, str(default)))
    except ValueError:
        return default


def get_chat_config() -> ChatConfig:
    """Load chat config from env (CHAT_*). Does not touch RAG config."""
    # LLM: default to Vertex AI; use Ollama only if CHAT_LLM_PROVIDER=ollama or LLM_PROVIDER=ollama
    llm_provider = _env("CHAT_LLM_PROVIDER") or os.getenv("LLM_PROVIDER") or "vertex"
    llm = ChatLLMConfig(
        provider=llm_provider.lower() or "vertex",
        model=_env("CHAT_LLM_MODEL") or (os.getenv("VERTEX_MODEL", "gemini-2.5-flash") if llm_provider.lower() == "vertex" else os.getenv("OLLAMA_MODEL", "llama3.1:8b")) or ("gemini-2.5-flash" if llm_provider.lower() == "vertex" else "llama3.1:8b"),
        temperature=_env_float("CHAT_LLM_TEMPERATURE", 0.1),
        vertex_project_id=_env("CHAT_VERTEX_PROJECT_ID") or os.getenv("VERTEX_PROJECT_ID"),
        vertex_location=_env("CHAT_VERTEX_LOCATION") or os.getenv("VERTEX_LOCATION", "us-central1"),
        vertex_model=_env("CHAT_VERTEX_MODEL") or os.getenv("VERTEX_MODEL", "gemini-2.5-flash"),
        ollama_base_url=_env("CHAT_OLLAMA_BASE_URL") or os.getenv("OLLAMA_BASE_URL", "http://localhost:11434"),
        ollama_model=_env("CHAT_OLLAMA_MODEL") or os.getenv("OLLAMA_MODEL", "llama3.1:8b"),
        ollama_num_predict=int(os.getenv("CHAT_OLLAMA_NUM_PREDICT") or os.getenv("OLLAMA_NUM_PREDICT", "8192")),
    )
    # Parser: optional CHAT_PARSER_PATIENT_KEYWORDS (comma-separated)
    patient_kw = _env("CHAT_PARSER_PATIENT_KEYWORDS")
    parser = ChatParserConfig(
        patient_keywords=[k.strip() for k in patient_kw.split(",") if k.strip()] if patient_kw else [
            "my doctor", "my medication", "my visit", "my record", "my care", "what did my doctor"
        ],
        decomposition_separators=[" and ", " also ", " then "],
    )
    # Prompts: optional CHAT_PROMPT_FIRST_GEN_SYSTEM / CHAT_PROMPT_FIRST_GEN_USER (multiline via \n)
    prompts = ChatPromptsConfig(
        first_gen_system=_env("CHAT_PROMPT_FIRST_GEN_SYSTEM") or (
            "You are a helpful assistant. Provide a concise, accurate response. Do not make up facts; if you don't know, say so."
        ),
        first_gen_user_template=_env("CHAT_PROMPT_FIRST_GEN_USER") or (
            "The user asked the following question. Provide a helpful, concise response.\n\n"
            "User question: {message}\n\n"
            "Plan: {plan_summary}\n\n"
            "Response:"
        ),
    )
    return ChatConfig(llm=llm, parser=parser, prompts=prompts)


def chat_config_for_api() -> dict:
    """Return chat config and prompts as a dict for GET /chat/config (frontend)."""
    c = get_chat_config()
    return {
        "llm": {
            "provider": c.llm.provider,
            "model": c.llm.model,
            "temperature": c.llm.temperature,
            "vertex_project_id": c.llm.vertex_project_id,
            "vertex_location": c.llm.vertex_location,
            "vertex_model": c.llm.vertex_model,
            "ollama_base_url": c.llm.ollama_base_url,
            "ollama_model": c.llm.ollama_model,
            "ollama_num_predict": c.llm.ollama_num_predict,
        },
        "parser": {
            "patient_keywords": c.parser.patient_keywords,
            "decomposition_separators": c.parser.decomposition_separators,
        },
        "prompts": {
            "first_gen_system": c.prompts.first_gen_system,
            "first_gen_user_template": c.prompts.first_gen_user_template,
        },
    }
