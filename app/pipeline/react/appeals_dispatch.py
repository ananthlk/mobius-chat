"""Appeals Agent dispatch — the five appeals tools, over direct HTTP.

EXTRACTED FROM react_loop._execute_tool, 2026-09-16 (Phase 1i, continuing the
split the ratchet asks for). 590 lines, lifted whole: the code is unchanged
except for its indentation and the four names it used to close over, which are
now parameters.

WHY THESE FIVE LIVE HERE RATHER THAN BEHIND MCP: they bypass MCP and call the
appeals REST API directly so the router treats them as Tier 1, the same weight
as rag. That decision is upstream of this module and unchanged by the move.

Closure dependencies, measured before extracting rather than guessed: the block
referenced exactly `tool`, `inputs`, `emit` and one write to `ctx.recital`.
Everything else it needs (httpx, re, logger, RETRIEVAL_SIGNAL_NO_SOURCES) is
module-level and imported here.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any
from urllib.parse import quote as _urlquote

import httpx

from app.services.doc_assembly import RETRIEVAL_SIGNAL_NO_SOURCES

logger = logging.getLogger(__name__)

#: The tools this module serves. react_loop dispatches on this set, so the
#: membership test lives with the implementation rather than being restated by
#: the caller — two copies of a tool list is how a tool becomes unreachable.
APPEALS_TOOLS: frozenset[str] = frozenset({
    "appeals_find_carc", "appeals_lookup_rules", "appeals_get_playbook",
    "appeals_validate_claim", "appeals_assemble_letter",
})


def dispatch(tool: str, inputs: dict, ctx: Any, emit) -> dict | None:
    """Run one appeals tool. Returns the tool-result dict, or None if the
    block falls through without producing one (the caller then continues its
    own dispatch chain, exactly as before the extraction)."""
    if tool in {
        "appeals_find_carc", "appeals_lookup_rules", "appeals_get_playbook",
        "appeals_validate_claim", "appeals_assemble_letter",
    }:
        import json as _json
        import os as _os

        _appeals_base = (
            _os.environ.get("APPEALS_AGENT_URL", "")
            or "https://mobius-appeals-prototype-ortabkknqa-uc.a.run.app"
        ).rstrip("/")

        # 🔴 "NOT IN THE LIBRARY" IS AN ANSWER, NOT A FAILURE.
        #
        # The appeals service returns 404 with {"detail": "CARC 24 not in
        # library"} for a code it does not cover -- it holds 18 CARCs and 24 is
        # not among them. raise_for_status turned that into an exception, the
        # handler turned it into "[appeals_lookup_rules] Error: ...", and react
        # reported a broken tool. Measured live 2026-09-15: the user's answer to
        # "How do I appeal a CARC 24 denial?" was
        #     "I attempted to look up appeal rules ... but the tools failed to
        #      execute correctly."
        # A confession about our internals, when the truth was a fact about the
        # library that the person could act on.
        #
        # This is the empty-versus-could_not_run distinction that Tool Manifest
        # built into the executor, arriving in chat's own branch: a 404 that
        # NAMES the gap is the library answering; anything else is us failing.
        class _AppealsNotInLibrary(Exception):
            """The service answered, and the answer is that it does not cover
            this code. Carries the service's own words rather than ours."""
            def __init__(self, detail: str):
                self.detail = detail
                super().__init__(detail)

        def _appeals_get(path: str, **params):
            with httpx.Client(timeout=30.0) as _c:
                _r = _c.get(f"{_appeals_base}{path}", params={k: v for k, v in params.items() if v is not None})
                if _r.status_code == 404:
                    _detail = ""
                    try:
                        _detail = str((_r.json() or {}).get("detail") or "")
                    except Exception:
                        _detail = ""
                    # Only a NAMED gap. A bare 404 ("Not Found") is a wrong URL
                    # -- our defect -- and must keep failing loudly rather than
                    # being reported to the user as an absence in the library.
                    if "not in library" in _detail.lower():
                        raise _AppealsNotInLibrary(_detail)
                _r.raise_for_status()
                return _r.json()

        def _appeals_post(path: str, body: dict):
            with httpx.Client(timeout=120.0) as _c:
                _r = _c.post(f"{_appeals_base}{path}", json=body)
                _r.raise_for_status()
                return _r.json()

        def _no_src():
            return {"tool": tool, "success": False, "result": f"[{tool}] failed", "signal": RETRIEVAL_SIGNAL_NO_SOURCES, "sources": []}

        try:
            if tool == "appeals_find_carc":
                desc = (inputs.get("denial_description") or "").strip()
                payor = (inputs.get("payor") or "").strip()
                emit(f"◌ Identifying denial code from: {desc[:60]}…")
                all_carcs = _appeals_get("/carc")
                desc_lower = desc.lower()
                _KEYWORD_HINTS = [
                    (["cob","coordination","secondary","primary insurance","other insurance","medicare primary"], "cob_secondary_payor"),
                    (["timely","filing","late","deadline","past deadline","time limit"], "timely_filing"),
                    (["auth","authorization","prior auth","pre-auth","not authorized","not approved"], "auth_required"),
                    (["missing","documentation","records","not on file","incomplete","no referral"], "missing_information"),
                    (["duplicate","already paid","previously processed","same claim"], "duplicate"),
                    (["not covered","exclusion","not a covered"], "not_covered"),
                    (["eligibility","not eligible","not enrolled","member not","no coverage"], "eligibility"),
                    (["coding","unbundling","modifier","cpt","hcpcs","procedure code"], "coding_mismatch"),
                    (["bundled","inclusive","component","included in"], "bundled_service"),
                    (["fee schedule","rate","allowed amount","maximum allowable"], "fee_schedule"),
                    (["medical necessity","not medically necessary","experimental"], "medical_necessity"),
                    (["referral","referral not","no referral","referral required"], "referral"),
                    (["provider type","not credentialed","out of network","not participating"], "provider_type"),
                    (["deductible","copay","coinsurance","cost share"], "cost_share"),
                ]
                arch_scores: dict[str, int] = {}
                for kws, arch in _KEYWORD_HINTS:
                    score = sum(1 for kw in kws if kw in desc_lower)
                    if score: arch_scores[arch] = arch_scores.get(arch, 0) + score
                scored = []
                for entry in all_carcs:
                    c = entry.get("carc", 0)
                    title_lower = (entry.get("title") or "").lower()
                    s = arch_scores.get(entry.get("archetype",""), 0)*2 + sum(1 for w in desc_lower.split() if len(w)>3 and w in title_lower)
                    if s > 0: scored.append((s, entry))
                scored.sort(key=lambda x: -x[0])
                matches = []
                for _, entry in scored[:4]:
                    try:
                        rd = _appeals_get(f"/rules/{entry['carc']}")
                        rules = rd.get("rules", []) if isinstance(rd, dict) else rd
                    except Exception:
                        rules = []
                    matches.append({
                        "carc": entry["carc"], "title": entry.get("title",""),
                        "archetype": entry.get("archetype",""), "rule_count": len(rules), "rules": rules,
                    })
                top = matches[0] if matches else {}
                result_data = {
                    "matches": matches, "top_carc": top.get("carc"),
                    "top_archetype": top.get("archetype",""),
                    "suggestion": (
                        f"Most likely CARC {top.get('carc')} ({top.get('title','')}). "
                        f"Each rule's appeal_argument is the assertion to make in the letter."
                    ) if top else "Could not identify CARC — check the EOB for the exact code.",
                }
                emit(f"✓ Likely CARC {top.get('carc')} — {top.get('title','')[:50]}" if top else "⊘ Could not identify denial code")
                return {
                    "tool": tool, "success": bool(matches),
                    "result": _json.dumps(result_data),
                    "signal": None if matches else RETRIEVAL_SIGNAL_NO_SOURCES,
                    "sources": [],
                    "section_hint": {"section_format": "appeals_rules", "label": "Appeal rules",
                                     "data": {**result_data, "admin_url": f"{_appeals_base}/admin/rules-library"}} if matches else None,
                }

            if tool == "appeals_lookup_rules":
                carc = inputs.get("carc") or inputs.get("code")
                payor = (inputs.get("payor") or "").strip() or None
                if not carc:
                    return {**_no_src(), "result": "[appeals_lookup_rules] carc is required"}
                emit(f"◌ Looking up CARC {carc} rules…")
                data = _appeals_get(f"/rules/{carc}", payor=payor)
                rules = data.get("rules", []) if isinstance(data, dict) else data
                n = len(rules)
                carc_info = _appeals_get(f"/carc-config/{carc}")
                result_data = {
                    "carc": carc,
                    "carc_title": carc_info.get("title", f"CARC {carc}") if isinstance(carc_info, dict) else f"CARC {carc}",
                    "archetype": carc_info.get("archetype", "") if isinstance(carc_info, dict) else "",
                    "payor": payor or "all",
                    "rules_found": n,
                    "rules": rules,
                }
                emit(f"✓ {n} rule{'s' if n!=1 else ''} for CARC {carc}")
                return {
                    "tool": tool, "success": n > 0,
                    "result": _json.dumps(result_data),
                    "signal": None if n > 0 else RETRIEVAL_SIGNAL_NO_SOURCES,
                    "sources": [],
                    "section_hint": {"section_format": "appeals_rules", "label": "Appeal rules", "data": {**result_data, "admin_url": f"{_appeals_base}/admin/rules-library"}} if n > 0 else None,
                }

            if tool == "appeals_get_playbook":
                # RAW, before any normalisation. The experiment turns on the
                # difference between what the model emitted and what we sent —
                # logging a stripped/case-folded copy would record what we think
                # was sent rather than what was sent, which is exactly the gap
                # being measured.
                _payor_raw = inputs.get("payor")
                payor = (inputs.get("payor") or "").strip()
                carc_group = (inputs.get("carc_group") or "").strip()
                carc = inputs.get("carc") or 0
                # PRECEDENCE BUG, fixed 2026-09-10. This was:
                #     lookup = carc_group or str(carc) if carc else carc_group
                # which Python parses as `(carc_group or str(carc)) if carc else
                # carc_group` — a conditional expression binds looser than `or`.
                # So whenever the model emitted a carc_group it WON over a
                # correct numeric carc. The endpoint is case-sensitive: `197`
                # hits, `PRECERT` hits, `precert` returns {}. A turn carrying
                # the right code sent the wrong key and reported "no playbook"
                # while holding the answer.
                #
                # That is the sporadic miss, and it explains the sporadic part:
                # whether a turn hit depended on whether the model happened to
                # emit a group. Live case, "how do i appeal a carc 197 denial
                # for sunshine health" — playbook id 55 exists, deadline 90d.
                #
                # NUMERIC FIRST because it is exact and unambiguous; the group
                # is a normalised fallback. BOTH are tried, because a miss is
                # 200 WITH {} rather than a 404, so the 404-only retry below
                # could never fire for this case.
                _lookups: list[str] = []
                if carc:
                    _lookups.append(str(carc))
                if carc_group:
                    _grp = carc_group.upper()          # endpoint is case-sensitive
                    if _grp not in _lookups:
                        _lookups.append(_grp)
                lookup = _lookups[0] if _lookups else ""
                if not payor or not lookup:
                    return {**_no_src(), "result": "[appeals_get_playbook] payor and (carc_group or carc) are required"}
                emit(f"◌ Checking {payor} playbook…")
                # The payor key is FREE TEXT from the model — whatever it
                # inferred from the user's phrasing — and appeals matches it
                # EXACTLY. "Sunshine Health" hits; "sunshine health", "Sunshine"
                # and "Sunshine Health Plan" all return 200 with {}, which is
                # byte-identical to "this payor has no playbook". So a name the
                # user happened to phrase differently reads as a missing
                # playbook, and that alone reproduces the sporadic-selection
                # symptom without any manifest, parsing or executor fault.
                # Recorded so the next empty result can be attributed instead of
                # guessed at. quote() because the payor is interpolated into a
                # URL path and previously was not encoded at all.
                from urllib.parse import quote as _q
                _pb_reason = ""
                # TRANSPORT vs CONTENT, kept separate on purpose.
                #   _call_ok  — did the request work at all
                #   found     — did a playbook actually come back
                # `success` binds to _call_ok. A payor with genuinely no
                # playbook is a CORRECT answer from a working tool, and
                # recording it as `appeals_get_playbook:failure` would corrupt
                # the dispatch funnel with a failure that did not happen.
                # (Caught by test_appeals_playbook_zero_result, which encodes
                # exactly this distinction — I had briefly collapsed the two.)
                _call_ok = True
                try:
                    # P0-0(d): the GUARDED read. Chat declares its audience —
                    # provider — from its own context; it is never inferred.
                    # Member-party actions (an enrollee fair-hearing rung in a
                    # provider ladder) are invisible unless they carry a consent
                    # artifact. Undeclared audience serves nothing. Reading the
                    # unguarded /playbook here is what let a member remedy render
                    # as rung 4 of a provider ladder in live customer output.
                    #
                    # audience="provider" is NOT optional and must not be dropped
                    # in a refactor: the route defaults it to "", which appeals
                    # currently fails OPEN on — scalars pass through, so a
                    # dropped argument leaks PROVIDER deadlines and fax numbers
                    # rather than serving nothing. Guarded by a test.
                    pb, _hit_key = {}, ""
                    for _k in _lookups:
                        # A miss is 200 with {}, so "did this key work?" cannot
                        # be answered by exception handling — it must be checked.
                        _r = _appeals_get(
                            f"/playbook-guarded/{_q(payor, safe='')}/{_q(_k, safe='')}",
                            audience="provider")
                        if isinstance(_r, dict) and _r:
                            pb, _hit_key = _r, _k
                            break
                    if _hit_key and _hit_key != lookup:
                        logger.info(
                            "[appeals_get_playbook] alternate key hit: tried=%s hit=%r "
                            "carc=%r carc_group=%r", _lookups, _hit_key, carc, carc_group)
                except httpx.HTTPStatusError as _e:
                    if _e.response.status_code == 404 and carc and carc_group:
                        try:
                            pb = _appeals_get(f"/playbook-guarded/{_q(payor, safe='')}/{_q(str(carc), safe='')}",
                                              audience="provider")
                        except Exception:
                            pb, _pb_reason = {}, "unsourced"
                    else:
                        pb, _pb_reason = {}, "unsourced"
                except Exception:
                    _call_ok = False
                    # Transport/timeout — the resolver could not answer. NOT the
                    # same as "there is no playbook", and the user should be told
                    # to retry rather than told nothing exists.
                    pb, _pb_reason = {}, "resolver_unavailable"
                if not isinstance(pb, dict):
                    pb, _pb_reason = {}, "unsourced"
                # A MISS IS 200 WITH {}, NEVER A 404. `found` used to be set from
                # HTTP success, so an empty body was recorded as "found" — the
                # lookup reporting a hit for a payor key that matched nothing.
                found = bool(pb)
                if not found and not _pb_reason:
                    _pb_reason = "unsourced"
                # PURPOSE UPDATED 2026-09-10 — this is no longer chasing payor
                # names. The free-text-payor-key hypothesis is RETIRED on
                # evidence: 121 paired observations recovered from
                # chat_turns.thinking_log (Feb-Sep) showed empties clustering on
                # `Sunshine Health`, the EXACT display name (23 of 25), while the
                # non-exact strings that appeared — `sunshine-health`,
                # `FL Medicaid` — returned USABLE. The prediction inverted.
                #
                # It stays because it records `lookup` beside the payor, which
                # settles the replacement hypothesis: a playbook is keyed
                # (payor x CARC) and appeals covers 72 CARCs, so a CARC outside
                # that set is a LEGITIMATE miss — which is what turn 143309c1
                # turned out to be. The retrospective emit data carries the payor
                # but not the CARC, so only this can answer it.
                #
                # `_payor_raw` is recorded UNMODIFIED alongside the sent value
                # so a model emitting " Sunshine " or "sunshine health" is
                # visible as itself. No normaliser is built here on purpose:
                # the canonical payor mapping lives in the Lexicon, and chat
                # inventing its own name-matching would be the same mistake as
                # chat inventing an FL Medicaid deadline — a service with no
                # payor data making a payor judgement.
                try:
                    from app.telemetry.spans import record as _rec_pb, KIND_TOOL_ARG
                    _raw_repr = "" if _payor_raw is None else str(_payor_raw)
                    _drift = "" if _raw_repr == payor else f" raw={_raw_repr[:40]!r}"
                    _rec_pb(ctx, KIND_TOOL_ARG,
                            f"appeals_get_playbook payor={payor[:60]!r}"
                            f" lookup={str(lookup)[:20]!r}"
                            f" -> {'nonempty' if found else 'empty'}{_drift}")
                except Exception:
                    pass
                #
                # NO INVENTED DEFAULT. This branch used to substitute
                # "Default FL Medicaid: 60 days, certified mail." for a missing
                # playbook. Removed, on the appeals seat's ruling and for the
                # reason they gave: a wrong appeal deadline is UNRECOVERABLE —
                # it loses the claim — and it was being generated by a service
                # with no payor data at all. Worse, those FL Medicaid deadlines
                # are blank BECAUSE appeals deliberately removed fabricated ones;
                # refilling the gap at render time made their remediation
                # invisible to the user. And "default FL Medicaid" is not a
                # well-formed idea: a deadline resolves on payer × product_line ×
                # state × network_status × audience × appeal_level × request_type
                # as of the denial date — a fair hearing, a plan appeal, a
                # provider claim dispute and a UM denial are four different
                # clocks. Render the REASON, never a number.
                result_data = {"found": found, **pb}
                if _pb_reason:
                    result_data["reason"] = _pb_reason
                # 2026-08-07 (Ananth, directly, live-query finding): this used
                # to report success=True + signal=None whenever the HTTP call
                # succeeded, even when the fetched playbook had neither a
                # deadline nor a submission method -- indistinguishable from a
                # real hit to ReactRetryGuard._is_zero_result (only fires on
                # signal=="no_sources"), so consecutive_failures_per_tool never
                # incremented and the tool-exhaustion block never fired.
                # Confirmed live: 7 consecutive appeals_get_playbook calls, all
                # "found" but content-empty ("?d deadline · "), burned rounds
                # 2-9 of a 10-round budget with zero loop-detection -- the
                # retry-guard machinery that should have caught this already
                # exists, it just never saw a failure signal. Mirrors
                # appeals_find_carc's existing pattern (n>0 -> None else
                # RETRIEVAL_SIGNAL_NO_SOURCES) immediately above -- this
                # handler had just never adopted it.
                days = pb.get("deadline_appeal_days") if found else None
                method = (pb.get("submission_method") or "").strip() if found else ""
                # PER FIELD, not OR-ed. `found and (days is not None or method)`
                # passed the exact case it was built to catch: FL Medicaid rows
                # carry submission_method="portal" with deadline_appeal_days=null
                # — 72 of 141 rows, 51% of the corpus — so a DEADLINE question
                # was answered "usable" from a playbook with no deadline. OR-ing
                # two independent facts into one boolean means the present field
                # vouches for the absent one.
                has_deadline = days is not None
                has_method = bool(method)
                # `usable` is retained for the retrieval signal, and now means
                # "at least one filing-critical field is actually present" —
                # the honest floor. Which field the ANSWER needs is decided by
                # the question, so both flags travel in the payload rather than
                # being collapsed here where the question is not known.
                usable = found and (has_deadline or has_method)
                # Actually put them in the payload — the model needs to see
                # WHICH field is missing to answer a deadline question honestly
                # from a playbook that only has a submission method.
                result_data["has_deadline"] = has_deadline
                result_data["has_submission_method"] = has_method
                if usable:
                    emit(f"✓ {payor} playbook: {days if days is not None else '?'}d deadline · {method}")
                elif found:
                    emit(f"⚠ {payor} playbook found but has no deadline/method data")
                else:
                    emit(f"✓ No playbook for {payor} — using FL defaults")
                # Golden-answer enrichment (W3.5 gate): attach the canonical
                # questions for this CARC×payor plus the admin deep link, so
                # the FE card renders the full contract (questions preview,
                # confidence badge, admin chip). All optional fields per the
                # locked AppealsPlaybookData contract — absent on error.
                if usable:
                    try:
                        _q_carc = carc or (pb.get("carc_codes") or [0])[0]
                        if _q_carc:
                            _qs = _appeals_get(f"/questions/{_q_carc}", payor=payor, min_level=0)
                            _q_list = (_qs.get("questions") or [])[:5]
                            _tops = [q for q in _q_list if not q.get("parent_question_id")]
                            if _tops:
                                # Guidance statements (Ananth 2026-08-10): chat
                                # informs with statements, the workbench asks
                                # questions. Emit guidance[] when authored;
                                # questions[] stays for backward compat.
                                def _detail_for(_stmt: str, _man: str) -> str | None:
                                    # manual_guidance often restates the
                                    # statement almost verbatim (observed on
                                    # thin-payor-fact CARCs like 22) — drop it
                                    # rather than render the same sentence twice.
                                    _m = (_man or "").strip()
                                    if not _m:
                                        return None
                                    _a, _b = _stmt.lower()[:60], _m.lower()[:60]
                                    if _a and (_a in _m.lower() or _b in _stmt.lower()):
                                        return None
                                    return _m[:140]

                                _guid = [
                                    {"n": i + 1, "text": (q.get("guidance_statement") or "").strip(),
                                     "detail": _detail_for(q.get("guidance_statement") or "",
                                                           q.get("manual_guidance") or "")}
                                    for i, q in enumerate(_tops)
                                    if (q.get("guidance_statement") or "").strip()
                                ]
                                if _guid:
                                    result_data["guidance"] = _guid
                                result_data["questions"] = [
                                    {"n": i + 1, "text": q.get("question_text", ""),
                                     "hint": (q.get("manual_guidance") or "")[:140] or None}
                                    for i, q in enumerate(_tops)
                                ]
                    except Exception as _q_exc:
                        # questions are optional; card degrades cleanly — but
                        # NEVER silently (Chat FE observed intermittent
                        # enrichment absence; this log is the diagnostic).
                        logger.warning(
                            "[appeals-enrich] questions fetch failed carc=%s payor=%s cid=%s: %s",
                            locals().get("_q_carc", carc), payor,
                            getattr(ctx, "correlation_id", "?"), _q_exc)
                    # Don't advertise a submission channel we can't point to.
                    # Several playbooks carry submission_method="portal" with an
                    # EMPTY portal_url (e.g. CARC 151 x Sunshine) — the card then
                    # tells a biller to use a portal it can't link (Ananth review
                    # 2026-08-10). Drop unsupported channels from the claim.
                    _sm = (result_data.get("submission_method") or "").strip()
                    if _sm:
                        _have = {
                            "portal": bool((result_data.get("portal_url") or "").strip()),
                            "fax": bool((result_data.get("fax") or "").strip()),
                            "mail": bool((result_data.get("mail_address") or "").strip()),
                        }
                        _kept = [
                            _p for _p in
                            (_x.strip() for _x in re.split(r"[,/;|]| or | and ", _sm))
                            if _p and _have.get(_p.lower(), True)
                        ]
                        _new_sm = ", ".join(_kept)
                        if _new_sm != _sm:
                            result_data["submission_method"] = _new_sm
                            logger.info(
                                "[appeals-enrich] pruned unsupported submission channels "
                                "%r -> %r (carc=%s payor=%s)", _sm, _new_sm, carc, payor)

                    # Card section 1 — "what is this denial": human title for
                    # the CARC so the card leads with meaning, not codes.
                    # Prefer the authored PLAIN-LANGUAGE description over the
                    # official CARC title — the title is payer jargon restated
                    # ("Information submitted does not support this many/
                    # frequency of services") and tells a biller nothing
                    # (Ananth review 2026-08-10). Title stays as fallback.
                    try:
                        _cfg_carc = carc or (pb.get("carc_codes") or [0])[0]
                        if _cfg_carc:
                            try:
                                _cd = _appeals_get(f"/carc-description/{_cfg_carc}")
                            except Exception:
                                _cd = {}
                            if (_cd.get("plain_description") or "").strip():
                                result_data.setdefault("description", _cd["plain_description"])
                                if (_cd.get("what_it_usually_means") or "").strip():
                                    result_data.setdefault(
                                        "what_it_usually_means", _cd["what_it_usually_means"])
                            else:
                                _cfg = _appeals_get(f"/carc-config/{_cfg_carc}")
                                if _cfg.get("title"):
                                    result_data.setdefault("description", _cfg["title"])
                    except Exception as _d_exc:
                        logger.warning("[appeals-enrich] carc description failed carc=%s: %s",
                                       carc, _d_exc)
                    from urllib.parse import quote as _urlquote

                    # Mode chooser — "start a new appeal in the Appeals Agent"
                    # is now standard on every playbook card (Ananth 2026-08-10).
                    # Ordered essentials -> copilot -> agentic (escalation ladder
                    # AND build order). Self-serve is live today: it hands off to
                    # the workbench with claim context prefilled. Copilot/agentic
                    # render visible-but-disabled with honest reasons until the
                    # assessment engine (M1) and data integrations exist.
                    _case_q = "&".join(
                        f"{_k}={_urlquote(str(_v))}"
                        for _k, _v in (("carc", carc or ""), ("payor", payor),
                                       ("src", "chat"), ("mode", "self_serve"))
                        if _v not in ("", None)
                    )
                    result_data.setdefault("modes", [
                        {"mode": "self_serve", "available": True,
                         "action": {"kind": "case_link",
                                    "url": f"{_appeals_base}/?{_case_q}"}},
                        {"mode": "copilot", "available": False,
                         "reason": "Guided assessment is in build — coming soon"},
                        {"mode": "agentic", "available": False,
                         "reason": "Needs claims-data integration for your org"},
                    ])

                    result_data.setdefault(
                        "admin_url",
                        f"{_appeals_base}/admin/rules-library?carc={carc or ''}"
                        f"&payor={_urlquote(payor)}&tab=playbook")
                return {
                    "tool": tool, "success": _call_ok,
                    "result": _json.dumps(result_data),
                    "signal": None if usable else RETRIEVAL_SIGNAL_NO_SOURCES,
                    "sources": [],
                    "section_hint": (
                        {"section_format": "appeals_playbook", "label": "Appeal playbook", "data": result_data}
                        if usable else None
                    ),
                }

            if tool == "appeals_validate_claim":
                carc = inputs.get("carc")
                if not carc:
                    return {**_no_src(), "result": "[appeals_validate_claim] carc is required"}
                emit(f"◌ Running AI recommendation for CARC {carc}…")
                try:
                    rules_raw = _appeals_get(f"/rules/{carc}")
                    rules = rules_raw.get("rules", [])[:8] if isinstance(rules_raw, dict) else rules_raw[:8]
                except Exception:
                    rules = []
                body = {
                    "carc": carc, "payor": inputs.get("payor") or "", "amount": inputs.get("amount") or "",
                    "dos": inputs.get("dos") or "",
                    "inv_signals": inputs.get("inv_signals") or {},
                    "rules": [{"rule_id": r.get("rule_id",""), "rule_name": r.get("rule_name",""),
                               "rule_statement": r.get("rule_statement",""), "triggers_when": r.get("triggers_when",""),
                               "appeal_argument": r.get("appeal_argument","")} for r in rules],
                }
                result = _appeals_post("/validate-rules", body)
                action = result.get("action", "appeal")
                conf = result.get("confidence", "medium")
                emit(f"✓ Recommendation: {action} ({conf})")
                return {
                    "tool": tool, "success": True,
                    "result": _json.dumps(result),
                    "signal": None,
                    "sources": [],
                }

            if tool == "appeals_assemble_letter":
                carc = inputs.get("carc")
                if not carc:
                    return {**_no_src(), "result": "[appeals_assemble_letter] carc is required"}
                emit(f"◌ Assembling appeal letter for CARC {carc} / {inputs.get('payor','?')} — takes 30–90s…")
                body = {k: inputs.get(k) for k in [
                    "carc","payor","amount","dos","denial_date","carc_group",
                    "action_items","inv_signals","action_path","session_id",
                ] if inputs.get(k) is not None}
                result = _appeals_post("/assemble", body)
                letter = result.get("letter_draft") or result.get("letter") or ""
                wc = len(letter.split()) if letter else 0
                emit(f"✓ Letter assembled ({wc} words)")
                if letter:
                    # RECITAL verbatim passthrough (Chat Architecture, 2026-08-06,
                    # LLM Agent coordinating the enricher side). Root cause: a
                    # fully-assembled legal letter was flowing through TWO lossy
                    # paraphrase passes -- react's own "write an answer" LLM step,
                    # then the integrator's enricher LLM step -- either of which
                    # can silently drop or reword content that must survive
                    # verbatim. `ctx.recital` reuses the SAME mechanism the
                    # skill-registry dispatch path already sets from
                    # env.extra["recital"] (see the `if _skill_registry.has(tool)`
                    # branch above) -- integrate.py's existing post-process step
                    # reads it, sets mode="RECITAL", and injects recital.verbatim
                    # into the final card regardless of what the enricher's LLM
                    # call produces for direct_answer/sections. `is_terminal`
                    # (below) stops react's OWN reasoning from getting a chance
                    # to paraphrase it into a prose "answer" first -- it's the
                    # same flag `refuse` sets, but WITHOUT react_bypass_integrate:
                    # refuse skips the integrator entirely (a bare status string,
                    # no AnswerCard); this needs the integrator to still run and
                    # build a real card (citations, next_steps, mode=RECITAL
                    # chrome) around the untouched letter.
                    ctx.recital = {"verbatim": True, "text": letter}  # type: ignore[attr-defined]
                return {
                    "tool": tool, "success": bool(letter),
                    "result": letter or _json.dumps(result),
                    "signal": None if letter else RETRIEVAL_SIGNAL_NO_SOURCES,
                    "sources": [],
                    "is_terminal": bool(letter),
                }

        except _AppealsNotInLibrary as _nil:
            # An EARNED absence: the library was asked and does not cover it.
            # success=False with the no-sources signal, same shape as any other
            # honest empty, so the retry guard sees it and react does not
            # re-ask -- and the text says what is missing, so the answer can be
            # "we have no appeal rules for this code" plus the payer's general
            # process, instead of "the tools failed".
            emit(f"  ↓ {tool}: {_nil.detail}")
            return {**_no_src(),
                    "result": (f"[{tool}] {_nil.detail}. The appeals library "
                               f"does not cover this code — answer from the "
                               f"payer's general appeal process instead, and "
                               f"say the code-specific rules are unavailable."),
                    "signal": RETRIEVAL_SIGNAL_NO_SOURCES}
        except Exception as _exc:
            emit(f"⊘ {tool} error: {_exc}")
            return {**_no_src(), "result": f"[{tool}] Error: {_exc}"}

    return None
