"""Conversational continuity and active skill context resolution.

Two problems solved together:

Problem 1 — Pronoun resolution:
  "Can you search the web for it?" after a failed query
  → resolve "it" from prior turn before planning

Problem 2 — Skill output awareness:
  "How many NPIs have issues with PML?" after a credentialing report
  → answer from report data in context, not from RAG or web
"""
from __future__ import annotations

import re
from typing import Any

# ---------------------------------------------------------------------------
# SECTION 1 — Pronoun / reference resolution
# ---------------------------------------------------------------------------

REFERENCE_SIGNALS = re.compile(
    r"\b(it|that|this one|those|the same|the previous one|"
    r"try again|try that again|search for it|"
    r"search the web for it|google it|look it up|find it|"
    r"can you find that|what about that|"
    r"try a different approach|use a different method|"
    r"search for that|can you search for it|"
    r"look that up)\b",
    re.I,
)

QUESTION_SCAFFOLDING = re.compile(
    r"^(what is|what are|what was|what were|"
    r"how do i|how does|how can i|how should i|"
    r"can you|could you|please|tell me|"
    r"find|look up|search for|get me|"
    r"do you know|is there|are there)\s+",
    re.I,
)


def _extract_core_topic(question: str) -> str:
    """
    Strip question scaffolding to get the searchable topic.
    "What is Sunshine Health's PA requirement for H0036?"
    → "Sunshine Health's PA requirement for H0036"
    """
    topic = (question or "").strip().rstrip("?").strip()
    topic = QUESTION_SCAFFOLDING.sub("", topic).strip()
    if topic and topic[0].islower():
        topic = topic[0].upper() + topic[1:]
    return topic or (question or "").strip()


def _get_prior_question(
    current_message: str,
    last_turns: list[dict[str, Any]],
    lookback: int = 3,
) -> str | None:
    """Most recent user question different from current message."""
    current = (current_message or "").strip().lower()
    for turn in (last_turns or [])[:lookback]:
        candidate = (
            turn.get("user_content")
            or turn.get("message")
            or turn.get("user_message")
            or ""
        ).strip()
        if candidate and candidate.lower() != current:
            return candidate
    return None


def _get_prior_failed_query(
    last_turns: list[dict[str, Any]],
    prior_failed_from_state: str | None = None,
) -> str | None:
    """Most recent turn that ended in an honest miss."""
    if prior_failed_from_state and (prior_failed_from_state or "").strip():
        return prior_failed_from_state.strip()

    for turn in (last_turns or [])[:3]:
        retrieval = (turn.get("retrieval_signal") or "").strip()
        layer_used = turn.get("layer_used")
        answer = (turn.get("assistant_content") or "").lower()
        is_miss = (
            "no_sources" in retrieval
            or layer_used == 5
            or "not available" in answer
            or "couldn't find" in answer
            or "cannot determine" in answer
            or "i don't have" in answer
            or "missing information" in answer
        )
        if is_miss:
            user_q = (
                turn.get("user_content")
                or turn.get("message")
                or turn.get("user_message")
                or ""
            ).strip()
            if user_q:
                return user_q
    return None


def resolve_pronouns(
    current_message: str,
    last_turns: list[dict[str, Any]],
    prior_failed_question: str | None = None,
) -> tuple[str, bool]:
    """
    Resolve pronoun/implicit references against conversation history.
    Returns (resolved_message, was_enriched).

    prior_failed_question: from ctx.merged_state.last_failed_query.question
      when turns don't yet have retrieval_signal/layer_used persisted.
    """
    text = (current_message or "").strip()

    if not last_turns and not prior_failed_question:
        return current_message, False

    if not REFERENCE_SIGNALS.search(text):
        return current_message, False

    prior_q = _get_prior_question(current_message, last_turns)
    prior_failed = _get_prior_failed_query(last_turns, prior_failed_from_state=prior_failed_question)

    if not prior_q and not prior_failed:
        return current_message, False

    reference_q = prior_failed or prior_q
    core_topic = _extract_core_topic(reference_q)
    text_lower = text.lower()

    # "search the web for it" → explicit web search for prior topic
    web_patterns = [
        r"search the web for it",
        r"search for it",
        r"google it",
        r"look it up on the web",
        r"find it online",
        r"can you search (the web )?for it",
    ]
    for pat in web_patterns:
        if re.search(pat, text_lower):
            return f"Search the web for {core_topic}", True

    # "try a different approach" → web search for prior failed topic
    if re.search(
        r"try a different (approach|method|way)|"
        r"use a different (approach|method)|"
        r"different approach",
        text_lower,
    ):
        if prior_failed:
            return f"Search the web for {core_topic}", True

    # "try again" → exact retry of prior failed query
    if re.search(r"\btry (that |it )?again\b", text_lower):
        if prior_failed:
            return prior_failed, True

    # "look it up" / "find it" / "can you find that"
    if re.search(
        r"\b(look it up|find it|can you find that|look that up|find that)\b",
        text_lower,
    ):
        resolved = re.sub(r"\b(it|that)\b", core_topic, text, flags=re.I, count=1)
        if resolved != text:
            return resolved, True

    # Generic "it" / "that" substitution
    if re.search(r"\bit\b|\bthat\b", text_lower):
        resolved = re.sub(r"\b(it|that)\b", core_topic, text, flags=re.I, count=1)
        if resolved != text:
            return resolved, True

    return current_message, False


