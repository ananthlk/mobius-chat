"""v2's own round loop.

Ananth, 2026-09-11: "so you don't have your own loop — why not start that, so
that we can really have a real A/B where both can complete."
"""

import ast
import pathlib

import pytest

from app.pipeline.v2 import loop as L
from app.pipeline.v2 import posture as P


def _src():
    return pathlib.Path("app/pipeline/v2/loop.py").read_text()


def _code():
    """Source with comments stripped. Six gates of mine today matched my own
    prose; the general fix is to stop handing the assertion prose."""
    import re
    return "\n".join(re.sub(r"#.*$", "", ln) for ln in _src().splitlines())


# ── the reason this loop exists ─────────────────────────────────────────────

def test_the_model_proposing_complete_is_an_INPUT_not_the_decision():
    """THE point of owning the loop.

    In v1, `is_complete` ends the turn — which is why the three-payer question
    stopped after round 2 with 62.6s of a 95s promise left, two gaps open, and
    v2 saying EXPLORE into a void. Here the proposal is recorded and the
    governor gets the next round to disagree.

    🔴 THIS TEST WAS POSITIONAL AND BROKE ON AN UNRELATED EDIT.

    It took `code.index(...)` of the FIRST `is_complete` and asserted over the
    next 900 characters. When the rung-0 exit added an earlier `is_complete`
    branch, the window moved onto the new code and the test failed while the
    property it guards was untouched — a fingerprint, not a property.

    Rewritten to assert the property over EVERY such branch: at least one of
    them must record-and-continue, and the branch that ENDS the turn must
    require the model to report nothing left open.
    """
    import re

    code = _code()
    spans = [m.start() for m in re.finditer(r'decision_json\.get\("is_complete"\)', code)]
    assert spans, "no is_complete branch at all"
    blocks = [code[i:i + 1400] for i in spans]
    assert any("continue" in b for b in blocks), (
        "is_complete still ends the turn immediately")
    assert any("_no_gaps_left" in b for b in blocks), (
        "nothing requires the model to report no gaps left before the turn ends")


def test_the_loop_can_EXTEND_not_only_stop():
    """The asymmetry this replaces: the framing hook could stop a turn and
    never extend one — a brake with no accelerator. A loop that only ever
    shortens is not a governor, it is a timeout."""
    import ast
    code = _code()
    # the round counter advances on a CONTINUE decision, not only on a stop
    assert "if not action.continues:" in code

    # 🔴 PARSED, NOT SLICED. This took `code[i:i+400]` and asserted `break`
    # appeared in it — so adding a comment inside the branch failed the test
    # while the behaviour was unchanged. A gate that measures the distance
    # between two strings breaks on prose and passes on a rewrite.
    #
    # The property is: the stop branch can leave the loop, and some other path
    # goes round again. Both read off the tree.
    tree = ast.parse(code)
    stop_branches = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.If)
        # The dump of `not action.continues` contains attr='continues' and
        # id='action' — never the source-form string "action.continues".
        # My first matcher looked for the latter and found nothing, which
        # read as "no branch tests it" when the branch was right there.
        and any(isinstance(x, ast.Attribute) and x.attr == "continues"
                and getattr(x.value, "id", None) == "action"
                for x in ast.walk(n.test))
    ]
    assert stop_branches, "no branch tests action.continues"
    assert any(isinstance(d, ast.Break)
               for n in stop_branches for d in ast.walk(n)), (
        "the stop branch cannot leave the loop — a governor that cannot stop "
        "is not a governor"
    )
    # ...and there is a path that goes round again
    assert "extensions_used += 1" in code


# ── the fuses, because a decision core defect already ran 98 rounds ─────────

