"""A classifier that did not answer must not be reported as PHI found.

LIVE, 2026-09-16. Ananth's question — "how do i appeal a CARC 22 denial for
sunshine health?" — was refused:

    {"phi_blocked": true, "phi_evidence": [], "identifier_labels": [],
     "message": "Message contains PHI — remove or override to proceed."}

The gate had returned gate='indeterminate', phi_flag=False, evidence=[]. Chat's
own log said why: "[phi-gate] /message-check unreachable (The read operation
timed out) — blocking (fail-closed)", three times in an hour. The classifier
answers in 0.15s (five probes, 0.136-0.230s) and Cloud Run scales it to zero,
so a cold start blew the 4s budget.

We accused a person of sending patient data because our classifier was asleep,
and handed them an empty evidence list to argue with.

THE FAIL-CLOSED POLICY IS CORRECT AND UNCHANGED. A gate bypassable by taking
the classifier offline is not a gate. What changes is that "we could not check"
and "we checked and found PHI" stop being the same sentence — the distinction
was already in the payload as gate='indeterminate' and was discarded at the
422.
"""

import ast
import inspect

import app.api.chat as C


def _detail_src():
    """The 422 construction, as the program holds it."""
    return inspect.getsource(C)


def test_the_check_retries_once_before_blocking():
    """The failure is a cold start; the cost of not retrying is a false
    accusation. Both attempts must fail to block."""
    src = inspect.getsource(C._phi_check_message)
    tree = ast.parse(src.strip())
    loops = [n for n in ast.walk(tree) if isinstance(n, ast.For)]
    assert loops, "no retry loop — one cold start still refuses the turn"
    # THE LOOP MUST ACTUALLY RETRY. My first version asserted only that a For
    # existed and passed on `for _attempt in (1,)` — a loop that tries once is
    # the defect wearing the shape of the fix.
    attempts = 0
    for lp in loops:
        it = lp.iter
        if isinstance(it, ast.Tuple):
            attempts = max(attempts, len(it.elts))
        elif isinstance(it, ast.Call) and getattr(it.func, "id", "") == "range":
            args = [a.value for a in it.args if isinstance(a, ast.Constant)]
            if len(args) == 1:
                attempts = max(attempts, args[0])
            elif len(args) >= 2:
                attempts = max(attempts, args[1] - args[0])
    assert attempts >= 2, (
        f"the retry loop makes {attempts} attempt(s) — a single cold start "
        "still refuses a legitimate turn as PHI")
    # and it must still fail closed
    assert '"block": True' in src and '"indeterminate"' in src


def test_a_non_200_is_not_retried():
    """A non-200 is the service ANSWERING badly, not a cold start. Retrying
    doubles the wait before the same block."""
    src = inspect.getsource(C._phi_check_message)
    assert "break" in src, "a non-200 falls through to a pointless second try"


def test_indeterminate_and_detected_are_different_facts_in_the_response():
    """Asserts the 422 carries a machine-readable reason, not just prose — the
    front end can then offer 'try again' for one and 'edit your message' for
    the other."""
    src = _detail_src()
    assert '"reason": "indeterminate" if _indeterminate else "phi_detected"' in src
    assert '"retryable": _indeterminate' in src


def test_an_unverified_message_is_not_called_PHI():
    """THE DEFECT. The user-facing sentence must not assert PHI when nothing
    was checked."""
    src = _detail_src()
    i = src.index('"reason": "indeterminate"')
    window = src[i:i + 1200]
    assert "could not verify" in window, (
        "the indeterminate branch does not say we could not check")
    # the accusation must be reachable ONLY on the detected branch
    assert "Message contains PHI" in window
    assert window.index("could not verify") < window.index("Message contains PHI"), (
        "the PHI accusation is not behind the else-branch")


def test_the_gate_still_blocks_when_it_cannot_check():
    """Fail-closed is the policy and this change must not soften it."""
    src = inspect.getsource(C._phi_check_message)
    assert 'return {"block": True' in src, (
        "an unreachable classifier no longer blocks — the gate is now "
        "bypassable by taking it offline")
