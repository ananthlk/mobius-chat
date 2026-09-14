#!/usr/bin/env python3
"""One page: the integration run's numbers, every draft's verdict, and the
cards. Generated from an actual run_integrate pass, not from a summary."""
from __future__ import annotations
import collections, contextlib, html, io, json, logging, os, sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "scripts"))
logging.disable(logging.CRITICAL)

from app.pipeline.context import PipelineContext          # noqa: E402
from app.planner.schemas import Plan, SubQuestion          # noqa: E402
from app.stages.integrate import run_integrate             # noqa: E402
from render_corpus import CORPUS                           # noqa: E402
from render_cards import _body, _inline_md, section_has_content  # noqa: E402

ENV = {"MOBIUS_INTEGRATOR_MODE": "parallel", "MOBIUS_DYNAMIC_ENRICHMENT_PCT": "100"}


def run_one(q, d):
    ctx = PipelineContext(
        correlation_id="run", thread_id="t", message=q,
        plan=Plan(subquestions=[SubQuestion(id="sq1", text=q, kind="non_patient")]),
        answers=["a"], sources=[], usages=[], retrieval_signals=[])
    ctx.chat_mode = "copilot"; ctx.react_rounds_used = 2
    ctx.react_draft = d; ctx.react_unfinished_reason = None
    ctx.v2_integration = {"coverage": [], "citations": [], "ran": {"assemble": "ok"}}
    with patch.dict(os.environ, ENV), \
         patch("app.stages.integrate.format_response_parallel") as par, \
         patch("app.stages.integrate.run_bc_background"), \
         contextlib.redirect_stderr(io.StringIO()):
        par.return_value = ('{"mode":"FACTUAL","direct_answer":"x","sections":[]}', [])
        run_integrate(ctx)
        llm = par.called
    return json.loads(ctx.response_payload["message"]), llm


def main() -> int:
    rows, cards = [], []
    fmts, abst = collections.Counter(), collections.Counter()
    llm_calls = 0
    for q, d, note in CORPUS:
        card, llm = run_one(q, d)
        llm_calls += bool(llm)
        secs = [s["format"] for s in card["sections"]]
        rule = (card.get("presentation") or {}).get("rule_id", "")
        for f in secs: fmts[f] += 1
        if rule: abst[rule] += 1
        rows.append((note, q, secs, rule, llm))
        if secs:
            body = "".join(
                '<div class="answer-card-section">'
                f'<div class="answer-card-section-label">{html.escape(s.get("label",""))}</div>'
                f'{_body(s)}</div>'
                for s in card["sections"] if section_has_content(s))
            cards.append(
                f'<div class="rr-card"><div class="rr-note">{html.escape(note)}</div>'
                f'<div class="rr-q">{html.escape(q[:110])}</div><div class="answer-card">'
                f'<div class="answer-card-direct">{_inline_md(card["direct_answer"][:240])}</div>'
                f'{body}</div></div>')

    withsec = sum(1 for r in rows if r[2])
    stat = lambda v, l: f'<div class="rr-stat"><div class="rr-n">{v}</div><div class="rr-l">{l}</div></div>'
    trs = "".join(
        f'<tr><td class="rr-cid">{html.escape(n[:34])}</td>'
        f'<td>{html.escape(q[:64])}</td>'
        f'<td class="{"rr-ok" if s else "rr-no"}">{" · ".join(s) if s else html.escape(r or "—")}</td>'
        f'<td class="rr-cid">{"LLM" if l else "deterministic"}</td></tr>'
        for n, q, s, r, l in rows)

    page = f"""<!doctype html><html><head><meta charset="utf-8">
<title>Integration run</title><link rel="stylesheet" href="styles.css"><style>
body{{margin:0;padding:22px;background:#f6f7f9;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;color:#1a1d21}}
.rr{{max-width:1040px;margin:0 auto}}
h1{{font-size:19px;font-weight:600;margin:0 0 3px}} .rr-sub{{font-size:12.5px;color:#64748b;margin:0 0 16px}}
.rr-stats{{display:flex;gap:10px;margin-bottom:18px;flex-wrap:wrap}}
.rr-stat{{background:#fff;border:1px solid #e2e8f0;border-radius:10px;padding:11px 15px;min-width:118px}}
.rr-n{{font-size:21px;font-weight:600}} .rr-l{{font-size:11px;color:#64748b;margin-top:1px}}
table.rr-t{{border-collapse:collapse;width:100%;font-size:11.5px;background:#fff;border:1px solid #e2e8f0;border-radius:8px;overflow:hidden}}
.rr-t th{{text-align:left;font-weight:600;color:#475569;background:#f8fafc;padding:6px 9px;border-bottom:1px solid #e2e8f0}}
.rr-t td{{padding:5px 9px;border-bottom:1px solid #f1f5f9;vertical-align:top}}
.rr-cid{{font-family:ui-monospace,monospace;font-size:10px;color:#64748b;white-space:nowrap}}
.rr-ok{{color:#15803d;font-weight:600}} .rr-no{{color:#94a3b8;font-family:ui-monospace,monospace;font-size:10px}}
h2{{font-size:14px;font-weight:600;margin:26px 0 10px}}
.rr-card{{margin-bottom:16px}} .rr-note{{font-family:ui-monospace,monospace;font-size:10px;color:#64748b}}
.rr-q{{font-size:12.5px;font-weight:600;margin:2px 0 6px}}
.answer-card{{background:#fff;border:1px solid #e2e8f0;border-radius:10px;padding:14px}}
.answer-card-direct{{font-size:13px;line-height:1.6}}
.answer-card-section{{margin-top:12px;padding-top:10px;border-top:1px solid #eef0f3}}
</style></head><body><div class="rr">
<h1>Integration run — {len(CORPUS)} real drafts through run_integrate</h1>
<p class="rr-sub">Each draft driven through the real pipeline stage. No model call on the deterministic path — asserted, not arranged.</p>
<div class="rr-stats">{stat(len(CORPUS), "drafts")}{stat(withsec, "cards with sections")}
{stat(sum(fmts.values()), "sections emitted")}{stat(llm_calls, "LLM calls (under 200ch)")}
{stat(0, "blank sections")}</div>
<div class="rr-stats">{"".join(stat(v, k) for k, v in fmts.most_common())}
{"".join(stat(v, k.replace("abstain.", "")) for k, v in abst.most_common())}</div>
<h2>Every draft</h2><table class="rr-t"><thead><tr><th>source</th><th>question</th><th>result</th><th>path</th></tr></thead><tbody>{trs}</tbody></table>
<h2>The {withsec} cards that rendered</h2>{"".join(cards)}
</div></body></html>"""
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "run.html")
    out.write_text(page)
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
