"""
Chat-specific config: LLM, parser, prompts. Self-contained; does not use or change RAG configs.
All factors for this module live here for faster development and separation.
"""
import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from app.trace_log import trace_entered

logger = logging.getLogger(__name__)

# Load env: module .env first, then global mobius-config/.env (reusable helper for any module)
_chat_root = Path(__file__).resolve().parent.parent
_config_dir = _chat_root.parent / "mobius-config"
if _config_dir.exists() and str(_config_dir) not in sys.path:
    sys.path.insert(0, str(_config_dir))
try:
    from env_helper import load_env, get_env_or
    load_env(_chat_root)
except ImportError:
    from dotenv import load_dotenv
    env_file = _chat_root / ".env"
    if env_file.exists():
        load_dotenv(env_file, override=True)
    get_env_or = lambda k, d, **_: (os.environ.get(k) or d) or d

# Ensure Vertex project ID is set (SDK may check env; avoid "Vertex AI requires CHAT_VERTEX_PROJECT_ID or VERTEX_PROJECT_ID")
_vp = (os.environ.get("VERTEX_PROJECT_ID") or os.environ.get("CHAT_VERTEX_PROJECT_ID") or "mobiusos-new").strip() or "mobiusos-new"
os.environ.setdefault("VERTEX_PROJECT_ID", _vp)
os.environ.setdefault("CHAT_VERTEX_PROJECT_ID", os.environ.get("VERTEX_PROJECT_ID", "mobiusos-new"))
    # Don't pass placeholder credential paths; resolve to first *.json in credentials/
    _c = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS") or ""
    if "/path/to/" in _c or "your-service-account" in _c or "your-" in _c.lower():
        os.environ.pop("GOOGLE_APPLICATION_CREDENTIALS", None)
    if not os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
        for _d in (_chat_root / "credentials", _chat_root.parent / "mobius-config" / "credentials"):
            if _d.exists():
                for _p in _d.glob("*.json"):
                    if _p.is_file():
                        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = str(_p.resolve())
                        break
                if os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
                    break

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
class ChatRAGConfig:
    """RAG for non-patient: Vertex AI Vector Search + Postgres published_rag_metadata (1536 dims)."""
    # Vertex AI Vector Search (required for RAG)
    vertex_index_endpoint_id: str = ""   # e.g. projects/123/locations/us-central1/indexEndpoints/abc or short id
    vertex_deployed_index_id: str = ""   # deployed index id on the endpoint (e.g. mobius_chat_published_rag)
    # Postgres for published_rag_metadata (metadata only; no embeddings in Postgres)
    database_url: str = ""   # e.g. postgresql://user:pass@host:port/mobius_chat
    top_k: int = 10
    # Optional filter defaults (payer, state, program, authority_level)
    filter_payer: str = ""
    filter_state: str = ""
    filter_program: str = ""
    filter_authority_level: str = ""


@dataclass
class ChatParserConfig:
    """Parser/planner factors (patient vs non-patient, decomposition)."""
    # Keywords that classify a subquestion as patient-related (no patient RAG yet)
    patient_keywords: list[str] = field(default_factory=lambda: [
        "my doctor", "my medication", "my visit", "my record", "my records", "my care",
        "what did my doctor", "do I qualify", "do we qualify", "my eligibility", "based on my",
        "my enrollment", "my coverage", "am I eligible", "are we eligible",
    ])
    # Split user message on these to get subquestions
    decomposition_separators: list[str] = field(default_factory=lambda: [" and ", " also ", " then "])


