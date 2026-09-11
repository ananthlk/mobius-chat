"""The kebab A/B toggle — fork a turn in the surface people already use.

Ananth, 2026-09-11: "this query invokes A/B therefore I am going to fork and
show", the toggle lives in the kebab menu, "persists until switched off", and
"no promotion for now".
"""

import ast
import pathlib


def _chat_src():
    return pathlib.Path("app/api/chat.py").read_text()


def test_the_shadow_arm_never_runs_in_the_users_thread():
    """THE load-bearing test.

    Thread history is keyed on thread_id. A shadow arm sharing the user's
    thread would write a second assistant turn into the conversation, and the
    next question would read both — so the comparison would corrupt the thing
    it is comparing. A fresh thread per shadow is what keeps the fork
    invisible to the conversation.
    """
    src = _chat_src()
    i = src.index("A/B FORK, the kebab toggle")
    block = src[i:src.index("return ChatResponse(", i)]
    code = "\n".join(ln.split("#")[0] for ln in block.splitlines())
    assert "ensure_thread(None)" in code, "the shadow arm may be reusing the user's thread"
    assert '_p["thread_id"] = _shadow_thread' in code
    # and the user's own thread_id must never be handed to a shadow payload
    assert "_p[\"thread_id\"] = thread_id" not in code


def test_forking_does_not_change_which_orchestrator_serves_the_person():
    """The thread's arm is whatever ROUTING already chose. If the toggle
    pinned the thread to v1, turning it on would silently alter the product
    for anyone using it, and the comparison would be measuring a turn nobody
    would otherwise have had."""
    src = _chat_src()
    i = src.index("A/B FORK, the kebab toggle")
    block = src[i:src.index("return ChatResponse(", i)]
    assert "_assign(correlation_id, _pct)" in block, \
        "the thread's arm is not taken from routing"


def test_a_failed_fork_degrades_to_a_normal_turn_and_returns_NO_comparison():
    """The thread's turn is already enqueued and unaffected. But a comparison
    block containing ONE arm is worse than none — a page renders it as "the
    other one had nothing", which is absence dressed as a result."""
    src = _chat_src()
    i = src.index("A/B FORK, the kebab toggle")
    block = src[i:src.index("return ChatResponse(", i)]
    # Target the OUTER handler by its own log line. A first version split on
    # "except Exception" and took [1] — which broke the moment an inner
    # try/except was added for the permalink, and would have broken silently
    # in the other direction: if the outer handler were REMOVED, [1] would
    # have found the inner one and passed.
    i = block.index('logger.warning("[v2.ab] fork failed')
    assert "comparison = None" in block[i:], \
        "the outer handler leaves a partial comparison in the response"


def test_the_shadow_turn_does_not_reuse_the_threads_promise():
    """An attestation keyed on the thread turn's promise would report two
    deliveries against one contract — two rows, one promise_version, and a
    kept/missed pair that cannot both be true of it."""
    src = _chat_src()
    i = src.index("A/B FORK, the kebab toggle")
    block = src[i:src.index("return ChatResponse(", i)]
    assert '_p.pop("promise", None)' in block


def test_the_server_does_not_REMEMBER_the_toggle():
    """"Persists until switched off" is the CLIENT's job. A server-side
    per-thread flag would keep forking after a tab closed or a session changed
    hands, and doubling spend should not outlive the intent behind it."""
    src = _chat_src()
    # the flag is read off the request body, never loaded from thread state
    assert "body.ab_fork" in src
    i = src.index("A/B FORK, the kebab toggle")
    block = src[i:src.index("return ChatResponse(", i)]
    for persisted in ("get_state", "thread_state", "load_state"):
        assert persisted not in block, f"the toggle is read from {persisted}"


def test_ab_fork_is_gated_and_doubles_the_turn_in_the_docstring():
    """It doubles the work of a turn: two pipelines, two model calls, two tool
    runs. That must be stated where the field is declared, not discovered from
    a bill."""
    src = _chat_src()
    tree = ast.parse(src)
    cls = next(n for n in ast.walk(tree)
               if isinstance(n, ast.ClassDef) and n.name == "ChatRequest")
    body = ast.unparse(cls)
    assert "DOUBLES" in body or "doubles" in body
    assert 'os.environ.get("MOBIUS_V2_AB_FORK"' in src


def test_a_chat_fork_is_MARKED_as_one_and_names_the_served_arm():
    """A chat fork is NOT the same object as a lab-bench run: one of its arms
    was served to a person and is in their thread. A reader who cannot tell
    those apart will eventually quote a chat fork as though nobody was served
    — and the harness banner says, in every render, "Nobody was served."
    """
    import ast
    tree = ast.parse(pathlib.Path("app/api/ab_harness.py").read_text())
    fn = next(f for f in ast.walk(tree)
              if isinstance(f, ast.FunctionDef) and f.name == "register_chat_fork")
    src = ast.unparse(fn)
    assert "'chat_fork'" in src or '"chat_fork"' in src
    assert "thread_arm" in src, "the run does not record which arm was served"
    assert "served in the thread" in src, "the arm labels do not say which was served"
    # the asymmetry a reader will otherwise mistake for v1 forgetting
    assert "answers cold" in src


def test_view_registration_failure_does_not_lose_the_second_arm():
    """A missing permalink is a missing link. A raised exception would have
    cost the whole comparison — the expensive half of a turn the user
    deliberately paid double for."""
    src = _chat_src()
    i = src.index("Register the pair as an ad-hoc harness run")
    block = src[i:i + 1400]
    assert "try:" in block and "except Exception" in block
    assert "view registration failed" in block


def test_the_chat_fork_does_not_build_a_second_renderer():
    """The two-column page is keyed on run_id + question_id, so a chat fork
    registers a run rather than growing a second two-column renderer inside
    the bubble. A comparison rendered differently from the product measures
    the renderer — which is what I told the FE seat, and it applies to me."""
    src = _chat_src()
    i = src.index("A/B FORK, the kebab toggle")
    block = src[i:src.index("return ChatResponse(", i)]
    assert 'comparison["view"]' in block
    for renderer_ish in ("innerHTML", "render_blocks", "build_assistant_envelope"):
        assert renderer_ish not in block, f"the fork path renders ({renderer_ish})"
