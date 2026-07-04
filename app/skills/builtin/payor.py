"""Builtin skills: payor_lookup + payor_readiness.

Thin chat-side proxies to the mobius-payor service (independent Cloud Run
service, the authoritative payor readiness registry). Mirrors the corpus_search
pattern: POST to ``{PAYOR_API_URL}/api/skills/v1/payor_*`` and map the response
into a ``SkillEnvelope``.

The registry is the AUTHORITY for operational payor facts (provider phone,
appeals fax, EDI payer ID, portal, addresses, timely filing) — facts the corpus
can't reliably ground. When a user asks one of these, the planner should call
``payor_lookup`` instead of ``search_corpus``.
"""
from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from typing import Any

from app.skills.registry import SkillCall, SkillEnvelope, SkillSpec, register

logger = logging.getLogger(__name__)

_TIMEOUT_S = 30.0


def _base_url() -> str | None:
    return (os.environ.get("PAYOR_API_URL") or "").strip() or None


def _post(path: str, body: dict[str, Any]) -> dict[str, Any]:
    url = _base_url().rstrip("/") + path
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "X-Caller": "mobius_chat"}, method="POST")
    with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as r:
        return json.loads(r.read().decode())


def _payor_from(call: SkillCall, inputs: dict) -> str:
    return (inputs.get("payor") or (call.active_context or {}).get("payer") or "").strip()


def _unavailable(msg: str) -> SkillEnvelope:
    return SkillEnvelope(text="", sources=[], signal="no_sources", extra={"error": msg})


def _lookup(call: SkillCall) -> SkillEnvelope:
    inputs = call.inputs if isinstance(call.inputs, dict) else {}
    payor = _payor_from(call, inputs)
    field = (inputs.get("field") or "").strip()
    if not _base_url():
        return _unavailable("payor_api_url_unset")
    if not payor or not field:
        return _unavailable("need payor and field")
    try:
        d = _post("/api/skills/v1/payor_lookup", {"payor": payor, "field": field})
    except Exception as e:
        logger.warning("payor_lookup transport failed: %s", e)
        return _unavailable(f"{type(e).__name__}")
    if not d.get("ok"):
        return SkillEnvelope(text="", sources=[], signal="no_sources",
                             extra={"error": d.get("error"), "payor": payor, "field": field})
    text = (f"{payor} — {d.get('field')}: {d.get('value')}\n"
            f"(authoritative from the payor registry · {d.get('source')} · verified {d.get('as_of','')[:10]})")
    if call.emitter:
        call.emitter(f"✓ Payor registry: {d.get('field')} = {d.get('value')}")
    return SkillEnvelope(text=text, sources=[], signal="corpus_only",
                         extra={"payor_fact": d, "serve": d.get("serve")})


def _readiness(call: SkillCall) -> SkillEnvelope:
    inputs = call.inputs if isinstance(call.inputs, dict) else {}
    payor = _payor_from(call, inputs)
    if not _base_url():
        return _unavailable("payor_api_url_unset")
    if not payor:
        return _unavailable("need payor")
    try:
        d = _post("/api/skills/v1/payor_readiness", {"payor": payor})
    except Exception as e:
        return _unavailable(f"{type(e).__name__}")
    if not d.get("ok"):
        return _unavailable(d.get("error", "unknown payor"))
    text = (f"{payor} readiness — in corpus {d.get('in_corpus')} "
            f"({d.get('corpus_coverage_pct')}% ingested), known coverage {d.get('known_coverage_pct')}%, "
            f"integrity {d.get('integrity_grounded')} grounded, {d.get('registry_served')} registry-served. "
            f"Missing: {', '.join(d.get('missing') or []) or 'none'}.")
    return SkillEnvelope(text=text, sources=[], signal="corpus_only", extra={"payor_readiness": d})


LOOKUP_SPEC = SkillSpec(
    name="payor_lookup",
    description=(
        "Authoritative operational fact for a payor from the payor registry — the "
        "SOURCE OF TRUTH for contact/access facts the corpus can't reliably ground. "
        "Use for: provider services phone, appeals/claims fax, EDI payer ID, provider "
        "portal, login/eligibility/prior-auth URLs, mailing addresses, timely filing. "
        "Prefer this over search_corpus for these. `field` accepts natural aliases "
        "(phone, appeals fax, edi, portal, prior auth, timely filing)."),
    handler=_lookup,
    inputs_schema={"type": "object", "properties": {
        "payor": {"type": "string"}, "field": {"type": "string"}}, "required": ["field"]},
    requires_jurisdiction=False, follow_up_capable=True, source="builtin",
    visible_to_planner=True, category="healthcare", display_name="Payor Fact Lookup")

READINESS_SPEC = SkillSpec(
    name="payor_readiness",
    description="Payor readiness scorecard: how much of a payor's required docs are ingested, "
                "known coverage, document-integrity grounded count, and gaps.",
    handler=_readiness,
    inputs_schema={"type": "object", "properties": {"payor": {"type": "string"}}},
    requires_jurisdiction=False, follow_up_capable=False, source="builtin",
    visible_to_planner=True, category="healthcare", display_name="Payor Readiness")

register(LOOKUP_SPEC)
register(READINESS_SPEC)
