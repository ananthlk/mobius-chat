#!/usr/bin/env python3
"""Simulate a question through the pipeline and print all emissions (thinking chunks).

Uses mocked LLM so it runs quickly without real API calls. Run from repo root:

  cd mobius-chat && QUEUE_TYPE=memory python scripts/simulate_question_emissions.py
  cd mobius-chat && QUEUE_TYPE=memory python scripts/simulate_question_emissions.py "What is PA for H0036?"

Default question: Create a credentialing report for David Lawrence Center
"""
from __future__ import annotations

import asyncio
import os
import sys
import json
from pathlib import Path
from unittest.mock import patch, MagicMock

CHAT_ROOT = Path(__file__).resolve().parent.parent
if str(CHAT_ROOT) not in sys.path:
    sys.path.insert(0, str(CHAT_ROOT))

# Env: ReAct on, memory queue
os.environ["MOBIUS_USE_REACT"] = "1"
os.environ["QUEUE_TYPE"] = "memory"

for env_path in (CHAT_ROOT / ".env", CHAT_ROOT.parent / "mobius-config" / ".env"):
    if env_path.exists():
        try:
            from dotenv import load_dotenv
            load_dotenv(env_path, override=False)
        except Exception:
            pass

# Collect emissions (thinking chunks) and final message (may be streamed in chunks)
emissions: list[str] = []
final_chunks: list[str] = []

def capture_send_to_user(correlation_id: str, payload: dict, **kwargs) -> None:
    ptype = (payload.get("type") or "").strip().lower()
    content = (payload.get("content") or "").strip()
    if ptype == "thinking" and content:
        emissions.append(content)
        print(f"  [emit] {content}")
    elif ptype == "final" and content:
        final_chunks.append(content)

# Mock LLM: first call = use run_credentialing_report, second = is_complete with good answer
_reason_call = [0]

def mock_call_llm_json(system: str, user: str, max_tokens: int = 800) -> str:
    _reason_call[0] += 1
    if _reason_call[0] == 1:
        # First reasoning: choose credentialing tool
        return json.dumps({
            "thought": "User asked for a credentialing report for an org; I'll use run_credentialing_report to generate it.",
            "tool": "run_credentialing_report",
            "inputs": {"org_name": "David Lawrence Center"},
            "is_complete": False,
        })
    # Second reasoning: tool already ran (mocked); synthesize success answer
    return json.dumps({
        "thought": "The tool result shows 'Report stored' and 'Step 11 done'; I'll confirm success and summarize.",
        "tool": None,
        "inputs": {},
        "is_complete": True,
        "answer": (
            "The credentialing report for David Lawrence Center has been generated and stored. "
            "Summary: 7 org NPIs, 4 locations, 103 providers. Utilization $368/member (2,425 members). "
            "43 PML-valid rows, 3 flagged; 51 providers to enroll with suggested taxonomy. "
            "Opportunity: Guaranteed $1,509,128, At-risk $110,424, Total $4.86M. "
            "You can ask follow-up questions (e.g. how many need enrollment, Section B/C breakdown)."
        ),
        "sources": [],
        "confidence": "high",
    })


def main() -> int:
    question = " ".join(sys.argv[1:]).strip() or "Create a credentialing report for David Lawrence Center"
    correlation_id = "sim-emissions-1"
    thread_id = None

    print("=" * 60)
    print("Simulating question (ReAct path, mocked LLM)")
    print("=" * 60)
    print(f"Question: {question}")
    print()
    print("Emissions (thinking chunks):")
    print("-" * 60)

    def mock_execute_tool(tool: str, inputs: dict, ctx, emitter=None) -> dict:
        """Return a realistic credentialing result so reasoning sees 'Report stored'."""
        if tool == "run_credentialing_report":
            org = inputs.get("org_name") or "David Lawrence Center"
            return {
                "tool": "run_credentialing_report",
                "success": True,
                "result": (
                    f"Steps for {org}: 1. Revenue metrics in place. 2. Found 7 org NPI(s). "
                    "3. Found 4 location(s). 4. Found 103 provider(s). Org benchmark: $368/member, 2425 members. "
                    "5. Found 36 service(s). 6. PML: 43 valid, 3 flagged. 7. 51 provider(s) to enroll. "
                    "Opportunity sizing: Guaranteed $1,509,128, At-risk $110,424, Total opp $4,859,254. "
                    "Report stored. You can ask any question about it. ✓ Step 11 done. Report generated."
                ),
                "signal": "corpus_only",
                "sources": [],
            }
        return {"tool": tool, "success": False, "result": "Unknown tool", "signal": "no_sources", "sources": []}

    # Mock integrator LLM so we get a good report AnswerCard without calling Vertex
    _report_card = json.dumps({
        "mode": "FACTUAL",
        "direct_answer": (
            "The credentialing report for David Lawrence Center has been generated and stored. "
            "Summary: 7 org NPIs, 4 locations, 103 providers. Utilization $368/member (2,425 members). "
            "43 PML-valid rows, 3 flagged; 51 providers to enroll with suggested taxonomy. "
            "Opportunity: Guaranteed $1,509,128, At-risk $110,424, Total $4.86M. "
            "You can ask follow-up questions (e.g. how many need enrollment, Section B/C breakdown)."
        ),
        "sections": [
            {"label": "Summary", "title": "Report summary", "content": "7 NPIs, 4 locations, 103 providers. 51 to enroll. Total opportunity $4.86M."},
        ],
    })

    async def mock_stream_generate(prompt):
        yield _report_card

    def mock_get_llm_provider():
        p = MagicMock()
        p.stream_generate = mock_stream_generate
        async def gen_usage(p):
            return (_report_card, {"input_tokens": 0, "output_tokens": 0})
        p.generate_with_usage = lambda p: asyncio.run(gen_usage(p))
        return p

    with patch("app.communication.gate.send_to_user", side_effect=capture_send_to_user):
        with patch("app.pipeline.react_loop._call_llm_json", side_effect=mock_call_llm_json):
            with patch("app.pipeline.react_loop._execute_tool", side_effect=mock_execute_tool):
                with patch("app.services.llm_provider.get_llm_provider", side_effect=mock_get_llm_provider):
                    try:
                        from app.pipeline.orchestrator import run_pipeline
                        run_pipeline(correlation_id, question, thread_id)
                    except Exception as e:
                        print(f"  [error] {e}")
                        import traceback
                        traceback.print_exc()
                        return 1

    print("-" * 60)
    full_final = "".join(final_chunks)
    if full_final:
        print("Final report (answer card):")
        print("-" * 60)
        if full_final.strip().startswith("{"):
            try:
                parsed = json.loads(full_final)
                print(json.dumps(parsed, indent=2))
            except json.JSONDecodeError:
                print(full_final[:800] + ("…" if len(full_final) > 800 else ""))
        else:
            print(full_final[:800] + ("…" if len(full_final) > 800 else ""))
        print("-" * 60)
    print(f"Thinking emissions: {len(emissions)}")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
