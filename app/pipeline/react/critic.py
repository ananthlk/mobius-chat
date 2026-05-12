"""ReAct critic — audits a draft answer against retrieved sources.

**The problem this module solves.** When the planner says
``is_complete=true`` and produces a draft answer, nothing in the
pipeline checks whether the draft's factual claims are grounded in
the retrieved sources. Shape validators (sections / direct_answer /
mode) care about structure. The post-run adjudicator gives a quality
score but runs too late — after delivery. So hallucinated phone
numbers, fabricated rule citations, and unsubstantiated "X is
required" assertions sail through to the user with citation markers
that look rigorous but don't actually point at supporting spans.

**How the critic works.** When the planner emits a completion, the
critic runs before ``_finalize_response`` is called. Its job is
narrow:

  Given the draft answer and all retrieved sources, return a
  structured list of claims that are NOT supported by at least one
  source, tagged with severity.

If any high-severity issues come back, ``run_react`` injects the
critique as a synthetic observation and the loop continues for
another round. The planner now has explicit, specific feedback and
can either find supporting evidence, revise the claim, or drop it.

If rounds are exhausted, the caller falls through to finalize with a
groundedness warning appended — never ship silently with known
high-severity fabrications.

**Why an LLM critic, not just regex.** Regex catches verbatim
hallucinations (wrong phone number, wrong HCPCS code). Those are a
real harm class and a future commit will add them as a
belt-and-suspenders deterministic check. But the critic also needs
to catch the modal-claim class — "prior authorization is required"
stated as fact when no source establishes it. That requires semantic
comparison of the draft against source spans, which is LLM territory.

**Model selection.** Uses a dedicated ``react_critic`` stage so the
model registry can route it to a fast, cheap model (Haiku-class).
The critic's task is narrow: extract claims, match to sources,
classify. It doesn't need Sonnet-class reasoning. Running it cheap
keeps per-turn latency + cost acceptable since the critic fires on
every completion round.

**Design principles.**

1. **Conservative.** False positives cost a ReAct round. False
   negatives cost user trust. Both are expensive, but the prompt
   deliberately errs toward "grounded" when the critic isn't sure,
   because a FP loop can cascade.

2. **Honest hedges are fine.** A draft that says "I couldn't find
   specific information about X" is CORRECT behavior, not a
   groundedness issue. The prompt is explicit about this.

3. **Source-only evidence.** The critic is told to judge claims
   against retrieved sources, NOT against its own training-data
   knowledge. "I know Sunshine Health's number is different" is
   NOT a valid reason to flag a phone number; only "no source
   contains this phone number" counts.

4. **Parseable output.** Returns JSON with a known shape so
   ``run_react`` can branch deterministically. Parsing failures
   fall closed (treat as approved) because a critic bug shouldn't
   break user delivery.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


# ── Feature flag ─────────────────────────────────────────────────────
#
# MOBIUS_REACT_CRITIC controls whether the critic runs at all.
# Default OFF for commit 1 so live rollout is gated — an operator
# flips it ON per environment after validation. Commit 2+ will
# default ON once the prompt is tuned.


def critic_enabled() -> bool:
    return (os.environ.get("MOBIUS_REACT_CRITIC") or "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


# ── Deterministic invocation gate ────────────────────────────────────
#
# The critic adds an LLM round-trip (~2–5s) on every completion. Most
# answers are canonical policy/process text where hallucination risk is
# low and there are no specific verifiable facts for the critic to catch.
# This gate makes the decision deterministic so the critic only fires
# when it actually has something useful to do.
#
# Run critic when ANY of:
#   • Answer contains specific numeric/code claims (dollar amounts, day
#     counts, HCPCS/CPT/ICD codes, percentages) — prime hallucination
#     surface; the critic can catch quote-level mismatches.
#   • Final signal is web/Google (less reliable than corpus sources).
#   • No sources at all but answer is long (LLM reasoning, no grounding).
#
# Skip critic when ALL of:
#   • No numeric/code patterns in the answer text.
#   • Retrieval was corpus-backed (not web-only).
#   • Answer is not suspiciously long for a "no sources" turn.
#
# This is intentionally conservative — false negatives (skipping when
# we should have run) are recoverable via the adjudicator; false
# positives (running when not needed) are just latency waste.

_FACTUAL_CLAIM_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r'\$[\d,]+(?:\.\d+)?'),           # dollar amounts: $45.00, $1,200
    # Time windows — "14 days", "72 hours", "14 calendar days", "3 business days"
    re.compile(
        r'\b\d+\s*(?:calendar\s+|business\s+|working\s+|clock\s+)?'
        r'(?:days?|hours?|months?|years?)\b',
        re.IGNORECASE,
    ),
    re.compile(r'\b\d+\s*%'),                    # percentages
    re.compile(r'\bH\d{4}\b'),                   # HCPCS codes: H0036, T1019
    re.compile(r'\bT\d{4}\b'),
    re.compile(r'\b\d{5}(?:-\d{2})?\b'),         # CPT codes (5-digit, optional modifier)
    re.compile(r'\b[A-Z]\d{2,3}(?:\.\d+)?\b'),  # ICD-10: F32.1, Z23
    re.compile(r'\b\d{3}[-.\s]\d{3,4}\b'),      # phone fragments
    re.compile(r'\bNPI\s*[#:]?\s*\d{10}\b', re.IGNORECASE),  # NPI numbers
    re.compile(r'\brate\s+of\s+\$', re.IGNORECASE),           # rate of $X
    re.compile(r'\bwithin\s+\d+\b', re.IGNORECASE),           # within N days/hours
    re.compile(r'\bdeadline\b', re.IGNORECASE),               # any deadline mention
]

_GOOGLE_SIGNAL = "google_only"  # matches RETRIEVAL_SIGNAL_GOOGLE_ONLY value


def should_run_critic(
    answer: str,
    all_sources: list[dict],
    final_signal: str,
    user_message: str = "",
) -> tuple[bool, str]:
    """Deterministic gate: returns (should_run, reason_string).

    Called before every critic invocation. When this returns False the
    critic LLM call is skipped entirely and the answer goes straight to
    finalize. The reason string is emitted to the thinking log so the
    skip is visible in analytics.
    """
    answer_text = (answer or "").strip()

    # Web-sourced answers are less reliably quoted → always audit.
    if _GOOGLE_SIGNAL in (final_signal or "").lower():
        return True, "run:web_sourced_answer"

    # Check for specific factual claim patterns BEFORE the no-sources
    # guard: a phone number / code asserted with zero retrieved sources is
    # MORE suspicious than one that at least has some corpus backing.
    # The critic will flag every claim as unverifiable (nothing to compare
    # against), which is the right outcome — honest "no evidence" signal.
    for pattern in _FACTUAL_CLAIM_PATTERNS:
        if pattern.search(answer_text):
            return True, f"run:factual_claim_detected({pattern.pattern[:30]})"

    # No numeric/code specifics found AND no web sources → critic has
    # nothing useful to compare against, and the answer is likely
    # policy/process prose. Low hallucination risk; skip.
    if not all_sources:
        return False, "skip:no_corpus_sources"

    # No numeric/code specifics found — answer is likely policy/process
    # prose. Low hallucination risk; skip the critic.
    return False, "skip:no_verifiable_claims_in_answer"


# ── Result shape ─────────────────────────────────────────────────────


@dataclass
class CritiqueIssue:
    """One flagged claim. Copy of the critic's JSON shape, typed."""

    claim: str
    severity: str  # "high" | "medium" | "low"
    reason: str

    @property
    def is_high(self) -> bool:
        return self.severity == "high"


