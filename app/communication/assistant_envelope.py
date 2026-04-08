"""assistant_envelope v1: ordered UI blocks for chat turns (server is arbiter, LLM may suggest ui_blocks)."""

from __future__ import annotations

import logging
import os
import re
from typing import Any
from urllib.parse import quote

from app.communication.followup_next_steps_quality import followup_blocks_collapsed_default

logger = logging.getLogger(__name__)

ENVELOPE_VERSION = 1
MAX_UI_BLOCKS = 16
MAX_CHART_B64_CHARS = 1_200_000
MAX_TABLE_ROWS = 40
MAX_TABLE_COLS = 20
MAX_MARKDOWN_REPORT_CHARS = 500_000

# tool_fired string -> (icon_hint, user-facing label)
TOOL_ATTRIBUTION: dict[str, tuple[str, str]] = {
    "search_corpus": ("book", "Provider manual"),
    "google_search": ("globe", "Web search"),
    "web_scrape": ("globe", "Web page"),
    "healthcare_npi_lookup": ("person", "Provider registry"),
    "npi_lookup": ("person", "Provider registry"),
    "healthcare_query": ("code", "Healthcare codes"),
    "run_credentialing_report": ("doc", "Credentialing report"),
    "validate_credentialing_step": ("doc", "Credentialing co-pilot"),
    "run_roster_reconciliation_report": ("doc", "Roster reconciliation report"),
    "roster_report": ("doc", "Credentialing report"),
    "refuse": ("block", "Not answerable"),
    "web_search": ("globe", "Web search"),
    "credentialing_qa": ("doc", "Credentialing Q&A"),
    "list_tasks": ("task", "Task manager"),
    "create_task": ("task", "Task manager"),
    "resolve_task": ("task", "Task manager"),
}


def tool_attribution_block(tool_fired: str) -> dict[str, Any]:
    key = (tool_fired or "").strip().lower().replace("-", "_")
    icon, label = TOOL_ATTRIBUTION.get(key, ("search", "Research"))
    return {"type": "tool_attribution", "tool_fired": tool_fired or "unknown", "icon": icon, "label": label}


def resolve_tool_fired(ctx: Any) -> str:
    t = getattr(ctx, "react_last_tool", None)
    if isinstance(t, str) and t.strip():
        return t.strip()
    sk = getattr(ctx, "active_skill", None)
    if isinstance(sk, dict):
        name = (sk.get("skill") or "").strip()
        if name:
            return name
    return "unknown"


def _corpus_open_href_from_template(
    template: str, document_id: str, page_number: Any
) -> str:
    href = template.replace("{document_id}", document_id)
    if page_number is not None and "{page}" in template:
        try:
            href = href.replace("{page}", str(int(page_number)))
        except (TypeError, ValueError):
            href = href.replace("{page}", "")
    return href


def _corpus_open_href_from_rag_app_public_url(document_id: str, page_number: Any) -> str | None:
    """Deep link to mobius-rag Read tab: ?tab=read&documentId=…&pageNumber=…"""
    base = (os.environ.get("MOBIUS_RAG_APP_PUBLIC_URL") or "").strip().rstrip("/")
    if not base:
        return None

    q = f"tab=read&documentId={quote(document_id, safe='')}"
    if page_number is not None:
        try:
            q += f"&pageNumber={int(page_number)}"
        except (TypeError, ValueError):
            pass
    return f"{base}/?{q}"


