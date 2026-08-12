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
    "patch_task": ("task", "Task manager"),
    "assign_task": ("task", "Task manager"),
    "dismiss_task": ("task", "Task manager"),
    "fetch_document": ("doc", "Document fetch"),
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


def _section_fallback_markdown(sec: dict[str, Any]) -> str:
    """A section with no recognized format (missing, or not one of the typed
    formats _section_to_typed_block handles) -- e.g. legacy/untyped
    sections with just bullets/body prose and no explicit "format" key.
    Genuinely free-form, so it belongs in detail per Chat Master's spec
    ("detail... only when there is genuinely free-form extended content
    that isn't a typed section") -- not silently dropped."""
    parts: list[str] = []
    label = (sec.get("label") or sec.get("title") or "").strip()
    if label:
        parts.append(f"**{label}**")
    for line in _section_list_lines(sec):
        parts.append(f"- {line}")
    for para in _section_prose_fields(sec):
        parts.append(para)
    return "\n".join(parts).strip()


_DOMAIN_CARD_FORMATS = ("appeals_playbook", "appeals_rules")


def _section_to_typed_block(sec: Any) -> dict[str, Any] | None:
    """card.sections[] -> one typed envelope block, per Chat Master's
    2026-08-10 full-collapse spec. Replaces _sections_to_detail_markdown on
    the sections path -- that converter never read sec["data"] at all, so
    every structured format (table/stats/steps/bars/conditions, all of
    which nest their content under "data" per chat_config.py's own prompt
    schema) silently lost its structure the moment it reached the envelope.
    None on anything unrecognized or malformed -- never guess a shape."""
    if not isinstance(sec, dict):
        return None
    fmt = (sec.get("format") or "").strip().lower()
    label = sec.get("label") or sec.get("title")
    label = label.strip() if isinstance(label, str) and label.strip() else None
    data = sec.get("data")
    data = data if isinstance(data, dict) else {}

    if fmt == "table":
        headers, rows = data.get("headers"), data.get("rows")
        if not isinstance(headers, list) or not isinstance(rows, list):
            return None
        # 2026-08-12 (Chat Master, live finding cid=997193e2): the model
        # (gemini-2.5-flash) generated a syntactically valid but
        # structurally malformed table -- one row with 2 cells against a
        # 4-column header ("Sunshine Health", "**18") -- and nothing
        # caught it before it shipped to the user as a visibly broken
        # cell. This is a genuine model-generation defect, not a token/
        # length truncation (completion_valid=true, well under max_tokens).
        # Cheap structural check: a row's cell count must match the header
        # count. Drop malformed rows, keep valid ones -- a partial table
        # with correct rows beats a table with a garbled cell. If every
        # row is malformed, drop the section entirely; direct_answer/
        # react_draft already carries the same information in prose as
        # the safe fallback.
        valid_rows = [r for r in rows if isinstance(r, list) and len(r) == len(headers)]
        if not valid_rows:
            return None
        out: dict[str, Any] = {"type": "table", "headers": headers, "rows": valid_rows}
    elif fmt in ("stats", "steps", "bars", "conditions"):
        items = data.get("items")
        if not isinstance(items, list) or not items:
            return None
        out = {"type": fmt, "items": items}
    elif fmt == "bullets":
        # Legacy LLM-authored bullets sections vary the key
        # (bullets/items/points/lines, all flat string lists) -- the
        # deterministic-format detector uses "bullets" specifically, tool
        # output and older LLM cards use the others. Same tolerance
        # _sections_to_detail_markdown already had, kept here.
        items = _section_list_lines(sec) or [
            str(x).strip() for x in (data.get("items") or []) if isinstance(x, str) and str(x).strip()
        ]
        if not items:
            return None
        out = {"type": "bullets", "items": items}
    elif fmt in _DOMAIN_CARD_FORMATS:
        if not data:
            return None
        out = {"type": "domain_card", "variant": fmt, "data": data}
    else:
        return None

    if label:
        out["label"] = label
    return out


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
    if btype == "document_download":
        docs = block.get("documents")
        if not isinstance(docs, list):
            return None
        safe_docs: list[dict[str, Any]] = []
        for d in docs[:5]:
            if not isinstance(d, dict):
                continue
            doc_id = str(d.get("document_id") or "").strip()
            dl_url = str(d.get("download_url") or "").strip()
            if not doc_id or not dl_url:
                continue
            entry: dict[str, Any] = {
                "document_id": doc_id[:100],
                "download_url": dl_url[:2000],
                "title": str(d.get("title") or d.get("filename") or "Document").strip()[:500],
            }
            fb = d.get("fallback_download_url")
            if isinstance(fb, str) and fb.strip():
                entry["fallback_download_url"] = fb.strip()[:2000]
            for key in ("filename", "host", "payer", "state", "program", "authority_level", "resolved_via"):
                v = d.get(key)
                if isinstance(v, str) and v.strip():
                    entry[key] = v.strip()[:200]
            safe_docs.append(entry)
        if not safe_docs:
            return None
        out = {"type": "document_download", "documents": safe_docs}
        q = block.get("query")
        if isinstance(q, str) and q.strip():
            out["query"] = q.strip()[:500]
        return out
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
        # v2 action flags (2026-07-07 TaskEnvelope) — pass through so the
        # frontend's per-card Dismiss/Assign/Edit gating is explicit
        # rather than relying on missing-key defaults.
        out["allow_edit"] = bool(block.get("allow_edit", True))
        out["allow_assign"] = bool(block.get("allow_assign", True))
        out["allow_dismiss"] = bool(block.get("allow_dismiss", True))
        op = block.get("operation")
        if isinstance(op, str) and op:
            out["operation"] = op
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