@dataclass
class CritiqueResult:
    """Parsed critic response. ``grounded`` is the dispatcher's
    decision signal: if False AND rounds remain, run_react continues.
    """

    grounded: bool = True
    issues: list[CritiqueIssue] = field(default_factory=list)
    raw: str = ""  # for telemetry / debugging

    @property
    def high_severity_issues(self) -> list[CritiqueIssue]:
        return [i for i in self.issues if i.is_high]

    @property
    def has_blocking_issues(self) -> bool:
        """True iff at least one high-severity issue exists. Medium /
        low issues do NOT block delivery — they're informational."""
        return any(i.is_high for i in self.issues)


# ── Prompts ──────────────────────────────────────────────────────────


CRITIC_SYSTEM_PROMPT = """\
You are auditing a DRAFT ANSWER against RETRIEVED SOURCES.

Your ONLY job: for each factual claim in the draft, decide whether at
least one retrieved source supports it.

Rules:
  1. Use the sources as your evidence. Do NOT consult your own
     knowledge of the topic. "I know the correct phone number is X"
     is NOT a valid reason — only "no source contains this phone
     number" counts.
  2. Honest hedges are CORRECT. If the draft says "I couldn't find
     specific X" or "the full policy was not available", that's the
     desired behavior, not an issue.
  3. Be conservative. If you aren't sure a claim is ungrounded, don't
     flag it. False positives are expensive — they force another loop
     round. Only flag claims you're confident no source supports.
  4. Focus on claims that could cause user harm if wrong:
       - Phone numbers / contact information
       - HCPCS / CPT / ICD-10 codes and their meanings
       - Rule citations (e.g. "Rule 59G-1.010")
       - URLs
       - Dollar amounts / rates
       - Definitive modal claims ("X is required", "must", "prohibited")
       - Dates / effective periods
  5. Don't flag generic summary language, paraphrasing that preserves
     meaning, or uncontroversial background facts.

Return JSON with this exact shape:

  {
    "grounded": true | false,
    "issues": [
      {
        "claim": "<the specific sentence or fragment from the draft>",
        "severity": "high" | "medium" | "low",
        "reason": "<why this isn't grounded, referencing sources>"
      }
    ]
  }

Severity:
  - high:   Fabricated specifics (wrong phone, invented rule,
            unsupported "is required"). Blocks delivery.
  - medium: Overstatement — sources say X broadly, draft asserts Y
            specifically. Informational.
  - low:    Minor drift. Informational.

Set grounded=false iff any high-severity issue exists. Medium alone
does NOT fail the check.

If every claim is supported, return {"grounded": true, "issues": []}.
"""


