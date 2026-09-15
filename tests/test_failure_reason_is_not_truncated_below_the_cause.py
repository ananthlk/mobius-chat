"""A failure reason must not be cut before the cause.

2026-09-15, three diagnoses blocked by one slice. The reason is built as
"<service url>: <exception>", so a 71-character Cloud Run URL owns the head and
the actual cause sits past a 120-character cut:

    …/mcp: ImportError: cannot import name 'streamablehttp_client'   <- cut here
    …/mcp: ExceptionGroup: unhandled errors in a TaskGroup           <- cut here

Each recovery cost a deploy cycle or another seat reading a log. The emit is
the diagnostic channel that matters: `gcloud logging` is PERMISSION_DENIED for
this account, so `logger.info` writes somewhere unreadable.
"""
import inspect
import re

from app.pipeline import react_loop

# Long enough for "<url>: <Type>: <message>" plus a chained cause. Not
# unbounded — a stack trace in a thinking log is its own problem.
MIN_REASON_CHARS = 400


def _slices_of(name: str, src: str):
    """Every `reason[:N]`-style truncation applied to a failure reason."""
    return [int(m.group(1)) for m in re.finditer(r"reason[^\[\n]{0,20}\[:(\d+)\]", src)]


def test_no_failure_reason_is_cut_below_the_cause():
    """🔴 If this fails, someone re-narrowed a diagnostic and the next person
    to debug an MCP or transport failure will see a URL and nothing else."""
    for fn in (react_loop._preload_runner_toolreg, react_loop._execute_via_toolreg):
        src = inspect.getsource(fn)
        tight = [n for n in _slices_of(fn.__name__, src) if n < MIN_REASON_CHARS]
        assert not tight, (
            f"{fn.__name__} truncates a failure reason to {tight} chars — the "
            f"service URL alone is ~71, so the cause is cut off"
        )


def test_the_reason_reaches_the_emit_not_only_the_log():
    """logger.info is unreadable without log access. The emit is what a reader
    actually has, so the reason must be in it."""
    src = inspect.getsource(react_loop._preload_runner_toolreg)
    i = src.index("COULD NOT RUN")
    # the summary handed back (and emitted) must carry the reason, not a code
    assert "reason" in src[i - 400:i + 400].lower()