def test_the_loop_has_a_hard_round_fuse():
    """On 2026-09-11 a decision-core defect produced 98 rounds on one turn and
    the thing that stopped it was a person watching. The executor's
    MAX_V2_EXTENSIONS bounds extensions; this bounds the LOOP."""
    assert L.MAX_ROUNDS_HARD <= 12
    # 🔴 v2's OWN ceiling now, bounded by the fuse. Ananth: "we develop our
    # own round ceilings". This asserted v1's number, with the rationale
    # "keeps the arms comparable" — which was the A/B, not this loop's job.
    code = _code()
    assert "V2_MAX_ROUNDS" in code, "the ceiling is not v2's own"
    assert "MAX_ROUNDS_HARD" in code, "the hard fuse is gone"
    for _mode, _n in L.V2_MAX_ROUNDS.items():
        assert 0 < _n <= L.MAX_ROUNDS_HARD, (
            f"{_mode} ceiling {_n} is outside the fuse")


def test_unusable_rounds_end_the_turn():
    """A loop that cannot tell "the model is stuck" from "keep trying" is how
    a runaway starts."""
    assert L.MAX_UNUSABLE_ROUNDS == 2
    code = _code()
    assert "unusable >= MAX_UNUSABLE_ROUNDS" in code


# ── one terminal ────────────────────────────────────────────────────────────

def test_every_exit_goes_through_ONE_publish():
    """Ananth: "there should just be one way to communicate out even on early
    exit, I have had too much trouble with that." Asserted over the AST: one
    _finalize_response call, and it is NOT inside the loop."""
    tree = ast.parse(_src())
    fn = next(f for f in ast.walk(tree)
              if isinstance(f, ast.FunctionDef) and f.name == "run_react_v2")
    calls = [n for n in ast.walk(fn)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
             and n.func.id == "_finalize_response"]
    assert len(calls) == 1, f"{len(calls)} publish sites — early exits will diverge"
    loops = [n for n in ast.walk(fn) if isinstance(n, (ast.While, ast.For))]
    for lp in loops:
        assert not any(c in ast.walk(lp) for c in calls), \
            "a publish inside the loop is a second terminal"


# ── holding everything else constant ────────────────────────────────────────

def test_the_loop_REUSES_reacts_prompt_model_and_tools():
    """The experiment is "the decision loop varies and nothing else does". A
    second prompt builder, model call or tool dispatcher would make every
    divergence unattributable."""
    code = _code()
    for shared in ("_react_reasoning_system", "build_reasoning_context",
                   "_call_llm_json", "_execute_tool_with_retry",
                   "_finalize_response"):
        assert shared in code, f"v2 does not reuse {shared}"
    # ...and does not roll its own
    for forbidden in ("import httpx", "requests.post", "openai", "genai"):
        assert forbidden not in code, f"v2's loop calls a model directly ({forbidden})"


def test_state_comes_from_the_SAME_builder_the_observer_uses():
    """Two state builders would drift, and the first place they drifted would
    be the place the comparison mattered."""
    assert "sh.state_from_ctx(" in _code()


def test_v1_fields_are_NULL_not_guessed():
    """v1 did not run this turn. A fabricated v1_directive would make the
    comparison v2-against-a-prediction-of-v1 rather than against v1."""
    code = _code()
    assert '"v1_directive": None' in code
    assert '"v1_reason": None' in code


def test_the_governor_never_writes_the_answer():
    """It decides WHEN to stop, never WHAT to say. If it stops a turn the
    answer is the model's own running answer from the evidence it had."""
    code = _code()
    assert "_best_running_answer" in code
    fn = next(f for f in ast.walk(ast.parse(_src()))
              if isinstance(f, ast.FunctionDef) and f.name == "_best_running_answer")
    src = ast.unparse(fn)
    assert "running_answer" in src
    # no literal apology/answer strings authored here
    assert "I couldn't" not in src and "Sorry" not in src


def test_the_signature_matches_run_react():
    """The orchestrator routes to one or the other without knowing which it
    called; a different signature would make the routing site know."""
    import inspect
    from app.pipeline.react_loop import run_react
    assert (list(inspect.signature(L.run_react_v2).parameters)
            == list(inspect.signature(run_react).parameters))


# ── routing: one turn, one loop ─────────────────────────────────────────────

