#!/usr/bin/env python3
"""Run 9 dev questions through Chat pipeline (parse → retrieve → doc assembly) and report per question.

Output: factual vs canonical score, retrieval blend, what chunks were sent, confidence labels, Google fallback.
Uses eval_questions_dev.yaml from mobius-retriever.

Run (from Mobius root):
  PYTHONPATH=mobius-chat python mobius-chat/scripts/nine_question_chat_report.py
  PYTHONPATH=mobius-chat python mobius-chat/scripts/nine_question_chat_report.py --bm25

  --bm25  Use BM25 path (retrieve → rerank → assemble) instead of Vertex vector (blend).
          Only needs CHAT_RAG_DATABASE_URL; no Vertex env required.

Requirements: CHAT_RAG_DATABASE_URL. For vector: Vertex env. For Google fallback when corpus returns 0:
  CHAT_SKILLS_GOOGLE_SEARCH_URL=http://localhost:8004/search?  (and run mobius-skills/google-search)
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# Add mobius-chat to path
CHAT_ROOT = Path(__file__).resolve().parent.parent
if str(CHAT_ROOT) not in sys.path:
    sys.path.insert(0, str(CHAT_ROOT))

# Load .env
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
from app.planner.parser import parse
from app.responder.final import blended_canonical_score
from app.services.retrieval_calibration import get_retrieval_blend
from app.services.doc_assembly import assemble_docs


def _truncate(text: str, max_len: int = 80) -> str:
    if not text:
        return ""
    t = (text or "").strip()
    return t[: max_len - 3] + "..." if len(t) > max_len else t


def main() -> int:
    parser = argparse.ArgumentParser(description="Run 9 dev questions through Chat pipeline.")
    parser.add_argument("--bm25", action="store_true", help="Use BM25 path (retrieve → rerank → assemble)")
    args = parser.parse_args()
    use_bm25 = args.bm25

    questions_path = CHAT_ROOT.parent / "mobius-retriever" / "eval_questions_dev.yaml"
    report_name = "dev_chat_pipeline_report_bm25.md" if use_bm25 else "dev_chat_pipeline_report.md"
    out_path = CHAT_ROOT.parent / "mobius-retriever" / "reports" / report_name

    if not questions_path.exists():
        print(f"Questions not found: {questions_path}", file=sys.stderr)
        return 1

    with open(questions_path) as f:
        data = yaml.safe_load(f) or {}
    questions = data.get("questions") or []

    from app.chat_config import get_chat_config
    cfg = get_chat_config()
    rag = cfg.rag
    if use_bm25:
        has_rag = bool(rag.database_url)
    else:
        has_rag = bool(rag.vertex_index_endpoint_id and rag.vertex_deployed_index_id and rag.database_url)
    has_google = bool(os.environ.get("CHAT_SKILLS_GOOGLE_SEARCH_URL", "").strip())

    out_path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    summary_rows: list[dict] = []

    lines.append("# Dev Chat Pipeline Report (9 Questions)")
    lines.append("")
    if use_bm25:
        lines.append("Flow: parse → retrieval (BM25, retrieve → rerank → assemble) → doc assembly (confidence, Google fallback).")
    else:
        lines.append("Flow: parse → retrieval (blend) → doc assembly (confidence, Google fallback).")
    lines.append("")
    lines.append(f"RAG configured: {has_rag}")
    lines.append(f"Retrieval backend: {'BM25 (retrieve→rerank→assemble)' if use_bm25 else 'Vertex vector (blend)'}")
    lines.append(f"Google search URL: {'set' if has_google else 'not set'}")
    lines.append("")
    lines.append("---")
    lines.append("")

    for q in questions:
        qid = q.get("id", "?")
        question = q.get("question", "")
        expect_in_manual = q.get("expect_in_manual", True)

        lines.append(f"## {qid}")
        lines.append("")
        lines.append(f"**Question:** {question}")
        lines.append("")
        lines.append(f"**expect_in_manual:** {expect_in_manual}")
        lines.append("")

        # Parse
        plan = parse(question)
        sqs = [sq for sq in plan.subquestions if sq.kind == "non_patient"]
        if not sqs:
            lines.append("No non_patient subquestions.")
            lines.append("")
            lines.append("---")
            lines.append("")
            continue

        sq = sqs[0]
        canonical = blended_canonical_score(plan)
        factual = 1.0 - canonical  # factual tendency: 0=canonical, 1=factual
        intent_score = getattr(sq, "intent_score", None)
        intent = getattr(sq, "question_intent", None) or "—"

        blend = get_retrieval_blend(intent_score if intent_score is not None else 0.5)
        n_h = blend["n_hierarchical"]
        n_f = blend["n_factual"]
        conf_min = blend["confidence_min"]
        use_blend = (n_h > 0 or n_f > 0) and not (n_h == 0 and n_f == 0)

        lines.append("### Factual vs Canonical")
        lines.append(f"| Score | Value |")
        lines.append(f"|-------|-------|")
        lines.append(f"| **canonical_score** | {canonical:.2f} |")
        lines.append(f"| **factual_score**   | {factual:.2f} |")
        lines.append(f"| intent_score (from parser) | {intent_score if intent_score is not None else '—'} |")
        lines.append(f"| question_intent | {intent} |")
        lines.append("")

        lines.append("### Retrieval Blend")
        lines.append(f"n_hierarchical={n_h}  n_factual={n_f}  confidence_min={conf_min}")
        lines.append("")

        # Retrieve
        emitted: list[str] = []
        def emit(msg: str) -> None:
            emitted.append(msg.strip() if msg.strip() else "")

        chunks: list[dict] = []
        if has_rag:
            try:
                if use_bm25:
                    from app.services.retriever_backend import retrieve_for_chat
                    k = cfg.rag.top_k
                    total_k = max(k, n_h + n_f) if (n_h > 0 or n_f > 0) else k
                    chunks, _ = retrieve_for_chat(
                        sq.text,
                        top_k=total_k,
                        database_url=rag.database_url or "",
                        filter_payer=rag.filter_payer or "",
                        filter_state=rag.filter_state or "",
                        filter_program=rag.filter_program or "",
                        filter_authority_level=rag.filter_authority_level or "",
                        n_factual=n_f,
                        n_hierarchical=n_h,
                        emitter=emit,
                    )
                    if conf_min is not None and chunks:
                        chunks = [c for c in chunks if (c.get("match_score") or c.get("rerank_score") or c.get("confidence") or 0.0) >= conf_min]
                else:
                    from app.services.published_rag_search import retrieve_with_blend, search_published_rag
                    use_blend = (n_h > 0 or n_f > 0) and not (n_h == 0 and n_f == 0)
                    if use_blend:
                        chunks = retrieve_with_blend(
                            sq.text,
                            n_hierarchical=n_h,
                            n_factual=n_f,
                            confidence_min=conf_min,
                            emitter=emit,
                        )
                    else:
                        k = cfg.rag.top_k
                        chunks = search_published_rag(
                            sq.text,
                            k=k,
                            confidence_min=conf_min,
                            emitter=emit,
                        )
            except Exception as e:
                lines.append(f"**Retrieval error:** {e}")
                lines.append("")
        else:
            lines.append("RAG not configured; no retrieval.")
        lines.append("")

        pre_count = len(chunks)

        # Doc assembly (runs even when chunks=[] to trigger Google fallback)
        assembly_emitted: list[str] = []
        def assembly_emit(msg: str) -> None:
            if msg.strip():
                assembly_emitted.append(msg.strip())

        if True:  # always run assembly (Google fallback when 0 chunks)
            try:
                chunks = assemble_docs(
                    chunks,
                    sq.text,
                    apply_google=True,
                    expand_neighbors=False,
                    database_url=rag.database_url if rag else None,
                    emitter=assembly_emit,
                )
            except Exception as e:
                lines.append(f"**Doc assembly error:** {e}")
                lines.append("")

        post_count = len(chunks)
        google_used = any("Google" in s or "external" in s.lower() or "Low corpus" in s for s in assembly_emitted)

        lines.append("### What Was Sent")
        lines.append(f"| Metric | Value |")
        lines.append(f"|--------|-------|")
        lines.append(f"| Chunks before assembly | {pre_count} |")
        lines.append(f"| Chunks after assembly (to LLM) | {post_count} |")
        lines.append(f"| Google fallback used | {'yes' if google_used else 'no'} |")
        lines.append("")

        if assembly_emitted:
            lines.append("**Assembly messages:**")
            for m in assembly_emitted:
                lines.append(f"- {m}")
            lines.append("")

        # Top chunks with confidence
        if chunks:
            lines.append("**Top chunks sent (with confidence_label):**")
            lines.append("| # | confidence_label | rerank_score | Snippet |")
            lines.append("|---|------------------|--------------|---------|")
            for i, c in enumerate(chunks[:5], 1):
                label = c.get("confidence_label") or "—"
                rs = c.get("rerank_score")
                rs_s = f"{rs:.3f}" if rs is not None else "—"
                snippet = _truncate(c.get("text") or "", 50).replace("|", "\\|")
                lines.append(f"| {i} | {label} | {rs_s} | {snippet} |")
        else:
            lines.append("*(No chunks sent to LLM)*")
        lines.append("")
        lines.append("---")
        lines.append("")

        summary_rows.append({
            "qid": qid,
            "question": _truncate(question, 45),
            "canonical": canonical,
            "factual": factual,
            "n_h": n_h,
            "n_f": n_f,
            "pre": pre_count,
            "post": post_count,
            "google": "✓" if google_used else "—",
            "top_label": chunks[0].get("confidence_label") if chunks else "—",
        })

    # Prepend summary table after intro
    summary_block = [
        "",
        "## Summary: Factual vs Canonical + What Was Sent",
        "",
        "| ID | Question | Canonical | Factual | n_h | n_f | Pre | Post | Google | Top Label |",
        "|----|----------|-----------|---------|-----|-----|-----|------|--------|-----------|",
    ]
    for r in summary_rows:
        summary_block.append(
            f"| {r['qid']} | {r['question']} | {r['canonical']:.2f} | {r['factual']:.2f} | "
            f"{r['n_h']} | {r['n_f']} | {r['pre']} | {r['post']} | {r['google']} | {r['top_label']} |"
        )
    summary_block.append("")
    summary_block.append("*Canonical: 0=factual, 1=canonical. Factual: 0=canonical, 1=factual. Pre/Post = chunks before/after doc assembly.*")
    summary_block.append("")
    summary_block.append("---")
    summary_block.append("")

    # Find first ## (start of per-question) and insert summary before it
    idx = next((i for i, l in enumerate(lines) if l.strip().startswith("## ") and l.strip() != "## Summary"), len(lines))
    final = lines[:idx] + summary_block + lines[idx:]

    with open(out_path, "w") as f:
        f.write("\n".join(final))

    print(f"Report written to {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
