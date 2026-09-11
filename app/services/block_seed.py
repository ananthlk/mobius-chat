"""Seed prompt_blocks + prompt_compositions + prompt_composition_members
(migration 053) from the validated v2 block set.

STAGED — ready to run the instant 053 lands (gated on the tables existing). This
is step 2 of docs/RUNBOOK_V2_DEPLOY.md. Idempotent: blocks upsert ON CONFLICT
(block_key, version) DO NOTHING; a composition is (re)written only when its
coherence gate passes, so an incoherent composition is never seeded active.

Design notes:
  * Long/authoritative bodies (the AnswerCard schema+rules block) are sourced
    from the single existing source of truth — chat_config — at seed time via
    `resolve_body`, NOT duplicated here. Short structural blocks are inline.
  * composition_hash is computed with the SAME prompt_blocks._hash the assembler
    uses, so the seeded hash matches what runtime will produce.
  * check_coherence() runs before an active composition is written — the real
    write-time gate, at seed time.
"""
from __future__ import annotations

from dataclasses import dataclass

import datetime as _dt

from app.services.prompt_blocks import BlockAssembler, Block, _hash

_VALIDATED_AT = "2026-07-26"          # v2 gate closed 6/6; blocks validated at sign-off


def _ts(s: str | None):
    """ISO date/datetime string → tz-aware datetime for a timestamptz column (asyncpg
    validates the Python type, so a bare string is rejected)."""
    if not s:
        return None
    return _dt.datetime.fromisoformat(s).replace(tzinfo=_dt.timezone.utc) if len(s) <= 10 \
        else _dt.datetime.fromisoformat(s)


@dataclass(frozen=True)
class BlockSpec:
    block_key: str
    block_kind: str
    role: str
    body: str
    condition: str | None = None
    is_authority: bool = False
    directives: tuple = ()
    owner: str = "llm-agent"
    validated_at: str | None = None    # authority blocks REQUIRE this (053 ck_authority_validated)


# ── the validated enricher block set (first module migrated; matches the A/B
#    decomposition proven BETTER against real Claude in v2_migrate_enricher.py) ──
_ENRICHER_PREAMBLE = (
    "You are the ENRICHER for a retrieval-based Q&A system.\n\n"
    "The user has ALREADY seen react_draft (provided in the input JSON). Your job is NOT to "
    "restate or rephrase it. Your job is to:\n"
    "  1. Correct any factual error in the draft (if sources contradict it)\n"
    "  2. Pull verbatim evidence from source_texts to back up key claims\n"
    "  3. Distill what matters into takeaways\n"
    "  4. Specify concrete next actions\n"
    "  5. Flag what the sources did not cover"
)
_ENRICHER_FACTUAL = (
    "Mode-specific rules for FACTUAL:\n"
    "- Set mode = 'FACTUAL'.\n"
    "- sections: 2–3 sections, each with 3–6 substantive bullets (8–25 words each). "
    "Intent must be one of: process, requirements, definitions, exceptions, references. "
    "No stub bullets. FACTUAL hides sections behind 'Show details' — they must carry real detail.\n"
    "- direct_answer is ONE sentence — the single operative fact.\n"
    "- required_variables: if the answer depends on an unknown (service code, plan subtype), "
    "list it. Add one followup question only if the user must clarify to get a definitive answer."
)

# 2026-08-07 (architecture directive, via Chat Master/Ananth): FACTUAL and BLENDED
# collapse into one unified path -- mode-specific branching between them was causing
# real bugs (early exits, bleed, display gaps). CANONICAL and RECITAL stay distinct
# (legitimate architectural reasons -- different corpus-confidence regime / verbatim
# rendering contract respectively). This is a genuinely NEW merged instruction, not a
# faithful slice of chat_config (unlike the @enricher: parts above) -- inline body.
# mode value stays 'FACTUAL' (see routing collapse in final.py/final_parallel.py):
# reused rather than renamed to keep the ~8 existing fallback/validator sites (which
# already treat "FACTUAL" as the safe default) and Chat FE's mode switch unchanged --
# the schema/output shape isn't changing, only the routing condition is removed, so a
# rename would touch every consumer for zero functional gain.
_ENRICHER_ANSWER = (
    "Mode-specific rules for FACTUAL:\n"
    "- Set mode = 'FACTUAL'.\n"
    "- sections: 2–4 sections, each with 3–6 substantive bullets (8–25 words each) when the "
    "source material supports it. No stub bullets. Intent must be one of: process, requirements, "
    "definitions, exceptions, references. 'requirements' and 'definitions' sections are visible "
    "by default; 'process', 'exceptions', 'references' are behind 'Show details'.\n"
    "- direct_answer: the single operative fact, in 1–3 sentences. Include specifics inline when "
    "sources supply them (codes, numbers, criteria names, page refs) -- do not retreat to a "
    "framework-name-only one-liner when the corpus supports more. If the corpus only supports one "
    "fact, one sentence is enough -- do not pad.\n"
    "- Provide concrete criteria as bullets in 'requirements'; code definitions in 'definitions'.\n"
    "- required_variables: if the answer depends on an unknown (service code, plan subtype), "
    "list it. Add one followup question only if the user must clarify to get a definitive answer."
)

