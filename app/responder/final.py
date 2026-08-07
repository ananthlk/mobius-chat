"""Final responder: turn plan + answers into one chat-friendly message via LLM (or fallback). Can stream the draft via message_chunk_callback."""

import json
import logging
from collections.abc import Callable
from typing import Any

from app.communication.json_display_sanitize import (
    DEFAULT_BLEED_FALLBACK,
    build_minimal_answer_card_preserving_metadata,
    display_text_for_parsed_answer_card,
    extract_user_visible_text_from_integrator_raw,
)
from app.planner.schemas import Plan
from app.services.usage import LLMUsageDict
from app.trace_log import trace_entered

logger = logging.getLogger(__name__)

ConsolidatorType = str  # "answer" | "canonical" -- FACTUAL/BLENDED collapsed into one
# unified "answer" path (2026-08-07 architecture directive); CANONICAL stays distinct.


def _emit(emitter: Callable[[str], None] | None, msg: str) -> None:
    if emitter and msg.strip():
        emitter(msg.strip())


def blended_canonical_score(plan: Plan) -> float:
    """Average of (1 - intent_score) over sub-questions where intent_score is not None. Fallback 0.5."""
    scores: list[float] = []
    subquestions = getattr(plan, "subquestions", None) or []
    for sq in subquestions:
        s = getattr(sq, "intent_score", None)
        if s is not None:
            try:
                x = float(s)
                if 0 <= x <= 1:
                    scores.append(1.0 - x)
            except (TypeError, ValueError):
                pass
    if not scores:
        return 0.5
    return sum(scores) / len(scores)


def choose_consolidator_type(
    canonical_score: float,
    factual_max: float,
    canonical_min: float,
) -> ConsolidatorType:
    """Map blended canonical score to answer | canonical. factual_max is accepted for
    call-site compatibility but no longer used -- FACTUAL/BLENDED collapsed into one
    unified 'answer' path (2026-08-07 architecture directive); CANONICAL alone stays
    distinct on a real corpus-confidence threshold."""
    if canonical_score > canonical_min:
        return "canonical"
    return "answer"


def _appeals_rules_table_data(hint_data: dict) -> dict | None:
    """Converts an appeals_rules section_hint's nested rule structure
    (matches[].rules[] from appeals_find_carc, or flat rules[] from
    appeals_lookup_rules) into a clean table {headers, rows} -- one row per
    rule, stripping the deeply-nested sub_rules/authority_tags/facts noise
    that isn't useful for display (2026-08-07, Task #60). Was previously
    passed through as format='appeals_rules' + raw nested data, relying on
    the enricher LLM to preserve that non-standard format string verbatim
    (a schema-instruction carve-out for this exact case) -- confirmed via a
    live card (a796845a) that instruction doesn't reliably hold: the model
    still wrote format='bullets' with the data blob orphaned underneath,
    rendering nothing. format='table' is a real schema-enum value, so there
    is no format-fidelity risk left to rely on the model for."""
    rule_groups = hint_data.get("matches") or [hint_data]
    rows: list[list[str]] = []
    for group in rule_groups:
        if not isinstance(group, dict):
            continue
        for rule in (group.get("rules") or []):
            if not isinstance(rule, dict):
                continue
            authority = rule.get("authority") or {}
            authority_bits = [v for v in (authority.get("state"), authority.get("federal"), authority.get("clinical")) if v]
            rows.append([
                str(rule.get("rule_name") or rule.get("rule_id") or ""),
                str(rule.get("rule_statement") or ""),
                str(rule.get("appeal_argument") or ""),
                ", ".join(authority_bits) or "—",
            ])
    if not rows:
        return None
    return {"headers": ["Rule", "Statement", "Appeal Argument", "Authority"], "rows": rows}