def _orch():
    return pathlib.Path("app/pipeline/orchestrator.py").read_text()


def test_a_turn_runs_ONE_loop_never_both():
    """Two loops on one turn would share ctx, tool_results and the publish
    path — the two-writer defect at maximum scale. The A/B harness forks by
    running TWO TURNS, which is a different thing entirely."""
    src = _orch()
    i = src.index("ROUTE TO A LOOP")
    # Slice to a REAL boundary, not a character count. A fixed 1800-char window
    # silently stopped short the moment the block grew, and an assertion over a
    # truncated region tests nothing. Third time today; the fix is always the
    # same and it is never the next magic number.
    j = src.index("_loop_fn(ctx, emitter=on_thinking)", i) + 60
    block = src[i:j]
    import re
    code = "\n".join(re.sub(r"#.*$", "", ln) for ln in block.splitlines())
    # exactly one invocation, through a selected function
    assert code.count("_loop_fn(ctx, emitter=on_thinking)") == 1
    assert "run_react(ctx, emitter=on_thinking)" not in code, \
        "v1's loop is still called unconditionally alongside the selection"


def test_the_own_loop_flag_is_SEPARATE_from_the_arm_split():
    """A v2 turn with the flag off still runs v1's loop with v2's substituted
    decision — the behaviour verified all day. Coupling them would mean
    turning on the arm split silently changed the loop too."""
    src = _orch()
    assert 'os.environ.get("MOBIUS_V2_OWN_LOOP"' in src
    assert 'getattr(ctx, "orchestrator_version", "v1") == "v2"' in src
    i = src.index("_v2_own_loop = (")
    assert "MOBIUS_V2_PCT" not in src[i:i + 400], \
        "the loop choice reads the arm-split percentage directly"


def test_the_own_loop_defaults_OFF_even_in_dev():
    """New and unproven on live traffic. Turning it on is a deliberate act,
    and the deploy allowlist must carry it or the lever cannot be pulled at
    all — SET_ENV_VARS is an allowlist, and that has silently disabled a v2
    flag once already today."""
    env = pathlib.Path("deploy/dev.env").read_text()
    assert "\nMOBIUS_V2_OWN_LOOP=\n" in env or "\nMOBIUS_V2_OWN_LOOP=" in env
    assert "MOBIUS_V2_OWN_LOOP=1" not in env, "it is on by default in dev"
    sh = pathlib.Path("scripts/deploy.sh").read_text()
    assert "MOBIUS_V2_OWN_LOOP=" in sh, "absent from the allowlist — cannot be enabled"


# ── v2's own prompts, and the honesty about not having them yet ─────────────

def test_the_prompt_SOURCE_is_recorded_per_round():
    """Ananth, 2026-09-11: "you can create your own prompts as against using
    the prompts from v1 — rather let v1 reuse its prompt, and over time you
    will replace tool selection and prompt selection with your own."

    Until the v2 blocks exist the loop falls back to v1's prompt. A run whose
    prompts silently came from v1 while the harness said "prompts varied"
    would be the reverse of tonight's cost_usd defect: a field claiming a
    provenance the value does not have.
    """
    from app.pipeline.react.prompts import _react_reasoning_system
    prompt, prov = L._v2_system_prompt(3, "copilot", None, _react_reasoning_system)
    assert prompt.strip(), "no prompt at all"
    # Sources, in order of authority: v2's own composition when the LLM seat
    # ships it, react's LIVE composition, then the legacy builder.
    assert isinstance(prov, dict)
    assert prov.get("source") in ("v2_composition", "v1_composition", "v1_legacy")
    assert '"v2_prompt_source"' in _code(), "the source is computed and discarded"


def test_the_fallback_is_a_REAL_prompt_not_an_empty_string():
    """A silent degrade to an empty system prompt would make v2 look
    catastrophically worse than v1 for a reason that is not v2."""
    from app.pipeline.react.prompts import _react_reasoning_system
    prompt, _ = L._v2_system_prompt(3, "copilot", None, _react_reasoning_system)
    assert len(prompt) > 500