def enrich_sources_open_hrefs(sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Add open_href / open_kind when template, RAG public URL, or source url is available."""
    template = (os.environ.get("MOBIUS_DOCUMENT_VIEWER_URL_TEMPLATE") or "").strip()
    out: list[dict[str, Any]] = []
    for s in sources or []:
        if not isinstance(s, dict):
            continue
        d = dict(s)
        url = d.get("url")
        if isinstance(url, str) and url.strip() and re.match(r"^https?://", url.strip(), re.I):
            d["open_href"] = url.strip()
            d["open_kind"] = "web"
        elif d.get("document_id") is not None:
            did = str(d["document_id"]).strip()
            if did:
                href: str | None = None
                if template:
                    href = _corpus_open_href_from_template(template, did, d.get("page_number"))
                else:
                    href = _corpus_open_href_from_rag_app_public_url(did, d.get("page_number"))
                if href:
                    d["open_href"] = href
                    d["open_kind"] = "corpus"
        cite = d.get("cite_text")
        if (
            isinstance(cite, str)
            and cite.strip()
            and d.get("open_kind") == "corpus"
            and isinstance(d.get("open_href"), str)
            and d["open_href"].strip()
        ):
            href = d["open_href"].strip()
            sep = "&" if "?" in href else "?"
            frag = "citeText=" + quote(cite.strip()[:400], safe="")
            d["open_href"] = href + sep + frag
        out.append(d)
    return out


def _section_list_lines(sec: dict[str, Any]) -> list[str]:
    """Bullets under keys various models emit instead of `bullets`."""
    for key in ("bullets", "items", "points", "lines"):
        v = sec.get(key)
        if isinstance(v, list):
            return [str(x).strip() for x in v if isinstance(x, str) and str(x).strip()]
    return []


def _section_prose_fields(sec: dict[str, Any]) -> list[str]:
    """Paragraph-style detail (models often use these when they skip bullet arrays)."""
    out: list[str] = []
    for key in ("body", "text", "content", "markdown", "summary", "narrative", "paragraph"):
        v = sec.get(key)
        if isinstance(v, str) and v.strip():
            out.append(v.strip())
    return out


def _sections_to_detail_markdown(sections: list[Any]) -> str:
    parts: list[str] = []
    for sec in sections or []:
        if not isinstance(sec, dict):
            continue
        label = (sec.get("label") or sec.get("title") or "").strip()
        if label:
            parts.append(f"**{label}**")
        for line in _section_list_lines(sec):
            parts.append(f"- {line}")
        for para in _section_prose_fields(sec):
            parts.append(para)
        parts.append("")
    return "\n".join(parts).strip()


def _resolutions_to_detail_markdown(resolutions: list[Any]) -> str:
    """Per-subquestion answers for the Details panel when sections are thin or empty."""
    parts: list[str] = []
    for item in resolutions or []:
        if not isinstance(item, dict):
            continue
        q = (item.get("question") or "").strip()
        src = (item.get("source") or "").strip()
        res = item.get("resolution")
        body = ""
        if isinstance(res, str) and res.strip():
            body = res.strip()
        elif isinstance(res, dict):
            da = res.get("direct_answer")
            if isinstance(da, str) and da.strip():
                body = da.strip()
        if not body:
            continue
        if q:
            head = f"**{q}**"
            if src:
                head += f" _({src})_"
            parts.append(f"{head}\n\n{body}")
        else:
            parts.append(body)
    return "\n\n".join(parts).strip()


def _supplemental_detail_markdown(answer_card: dict[str, Any]) -> str:
    """Confidence note, citations, and required variables when sections are empty or thin."""
    chunks: list[str] = []

    cn = answer_card.get("confidence_note")
    if isinstance(cn, str) and cn.strip():
        chunks.append("**Note on confidence**\n\n" + cn.strip()[:8000])

    rv = answer_card.get("required_variables")
    if isinstance(rv, list) and rv:
        names = [str(x).strip() for x in rv if x is not None and str(x).strip()]
        if names:
            chunks.append("**Depends on**\n\n" + "\n".join(f"- {n}" for n in names[:50]))

    cites = answer_card.get("citations")
    if isinstance(cites, list) and cites:
        cite_lines: list[str] = ["**Citations**", ""]
        for c in cites[:30]:
            if not isinstance(c, dict):
                continue
            title = (c.get("doc_title") or c.get("title") or "").strip()
            loc = (c.get("locator") or "").strip()
            snip = (c.get("snippet") or "").strip()
            head = " — ".join(x for x in (title, loc) if x)
            if not head and not snip:
                continue
            line = f"- {head}" if head else "- (source)"
            if snip:
                line += f"\n\n  > {snip[:500]}"
            cite_lines.append(line)
        if len(cite_lines) > 2:
            chunks.append("\n".join(cite_lines).strip())

    return "\n\n".join(chunks).strip()


def _merge_detail_markdown(existing: str, addition: str, *, max_len: int = 80000) -> str:
    a = (existing or "").strip()
    b = (addition or "").strip()
    if not a:
        return b[:max_len]
    if not b:
        return a[:max_len]
    merged = f"{a}\n\n{b}".strip()
    if len(merged) > max_len:
        merged = merged[: max_len - 3] + "…"
    return merged


def _validate_ui_block(block: Any, *, max_source_index: int) -> dict[str, Any] | None:
    if not isinstance(block, dict):
        return None
    btype = block.get("type")
    if not isinstance(btype, str):
        return None
    btype = btype.strip().lower()
    if btype == "chart":
        title = block.get("title")
        if title is not None and not isinstance(title, str):
            return None
        caption = block.get("caption")
        if caption is not None and not isinstance(caption, str):
            return None
        b64 = block.get("image_base64")
        if isinstance(b64, str) and len(b64) > MAX_CHART_B64_CHARS:
            logger.debug("assistant_envelope: chart image_base64 truncated/over max")
            return None
        if not isinstance(b64, str) or not b64.strip():
            return None
        out: dict[str, Any] = {"type": "chart", "image_base64": b64.strip()}
        if isinstance(title, str) and title.strip():
            out["title"] = title.strip()[:500]
        if isinstance(caption, str) and caption.strip():
            out["caption"] = caption.strip()[:2000]
        return out
    if btype == "table":
        headers = block.get("headers")
        rows = block.get("rows")
        if not isinstance(headers, list) or not isinstance(rows, list):
            return None
        hdr = [str(h)[:200] for h in headers[:MAX_TABLE_COLS] if h is not None]
        clean_rows: list[list[str]] = []
        for row in rows[:MAX_TABLE_ROWS]:
            if not isinstance(row, list):
                continue
            clean_rows.append([str(c)[:500] for c in row[:MAX_TABLE_COLS]])
        if not hdr and not clean_rows:
            return None
        return {"type": "table", "headers": hdr, "rows": clean_rows}
    if btype == "callout":
        body = block.get("body") or block.get("text")
        if not isinstance(body, str) or not body.strip():
            return None
        variant = block.get("variant")
        vo: dict[str, Any] = {"type": "callout", "body": body.strip()[:8000]}
        if isinstance(variant, str) and variant.strip() in ("info", "warning", "tip"):
            vo["variant"] = variant.strip()
        return vo
    if btype == "detail":
        md = block.get("markdown") or block.get("body")
        if not isinstance(md, str) or not md.strip():
            return None
        return {"type": "detail", "markdown": md.strip()[:80000], "collapsed_default": bool(block.get("collapsed_default", True))}
    if btype == "task_list":
        tasks = block.get("tasks")
        if not isinstance(tasks, list):
            return None
        # Trim tasks to avoid huge payloads
        safe_tasks = tasks[:100]
        out: dict[str, Any] = {"type": "task_list", "tasks": safe_tasks}
        filters = block.get("filters")
        if isinstance(filters, dict):
            out["filters"] = filters
        out["allow_create"] = bool(block.get("allow_create", False))
        out["allow_resolve"] = bool(block.get("allow_resolve", True))
        return out
    # ignore unknown / unsupported types
    return None


def _followup_items_for_envelope(items: list[Any], *, fallback_clickable: bool) -> list[dict[str, Any]]:
    """Build ``[{text, clickable}, ...]`` for envelope blocks (accepts normalized dicts or legacy strings)."""
    out: list[dict[str, Any]] = []
    for x in items or []:
        if isinstance(x, dict):
            t = (x.get("text") or "").strip()
            if not t:
                continue
            c = x.get("clickable")
            if c is None:
                c = fallback_clickable
            out.append({"text": t[:500], "clickable": bool(c)})
        elif isinstance(x, str) and x.strip():
            out.append({"text": x.strip()[:500], "clickable": fallback_clickable})
        if len(out) >= 8:
            break
    return out


def build_assistant_envelope_v1(
    *,
    answer_card: dict[str, Any] | None,
    ui_blocks_raw: list[Any] | None,
    tool_fired: str,
    response_sources: list[dict[str, Any]],
    next_steps: list[Any],
    next_questions_for_user: list[Any],
    roster_report_final_md: str | None,
    has_roster_pdf: bool,
    resolutions: list[Any] | None = None,
    source_confidence_strip: str = "",
    pipeline_human_gate: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Merge authoritative data with validated LLM ui_blocks."""
    blocks: list[dict[str, Any]] = []
    blocks.append(tool_attribution_block(tool_fired))

    if isinstance(pipeline_human_gate, dict) and (pipeline_human_gate.get("run_id") or "").strip():
        blocks.append(
            {
                "type": "pipeline_human_gate",
                "version": 1,
                "gate": pipeline_human_gate,
            }
        )

    if answer_card and isinstance(answer_card.get("direct_answer"), str):
        da = answer_card["direct_answer"].strip()
        if da:
            blocks.append({"type": "direct_answer", "markdown": da[:50000]})
        secs = answer_card.get("sections")
        section_md = ""
        if isinstance(secs, list) and secs:
            section_md = _sections_to_detail_markdown(secs)
        resolution_md = _resolutions_to_detail_markdown(resolutions or [])
        supplemental = _supplemental_detail_markdown(answer_card)
        detail_parts = [p for p in (section_md, resolution_md, supplemental) if p]
        if detail_parts:
            combined_detail = "\n\n".join(detail_parts)
            blocks.append({"type": "detail", "markdown": combined_detail, "collapsed_default": True})

    seen_types: set[str] = set()
    for raw in (ui_blocks_raw or [])[:MAX_UI_BLOCKS]:
        vb = _validate_ui_block(raw, max_source_index=max(0, len(response_sources)))
        if not vb:
            continue
        # avoid duplicate heavy types from model
        t = vb["type"]
        if t == "chart" and "chart" in seen_types:
            continue
        if t == "detail":
            existing = next((b for b in blocks if b.get("type") == "detail"), None)
            if existing is not None:
                add_md = str(vb.get("markdown") or "").strip()
                if add_md:
                    existing["markdown"] = _merge_detail_markdown(
                        str(existing.get("markdown") or ""), add_md
                    )
                continue
        seen_types.add(t)
        blocks.append(vb)

    refs: list[dict[str, Any]] = []
    for s in response_sources or []:
        if not isinstance(s, dict):
            continue
        ref: dict[str, Any] = {
            "index": int(s.get("index") or 0),
            "title": (s.get("document_name") or "Source")[:500],
            "page": s.get("page_number"),
            "snippet": (s.get("text") or "")[:400],
        }
        if s.get("document_id") is not None:
            ref["document_id"] = s.get("document_id")
        oh = s.get("open_href")
        ok = s.get("open_kind")
        if isinstance(oh, str) and oh.strip():
            ref["open"] = {"kind": (ok if isinstance(ok, str) else "external")[:32], "href": oh.strip()[:2000]}
        refs.append(ref)
    blocks.append({"type": "sources", "refs": refs})

    followups_collapsed = followup_blocks_collapsed_default(source_confidence_strip)
    step_items = _followup_items_for_envelope(next_steps, fallback_clickable=False)
    if step_items:
        blocks.append(
            {
                "type": "next_steps",
                "items": step_items,
                "collapsed_default": followups_collapsed,
            }
        )
    q_items = _followup_items_for_envelope(next_questions_for_user, fallback_clickable=True)
    if q_items:
        blocks.append(
            {
                "type": "suggested_questions",
                "items": q_items,
                "collapsed_default": followups_collapsed,
            }
        )

    if roster_report_final_md and str(roster_report_final_md).strip():
        md = str(roster_report_final_md).strip()
        if len(md) > MAX_MARKDOWN_REPORT_CHARS:
            md = md[:MAX_MARKDOWN_REPORT_CHARS] + "\n\n…"
        blocks.append({"type": "markdown_report", "markdown": md})
    if has_roster_pdf:
        blocks.append({"type": "attachments", "has_pdf": True})

    return {"version": ENVELOPE_VERSION, "blocks": blocks}