def _build_consolidator_input_json(
    plan: Plan,
    stub_answers: list[str],
    user_message: str,
    *,
    retrieval_metadata: dict | None = None,
    jurisdiction_summary: str | None = None,
    user_provided_context: str | None = None,
    workflow_selection_ui: dict[str, Any] | None = None,
    previous_thread_summary: str | None = None,
    react_draft: str | None = None,
    rag_chunks: list[dict] | None = None,
    tool_outputs: dict[str, list[dict]] | None = None,
    reasoning_ledger: list[dict] | None = None,
    task_context: dict | None = None,
    instant_rag_context: dict | None = None,
    recital_context: dict | None = None,
    tool_section_hints: list[dict] | None = None,
) -> str:
    """Build JSON payload for consolidator: user_message, subquestions, answers,
    retrieval_metadata, rag_chunks, jurisdiction_summary, user_provided_context,
    previous_thread_summary.

    2026-08-07 (Task #58, factory model): rag_chunks replaces the old
    sources_summary/source_texts split -- one unified list, capped/scored at
    the call site (integrate.py owns the cap; the upstream pool ctx.rag_chunks
    is deliberately uncapped since react's keep-set can exceed 7). tool_outputs
    and reasoning_ledger are new: raw non-rag tool results and react's
    per-round enrichment (learned/running_answer/gaps), respectively -- under
    the factory model react does the reasoning inline, so the enricher's job
    shrinks from synthesizing to formatting what react already concluded.
    """
    _subs = getattr(plan, "subquestions", None) or []
    _stub = stub_answers if stub_answers is not None else []
    subquestions = [{"id": sq.id, "text": sq.text} for sq in _subs]
    answers = []
    for i, sq in enumerate(_subs):
        ans = _stub[i] if i < len(_stub) else "[No answer yet]"
        answers.append({"sq_id": sq.id, "answer": (ans or "").strip()})
    payload = {
        "user_message": user_message.strip(),
        "subquestions": subquestions,
        "answers": answers,
    }
    if retrieval_metadata:
        payload["retrieval_metadata"] = retrieval_metadata
    if rag_chunks:
        payload["rag_chunks"] = rag_chunks
    if jurisdiction_summary and jurisdiction_summary.strip():
        payload["jurisdiction_summary"] = jurisdiction_summary.strip()
    if user_provided_context and user_provided_context.strip():
        payload["user_provided_context"] = user_provided_context.strip()
    if workflow_selection_ui:
        payload["workflow_selection_ui"] = workflow_selection_ui
    # Phase 13.7 — rolling thread summary. The integrator gets the
    # PREVIOUS summary as input and is asked to refine it (not append)
    # to integrate this turn. Output goes back as ``thread_summary`` in
    # the AnswerCard JSON. ≤60 words. See prompt instructions.
    if previous_thread_summary and previous_thread_summary.strip():
        payload["previous_thread_summary"] = previous_thread_summary.strip()
    # react_draft is what the user already saw (Summary tab, pinned permanently
    # per Ananth's ruling). Under the factory model react has already reasoned
    # across evidence inline (reasoning_ledger carries that); rag_chunks still
    # provides verbatim text for accurate citation snippets, since citations
    # need real chunk text, not just react's enrichment prose.
    if react_draft and react_draft.strip():
        # Raise cap for analytics queries (tool_section_hints present) — rate tables
        # are inherently long and a 6000-char cut produces truncated mid-sentence output.
        _draft_cap = 16000 if tool_section_hints else 6000
        payload["react_draft"] = react_draft.strip()[:_draft_cap]
    if reasoning_ledger:
        payload["reasoning_ledger"] = reasoning_ledger
    if tool_outputs:
        payload["tool_outputs"] = tool_outputs
    if task_context:
        payload["task_context"] = task_context
    if instant_rag_context:
        payload["instant_rag_context"] = instant_rag_context
    if recital_context:
        payload["recital_context"] = recital_context
    # Structured section hints from analytics tool outputs (section_format, headers, rows, items).
    # Pre-build typed sections from hints so the integrator cannot fall back to bullets
    # for data that has explicit structure. The LLM must include these sections verbatim
    # and may add 1-2 additional narrative sections around them.
    if tool_section_hints:
        # Cap rows per table hint — uncapped rate tables (2000+ HCPCS rows) blow
        # up the integrator prompt past Vertex AI's per-minute TPM limit (observed
        # 473K char prompt → 429). pre_built_sections is what the LLM actually
        # copies verbatim; tool_section_hints is dropped from the payload to avoid
        # sending the same data twice.
        _MAX_ROWS = 200
        pre_built: list[dict] = []
        for h in tool_section_hints:
            fmt = h.get("section_format", "")
            if not fmt or fmt == "bullets":
                continue
            section: dict = {
                "intent": "process",
                "label": h.get("label") or h.get("section_title") or "Data",
                "format": fmt,
            }
            if fmt == "table":
                hdrs = h.get("table_headers") or []
                rows = h.get("rows") or []
                if hdrs and rows:
                    capped = rows[:_MAX_ROWS]
                    section["data"] = {"headers": hdrs, "rows": capped}
                    if len(rows) > _MAX_ROWS:
                        section["data"]["truncated"] = len(rows) - _MAX_ROWS
                    pre_built.append(section)
            elif fmt in ("stats", "bars"):
                items = h.get("items") or []
                if items:
                    section["data"] = {"items": items}
                    pre_built.append(section)
            elif fmt == "appeals_rules" and isinstance(h.get("data"), dict):
                # Task #60: real table format + clean row data, not a custom
                # format string the model has to preserve verbatim.
                table_data = _appeals_rules_table_data(h["data"])
                if table_data:
                    rows = table_data["rows"]
                    if len(rows) > _MAX_ROWS:
                        table_data = {**table_data, "rows": rows[:_MAX_ROWS], "truncated": len(rows) - _MAX_ROWS}
                    section["format"] = "table"
                    section["data"] = table_data
                    section["visibility"] = "primary"
                    pre_built.append(section)
            elif h.get("data") is not None:
                # Generic pass-through for other custom formats (e.g.
                # appeals_playbook) — data blob is owned by the frontend
                # renderer. visibility:primary ensures it renders in the
                # Summary tab, not tucked behind "Show details".
                section["data"] = h["data"]
                section["visibility"] = "primary"
                pre_built.append(section)
        if pre_built:
            payload["pre_built_sections"] = pre_built
    return json.dumps(payload, indent=2)