# Governor seat, 2026-09-11: card.shape_schema + enricher.how are a split of
# enricher.answercard_schema_and_rules@14 (the live Studio-edited version, NOT
# the @enricher:schema marker above -- that marker resolves against
# chat_config's CURRENT integrator_*_system strings, which the Studio-edited
# v14 has long since diverged from; verified live before writing this).
#
# The split boundary is the ONLY clean one found: the raw JSON schema object
# (card.shape_schema, below) is genuinely source-agnostic -- any producer of
# an AnswerCard needs the same fields. Everything else (enricher.how) is
# interleaved with HOW to fill it from a prior react_draft/reasoning_ledger --
# "built from the LATEST reasoning_ledger entry's running_answer... fall back
# to react_draft + rag_chunks" is not shape with a how-footnote, the sourcing
# IS the instruction. Forcing those field rules into a shared block would mean
# authoring new source-agnostic rules, not moving lines -- rejected as too
# risky against the byte-identical invariant on this live path. A second
# consumer's "how" (react.communicate.how, for the no-enricher path) is
# authored fresh, later, once that routing exists -- it cannot be extracted,
# because react has no reasoning_ledger to consolidate and no react_draft to
# check against.
#
# Verified byte-identical via the real BlockAssembler before publishing:
# rendering [card.shape_schema, enricher.how] together (assembler's "\n\n"
# join) reproduces enricher.answercard_schema_and_rules@14's exact text --
# and the full integrator_enricher_answer composition (all 5 members) renders
# to the identical 17,104 characters before and after the swap. No exit-mode
# fields added in this pass (Governor's own correction: don't ship a schema
# slot nothing populates yet -- the field and its first writer ship together).
_CARD_SHAPE_SCHEMA = 'AnswerCard schema:\n{"mode":"FACTUAL|CANONICAL|BLENDED|RECITAL","direct_answer":"string (one-sentence backup — shown if draft unavailable)","correction":null,"takeaways":["string"],"sections":[{"intent":"process|requirements|definitions|exceptions|references","label":"string","format":"bullets|table|steps|stats|bars|conditions","bullets":["string"],"data":{"headers":["string"],"rows":[["string"]],"items":[{"label":"string","value":"string","note":"string","weight":0.0,"condition":"string","result":"string"}]}}],"recital":{"verbatim":"string","document_id":"string","section":"string"},"citations":[{"claim":"string","doc_title":"string","locator":"string","snippet":"string"}],"next_steps":["string"],"gaps":["string"],"next_questions_for_user":["string"],"thread_summary":"string","suggested_actions":[{"type":"external_link","label":"string","url":"string","icon":"string"}],"output_intent":"read|report|email|sms|emr|appeal|payor_report","display_summary":"string","tldr_summary":"string"}'

