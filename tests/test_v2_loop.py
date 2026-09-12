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
    """
    code = _code()
    i = code.index('decision_json.get("is_complete")')
    block = code[i:i + 900]
    assert "continue" in block, "is_complete still ends the turn immediately"
    # it may only break when the model itself reports nothing left open
    assert "_no_gaps_left" in block


def test_the_loop_can_EXTEND_not_only_stop():
    """The asymmetry this replaces: the framing hook could stop a turn and
    never extend one — a brake with no accelerator. A loop that only ever
    shortens is not a governor, it is a timeout."""
    code = _code()
    # the round counter advances on a CONTINUE decision, not only on a stop
    assert "if not action.continues:" in code
    i = code.index("if not action.continues:")
    after = code[i:i + 400]
    assert "break" in after
    # ...and there is a path that goes round again
    assert "extensions_used += 1" in code


# ── the fuses, because a decision core defect already ran 98 rounds ─────────

def test_the_loop_has_a_hard_round_fuse():
    """On 2026-09-11 a decision-core defect produced 98 rounds on one turn and
    the thing that stopped it was a person watching. The executor's
    MAX_V2_EXTENSIONS bounds extensions; this bounds the LOOP."""
    assert L.MAX_ROUNDS_HARD <= 12
    assert "min(react_max_iterations_for_mode(mode), MAX_ROUNDS_HARD)" in _code()


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
              if isinstance(f, ast.FunctionDef) and f.name == "_v2_system_prompt")
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
              if isinstance(f, ast.FunctionDef) and f.name == "_v2_system_prompt")

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


def test_an_empty_answer_DEFERS_to_v1_instead_of_publishing_a_void():
    """My own rule, written into the framing hook this morning and NOT written
    here: "finalising an empty answer turns a governor decision into a blank
    screen, which is worse than the round it is trying to save."

    Live it was worse than a blank screen. The loop handed _finalize_response
    an empty string and everything downstream composed an answer from NOTHING.
    A void does not stay a void; it gets filled.
    """
    code = _code()
    assert "if not answer.strip():" in code, "an empty answer still publishes"
    i = code.index("if not answer.strip():")
    block = code[i:i + 900]
    assert "run_react as _v1_loop" in block and "_v1_loop(ctx" in block, \
        "an empty result does not defer to the known-good loop"
    assert "return" in block
    # the deferral must happen BEFORE the publish, or it publishes anyway
    assert i < code.index("_finalize_response(ctx, answer")


def test_the_deferral_is_RECORDED():
    """A turn that silently fell back to v1 while labelled v2 would put v1's
    behaviour in v2's column — the comparison would be measuring v1 twice."""
    assert "v2_loop_deferred_to_v1" in _code()


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
    code = _code()
    assert "_execute_tool_with_retry(\n                tool, decision_json" in code \
        or "tool, decision_json.get(\"inputs\") or {}, ctx, rn, emit, emitter," in code, \
        "the call does not pass all six positionally, as react's own call site does"


def test_a_tool_failure_is_not_silently_swallowed():
    """My except turned a TypeError — a programming error — into
    {"success": False}, indistinguishable from a tool that ran and found
    nothing. The log line is what made it findable at all."""
    code = _code()
    i = code.index("_execute_tool_with_retry(")
    block = code[i:i + 900]
    assert "logger.warning" in block, "a tool failure leaves no trace"