def _extract_json_from_text(text: str) -> str:
    """Extract JSON object from text that may have markdown fences or leading/trailing prose."""
    text = (text or "").strip()
    if not text:
        return ""
    # Strip markdown code fence (```json ... ``` or ``` ... ```)
    if "```" in text:
        start = text.find("```")
        rest = text[start + 3 :].lstrip()
        if rest.lower().startswith("json"):
            rest = rest[4:].lstrip()
        end = rest.find("```")
        if end >= 0:
            rest = rest[:end].rstrip()
        text = rest
    # If text looks like it has JSON, try to find the outermost {...}
    if "{" in text and "}" in text:
        start = text.find("{")
        depth = 0
        for i, c in enumerate(text[start:], start):
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    return text[start : i + 1]
    return text.strip()


def _parse_answer_card(text: str, emitter: Callable[[str], None] | None = None) -> dict | None:
    """Parse text as JSON and validate AnswerCard shape (mode, direct_answer, sections). Returns dict or None.
    Tries stdlib json first, then json_repair for malformed LLM output. Optionally emits progress to emitter."""
    if not text or not text.strip():
        return None
    text = _extract_json_from_text(text)
    if not text:
        return None

    def _normalize_answer_card(data: dict) -> dict:
        """Coerce sections so one bad intent does not void the whole card (avoids losing details)."""
        valid_intents = ("process", "requirements", "definitions", "exceptions", "references")
        valid_formats = ("bullets", "table", "stats", "bars", "steps", "conditions")
        out = dict(data)
        sections = out.get("sections")
        if not isinstance(sections, list):
            return out
        fixed: list[dict] = []
        for item in sections:
            if not isinstance(item, dict):
                continue
            sec = dict(item)
            intent = sec.get("intent")
            if intent is None or intent not in valid_intents:
                if intent is not None and intent not in valid_intents:
                    logger.warning(
                        "Integrator AnswerCard: invalid section intent %r coerced to references",
                        intent,
                    )
                sec["intent"] = "references"
            # Normalize LLM-invented ui_blocks → canonical format/data structure.
            # The LLM sometimes puts table/stats data in ui_blocks instead of
            # format+data when given tool_section_hints. Map it back to schema.
            if "ui_blocks" in sec and not sec.get("format"):
                blocks = sec.pop("ui_blocks", []) or []
                for blk in blocks:
                    if not isinstance(blk, dict):
                        continue
                    blk_type = blk.get("type", "")
                    if blk_type == "table" and blk.get("headers") and blk.get("rows"):
                        sec["format"] = "table"
                        sec["data"] = {"headers": blk["headers"], "rows": blk["rows"]}
                        sec.pop("bullets", None)
                        break
                    if blk_type in ("stats", "bars") and blk.get("items"):
                        sec["format"] = blk_type
                        sec["data"] = {"items": blk["items"]}
                        sec.pop("bullets", None)
                        break
            # Ensure format is set; default bullets when absent.
            if sec.get("format") and sec["format"] not in valid_formats:
                sec["format"] = "bullets"
            fixed.append(sec)
        out["sections"] = fixed
        return out

    def _validate(data: object) -> dict | None:
        if not isinstance(data, dict):
            return None
        if "mode" not in data or "direct_answer" not in data or "sections" not in data:
            return None
        if data.get("mode") not in ("FACTUAL", "CANONICAL", "BLENDED"):
            return None
        sections = data.get("sections")
        if not isinstance(sections, list):
            return None
        data = _normalize_answer_card(data)
        return data

    def _try_parse(raw: str) -> dict | None:
        for parse_fn, label in [(json.loads, "json"), (_json_repair_loads, "json_repair")]:
            try:
                data = parse_fn(raw)
                return _validate(data)
            except Exception:
                pass
        return None

    try:
        out = _try_parse(text)
        if out is not None:
            logger.debug("AnswerCard parsed successfully")
            return out
    except Exception as e:
        logger.debug("AnswerCard parse failed: %s", e)
    return None