def test_v2_holds_NO_prompt_text_of_its_own():
    """[RULED] v2 reads prompts from prompt_blocks ONLY. No prompt text in v2
    code — otherwise the LLM seat cannot own or version what the model is
    told, and two copies of a prompt drift the first time either changes."""
    code = _code()
    # the module_key is a key, not a prompt; no multi-line instruction blobs
    for tell in ("You are ", "Your response each round", "Respond with JSON"):
        assert tell not in code, f"prompt text inlined in v2 ({tell!r})"


def test_the_block_reader_is_REACTS_OWN_not_a_guessed_api():
    """My first version imported prompt_blocks.get_block, which does not
    exist. It would have thrown, been swallowed by the except, and fallen back
    to v1 FOREVER while the loop reported it was using v2 prompts — the silent
    degrade the `source` return value exists to prevent, defeated by the
    mechanism meant to enforce it."""
    code = _code()
    assert "resolve_composition_sync" in code
    assert "prompt_blocks import get_block" not in code


def test_prompt_provenance_names_the_COMPOSITION_not_a_binary_flag():
    """The LLM seat's scoping, 2026-09-11: react.v2_governor reuses
    response_shape / format_rules / tool_manifest / user_profile UNCHANGED and
    replaces only the identity + critical_rules framing — the parts describing
    v1's fixed "up to N rounds" machine, which v2 does not have.

    So a binary "v2_blocks" is too coarse: a mostly-shared composition would
    report itself as wholly v2's, and a later reader comparing arms would
    believe the prompts differed far more than they did. The composition id,
    hash and block manifest are what actually say which prompt ran — and they
    are already how llm_calls attributes one.
    """
    import ast
    fn = next(f for f in ast.walk(ast.parse(_src()))
              # 🔴 _base, not the wrapper. _v2_system_prompt was split on
              # 2026-09-15: a thin wrapper appends the posture block at a
              # single exit, and the composition logic these tests protect
              # lives in _v2_system_prompt_base. Inspecting the wrapper found
              # none of it and read as "the composition is never read".
              if isinstance(f, ast.FunctionDef) and f.name == "_v2_system_prompt_base")
    src = ast.unparse(fn)
    for field in ("composition_id", "composition_hash", "blocks", "variant_id"):
        assert field in src, f"provenance omits {field}"


def test_the_composition_is_read_off_the_REAL_attribute():
    """`.system_prompt` on RenderedComposition — read off the dataclass, not
    guessed. The first version tried `.text` / `.rendered`; neither exists, so
    it would have returned None and fallen back to v1 FOREVER while the
    provenance said v2.

    That is the identical defect confessed one line above (importing a
    get_block that does not exist), committed again in the same function:
    reading the call site fixed the import, and then I guessed the RETURN
    SHAPE instead of reading the class.
    """
    import ast
    from app.services.prompt_manager import RenderedComposition
    assert "system_prompt" in RenderedComposition.__dataclass_fields__
    fn = next(f for f in ast.walk(ast.parse(_src()))
              # 🔴 _base, not the wrapper. _v2_system_prompt was split on
              # 2026-09-15: a thin wrapper appends the posture block at a
              # single exit, and the composition logic these tests protect
              # lives in _v2_system_prompt_base. Inspecting the wrapper found
              # none of it and read as "the composition is never read".
              if isinstance(f, ast.FunctionDef) and f.name == "_v2_system_prompt_base")

    # PRESENCE IS NOT ENOUGH. A first version asserted "rc.system_prompt" was
    # somewhere in the source — and a mutation that broke the GUARD
    # (`getattr(rc, "text", None) or ...`) still passed, because the RETURN
    # line mentioned the attribute. The eighth gate of mine today that was
    # weaker than it looked. Assert over the attribute ACCESSES, so every read
    # of the composition has to be a real field.
    reads = {n.attr for n in ast.walk(fn)
             if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
             and n.value.id == "rc"}
    assert reads, "the composition is never read"
    real = set(RenderedComposition.__dataclass_fields__)
    assert reads <= real, f"reads fields that do not exist: {reads - real}"
    assert "system_prompt" in reads

    # ...and no getattr() escape hatch on rc, which is how a guessed name
    # sneaks back in while returning None instead of raising.
    for n in ast.walk(fn):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "getattr":
            tgt = n.args[0] if n.args else None
            assert not (isinstance(tgt, ast.Name) and tgt.id == "rc"), \
                "getattr on the composition hides a wrong attribute name"


