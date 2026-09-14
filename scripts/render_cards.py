#!/usr/bin/env python3
"""Render real answer drafts through the deterministic formatter into the
REAL frontend DOM, so the cards can be looked at rather than asserted about.

    python3 scripts/render_cards.py <out.html>

Not a mockup: the class names and element structure are copied from
bubble.ts's _renderSectionBody / renderOneSection, and the page imports
frontend/static/styles.css unmodified. If a card looks wrong here it looks
wrong in the product, and if it looks right the only remaining gap is the
frontend's own JS.
"""
from __future__ import annotations

import html
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.responder.deterministic_format import deterministic_format  # noqa: E402
from app.responder.envelope_classifier import (  # noqa: E402
    classify_envelope,
)
from app.responder.deterministic_format import segment_draft  # noqa: E402

_MD_BOLD = re.compile(r"\*\*(.+?)\*\*")


def _inline_md(text: str) -> str:
    """bubble.ts's _inlineMd, for the bold it actually renders."""
    return _MD_BOLD.sub(r"<strong>\1</strong>", html.escape(str(text)))


def _body(sec: dict) -> str:
    fmt = sec.get("format") or "bullets"
    data = sec.get("data") or {}

    if fmt == "table" and data.get("headers") and data.get("rows"):
        head = "".join(f"<th>{_inline_md(h)}</th>" for h in data["headers"])
        rows = "".join(
            "<tr>" + "".join(f"<td>{_inline_md(c)}</td>" for c in row) + "</tr>"
            for row in data["rows"])
        return ('<div class="ac-fmt-table-scroll"><table class="ac-fmt-table">'
                f"<thead><tr>{head}</tr></thead><tbody>{rows}</tbody></table></div>")

    if fmt == "steps" and data.get("items"):
        lis = "".join(
            f'<li class="ac-fmt-step">{_inline_md(i.get("label", ""))}</li>'
            for i in data["items"])
        return f'<ol class="ac-fmt-steps">{lis}</ol>'

    if fmt == "stats" and data.get("items"):
        tiles = "".join(
            '<div class="ac-fmt-stat-tile">'
            f'<div class="ac-fmt-stat-value">{_inline_md(i.get("value",""))}</div>'
            f'<div class="ac-fmt-stat-label">{_inline_md(i.get("label",""))}</div>'
            + (f'<div class="ac-fmt-stat-note">{_inline_md(i["note"])}</div>'
               if i.get("note") else "")
            + "</div>"
            for i in data["items"][:4])
        return f'<div class="ac-fmt-stats">{tiles}</div>'

    if fmt == "bars" and data.get("items"):
        rows = "".join(
            '<div class="ac-fmt-bar-row">'
            f'<div class="ac-fmt-bar-label">{_inline_md(i.get("label",""))}</div>'
            '<div class="ac-fmt-bar-track"><div class="ac-fmt-bar-fill" '
            f'style="width:{round(min(1,max(0,i.get("weight") or 0))*100)}%"></div></div>'
            + "</div>"
            for i in data["items"])
        return f'<div class="ac-fmt-bars">{rows}</div>'

    if fmt == "conditions" and data.get("items"):
        rows = "".join(
            '<div class="ac-fmt-condition-row">'
            f'<div class="ac-fmt-condition-if">{_inline_md(i.get("condition",""))}</div>'
            f'<div class="ac-fmt-condition-then">{_inline_md(i.get("result",""))}</div>'
            "</div>"
            for i in data["items"])
        return f'<div class="ac-fmt-conditions">{rows}</div>'

    lis = "".join(f"<li>{_inline_md(b)}</li>" for b in (sec.get("bullets") or []))
    return f"<ul>{lis}</ul>"


def render_card(question: str, draft: str, note: str = "") -> str:
    card = deterministic_format(draft)
    verdicts = [classify_envelope(b) for b in segment_draft(draft).blocks]
    trace = " · ".join(f"{v.rule_id}→{v.format or 'prose'}" for v in verdicts) or "no blocks"

    secs = "".join(
        f'<div class="answer-card-section">'
        f'<div class="answer-card-section-label">{html.escape(s.get("label",""))}</div>'
        f"{_body(s)}</div>"
        for s in card["sections"])

    pres = card.get("presentation")
    pres_html = ""
    if pres:
        pres_html = (f'<div class="harness-abstain"><b>{html.escape(pres["rule_id"])}</b> — '
                     f'{html.escape(pres.get("note") or pres["why"])}</div>')

    return f"""
<section class="harness-case">
  <div class="harness-q">{html.escape(question)}</div>
  {f'<div class="harness-note">{html.escape(note)}</div>' if note else ''}
  <div class="answer-card">
    <div class="answer-card-direct">{_inline_md(card["direct_answer"])}</div>
    {secs}
    {pres_html}
  </div>
  <div class="harness-trace">{html.escape(trace)}
    &nbsp;|&nbsp; sections: {html.escape(json.dumps([s.get("format") for s in card["sections"]]))}</div>
</section>"""


PAGE = """<!doctype html><html><head><meta charset="utf-8">
<title>Deterministic formatter — real drafts, real CSS</title>
<link rel="stylesheet" href="styles.css">
<style>
body {{ margin:0; padding:24px; background:#f6f7f9; font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }}
.harness-head {{ max-width:860px; margin:0 auto 20px; }}
.harness-head h1 {{ font-size:19px; font-weight:600; margin:0 0 4px; }}
.harness-head p {{ font-size:13px; color:#555; margin:0; }}
.harness-case {{ max-width:860px; margin:0 auto 22px; }}
.harness-q {{ font-size:13px; font-weight:600; color:#1a1d21; margin-bottom:6px; }}
.harness-note {{ font-size:12px; color:#6b7280; margin-bottom:6px; }}
.answer-card {{ background:#fff; border:1px solid #e2e8f0; border-radius:10px; padding:16px; }}
.answer-card-direct {{ font-size:14px; line-height:1.65; color:#1a1d21; white-space:pre-wrap; }}
.answer-card-section {{ margin-top:14px; padding-top:12px; border-top:1px solid #eef0f3; }}
.harness-abstain {{ margin-top:12px; padding:8px 10px; background:#fff7ed; border:1px solid #fed7aa;
  border-radius:6px; font-size:12px; color:#9a3412; }}
.harness-trace {{ font-family:ui-monospace,monospace; font-size:10.5px; color:#94a3b8; margin-top:6px; }}
</style></head><body>
<div class="harness-head"><h1>Deterministic formatter — {n} real drafts</h1>
<p>Rendered through the real <code>.ac-fmt-*</code> DOM and frontend/static/styles.css. No model call.</p></div>
{cases}
</body></html>"""


def main() -> int:
    from render_corpus import CORPUS  # noqa: E402

    cases = "".join(render_card(q, d, note) for q, d, note in CORPUS)
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "cards.html")
    out.write_text(PAGE.format(n=len(CORPUS), cases=cases))
    print(f"wrote {out} ({len(CORPUS)} cards)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
