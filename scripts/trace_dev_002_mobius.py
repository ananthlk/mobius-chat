#!/usr/bin/env python3
"""Trace a dev question through full Mobius path: extract → rerank → decay (per-category) → blend → assemble → LLM.

Run (from Mobius root):
  PYTHONPATH=mobius-chat python mobius-chat/scripts/trace_dev_002_mobius.py --inline
  PYTHONPATH=mobius-chat python mobius-chat/scripts/trace_dev_002_mobius.py --inline --id dev_004
  PYTHONPATH=mobius-chat python mobius-chat/scripts/trace_dev_002_mobius.py --list
  PYTHONPATH=mobius-chat python mobius-chat/scripts/trace_dev_002_mobius.py --inline --all

Uses eval_questions_dev.yaml (9 dev questions). Default: dev_002.
Shows every variable: extract, merge, rerank, per-category decay (thresholds, top_score, kept/dropped),
blend selection (sentence vs paragraph pools), assemble, LLM response.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import yaml
from pathlib import Path

CHAT_ROOT = Path(__file__).resolve().parent.parent
if str(CHAT_ROOT) not in sys.path:
    sys.path.insert(0, str(CHAT_ROOT))

_root = CHAT_ROOT.parent
for env_path in (CHAT_ROOT / ".env", _root / "mobius-config" / ".env", _root / ".env"):
    if env_path.exists():
        try:
            from dotenv import load_dotenv
            load_dotenv(env_path, override=False)
        except Exception:
            pass
        break

QUESTIONS_PATH = _root / "mobius-retriever" / "eval_questions_dev.yaml"


def _load_questions() -> list[dict]:
    """Load questions from eval_questions_dev.yaml."""
    if not QUESTIONS_PATH.exists():
        return []
    with open(QUESTIONS_PATH) as f:
        data = yaml.safe_load(f) or {}
    return data.get("questions") or []


def _get_question_by_id(qid: str) -> tuple[str, str] | None:
    """Return (question_text, qid) for the given id, or None if not found."""
    for q in _load_questions():
        if q.get("id") == qid:
            return (q.get("question") or "", qid)
    return None


def _truncate(t: str, n: int = 70) -> str:
    t = (t or "").strip().replace("\n", " ")
    return (t[: n - 3] + "...") if len(t) > n else t


def _print_trace(trace: dict) -> None:
    """Pretty-print trace with every variable."""
    print("\n" + "=" * 80)
    print("TRACE: EXTRACT")
    print("=" * 80)
    ex = trace.get("extract") or {}
    print(f"  bm25_raw_n: {ex.get('bm25_raw_n')}")
    print(f"  vector_raw_n: {ex.get('vector_raw_n')}")
    print(f"  vector_abstention_cutoff: {ex.get('vector_abstention_cutoff')}")
    print(f"  vector_filtered_n: {ex.get('vector_filtered_n')}")
    print(f"  merged_n: {ex.get('merged_n')}")
    if ex.get("bm25_chunks"):
        print("\n  BM25 chunks (top 8):")
        for i, c in enumerate(ex["bm25_chunks"][:8], 1):
            print(f"    {i}. provision_type={c.get('provision_type')} raw_score={c.get('raw_score')}")
            print(f"       snippet: {c.get('snippet', '')}")
    if ex.get("vector_chunks"):
        print("\n  Vector chunks (top 5):")
        for i, c in enumerate(ex["vector_chunks"][:5], 1):
            print(f"    {i}. similarity={c.get('similarity')}")
            print(f"       snippet: {c.get('snippet', '')}")

    print("\n" + "=" * 80)
    print("TRACE: MERGE (dedupe by id)")
    print("=" * 80)
    mrg = trace.get("merge") or {}
    print(f"  n_added_bm25: {mrg.get('n_added_bm25')}")
    print(f"  n_skipped_bm25: {mrg.get('n_skipped_bm25')}")
    print(f"  n_added_vector: {mrg.get('n_added_vector')}")
    print(f"  n_skipped_vector: {mrg.get('n_skipped_vector')}")
    if mrg.get("bm25_processed"):
        print("\n  BM25 processed (id, provision_type, retrieval_source, action):")
        for i, p in enumerate(mrg["bm25_processed"][:15], 1):
            print(f"    {i}. id={p.get('id')!r} pt={p.get('provision_type')} src={p.get('retrieval_source')} action={p.get('action')}")
    if mrg.get("merged_ids_by_source"):
        print("\n  merged_ids_by_source:")
        for src, ids in mrg["merged_ids_by_source"].items():
            print(f"    {src}: {ids[:8]}{'...' if len(ids) > 8 else ''}")

    print("\n" + "=" * 80)
    print("TRACE: RERANK")
    print("=" * 80)
    rr = trace.get("rerank") or {}
    print(f"  n_chunks_input: {rr.get('n_chunks_input')}")
    print(f"  signal_names: {rr.get('signal_names')}")
    print(f"  by_category_keys: {rr.get('by_category_keys')}")
    print(f"  n_chunks_after_decay: {rr.get('n_chunks_after_decay')}")
    print(f"  post_rerank_decay_threshold (fallback): {rr.get('post_rerank_decay_threshold')}")
    print(f"  post_rerank_decay_by_category: {rr.get('post_rerank_decay_by_category')}")
    if rr.get("chunks_before_decay"):
        print("\n  Chunks before decay (id, retrieval_source, rerank_score):")
        for i, c in enumerate(rr["chunks_before_decay"][:12], 1):
            print(f"    {i}. id={c.get('id')!r} src={c.get('retrieval_source')} rerank={c.get('rerank_score')}")

    print("\n" + "=" * 80)
    print("TRACE: PER-CATEGORY DECAY (each category has own threshold and top_score)")
    print("=" * 80)
    for dc in trace.get("decay_per_category") or []:
        cat = dc.get("category", "?")
        print(f"\n  Category: {cat}")
        print(f"    n_before: {dc.get('n_before')}")
        print(f"    top_score_in_category: {dc.get('top_score_in_category')}")
        print(f"    threshold: {dc.get('threshold')}")
        print(f"    n_after (kept): {dc.get('n_after')}")
        print(f"    Chunks before decay:")
        for i, c in enumerate((dc.get("chunks_before") or [])[:6], 1):
            print(f"      {i}. rerank_score={c.get('rerank_score')} | {_truncate(c.get('snippet', ''), 50)}")
        print(f"    Chunks kept (decay_ratio >= threshold):")
        for i, c in enumerate(dc.get("chunks_kept") or [], 1):
            print(f"      {i}. rerank_score={c.get('rerank_score')} decay_ratio={c.get('decay_ratio')}")

    print("\n" + "=" * 80)
    print("TRACE: BLEND SELECTION")
    print("=" * 80)
    bl = trace.get("blend_selection") or {}
    print(f"  chunks_input_n: {bl.get('chunks_input_n')}")
    print(f"  chunks_by_retrieval_source: {bl.get('chunks_by_retrieval_source')}")
    print(f"  n_factual: {bl.get('n_factual')} (sentence-level = BM25 sentence only)")
    print(f"  n_hierarchical: {bl.get('n_hierarchical')} (paragraph-level = BM25 paragraph + vector)")
    print(f"  n_sentence_level_pool: {bl.get('n_sentence_level_pool')}")
    print(f"  n_paragraph_level_pool: {bl.get('n_paragraph_level_pool')}")
    print(f"  n_output: {bl.get('n_output')}")
    if bl.get("sentence_level_chunks"):
        print("\n  Sentence-level pool (BM25 sentence, sorted by rerank desc):")
        for i, c in enumerate(bl["sentence_level_chunks"][:8], 1):
            print(f"    {i}. {c.get('retrieval_source')} rerank={c.get('rerank_score')} | {_truncate(c.get('snippet', ''), 55)}")
    if bl.get("top_sentence_selected"):
        print("\n  Top sentence selected (sent to LLM):")
        for i, c in enumerate(bl["top_sentence_selected"], 1):
            print(f"    {i}. rerank={c.get('rerank_score')} | {_truncate(c.get('snippet', ''), 55)}")
    if bl.get("top_paragraph_selected"):
        print("\n  Top paragraph selected:")
        for i, c in enumerate(bl["top_paragraph_selected"], 1):
            print(f"    {i}. rerank={c.get('rerank_score')} | {_truncate(c.get('snippet', ''), 55)}")


def _run_pipeline_with_trace(question: str) -> tuple[list[dict], dict]:
    """Run run_rag_pipeline with trace; return (assembled_docs, trace)."""
    from mobius_retriever.pipeline import run_rag_pipeline

    trace: dict = {}
    cfg_path = _root / "mobius-retriever" / "configs" / "path_b_v1.yaml"
    if not cfg_path.exists():
        cfg_path = _root / "configs" / "path_b_v1.yaml"
    cfg_path = str(cfg_path)

    docs = run_rag_pipeline(
        question=question,
        path="mobius",
        config_path=cfg_path,
        top_k=15,
        apply_google_fallback=True,
        google_search_url=os.environ.get("CHAT_SKILLS_GOOGLE_SEARCH_URL", "").strip() or None,
        n_factual=10,
        n_hierarchical=0,
        trace=trace,
    )
    return docs, trace


def _run_one_question(question: str, qid: str) -> tuple[list[dict], str]:
    """Run pipeline + LLM for one question. Returns (assembled_docs, llm_answer, trace or None)."""
    from app.chat_config import get_chat_config
    from app.services.llm_provider import get_llm_provider

    assembled, trace = _run_pipeline_with_trace(question)
    n_corpus = sum(1 for c in assembled if (c.get("rerank_score") or 0) > 0)
    n_google = len(assembled) - n_corpus

    cfg = get_chat_config()
    context_parts = [f"[{i+1}] {c.get('text', '')}" for i, c in enumerate(assembled) if c.get("text")]
    context = "\n\n".join(context_parts) if context_parts else "(No context.)"
    template = cfg.prompts.rag_answering_user_template
    prompt = template.format(context=context, question=question)

    try:
        provider = get_llm_provider()
        answer, _ = asyncio.run(provider.generate_with_usage(prompt))
        return assembled, answer.strip()
    except Exception as e:
        return assembled, f"(LLM failed: {e})"


def main() -> int:
    import argparse
    p = argparse.ArgumentParser(description="Trace a dev question through Mobius path")
    p.add_argument("--inline", action="store_true", help="Run pipeline inline with full trace; ignore RAG_API_URL")
    p.add_argument("--id", default="dev_002", help="Question id from eval_questions_dev.yaml (e.g. dev_001 .. dev_009)")
    p.add_argument("--list", action="store_true", help="List all 9 dev questions and exit")
    p.add_argument("--all", action="store_true", help="Run all 9 questions and print compact summary (requires --inline)")
    args = p.parse_args()

    if args.all:
        db_url = os.environ.get("CHAT_RAG_DATABASE_URL", "").strip()
        if not db_url:
            print("CHAT_RAG_DATABASE_URL not set.")
            return 1
        questions = _load_questions()
        if not questions:
            print("No questions found in eval_questions_dev.yaml")
            return 1
        print("=" * 80)
        print("TRACE: All 9 dev questions — Mobius path")
        print("=" * 80)
        results: list[dict] = []
        for q in questions:
            qid = q.get("id", "?")
            question = q.get("question") or ""
            expect = q.get("expect_in_manual", True)
            print(f"\n--- {qid} ---")
            print(f"Q: {question}")
            assembled, answer = _run_one_question(question, qid)
            n_corpus = sum(1 for c in assembled if (c.get("rerank_score") or 0) > 0)
            n_google = len(assembled) - n_corpus
            print(f"Assembled: {len(assembled)} total ({n_corpus} corpus, {n_google} Google)")
            print(f"A: {_truncate(answer, 500)}")
            results.append({"qid": qid, "question": question, "expect_in_manual": expect, "n": len(assembled), "n_corpus": n_corpus, "n_google": n_google, "answer": answer})
        print("\n" + "=" * 80)
        print("SUMMARY")
        print("=" * 80)
        for r in results:
            print(f"\n{r['qid']} (expect_in_manual={r['expect_in_manual']})")
            print(f"  n_assembled={r['n']} (corpus={r['n_corpus']}, google={r['n_google']})")
            print(f"  Answer: {_truncate(r['answer'], 300)}")
        return 0

    if args.list:
        questions = _load_questions()
        print("Dev questions (eval_questions_dev.yaml):")
        for q in questions:
            qid = q.get("id", "?")
            expect = q.get("expect_in_manual", True)
            qtext = _truncate(q.get("question") or "", 70)
            print(f"  {qid}: {qtext}")
            print(f"       expect_in_manual={expect}")
        return 0

    resolved = _get_question_by_id(args.id)
    if not resolved:
        print(f"Question id {args.id!r} not found. Use --list to see available ids.")
        return 1
    question, qid = resolved

    print("=" * 80)
    print(f"TRACE: {qid} — Mobius path")
    print("=" * 80)
    print(f"\nQuestion: {question}\n")

    rag_api_url = (os.environ.get("RAG_API_URL") or "").strip() if not args.inline else ""
    assembled: list[dict] = []
    trace: dict = {}

    if rag_api_url:
        print("Using RAG API (RAG_API_URL set). No pipeline trace available.")
        import urllib.request
        payload = json.dumps({
            "question": question,
            "path": "mobius",
            "top_k": 15,
            "apply_google": True,
            "n_factual": 10,
            "n_hierarchical": 0,
        }).encode("utf-8")
        req = urllib.request.Request(
            f"{rag_api_url.rstrip('/')}/retrieve",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=90) as resp:
            data = json.loads(resp.read().decode())
        assembled = data.get("docs") or []
    else:
        db_url = os.environ.get("CHAT_RAG_DATABASE_URL", "").strip()
        if not db_url:
            print("CHAT_RAG_DATABASE_URL not set.")
            return 1
        assembled, trace = _run_pipeline_with_trace(question)
        _print_trace(trace)

    print("\n" + "=" * 80)
    print("ASSEMBLED DOCS SENT TO LLM")
    print("=" * 80)
    for i, c in enumerate(assembled[:12], 1):
        label = c.get("confidence_label", "?")
        rs = c.get("rerank_score")
        src = c.get("retrieval_source", "")
        txt = _truncate(c.get("text") or "", 60)
        rs_str = f"{rs:.3f}" if rs is not None else "0.000"
        print(f"  {i}. {label:22} rerank={rs_str} {src:14} | {txt}")

    print("\n" + "=" * 80)
    print("STEP: LLM RESPONSE")
    print("=" * 80)

    from app.chat_config import get_chat_config
    from app.services.llm_provider import get_llm_provider

    cfg = get_chat_config()
    context_parts = [f"[{i+1}] {c.get('text', '')}" for i, c in enumerate(assembled) if c.get("text")]
    context = "\n\n".join(context_parts) if context_parts else "(No context.)"
    template = cfg.prompts.rag_answering_user_template
    prompt = template.format(context=context, question=question)

    print("\nCalling LLM...")
    try:
        provider = get_llm_provider()
        answer, usage = asyncio.run(provider.generate_with_usage(prompt))
        print("\n--- LLM Answer ---")
        print(answer.strip())
        if usage:
            print(f"\nUsage: {usage.get('input_tokens', '?')} in / {usage.get('output_tokens', '?')} out")
    except Exception as e:
        print(f"LLM failed: {e}")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
