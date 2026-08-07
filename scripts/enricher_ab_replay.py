"""Replay harness for the FACTUAL/BLENDED enricher-merge A/B (2026-08-07).

Takes a chat_turns row (by correlation_id) and reconstructs the enricher's
input from what's actually persisted -- plan_snapshot (subquestions), sources
(verbatim cite_text -> source_texts), and the draft_ready progress event
(react_draft) -- NOT live re-retrieval, so retrieval variance never confounds
the prompt comparison. Runs the SAME reconstructed input through both the
pre-merge (FACTUAL or BLENDED, caller's choice) and post-merge (unified
answer) composed system prompts, via the exact prompt_system + "\n\n" +
prompt_user + generate_sync() construction format_response() itself uses --
this script does not modify or call format_response()/choose_consolidator_type
directly, so it can never affect the live routing path.

Both variants use the SAME stage="integrator" for generate_sync(), so model
routing (hence temperature/model) is identical across the pair -- the prompt
text is the only variable.

Usage:
    python3 -m scripts.enricher_ab_replay --ids id1,id2,... --out /tmp/ab_pairs.json
    python3 -m scripts.enricher_ab_replay --sample 50 --out /tmp/ab_pairs.json
        (pulls a stratified-by-corpus-size sample of historical BLENDED turns)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DB_URL = os.environ.get(
    "CHAT_RAG_DATABASE_URL_RAW",
    "postgresql://postgres:MobiusDev123$@127.0.0.1:5433/mobius_chat",
)

# Composed system prompts (intro + schema v7 + mode-rules), pinned to schema
# v7 for both sides -- v8 is an unrelated RECITAL-only fix shipped the same
# day and must not leak into this A/B. See the DB for exact provenance:
# module.enricher v1, enricher.answercard_schema_and_rules v7,
# module.enricher.{factual,blended,answer} v1.
_INTRO = (
    "You are the ENRICHER for a retrieval-based Q&A system.\n\n"
    "The user has ALREADY seen react_draft (provided in the input JSON). Your job is NOT to "
    "restate or rephrase it. Your job is to:\n"
    "  1. Correct any factual error in the draft (if sources contradict it)\n"
    "  2. Pull verbatim evidence from source_texts to back up key claims\n"
    "  3. Distill what matters into takeaways\n"
    "  4. Specify concrete next actions\n"
    "  5. Flag what the sources did not cover"
)

_FACTUAL_RULES = (
    "Mode-specific rules for FACTUAL:\n"
    "- Set mode = 'FACTUAL'.\n"
    "- sections: 2–3 sections, each with 3–6 substantive bullets (8–25 words each). "
    "Intent must be one of: process, requirements, definitions, exceptions, references. "
    "No stub bullets. FACTUAL hides sections behind 'Show details' — they must carry real detail.\n"
    "- direct_answer is ONE sentence — the single operative fact.\n"
    "- required_variables: if the answer depends on an unknown (service code, plan subtype), "
    "list it. Add one followup question only if the user must clarify to get a definitive answer."
)
_BLENDED_RULES = (
    "Mode-specific rules for BLENDED:\n"
    "- Set mode = 'BLENDED'.\n"
    "- sections: 2–4 sections. requirements + definitions sections are visible by default; "
    "process, exceptions, references are behind 'Show details'.\n"
    "- direct_answer: 1–3 sentences. Include specifics inline when sources supply them (codes, "
    "numbers, criteria names, page refs). Do not retreat to framework-name-only one-liners.\n"
    "- Provide concrete criteria as bullets in 'requirements'; code definitions in 'definitions'."
)
_ANSWER_RULES = (
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

VARIANTS = {
    "before_factual": _FACTUAL_RULES,
    "before_blended": _BLENDED_RULES,
    "after_unified": _ANSWER_RULES,
}


async def _fetch_schema_v7(conn) -> str:
    row = await conn.fetchrow(
        "select template_body from prompt_blocks where block_key=$1 and version=7",
        "enricher.answercard_schema_and_rules",
    )
    return row["template_body"]


def _composed_system(schema_v7: str, mode_rules: str) -> str:
    return "\n\n".join([_INTRO, schema_v7, mode_rules])


async def _fetch_row(conn, correlation_id: str) -> dict | None:
    row = await conn.fetchrow(
        "select correlation_id, question, plan_snapshot, sources, created_at "
        "from chat_turns where correlation_id=$1",
        correlation_id,
    )
    if not row:
        return None
    draft_row = await conn.fetchrow(
        "select event_data from chat_progress_events "
        "where correlation_id=$1 and event_type='draft_ready' "
        "order by created_at asc limit 1",
        correlation_id,
    )
    react_draft = None
    if draft_row and draft_row["event_data"]:
        _ed = draft_row["event_data"]
        _ed = json.loads(_ed) if isinstance(_ed, str) else _ed
        react_draft = (_ed or {}).get("text")
    return {
        "correlation_id": row["correlation_id"],
        "question": row["question"],
        "plan_snapshot": json.loads(row["plan_snapshot"]) if isinstance(row["plan_snapshot"], str) else row["plan_snapshot"],
        "sources": json.loads(row["sources"]) if isinstance(row["sources"], str) else (row["sources"] or []),
        "created_at": str(row["created_at"]),
        "react_draft": react_draft,
    }


def _build_inputs(row: dict):
    from app.planner.schemas import Plan

    plan_snapshot = row["plan_snapshot"] or {"subquestions": []}
    plan = Plan.model_validate(plan_snapshot)
    stub_answers = ["" for _ in plan.subquestions]  # not persisted per-sq; react_draft carries content

    sources = row["sources"] or []
    source_texts = []
    seen_docs = {}
    for s in sources:
        if not isinstance(s, dict):
            continue
        text = s.get("cite_text") or s.get("text") or ""
        if not text.strip():
            continue
        source_texts.append({
            "text": text,
            "document_id": s.get("document_id"),
            "document_name": s.get("document_name"),
            "page_number": s.get("page_number"),
        })
        doc_key = s.get("document_id") or s.get("document_name")
        if doc_key and doc_key not in seen_docs:
            seen_docs[doc_key] = {
                "title": s.get("document_name"),
                "url": s.get("url"),
                "document_id": s.get("document_id"),
            }
    sources_summary = list(seen_docs.values())
    return plan, stub_answers, source_texts, sources_summary


def _corpus_bucket(row: dict) -> str:
    """thin | thick, by total char count of persisted source cite_text -- the
    stratification axis Eval asked for (thin-corpus turns stress the new
    3-6-bullet floor and the direct_answer terseness watch-point hardest)."""
    total = sum(len((s.get("cite_text") or s.get("text") or "")) for s in (row["sources"] or []) if isinstance(s, dict))
    return "thin" if total < 1500 else "thick"


async def _run_pair(conn, cfg, row: dict, before_variant: str) -> dict:
    from app.responder.final import _build_consolidator_input_json
    from app.services.llm_manager import generate_sync

    schema_v7 = await _fetch_schema_v7(conn)
    plan, stub_answers, source_texts, sources_summary = _build_inputs(row)

    consolidator_input_json = _build_consolidator_input_json(
        plan, stub_answers, row["question"] or "",
        sources_summary=sources_summary or None,
        react_draft=row.get("react_draft"),
        source_texts=source_texts or None,
    )
    prompt_user = cfg.prompts.integrator_user_template.format(
        consolidator_input_json=consolidator_input_json,
    )

    outputs = {}
    for label, rules in ((before_variant, VARIANTS[before_variant]), ("after_unified", VARIANTS["after_unified"])):
        system = _composed_system(schema_v7, rules)
        prompt = f"{system}\n\n{prompt_user}"
        text, usage = generate_sync(
            prompt,
            stage="integrator",
            max_tokens=4096,
            correlation_id=f"{row['correlation_id']}-ab-{label}",
        )
        outputs[label] = {"raw_output": text, "usage": usage}

    return {
        "correlation_id": row["correlation_id"],
        "question": row["question"],
        "corpus_bucket": _corpus_bucket(row),
        "n_sources": len(source_texts),
        "react_draft_present": bool(row.get("react_draft")),
        "before_variant": before_variant,
        "outputs": outputs,
    }


async def _pull_sample(conn, n: int) -> list[str]:
    """Stratified pull: half thin-corpus, half thick-corpus, from historical
    BLENDED turns that also have a draft_ready event on file (real react_draft
    fidelity) -- last 30 days, where draft_ready retention is reliable."""
    rows = await conn.fetch(
        """
        select t.correlation_id, t.sources
        from chat_turns t
        where t.final_message like '%"mode": "BLENDED"%'
          and t.created_at < '2026-08-07 13:18:00+00'
          and t.created_at >= '2026-08-07 13:18:00+00'::timestamptz - interval '30 days'
          and t.question is not null and t.question != ''
          and exists (
              select 1 from chat_progress_events e
              where e.correlation_id = t.correlation_id and e.event_type = 'draft_ready'
          )
        order by random()
        limit 400
        """
    )
    thin, thick = [], []
    for r in rows:
        sources = json.loads(r["sources"]) if isinstance(r["sources"], str) else (r["sources"] or [])
        total = sum(len((s.get("cite_text") or s.get("text") or "")) for s in sources if isinstance(s, dict))
        (thin if total < 1500 else thick).append(r["correlation_id"])
    half = n // 2
    picked = thin[:half] + thick[:n - half]
    return picked


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ids", help="comma-separated correlation_ids")
    ap.add_argument("--sample", type=int, help="stratified sample size to pull automatically")
    ap.add_argument("--before-variant", default="before_blended", choices=["before_blended", "before_factual"])
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    import asyncpg
    from app.chat_config import get_chat_config

    conn = await asyncpg.connect(DB_URL)
    cfg = get_chat_config()

    if args.ids:
        ids = [i.strip() for i in args.ids.split(",") if i.strip()]
    elif args.sample:
        ids = await _pull_sample(conn, args.sample)
        print(f"stratified sample: {len(ids)} correlation_ids", file=sys.stderr)
    else:
        raise SystemExit("pass --ids or --sample")

    results = []
    for i, cid in enumerate(ids, 1):
        row = await _fetch_row(conn, cid)
        if not row:
            print(f"[{i}/{len(ids)}] {cid}: not found, skipping", file=sys.stderr)
            continue
        try:
            pair = await _run_pair(conn, cfg, row, args.before_variant)
            results.append(pair)
            print(f"[{i}/{len(ids)}] {cid}: ok ({pair['corpus_bucket']}, {pair['n_sources']} sources)", file=sys.stderr)
        except Exception as e:
            print(f"[{i}/{len(ids)}] {cid}: FAILED — {e}", file=sys.stderr)

    with open(args.out, "w") as f:
        json.dump(results, f, indent=2)
    print(f"wrote {len(results)} pairs to {args.out}", file=sys.stderr)

    await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
