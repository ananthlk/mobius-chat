"""The gap is the feedback, and the feedback drives the next call.

Ananth, 2026-09-12:
  "we sent rag (or any tool).. react (if not satisfied, it needs to provide
   real feedback in the form of gaps so that we can use that).. i dont think
   the logic we have is required.. this is the piece that is missing"

Two halves, and they only work as a pair:
  REMOVED  the code-level blind retry (same query, LLM never consulted)
  ADDED    the targeted gap becomes the next query
"""
import re

_RAW = open("app/pipeline/react_loop.py").read()


def _strip_comments(text):
    """COMMENTS OUT, always. Six gates in one day passed by matching my own
    prose -- a comment asserting a property is not the property. Everything
    below must match executable code or fail."""
    out = []
    for ln in text.splitlines():
        st = ln.strip()
        if st.startswith("#"):
            continue
        # trailing comment, crude but safe: only when no quote precedes the #
        h = ln.find("#")
        if h > 0 and '"' not in ln[:h] and "'" not in ln[:h]:
            ln = ln[:h]
        out.append(ln)
    return "\n".join(out)


SRC = _strip_comments(_RAW)
LINES = SRC.splitlines()


def _body_of(start_pat, n=60):
    i = next(i for i, l in enumerate(LINES) if start_pat in l)
    return "\n".join(LINES[i:i + n])


# ── the blind retry is gone for v2 ──────────────────────────────────────────

def test_the_low_confidence_loop_cannot_run_under_v2():
    """It re-dispatched the IDENTICAL query string, in code, with the LLM never
    consulted. v2's answer to dissatisfaction is a gap, not a repeat."""
    body = _body_of("_low_confidence_call_number = 1", 40)
    assert "_v2_no_blind_retry" in body
    # The guard must be in the WHILE condition, not merely defined above it.
    w = body[body.index("while ("):]
    assert "not _v2_no_blind_retry" in w[:200], w[:200]


def test_the_non_retry_is_announced():
    """A silent non-retry looks like a turn that gave up. The user is owed the
    difference between 'we stopped asking' and 'we stopped'."""
    body = _body_of("_low_confidence_call_number = 1", 30)
    assert "not re-asking the same question" in body


def test_v1_keeps_the_loop():
    """v1 is the arm whose behaviour is known and the loop is load-bearing
    there. A fix that changes both arms cannot be attributed to either."""
    body = _body_of("_low_confidence_call_number = 1", 40)
    assert 'orchestrator_version", "v1") == "v2"' in body


# ── the gap drives the next call ────────────────────────────────────────────

def test_the_targeted_gap_becomes_the_query():
    """Before this, preload ran ONCE on the raw user message and gaps were
    consumed by nothing -- react could name what was missing and no tool call
    was ever driven by it."""
    body = _body_of("_regap_ran = False", 55)
    assert "_v2pre2.execute(" in body
    # The gap TEXT is the query argument -- not ctx.message, not the root.
    assert "_steer_gap.text," in body, "re-preload is not driven by the gap"


def test_the_re_preload_is_budget_gated():
    """A re-preload is a real rag call. Spending it past the promise is the
    exact failure this governor exists to prevent."""
    body = _body_of("_regap_ran = False", 55)
    assert "react_hard_ceiling_s" in body and "_regap_left > _regap_cost" in body


def test_skipping_on_budget_is_said_out_loud():
    """A silent skip is indistinguishable from a gap nobody worked -- which is
    the never-searched/searched-empty collapse, one level up."""
    body = _body_of("_regap_ran = False", 60)
    assert "Not enough time left to search" in body


def test_round_one_does_not_re_preload():
    """Round 1's preload already ran on the question; re-running it as a 'gap'
    would be the blind retry we just deleted, wearing a new name."""
    body = _body_of("_regap_ran = False", 12)
    assert "rn > 1" in body


def test_the_preload_flag_is_recomputed_after_the_gap_call():
    """_v2_has_preload feeds Ctx.preloaded, which selects the judging role. A
    stale False renders 'go and search' directly above the search results."""
    i = SRC.index("_regap_ran = False")
    j = SRC.index("_v2_ctx = _v2st.Ctx(", i)
    assert "_v2_has_preload = bool(getattr(ctx, \"_v2_preloaded\", None))" in SRC[i:j]


def test_one_preload_implementation_not_two():
    """Both the round-1 preload and the gap re-preload go through
    preload.execute(). A second implementation would drift from the first."""
    assert len(re.findall(r"_v2pre2?\.execute\(", SRC)) == 2