def test_the_loop_can_be_pinned_PER_TURN_not_only_service_wide():
    """MOBIUS_V2_OWN_LOOP is service-wide, so the only way to try the governor
    loop was to route EVERY v2 turn through code that had never executed a live
    turn. That is not a test, it is a cutover.

    The pin rides the same gate as ab_arm (MOBIUS_V2_AB_FORK) for the same
    reason: a caller that can choose its loop can choose it per question and
    hand back a comparison that is really a selection.
    """
    chat = pathlib.Path("app/api/chat.py").read_text()
    assert 'payload["ab_loop"] = body.ab_loop' in chat
    i = chat.index('payload["ab_loop"]')
    assert 'MOBIUS_V2_AB_FORK' in chat[max(0, i - 220):i], "the loop pin is ungated"
    orch = _orch()
    assert 'if ab_loop in ("v1", "v2"):' in orch
    # the pin must WIN over the env flag, or a per-turn test cannot run while
    # the service-wide flag is off
    i = orch.index('if ab_loop in ("v1", "v2"):')
    assert orch.index("MOBIUS_V2_OWN_LOOP", i) > i, "the env flag still overrides the pin"


def test_arm_and_loop_are_INDEPENDENT():
    """The ARM says whose decisions run; the LOOP says whose round sequence
    runs them. A v2 arm on v1's loop is the substituted-decision build verified
    all day. Collapsing them would make "try the new loop" silently also change
    which orchestrator decides."""
    chat = pathlib.Path("app/api/chat.py").read_text()
    assert "ab_loop:" in chat and "ab_arm:" in chat
    orch = _orch()
    i = orch.index('if ab_loop in ("v1", "v2"):')
    assert "ab_arm" not in orch[i:i + 300], "the loop pin reads the arm pin"


# ── the two defects the first live turn exposed ─────────────────────────────

def test_the_model_response_is_parsed_with_REACTS_parser():
    """_call_llm_json RETURNS A STRING. My first version did
    `raw if isinstance(raw, dict) else {}` — which was therefore ALWAYS {}.
    Every round parsed as unusable, the loop bailed on its own
    MAX_UNUSABLE_ROUNDS fuse, and published an EMPTY answer that everything
    downstream then filled with an UNGROUNDED one: zero tool calls, zero
    sources, a fluent three-payer comparison the corpus never supported.

    Third return-shape guessed today (get_block, RenderedComposition, this).
    The gates I had asserted that the imports RESOLVE — never that a round
    produces a usable decision.
    """
    code = _code()
    assert "_parse_react_decision_json(raw)" in code, "the response is not parsed"
    assert "isinstance(raw, dict)" not in code, "the dict-shape guess is back"
    # and it uses react's parser rather than a second one that would drift
    assert "from app.pipeline.react.parsing import _parse_react_decision_json" in _src()


def test_a_round_parses_a_REAL_model_response():
    """Behaviour, not imports. Drives react's parser with the shapes a model
    actually emits — bare, fenced, and wrapped in prose."""
    from app.pipeline.react.parsing import _parse_react_decision_json as parse
    for raw in (
        '{"thought":"t","tool":"rag","inputs":{"query":"x"},"is_complete":false}',
        '```json\n{"thought":"t","tool":"rag","inputs":{"query":"x"},"is_complete":false}\n```',
        'preamble {"tool":"rag","inputs":{},"is_complete":false} trailing',
    ):
        d = parse(raw) or {}
        assert d.get("tool") == "rag", raw[:40]


