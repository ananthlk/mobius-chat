"""ReAct critic integration — end-to-end loop with critic ON.

What this file covers that test_react_critic.py (unit-level) doesn't:
the full ``run_react`` call with a scripted LLM + scripted tool
executor, proving that on a real (mocked) Sunshine Health turn the
loop actually:

  1. Detects the hallucinated draft via critic
  2. Injects the critique as a synthetic observation
  3. Runs another round where the planner produces a grounded answer
  4. Approves the grounded answer and finalizes

This is the 'the architecture works cold' test. If someone accidentally
reorders the critic block inside ``run_react``, or forgets to wire
``tool_results.append`` on critique injection, or the round-counter
logic drifts — this test fails where the unit tests wouldn't.

The price of integration tests is fixture weight. To keep it
manageable, we patch the two external calls that actually reach LLMs
or tools (``_call_llm_json`` and ``_execute_tool``) with scripted
responses. Everything else — the reasoning-context builder, the
retry guard, the finalize path, the tool-result bookkeeping — runs
for real.

**Why we assert ctx.final_message, not intermediate state.** The
final_message is the user-facing artifact. If the loop ends with a
message that still contains the hallucinated phone number, the
architecture failed regardless of what the intermediate state looks
like. That's the contract these tests lock.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest


# ── Test fixture: the Sunshine Health scenario ───────────────────────


# Round 1 planner output: pick search_corpus. Structured as the JSON
# the ReAct parser expects.
_PLANNER_ROUND_1 = """
{
  "thought": "User asks about Sunshine Health H0036 medical necessity — policy question, try corpus first.",
  "tool": "search_corpus",
  "inputs": {"query": "Sunshine Health H0036 medical necessity prior authorization"},
  "is_complete": false
}
"""

# Round 2 planner output: claims completion with the live-failure
# hallucinations. This is the exact shape of the draft that shipped
# in the 2026-04-19 live test.
_PLANNER_ROUND_2_HALLUCINATED = """
{
  "thought": "The corpus returned Provider Manual context. I have enough to answer.",
  "tool": null,
  "inputs": {},
  "is_complete": true,
  "answer": "For Sunshine Health, prior authorization is required for HCPCS code H0036. Medical necessity is evaluated using InterQual/MCG. Call Sunshine Health Provider Services at 1-844-477-8442 for the complete policy."
}
"""

# Critic flags both the fabricated phone AND the unsupported PA
# claim. high-severity on both → loop should reject and continue.
_CRITIC_REJECTS_ROUND_2 = """
{
  "grounded": false,
  "issues": [
    {
      "claim": "prior authorization is required for HCPCS code H0036",
      "severity": "high",
      "reason": "No retrieved source establishes PA requirement for H0036 specifically."
    },
    {
      "claim": "Call Sunshine Health Provider Services at 1-844-477-8442",
      "severity": "high",
      "reason": "Sources contain the provider services number as 1-844-477-8313. The draft's 1-844-477-8442 is not in any source."
    }
  ]
}
"""

# Round 3 planner output: reacted to the critique. Hedged the PA
# claim (the critic's feedback told the planner to revise or drop)
# and removed the specific phone number (just points at the provider
# portal instead). This is what a well-behaved planner would produce
# on retry with the critic observation in context.
_PLANNER_ROUND_3_GROUNDED = """
{
  "thought": "The critic flagged the PA and phone claims. I'll produce a more conservative answer that only states what the sources actually establish.",
  "tool": null,
  "inputs": {},
  "is_complete": true,
  "answer": "Sunshine Health reviews H0036 requests through utilization management using InterQual as the primary decision-support tool. The retrieved Provider Manual does not specify whether H0036 has its own PA requirement — verify via the Pre-Auth Check Tool on the Sunshine Health provider portal before rendering services."
}
"""

# Critic approves the grounded round 3 draft.
_CRITIC_APPROVES_ROUND_3 = '{"grounded": true, "issues": []}'


# Mocked tool result for search_corpus in round 1. Shape matches what
# _execute_tool actually returns.
_SEARCH_CORPUS_RESULT = {
    "tool": "search_corpus",
    "success": True,
    "result": (
        "Utilization management reviews prior authorization requests based on "
        "medical necessity criteria. InterQual is the primary decision-support "
        "tool for medical services. Provider Services can be reached at "
        "1-844-477-8313."
    ),
    "signal": "corpus_only",
    "sources": [
        {
            "document_name": "Sunshine Provider Manual",
            "page": 34,
            "text": (
                "When a request for authorization for services has been received "
                "from a practitioner or provider, the utilization management nurse "
                "or licensed clinician will review all relevant clinical information."
            ),
            "index": 1,
            "source_type": "internal",
        },
        {
            "document_name": "Sunshine Provider Manual",
            "page": 36,
            "text": (
                "InterQual is the primary decision-support tool for medical services. "
                "Provider Services can be reached at 1-844-477-8313."
            ),
            "index": 2,
            "source_type": "internal",
        },
    ],
    "usage": None,
}


# ── Scripted LLM + tool mocks ────────────────────────────────────────


class ScriptedLLM:
    """Returns a pre-recorded sequence of responses based on the
    ``stage`` arg. Tracks call order so tests can assert exact
    sequence + count."""

    def __init__(self, script: dict[str, list[str]]):
        self.script = {k: list(v) for k, v in script.items()}
        self.calls: list[dict] = []  # {stage, user_prompt_preview}

    def __call__(
        self,
        system,
        user,
        max_tokens=800,
        ctx=None,
        stage="planner",
        **kwargs,
    ):
        self.calls.append({
            "stage": stage,
            "user_preview": (user or "")[:200],
            "max_tokens": max_tokens,
        })
        queue = self.script.get(stage, [])
        if not queue:
            raise AssertionError(
                f"ScriptedLLM got unexpected call for stage={stage!r}. "
                f"Scripted stages: {sorted(self.script.keys())}. "
                f"Call history: {[c['stage'] for c in self.calls]}"
            )
        return queue.pop(0)


# ── The integration test ────────────────────────────────────────────


@pytest.fixture
def critic_on(monkeypatch):
    monkeypatch.setenv("MOBIUS_REACT_CRITIC", "1")
    yield


def _emit_collector():
    """Return (emit_fn, lines_list) where emit_fn mirrors
    orchestrator.on_thinking's string/dict handling.

    2026-04-19 (Sprint A.1 commit 1): run_react's critic block now
    produces envelope DICTS, not strings. A naive collector that
    just appends whatever it gets would leave lines_list as a mixed
    array that "\\n".join can't handle. This helper renders dicts
    to their envelope 'note' field for display, matching what the
    orchestrator + FE see."""
    lines: list[str] = []

    def emit(msg) -> None:  # str | dict
        if isinstance(msg, dict) and isinstance(msg.get("signal"), str):
            lines.append(msg.get("note") or f"[{msg['signal']}]")
        else:
            lines.append(str(msg) if msg is not None else "")

    return emit, lines


def _make_ctx(message: str):
    """Minimal PipelineContext for run_react. We don't need a real DB
    or queue — run_react writes to ctx directly and the test reads
    from there."""
    from app.pipeline.context import PipelineContext

    ctx = PipelineContext(
        correlation_id="test-cid-sunshine",
        thread_id=None,
        message=message,
    )
    ctx.effective_message = message
    ctx.merged_state = {"active": {"payer": "Sunshine Health", "jurisdiction": "Florida"}}
    ctx.last_turns = []
    ctx.chat_mode = "agentic"  # 6 rounds — plenty of room for critic retry
    return ctx


class TestRunReactCriticIntegration:
    """End-to-end: run_react with critic ON, scripted planner +
    scripted tool + scripted critic. Assert the loop catches the
    Sunshine Health hallucinations and ships a grounded answer."""

    def test_loop_catches_hallucinated_draft_and_retries(self, critic_on):
        """The load-bearing test for this whole architecture.

        Sequence:
          Round 1: planner picks search_corpus
          [tool runs — returns Provider Manual chunks]
          Round 2: planner claims is_complete=true with hallucinated draft
          [critic rejects — flags fabricated phone + unsupported PA]
          [loop continues — synthetic observation injected]
          Round 3: planner produces grounded draft
          [critic approves]
          [loop finalizes]

        Final: ctx.final_message carries the grounded text, NOT the
        hallucinated one. Specifically:
          - No '1-844-477-8442' (fabricated phone)
          - No 'prior authorization is required' bare assertion
          - Contains Pre-Auth Check Tool recommendation (the safe
            behavior the revised round produced)
        """
        from app.pipeline.react_loop import run_react

        scripted_llm = ScriptedLLM({
            # Round N planner calls go through stage=react_{N}.
            # Critic calls go through stage=critique (cheap-model bucket,
            # see model_registry._COMPOSITE_LAT_CAP_MS_BY_BUCKET).
            "react_1": [_PLANNER_ROUND_1],
            "react_2": [_PLANNER_ROUND_2_HALLUCINATED],
            "react_3": [_PLANNER_ROUND_3_GROUNDED],
            "critique": [_CRITIC_REJECTS_ROUND_2, _CRITIC_APPROVES_ROUND_3],
        })

        ctx = _make_ctx("What are Sunshine Health's medical necessity criteria for H0036?")

        emit, emit_lines = _emit_collector()

        with patch("app.pipeline.react_loop._call_llm_json", side_effect=scripted_llm), \
             patch("app.pipeline.react_loop._execute_tool_with_retry", return_value=_SEARCH_CORPUS_RESULT):
            run_react(ctx, emitter=emit)

        # ── Final answer is the grounded one, not the hallucinated one
        final = (ctx.final_message or "")
        assert "1-844-477-8442" not in final, (
            "Fabricated phone number leaked into final answer. "
            "Critic architecture failed to catch + retry."
        )
        assert "prior authorization is required for HCPCS code H0036" not in final.lower(), (
            "Unsupported PA assertion leaked into final answer. "
            "Critic should have flagged + planner should have revised."
        )
        # Evidence of the grounded revised answer:
        assert "Pre-Auth Check Tool" in final or "InterQual" in final

        # ── LLM call sequence was: planner-1 → planner-2 → critic → planner-3 → critic
        stages = [c["stage"] for c in scripted_llm.calls]
        assert stages == [
            "react_1",    # round 1 decision (planner bucket)
            "react_2",    # round 2 decision (planner bucket) — hallucinated
            "critique",   # audit → reject (cheap bucket)
            "react_3",    # round 3 decision (planner bucket) — revised
            "critique",   # audit → approve (cheap bucket)
        ], f"Unexpected call sequence: {stages}"

        # ── Thinking trail carries the critic's signals so the user
        #    sees what happened
        trail = "\n".join(emit_lines)
        assert "Critic auditing draft against sources" in trail, (
            "Critic audit emit missing — user can't see that the gate ran."
        )
        assert "Critic flagged" in trail, (
            "Rejection emit missing — user can't see the retry was triggered."
        )
        assert "Critic approved" in trail, (
            "Approval emit missing — user can't see the retry succeeded."
        )

    def test_critic_disabled_does_not_intercept(self, monkeypatch):
        """Regression guard: with MOBIUS_REACT_CRITIC unset, the
        hallucinated draft ships as-is (the pre-critic behavior).
        This proves the flag actually gates the critic — if the audit
        ran when disabled, turning off the flag wouldn't be a true
        rollback."""
        monkeypatch.delenv("MOBIUS_REACT_CRITIC", raising=False)

        from app.pipeline.react_loop import run_react

        # No critique stage in the script — if the critic fires despite
        # being disabled, ScriptedLLM raises "unexpected call".
        scripted_llm = ScriptedLLM({
            "react_1": [_PLANNER_ROUND_1],
            "react_2": [_PLANNER_ROUND_2_HALLUCINATED],
        })

        ctx = _make_ctx("What are Sunshine Health's medical necessity criteria for H0036?")

        with patch("app.pipeline.react_loop._call_llm_json", side_effect=scripted_llm), \
             patch("app.pipeline.react_loop._execute_tool_with_retry", return_value=_SEARCH_CORPUS_RESULT):
            run_react(ctx, emitter=lambda _m: None)

        # With critic off, the hallucinated draft ships verbatim.
        # This is the pre-critic baseline — confirms the flag actually
        # disables the audit.
        assert "1-844-477-8442" in (ctx.final_message or ""), (
            "Expected fabricated phone to ship when critic is disabled "
            "(baseline). If it's missing, something else is stripping it."
        )

        # And the critique stage was never called:
        stages = [c["stage"] for c in scripted_llm.calls]
        assert "critique" not in stages

    def test_rounds_exhausted_ships_with_warning(self, critic_on):
        """When the planner keeps producing hallucinated drafts and
        rounds run out, the loop ships the last draft WITH a
        groundedness warning appended. Honest degradation beats
        silent hallucination."""
        from app.pipeline.react_loop import run_react

        # All planner rounds produce the same hallucinated draft;
        # all critic calls reject. Copilot mode (3 rounds) keeps the
        # test fast.
        scripted_llm = ScriptedLLM({
            "react_1": [_PLANNER_ROUND_2_HALLUCINATED],  # completes on round 1
            "react_2": [_PLANNER_ROUND_2_HALLUCINATED],  # and again
            "react_3": [_PLANNER_ROUND_2_HALLUCINATED],  # and again
            "critique": [
                _CRITIC_REJECTS_ROUND_2,
                _CRITIC_REJECTS_ROUND_2,
                _CRITIC_REJECTS_ROUND_2,
            ],
        })

        ctx = _make_ctx("What are Sunshine Health's medical necessity criteria for H0036?")
        ctx.chat_mode = "copilot"  # 3 rounds

        emit, emit_lines = _emit_collector()
        with patch("app.pipeline.react_loop._call_llm_json", side_effect=scripted_llm), \
             patch("app.pipeline.react_loop._execute_tool_with_retry", return_value=_SEARCH_CORPUS_RESULT):
            run_react(ctx, emitter=emit)

        final = ctx.final_message or ""
        # Last-round ship: the hallucinated body is delivered WITH the
        # groundedness warning block.
        assert "Groundedness notice" in final, (
            "Expected ⚠ Groundedness notice block on rounds-exhausted ship. "
            "Without it, the user would see the hallucinated answer with no "
            "signal that anything was flagged."
        )
        # Specific flagged claims appear in the warning:
        assert "1-844-477-8442" in final
        # Emit trail shows the exhaustion signal:
        trail = "\n".join(emit_lines)
        assert "rounds exhausted" in trail.lower() or "unresolved claim" in trail.lower()
