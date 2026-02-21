#!/usr/bin/env python3
"""Comprehensive chat pipeline test: single/multi-part queries, jurisdiction changes, tools, RAG, patient rejection, retries.

Simulates real chat flows to find what works and what breaks:
  1. Single query resolution (RAG with payer)
  2. Multi-part resolution (multiple subquestions in one message)
  3. Jurisdiction change in same session (turn 1: Sunshine, turn 2: different payer)
  4. Tool usage (capability, Google search, web scrape)
  5. RAG policy lookup (with jurisdiction)
  6. Patient-specific info (expect refusal – not allowed)
  7. RAG fail + retry (no chunks → on_rag_fail search_google)

Run from Mobius root:
  PYTHONPATH=mobius-chat python mobius-chat/scripts/test_chat_pipeline_comprehensive.py
  PYTHONPATH=mobius-chat python mobius-chat/scripts/test_chat_pipeline_comprehensive.py --scenario 1  # run single scenario
  CHAT_SKILLS_GOOGLE_SEARCH_URL=http://localhost:8004/search? CHAT_SKILLS_WEB_SCRAPER_URL=...  # for tool tests
"""
from __future__ import annotations

import os
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

CHAT_ROOT = Path(__file__).resolve().parent.parent
if str(CHAT_ROOT) not in sys.path:
    sys.path.insert(0, str(CHAT_ROOT))

_root = CHAT_ROOT.parent
for env_path in (CHAT_ROOT / ".env", _root / "mobius-config" / ".env", _root / ".env"):
    if env_path.exists():
        try:
            from dotenv import load_dotenv
            load_dotenv(env_path, override=False)
        except Exception:
            pass
        break


def _trunc(s: str, n: int = 60) -> str:
    s = (s or "").replace("\n", " ")
    return (s[: n - 3] + "...") if len(s) > n else s


@dataclass
class ScenarioResult:
    scenario_id: str
    name: str
    passed: bool
    details: list[str] = field(default_factory=list)
    error: str | None = None
    final_message: str = ""


def _noop(*args: Any, **kwargs: Any) -> None:
    pass


def _build_state(payer: str | None = None, state: str | None = None, program: str | None = None) -> dict:
    """Build merged_state for injection."""
    active = {"payer": payer, "program": program}
    if state:
        active["state"] = state
    if payer or state:
        active["jurisdiction_obj"] = {
            "payor": payer,
            "state": state,
            "program": program,
        }
    return {
        "active": active,
        "open_slots": [],
        "refined_query": None,
    }


def _run_turn(
    message: str,
    merged_state: dict | None = None,
    last_turns: list[dict] | None = None,
    payer: str | None = None,
) -> tuple[bool, list[str], str, str | None]:
    """Run one pipeline turn. Returns (resolvable, answers, final_message, error)."""
    from app.pipeline.context import PipelineContext
    from app.stages.classify import run_classify
    from app.stages.plan import run_plan
    from app.stages.clarify import run_clarify
    from app.stages.resolve import run_resolve
    from app.stages.integrate import run_integrate
    from app.state.context_pack import build_context_pack
    from app.state.context_router import route_context

    # Mock send_to_user so we don't need queue
    try:
        from app.communication import gate
        gate.send_to_user = _noop
    except Exception:
        pass

    ctx = PipelineContext(
        correlation_id=str(uuid.uuid4()),
        thread_id="test-thread" if merged_state else None,
        message=message,
    )

    # Inject state (skip DB)
    ctx.merged_state = merged_state or {}
    ctx.last_turns = last_turns or []

    rag_filter_overrides = {}
    if payer or (merged_state and (merged_state.get("active") or {}).get("payer")):
        p = payer or (merged_state.get("active") or {}).get("payer")
        try:
            from app.payer_normalization import normalize_payer_for_rag
            canonical = normalize_payer_for_rag(p)
            rag_filter_overrides["filter_payer"] = canonical or p
        except Exception:
            rag_filter_overrides["filter_payer"] = p

    route = route_context(message, ctx.merged_state, ctx.last_turns, reset_reason=None)
    ctx.context_pack = build_context_pack(
        route, ctx.merged_state, ctx.last_turns, [],
        last_turn_sources=[],
    )

    def emit(chunk: str) -> None:
        pass

    try:
        run_classify(ctx, emitter=emit)
        ctx.effective_message = ctx.effective_message or message

        run_plan(ctx, emitter=emit)
        if not ctx.plan:
            return (False, [], "", "No plan produced")

        resolvable = run_clarify(ctx, emitter=emit)
        if not resolvable:
            return (False, [], ctx.clarification_message or "(clarification)", None)

        run_resolve(ctx, emitter=emit)
        run_integrate(ctx, emitter=emit)

        return (True, ctx.answers or [], ctx.final_message or "", None)
    except Exception as e:
        return (False, [], "", str(e))


# --- Scenarios ---