_ENRICHER_HOW = 'OR when there IS a correction:\n"correction":{"original":"the specific wrong claim from react_draft","corrected":"accurate statement per rag_chunks/tool_outputs"}\n\nEVERY key in the schema above is REQUIRED in your JSON output, on every single response, with no exceptions -- mode, direct_answer, correction, takeaways, sections, citations, next_steps, gaps, next_questions_for_user, thread_summary, suggested_actions, output_intent, display_summary, tldr_summary. Below, "omit" ALWAYS means "use an empty value for that key\'s type" (e.g. citations: [], next_steps: [], gaps: [], correction: null) -- it NEVER means "leave the key out of the JSON object." A short, simple, or low-reasoning turn is not an exception: every key must still appear. If you find yourself about to write a JSON object with fewer keys than the schema above lists, stop -- you are missing something.\n\nField rules (apply to ALL modes):\n- direct_answer: one sentence max. Backup only — do not repeat react_draft verbatim. Used when the draft is unavailable; keep it to the single operative fact.\n- output_intent: classify what kind of deliverable this answer is, based on the question\'s own phrasing — "read" | "report" | "email" | "sms" | "emr" | "appeal" | "payor_report".\n  - "read" (default): the user is asking to understand something, in-chat.\n  - "report": the user wants something exportable/shareable.\n  - "email": the question implies sending this to someone else.\n  - "sms": very short, single-fact lookup where a text-length answer is natural.\n  - "emr": clinical/patient-record-adjacent phrasing suggesting it belongs in a chart note.\n  - "appeal": the question is specifically about drafting/understanding an appeal or dispute.\n  - "payor_report": the question is about payer-facing documentation/compliance reporting.\n  If genuinely ambiguous, use "read" — do not force a more specific intent without a real signal.\n- display_summary: write this as if handing the user the finished answer directly — plain language, verdict up front, built from the LATEST reasoning_ledger entry\'s running_answer (react already reasoned this out; refine into finished prose, don\'t re-derive it). If reasoning_ledger is empty, fall back to react_draft + rag_chunks directly. For "email" phrase it as something ready to paste (greeting/sign-off placeholder); for "sms" keep it to 1-2 sentences; for "appeal" frame it as appeal-letter-ready language. This MUST be the full synthesized answer — same completeness and confidence as tldr_summary/direct_answer, never a reduced or hedged version. Do NOT let a correction or honesty-check assessment shrink display_summary down to only what was verified — if the running_answer leaned cautious after a groundedness check, still write display_summary as the best complete answer actually supported. Whatever couldn\'t be verified belongs in the correction field ONLY — it must never substitute for or dilute display_summary\'s own content.\n- tldr_summary: a 2-4 sentence TL;DR of the answer — the bottom line only (verdict + the one or two facts that matter most), NOT a shorter rewrite of display_summary\'s full formatted content. Always populate this on every complete turn, regardless of output_intent. This is separate from direct_answer (which stays a one-sentence fallback for when no draft exists) — tldr_summary is always written, direct_answer is the backup path.\n- correction: null unless a specific, verifiable claim in react_draft is directly contradicted by rag_chunks or tool_outputs. This should be RARE under the factory model — react already reconciled evidence during its own rounds, so a correction here usually means something changed between react\'s round and now, not that react was wrong. Only correct clear factual errors (wrong numbers, wrong codes, wrong deadlines) — not tone, phrasing, or level of detail. When in doubt, null.\n- takeaways: 2–3 short bullets — pull from the LATEST reasoning_ledger entry\'s running_answer, don\'t re-derive from rag_chunks/tool_outputs from scratch. Each bullet 10–20 words. Omit if nothing concrete emerged.\n- citations: for each key claim in react_draft/display_summary that rag_chunks supports, produce one entry. snippet MUST be a verbatim excerpt (≤200 chars) copied directly from the rag_chunks text field — do not paraphrase. locator = section heading or page reference if visible in the text. Omit entries where no verbatim match exists in rag_chunks.\n- next_steps: 0–3 CONCRETE actions. HARD RULE: every next_step MUST start with an external-action verb in imperative form — Submit / Call / Attach / File / Obtain / Include / Request / Contact. It must NEVER start with Determine / Decide / Check whether / Consider / Verify whether — those describe a mental step, not a physical one, and are FORBIDDEN as the step itself. If a scenario branches (e.g. urgent vs routine), do NOT write a branching-decision step ("Determine whether X or Y") — instead write ONE step per branch, each already committed to its own action. It is CORRECT and PREFERRED to return next_steps: [] when nothing concrete beyond the answer applies — never manufacture a task, and never restate a fact already in direct_answer/sections as if it were a new task.\n- gaps: 1–2 genuine coverage holes — pull from the LATEST reasoning_ledger entry\'s gaps_open first (react already identified these during its rounds; gaps_closed is context, not output material). Only fall back to assessing coverage yourself if reasoning_ledger is empty. Base this ONLY on the answer that was actually given, not on parallel retrieval arms that returned nothing. Omit (empty array []) if the answer was thorough.\n- next_questions_for_user: 0–3 follow-up questions. First decide which MODE applies:\n  MODE A — UNRESOLVED (gaps exist / the answer is not fully precise): each question asks for exactly ONE specific missing FACT/VARIABLE that would let the system re-answer precisely — a name, plan, date, code, dollar amount, provider type. Template: "What is [the missing variable]?" or "Which [specific thing] applies to your case?" BANNED in Mode A: any question about what to do AFTER getting an answer, tracking/verifying/next-steps, or "are you asking because..." speculation — that is Mode B\'s job, not Mode A\'s. If you catch yourself writing a question about a FUTURE action, delete it — Mode A only asks for missing INPUT DATA.\n  MODE B — RESOLVED (the answer is materially complete, no meaningful gap): each question is the NEXT step in the user\'s real-world task AFTER this fact — what they\'d realistically need next (how to act on it, what happens in an edge case, the next process step). Do NOT ask the user to restate or clarify what they just asked.\n  In both modes: 0–3 questions, each distinct in substance, never generic FAQ padding, never included just to hit a count. If task_context is present, suggest task-related follow-ups (filter by status/kind/org, create a task, show overdue). If instant_rag_context is present (user-uploaded document), generate questions that explore the document\'s specific content from the user\'s professional angle, tailored to user_role/user_org when available. 8–20 words each. Do not ask the user to share documents.\n- pre_built_sections: when present in input, each entry is a pre-formatted typed section (format/data already set). You MUST copy each pre_built_section into your sections[] output exactly as given — preserve format, label, and data BYTE-FOR-BYTE, including the format string itself. pre_built_sections.format is NOT limited to the bullets|table|steps|stats|bars|conditions list above — it may be a custom value owned by the frontend renderer (e.g. "appeals_rules", "appeals_playbook"). If you don\'t recognize the format value, that is expected — copy it exactly as given anyway. Do NOT normalize, correct, or substitute it with one of the six standard values, and do NOT convert pre_built_sections to bullets. Add up to 2 additional narrative sections around them.\n- sections[].format: for sections you write yourself (not pre_built_sections), choose by the CONTENT\'S NATURE, not just whether the source text already looks tabular -- bullets is the fallback of last resort, not the default. Reorganize prose into a richer format whenever the content fits one, even if the source itself was plain text:\n  • Comparing multiple things (programs, plans, options, tiers) → format=\'table\', one row per thing, data={headers:[...],rows:[[...],...]}. E.g. 3+ programs/plans described side by side is a table, even if the source described them in separate paragraphs.\n  • Rows with consistent columns in the source → format=\'table\' (same as above).\n  • 2–5 standalone numeric KPIs or summary counts → format=\'stats\', data={items:[{label,value,note?},...]}.\n  • Ranked list with weights → format=\'bars\', data={items:[{label,weight,note?},...]}.\n  • A sequential process (how to do X, step-by-step) → format=\'steps\', even if the source phrased it as a paragraph rather than a numbered list.\n  • Eligibility/plan-specific gates or if/then logic → format=\'conditions\'.\n  • Default format=\'bullets\' ONLY when the content is a flat list with no comparison, sequence, metric, or conditional structure to exploit.\nWhen format is not \'bullets\', omit bullets[] and use data instead.\n- recital: when recital_context is present and recital_context.verbatim is true, output mode "RECITAL" (not FACTUAL/CANONICAL/BLENDED). Schema: {"mode":"RECITAL","direct_answer":"From the [document name]:","recital":{"verbatim":"[exact text, markdown preserved]","document_id":"[recital_context.document_id if present, else omit]","section":"[recital_context.section if present, else omit]"}}. Do NOT include sections[]. Do NOT paraphrase or compress verbatim. Preserve all markdown formatting (bold, italics) from the source text. The literal schema above shows only the RECITAL-specific fields — it does NOT mean the other AnswerCard fields are skipped. Still populate next_questions_for_user (Mode B — presenting the verbatim document IS the complete resolved answer, so ask about the user\'s next real-world step: e.g. does the denial cite a specific rule, do they have the member\'s EOB, has this claim been appealed before), thread_summary, and suggested_actions per the general rules above/below. Only sections[]/direct_answer/display_summary are RECITAL-specific (kept minimal so nothing paraphrases the verbatim text) — every other field follows the same rules as every other mode.\n- thread_summary: topic label ≤60 chars. No question marks, no \'User asked\'. E.g. \'Claim dispute process — Sunshine Health\'.\n- suggested_actions: populate ONLY for claim denial, appeal, reconsideration, CARC/RARC code, or dispute questions. One entry: {"type":"external_link","label":"Open Appeals Agent","url":"https://mobius-appeals-prototype-ortabkknqa-uc.a.run.app","icon":"⚖️"}. Empty array [] otherwise.\n- Use ONLY facts from the input. Do not add new facts not present in answers, reasoning_ledger, rag_chunks, or tool_outputs.\n- tool_outputs: raw non-rag tool results (dict keyed by tool name -> list of calls), not pre-extracted into a section_hint. Check this directly when react_draft/reasoning_ledger references something not already covered by rag_chunks or a pre_built_section — do not say data doesn\'t exist without checking here first.\n\n\n'