def build_critic_user_message(
    *,
    question: str,
    draft_answer: str,
    sources: list[dict[str, Any]],
    tool_results: list[dict[str, Any]] | None = None,
) -> str:
    """Format the audit request. The critic sees:

      - Original question (so it can judge claim-relevance)
      - Draft answer (the thing being audited)
      - Numbered sources with document_name, page, and full text
      - Recent tool output text (web scrapes, healthcare queries, etc.)
        — these are ALSO sources, even though they aren't in the
        ``sources`` list passed to the integrator

    Source text is **not truncated** here — the critic needs the full
    context to judge whether a claim is supported. Callers upstream
    should already have chunk-size caps applied.
    """
    lines: list[str] = []
    lines.append("## Question")
    lines.append(question.strip() or "(empty)")
    lines.append("")
    lines.append("## Draft answer (audit this)")
    lines.append(draft_answer.strip() or "(empty)")
    lines.append("")

    lines.append("## Retrieved sources")
    if not sources:
        lines.append("(no sources retrieved — any specific factual claim should be flagged)")
    else:
        for i, src in enumerate(sources, 1):
            name = (src.get("document_name") or "unknown").strip()
            page = src.get("page")
            header = f"[{i}] {name}"
            if page:
                header += f" (page {page})"
            lines.append(header)
            text = (src.get("text") or src.get("content") or "").strip()
            if text:
                lines.append(text)
            else:
                lines.append("(source had no text extractable)")
            lines.append("")

    if tool_results:
        # Tool outputs (web scrapes, healthcare_query results, etc.)
        # also count as sources for grounding. Include their text so
        # the critic can verify claims like "the website says …"
        interesting = [tr for tr in tool_results if tr.get("success") and tr.get("result")]
        if interesting:
            lines.append("## Tool outputs (also count as sources)")
            for tr in interesting:
                tool = tr.get("tool", "?")
                result = str(tr.get("result") or "").strip()
                lines.append(f"### {tool}")
                lines.append(result)
                lines.append("")

    lines.append("## Your task")
    lines.append(
        "Return JSON per the system instructions. Focus on high-severity "
        "issues: wrong phone numbers, fabricated rule citations, unsupported "
        "'is required' assertions, invented URLs, etc."
    )
    return "\n".join(lines)


