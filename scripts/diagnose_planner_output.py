#!/usr/bin/env python3
"""Diagnose planner output: run complex queries and capture raw LLM JSON, parsed TaskPlan, and Plan.

Usage:
  PYTHONPATH=mobius-chat python mobius-chat/scripts/diagnose_planner_output.py --query "Compare care management for Sunshine, United, Molina"
  PYTHONPATH=mobius-chat python mobius-chat/scripts/diagnose_planner_output.py --file mobius-chat/scripts/planner_complex_queries.txt
  PYTHONPATH=mobius-chat python mobius-chat/scripts/diagnose_planner_output.py --query "Q1" --query "Q2" --output results.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

os.environ.setdefault("QUEUE_TYPE", "memory")

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


def _run_planner_diagnostic(message: str, context: str = "") -> dict:
    """Call LLM, capture raw output, parse, adapt. Returns diagnostic dict."""
    import asyncio

    from app.chat_config import get_chat_config
    from app.planner.adapter import task_plan_to_plan
    from app.planner.mobius_parse import parse_task_plan_from_json
    from app.services.llm_provider import get_llm_provider
    from app.stages.agents.capabilities import planner_input_json

    cfg = get_chat_config()
    system = getattr(cfg.prompts, "decompose_system_mobius", None) or ""
    user_tpl = getattr(cfg.prompts, "decompose_user_template_mobius", None) or ""
    if not system or not user_tpl:
        return {
            "error": "Mobius planner prompts not configured",
            "raw": None,
            "task_plan": None,
            "plan": None,
        }

    planner_input = planner_input_json(message, context or "")
    planner_input_str = json.dumps(planner_input, indent=2)
    user = user_tpl.format(planner_input_json=planner_input_str)
    prompt = f"{system}\n\n{user}"

    provider = get_llm_provider()
    raw, usage = asyncio.run(provider.generate_with_usage(prompt))

    result = {
        "message": message,
        "raw": raw.strip() if raw else None,
        "llm_usage": usage,
        "task_plan": None,
        "plan": None,
    }

    if not raw or not raw.strip():
        result["error"] = "LLM returned empty response"
        return result

    task_plan = parse_task_plan_from_json(raw)
    if not task_plan:
        result["error"] = "Parse failed"
        return result

    # Serialize TaskPlan for output
    def _task_plan_to_dict(tp):
        if tp is None:
            return None
        tasks_out = []
        for t in tp.tasks or []:
            tasks_out.append({
                "id": t.id,
                "subquestion_id": t.subquestion_id,
                "modality": t.modality,
                "goal": t.goal,
                "steps": [{"step": s.step, "action": s.action} for s in (t.steps or [])],
                "fallbacks": [{"if": f.if_condition, "then": f.then} for f in (t.fallbacks or [])],
            })
        sqs_out = []
        for sq in tp.subquestions or []:
            caps = sq.capabilities_needed
            sqs_out.append({
                "id": sq.id,
                "text": sq.text,
                "kind": sq.kind,
                "question_intent": sq.question_intent,
                "intent_score": sq.intent_score,
                "capabilities_needed": {
                    "primary": caps.primary if caps else None,
                    "fallbacks": list(caps.fallbacks) if caps else [],
                } if caps else None,
            })
        return {
            "message_summary": tp.message_summary,
            "subquestions": sqs_out,
            "tasks": tasks_out,
            "retry_policy": {
                "max_attempts": tp.retry_policy.max_attempts,
                "on_missing_jurisdiction": tp.retry_policy.on_missing_jurisdiction,
                "on_no_results": tp.retry_policy.on_no_results,
                "on_tool_error": tp.retry_policy.on_tool_error,
            } if tp.retry_policy else None,
        }

    result["task_plan"] = _task_plan_to_dict(task_plan)

    plan = task_plan_to_plan(task_plan, [], llm_usage=usage)
    result["plan"] = {
        "subquestions": [
            {
                "id": sq.id,
                "text": sq.text,
                "kind": sq.kind,
                "capabilities_primary": sq.capabilities_primary,
                "on_rag_fail": sq.on_rag_fail,
            }
            for sq in plan.subquestions
        ],
    }
    result.pop("error", None)
    return result


def _serialize_for_json(obj):
    """Make obj JSON-serializable."""
    if obj is None:
        return None
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    if isinstance(obj, dict):
        return {k: _serialize_for_json(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_serialize_for_json(x) for x in obj]
    return obj


def main():
    parser = argparse.ArgumentParser(description="Diagnose planner output for complex queries")
    parser.add_argument("--query", action="append", help="Query string (repeat for multiple)")
    parser.add_argument("--file", type=Path, help="File with one query per line")
    parser.add_argument("--output", "-o", type=Path, help="Write results to JSON file")
    parser.add_argument("--verbose", "-v", action="store_true", help="Print full output")
    args = parser.parse_args()

    queries: list[str] = []
    if args.query:
        queries.extend(args.query)
    if args.file:
        path = args.file if args.file.is_absolute() else Path.cwd() / args.file
        if path.exists():
            queries.extend(ln.strip() for ln in path.read_text().splitlines() if ln.strip())
        else:
            print(f"File not found: {path}", file=sys.stderr)
            sys.exit(1)
    if not queries:
        print("No queries. Use --query '...' or --file path", file=sys.stderr)
        sys.exit(1)

    results = []
    for i, q in enumerate(queries):
        print(f"\n--- Query {i + 1}/{len(queries)} ---", file=sys.stderr)
        print(q[:80] + ("..." if len(q) > 80 else ""), file=sys.stderr)
        diag = _run_planner_diagnostic(q)
        results.append(diag)
        if diag.get("error"):
            print(f"  ERROR: {diag['error']}", file=sys.stderr)
            continue
        n_sq = len(diag.get("task_plan", {}).get("subquestions") or [])
        n_tasks = len(diag.get("task_plan", {}).get("tasks") or [])
        n_fallbacks = sum(
            len(t.get("fallbacks") or [])
            for t in (diag.get("task_plan") or {}).get("tasks") or []
        )
        has_steps = any(
            (t.get("steps") or []) for t in (diag.get("task_plan") or {}).get("tasks") or []
        )
        print(f"  Subquestions: {n_sq}, Tasks: {n_tasks}, Task fallbacks: {n_fallbacks}, Has steps: {bool(has_steps)}", file=sys.stderr)

    out = {"queries": queries, "results": results}
    serialized = _serialize_for_json(out)
    if serialized and "results" in serialized:
        for r in serialized["results"]:
            if "llm_usage" in r and r["llm_usage"] is not None:
                try:
                    r["llm_usage"] = dict(r["llm_usage"])
                except Exception:
                    r["llm_usage"] = str(r["llm_usage"])

    json_str = json.dumps(serialized, indent=2, default=str)
    if args.output:
        args.output.write_text(json_str)
        print(f"\nWrote {args.output}", file=sys.stderr)
    else:
        print(json_str)

    failed = sum(1 for r in results if r.get("error"))
    if failed:
        print(f"\n{failed}/{len(results)} queries had errors", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