BLOCK_SPECS: list[BlockSpec] = [
    # The enricher blocks are a FAITHFUL, byte-preserving decomposition of the live
    # chat_config integrator_{factual,blended,canonical}_system prompts (single source
    # of truth) — sliced at section markers, not re-typed. intro+schema are IDENTICAL
    # across all three modes (verified: only the mode-rules tail differs), so they're
    # shared blocks; each mode gets its own rules leaf.
    # concat(intro, schema, <mode>_rules) == chat_config.integrator_<mode>_system.
    BlockSpec("module.enricher", "static", "system", "@enricher:intro", owner="llm-agent"),
    BlockSpec("enricher.answercard_schema_and_rules", "static", "system",
              "@enricher:schema", owner="llm-agent"),
    BlockSpec("module.enricher.factual", "static", "system", "@enricher:factual", owner="llm-agent"),
    BlockSpec("module.enricher.blended", "static", "system", "@enricher:blended", owner="llm-agent"),
    # Unified FACTUAL+BLENDED path (2026-08-07 architecture directive) -- supersedes
    # the two block/composition pairs above, which are left seeded (for history/
    # rollback) but no longer referenced by any live composition.
    BlockSpec("module.enricher.answer", "static", "system", _ENRICHER_ANSWER, owner="llm-agent"),
    # card.shape_schema / enricher.how: see the comment above _CARD_SHAPE_SCHEMA.
    BlockSpec("card.shape_schema", "static", "system", _CARD_SHAPE_SCHEMA, owner="llm-agent"),
    BlockSpec("enricher.how", "static", "system", _ENRICHER_HOW, owner="llm-agent"),
    BlockSpec("hipaa_context", "conditional", "system",
              "HIPAA: never surface or invent a patient identifier (name, MRN, DOB, address). "
              "Never fabricate a clinical fact. If PHI is required to answer and none is grounded, "
              "say so — do not guess.",
              condition="hipaa_on", is_authority=True, owner="compliance", validated_at=_VALIDATED_AT),
    BlockSpec("forced_json", "conditional", "system",
              "Return ONLY valid JSON. No markdown, no commentary, no extra text.",
              condition="emits_json", is_authority=True, directives=("output:json",),
              owner="llm-agent", validated_at=_VALIDATED_AT),
]