def _json_repair_loads(text: str) -> object:
    import json_repair
    return json_repair.loads(text)


# Phase 0.16b: the LLM-based ``_repair_json`` tier was deleted.
# ``_parse_answer_card`` already runs ``json.loads`` followed by the
# ``json_repair`` library (line ~172) — between them they handle the
# malformed shapes an LLM re-emission pass would have caught, without an
# additional API call. The LLM repair path was responsible for the
# Groq ``daily TPD exhausted`` leak (see worker logs 2026-04-17) and
# added $0.01 + 2-5s per malformed turn. If both stdlib + json_repair
# fail, the flow below now goes straight to
# ``extract_user_visible_text_from_integrator_raw`` which wraps the
# prose as FACTUAL and emits via the Phase 0.12 envelope — clean
# failure, no extra LLM burn.


def _emit_integrator_chunks(text: str, message_chunk_callback: Callable[[str], None] | None) -> None:
    """Simulate streaming for UI when using non-streaming llm_manager path."""
    if not message_chunk_callback or not text:
        return
    step = max(32, min(256, len(text) // 48 or 32))
    for i in range(0, len(text), step):
        message_chunk_callback(text[i : i + step])


def _fallback_message(plan: Plan, stub_answers: list[str]) -> str:
    """Exception-path fallback (the integrator LLM call itself failed/timed
    out) -- returns a valid minimal AnswerCard JSON string, not raw text.
    2026-08-07: this used to return plain joined paragraphs, no card
    structure at all. On a tool-heavy turn, stub_answers can hold a tool's
    raw JSON result verbatim (e.g. appeals_validate_claim's output) -- with
    no wrapping, that raw tool JSON leaked to the user as if it were the
    answer (surfaced by a genuine Vertex gemini-2.5-pro timeout, not a bug
    in the caller). Wrapping here means the downstream AnswerCard validator
    accepts it as-is (valid mode/direct_answer/sections) instead of further
    replacing it with a generic try-again stub -- one fallback layer, not two
    diverging ones."""
    from app.communication.json_display_sanitize import DEFAULT_BLEED_FALLBACK

    parts: list[str] = []
    _subs = getattr(plan, "subquestions", None) or []
    _stub = stub_answers if stub_answers is not None else []
    for i, sq in enumerate(_subs):
        ans = _stub[i] if i < len(_stub) else "[No answer yet]"
        parts.append(ans.strip())
    text = "\n\n".join(p for p in parts if p) or DEFAULT_BLEED_FALLBACK
    return json.dumps({"mode": "FACTUAL", "direct_answer": text, "sections": []})


def format_response(
    plan: Plan,
    stub_answers: list[str],
    user_message: str,
    emitter: Callable[[str], None] | None = None,
    message_chunk_callback: Callable[[str], None] | None = None,
    *,
    retrieval_metadata: dict | None = None,
    jurisdiction_summary: str | None = None,
    user_provided_context: str | None = None,
    workflow_selection_ui: dict[str, Any] | None = None,
    correlation_id: str | None = None,
    thread_id: str | None = None,
    config_sha: str | None = None,
    phi_detected: bool = False,
    llm_stage: str = "integrator",
    mode: str | None = None,
    previous_thread_summary: str | None = None,
    user_profile: dict | None = None,
    react_draft: str | None = None,
    rag_chunks: list[dict] | None = None,
    tool_outputs: dict[str, list[dict]] | None = None,
    reasoning_ledger: list[dict] | None = None,
    task_context: dict | None = None,
    instant_rag_context: dict | None = None,
    recital_context: dict | None = None,
    tool_section_hints: list[dict] | None = None,
) -> tuple[str, LLMUsageDict | None]:
    """Turn plan + answers into one chat-friendly message via llm_manager (integrator or integrator_roster).
    On LLM failure, returns fallback and None usage."""
    _subs = getattr(plan, "subquestions", None) or []
    trace_entered("responder.final.format_response", subquestions=len(_subs))
    if not _subs:
        return ("", None)

    # Formatting message emitted by orchestrator before integrate
    usage: LLMUsageDict | None = None

    try:
        from app.chat_config import get_chat_config
        from app.services.llm_manager import generate_sync

        cfg = get_chat_config()
        consolidator_input_json = _build_consolidator_input_json(
            plan, stub_answers, user_message,
            retrieval_metadata=retrieval_metadata,
            rag_chunks=rag_chunks,
            jurisdiction_summary=jurisdiction_summary,
            user_provided_context=user_provided_context,
            workflow_selection_ui=workflow_selection_ui,
            previous_thread_summary=previous_thread_summary,
            react_draft=react_draft,
            tool_outputs=tool_outputs,
            reasoning_ledger=reasoning_ledger,
            task_context=task_context,
            instant_rag_context=instant_rag_context,
            recital_context=recital_context,
            tool_section_hints=tool_section_hints,
        )
        canonical_score = blended_canonical_score(plan)
        consolidator_type = choose_consolidator_type(
            canonical_score,
            cfg.prompts.consolidator_factual_max,
            cfg.prompts.consolidator_canonical_min,
        )
        consolidator_line = f"Consolidator: {consolidator_type.capitalize()} (blended canonical score: {canonical_score:.2f})"
        logger.info("[consolidator] %s", consolidator_line)
        _emit(
            emitter,
            f"  → Building answer card ({consolidator_type} mode, score {canonical_score:.2f})…",
        )

        if consolidator_type == "canonical":
            prompt_system = cfg.prompts.integrator_canonical_system
        else:
            # unified "answer" path (FACTUAL/BLENDED collapsed) -- legacy hardcoded
            # fallback only; MOBIUS_PROMPT_SOURCE=composition (live in dev) resolves
            # module.enricher.answer below instead.
            prompt_system = cfg.prompts.integrator_blended_system

        # v2 modular-prompt path (MOBIUS_PROMPT_SOURCE=composition): resolve the
        # enricher system prompt from the DB-backed composable blocks instead of the
        # hardcoded string. Flag-gated + fail-soft — any miss/error keeps the prompt
        # above, so live chat is untouched until the flag flips and never breaks on a
        # resolution problem. (LLMManager v2 §5; composition seeded via block_seed.)
        import os as _os
        _rc = None
        if (_os.environ.get("MOBIUS_PROMPT_SOURCE") or "").strip().lower() == "composition":
            try:
                from app.services.prompt_manager import resolve_composition_sync
                _mk = {
                    "answer": "integrator_enricher_answer",
                }.get(consolidator_type)
                if _mk:
                    _rc = resolve_composition_sync(
                        _mk,
                        conditions={"emits_json": True, "hipaa_on": bool(phi_detected), "has_org": False},
                    )
                    if _rc and _rc.system_prompt.strip():
                        prompt_system = _rc.system_prompt
                        # FULL hash logged (not truncated) — this line is the display copy of
                        # composition_hash, the real value stored/compared is always full-sha256
                        # (prompt_blocks.py._hash); a prior truncated display copy here was
                        # mistaken for a storage regression during Tech Health's spot-check.
                        logger.info("[integrator] v2 composition prompt module=%s hash=%s",
                                    _mk, _rc.composition_hash)
            except Exception as _e:
                logger.warning("[integrator] composition resolve failed, using hardcoded: %s", _e)
        # 2026-05-06: integrator/consolidator is the user-facing voice —
        # tone + ai_experience_level matter most here. Splice user
        # profile rendered_prompt into the system block with an explicit
        # VOICE DIRECTIVE header so it wins over section-count defaults.
        # Without the header the model reads rendered_prompt as a
        # postscript and the structural JSON schema overrides it
        # (e.g. "no bullet scaffolding" loses to "2-3 sections required").
        try:
            _rp = (user_profile or {}).get("rendered_prompt", "") if isinstance(user_profile, dict) else ""
            if _rp and _rp.strip():
                prompt_system = (
                    f"{prompt_system}\n\n"
                    "VOICE DIRECTIVE (overrides section-count defaults above):\n"
                    f"{_rp.strip()}\n"
                    "For tone=concise: reduce sections to 0–1 with 1–2 bullets max; "
                    "direct_answer carries the full verdict.\n"
                    "For tone=friendly: sections allowed; use conversational bullets and direct_answer warmth.\n"
                    "For tone=professional: follow mode-specific section counts; formal precise language."
                )
            elif user_profile:
                from app.pipeline.personalization import splice_user_profile
                prompt_system = splice_user_profile(prompt_system, user_profile)
        except Exception:
            pass  # never let personalization break a real turn
        prompt_user = cfg.prompts.integrator_user_template.format(
            consolidator_input_json=consolidator_input_json,
        )
        prompt = f"{prompt_system}\n\n{prompt_user}"

        _emit(emitter, "  Draft composer: calling LLM to generate answer card…")
        text, usage = generate_sync(
            prompt,
            stage=llm_stage,
            max_tokens=4096,
            config_sha=config_sha,
            correlation_id=correlation_id,
            thread_id=thread_id,
            phi_detected=phi_detected,
            mode=mode,
            composition_id=(_rc.composition_id if _rc else None),
            composition_hash=(_rc.composition_hash if _rc else None),
        )
        text = (text or "").strip()

        if text:
            _emit(emitter, "  Validator: checking answer card (mode, direct_answer, sections)…")
            parsed = _parse_answer_card(text, emitter=emitter)
            # Phase 0.16b: no LLM-based repair tier. _parse_answer_card already
            # tries json.loads then json_repair — if both fail, fall straight
            # through to the FACTUAL-wrap fallback below.
            if parsed is not None:
                _emit(emitter, "  Final composer: answer card ready.")
                logger.debug("Emitting canonical AnswerCard JSON to frontend")
                parsed = dict(parsed)
                display_txt = display_text_for_parsed_answer_card(parsed)
                if not display_txt.strip():
                    # BETA-sprint Move 1 — JSON reliability on the
                    # transform path. The bench surfaced a failure
                    # mode where the integrator emits valid JSON but
                    # the direct_answer field bleeds (nested JSON,
                    # raw markdown). On a continuation turn (signaled
                    # by previous_thread_summary != None) we have a
                    # perfectly good stub answer — the transform
                    # skill's prose — sitting in stub_answers. Use it
                    # before going to the generic "trouble formatting"
                    # message. This converts a user-visible "rephrase
                    # your question" failure into the actual answer
                    # the model intended to give.
                    if previous_thread_summary and stub_answers:
                        candidate = (stub_answers[0] if stub_answers else "").strip()
                        # Require enough text that we're confident
                        # we're not papering over a real failure.
                        if candidate and len(candidate) >= 20:
                            display_txt = candidate[:8000]
                            logger.warning(
                                "[transform-path] integrator direct_answer "
                                "bled; recovered using stub answer (cid=%s, "
                                "stub_len=%d)",
                                (correlation_id or "?")[:8], len(candidate),
                            )
                if not display_txt.strip():
                    display_txt = DEFAULT_BLEED_FALLBACK
                parsed["direct_answer"] = display_txt
                _emit_integrator_chunks(display_txt, message_chunk_callback)
                # Emit canonical JSON so frontend receives clean JSON (no markdown fence)
                return (json.dumps(parsed), usage)
            # Not valid AnswerCard: never stream raw model JSON (common: resolutions-only blob).
            _log_truncated = (text or "")[:2000] + ("..." if len(text or "") > 2000 else "")
            logger.warning(
                "Consolidator output was not valid AnswerCard JSON; wrapping prose as FACTUAL. LLM response (truncated): %s",
                _log_truncated,
            )
            visible = extract_user_visible_text_from_integrator_raw(text)
            minimal = build_minimal_answer_card_preserving_metadata(visible, text)
            _emit_integrator_chunks(visible, message_chunk_callback)
            return (json.dumps(minimal), usage)
    except Exception as e:
        logger.warning(
            "Integrator LLM failed, using fallback (no valid response). exception=%s",
            e,
            exc_info=True,
        )
        logger.debug("Using simple format (integrator LLM failed)")

    fb = _fallback_message(plan, stub_answers)
    _emit_integrator_chunks(fb, message_chunk_callback)
    return (fb, None)
