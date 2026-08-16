"""verify_claim scaffold — resolve → page-text → [judge] → verdict.

Requirements: DOWNLOAD_AGENT_CORE_REQUIREMENTS.md §3/§4/§6a/§6b/§8.4/§8.5.

A DISTINCT capability from fetch_document (not a mode flag): Fact Store's
certification loop needs "does source document X still support claim Y on
page N", callable OUTSIDE a chat turn, returning a small machine-actionable
verdict, loud on failure. It shares the resolve + page-text substrate with
fetch_document but exposes its own entry point (`verify_claim` here, wrapped
by POST /chat/verify-claim).

SCAFFOLD STATUS (2026-08-16): everything up to the verdict JUDGMENT is
built and runnable — PK-resolve (reuses fetch_document._resolve_by_id, so
document_id is hard/PK-only per §6b, no fuzzy fallback), page-text fetch
from RAG's /documents/{id}/pages, and verdict shaping. The judgment itself
("does this page text support this claim") is deliberately NOT implemented
here: it is the same primitive Eval owns as `retrieval_grade` (judge ==
prod scorer == bandit reward), and building a second one would fork the
fact-checker. It's a pluggable injection point (`set_judge`). Until Eval
wires their grader, every call returns verdict=low_coverage with
status="judge_unwired" — LOUD, never a silent/false `agree`.

Verdict schema (§8.4, agreed with Fact Store):
    {verdict: "agree"|"contradict"|"low_coverage", quote, page, document_id}
plus scaffold fields (status, page_text_chars) so a caller can see the
substrate worked even while the judge is unwired.
"""

from __future__ import annotations

import logging
import os
import urllib.parse
import urllib.request
import json
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

# Cap page text handed to the judge / echoed, so a 261-page scan can't
# blow the judge's context or the response size.
_MAX_PAGE_TEXT_CHARS = 20_000

# Judge signature: (claim, page_text) -> {"verdict": <enum>, "quote": str}.
# Eval wires their retrieval_grade in via set_judge(); until then it's None
# and every verify returns a loud judge_unwired verdict.
JudgeFn = Callable[[str, str], dict]
_judge: Optional[JudgeFn] = None


def set_judge(fn: Optional[JudgeFn]) -> None:
    """Inject the verdict judge (Eval's retrieval_grade). Pass None to unwire."""
    global _judge
    _judge = fn


def judge_is_wired() -> bool:
    return _judge is not None


def _rag_base() -> str:
    return (
        os.environ.get("RAG_API_URL") or os.environ.get("RAG_API_BASE") or ""
    ).strip().rstrip("/")


def _fetch_page_text(document_id: str, page: Optional[int]) -> tuple[str, Optional[int]]:
    """Pull page text from RAG's /documents/{id}/pages.

    Returns (text, resolved_page). When ``page`` is given, fetches exactly
    that page. When omitted, fetches all pages and concatenates (capped) so
    a future scanning judge can locate the claim; resolved_page is then None
    until the judge pins it.
    """
    base = _rag_base()
    if not base:
        return "", page
    url = f"{base}/documents/{urllib.parse.quote(document_id)}/pages"
    if page is not None:
        url += f"?page_number={int(page)}"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    pages = payload.get("pages") or []
    if not pages:
        return "", page
    if page is not None:
        # Exact page requested — take the markdown (richer) or plain text.
        p = pages[0]
        text = (p.get("text_markdown") or p.get("text") or "")
        return text[:_MAX_PAGE_TEXT_CHARS], p.get("page_number")
    # No page given — concatenate all, page-tagged, capped.
    parts: list[str] = []
    total = 0
    for p in pages:
        t = (p.get("text_markdown") or p.get("text") or "").strip()
        if not t:
            continue
        chunk = f"[page {p.get('page_number')}]\n{t}"
        parts.append(chunk)
        total += len(chunk)
        if total >= _MAX_PAGE_TEXT_CHARS:
            break
    return "\n\n".join(parts)[:_MAX_PAGE_TEXT_CHARS], None