# module_key → ordered block_keys (authority blocks last; assembler re-asserts).
COMPOSITIONS: dict[str, list[str]] = {
    "integrator_enricher_factual": [
        "module.enricher",
        "enricher.answercard_schema_and_rules",
        "module.enricher.factual",
        "hipaa_context",
        "forced_json",
    ],
    "integrator_enricher_blended": [
        "module.enricher",
        "enricher.answercard_schema_and_rules",
        "module.enricher.blended",
        "hipaa_context",
        "forced_json",
    ],
    # v2 (2026-09-11, Governor seat): enricher.answercard_schema_and_rules
    # replaced by card.shape_schema + enricher.how -- see the comment above
    # _CARD_SHAPE_SCHEMA. This module_key's own seed() path only ever writes
    # composition version 1 (_UPSERT_COMPOSITION hardcodes it) -- the live
    # v2 that actually carries this change was published directly against
    # dev (see docs/governor-contracts-settled.md), matching this list so a
    # future re-seed-from-scratch reproduces the same member set. Real
    # composition version-bump support (deactivate-old + insert-new, the way
    # react_block_seed.py already does it) does not exist in this file --
    # flagged, not built, out of scope for this change.
    "integrator_enricher_answer": [
        "module.enricher",
        "card.shape_schema",
        "enricher.how",
        "module.enricher.answer",
        "hipaa_context",
        "forced_json",
    ],
}