# Backward compatibility: same behavior as resolve_pronouns with prior_failed_question
def resolve_message_references(
    current_message: str,
    last_turns: list[dict[str, Any]],
    prior_failed_question: str | None = None,
) -> tuple[str, bool]:
    """Alias for resolve_pronouns with prior_failed_question (state-based failed query)."""
    return resolve_pronouns(current_message, last_turns, prior_failed_question=prior_failed_question)


# ---------------------------------------------------------------------------
# SECTION 2 — Active skill context detection
# ---------------------------------------------------------------------------

SKILL_REFERENCE_SIGNALS = re.compile(
    r"\b(how many|which ones|which providers|list the|"
    r"show me|from the report|in the report|"
    r"from that|in that|in the results|from the results|"
    r"what about the|tell me more about the|"
    r"what does (section|part) [a-e] (say|show|mean)|"
    r"explain (section|part) [a-e]|"
    r"break down|summarize|what is the total|"
    r"what are the issues|what needs to be fixed|"
    r"what is the readiness|what is the score|"
    r"how much (is|are|could|would)|"
    r"what is the opportunity)\b",
    re.I,
)

# Matches follow-ups after org NPI lookup (ReAct stores active_context["tool"] as "lookup_npi" — same pattern).
_NPI_LOOKUP_FOLLOWUP = re.compile(
    r"\b(npi|which one|the first one|the second one|"
    r"the [a-z]+ location|that npi|those npis|these npis|"
    r"practice location|practice locations|"
    r"find locations?|list locations?|sites? for|addresses? for|"
    r"tied to these|tied to those|tied to the)\b",
    re.I,
)

SKILL_TERMS = {
    "roster_report": re.compile(
        r"\b(pml|provider master list|enrollment gap|"
        r"at.risk|taxonomy|section [a-e]|waterfall|"
        r"readiness score|run rate|npi|enrolled|"
        r"missing enrollment|address gap|ghost billing|"
        r"revenue opportunity|section a|section b|"
        r"section c|section d|section e)\b",
        re.I,
    ),
    "npi_lookup": _NPI_LOOKUP_FOLLOWUP,
    "lookup_npi": _NPI_LOOKUP_FOLLOWUP,
}


_WORKFLOW_BILLING_NPI_LINE = re.compile(r"use\s+billing\s+npi\s+\d{10}", re.I)
_LOCATION_FOLLOWUP_WITH_NPIS = re.compile(
    r"\b(location|locations|locate|practice site|sites?|addresses?|find the)\b",
    re.I,
)


def detect_skill_reference(
    message: str,
    active_skill: dict | None,
) -> tuple[bool, str | None]:
    """
    Detect whether the current message refers to the output of the most recently used skill.

    Returns (is_skill_reference, skill_name).
    is_skill_reference=True → route to reasoning against skill output in context; do NOT RAG/web.
    """
    if not active_skill:
        return False, None

    skill_name = active_skill.get("skill")
    if not skill_name:
        return False, None

    text = (message or "").strip()
    npi_in_msg = bool(re.search(r"\b\d{10}\b", text))
    user_sent_chip_payload = bool(_WORKFLOW_BILLING_NPI_LINE.search(text))
    if skill_name in ("lookup_npi", "npi_lookup"):
        if user_sent_chip_payload or (npi_in_msg and _LOCATION_FOLLOWUP_WITH_NPIS.search(text)):
            return False, None

    has_reference_signal = bool(SKILL_REFERENCE_SIGNALS.search(text))
    skill_pattern = SKILL_TERMS.get(skill_name)
    has_skill_term = bool(skill_pattern and skill_pattern.search(text))

    if has_reference_signal or has_skill_term:
        return True, skill_name

    return False, None


