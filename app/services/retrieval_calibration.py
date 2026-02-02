"""Map parser intent (factual/canonical) to numeric score and retrieval blend.

- Parser emits intent_score in [0, 1] per sub-question.
- Blend: n_hierarchical (top-x hierarchical) + n_factual (top-x factual); mix or all one type.
- 0 = canonical: all hierarchical (3). 1 = factual: all factual (10). In between: e.g. 1 hierarchical + 7 factual.
"""
from typing import Any

# Intent literals from planner schema
FACTUAL = "factual"
CANONICAL = "canonical"


def intent_to_score(question_intent: str | None) -> float:
    """Convert binary question_intent to numeric score in [0, 1].

    - factual -> 1.0
    - canonical -> 0.0
    - None or unknown -> 0.5 (middle)
    """
    if question_intent is None:
        return 0.5
    intent = (question_intent or "").strip().lower()
    if intent == FACTUAL:
        return 1.0
    if intent == CANONICAL:
        return 0.0
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
