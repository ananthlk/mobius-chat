"""The A/B harness depends on the ABSENCE of retention. Assert it.

Ananth: "the setup will exist potentially forever."

The FE seat's point, and it is the right one: verifying by grep proves TODAY.
"It's a table, not TTL'd" stays true until someone adds a prune job -- and the
person who adds it will not know this contract depends on its absence. A
dependency that exists only as a comment is not a dependency; it is a hope.

This is the same family as the four comment-only rules found on 2026-09-10, and
the fix is the same shape: make the rule fail loudly when it is broken, rather
than discovering it when a six-month-old comparison renders empty.

If you are here because this test failed, you did nothing wrong -- you added
retention, which is reasonable. What you need to know is that the A/B harness
serves comparisons that are expected to be re-openable indefinitely, and that
GET /chat/response is already TTL'd (which is why ab_run_questions snapshots the
envelope in the first place). Either exempt these tables or accept that runs
expire, and say which in the contract.
"""

import re
from pathlib import Path

import pytest

# Tables the harness must be able to read for the life of a run.
PROTECTED = (
    "ab_runs",
    "ab_run_questions",
    "ab_verdicts",
    "turn_rounds",
    "turn_attestations",
    "thread_gaps",
)

_DESTRUCTIVE = re.compile(
    r"\b(DELETE\s+FROM|TRUNCATE(\s+TABLE)?|DROP\s+TABLE)\s+(?:IF\s+EXISTS\s+)?"
    r"(?P<table>[a-z_\.\"]+)",
    re.IGNORECASE,
)


def _sources():
    root = Path("app")
    for p in list(root.rglob("*.py")) + list(Path("scripts").rglob("*.py")):
        if "test" in p.parts or p.name.startswith("test_"):
            continue
        yield p


def test_nothing_deletes_from_the_tables_the_harness_depends_on():
    hits = []
    for path in _sources():
        try:
            text = path.read_text()
        except Exception:
            continue
        for m in _DESTRUCTIVE.finditer(text):
            table = m.group("table").strip('"').split(".")[-1]
            if table in PROTECTED:
                line = text[: m.start()].count("\n") + 1
                hits.append(f"{path}:{line} {m.group(0)}")
    assert hits == [], (
        "A/B harness durability broken -- something now removes rows the harness "
        "must still be able to read:\n  " + "\n  ".join(hits) +
        "\n\nSee this module's docstring before exempting."
    )


@pytest.mark.parametrize("table", PROTECTED)
def test_the_guard_actually_catches_a_deletion(tmp_path, table):
    """MUTATION CHECK, in-test rather than by hand.

    A guard nobody has seen fail is a guard nobody knows works -- the failure
    this program found three green tests running against 209 unreachable lines.
    """
    probe = f'cur.execute("DELETE FROM {table} WHERE thread_id = %s", (tid,))'
    assert _DESTRUCTIVE.search(probe), f"regex misses a plain deletion of {table}"
    m = _DESTRUCTIVE.search(probe)
    assert m.group("table").strip('"').split(".")[-1] == table


def test_the_guard_does_not_fire_on_unprotected_tables():
    """Deleting chat_tool_results or user_tool_subscriptions is existing, correct
    behaviour. A guard that flags legitimate code gets disabled."""
    for benign in (
        'cur.execute("DELETE FROM chat_tool_results WHERE thread_id = %s", (t,))',
        '"DELETE FROM user_tool_subscriptions WHERE user_id = :uid"',
    ):
        m = _DESTRUCTIVE.search(benign)
        assert m is not None
        assert m.group("table").strip('"').split(".")[-1] not in PROTECTED
