"""Builtin skill: web_scrape — scrape a URL via web_scrape_review MCP.

Second of five migrations in the skill-registry refactor (commit 2).

Semantics to preserve vs. the legacy ``if hint == "web_scrape"`` branch
in ``app/services/tool_agent.py::_answer_tool_impl``:

  1. The handler assumes a URL is present in ``SkillCall.inputs``. The
     hint-dispatcher in tool_agent.py does URL extraction + the
     fall-through-to-google_search-when-no-URL rewrite BEFORE calling
     the registry, so by the time we're here, we have a URL.

  2. ``scrape_mode`` comes from ``tool_inputs`` (planner) or defaults to
     "quick". The legacy ``_run_web_scrape`` handles normalization.

  3. The handler wraps the existing ``_run_web_scrape`` helper in
     tool_agent.py rather than reimplementing the MCP call + mode
     dispatch + cap logic. Commit 3 will inline that helper into this
     file once the legacy branch is deleted; for now, wrapping keeps
     parity trivial to assert.

Envelope shape:
  - ``text``    = truncated page content (or error message)
  - ``sources`` = one SourceRef per scraped page with url + domain
  - ``signal``  = "google_only" on success, "no_sources" on failure
"""

from __future__ import annotations

from app.skills.registry import SkillCall, SkillEnvelope, SkillSpec, SourceRef, register


def _run(call: SkillCall) -> SkillEnvelope:
    # Lazy import to avoid circular load: tool_agent imports the
    # registry, and the registry loads this module at import time.
    from app.services.tool_agent import _run_web_scrape

    url = (call.inputs.get("url") or "").strip()
    if not url:
        # Defensive — the dispatcher should have rewritten hint to
        # google_search when no URL was available. If we somehow got
        # here, give the same response the keyword-path produces.
        return SkillEnvelope(
            text=(
                "I can scrape web pages when you give me a URL. "
                "Try: 'Scrape https://example.com' or paste the URL."
            ),
            signal="no_sources",
        )

    scrape_mode = call.inputs.get("scrape_mode") or call.inputs.get("mode")
    text, sources, usage, signal = _run_web_scrape(
        url,
        emitter=call.emitter,
        scrape_mode=scrape_mode,
    )
    return SkillEnvelope(
        text=text,
        sources=[
            SourceRef(
                document_name=(s.get("document_name") or "").strip() or "web",
                index=int(s.get("index") or 1),
                text=(s.get("text") or "")[:300],
                source_type=s.get("source_type") or "web",
                url=s.get("url"),
            )
            for s in (sources or [])
        ],
        usage=usage,
        signal=signal,
    )


register(
    SkillSpec(
        name="web_scrape",
        description=(
            "Read the web: **quick** (default), **medium**, or **detailed** crawl from the seed URL.\n"
            "scrape_mode:\n"
            "  **quick** — single page, fastest (default when unsure).\n"
            "  **medium** — same-site tree crawl: depth up to **3**, up to **6** HTML pages (no doc download quota).\n"
            "  **detailed** — deeper crawl: depth up to **5**, up to **50** pages, up to **10** linked document downloads (e.g. PDFs) when the scraper supports it.\n"
            "Use **quick** for one policy page or a direct link; **medium** for a small site section; **detailed** when the user needs broad coverage or many linked files and latency is acceptable.\n"
            "Omit scrape_mode or use **quick** unless the user (or agentic mode) clearly needs more coverage.\n"
            "Returns: extracted text (combined across pages when crawling)."
        ),
        handler=_run,
        inputs_schema={
            "type": "object",
            "properties": {
                "url": {"type": "string", "format": "uri"},
                "scrape_mode": {
                    "type": "string",
                    "enum": ["quick", "medium", "detailed"],
                    "default": "quick",
                },
            },
            "required": ["url"],
        },
        requires_jurisdiction=False,
        follow_up_capable=False,
    )
)
