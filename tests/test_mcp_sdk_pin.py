"""The MCP SDK major version must be pinned, and chat must match it.

2026-09-15: `mcp>=1.0.0` was unpinned. The image resolved 2.2.0, which renamed
the streamable-http transport and dropped the legacy alias. Tool Manifest's
vendored library imported the old name, so every MCP tool was unreachable in
the deployed process — while their 410 tests passed against 1.26.0, where both
spellings still exist.

chat's own client was already 2.x-correct, which means the exposure ran the
OTHER way too: an older resolution would have broken chat instead. Neither
direction was declared anywhere.
"""
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _req() -> str:
    return (ROOT / "requirements.txt").read_text()


def test_the_mcp_sdk_major_is_pinned():
    """🔴 An unpinned major let a renamed API into the image silently, and the
    only place it surfaced was production."""
    line = [l for l in _req().splitlines()
            if l.strip().startswith("mcp") and "=" in l and not l.strip().startswith("#")]
    assert line, "no mcp requirement found"
    spec = line[0]
    assert re.search(r"<\s*\d", spec), (
        f"mcp has no upper bound: {spec!r} — a major-version rename can enter "
        f"the image without a code change"
    )


def test_chat_uses_the_transport_name_its_pin_provides():
    """The pin and the import must agree. chat imports the 2.x spelling
    (`streamable_http_client`); pinning back to 1.x would break chat itself."""
    users = list((ROOT / "app").rglob("*.py"))
    importers = [p for p in users
                 if "from mcp.client.streamable_http import" in p.read_text(errors="ignore")]
    assert importers, "no module imports the streamable-http transport"
    for p in importers:
        txt = p.read_text(errors="ignore")
        assert "import streamable_http_client" in txt, (
            f"{p.relative_to(ROOT)} imports the legacy transport name, which "
            f"mcp 2.x removed"
        )


def test_the_stream_tuple_is_unpacked_by_index_not_arity():
    """1.x yields (read, write, get_session_id); 2.x yields (read, write).
    Unpacking by arity moves a rename failure to a ValueError — which is how
    a second, independent defect hid behind the first."""
    for p in (ROOT / "app").rglob("*.py"):
        txt = p.read_text(errors="ignore")
        if "streamable_http_client(" not in txt:
            continue
        assert not re.search(r"\b\w+\s*,\s*\w+\s*,\s*\w+\s*=\s*await?\s*streamable_http_client",
                             txt), f"{p.relative_to(ROOT)} unpacks the stream tuple by arity"