def _build_credentialing_card_block(data: dict[str, Any]) -> dict[str, Any] | None:
    """Validate and build credentialing_card block from provider data dict.

    Expected shapes:
    - Provider-level: {npi, provider_name, org, status, flags, action_url}
    - Org-level summary: {org, provider_name, status, flags, action_url, org_summary: true}
    At least one of npi or org must be present.
    """
    npi = (data.get("npi") or "").strip()
    org = (data.get("org") or "").strip()
    if not npi and not org:
        return None
    flags: list[dict[str, str]] = []
    for f in (data.get("flags") or [])[:20]:
        if not isinstance(f, dict):
            continue
        text = (f.get("text") or "").strip()
        severity = (f.get("severity") or "info").strip().lower()
        if text and severity in ("info", "warning", "error"):
            flags.append({"text": text[:200], "severity": severity})
    block: dict[str, Any] = {
        "type": "credentialing_card",
        "provider_name": (data.get("provider_name") or "").strip()[:200],
        "org": org[:200],
        "status": (data.get("status") or "unknown").strip()[:50],
        "flags": flags,
    }
    if npi:
        block["npi"] = npi[:10]
    if data.get("org_summary"):
        block["org_summary"] = True
    action_url = (data.get("action_url") or "").strip()
    if action_url:
        block["action_url"] = action_url[:2000]
    return block


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
    credentialing_card_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Merge authoritative data with validated LLM ui_blocks.

    2026-08-10 full-collapse (Chat Master spec, Ananth-approved): the
    envelope is now the COMPLETE render source, not chrome-only alongside a
    separately-parsed AnswerCard -- that dual-read was the triple-print
    mechanism (direct_answer/display_summary and sections rendered from
    both the raw card JSON and the envelope independently). Ordering below
    is deliberate and owned by the backend; FE renders blocks top-to-bottom
    in list order, no client-side reordering.
    """
    blocks: list[dict[str, Any]] = []

    if answer_card:
        _mode = answer_card.get("mode")
        if isinstance(_mode, str) and _mode.strip():
            blocks.append({"type": "mode_badge", "mode": _mode.strip()})

    blocks.append(tool_attribution_block(tool_fired))

    if isinstance(pipeline_human_gate, dict) and (pipeline_human_gate.get("run_id") or "").strip():
        blocks.append(
            {
                "type": "pipeline_human_gate",
                "version": 1,
                "gate": pipeline_human_gate,
            }
        )

    if isinstance(credentialing_card_data, dict):
        _cc = _build_credentialing_card_block(credentialing_card_data)
        if _cc:
            blocks.append(_cc)

    if answer_card and isinstance(answer_card.get("direct_answer"), str):
        da = answer_card["direct_answer"].strip()
        if da:
            blocks.append({"type": "direct_answer", "markdown": da[:50000]})

        # Typed sections, in sections[] order -- replaces
        # _sections_to_detail_markdown, which never read sec["data"] and so
        # silently dropped every structured format's actual content. A
        # section with no recognized format (missing "format", or legacy/
        # untyped shapes) falls back into the detail block below instead of
        # being dropped -- still genuinely free-form content.
        secs = answer_card.get("sections")
        _section_fallback_parts: list[str] = []
        if isinstance(secs, list):
            for sec in secs:
                tb = _section_to_typed_block(sec)
                if tb:
                    blocks.append(tb)
                elif isinstance(sec, dict):
                    fb = _section_fallback_markdown(sec)
                    if fb:
                        _section_fallback_parts.append(fb)

        _tldr = answer_card.get("tldr_summary")
        if isinstance(_tldr, str) and _tldr.strip():
            blocks.append({"type": "tldr", "markdown": _tldr.strip()[:4000]})

        _draft = answer_card.get("react_draft")
        _trace = answer_card.get("reasoning_trace")
        if (isinstance(_draft, str) and _draft.strip()) or isinstance(_trace, list):
            fp: dict[str, Any] = {"type": "first_pass", "collapsed_default": True}
            if isinstance(_draft, str) and _draft.strip():
                fp["draft_markdown"] = _draft.strip()[:20000]
            if isinstance(_trace, list) and _trace:
                fp["trace_rounds"] = _trace
            blocks.append(fp)

        # Takeaways block — distilled bullets, shown after the draft answer.
        _tw = answer_card.get("takeaways")
        if isinstance(_tw, list):
            _tw_items = [str(t).strip() for t in _tw if t and str(t).strip()][:5]
            if _tw_items:
                blocks.append({"type": "takeaways", "items": _tw_items})

        # Gaps block — folded into the detail section as a callout when present.
        _gaps = answer_card.get("gaps")
        if isinstance(_gaps, list):
            _gap_lines = [str(g).strip() for g in _gaps if g and str(g).strip()][:4]
            if _gap_lines:
                blocks.append({"type": "callout", "variant": "info",
                                "body": "**Sources did not cover:**\n\n" + "\n".join(f"- {g}" for g in _gap_lines)})

        # incomplete_coverage / "Continue gathering" (2026-08-11, Task #84,
        # Chat Master) -- react self-reported real partial progress on a
        # multi-item question (e.g. found 2 of 4 payors) before exhausting
        # its round budget. Distinct from suggest_escalate (genuinely
        # stalled, suggests a different mode entirely) -- this is "more of
        # the same search would likely finish the job," so the callout
        # names what's true and the chip below offers to keep going with
        # the SAME approach, not a different one.
        if answer_card.get("incomplete_coverage"):
            _ic_summary = (answer_card.get("incomplete_coverage_summary") or "").strip()
            _ic_body = "**Partial answer — more information may be available.**"
            if _ic_summary:
                _ic_body += f"\n\n{_ic_summary}"
            blocks.append({"type": "callout", "variant": "info", "body": _ic_body})

        # detail is no longer the sections catch-all -- only genuinely
        # free-form content that isn't a typed section (resolutions,
        # confidence note, citations, required variables) lands here.
        section_fallback_md = "\n\n".join(_section_fallback_parts)
        resolution_md = _resolutions_to_detail_markdown(resolutions or [])
        supplemental = _supplemental_detail_markdown(answer_card)
        detail_parts = [p for p in (section_fallback_md, resolution_md, supplemental) if p]
        if detail_parts:
            combined_detail = "\n\n".join(detail_parts)
            blocks.append({"type": "detail", "markdown": combined_detail, "collapsed_default": True})

        # Layer 2 appeals integration — pass action chips through the envelope.
        _sa = answer_card.get("suggested_actions")
        _chips: list[dict[str, Any]] = []
        if isinstance(_sa, list) and _sa:
            _chips.extend(
                a for a in _sa
                if isinstance(a, dict)
                and a.get("type") == "external_link"
                and isinstance(a.get("label"), str)
                and isinstance(a.get("url"), str)
            )
        # continue_search chip (Task #84): FE resubmits the SAME turn with
        # is_continuation=True (Task #83's full-context path) + an extended
        # round budget -- not a link, an in-app resubmit action, so it's a
        # distinct chip "type" from external_link within the same block
        # rather than a new block type.
        if answer_card.get("incomplete_coverage"):
            _chips.append({"type": "continue_search", "label": "Continue gathering"})
        if _chips:
            blocks.append({"type": "action_chips", "chips": _chips})

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

    if answer_card:
        _corr = answer_card.get("correction")
        if isinstance(_corr, dict):
            _orig = (_corr.get("original") or "").strip()
            _fixed = (_corr.get("corrected") or "").strip()
            if _orig and _fixed:
                blocks.append({"type": "correction", "original": _orig[:2000], "corrected": _fixed[:2000]})

    return {"version": ENVELOPE_VERSION, "blocks": blocks}
