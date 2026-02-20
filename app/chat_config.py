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

try:
    from app.trace_log import trace_entered
except Exception:
    def trace_entered(_component: str, **_kwargs: Any) -> None:
        pass

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

# Don't pass placeholder credential paths; resolve to first *.json in credentials/ (run after env load)
_c = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS") or ""
if "/path/to/" in _c or "your-service-account" in _c or "your-" in (_c or "").lower():
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

# Ensure Vertex project ID is set (SDK may check env; avoid "Vertex AI requires CHAT_VERTEX_PROJECT_ID or VERTEX_PROJECT_ID")
_vp = (os.environ.get("VERTEX_PROJECT_ID") or os.environ.get("CHAT_VERTEX_PROJECT_ID") or "mobiusos-new").strip() or "mobiusos-new"
os.environ.setdefault("VERTEX_PROJECT_ID", _vp)
os.environ.setdefault("CHAT_VERTEX_PROJECT_ID", os.environ.get("VERTEX_PROJECT_ID", "mobiusos-new"))

# Optional: load from YAML if present (app/chat_config.yaml)
_CHAT_CONFIG_DIR = Path(__file__).resolve().parent
_CHAT_CONFIG_YAML = _CHAT_CONFIG_DIR / "chat_config.yaml"

# Prompts+LLM versioned config (config/prompts_llm.yaml); when present, used for llm/parser/prompts
try:
    from app.prompts_llm_config import load_prompts_llm_config
