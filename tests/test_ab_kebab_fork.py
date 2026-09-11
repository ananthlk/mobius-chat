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
    assert "comparison = None" in block.split("except Exception")[1], \
        "a failed fork leaves a partial comparison in the response"


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