# ── Response parser ──────────────────────────────────────────────────


def parse_critic_response(raw: str) -> CritiqueResult:
    """Parse the critic's JSON output into a ``CritiqueResult``.

    Falls back to grounded=True on parse failures. Rationale: a broken
    critic output shouldn't block user delivery. The parsing failure
    is logged at WARNING so operators see it and can tune the prompt,
    but the user still gets their answer.
    """
    raw_text = (raw or "").strip()
    result = CritiqueResult(grounded=True, raw=raw_text)

    if not raw_text:
        logger.warning("critic returned empty response; treating as grounded")
        return result

    # Strip markdown fencing the model sometimes wraps JSON in.
    body = raw_text
    if body.startswith("```"):
        lines = body.split("\n")
        # Remove first fence line
        lines = lines[1:]
        # Remove trailing fence if present
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        body = "\n".join(lines)

    # Try balanced-object extraction when model emits prose + JSON
    body = body.strip()
    if not body.startswith("{"):
        start = body.find("{")
        end = body.rfind("}")
        if start != -1 and end > start:
            body = body[start : end + 1]

    try:
        parsed = json.loads(body)
    except json.JSONDecodeError as e:
        # json_repair would help but adds a dep; keep this module's
        # footprint minimal. Parsing failures fall closed (grounded).
        logger.warning(
            "critic JSON parse failed: %s; first 200 chars: %r; treating as grounded",
            e,
            raw_text[:200],
        )
        return result

    if not isinstance(parsed, dict):
        logger.warning("critic returned non-dict JSON; treating as grounded")
        return result

    grounded = parsed.get("grounded")
    if isinstance(grounded, bool):
        result.grounded = grounded

    raw_issues = parsed.get("issues")
    if isinstance(raw_issues, list):
        for item in raw_issues:
            if not isinstance(item, dict):
                continue
            claim = str(item.get("claim") or "").strip()
            severity = str(item.get("severity") or "low").strip().lower()
            reason = str(item.get("reason") or "").strip()
            if severity not in ("high", "medium", "low"):
                severity = "low"
            if claim:
                result.issues.append(
                    CritiqueIssue(claim=claim, severity=severity, reason=reason)
                )

    # Defensive: if the model reported issues but forgot to set
    # grounded=false for a high-severity issue, flip it. (Rare, but
    # seen in practice with small models under-following format.)
    if result.grounded and result.has_blocking_issues:
        logger.debug(
            "critic reported high-severity issues but grounded=true; "
            "flipping to false. issues=%s",
            [i.claim[:60] for i in result.high_severity_issues],
        )
        result.grounded = False

    return result


# ── Feedback formatter for the ReAct loop ────────────────────────────


def format_critique_as_observation(issues: list[CritiqueIssue]) -> str:
    """Render the critic's high-severity issues as a synthetic tool
    observation the planner sees on the next round.

    The planner already knows how to read tool results (`{"tool": ...,
    "result": ..., "success": ...}`). The critique is injected in
    that shape so the existing reasoning-context builder handles it
    without a new code path.
    """
    if not issues:
        return "The critic approved the draft — proceed."

    lines = [
        "Your draft has claims that are NOT supported by the retrieved sources. "
        "Either find supporting evidence (use a tool), revise the claim to match "
        "the sources, or drop the claim entirely. Do NOT restate the same claim "
        "on the next round without new evidence.",
        "",
        "Flagged claims:",
    ]
    for i, issue in enumerate(issues, 1):
        claim_preview = issue.claim
        if len(claim_preview) > 200:
            claim_preview = claim_preview[:200].rstrip() + "…"
        lines.append(f"  {i}. [{issue.severity}] {claim_preview}")
        if issue.reason:
            lines.append(f"     Why: {issue.reason}")
    lines.append("")
    lines.append(
        "Next step: either search for specific evidence that supports each "
        "flagged claim, or revise/remove the claims and complete with a more "
        "conservative answer."
    )
    return "\n".join(lines)
