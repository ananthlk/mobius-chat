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


def test_no_field_in_the_comparison_must_be_identified_BY_ITS_TYPE():
    """The Chat FE seat derived the shadow arm as "the key whose value is a
    string and isn't thread_arm" — correct against the contract I sent them.
    Then I added `view` (a string), and `thread_arm`'s own value is a string,
    so their rule picked THREE candidates where there is one.

    I changed the payload and did not tell them. The fix is not a better
    heuristic on their side; it is to stop asking a reader to infer STRUCTURE
    from a VALUE'S TYPE. `shadow_arms` answers the question they actually have,
    once, as data.
    """
    src = _chat_src()
    i = src.index("A/B FORK, the kebab toggle")
    block = src[i:src.index("return ChatResponse(", i)]
    assert '"shadow_arms": []' in block, "no explicit shadow-arm list"
    assert 'comparison["shadow_arms"].append(_arm)' in block
    assert '"served": True' in block and '"served": False' in block, \
        "which arm was served must be stated, not inferred from thread_arm"


def test_the_shadow_arms_list_survives_a_new_string_field():
    """The regression that caused this: adding ANY string-valued field to the
    comparison block broke a type-based pick. Simulate the shape and assert
    that reading `shadow_arms` is unaffected by unrelated keys."""
    comparison = {
        "thread_arm": "v2",
        "v2": "cid-served", "v1": "cid-shadow",
        "shadow_arms": ["v1"],
        "arms": {"v2": {"correlation_id": "cid-served", "served": True},
                 "v1": {"correlation_id": "cid-shadow", "served": False}},
        "view": "/ab?run=ab-1&q=q01",
        "some_future_string_field": "whatever ships next",
    }
    # the type-based rule, for the record — it picks 4 here
    naive = [k for k, v in comparison.items()
             if isinstance(v, str) and k != comparison["thread_arm"]]
    assert len(naive) > 1, "fixture no longer reproduces the ambiguity"
    # the stated rule picks exactly one, whatever else is added
    assert comparison["shadow_arms"] == ["v1"]
    assert comparison["arms"][comparison["shadow_arms"][0]]["served"] is False


def test_shadows_go_to_a_LOWER_PRIORITY_lane():
    """Observed live while Ananth was testing: served turn 586a74a6 emitted two
    thinking events and never settled AT ALL, while its own shadow completed
    27s later. The arm someone was waiting on is the arm that died.

    The consumer is single-slot — it calls callback() synchronously — so on one
    list a shadow occupies the worker for its whole duration and the next REAL
    question queues behind work no person wants.
    """
    q = pathlib.Path("app/queue/redis_queue.py").read_text()
    assert 'self._shadow_key = f"{self._request_key}:shadow"' in q
    assert 'key = self._shadow_key if payload.get("ab_shadow") else self._request_key' in q
    # SUPERSEDED 2026-09-11: this asserted a priority BRPOP
    # (`r.brpop([request_key, shadow_key])`). That was correct and made things
    # WORSE — see test_the_shadow_lane_has_its_OWN_consumer_thread. The lane
    # SPLIT is still the right thing and is what this test now guards; who
    # drains it moved to its own consumer. Rewritten rather than deleted: a
    # gate quietly removed when its subject changes is how a rule stops
    # existing without anyone deciding to remove it.
    assert "def consume_shadow_requests" in q, \
        "the shadow lane has no consumer at all — shadows would never run"
    chat = _chat_src()
    assert '_p["ab_shadow"] = True' in chat, "shadow turns are not marked"


def test_the_SERVED_turn_is_never_marked_shadow():
    """If the served arm were ever routed to the shadow lane, the person
    waiting would queue behind every comparison — the exact inversion this
    fixes, with the same symptom and the opposite cause."""
    src = _chat_src()
    i = src.index("A/B FORK, the kebab toggle")
    block = src[i:src.index("return ChatResponse(", i)]
    # ab_shadow is set ONLY on the copied shadow payload (_p), never on payload
    assert 'payload["ab_shadow"]' not in block
    assert '_p["ab_shadow"] = True' in block


def test_the_shadow_lane_has_its_OWN_consumer_thread():
    """Priority alone made it worse. With a single synchronous consumer,
    giving served turns precedence meant a shadow could only START once the
    served turn had finished — observed live: served 9ac55ac2 completed at
    23:08:02, its shadow did not begin until 23:09:11, 68s later, long after
    anything was listening. It also destroyed the simultaneity that is the
    fork's entire justification.

    Two consumers, one lane each: a shadow runs ALONGSIDE its served turn.
    """
    q = pathlib.Path("app/queue/redis_queue.py").read_text()
    assert "def consume_shadow_requests" in q
    # the served consumer must no longer read the shadow lane at all
    i = q.index("def consume_requests")
    j = q.index("def consume_shadow_requests")
    served = q[i:j] if i < j else q[i:]
    assert "self._shadow_key" not in served, \
        "the served consumer still drains the shadow lane — it will serialise again"
    w = pathlib.Path("app/worker/run.py").read_text()
    assert "consume_shadow_requests" in w and "daemon=True" in w


def test_a_missing_shadow_lane_cannot_stop_the_worker_starting():
    """A comparison feature must not be able to prevent the product's worker
    from running. The in-memory queue has no shadow lane at all."""
    w = pathlib.Path("app/worker/run.py").read_text()
    assert 'hasattr(q, "consume_shadow_requests")' in w
    i = w.index("A/B SHADOW CONSUMER")
    block = w[i:i + 1400]
    assert "except Exception" in block and "served turns" in block


def test_a_forked_turn_captures_ITSELF_at_settle():
    """The kebab fork registers a run so the comparison has a permalink, and
    nothing captured it — both arms sat at status='running' forever and the
    page rendered two empty columns.

    /ab/ask only worked because its driving script called capture explicitly.
    A person clicking a toggle has no script. The write path had no caller,
    which is the defect this program has now found fourteen times.
    """
    import ast
    h = pathlib.Path("app/api/ab_harness.py").read_text()
    assert "def capture_if_harness_arm" in h
    tree = ast.parse(h)
    fn = next(f for f in ast.walk(tree)
              if isinstance(f, ast.FunctionDef) and f.name == "capture_if_harness_arm")
    src = ast.unparse(fn)
    assert "answer_envelope is null" in src, "it would re-capture an already-frozen arm"
    assert "get_chat_response" in src, "it does not read the real envelope"
    assert "'captured'" in src or '"captured"' in src
    orch = pathlib.Path("app/pipeline/orchestrator.py").read_text()
    assert "capture_if_harness_arm(correlation_id)" in orch, "the hook has no caller"


def test_capture_failure_cannot_touch_the_turn_that_produced_it():
    """Telemetry must never fail a turn — and the envelope is still reachable
    from /chat/response until its TTL expires, so a failed capture costs a
    permalink, not an answer."""
    import ast
    tree = ast.parse(pathlib.Path("app/api/ab_harness.py").read_text())
    fn = next(f for f in ast.walk(tree)
              if isinstance(f, ast.FunctionDef) and f.name == "capture_if_harness_arm")
    assert any(isinstance(n, ast.Try) for n in ast.walk(fn)), "unguarded"
    orch = pathlib.Path("app/pipeline/orchestrator.py").read_text()
    i = orch.index("capture_if_harness_arm(correlation_id)")
    assert "except Exception" in orch[i:i + 300], "the call site is unguarded"
