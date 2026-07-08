"""product_help_search — answer "how do I use Mobius?" from the product docs.

Thin chat-side skill: HTTP-calls the standalone product-awareness retrieval service,
renders the answer, and — on a gap outcome — files a docs_gap / feature_request to the
feedback agent's storage IN-PROCESS (best-effort, post-answer; the gap write can never
break the answer path). See docs/product-awareness-feedback-contract.md.

Parallels vibe / product_feedback: stateless service + chat-side SkillSpec handler.
"""
from __future__ import annotations

import json
import logging
import os
import urllib.request

from app.skills.registry import SkillCall, SkillEnvelope, SkillSpec, SourceRef, register

logger = logging.getLogger(__name__)

PRODUCT_HELP_URL = os.environ.get(
    "CHAT_SKILLS_PRODUCT_HELP_SEARCH_URL",
    "http://localhost:8070/search",
).rstrip("/")
PRODUCT_HELP_TIMEOUT_SEC = float(
    os.environ.get("CHAT_SKILLS_PRODUCT_HELP_SEARCH_TIMEOUT_SEC", "10")
)


def _ctx_field(call: SkillCall, name: str):
    ctx = getattr(call, "pipeline_ctx", None)
    return getattr(ctx, name, None) if ctx is not None else None


def _search(payload: dict) -> dict | None:
    try:
        req = urllib.request.Request(
            PRODUCT_HELP_URL,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=PRODUCT_HELP_TIMEOUT_SEC) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        logger.warning("[product_help] service call failed: %s", e)
        return None


def _file_gap(gap: dict, *, query: str, user_id, thread_id, correlation_id,
              org_slug, config_sha) -> str | None:
    """Best-effort, in-process write to the feedback store (the contract seam).

    Wrapped so a DB failure degrades to a log — the answer has already been returned.
    """
    if not gap:
        return None
    try:
        from app.storage import product_feedback as fb

        module = gap.get("module")
        area_tags = [module] if module and module != "unknown" else None
        return fb.insert_open_feedback(
            trigger="auto_harvest",              # contract (feedback agent, 2026-07-02): machine-harvested
                                                 # gap, cleanly separable from user-voiced feedback
            category=gap["category"],            # docs_gap | feature_request
            verbatim=gap.get("verbatim") or query,
            summary=gap.get("summary", ""),
            area_tags=area_tags,
            routed_to=fb.route_for(gap["category"]),
            user_id=user_id,
            thread_id=thread_id,
            correlation_id=correlation_id,
            org_slug=org_slug,
            config_sha=config_sha,
        )
    except Exception:
        logger.warning("[product_help] gap logging failed — continuing", exc_info=True)
        return None


def _sources(items: list[dict] | None) -> list[SourceRef]:
    out: list[SourceRef] = []
    for i, s in enumerate(items or [], 1):
        module = s.get("module") or ""
        section = s.get("section") or ""
        name = f"{module} · {section}".strip(" ·")
        # document_id must RESOLVE when the user clicks "Open document":
        # 'product-docs:<module>' is routed by chat's doc_reader proxy to the
        # product-awareness /doc endpoint (keeping product docs out of rag.documents).
        # The chunk_id is kept in extra for tracing.
        doc_id = f"product-docs:{module}" if module else None
        out.append(SourceRef(
            document_name=name or "product docs",
            index=i,
            text=section,                      # section heading — the highlight anchor
            source_type="document",
            document_id=doc_id,
            extra={"chunk_id": s.get("chunk_id"), "score": s.get("score"),
                   "source_path": s.get("source_path"), "doc_type": s.get("doc_type"),
                   "cite_text": section},
        ))
    return out


def _run_product_help(call: SkillCall) -> SkillEnvelope:
    inputs = call.inputs or {}
    query = (inputs.get("query") or call.question or call.user_message or "").strip()
    if not query:
        return SkillEnvelope(text="", signal="no_sources")

    payload = {
        "query": query,
        "k": int(inputs.get("k") or 6),
        "audience": inputs.get("audience"),
        "module": inputs.get("module"),
        "in_scope_only": bool(inputs.get("in_scope_only", False)),
    }
    resp = _search(payload)
    if resp is None:
        # service unreachable — return empty so the integrator sees a clean miss,
        # not an error string that it tries to format into an answer card.
        return SkillEnvelope(text="", signal="no_sources")

    outcome = resp.get("outcome")
    answer = resp.get("text") or ""

    # ANSWER first; THEN file any gap (post-answer, best-effort) — the load-bearing invariant.
    feedback_id = None
    gap = resp.get("gap")
    if gap:
        feedback_id = _file_gap(
            gap,
            query=query,
            user_id=_ctx_field(call, "user_id"),
            thread_id=call.thread_id or _ctx_field(call, "thread_id"),
            correlation_id=_ctx_field(call, "correlation_id"),
            org_slug=_ctx_field(call, "org_slug"),
            config_sha=_ctx_field(call, "config_sha"),
        )

    return SkillEnvelope(
        text=answer,
        sources=_sources(resp.get("sources")) if outcome == "answer" else [],
        signal="corpus_only" if outcome == "answer" else "no_sources",
        extra={
            "outcome": outcome,
            "module": resp.get("module"),
            "s_top": resp.get("s_top"),
            "feedback_id": feedback_id,
            # mobius-interact "▶ Show me" ref ({script_id, title}) — frontend renders
            # a chip that fetches the script from the interact registry and runs it
            # in guide mode. None when the matched section has no demo.
            "demo": resp.get("demo"),
        },
    )


register(
    SkillSpec(
        name="product_help_search",
        description=(
            "Answer questions about how to USE Mobius itself — its features, setup, "
            "navigation, and 'how do I…' / 'where is…' questions about the product "
            "(chat, RAG, lexicon, skills, strategy). Grounded in the product documentation.\n"
            "Use when: the user asks about the product / app itself — how a feature works, "
            "how to do something in Mobius, what a capability is, or WHAT A NAMED MOBIUS "
            "FEATURE OR TILE IS/DOES even if the name sounds generic (e.g. 'what is the "
            "Public Library', 'Vault', 'Strategy', 'Roster', 'the Pipeline'). Also for "
            "reactions to a feature ('I love X', 'X is confusing', 'where did Y go?'). "
            "Prefer this over web_search for anything that could be a Mobius feature name.\n"
            "Do NOT use when: the user asks a healthcare-policy or data question (use corpus "
            "search), or is giving feedback (use product_feedback). Returns documentation "
            "passages; if nothing is documented it says so and logs the gap."
        ),
        inputs_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "The product 'how do I…' question."},
                "k": {"type": "integer", "minimum": 1, "maximum": 20},
                "module": {
                    "type": "string",
                    "description": "Optional module filter (chat, rag, lexicon, skills, strategy, …).",
                },
                "audience": {
                    "type": "string",
                    "enum": ["user", "dev", "mixed"],
                    "description": "Optional audience filter.",
                },
            },
            "required": ["query"],
        },
        handler=_run_product_help,
        requires_jurisdiction=False,
        follow_up_capable=True,
        visible_to_planner=True,
        category="documents",
        display_name="Product Help",
    )
)
