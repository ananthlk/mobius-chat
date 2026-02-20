#!/usr/bin/env python3
"""Run 9 dev questions through BM25 pipeline with detailed report.

Shows per-question, per-doc:
  1. Pre-ranking: BM25 raw_score, match_score, docs
  2. Rerank: effect by component (score, tag_match, authority_level, length)
  3. Assembly: confidence labels, filter_abstain, Google fallback

Run (from Mobius root):
  PYTHONPATH=mobius-chat python mobius-chat/scripts/nine_question_bm25_detailed_report.py

Requires: CHAT_RAG_DATABASE_URL, mobius-retriever installed.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

CHAT_ROOT = Path(__file__).resolve().parent.parent
if str(CHAT_ROOT) not in sys.path:
    sys.path.insert(0, str(CHAT_ROOT))

_root = CHAT_ROOT.parent
for env_path in (CHAT_ROOT / ".env", _root / ".env"):
    if env_path.exists():
        try:
            from dotenv import load_dotenv
            load_dotenv(env_path, override=False)
        except Exception:
            pass
        break

import yaml


def _truncate(text: str, max_len: int = 60) -> str:
    if not text:
        return ""
    t = (text or "").strip().replace("\n", " ")
    return t[: max_len - 3] + "..." if len(t) > max_len else t


def _bm25_to_rerank_dict(c: dict, bm25_cfg: dict | None) -> dict:
    """Convert BM25 chunk to reranker input."""
    raw = c.get("raw_score")
    pt = c.get("provision_type", "sentence")
    if raw is not None and bm25_cfg:
        from mobius_retriever.config import apply_normalize_bm25
        sim = apply_normalize_bm25(float(raw), pt, bm25_cfg)
    elif raw is not None:
        sim = min(1.0, float(raw) / 50.0)
    else:
        sim = c.get("similarity") or 0.0
    return {
        "id": c.get("id"),
        "text": c.get("text") or "",
        "document_id": c.get("document_id"),
        "document_name": c.get("document_name") or "document",
        "document_authority_level": c.get("document_authority_level"),
        "page_number": c.get("page_number"),
        "similarity": sim,
        "raw_score": raw,
        "provision_type": pt,
        "source_type": c.get("source_type", "hierarchical"),
    }


def main() -> int:
    questions_path = CHAT_ROOT.parent / "mobius-retriever" / "eval_questions_dev.yaml"
    out_path = CHAT_ROOT.parent / "mobius-retriever" / "reports" / "dev_chat_pipeline_report_bm25_detailed.md"

    if not questions_path.exists():
        print(f"Questions not found: {questions_path}", file=sys.stderr)
        return 1

    with open(questions_path) as f:
        data = yaml.safe_load(f) or {}
    questions = data.get("questions") or []

    from app.chat_config import get_chat_config
    from app.services.doc_assembly import (
        DocAssemblyConfig,
        assign_confidence_batch,
        filter_abstain,
        best_score,
        google_search_via_skills_api,
    )
    from mobius_retriever.retriever import retrieve_bm25
    from mobius_retriever.config import load_bm25_sigmoid_config, load_reranker_config, apply_normalize_bm25
    from mobius_retriever.reranker import rerank_with_config_verbose
    from mobius_retriever.jpd_tagger import (
        tag_question_and_resolve_document_ids,
        fetch_document_tags_by_ids,
        fetch_line_tags_for_chunks,
    )

    cfg = get_chat_config()
    rag = cfg.rag
    database_url = rag.database_url or ""
    if not database_url:
        print("CHAT_RAG_DATABASE_URL not set", file=sys.stderr)
        return 1

    bm25_cfg = load_bm25_sigmoid_config()
    reranker_cfg = None
    try:
        reranker_cfg = load_reranker_config("configs/reranker_v1.yaml")
    except FileNotFoundError:
        pass

    out_path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    assembly_cfg = DocAssemblyConfig()

    lines.append("# BM25 Pipeline Report (9 Questions) — Detailed")
    lines.append("")
    lines.append("Flow: Pre-ranking (BM25) → Rerank (by component) → Assembly (confidence, Google).")
    lines.append("")
    summary_rows: list[dict] = []

    for q in questions:
        qid = q.get("id", "?")
        question = q.get("question", "")

        lines.append("---")
        lines.append("")
        lines.append(f"## {qid}: {_truncate(question, 70)}")
        lines.append("")

        # --- 1. Pre-ranking (BM25) ---
        lines.append("### 1. Pre-ranking (BM25 retrieved)")
        lines.append("")
        tag_filters: dict[str, str] = {}
        if rag.filter_payer:
            tag_filters["document_payer"] = rag.filter_payer
        if rag.filter_state:
            tag_filters["document_state"] = rag.filter_state
        if rag.filter_program:
            tag_filters["document_program"] = rag.filter_program
        result = retrieve_bm25(
            question=question,
            postgres_url=database_url,
            rag_database_url=database_url,
            authority_level=rag.filter_authority_level or None,
            tag_filters=tag_filters or None,
            top_k=20,
            use_jpd_tagger=True,
            emitter=None,
        )
        raw_chunks = result.raw

        if not raw_chunks:
            lines.append("| Rank | Chunk ID | Doc | raw_score | match_score | provision |")
            lines.append("|------|----------|-----|-----------|-------------|-----------|")
            lines.append("| *(none)* | | | | | |")
        else:
            lines.append("| Rank | Chunk ID | Doc | raw_score | match_score | provision |")
            lines.append("|------|----------|-----|-----------|-------------|-----------|")
            for i, c in enumerate(raw_chunks[:15], 1):
                cid = str(c.get("id", ""))[:16]
                doc = _truncate(c.get("document_name") or c.get("document_id") or "?", 25)
                raw = c.get("raw_score")
                pt = c.get("provision_type", "?")
                match = apply_normalize_bm25(float(raw or 0), pt, bm25_cfg) if bm25_cfg and raw is not None else (float(raw or 0) / 50.0)
                raw_s = f"{raw:.3f}" if raw is not None else "—"
                match_s = f"{match:.3f}" if match is not None else "—"
                lines.append(f"| {i} | `{cid}...` | {doc} | {raw_s} | {match_s} | {pt} |")
        lines.append("")

        # --- 2. Rerank (effect by component) ---
        lines.append("### 2. Rerank: effect by component")
        lines.append("")
        if reranker_cfg and reranker_cfg.signals and raw_chunks:
            dicts = [_bm25_to_rerank_dict(c, bm25_cfg) for c in raw_chunks]
            doc_ids = list({str(d.get("document_id", "")) for d in dicts if d.get("document_id")})
            doc_tags = fetch_document_tags_by_ids(database_url, doc_ids) if doc_ids else {}
            line_tags = fetch_line_tags_for_chunks(database_url, dicts) if dicts else {}
            jpd = tag_question_and_resolve_document_ids(question, database_url, emitter=None)
            qtags = jpd if ("tag_match" in (reranker_cfg.signals or {})) and jpd.has_tags else None
            ranked, debug = rerank_with_config_verbose(
                dicts, reranker_cfg, question_tags=qtags, doc_tags_by_id=doc_tags, line_tags_by_key=line_tags
            )

            # Before vs after rank
            lines.append("**Before rerank (id, similarity):**")
            for br in debug.get("before_rank", [])[:10]:
                lines.append(f"  {br.get('rank')}. id={str(br.get('id',''))[:16]}... sim={br.get('similarity', 0):.3f} doc={_truncate(str(br.get('doc','')), 30)}")
            lines.append("")

            lines.append("**After rerank (new order, rerank_score):**")
            for ar in debug.get("after_rank", [])[:10]:
                lines.append(f"  {ar.get('rank')}. id={str(ar.get('id',''))[:16]}... rerank={ar.get('rerank_score', 0):.3f}")
            lines.append("")

            # Per-chunk signals (top 5)
            lines.append("**Per-chunk signal contribution (top 5):**")
            weights = "  ".join(f"{n}={s.get('weight',0)}" for n, s in (debug.get("config", {}).get("signals") or {}).items())
            lines.append(f"  Weights: {weights}")
            for pc in debug.get("per_chunk", [])[:5]:
                cid = str(pc.get("id", ""))[:16]
                rs = pc.get("rerank_score", 0)
                lines.append(f"  - id={cid}... **rerank_score={rs:.4f}**")
                for sig_name, vals in (pc.get("signals") or {}).items():
                    r = vals.get("raw")
                    n = vals.get("norm")
                    w = vals.get("weight", 0)
                    lines.append(f"      {sig_name}: raw={r} norm={n} weight={w}")
            chunks_for_assembly = ranked
        else:
            lines.append("*(Reranker not run; using BM25 order)*")
            chunks_for_assembly = [_bm25_to_rerank_dict(c, bm25_cfg) for c in raw_chunks]
            for c in chunks_for_assembly:
                c["rerank_score"] = c.get("similarity") or 0.0
        lines.append("")

        # Convert to chat format for assembly
        chat_chunks = []
        for c in chunks_for_assembly:
            raw = c.get("raw_score")
            pt = c.get("provision_type", "sentence")
            match = apply_normalize_bm25(float(raw or 0), pt, bm25_cfg) if bm25_cfg and raw is not None else (c.get("similarity") or 0.0)
            chat_chunks.append({
                "id": c.get("id"),
                "text": c.get("text") or "",
                "document_id": c.get("document_id"),
                "document_name": c.get("document_name") or "document",
                "page_number": c.get("page_number"),
                "source_type": c.get("source_type", "chunk"),
                "match_score": match,
                "confidence": match,
                "rerank_score": c.get("rerank_score") or match,
            })

        # --- 3. Assembly ---
        lines.append("### 3. Assembly")
        lines.append("")
        with_conf = assign_confidence_batch(chat_chunks, assembly_cfg)
        best = best_score(with_conf)
        kept = filter_abstain(with_conf)

        lines.append("**assign_confidence (rerank_score → label):**")
        lines.append("| Chunk ID | Doc | rerank_score | confidence_label | llm_guidance |")
        lines.append("|----------|-----|--------------|------------------|--------------|")
        for c in with_conf[:10]:
            cid = str(c.get("id", ""))[:16]
            doc = _truncate(c.get("document_name") or "?", 25)
            rs = c.get("rerank_score")
            label = c.get("confidence_label", "—")
            guidance = _truncate(c.get("llm_guidance") or "", 30)
            rs_s = f"{rs:.3f}" if rs is not None else "—"
            lines.append(f"| `{cid}...` | {doc} | {rs_s} | {label} | {guidance} |")
        lines.append("")

        lines.append("**Assembly decision:**")
        if best >= assembly_cfg.confidence_process_confident_min:
            lines.append(f"  best_score={best:.3f} → corpus only (no Google)")
        elif best >= assembly_cfg.google_fallback_low_match_min:
            lines.append(f"  best_score={best:.3f} → corpus + Google complement")
        else:
            lines.append(f"  best_score={best:.3f} → low confidence → Google only")
        lines.append(f"  filter_abstain: {len(with_conf)} → {len(kept)} kept")
        lines.append("")

        # Google fallback
        if best < assembly_cfg.confidence_process_confident_min:
            if best < assembly_cfg.google_fallback_low_match_min or not kept:
                google_res = google_search_via_skills_api(question)
                lines.append(f"  Google fallback: {len(google_res)} external results added")
                for i, g in enumerate(google_res[:3], 1):
                    lines.append(f"    {i}. {_truncate(g.get('document_name') or '', 50)}")
            else:
                google_res = google_search_via_skills_api(question)
                lines.append(f"  Google complement: {len(google_res)} external results appended")
        lines.append("")

        # Final sent
        from app.services.doc_assembly import apply_google_fallback
        assembly_emitted: list[str] = []
        def _emit(m: str) -> None:
            if m.strip():
                assembly_emitted.append(m.strip())
        final = apply_google_fallback(chat_chunks, question, assembly_cfg, emitter=_emit)

        # Assembly decision label for summary
        if best >= assembly_cfg.confidence_process_confident_min:
            asm_decision = "corpus only"
        elif best >= assembly_cfg.google_fallback_low_match_min:
            asm_decision = "corpus + Google"
        else:
            asm_decision = "Google only"
        summary_rows.append({
            "qid": qid,
            "question": _truncate(question, 45),
            "pre": len(raw_chunks),
            "post_rerank": len(chunks_for_assembly),
            "kept": len(kept),
            "final": len(final),
            "best": best,
            "decision": asm_decision,
        })

        lines.append("**Final sent to LLM:**")
        lines.append("| # | confidence_label | rerank_score | Snippet |")
        lines.append("|---|------------------|--------------|---------|")
        for i, c in enumerate(final[:8], 1):
            label = c.get("confidence_label") or "—"
            rs = c.get("rerank_score")
            rs_s = f"{rs:.3f}" if rs is not None else "—"
            snippet = _truncate(c.get("text") or "", 45).replace("|", "\\|")
            lines.append(f"| {i} | {label} | {rs_s} | {snippet} |")
        lines.append("")

    # Prepend summary table
    summary_block = [
        "",
        "## Summary",
        "",
        "| ID | Question | Pre (BM25) | Post rerank | Kept | Final | best | Decision |",
        "|----|----------|------------|-------------|------|-------|------|----------|",
    ]
    for r in summary_rows:
        summary_block.append(
            f"| {r['qid']} | {r['question']} | {r['pre']} | {r['post_rerank']} | {r['kept']} | {r['final']} | {r['best']:.3f} | {r['decision']} |"
        )
    summary_block.append("")
    summary_block.append("---")
    summary_block.append("")
    lines = lines[:6] + summary_block + lines[6:]

    with open(out_path, "w") as f:
        f.write("\n".join(lines))

    print(f"Report written to {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