@dataclass
class ChatPromptsConfig:
    """Prompt templates used by the chat worker (first-gen LLM, planner decomposition, etc.)."""
    # Planner: decompose user message into sub-questions and classify each (JSON output only)
    decompose_system: str = (
        "You are a question decomposition assistant. You do NOT answer questions.\n\n"
        "Your ONLY job: given a user message, output a JSON object that lists the sub-questions and classifies each one. "
        "Do not answer the user's question. Do not give advice, explanations, or any other text. "
        "Your response must be nothing but the JSON object—no preamble, no explanation, no answer.\n\n"
        "Rules for sub-questions:\n"
        "- If the user asked a single clear question, output exactly one sub-question (id sq1, text, kind).\n"
        "- If the user asked multiple questions (e.g. joined with \"and\", \"also\"), output one sub-question per distinct question.\n"
        "- Do not over-split; keep one logical question per item.\n"
        "- Preserve the user's intent; only rephrase slightly if needed.\n\n"
        "Classification (kind) for each sub-question:\n"
        "- patient: about the user's own records, eligibility, care, medications, visits, or personal info. We do not have access to their data.\n"
        "- non_patient: general policy, how-to, contract terms, or other document knowledge we might have.\n\n"
        "Question intent (question_intent) for each sub-question—use for RAG prioritization:\n"
        "- factual: asks for a specific fact, number, date, definition, or lookup (e.g. What is the prior auth requirement? How many days for appeal?)\n"
        "- canonical: asks for general policy, process, or canonical description (e.g. Describe the medical necessity criteria. How does enrollment work?)\n\n"
        "Intent score (intent_score) for each sub-question—a number between 0 and 1:\n"
        "- 0 = fully canonical (policy/process); 1 = fully factual (specific fact/lookup); values in between = blend of both.\n"
        "Use this to set how we retrieve: more hierarchical at 0, more factual at 1.\n\n"
        "Output format (your entire response must be valid JSON in this shape):\n"
        '{"subquestions": [{"id": "sq1", "text": "first subquestion", "kind": "non_patient", "question_intent": "factual", "intent_score": 0.9}, {"id": "sq2", "text": "second subquestion", "kind": "non_patient", "question_intent": "canonical", "intent_score": 0.2}]}'
    )
    decompose_user_template: str = (
        "List the sub-questions in this message as JSON only. Do not answer the question.\n\n"
        "User message:\n{message}\n\n"
        "Output ONLY the JSON object (no other text):"
    )
    # First-gen response (after plan is built)
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
    # Non-patient RAG: answer from retrieved context
    rag_answering_user_template: str = (
        "Use the following context to answer the question. Only use the context; if the context does not contain the answer, say so.\n\n"
        "When the question asks about philosophy, policy, process, or how something works (e.g. care management philosophy, program design), "
        "give a substantive answer that draws on all relevant context—use multiple relevant points and short paragraphs rather than a single sentence.\n\n"
        "Context:\n{context}\n\n"
        "Question: {question}\n\n"
        "Answer:"
    )
    # Final integrator: turn plan + answers into one chat-friendly message
    integrator_system: str = (
        "You are formatting a chat response. Your job is to turn separate answers into one clear, professional message.\n\n"
        "Rules:\n"
        "- Do NOT use internal labels (sq1, sq2, Part 1, etc.). The user never sees those.\n"
        "- Do NOT repeat the user's question(s) back to them.\n"
        "- Write in a warm, professional tone suitable for chat. Use short paragraphs and clear line breaks.\n"
        "- Do NOT use markdown bullets (e.g. * or -). Use plain language, short sentences, or simple line breaks so it reads well in chat.\n"
        "- Merge the content into one coherent reply that directly answers what they asked."
    )
    integrator_user_template: str = (
        "Original user question:\n{user_message}\n\n"
        "Answers we have (use this content to write one combined reply; do not repeat verbatim or list as Part 1 / Part 2):\n{answers_block}\n\n"
        "Write a single chat message that answers the user. No labels, no repeated question, no bullet markdown. Clear paragraphs only."
    )