def test_an_empty_answer_PUBLISHES_AN_HONEST_FAILURE_not_a_deferral():
    """🔴 REPLACES test_an_empty_answer_DEFERS_to_v1. Ananth: "no fall back".

    The old design handed a void turn to v1's loop. That net caught a real bug
    — the loop could exit before the model was ever asked to write — but it
    cost 40% of every turn's budget held in reserve, and it made a v2 failure
    INVISIBLE: v1 quietly answered and the turn looked fine.

    The failure is v2's now and it is said out loud. Still exactly one publish
    path: an honest failure goes through the same terminal as an answer.
    """
    code = _code()
    assert "run_react as _v1_loop" not in code, (
        "the loop still defers to v1 — the fallback was removed on purpose")
    assert "v2_loop_deferred_to_v1" not in code, (
        "a deferral flag survives a design that no longer defers")
    i = code.index("if not answer.strip():")
    assert "_finalize_response" in code[i:], (
        "an empty answer must still publish through the one terminal")

def test_the_reason_it_stopped_is_RECORDED():
    """Whatever ends the turn, the branch that ended it lands on ctx. With no
    fallback this is the ONLY record of why a turn produced nothing."""
    code = _code()
    assert "ctx.v2_stopped_by" in code

def test_the_prompt_is_REACTS_LIVE_one_with_a_tool_manifest():
    """Ananth: "is this a prompt thing — check v1 prompt." It was.

    react builds its round prompt at react_loop.py:4807 via
    resolve_react_system_prompt_v2 whenever MOBIUS_PROMPT_SOURCE=composition,
    which is SET in dev. I used `_react_reasoning_system` — the legacy builder
    react's own comments call "rarely hit live" — AND passed no allowed_tools.
    A prompt whose tool manifest is empty gives the model nothing to call,
    which is exactly the "no usable tool call" that made every round unusable
    on the first live run.
    """
    from app.pipeline.react.prompts import _react_reasoning_system
    prompt, prov = L._v2_system_prompt(
        10, "agentic", None, _react_reasoning_system,
        allowed_tools=None, agent_role="explore")
    assert prov["source"] in ("v2_composition", "v1_composition", "v1_legacy")
    assert prov["source"] != "v1_fallback", "the museum-piece fallback is back"
    # the prompt must actually offer tools, or the model cannot call one
    assert "rag" in prompt.lower(), "the prompt carries no tool manifest"
    assert len(prompt) > 5000


def test_allowed_tools_reaches_every_prompt_path():
    """react passes ctx.allowed_tools into BOTH its builders. Omitting it is
    how the manifest goes empty — and an empty manifest is indistinguishable,
    from the outside, from a model that simply chose not to call a tool."""
    code = _code()
    assert 'allowed_tools=getattr(ctx, "allowed_tools", None)' in code
    src = _src()
    i = src.index("def _v2_system_prompt")
    body = src[i:]
    assert "resolve_react_system_prompt_v2(" in body
    assert "allowed_tools, agent_role)" in body, "the composition path drops allowed_tools"
    assert "allowed_tools=allowed_tools" in body, "the legacy path drops allowed_tools"


def test_the_tool_call_matches_the_REAL_signature():
    """`_execute_tool_with_retry` has SIX required parameters. I passed five —
    omitting `tool_emitter`, the raw emitter react passes alongside emit_fn —
    and the TypeError was swallowed by my own except into "tool failed". The
    loop then produced no answer at all.

    Fourth signature/shape error today. Every one came from calling a function
    whose NAME I had read rather than whose SIGNATURE, and every one was four
    seconds of inspect.signature away. Bound against the live function so a
    change there fails here rather than at runtime.
    """
    import inspect
    from app.pipeline.react_loop import _execute_tool_with_retry
    required = [n for n, p in inspect.signature(_execute_tool_with_retry).parameters.items()
                if p.default is inspect.Parameter.empty]
    assert required == ["tool", "inputs", "ctx", "round_num", "emit_fn", "tool_emitter"]
    # 🔴 PARSED, NOT PINNED. This asserted the literal source text of the
    # call — so renaming two loop variables broke it while the contract it
    # protects was unchanged. A fingerprint passes on the next instance and
    # fails on a rename; the PROPERTY is that all six required parameters are
    # passed positionally, exactly as react's own call site does.
    import ast
    tree = ast.parse(_code())
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call)
             and getattr(n.func, "id", "") == "_execute_tool_with_retry"]
    assert calls, "the loop never calls the tool dispatcher"
    for c in calls:
        assert len(c.args) == len(required), (
            f"call at line {c.lineno} passes {len(c.args)} positional args; "
            f"the signature requires {len(required)}: {required}"
        )


