#!/usr/bin/env python3
"""v1 vs v2: the same LLM-written card, through both arms.

Drives run_integrate for real, so what is rendered is the payload the worker
publishes -- not the formatter called directly. The only difference between
the two columns is whether ctx carries v2 markers.
"""
from __future__ import annotations

import html
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from app.pipeline.context import PipelineContext          # noqa: E402
from app.planner.schemas import Plan, SubQuestion          # noqa: E402
from app.stages.integrate import run_integrate             # noqa: E402
from render_cards import PAGE, _body, _inline_md, section_has_content  # noqa: E402

DRAFT = "Sunshine Health requires initial claims within 180 days of service. " * 4
V2_MARKERS = {"coverage": [], "citations": [], "ran": {"assemble": "ok"}}

# Real LLM-integrator output shapes: the model naming its own formats.
CASES = [
 ("What are the therapy rates for Sunshine Health?",
  "The model called a label/value list “bullets”",
  [{"format": "bullets", "label": "Rates", "data": {"items": [
      {"label": "90834 — Individual therapy, 45 min", "value": "$85.00"},
      {"label": "90837 — Extended therapy, 60 min", "value": "$120.00"},
      {"label": "90853 — Group therapy", "value": "$27.40"}]}}]),

 ("What are the appeal levels and their deadlines?",
  "The model called SIX items “stats” — the renderer draws four",
  [{"format": "stats", "label": "Appeal levels", "data": {"items": [
      {"label": "Level 1 — Reconsideration", "value": "90 days"},
      {"label": "Level 2 — Formal appeal", "value": "60 days"},
      {"label": "State fair hearing", "value": "120 days"},
      {"label": "Expedited review", "value": "72 hours"},
      {"label": "Standard turnaround", "value": "30 days"},
      {"label": "COB claims", "value": "90 days"}]}}]),

 ("How do I submit a corrected claim?",
  "An ordered procedure — must survive as steps, not collapse to bullets",
  [{"format": "steps", "label": "Submission", "data": {"items": [
      {"label": "Pull the original claim number from the remittance advice."},
      {"label": "Complete the Provider Dispute Resolution Request form."},
      {"label": "Submit through the portal and keep the confirmation number."}]}}]),
]


def _publish(sections, v2: bool) -> dict:
    ctx = PipelineContext(
        correlation_id="render-compare", thread_id="t", message="q",
        plan=Plan(subquestions=[SubQuestion(id="sq1", text="q", kind="non_patient")]),
        answers=["a"], sources=[], usages=[], retrieval_signals=[])
    ctx.chat_mode = "copilot"
    ctx.react_rounds_used = 5          # past the gate: the LLM path, deliberately
    ctx.react_draft = DRAFT
    ctx.react_unfinished_reason = None
    if v2:
        ctx.v2_integration = V2_MARKERS
    card = {"mode": "FACTUAL", "direct_answer": "Here is what the manual says.",
            "sections": [dict(s) for s in sections]}
    with (
        patch.dict(os.environ, {"MOBIUS_INTEGRATOR_MODE": "parallel",
                                "MOBIUS_DYNAMIC_ENRICHMENT_PCT": "0"}),
        patch("app.stages.integrate.format_response_parallel") as parallel,
        patch("app.stages.integrate.run_bc_background"),
    ):
        parallel.return_value = (json.dumps(card), [])
        run_integrate(ctx)
    return json.loads(ctx.response_payload["message"])


_DROPPED = ('<div class="harness-dropped">every section dropped — '
            'renderOneSection returned null</div>')


def _card_html(card: dict, arm: str, sub: str) -> str:
    secs = "".join(
        '<div class="answer-card-section">'
        f'<div class="answer-card-section-label">{html.escape(s.get("label",""))}</div>'
        f"{_body(s)}</div>"
        for s in card["sections"] if section_has_content(s))
    return (f'<div class="harness-col"><div class="harness-arm">{arm}</div>'
            f'<div class="harness-note">{html.escape(sub)}</div>'
            f'<div class="answer-card">'
            f'<div class="answer-card-direct">{_inline_md(card["direct_answer"])}</div>'
            f"{secs or _DROPPED}</div></div>")


def main() -> int:
    blocks = []
    for question, note, sections in CASES:
        v1, v2 = _publish(sections, v2=False), _publish(sections, v2=True)
        blocks.append(
            f'<section class="harness-case"><div class="harness-q">{html.escape(question)}</div>'
            f'<div class="harness-note">{html.escape(note)}</div><div class="harness-pair">'
            + _card_html(v1, "v1 — control", "whatever the integrator called it")
            + _card_html(v2, "v2 — re-classified", "the ladder decides from content")
            + "</div></section>")
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "compare.html")
    page = PAGE.format(n=len(CASES), cases="".join(blocks)).replace(
        "Deterministic formatter — {n} real drafts".format(n=len(CASES)),
        "v1 vs v2 — the same LLM card through both arms")
    page = page.replace("</style>", """
.harness-case { max-width: 1180px; }
.harness-pair { display: flex; gap: 16px; align-items: flex-start; }
.harness-col { flex: 1 1 0; min-width: 0; }
.harness-arm { font-size: 11px; font-weight: 600; color: #475569; margin-bottom: 2px; }
.harness-dropped { margin-top: 10px; padding: 8px 10px; background: #fef2f2;
  border: 1px solid #fecaca; border-radius: 6px; font-size: 11.5px; color: #991b1b; }
</style>""")
    out.write_text(page)
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
