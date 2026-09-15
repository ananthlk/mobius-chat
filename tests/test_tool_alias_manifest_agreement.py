"""Every alias must land on a tool react is actually told about.

Found 2026-09-15 by Tool Manifest auditing chat's dispatch against their
catalogue. `tool_manifest.py` retired the recall_search and precision_search
prompt blocks as "merged into search_corpus(mode=...)" and promises "rag: the
ONE retrieval tool" -- but `_TOOL_ALIASES` was not updated with it, so THIRTEEN
aliases still routed into the retired single-arm branches.

Not a hallucinated name hitting nothing. A PLAUSIBLE name -- `lookup`, `exact`,
`broad`, `explore`, `keyword_search`, `semantic_search` -- hitting a real branch
whose documentation had been deleted. The effect was a silent downgrade from the
documented hybrid portfolio to a single retrieval arm, with no error and nothing
in the trace saying a narrower search had been substituted.

The inverse of "branch exists, branch not reached": branch NOT OFFERED, branch
still REACHABLE -- and invisible from the manifest precisely because the
manifest entry is what was removed.
"""
import pytest

from app.pipeline.react_loop import _TOOL_ALIASES, _normalize_tool_name
from app.pipeline import tool_manifest as tm

# The canonical retrieval branch. `rag` is the prompt-facing name;
# `search_corpus` is the dispatch name and they share one branch.
RETRIEVAL = {"rag", "search_corpus"}


@pytest.fixture(scope="module")
def manifest():
    return tm.get_tool_manifest()


def test_no_alias_routes_to_a_tool_the_manifest_never_names(manifest):
    """The property, not the instance. Any future alias pointing at a tool
    react is not told about reintroduces the same silent substitution."""
    orphans = {}
    for alias, target in _TOOL_ALIASES.items():
        if target in RETRIEVAL:
            continue                      # documented as `rag`
        if target not in manifest:
            orphans.setdefault(target, []).append(alias)
    assert not orphans, (
        "aliases route to dispatch targets react is never offered — a "
        f"plausible tool name would silently substitute a different tool: {orphans}"
    )


@pytest.mark.parametrize("word", [
    "lookup", "exact", "exact_match", "keyword_search", "bm25", "bm25_search",
    "broad", "explore", "explore_search", "broad_search", "vector_search",
    "semantic_search", "lazy_corpus_search",
])
def test_plausible_search_words_reach_the_full_portfolio(word):
    """These are words an LLM reaches for unprompted. Each must land on the
    documented hybrid retrieval tool, not a single arm of it."""
    assert _normalize_tool_name(word) in RETRIEVAL, (
        f"{word!r} routes to {_normalize_tool_name(word)!r} — the manifest "
        f"promises one retrieval tool, so this is a silent downgrade"
    )


def test_the_gate_is_not_vacuous(manifest):
    """Passes trivially if the alias map empties or the manifest stops
    rendering. Assert both are real before trusting the checks above."""
    assert len(_TOOL_ALIASES) > 10, "alias map is suspiciously small"
    assert len(manifest) > 1000, "manifest did not render"
    assert "rag" in manifest, "the retrieval tool is not in the manifest at all"
