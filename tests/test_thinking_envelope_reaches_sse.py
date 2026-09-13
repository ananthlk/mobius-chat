"""The structured envelope must survive the gate and reach the SSE event.

2026-09-13: send_to_user read only payload["content"]; the "envelope" key that
on_thinking has passed since Sprint A.1 was discarded there. append_thinking
then published {line, ts} and nothing else. Net effect: NO structured signal
ever reached the LIVE stream -- react_trace, retrieval_trace and v2_trace were
all legible only post-turn off chat_turns.thinking_log.

These tests drive the REAL send_to_user and read the REAL published event, so
they fail if either half of the seam stops carrying the envelope.
"""
import pytest

from app.communication.gate import send_to_user
from app.storage import progress as P


@pytest.fixture
def cid(monkeypatch):
    """A live turn in the progress registry, with publishing captured."""
    c = "cid-envelope-test"
    with P._lock:
        P._progress[c] = {"thinking": [], "events": []}
    published: list[dict] = []
    monkeypatch.setattr(P, "_publish_progress_events_ordered",
                        lambda _c, evs: published.extend(evs))
    yield c, published
    with P._lock:
        P._progress.pop(c, None)


def _thinking_events(published):
    return [e for e in published if e.get("event") == "thinking"]


def test_envelope_rides_the_sse_thinking_event(cid):
    c, published = cid
    env = {"signal": "v2_trace", "note": "✓ Found 15 passage(s)",
           "data": {"detail": ["  from:  rag"], "key": "preload", "state": "done"}}
    send_to_user(c, {"type": "thinking", "content": env["note"], "envelope": env})

    evs = _thinking_events(published)
    assert len(evs) == 1, f"expected one thinking event, got {len(evs)}"
    data = evs[0]["data"]
    assert data["line"] == "✓ Found 15 passage(s)", "the line must stay authoritative"
    assert data.get("envelope") == env, (
        "envelope did not survive the gate -> append_thinking seam; the live "
        "stream can only render the bare headline"
    )


def test_legacy_string_emit_is_unchanged(cid):
    """No envelope key at all -- the event must look exactly as it always did."""
    c, published = cid
    send_to_user(c, {"type": "thinking", "content": "◌ Searching…"})
    data = _thinking_events(published)[0]["data"]
    assert data["line"] == "◌ Searching…"
    assert "envelope" not in data, "a legacy emit must not grow an envelope key"


def test_multiline_chunk_attaches_the_envelope_once(cid):
    """One envelope describes one step; repeating it would make N rows of one."""
    c, published = cid
    env = {"signal": "v2_trace", "note": "head"}
    send_to_user(c, {"type": "thinking", "content": "head\nsecond\nthird",
                     "envelope": env})
    evs = _thinking_events(published)
    assert [e["data"]["line"] for e in evs] == ["head", "second", "third"]
    assert [("envelope" in e["data"]) for e in evs] == [True, False, False]


def test_non_dict_envelope_is_refused_not_forwarded(cid):
    """A dataclass or string in that slot must not reach the wire as one."""
    c, published = cid
    send_to_user(c, {"type": "thinking", "content": "head",
                     "envelope": "EmitEnvelope(signal='v2_trace'...)"})
    assert "envelope" not in _thinking_events(published)[0]["data"]