@dataclass
class ChatConfig:
    """All chat-specific factors. Env overrides: CHAT_LLM_*, CHAT_PARSER_*, CHAT_PROMPT_*, CHAT_RAG_*."""
    llm: ChatLLMConfig = field(default_factory=ChatLLMConfig)
    rag: ChatRAGConfig = field(default_factory=ChatRAGConfig)
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
    trace_entered("chat_config.get_chat_config")
    # LLM: default to Vertex AI; use Ollama only if CHAT_LLM_PROVIDER=ollama or LLM_PROVIDER=ollama
    llm_provider = _env("CHAT_LLM_PROVIDER") or os.getenv("LLM_PROVIDER") or "vertex"
    llm = ChatLLMConfig(
        provider=llm_provider.lower() or "vertex",
        model=_env("CHAT_LLM_MODEL") or (os.getenv("VERTEX_MODEL", "gemini-2.5-flash") if llm_provider.lower() == "vertex" else os.getenv("OLLAMA_MODEL", "llama3.1:8b")) or ("gemini-2.5-flash" if llm_provider.lower() == "vertex" else "llama3.1:8b"),
        temperature=_env_float("CHAT_LLM_TEMPERATURE", 0.1),
        vertex_project_id=(_env("CHAT_VERTEX_PROJECT_ID") or get_env_or("VERTEX_PROJECT_ID", "mobiusos-new") or os.getenv("VERTEX_PROJECT_ID") or os.getenv("CHAT_VERTEX_PROJECT_ID") or "mobiusos-new").strip() or "mobiusos-new",
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
            "my doctor", "my medication", "my visit", "my record", "my records", "my care",
            "what did my doctor", "do I qualify", "do we qualify", "my eligibility", "based on my",
            "my enrollment", "my coverage", "am I eligible", "are we eligible",
        ],
        decomposition_separators=[" and ", " also ", " then "],
    )
    # RAG: Vertex AI Vector Search + Postgres published_rag_metadata
    vertex_endpoint = _env("CHAT_VERTEX_INDEX_ENDPOINT_ID") or os.getenv("VERTEX_INDEX_ENDPOINT_ID")
    vertex_deployed_raw = _env("CHAT_VERTEX_DEPLOYED_INDEX_ID") or os.getenv("VERTEX_DEPLOYED_INDEX_ID")
    logger.info(
        "[RAG config] env: CHAT_VERTEX_DEPLOYED_INDEX_ID=%r VERTEX_DEPLOYED_INDEX_ID=%r → raw=%r _chat_root=%s",
        _env("CHAT_VERTEX_DEPLOYED_INDEX_ID"),
        os.getenv("VERTEX_DEPLOYED_INDEX_ID"),
        vertex_deployed_raw,
        _chat_root,
    )
    vertex_deployed = vertex_deployed_raw
    # Vertex API requires the deployed index ID (e.g. endpoint_mobius_chat_publi_1769989702095), not the display name
    if vertex_deployed and vertex_deployed.strip() in ("Endpoint_mobius_chat_published_rag", "mobius_chat_published_rag"):
        vertex_deployed = "endpoint_mobius_chat_publi_1769989702095"
        logger.info("[RAG config] normalized display name → deployed_index_id=%r", vertex_deployed)
    rag_db_url = _env("CHAT_RAG_DATABASE_URL") or os.getenv("RAG_DATABASE_URL") or os.getenv("CHAT_DATABASE_URL")
    rag_k = int(os.getenv("CHAT_RAG_TOP_K") or os.getenv("RAG_TOP_K", "10"))
    rag = ChatRAGConfig(
        vertex_index_endpoint_id=vertex_endpoint,
        vertex_deployed_index_id=vertex_deployed,
        database_url=rag_db_url,
        top_k=max(1, min(100, rag_k)),
        filter_payer=_env("CHAT_RAG_FILTER_PAYER"),
        filter_state=_env("CHAT_RAG_FILTER_STATE"),
        filter_program=_env("CHAT_RAG_FILTER_PROGRAM"),
        filter_authority_level=_env("CHAT_RAG_FILTER_AUTHORITY_LEVEL"),
    )
    # Prompts: optional CHAT_PROMPT_DECOMPOSE_*, CHAT_PROMPT_FIRST_GEN_*, CHAT_PROMPT_RAG_*
    prompts = ChatPromptsConfig(
        decompose_system=_env("CHAT_PROMPT_DECOMPOSE_SYSTEM") or ChatPromptsConfig.decompose_system,
        decompose_user_template=_env("CHAT_PROMPT_DECOMPOSE_USER_TEMPLATE") or ChatPromptsConfig.decompose_user_template,
        first_gen_system=_env("CHAT_PROMPT_FIRST_GEN_SYSTEM") or (
            "You are a helpful assistant. Provide a concise, accurate response. Do not make up facts; if you don't know, say so."
        ),
        first_gen_user_template=_env("CHAT_PROMPT_FIRST_GEN_USER") or (
            "The user asked the following question. Provide a helpful, concise response.\n\n"
            "User question: {message}\n\n"
            "Plan: {plan_summary}\n\n"
            "Response:"
        ),
        rag_answering_user_template=_env("CHAT_PROMPT_RAG_ANSWERING_USER") or ChatPromptsConfig.rag_answering_user_template,
        integrator_system=_env("CHAT_PROMPT_INTEGRATOR_SYSTEM") or ChatPromptsConfig.integrator_system,
        integrator_user_template=_env("CHAT_PROMPT_INTEGRATOR_USER") or ChatPromptsConfig.integrator_user_template,
    )
    return ChatConfig(llm=llm, rag=rag, parser=parser, prompts=prompts)


def chat_config_for_api() -> dict:
    """Return chat config and prompts as a dict for GET /chat/config (frontend)."""
    c = get_chat_config()
    return {
        "rag": {
            "vertex_index_endpoint_id_set": bool(c.rag.vertex_index_endpoint_id),
            "vertex_deployed_index_id_set": bool(c.rag.vertex_deployed_index_id),
            "database_url_set": bool(c.rag.database_url),
            "top_k": c.rag.top_k,
        },
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
            "decompose_system": c.prompts.decompose_system,
            "decompose_user_template": c.prompts.decompose_user_template,
            "first_gen_system": c.prompts.first_gen_system,
            "first_gen_user_template": c.prompts.first_gen_user_template,
            "integrator_system": c.prompts.integrator_system,
            "integrator_user_template": c.prompts.integrator_user_template,
        },
    }
