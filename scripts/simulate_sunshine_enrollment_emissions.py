#!/usr/bin/env python3
"""Run the two Sunshine Health queries and print all emissions (thinking chunks).

Query 1: "How does a provider enroll with Sunshine Health?"
Query 2: "can you get me a link to their website and also the specific page you are referencing"
        (with "Recent conversation" = Q1 + answer without link, to exercise Rule 8)

Uses mocked LLM and tools so it runs without real API calls. Run from repo root:

  cd mobius-chat && QUEUE_TYPE=memory python scripts/simulate_sunshine_enrollment_emissions.py

Emissions are printed in order so you can see ReAct rounds and integrate step messages.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

CHAT_ROOT = Path(__file__).resolve().parent.parent
if str(CHAT_ROOT) not in sys.path:
    sys.path.insert(0, str(CHAT_ROOT))

os.environ["MOBIUS_USE_REACT"] = "1"
os.environ["QUEUE_TYPE"] = "memory"

for env_path in (CHAT_ROOT / ".env", CHAT_ROOT.parent / "mobius-config" / ".env"):
    if env_path.exists():
        try:
            from dotenv import load_dotenv
            load_dotenv(env_path, override=False)
        except Exception:
            pass

# ---------------------------------------------------------------------------
# Emission capture
# ---------------------------------------------------------------------------

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


def reset_capture() -> None:
    emissions.clear()
    final_chunks.clear()


# ---------------------------------------------------------------------------
# Query 1: "How does a provider enroll with Sunshine Health?"
# Simulate: round 1 = search_corpus, round 2 = google_search, round 3 = is_complete
# ---------------------------------------------------------------------------

Q1 = "How does a provider enroll with Sunshine Health?"
_reason_call_q1 = [0]


def mock_call_llm_json_q1(system: str, user: str, max_tokens: int = 800) -> str:
    _reason_call_q1[0] += 1
    n = _reason_call_q1[0]
    if n == 1:
        return json.dumps({
            "thought": "The user is asking about the provider enrollment process for Sunshine Health, which is a policy question best answered by the internal knowledge base.",
            "tool": "search_corpus",
            "inputs": {"query": "Sunshine Health provider enrollment process"},
            "is_complete": False,
        })
    if n == 2:
        return json.dumps({
            "thought": "The user is asking about the provider enrollment process for Sunshine Health, and my initial corpus search failed to find the answer, so I will now use Google to find the information on the web.",
            "tool": "google_search",
            "inputs": {"query": "Sunshine Health provider enrollment process"},
            "is_complete": False,
        })
    if n == 3:
        return json.dumps({
            "thought": "The corpus search failed to find the enrollment process, but the Google search found the exact page on the Sunshine Health website describing it. I can now synthesize a final answer based on this information.",
            "tool": None,
            "inputs": {},
            "is_complete": True,
            "answer": "For Sunshine Health, providers can enroll by using a form on the Sunshine Health website. This form is specifically for adding a new practitioner or facility to an existing contract. The form provides clear, guided instructions to make the process smoother and more efficient.",
            "sources": [],
            "confidence": "high",
        })
    return json.dumps({"tool": None, "inputs": {}, "is_complete": True, "answer": "No further action.", "sources": [], "confidence": "high"})


def mock_execute_tool_q1(tool: str, inputs: dict, ctx, emitter=None) -> dict:
    if tool == "search_corpus":
        return {
            "tool": "search_corpus",
            "success": False,
            "result": "No direct match for provider enrollment process in our materials.",
            "signal": "no_sources",
            "sources": [],
        }
    if tool == "google_search":
        return {
            "tool": "google_search",
            "success": True,
            "result": "Sunshine Health offers a new user-friendly online form for provider enrollment. The form is for adding a new practitioner or facility to an existing contract. Clear guided instructions; aims to increase accuracy and reduce submission errors.",
            "signal": "google_only",
            "sources": [{"document_name": "sunshinehealth.com", "index": 1}],
        }
    return {"tool": tool, "success": False, "result": "Unknown tool", "signal": "no_sources", "sources": []}


# ---------------------------------------------------------------------------
# Query 2: "can you get me a link to their website and also the specific page you are referencing"
# With last_turns = [Q1 + answer without link]. Rule 8: round 1 = tool (google_search), round 2 = is_complete
# ---------------------------------------------------------------------------

Q2 = "can you get me a link to their website and also the specific page you are referencing"
# Prior answer (no link) — so model should NOT set is_complete in round 1
Q1_ANSWER_NO_LINK = (
    "For Sunshine Health, providers can enroll by using a form on the Sunshine Health website. "
    "This form is specifically for adding a new practitioner or facility to an existing contract. "
    "The form provides clear, guided instructions."
)

_reason_call_q2 = [0]


def mock_call_llm_json_q2(system: str, user: str, max_tokens: int = 800) -> str:
    _reason_call_q2[0] += 1
    n = _reason_call_q2[0]
    # Rule 8: "Recent conversation" present, user asked for link — answer is INSUFFICIENT → call tool first
    if n == 1:
        return json.dumps({
            "thought": "The user is asking for a link to the Sunshine Health website and the specific page. The prior answer did not include a link, so the answer is insufficient. I will use google_search to find the URL.",
            "tool": "google_search",
            "inputs": {"query": "Sunshine Health provider enrollment form website"},
            "is_complete": False,
        })
    if n == 2:
        return json.dumps({
            "thought": "I found the Sunshine Health provider enrollment page. I can now provide the link and specific page in the answer.",
            "tool": None,
            "inputs": {},
            "is_complete": True,
            "answer": "Sunshine Health provider enrollment: https://www.sunshinehealth.com/provider-enrollment. This is the specific page for the new user-friendly form to add a practitioner or facility to an existing contract.",
            "sources": [],
            "confidence": "high",
        })
    return json.dumps({"tool": None, "inputs": {}, "is_complete": True, "answer": "Done.", "sources": [], "confidence": "high"})


def mock_execute_tool_q2(tool: str, inputs: dict, ctx, emitter=None) -> dict:
    if tool == "google_search":
        return {
            "tool": "google_search",
            "success": True,
            "result": "Sunshine Health provider enrollment page: https://www.sunshinehealth.com/provider-enrollment. Form for adding new practitioner or facility to existing contract.",
            "signal": "google_only",
            "sources": [{"document_name": "sunshinehealth.com", "index": 1}],
        }
    return {"tool": tool or "search_corpus", "success": False, "result": "Unknown", "signal": "no_sources", "sources": []}


# ---------------------------------------------------------------------------
# Shared: integrator mock (answer card)
# ---------------------------------------------------------------------------

def make_integrator_card(direct_answer: str) -> str:
    return json.dumps({
        "mode": "BLENDED",
        "direct_answer": direct_answer,
        "sections": [],
    })


def mock_get_llm_provider(direct_answer: str):
    card = make_integrator_card(direct_answer)
    p = MagicMock()

    async def gen_usage(prompt):
        return (card, {"input_tokens": 0, "output_tokens": 0})
    p.generate_with_usage = lambda prompt: asyncio.run(gen_usage(prompt))

    async def async_stream(prompt):
        yield card
    p.stream_generate = async_stream
    return p


# ---------------------------------------------------------------------------
# Run pipeline for one question with given mocks
# ---------------------------------------------------------------------------

def run_one(
    question: str,
    thread_id: str | None,
    mock_call_llm_json,
    mock_execute_tool,
    integrator_answer: str,
    label: str,
) -> bool:
    reset_capture()
    correlation_id = f"sim-{label}-{hash(question) % 10000}"
    get_provider = lambda: mock_get_llm_provider(integrator_answer)

    import app.pipeline.react_loop as react_loop_module
    # Patch gate in orchestrator namespace so run_pipeline's on_thinking uses our capture
    with patch("app.pipeline.orchestrator.send_to_user", side_effect=capture_send_to_user):
        with patch.object(react_loop_module, "_call_llm_json", side_effect=mock_call_llm_json):
            with patch.object(react_loop_module, "_execute_tool", side_effect=mock_execute_tool):
                with patch("app.services.llm_provider.get_llm_provider", side_effect=get_provider):
                    try:
                        from app.pipeline.orchestrator import run_pipeline
                        run_pipeline(correlation_id, question, thread_id)
                    except Exception as e:
                        print(f"  [error] {e}")
                        import traceback
                        traceback.print_exc()
                        return False
    return True


def main() -> int:
    print("=" * 70)
    print("Sunshine Health queries — emissions (thinking chunks) for each run")
    print("=" * 70)

    # ----- Query 1 -----
    print()
    print("--- Query 1 (enrollment) ---")
    print(f"Question: {Q1}")
    print("Emissions:")
    print("-" * 70)
    ok1 = run_one(
        Q1,
        thread_id=None,
        mock_call_llm_json=mock_call_llm_json_q1,
        mock_execute_tool=mock_execute_tool_q1,
        integrator_answer="For Sunshine Health, providers can enroll by using a form on the Sunshine Health website. This form is for adding a new practitioner or facility to an existing contract.",
        label="q1",
    )
    print("-" * 70)
    print(f"Query 1 emissions count: {len(emissions)}")
    if final_chunks:
        print("Final (answer card) preview:", ("".join(final_chunks))[:200] + "…")
    print()

    # ----- Query 2: with last_turns so "Recent conversation" is present -----
    # Patch storage so state_load sees one prior turn (Q1 + answer without link)
    FAKE_LAST_TURNS = [
        {
            "user_content": Q1,
            "assistant_content": Q1_ANSWER_NO_LINK,
            "message": Q1,
        },
    ]

    def fake_get_state(thread_id: str):
        return {} if thread_id == "test-sunshine-thread" else None

    def fake_get_last_turn_messages(thread_id: str, limit_turns: int = 2):
        if thread_id == "test-sunshine-thread":
            return FAKE_LAST_TURNS
        return []

    print("--- Query 2 (link follow-up; Recent conversation = Q1 + answer without link) ---")
    print(f"Question: {Q2}")
    print("Emissions (expect multiple rounds: round 1 = tool, round 2 = synthesize):")
    print("-" * 70)
    reset_capture()
    _reason_call_q2[0] = 0
    correlation_id = "sim-q2-9999"
    get_provider_q2 = lambda: mock_get_llm_provider(
        "Sunshine Health provider enrollment: https://www.sunshinehealth.com/provider-enrollment. This is the specific page for the new user-friendly form."
    )

    import app.pipeline.react_loop as react_loop_module
    import app.stages.state_load as state_load_module
    # Patch storage in state_load namespace so run_pipeline's state_load uses fakes (no DB)
    with patch("app.pipeline.orchestrator.send_to_user", side_effect=capture_send_to_user):
        with patch.object(react_loop_module, "_call_llm_json", side_effect=mock_call_llm_json_q2):
            with patch.object(react_loop_module, "_execute_tool", side_effect=mock_execute_tool_q2):
                with patch("app.services.llm_provider.get_llm_provider", side_effect=get_provider_q2):
                    with patch.object(state_load_module, "get_state", side_effect=fake_get_state):
                        with patch.object(state_load_module, "get_last_turn_messages", side_effect=fake_get_last_turn_messages):
                            with patch.object(state_load_module, "get_last_turn_sources", return_value=[]):
                                with patch.object(state_load_module, "save_state_full", return_value=None):
                                    with patch("app.pipeline.orchestrator.save_state_full", return_value=None):
                                        mock_persist = MagicMock()
                                        mock_persist.save_turn_with_messages = MagicMock(return_value=None)
                                        mock_persist.save_turn = MagicMock(return_value=None)
                                        with patch("app.pipeline.orchestrator.get_persistence", return_value=mock_persist):
                                            try:
                                                from app.pipeline.orchestrator import run_pipeline
                                                run_pipeline(correlation_id, Q2, "test-sunshine-thread")
                                            except Exception as e:
                                                print(f"  [error] {e}")
                                                import traceback
                                                traceback.print_exc()
                                                return 1

    print("-" * 70)
    print(f"Query 2 emissions count: {len(emissions)}")
    if final_chunks:
        print("Final (answer card) preview:", ("".join(final_chunks))[:220] + "…")
    print()
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