def test_a_tool_failure_is_not_silently_swallowed():
    """My except turned a TypeError — a programming error — into
    {"success": False}, indistinguishable from a tool that ran and found
    nothing. The log line is what made it findable at all."""
    code = _code()
    i = code.index("_execute_tool_with_retry(")
    block = code[i:i + 900]
    assert "logger.warning" in block, "a tool failure leaves no trace"


def test_v2_spends_the_WHOLE_turn_budget():
    """🔴 INVERTS test_v2_never_spends_the_whole_turn_budget. Ananth: "v2 gets
    whole budget".

    The fraction existed so a failing v2 turn left v1 time to run. With no
    fallback there is nobody to leave it for, and reserving 38s of a 95s
    promise for a recovery that cannot happen is just a shorter promise.
    """
    assert L.V2_BUDGET_FRACTION == 1.0

def test_the_wall_clock_is_checked_at_the_TOP_of_every_round():
    """spendable() is checked inside select(), BEFORE a round; nothing stops a
    round already running. A single tool call took 205s against a 95s promise
    and nothing noticed until the round ended. This is the backstop, and it
    must run before any spending in the iteration."""
    code = _code()
    i = code.index("while rn < max_rounds:")
    body = code[i:]
    assert "_budget_exhausted(elapsed" in body, "no wall-clock backstop"
    # it must come before the model call and the tool call
    assert body.index("_budget_exhausted(elapsed") < body.index("_call_llm_json("), \
        "the budget check runs after the model call"
    assert body.index("_budget_exhausted(elapsed") < body.index("_execute_tool_with_retry("), \
        "the budget check runs after the tool call"


# ── the exit label must survive the exit ────────────────────────────────────

def test_stopped_by_is_never_assigned_unguarded():
    """THE DEFECT: `res.stopped_by = "max_rounds"` sat in the `else` of
    `if pending:`, so every exit leaving no queued tool — which is every CLEAN
    exit — was relabelled a budget exhaustion on its way out.
    model_complete_no_gaps, unusable_rounds, model_error and every governor
    branch all reached the trace as "max_rounds".

    Reads the AST of the real function, NOT a re-implementation of the guard:
    a test that restates the fix passes just as happily on the reverted code.

    The property: the literal "max_rounds" is only ever assigned to
    res.stopped_by underneath a test of res.stopped_by.
    """
    tree = ast.parse(_src())
    fn = next(f for f in ast.walk(tree)
              if isinstance(f, ast.FunctionDef) and f.name == "run_react_v2")

    def assigns_max_rounds(node):
        return (isinstance(node, ast.Assign)
                and isinstance(node.value, ast.Constant)
                and node.value.value == "max_rounds")

    guarded = set()
    for n in ast.walk(fn):
        if isinstance(n, ast.If) and "stopped_by" in ast.dump(n.test):
            for inner in ast.walk(n):
                if assigns_max_rounds(inner):
                    guarded.add(id(inner))

    sites = [n for n in ast.walk(fn) if assigns_max_rounds(n)]
    assert sites, "the fallback label is gone entirely — did the exit change?"
    for site in sites:
        assert id(site) in guarded, (
            f'line {site.lineno}: stopped_by = "max_rounds" is assigned '
            "without first testing whether a label is already set — it will "
            "overwrite every clean exit reason")