except Exception:
    def load_prompts_llm_config() -> tuple[dict, str]:
        return {}, ""


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
        "- If the user asks to compare multiple entities (e.g. \"compare the care management programs of Sunshine Health, United Healthcare and Molina\"), output one sub-question per entity (e.g. What is Sunshine Health's care management program? What is United Healthcare's? What is Molina's?) and optionally one for a direct comparison; this allows retrieval and answer for each.\n"
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
        "{context}\n\n"
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
    # Non-patient RAG: answer from retrieved context (first drafter; integrator customizes final message)
    rag_answering_user_template: str = (
        "Use the following context to answer the question. Only use the context; if the context does not contain the answer, say so.\n\n"
        "When the question asks about philosophy, policy, process, or how something works (e.g. care management philosophy, program design), "
        "give a substantive answer that draws on all relevant context—use multiple relevant points and short paragraphs rather than a single sentence.\n\n"
        "When you have enough in the context to suggest concrete next steps, include 1–2 viable next steps the user could take (e.g. a tool, form, or follow-up action mentioned in the context). "
        "Only suggest next steps that are logical and that the context supports; do not invent actions. If the context does not support any clear next step, omit this.\n\n"
        "Context:\n{context}\n\n"
        "Question: {question}\n\n"
        "Answer:"
    )
    # Consolidator thresholds: blended canonical score → factual | canonical | blended
    consolidator_factual_max: float = 0.4   # canonical score < this → factual
    consolidator_canonical_min: float = 0.6  # canonical score > this → canonical; else blended

    # Final integrator (legacy single prompt; used as fallback if dynamic prompts not set)
    integrator_system: str = (
        "You are formatting a chat response. Your job is to turn separate answers into one clear, professional message.\n\n"
        "Structure the reply in this order:\n"
        "1. Short answer — one or two sentences that give the bottom line (e.g. \"Yes — prior authorization is required for certain services.\" or \"No — that typically does not require prior auth.\").\n"
        "2. More detail — when something is required, when it is not, exceptions, or other context. Use short paragraphs and clear line breaks; no markdown bullets (no * or -).\n"
        "3. Next steps — if the answers suggest concrete next steps (tools, forms, follow-up), put them in a clear \"what to do next\" line or short paragraph; otherwise omit this section.\n\n"
        "Rules:\n"
        "- Do NOT use internal labels (sq1, sq2, Part 1, etc.). The user never sees those.\n"
        "- Do NOT repeat the user's question(s) back to them.\n"
        "- Write in a warm, professional tone suitable for chat.\n"
        "- Merge the content into one coherent reply that directly answers what they asked."
    )
    integrator_user_template: str = (
        "Input (JSON):\n{consolidator_input_json}\n\n"
        "Using ONLY the information in the JSON above, return a single valid JSON object matching the AnswerCard schema. "
        "Do not include markdown, explanations, or extra text. Return ONLY the JSON."
    )

    # Repair prompt when LLM returns invalid JSON (one retry)
    integrator_repair_system: str = (
        "You returned invalid JSON. Return ONLY valid JSON that matches the AnswerCard schema. "
        "Do not include any commentary or markdown. Ensure all strings are quoted and arrays/objects are valid. "
        "Each section must include \"intent\" (one of: process, requirements, definitions, exceptions, references). "
        "Use the same content as before; do not add new facts."
    )

    # Dynamic consolidator: three system prompts (JSON AnswerCard output)
    integrator_factual_system: str = (
        "You are the CONSOLIDATOR for a retrieval-based system.\n\n"
        "Return ONLY valid JSON matching the AnswerCard schema below. Do not include markdown, explanations, or extra text.\n\n"
        "AnswerCard schema:\n"
        '{"mode":"FACTUAL","direct_answer":"string","sections":[{"intent":"process|requirements|definitions|exceptions|references","label":"string","bullets":["string"]}],'
        '"required_variables":["string"],"confidence_note":"string",'
        '"citations":[{"id":"string","doc_title":"string","locator":"string","snippet":"string"}],'
        '"followups":[{"question":"string","reason":"string","field":"string"}]}\n\n'
        "Rules for FACTUAL mode:\n"
        "- direct_answer is required and must stand alone.\n"
        "- Classify each section with exactly one intent: process, requirements, definitions, exceptions, or references. You do not control visibility; the UI will show only the direct answer and hide sections behind 'Show details'.\n"
        "- Use ONLY the facts provided in the input. Do not add new facts.\n"
        "- Do not include policy intent or justification language.\n"
        "- Prefer short bullets; do not write paragraphs.\n"
        "- Include at most 3 sections and at most 4 bullets per section.\n"
        "- If the answer depends on an unknown variable (service code, setting, plan subtype), put it in required_variables.\n"
        "- Only add followups if required_variables is non-empty and the user must provide something to be definitive.\n"
        "- direct_answer must be one sentence, operational, and non-hedgy.\n"
        "If the facts are insufficient: direct_answer should say what is missing (one sentence). "
        "sections may include a label \"What's missing\" with bullets. Do not guess."
    )
    integrator_canonical_system: str = (
        "You are the CONSOLIDATOR for a retrieval-based system.\n\n"
        "Return ONLY valid JSON matching the AnswerCard schema below. Do not include markdown, explanations, or extra text.\n\n"
        "AnswerCard schema:\n"
        '{"mode":"CANONICAL","direct_answer":"string","sections":[{"intent":"process|requirements|definitions|exceptions|references","label":"string","bullets":["string"]}],'
        '"required_variables":["string"],"confidence_note":"string",'
        '"citations":[{"id":"string","doc_title":"string","locator":"string","snippet":"string"}],'
        '"followups":[{"question":"string","reason":"string","field":"string"}]}\n\n'
        "Rules for CANONICAL mode:\n"
        "- direct_answer is required and must stand alone. Classify each section with exactly one intent: process, requirements, definitions, exceptions, or references. The UI will show direct_answer and all sections.\n"
        "- Use ONLY the information provided in the input.\n"
        "- Explain the standard definition/rule/scope in a stable, reusable way.\n"
        "- Avoid edge cases unless explicitly included in the facts.\n"
        "- Prefer bullets; keep it scannable for non-technical users.\n"
        "- Include 2–4 sections max, 2–4 bullets per section.\n"
        "- required_variables should usually be empty unless the concept inherently depends on a variable the user asked about.\n"
        "- direct_answer should be a one-sentence summary of the canonical rule.\n"
        "- Do not include procedural \"how to submit\" steps unless the policy explicitly describes the process.\n"
        "If insufficient: direct_answer should state what is missing to give a canonical explanation."
    )
    integrator_blended_system: str = (
        "You are the CONSOLIDATOR for a retrieval-based system.\n\n"
        "Return ONLY valid JSON matching the AnswerCard schema below. Do not include markdown, explanations, or extra text.\n\n"
        "AnswerCard schema:\n"
        '{"mode":"BLENDED","direct_answer":"string","sections":[{"intent":"process|requirements|definitions|exceptions|references","label":"string","bullets":["string"]}],'
        '"required_variables":["string"],"confidence_note":"string",'
        '"citations":[{"id":"string","doc_title":"string","locator":"string","snippet":"string"}],'
        '"followups":[{"question":"string","reason":"string","field":"string"}]}\n\n'
        "Rules for BLENDED mode:\n"
        "- direct_answer is required and must stand alone. Classify each section with exactly one intent: process, requirements, definitions, exceptions, or references. The UI will show direct_answer and requirements sections; process, definitions, exceptions, and references will be behind 'Show details'.\n"
        "- Use ONLY the information provided in the input.\n"
        "- Start with a short explanatory summary in direct_answer (1–2 sentences max).\n"
        "- Then provide concrete requirements/conditions as bullets in sections.\n"
        "- Include a practical note section only if supported by the facts.\n"
        "- Include 2–4 sections max, 2–4 bullets per section.\n"
        "- If the answer depends on an unknown variable, include it in required_variables and add at most one followup question.\n"
        "- Do not speculate or add hypotheticals.\n"
        "If insufficient: direct_answer should state what is missing; do not guess."
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


def _build_rag_from_env() -> ChatRAGConfig:
    """Build RAG config from env only (unchanged from original get_chat_config)."""
    vertex_endpoint = _env("CHAT_VERTEX_INDEX_ENDPOINT_ID") or os.getenv("VERTEX_INDEX_ENDPOINT_ID")
    vertex_deployed_raw = _env("CHAT_VERTEX_DEPLOYED_INDEX_ID") or os.getenv("VERTEX_DEPLOYED_INDEX_ID")
    logger.debug(
        "[RAG config] env: CHAT_VERTEX_DEPLOYED_INDEX_ID=%r VERTEX_DEPLOYED_INDEX_ID=%r → raw=%r _chat_root=%s",
        _env("CHAT_VERTEX_DEPLOYED_INDEX_ID"),
        os.getenv("VERTEX_DEPLOYED_INDEX_ID"),
        vertex_deployed_raw,
        _chat_root,
    )
    vertex_deployed = (vertex_deployed_raw or "").strip()
    if vertex_deployed and not vertex_deployed.startswith("endpoint_mobius_chat_publi_") and (
        vertex_deployed in ("Endpoint_mobius_chat_published_rag", "mobius_chat_published_rag")
        or "published_rag" in vertex_deployed.lower()
    ):
        vertex_deployed = "endpoint_mobius_chat_publi_1769989702095"
        logger.info("[RAG config] normalized display name → deployed_index_id=%r", vertex_deployed)
    # DB URL conventions across repo:
    # - mobius-chat uses CHAT_RAG_DATABASE_URL (preferred)
    # - some scripts use RAG_DATABASE_URL
    # - mobius-dbt uses CHAT_DATABASE_URL as the destination (chat) Postgres URL
    rag_db_url = _env("CHAT_RAG_DATABASE_URL") or os.getenv("RAG_DATABASE_URL") or os.getenv("CHAT_DATABASE_URL")
    rag_k = int(os.getenv("CHAT_RAG_TOP_K") or os.getenv("RAG_TOP_K", "10"))
    return ChatRAGConfig(
        vertex_index_endpoint_id=vertex_endpoint,
        vertex_deployed_index_id=vertex_deployed,
        database_url=rag_db_url,
        top_k=max(1, min(100, rag_k)),
        filter_payer=_env("CHAT_RAG_FILTER_PAYER"),
        filter_state=_env("CHAT_RAG_FILTER_STATE"),
        filter_program=_env("CHAT_RAG_FILTER_PROGRAM"),
        filter_authority_level=_env("CHAT_RAG_FILTER_AUTHORITY_LEVEL"),
    )


def _chat_config_from_prompts_llm(pl: dict, rag: ChatRAGConfig) -> ChatConfig:
    """Build ChatConfig from prompts_llm dict (llm, parser, prompts). RAG is from env."""
    def _str(d: dict, k: str, default: str = "") -> str:
        v = d.get(k)
        return (v.strip() if isinstance(v, str) else str(v)) if v is not None else default

    def _float(d: dict, k: str, default: float = 0.0) -> float:
        try:
            v = d.get(k)
            return float(v) if v is not None else default
        except (TypeError, ValueError):
            return default

    def _int(d: dict, k: str, default: int = 0) -> int:
        try:
            v = d.get(k)
            return int(v) if v is not None else default
        except (TypeError, ValueError):
            return default

    def _list_str(d: dict, k: str, default: list[str] | None = None) -> list[str]:
        v = d.get(k)
        if isinstance(v, list):
            return [x.strip() if isinstance(x, str) else str(x) for x in v if x is not None]
        return default or []

    llm_d = pl.get("llm") or {}
    vertex_project = (llm_d.get("vertex_project_id") or "").strip() or None
    if not vertex_project:
        vertex_project = (_env("CHAT_VERTEX_PROJECT_ID") or get_env_or("VERTEX_PROJECT_ID", "mobiusos-new") or "mobiusos-new").strip() or "mobiusos-new"
    llm = ChatLLMConfig(
        provider=(_str(llm_d, "provider") or "vertex").lower(),
        model=_str(llm_d, "model") or "gemini-2.5-flash",
        temperature=_float(llm_d, "temperature", 0.1),
        vertex_project_id=vertex_project or None,
        vertex_location=_str(llm_d, "vertex_location") or "us-central1",
        vertex_model=_str(llm_d, "vertex_model") or "gemini-2.5-flash",
        ollama_base_url=_str(llm_d, "ollama_base_url") or "http://localhost:11434",
        ollama_model=_str(llm_d, "ollama_model") or "llama3.1:8b",
        ollama_num_predict=_int(llm_d, "ollama_num_predict", 8192),
    )
    parser_d = pl.get("parser") or {}
    parser = ChatParserConfig(
        patient_keywords=_list_str(parser_d, "patient_keywords") or [
            "my doctor", "my medication", "my visit", "my record", "my records", "my care",
            "what did my doctor", "do I qualify", "do we qualify", "my eligibility", "based on my",
            "my enrollment", "my coverage", "am I eligible", "are we eligible",
        ],
        decomposition_separators=_list_str(parser_d, "decomposition_separators") or [" and ", " also ", " then "],
    )
    prompts_d = pl.get("prompts") or {}
    _def = ChatPromptsConfig()
    prompts = ChatPromptsConfig(
        decompose_system=_str(prompts_d, "decompose_system") or _def.decompose_system,
        decompose_user_template=_str(prompts_d, "decompose_user_template") or _def.decompose_user_template,
        first_gen_system=_str(prompts_d, "first_gen_system") or _def.first_gen_system,
        first_gen_user_template=_str(prompts_d, "first_gen_user_template") or _def.first_gen_user_template,
        rag_answering_user_template=_str(prompts_d, "rag_answering_user_template") or _def.rag_answering_user_template,
        consolidator_factual_max=_float(prompts_d, "consolidator_factual_max", _def.consolidator_factual_max),
        consolidator_canonical_min=_float(prompts_d, "consolidator_canonical_min", _def.consolidator_canonical_min),
        integrator_system=_str(prompts_d, "integrator_system") or _def.integrator_system,
        integrator_user_template=_str(prompts_d, "integrator_user_template") or _def.integrator_user_template,
        integrator_repair_system=_str(prompts_d, "integrator_repair_system") or _def.integrator_repair_system,
        integrator_factual_system=_str(prompts_d, "integrator_factual_system") or _def.integrator_factual_system,
        integrator_canonical_system=_str(prompts_d, "integrator_canonical_system") or _def.integrator_canonical_system,
        integrator_blended_system=_str(prompts_d, "integrator_blended_system") or _def.integrator_blended_system,
    )
    return ChatConfig(llm=llm, rag=rag, parser=parser, prompts=prompts)


def get_config_sha() -> str:
    """Return config_sha when prompts_llm.yaml is in use; otherwise empty string."""
    _, sha = load_prompts_llm_config()
    return sha


def get_chat_config() -> ChatConfig:
    """Load chat config: prompts_llm.yaml when present (llm/parser/prompts), else env. RAG always from env."""
    trace_entered("chat_config.get_chat_config")
    pl_config, _ = load_prompts_llm_config()
    rag = _build_rag_from_env()
    if pl_config:
        return _chat_config_from_prompts_llm(pl_config, rag)
    # Fallback: env-only (no prompts_llm.yaml)
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
    patient_kw = _env("CHAT_PARSER_PATIENT_KEYWORDS")
    parser = ChatParserConfig(
        patient_keywords=[k.strip() for k in patient_kw.split(",") if k.strip()] if patient_kw else [
            "my doctor", "my medication", "my visit", "my record", "my records", "my care",
            "what did my doctor", "do I qualify", "do we qualify", "my eligibility", "based on my",
            "my enrollment", "my coverage", "am I eligible", "are we eligible",
        ],
        decomposition_separators=[" and ", " also ", " then "],
    )
    # Prompts: optional CHAT_PROMPT_*, CHAT_CONSOLIDATOR_*
    _prompts_default = ChatPromptsConfig()
    consolidator_factual_max = _env_float("CHAT_CONSOLIDATOR_FACTUAL_MAX", _prompts_default.consolidator_factual_max)
    consolidator_canonical_min = _env_float("CHAT_CONSOLIDATOR_CANONICAL_MIN", _prompts_default.consolidator_canonical_min)
    prompts = ChatPromptsConfig(
        decompose_system=_env("CHAT_PROMPT_DECOMPOSE_SYSTEM") or _prompts_default.decompose_system,
        decompose_user_template=_env("CHAT_PROMPT_DECOMPOSE_USER_TEMPLATE") or _prompts_default.decompose_user_template,
        first_gen_system=_env("CHAT_PROMPT_FIRST_GEN_SYSTEM") or (
            "You are a helpful assistant. Provide a concise, accurate response. Do not make up facts; if you don't know, say so."
        ),
        first_gen_user_template=_env("CHAT_PROMPT_FIRST_GEN_USER") or (
            "The user asked the following question. Provide a helpful, concise response.\n\n"
            "User question: {message}\n\n"
            "Plan: {plan_summary}\n\n"
            "Response:"
        ),
        rag_answering_user_template=_env("CHAT_PROMPT_RAG_ANSWERING_USER") or _prompts_default.rag_answering_user_template,
        consolidator_factual_max=consolidator_factual_max,
        consolidator_canonical_min=consolidator_canonical_min,
        integrator_system=_env("CHAT_PROMPT_INTEGRATOR_SYSTEM") or _prompts_default.integrator_system,
        integrator_user_template=_env("CHAT_PROMPT_INTEGRATOR_USER") or _prompts_default.integrator_user_template,
        integrator_factual_system=_env("CHAT_PROMPT_INTEGRATOR_FACTUAL_SYSTEM") or _prompts_default.integrator_factual_system,
        integrator_canonical_system=_env("CHAT_PROMPT_INTEGRATOR_CANONICAL_SYSTEM") or _prompts_default.integrator_canonical_system,
        integrator_blended_system=_env("CHAT_PROMPT_INTEGRATOR_BLENDED_SYSTEM") or _prompts_default.integrator_blended_system,
        integrator_repair_system=_env("CHAT_PROMPT_INTEGRATOR_REPAIR_SYSTEM") or _prompts_default.integrator_repair_system,
    )
    return ChatConfig(llm=llm, rag=rag, parser=parser, prompts=prompts)


def chat_config_for_api() -> dict:
    """Return chat config and prompts as a dict for GET /chat/config (frontend). Includes config_sha when using prompts_llm.yaml."""
    c = get_chat_config()
    out = {
        "config_sha": get_config_sha(),
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
            "rag_answering_user_template": c.prompts.rag_answering_user_template,
            "integrator_system": c.prompts.integrator_system,
            "integrator_user_template": c.prompts.integrator_user_template,
            "integrator_repair_system": c.prompts.integrator_repair_system,
            "consolidator_factual_max": c.prompts.consolidator_factual_max,
            "consolidator_canonical_min": c.prompts.consolidator_canonical_min,
            "integrator_factual_system": c.prompts.integrator_factual_system,
            "integrator_canonical_system": c.prompts.integrator_canonical_system,
            "integrator_blended_system": c.prompts.integrator_blended_system,
        },
    }
    return out