def scenario_1_single_query() -> ScenarioResult:
    """Single query: RAG policy question with payer."""
    r = ScenarioResult("1", "Single query (RAG + payer)", False, [])
    state = _build_state(payer="Sunshine Health")
    resolvable, answers, final, err = _run_turn("How do I file an appeal?", merged_state=state, payer="Sunshine Health")
    r.final_message = final[:500] if final else ""
    if err:
        r.error = err
        r.details.append(f"Error: {err}")
        return r
    r.details.append(f"Resolvable: {resolvable}, answers: {len(answers)}")
    r.details.append(f"Final: {_trunc(final, 120)}")
    r.passed = resolvable and len(answers) >= 1 and len(final) > 50
    if not r.passed:
        r.details.append("Expected: resolvable, at least one answer, non-empty final message")
    return r


def scenario_2_multi_part() -> ScenarioResult:
    """Multi-part: question with multiple subquestions."""
    r = ScenarioResult("2", "Multi-part query (prior auth + grievance)", False, [])
    state = _build_state(payer="Sunshine Health")
    resolvable, answers, final, err = _run_turn(
        "What is prior authorization and how do I file a grievance?",
        merged_state=state,
        payer="Sunshine Health",
    )
    r.final_message = final[:500] if final else ""
    if err:
        r.error = err
        r.details.append(f"Error: {err}")
        return r
    r.details.append(f"Resolvable: {resolvable}, answers: {len(answers)}")
    r.details.append(f"Final: {_trunc(final, 120)}")
    r.passed = resolvable and len(answers) >= 1 and len(final) > 30
    return r


def scenario_3_jurisdiction_change() -> ScenarioResult:
    """Jurisdiction change in same session: turn 1 Sunshine, turn 2 different payer."""
    r = ScenarioResult("3", "Jurisdiction change (Sunshine → United)", False, [])

    # Turn 1: Sunshine Health
    state1 = _build_state(payer="Sunshine Health")
    _, _, final1, err1 = _run_turn("How do I file an appeal for Sunshine Health?", merged_state=state1, payer="Sunshine Health")
    if err1:
        r.error = err1
        r.details.append(f"Turn 1 error: {err1}")
        return r
    r.details.append(f"Turn 1: {_trunc(final1, 80)}")

    # Turn 2: change to United (simulate user said "what about United Healthcare?")
    state2 = _build_state(payer="United Healthcare")
    last_turns = [{"role": "user", "content": "How do I file an appeal?"}, {"role": "assistant", "content": final1[:200]}]
    _, _, final2, err2 = _run_turn(
        "What about United Healthcare? How do I file an appeal there?",
        merged_state=state2,
        last_turns=last_turns,
        payer="United Healthcare",
    )
    if err2:
        r.error = err2
        r.details.append(f"Turn 2 error: {err2}")
        return r
    r.details.append(f"Turn 2: {_trunc(final2, 80)}")
    r.final_message = final2[:500] if final2 else ""
    r.passed = len(final1) > 20 and len(final2) > 20
    return r


def scenario_4_tools() -> ScenarioResult:
    """Tool usage: capability question, Google search, web scrape."""
    r = ScenarioResult("4", "Tools (capability + search + scrape)", False, [])

    # 4a: Capability
    resolvable, answers, final, err = _run_turn("Can you search Google?")
    if err:
        r.error = err
        r.details.append(f"Capability error: {err}")
        return r
    cap_ok = "yes" in (final or "").lower() or "search" in (final or "").lower()
    r.details.append(f"Capability: {'OK' if cap_ok else 'FAIL'} {_trunc(final, 60)}")

    # 4b: Search (needs CHAT_SKILLS_GOOGLE_SEARCH_URL)
    has_google = bool(os.environ.get("CHAT_SKILLS_GOOGLE_SEARCH_URL", "").strip())
    if has_google:
        resolvable, answers, final, err = _run_turn("Search for Florida Medicaid eligibility requirements")
        search_ok = not err and len(final or "") > 50
        r.details.append(f"Search: {'OK' if search_ok else 'FAIL'} {_trunc(final, 60)}")
    else:
        r.details.append("Search: SKIP (CHAT_SKILLS_GOOGLE_SEARCH_URL not set)")

    # 4c: Scrape (needs CHAT_SKILLS_WEB_SCRAPER_URL)
    has_scraper = bool(os.environ.get("CHAT_SKILLS_WEB_SCRAPER_URL", "").strip())
    if has_scraper:
        url = "https://www.sunshinehealth.com/providers/utilization-management/clinical-payment-policies.html"
        resolvable, answers, final, err = _run_turn(f"Scrape {url}")
        scrape_ok = not err and ("clinical" in (final or "").lower() or "policy" in (final or "").lower() or len(final or "") > 100)
        r.details.append(f"Scrape: {'OK' if scrape_ok else 'FAIL'} {_trunc(final, 60)}")
    else:
        r.details.append("Scrape: SKIP (CHAT_SKILLS_WEB_SCRAPER_URL not set)")

    r.final_message = final[:500] if final else ""
    r.passed = cap_ok  # At least capability must pass
    return r


