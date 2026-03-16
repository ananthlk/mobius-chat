"""Map parser intent (factual/canonical) to numeric score and retrieval blend.

- Parser emits intent_score in [0, 1] per sub-question.
- Blend: n_hierarchical (top-x hierarchical) + n_factual (top-x factual); mix or all one type.
- 0 = canonical: all hierarchical (3). 1 = factual: all factual (10). In between: e.g. 1 hierarchical + 7 factual.
"""
from typing import Any

# Intent literals from planner schema
FACTUAL = "factual"
CANONICAL = "canonical"


def intent_to_score(question_intent: str | None, question_text: str | None = None) -> float:
    """Convert question_intent to numeric score in [0, 1].

    Score drives retrieval blend:
      0.0 (canonical)  → n_hierarchical=5, n_factual=0  — full paragraphs for policy/process docs
      0.3 (procedural) → n_hierarchical=4, n_factual=3  — mostly paragraphs + some sentences
      0.5 (default)    → n_hierarchical=2, n_factual=5  — balanced
      1.0 (factual)    → n_hierarchical=0, n_factual=10 — sentences only for pure data lookups

    "factual" means a pure data lookup (NPI number, ICD-10 code, date, limit).
    "canonical"/"procedural" means a process/policy question whose answer lives in paragraphs.
    The planner's intent_score (routing confidence) must NOT be used here — it is always high
    for correctly-routed questions and has no factual/canonical meaning.

    question_text: optional original question text used as a tiebreaker when
    question_intent="factual" but the question is actually about a process/how-to.
    """
    # Signals in question_intent string (free-form planner output)
    _CANONICAL_SIGNALS = (
        "process", "how to", "how do", "steps", "procedure", "submit",
        "appeal", "enroll", "credential", "authorization", "grievance",
        "policy", "coverage", "eligibility", "benefit",
    )
    _FACTUAL_SIGNALS = (
        "npi", "icd", "code", "number", "date", "deadline", "limit",
        "rate", "fee", "amount", "cms", "lookup",
    )

    # Strong process-question patterns in the actual question text.
    # These override a "factual" intent classification.
    # Deliberately narrow: only fire on explicit how-to/submission phrasing.
    _PROCESS_TEXT_SIGNALS = (
        "how do i", "how do you", "how to", "how does",
        "steps to", "steps for", "what steps",
        "process for", "process to", "procedure for",
        "submit ", "submitting", "how can i",
    )

    if question_intent is None:
        intent = ""
    else:
        intent = (question_intent or "").strip().lower()

    # Canonical exact match — always return 0.0 immediately
    if intent == CANONICAL:
        return 0.0

    # For "factual" exact match: check question_text before accepting it.
    # The planner sometimes marks process questions as "factual" because they have
    # a definite answer; use the question text to detect explicit how-to phrasing.
    if intent == FACTUAL:
        if question_text:
            qt = question_text.strip().lower()
            for sig in _PROCESS_TEXT_SIGNALS:
                if sig in qt:
                    return 0.3  # Process question misclassified as factual
        # Pure data lookup — trust the factual classification
        return 1.0

    # Substring signals in the question_intent string (free-form planner output)
    for sig in _FACTUAL_SIGNALS:
        if sig in intent:
            return 1.0
    for sig in _CANONICAL_SIGNALS:
        if sig in intent:
            return 0.3

    # Named intent types
    if intent in ("procedural", "diagnostic"):
        return 0.3
    if intent == "creative":
        return 0.6

    return 0.5


def get_retrieval_blend(score: float) -> dict[str, Any]:
    """Map intent score in [0, 1] to (n_hierarchical, n_factual, confidence_min).

    - score 0 (canonical): (5, 0) — all hierarchical (more context for philosophy/process questions).
    - score 1 (factual): (0, 10) — all factual, high confidence_min.
    - In between: mix e.g. (4, 2) at 0.2 so canonical questions get more hierarchical chunks.
    """
    score = max(0.0, min(1.0, float(score)))
    # Canonical: up to 5 hierarchical; factual: up to 10. Blend so low score = more hierarchical.
    n_hierarchical = max(0, min(10, int(round(5 * (1 - score)))))
    n_factual = max(0, min(10, int(round(10 * score))))
    confidence_min = round(0.5 + 0.3 * score, 2)
    confidence_min = max(0.0, min(1.0, confidence_min))
    return {
        "n_hierarchical": n_hierarchical,
        "n_factual": n_factual,
        "confidence_min": confidence_min,
    }


def get_retrieval_params(score: float) -> dict[str, Any]:
    """Legacy: single-path params. Prefer get_retrieval_blend for two-path retrieval."""
    blend = get_retrieval_blend(score)
    n_h = blend["n_hierarchical"]
    n_f = blend["n_factual"]
    total = n_h + n_f
    return {
        "top_k": total if total > 0 else 5,
        "confidence_min": blend["confidence_min"],
        "n_hierarchical": n_h,
        "n_factual": n_f,
    }