_ENRICHER_MODE_ATTR = {
    "factual": "integrator_factual_system",
    "blended": "integrator_blended_system",
    "canonical": "integrator_canonical_system",
}
_SCHEMA_MARKER = "AnswerCard schema"


def _decompose_enricher(strict: bool = True) -> dict:
    """FAITHFUL byte-preserving split of each chat_config.integrator_<mode>_system into
    (intro, schema, <mode>) at the shared schema marker + the per-mode rules tail.
    concat(intro, schema, mode) == the original prompt for EVERY mode — no re-typing,
    no drift. intro/schema are asserted IDENTICAL across modes (else seeding refuses —
    a divergence there would mean the modes no longer share the schema, and silently
    seeding one mode's intro/schema for all three would be wrong). strict=False
    tolerates absence (dry-run)."""
    try:
        from app.chat_config import ChatPromptsConfig
        cfg = ChatPromptsConfig()
    except Exception:
        cfg = None

    out: dict[str, str] = {}
    intro = schema = None
    for mode, attr in _ENRICHER_MODE_ATTR.items():
        s = (getattr(cfg, attr, "") or "") if cfg else ""
        a = s.find(_SCHEMA_MARKER)
        rules_marker = f"Mode-specific rules for {mode.upper()}"
        b = s.find(rules_marker)
        if not s or a <= 0 or b <= a:
            if strict:
                raise ValueError(
                    f"block_seed: could not decompose chat_config.{attr} at "
                    f"{_SCHEMA_MARKER!r} / {rules_marker!r} — prompt structure changed."
                )
            out[mode] = f"<<{mode}>>"
            continue
        this_intro, this_schema, this_rules = s[:a], s[a:b], s[b:]
        if intro is None:
            intro, schema = this_intro, this_schema
        elif strict and (this_intro != intro or this_schema != schema):
            raise ValueError(
                f"block_seed: {attr}'s intro/schema diverges from the other modes' — "
                f"they're supposed to be shared blocks. Re-check before seeding a split."
            )
        out[mode] = this_rules
    out["intro"] = intro or "<<intro>>"
    out["schema"] = schema or "<<schema>>"
    return out


def resolve_body(spec: BlockSpec, *, strict: bool = True) -> str:
    """Single-source-of-truth resolver. '@enricher:<part>' bodies are sliced from the
    live chat_config.integrator_factual_system (faithful decomposition) so the seeded
    blocks never drift from the live prompt. Inline bodies pass through unchanged.
    strict=True (real seed): a failed decomposition RAISES (no blind seed);
    strict=False (dry-run): placeholders, since body TEXT doesn't affect coherence."""
    if not spec.body.startswith("@enricher:"):
        return spec.body
    part = spec.body.split(":", 1)[1]
    return _decompose_enricher(strict=strict)[part]


def _to_block(spec: BlockSpec, body: str, version: int = 1) -> Block:
    return Block(
        block_key=spec.block_key, block_kind=spec.block_kind, role=spec.role,
        template_body=body, version=version, condition=spec.condition,
        is_authority=spec.is_authority, directives=frozenset(spec.directives),
        owner=spec.owner, validated_at=spec.validated_at,
    )


def build_plan(*, strict: bool = True) -> tuple[list[tuple[BlockSpec, str]], dict[str, dict]]:
    """Resolve bodies + coherence-check every composition BEFORE any DB write.
    Returns (resolved_blocks, compositions_plan). Raises on an incoherent
    composition — the real check_coherence() gate, at seed time. strict=False
    lets the plan validate without a wired chat_config attr (dry-run)."""
    resolved = [(s, resolve_body(s, strict=strict)) for s in BLOCK_SPECS]
    blocks = {s.block_key: _to_block(s, body) for s, body in resolved}

    asm = BlockAssembler()
    plan: dict[str, dict] = {}
    for module_key, comp in COMPOSITIONS.items():
        problems = asm.check_coherence(
            comp, blocks,
            required={"hipaa_on": "hipaa_context"},
            active_conditions={"hipaa_on", "emits_json"},
        )
        if problems:
            raise ValueError(f"block_seed: composition {module_key!r} is incoherent: {problems}")
        pairs = tuple((blocks[bk].block_key, blocks[bk].version) for bk in comp)
        plan[module_key] = {"blocks": comp, "composition_hash": _hash(pairs)}
    return resolved, plan