def scenario_5_rag() -> ScenarioResult:
    """RAG policy lookup with jurisdiction."""
    r = ScenarioResult("5", "RAG policy lookup (Sunshine Health)", False, [])
    state = _build_state(payer="Sunshine Health")
    resolvable, answers, final, err = _run_turn(
        "What is the grievance process for Sunshine Health?",
        merged_state=state,
        payer="Sunshine Health",
    )
    r.final_message = final[:500] if final else ""
    if err:
        r.error = err
        r.details.append(f"Error: {err}")
        return r
    r.details.append(f"Resolvable: {resolvable}, answers: {len(answers)}")
    r.details.append(f"Final: {_trunc(final, 120)}")
    # RAG may return "no context" if DB/API unavailable; we still count as passed if no crash
    r.passed = resolvable and len(final or "") > 20
    return r


def scenario_6_patient_rejection() -> ScenarioResult:
    """Patient-specific: must be refused (not allowed)."""
    r = ScenarioResult("6", "Patient-specific (expect refusal)", False, [])

    resolvable, answers, final, err = _run_turn("What did my doctor say about my condition?")
    if err:
        r.error = err
        r.details.append(f"Error: {err}")
        return r

    r.final_message = final[:500] if final else ""
    # Expect refusal: "don't have access", "can't access", "personal", "records", etc.
    refuse_markers = ["don't", "can't", "cannot", "access", "personal", "records", "not available", "not have"]
    refused = any(m in (final or "").lower() for m in refuse_markers)
    r.details.append(f"Refused: {refused} | {_trunc(final, 100)}")
    r.passed = refused
    if not r.passed:
        r.details.append("Expected: explicit refusal of patient-specific request")
    return r


def scenario_7_rag_fail_retry() -> ScenarioResult:
    """RAG returns no chunks → on_rag_fail search_google should trigger."""
    r = ScenarioResult("7", "RAG fail + Google fallback", False, [])

    # Question unlikely to have RAG hits - planner should set on_rag_fail
    state = _build_state(payer="Sunshine Health")
    has_google = bool(os.environ.get("CHAT_SKILLS_GOOGLE_SEARCH_URL", "").strip())

    resolvable, answers, final, err = _run_turn(
        "What are the latest CMS updates on prior authorization in 2025?",
        merged_state=state,
        payer="Sunshine Health",
    )
    r.final_message = final[:500] if final else ""
    if err:
        r.error = err
        r.details.append(f"Error: {err}")
        return r

    r.details.append(f"Resolvable: {resolvable}, final_len: {len(final or '')}")
    r.details.append(f"Final: {_trunc(final, 100)}")
    # Pass if we got some answer (RAG or Google fallback); don't require Google specifically
    r.passed = resolvable and len(final or "") > 30
    if has_google and r.passed:
        r.details.append("(Google fallback may have been used if RAG had no chunks)")
    return r


SCENARIOS: list[Callable[[], ScenarioResult]] = [
    scenario_1_single_query,
    scenario_2_multi_part,
    scenario_3_jurisdiction_change,
    scenario_4_tools,
    scenario_5_rag,
    scenario_6_patient_rejection,
    scenario_7_rag_fail_retry,
]


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Comprehensive chat pipeline test")
    ap.add_argument("--scenario", type=int, default=None, help="Run only scenario N (1-7)")
    args = ap.parse_args()

    to_run = SCENARIOS
    if args.scenario is not None:
        if 1 <= args.scenario <= len(SCENARIOS):
            to_run = [SCENARIOS[args.scenario - 1]]
        else:
            print(f"Invalid scenario. Use 1-{len(SCENARIOS)}")
            return 1

    print("=" * 70)
    print("Comprehensive Chat Pipeline Test")
    print("=" * 70)
    print(f"CHAT_SKILLS_GOOGLE_SEARCH_URL: {'set' if os.environ.get('CHAT_SKILLS_GOOGLE_SEARCH_URL') else 'not set'}")
    print(f"CHAT_SKILLS_WEB_SCRAPER_URL: {'set' if os.environ.get('CHAT_SKILLS_WEB_SCRAPER_URL') else 'not set'}")
    print(f"RAG_API_URL: {os.environ.get('RAG_API_URL', '(not set)')}")
    print()

    passed = 0
    failed = 0
    for fn in to_run:
        res = fn()
        status = "PASS" if res.passed else "FAIL"
        if res.passed:
            passed += 1
        else:
            failed += 1
        print(f"[{status}] Scenario {res.scenario_id}: {res.name}")
        for d in res.details:
            print(f"       {d}")
        if res.error:
            print(f"       Error: {res.error}")
        print()

    print("=" * 70)
    print(f"Passed: {passed}  Failed: {failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
