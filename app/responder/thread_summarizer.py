"""Dedicated rolling-thread-summary generator.

Why a separate LLM call instead of fields on the integrator's AnswerCard:
the production model (Gemini 2.5 Flash) reliably emits a *minimal* answer
card and drops trailing optional fields (it never emitted ``thread_state``)
while ignoring the soft "short label, no 'the user is asking'" rules.
Claude followed those; Gemini does not. A narrow ``{short, long}`` JSON task
is something Gemini does reliably.

This runs AFTER the answer has streamed to the user (off the critical
path). Its output populates:
  * chat_threads.summary_short — the sidebar label (rolling, ≤60 chars)
  * chat_threads.summary_long  — next-turn memory (≤~60 words): payer +
    jurisdiction, codes, URLs, form names, answered-vs-still-wanted

Best-effort: any failure returns (None, None) and callers fall back to the
integrator's ``thread_summary``.
"""
from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

# Leading narration the model occasionally emits despite instructions
# ("User is seeking…", "The user asked about…"). Stripped deterministically
# so the sidebar label stays a clean noun phrase even on a non-compliant turn.
_NARRATION_PREFIX = re.compile(
    r"^\s*(the\s+user|user|the\s+assistant|assistant|the\s+system|system)\s+"
    r"(is\s+|was\s+|has\s+|have\s+|are\s+)?"
    r"(asking\s+about|asking\s+for|asking|seeking|inquired\s+about|inquiring\s+about|"
    r"inquiring|wants?\s+to\s+know(\s+about)?|wants?|would\s+like|looking\s+for|"
    r"requested|requesting|asked\s+about|asked\s+for|asked|needs?|querying\s+about)\s+",
    re.IGNORECASE,
)


def _strip_narration(s: str) -> str:
    out = _NARRATION_PREFIX.sub("", (s or "").strip()).strip()
    if out and out[0].islower():
        out = out[0].upper() + out[1:]
    return out


def _is_clean_label(s: str | None) -> bool:
    """A usable sidebar label: present, short, no question mark, and not a
    narration-style sentence ('The user…', 'Assistant…')."""
    if not s or not s.strip():
        return False
    s = s.strip()
    if len(s) > 70 or "?" in s:
        return False
    return _NARRATION_PREFIX.match(s) is None


def _derive_short(short: str | None, long_: str | None) -> str | None:
    """Guarantee a clean sidebar label. Prefer the model's short; if it
    narrates or is missing, derive one from the long brief's first clause,
    stripping any narration prefix. Best-effort — returns the original short
    if nothing better can be made."""
    if _is_clean_label(short):
        return short.strip()
    # Derive from long: first sentence/clause, narration stripped.
    base = (long_ or short or "").strip()
    if not base:
        return short
    # First sentence, then prefer cutting at the em-dash topic boundary.
    first = re.split(r"(?<=[.!])\s+", base)[0]
    cand = _strip_narration(first).strip(" .—-")
    if len(cand) > 60:
        cand = cand[:60].rsplit(" ", 1)[0].strip(" .,—-")
    return cand or short

_SUM_SYS = (
    "You maintain a ROLLING summary of an ongoing healthcare support chat thread. "
    "Output ONLY a JSON object and nothing else: {\"short\": \"...\", \"long\": \"...\"}.\n"
    "CRITICAL: Do NOT narrate the conversation. NEVER begin either field with 'The user', 'User', "
    "'Assistant', 'The assistant', or 'The system'. Write about the SUBJECT MATTER, not about who "
    "said what.\n"
    "- short: <=60 characters. A sidebar topic LABEL as a NOUN PHRASE — the thread's subject as it "
    "stands NOW. No verbs like 'asking'/'seeking'/'wants', no question marks.\n"
    "    GOOD: 'Provider enrollment — Sunshine Health (FL Medicaid)'\n"
    "    BAD:  'The user is asking about provider enrollment'\n"
    "- long: <=60 words. The thread's running MEMORY for future turns, as a topic-anchored brief "
    "(not a play-by-play). If a PRIOR brief is given, REFINE it to fold in this turn — do not restart "
    "and do not drop still-relevant facts. Lead with the subject, then carry the durable facts a later "
    "turn needs: payer + jurisdiction, codes, form names, URLs, page refs, and what is resolved vs. "
    "still needed.\n"
    "    GOOD: 'Provider enrollment — Sunshine Health (FL Medicaid). Enroll via the Practitioner "
    "Enrollment Requests page at sunshinehealth.com; standardized app or CAQH accepted. Downloadable "
    "form/link still needed.'\n"
    "    BAD:  'The user asked how to enroll. The assistant could not find details.'"
)