def build_skill_context_summary(active_skill: dict) -> str:
    """
    Build a plain-language summary of active skill output to inject into the planner context pack.
    The planner and integrator use this to answer follow-up questions without re-running the skill.
    """
    skill = active_skill.get("skill")
    data = active_skill.get("data") or {}
    org = active_skill.get("org") or "the organization"

    if skill == "roster_report":
        lines = [
            f"ACTIVE SKILL OUTPUT: Credentialing report for {org}",
            "Generated this turn — answer follow-up questions from this data.",
            "",
        ]
        if data.get("readiness_score") is not None:
            lines.append(f"Readiness score: {data['readiness_score']}%")
        if data.get("section_a_count") is not None:
            lines.append(
                f"Section A — Enrolled providers (current run rate): {data['section_a_count']} providers"
            )
        if data.get("section_b_count") is not None:
            lines.append(
                f"Section B — At-risk (address/enrollment gaps, fix now): {data['section_b_count']} providers"
            )
        if data.get("section_c_count") is not None:
            lines.append(
                f"Section C — Missing PML enrollment (enroll now): {data['section_c_count']} providers"
            )
        if data.get("section_d_count") is not None:
            lines.append(
                f"Section D — Taxonomy optimization (verify): {data['section_d_count']} providers"
            )

        b = data.get("section_b_count", 0) or 0
        c = data.get("section_c_count", 0) or 0
        if b or c:
            lines.append(
                f"Total NPIs with PML issues: {b + c} "
                f"({b} at-risk in Section B + {c} missing enrollment in Section C)"
            )

        if data.get("total_opportunity") is not None:
            lines.append(f"Total revenue opportunity (B+C+D): ${float(data['total_opportunity']):,.2f}")
        if data.get("section_b_revenue") is not None:
            lines.append(f"Section B revenue at risk: ${float(data['section_b_revenue']):,.2f}")
        if data.get("section_c_revenue") is not None:
            lines.append(f"Section C enrollment gap revenue: ${float(data['section_c_revenue']):,.2f}")
        if data.get("section_d_revenue") is not None:
            lines.append(f"Section D taxonomy uplift: ${float(data['section_d_revenue']):,.2f}")
        if data.get("locations") is not None:
            lines.append(f"Locations: {data['locations']} active locations")

        return "\n".join(lines)

    if skill == "npi_lookup":
        results = data.get("results") or []
        lines = [
            f"ACTIVE SKILL OUTPUT: NPI lookup for {org}",
            f"Found {len(results)} NPIs.",
            "Follow-up questions about these NPIs answer from this list.",
        ]
        for r in results[:10]:
            lines.append(
                f"  {r.get('name', '')} — NPI {r.get('npi', '')} ({r.get('match_type', 'match')})"
            )
        return "\n".join(lines)

    return f"ACTIVE SKILL OUTPUT: {skill} for {org}"


# ---------------------------------------------------------------------------
# SECTION 3 — Active skill data extraction (after skill completes)
# ---------------------------------------------------------------------------


def extract_roster_skill_data(ctx: Any) -> dict:
    """
    Extract structured summary data from a completed credentialing report for use in follow-up questions.
    Call after run_resolve() when tool_hint=roster_report.
    """
    data: dict[str, Any] = {}

    step_outputs = getattr(ctx, "roster_step_outputs", None) or []
    md = getattr(ctx, "roster_report_final_md", "") or ""
    answer_set = getattr(ctx, "answer_set", {}) or {}

    for _sq_id, entry in answer_set.items():
        answer_text = (entry.get("answer") or "").lower()

        b_match = re.search(r"section b[^:]*:\s*(\d+)\s*provider", answer_text)
        if b_match:
            data["section_b_count"] = int(b_match.group(1))

        c_match = re.search(r"section c[^:]*:\s*(\d+)\s*provider", answer_text)
        if c_match:
            data["section_c_count"] = int(c_match.group(1))

        a_match = re.search(r"section a[^:]*:\s*(\d+)\s*provider", answer_text)
        if a_match:
            data["section_a_count"] = int(a_match.group(1))

        d_match = re.search(r"section d[^:]*:\s*(\d+)\s*provider", answer_text)
        if d_match:
            data["section_d_count"] = int(d_match.group(1))

        rs_match = re.search(r"readiness score[:\s]+(\d+\.?\d*)%?", answer_text)
        if rs_match:
            data["readiness_score"] = float(rs_match.group(1))

    if md:
        opp_match = re.search(
            r"(?:total|operational) opportunity[:\s]+\$?([\d,]+\.?\d*)",
            md,
            re.I,
        )
        if opp_match:
            data["total_opportunity"] = float(opp_match.group(1).replace(",", ""))

        b_rev = re.search(
            r"(?:at.risk|section b)[^\$]*\$([\d,]+\.?\d*)",
            md,
            re.I,
        )
        if b_rev:
            data["section_b_revenue"] = float(b_rev.group(1).replace(",", ""))

        c_rev = re.search(
            r"(?:enrollment gap|section c)[^\$]*\$([\d,]+\.?\d*)",
            md,
            re.I,
        )
        if c_rev:
            data["section_c_revenue"] = float(c_rev.group(1).replace(",", ""))

        d_rev = re.search(
            r"(?:taxonomy|section d)[^\$]*\$([\d,]+\.?\d*)",
            md,
            re.I,
        )
        if d_rev:
            data["section_d_revenue"] = float(d_rev.group(1).replace(",", ""))

    return data