def _run_judge(claim: str, page_text: str) -> dict[str, Any]:
    """Invoke the injected judge, or return the loud unwired default.

    Deliberately NO fallback heuristic — a substring/keyword check here
    would be the exact forked fact-checker §8.5 refuses. Unwired means
    unwired: low_coverage, never a guessed agree. A judge that raises
    (transient LLM/HTTP failure) also maps to low_coverage, never agree
    (Eval §16.3: error/transient → low_coverage).
    """
    if _judge is None:
        return {"verdict": "low_coverage", "quote": "", "_unwired": True}
    try:
        result = _judge(claim, page_text) or {}
    except Exception as exc:
        logger.warning("verify_claim: judge call failed (transient → low_coverage): %s", exc)
        return {"verdict": "low_coverage", "quote": ""}
    verdict = result.get("verdict")
    if verdict not in ("agree", "contradict", "low_coverage"):
        logger.warning("verify_claim: judge returned bad verdict %r; coercing to low_coverage", verdict)
        return {"verdict": "low_coverage", "quote": str(result.get("quote") or "")}
    return {"verdict": verdict, "quote": str(result.get("quote") or "")}


# ── Eval grader wiring (§16.4 option b) ──────────────────────────────
# Eval ships POST /eval/grade-claim {claim, source_text, page?} ->
# {verdict, quote, page, fact_checker_version}, wrapping the SAME locked
# check_facts core (stage="rag_eval_adjudicate", the locked ruler — the
# server-side fail-closed guarantee from §16.2). We inject a thin
# pass-through client so the verdict semantics + locked ruler + version
# stamp all stay in Eval's lane. Until EVAL_GRADE_CLAIM_URL is set, the
# judge stays unwired (loud low_coverage) — shipping the endpoint is a
# config flip, not a code change here.


def _eval_grade_claim_client(url: str, timeout: int = 30) -> JudgeFn:
    def _judge(claim: str, page_text: str) -> dict[str, Any]:
        req = urllib.request.Request(
            url,
            data=json.dumps({"claim": claim, "source_text": page_text}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8")) or {}
        return {"verdict": payload.get("verdict"), "quote": payload.get("quote") or ""}
    return _judge


def configure_judge_from_env() -> bool:
    """Wire the Eval grader if EVAL_GRADE_CLAIM_URL is set. Returns True
    when a judge was wired. Idempotent; safe to call at import + startup."""
    url = (os.environ.get("EVAL_GRADE_CLAIM_URL") or "").strip()
    if not url:
        return False
    set_judge(_eval_grade_claim_client(url))
    logger.info("verify_claim: judge wired to Eval grader at %s", url)
    return True


# Wire at import so a deploy with EVAL_GRADE_CLAIM_URL set comes up live.
configure_judge_from_env()


def verify_claim(document_id: str, claim: str, page: Optional[int] = None) -> dict[str, Any]:
    """Verify whether ``claim`` is supported by document ``document_id`` (page ``page``).

    document_id is REQUIRED and resolved by primary key only — no fuzzy
    fallback (§6b: 18 near-dup AHCA contract versions with NULL
    effective_date make any name/query resolve ambiguous; the caller must
    already hold the exact id). Returns the §8.4 verdict schema plus scaffold
    status fields.
    """
    document_id = (document_id or "").strip()
    claim = (claim or "").strip()
    base_result: dict[str, Any] = {
        "verdict": "low_coverage",
        "quote": "",
        "page": page,
        "document_id": document_id,
    }
    if not document_id or not claim:
        return {**base_result, "status": "bad_request"}

    # 1. Resolve by PK (reuses fetch_document's UUID-guarded resolver).
    from app.skills.builtin.fetch_document import _resolve_by_id

    row = _resolve_by_id(document_id)
    if row is None:
        return {**base_result, "status": "document_not_found"}

    # 2. Fetch page text from RAG.
    try:
        page_text, resolved_page = _fetch_page_text(document_id, page)
    except Exception as exc:
        logger.warning("verify_claim: page-text fetch failed for %s p=%s: %s", document_id, page, exc)
        return {**base_result, "status": "page_fetch_error"}
    if not page_text:
        return {**base_result, "status": "no_page_text"}

    # 3. Judge (or loud-unwired default).
    judged = _run_judge(claim, page_text)

    # 4. Shape the verdict.
    return {
        "verdict": judged["verdict"],
        "quote": judged.get("quote", ""),
        "page": resolved_page if resolved_page is not None else page,
        "document_id": document_id,
        # Scaffold transparency: proves resolve + page-fetch worked even
        # while the judge is unwired. status="ok" once Eval's grader lands.
        "status": "ok" if judge_is_wired() else "judge_unwired",
        "page_text_chars": len(page_text),
    }
