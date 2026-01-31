"""Planner: decompose user message into subquestions and classify patient vs non-patient.
Emits 'thinking' chunks via callback for streaming display.
"""
import logging
import re
from collections.abc import Callable

from app.planner.schemas import Plan, SubQuestion

logger = logging.getLogger(__name__)


def _emit(thinking_emitter: Callable[[str], None] | None, chunk: str) -> None:
    if thinking_emitter and chunk.strip():
        thinking_emitter(chunk.strip())


def parse(
    message: str,
    *,
    thinking_emitter: Callable[[str], None] | None = None,
) -> Plan:
    """Parse user message into a plan (subquestions + patient/non_patient).
    Calls thinking_emitter(chunk) for each 'thinking' fragment (e.g. for UI).
    """
    thinking_log: list[str] = []

    def emit(chunk: str) -> None:
        thinking_log.append(chunk)
        if thinking_emitter:
            thinking_emitter(chunk)

    emitter = emit

    text = (message or "").strip()
    _emit(emitter, "Reading your question...")
    if not text:
        _emit(emitter, "Empty message; no subquestions.")
        return Plan(subquestions=[], thinking_log=thinking_log)

    from app.chat_config import get_chat_config
    parser_cfg = get_chat_config().parser
    separators = parser_cfg.decomposition_separators or [" and ", " also ", " then "]
    pattern = "|".join(re.escape(s) for s in separators)
    parts = re.split(pattern, text, flags=re.IGNORECASE)
    parts = [p.strip() for p in parts if p.strip()]
    if not parts:
        parts = [text]

    keywords = parser_cfg.patient_keywords or [
        "my doctor", "my medication", "my visit", "my record", "my care", "what did my doctor"
    ]
    patient_pattern = r"\b(" + "|".join(re.escape(k) for k in keywords) + r")\b"
    patient_keywords = re.compile(patient_pattern.replace(" ", r"\s+"), re.IGNORECASE)
    subquestions: list[SubQuestion] = []
    for i, p in enumerate(parts):
        sq_id = f"sq{i+1}"
        kind = "patient" if patient_keywords.search(p) else "non_patient"
        subquestions.append(SubQuestion(id=sq_id, text=p, kind=kind))
        _emit(emitter, f"Subquestion {sq_id}: {p[:50]}... → {kind}")

    _emit(emitter, f"Plan: {len(subquestions)} subquestion(s).")
    return Plan(subquestions=subquestions, thinking_log=thinking_log)