def _build_user(
    previous_long: str | None,
    user_message: str | None,
    answer_text: str | None,
    jurisdiction_summary: str | None,
) -> str:
    parts = [
        "PRIOR brief: "
        + (previous_long.strip() if previous_long and previous_long.strip() else "(none — first turn)")
    ]
    if jurisdiction_summary and jurisdiction_summary.strip():
        parts.append("Jurisdiction (if known): " + jurisdiction_summary.strip())
    parts.append("This turn — user: " + (user_message or "").strip())
    ans = (answer_text or "").strip()
    if len(ans) > 1500:
        ans = ans[:1500] + "…"
    parts.append("This turn — assistant answer: " + ans)
    parts.append("Output the JSON now.")
    return "\n".join(parts)


def _format_quality(short: str | None, long_: str | None) -> float:
    """Bandit reward in [0,1]: did this model produce a clean, label-shaped
    short AND a non-narration long, with both fields present? Scored on the
    model's RAW output so a Flash 'The user is asking…' response scores lower
    than a clean Pro label — that gap is what teaches the bandit."""
    s = 0.0
    if short and short.strip():
        s += 0.35
        if _is_clean_label(short):
            s += 0.25
    if long_ and long_.strip():
        s += 0.20
        if _NARRATION_PREFIX.match(long_.strip()) is None:
            s += 0.20
    return round(min(1.0, s), 3)


def _record_reward(call_id, short_raw: str | None, long_: str | None) -> None:
    """Best-effort: write the format-compliance reward to the thread_summary
    stage's llm_calls row (quality_score). Safe no-op on failure. The row was
    already inserted (generate() awaits _write_async before returning), so
    there is no insert/update race."""
    if not call_id:
        return
    try:
        import asyncio

        from app.services.llm_analytics import update_quality_async

        score = _format_quality(short_raw, long_)
        coro = update_quality_async(call_id, score, "summary_format_v1")
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop and loop.is_running():
            loop.create_task(coro)
        else:
            asyncio.run(coro)
    except Exception as e:  # noqa: BLE001
        logger.debug("summary reward write skipped: %s", e)


def _parse(text: str | None) -> tuple[str | None, str | None]:
    import json

    raw = (text or "").strip()
    if not raw:
        return None, None
    if "```" in raw:
        # take the fenced block body if present
        segs = raw.split("```")
        if len(segs) >= 2:
            raw = segs[1]
        if raw.lower().startswith("json"):
            raw = raw[4:]
    s, e = raw.find("{"), raw.rfind("}")
    if s < 0 or e < 0 or e <= s:
        return None, None
    blob = raw[s : e + 1]
    obj = None
    try:
        obj = json.loads(blob)
    except Exception:
        try:
            from json_repair import repair_json

            obj = json.loads(repair_json(blob))
        except Exception:
            return None, None
    if not isinstance(obj, dict):
        return None, None
    short = (str(obj.get("short") or "")).strip() or None
    long_ = (str(obj.get("long") or "")).strip() or None
    if short:
        short = short[:120]
    if long_:
        long_ = long_[:600]
    return short, long_


def summarize_thread(
    *,
    previous_long: str | None,
    user_message: str | None,
    answer_text: str | None,
    jurisdiction_summary: str | None = None,
    correlation_id: str | None = None,
    thread_id: str | None = None,
    config_sha: str | None = None,
    mode: str | None = None,
) -> tuple[str | None, str | None]:
    """Generate the rolling (short, long) thread summary via one focused
    LLM call. Returns (None, None) on any failure — callers must fall back
    to the integrator's thread_summary."""
    try:
        from app.services.llm_manager import generate_sync

        prompt = _SUM_SYS + "\n\n" + _build_user(
            previous_long, user_message, answer_text, jurisdiction_summary
        )
        text, _usage = generate_sync(
            prompt,
            stage="thread_summary",
            max_tokens=400,
            config_sha=config_sha,
            correlation_id=correlation_id,
            thread_id=thread_id,
            mode=mode,
        )
        short, long_ = _parse(text)
        # Reward the bandit on the MODEL's RAW format compliance (before our
        # deterministic cleanup) so it learns which model produces clean
        # labels for this stage.
        _record_reward((_usage or {}).get("llm_call_id"), short, long_)
        # Deterministic guarantee: clean sidebar label even if the model
        # narrated or dropped the short field this turn.
        short = _derive_short(short, long_)
        return short, long_
    except Exception as e:  # noqa: BLE001 — best-effort, never break a turn
        logger.warning("thread summarizer failed (non-fatal): %s", e)
        return None, None