# ── DB write (idempotent). Executed only post-053; guarded so import is safe. ──
# Block inserts go through app.services.prompt_block_publish.
# publish_block_version() now (Governor seat, 2026-09-10) -- no
# block-insert SQL lives in this file anymore.
_UPSERT_COMPOSITION = """
INSERT INTO prompt_compositions
  (module_key, variant_id, version, status, active, composition_hash,
   coherence_checked_at, created_by)
VALUES ($1,'default',1,'validated',true,$2, NOW(),'block_seed')
ON CONFLICT (module_key, variant_id, version) DO NOTHING
RETURNING id
"""
_INSERT_MEMBER = """
INSERT INTO prompt_composition_members (composition_id, position, block_key, pinned_version)
VALUES ($1,$2,$3,$4)
ON CONFLICT (composition_id, position) DO NOTHING
"""


async def seed(conn) -> dict:
    """Idempotent seed against a live 053 schema. Coherence is checked before any
    write (build_plan raises otherwise). Returns a per-module summary.

    Governor seat, 2026-09-10: flagged as a third independent writer to
    prompt_blocks, alongside react_block_seed.py and admin_prompts.
    create_block_version — both of which already route through
    publish_block_version() for the same reason this one now does:
    migration 066 (token_counts) landed in neither of the OTHER two
    originally either, until each was found and fixed. Confirmed live
    this isn't hypothetical: 5 of this file's 7 block_keys (forced_json,
    hipaa_context, module.enricher.answer/.blended/.factual) are STILL at
    the v1 this seed wrote, with no token_counts, actively serving live
    compositions today (the other 2 have since moved on entirely via the
    Studio UI path). auto_validate=False + explicit validated_at/
    validated_by passthrough matters MORE here than in react_block_seed.py
    -- this file's authority blocks (hipaa_context, forced_json) carry a
    real historical validation date (_VALIDATED_AT = "2026-07-26", the v2
    gate sign-off), not react's always-None. Stamping now() here would
    have fabricated a re-validation that never happened.
    """
    from app.services.prompt_block_publish import publish_block_version

    resolved, plan = build_plan()
    for spec, body in resolved:
        blk = _to_block(spec, body)
        await publish_block_version(
            conn,
            block_key=blk.block_key,
            template_body=blk.template_body,
            block_kind=blk.block_kind,
            role=blk.role,
            condition=blk.condition,
            is_authority=blk.is_authority,
            directives=list(blk.directives),
            owner=blk.owner,
            created_by="block_seed",
            activate=True,
            version=blk.version,
            auto_validate=False,
            validated_at=_ts(blk.validated_at),
            validated_by=(blk.owner if blk.validated_at else None),
        )
    out = {}
    for module_key, info in plan.items():
        row = await conn.fetchrow(_UPSERT_COMPOSITION, module_key, info["composition_hash"])
        if row is not None:  # freshly inserted; write its ordered members
            for pos, bk in enumerate(info["blocks"], start=1):
                # pinned_version=NULL (not a hardcoded version): a 'validated' (not yet
                # 'frozen') composition is meant to FLOAT to each block's latest active
                # version — that's the whole point of the live-editable block system (a
                # new block version takes effect with no redeploy, no composition edit).
                # Pinning here would silently defeat that (caught live: a v2 block edit
                # had no effect until this was found and fixed).
                await conn.execute(_INSERT_MEMBER, row["id"], pos, bk, None)
            out[module_key] = {"seeded": True, "composition_hash": info["composition_hash"]}
        else:
            out[module_key] = {"seeded": False, "reason": "already present"}
    return out


if __name__ == "__main__":  # pragma: no cover — dry-run without a DB
    # `python3 -m app.services.block_seed` validates the plan (coherence + hashes)
    # WITHOUT touching a DB, so the seed can be checked pre-053.
    _resolved, _plan = build_plan(strict=False)
    print("block_seed dry-run — plan is coherent:")
    for mk, info in _plan.items():
        print(f"  {mk}: {len(info['blocks'])} blocks  hash={info['composition_hash'][:16]}…")
