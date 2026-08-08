"""Task #77 (2026-08-08, Chat Master): guard against prompt_blocks seed
version drift.

The incident: react.critical_rules's Python constant (REACT_CRITICAL_RULES_TEXT)
gained a full evidence_review/citation-discipline framework, but the DB's
active row for that block_key was never re-seeded to match -- silent for
~a day, no errors, production just served the stale prompt. The root cause
is structural, not a one-off mistake: prompt_blocks is append-only
(`ON CONFLICT (block_key, version) DO NOTHING`), so editing a block's body
WITHOUT bumping its `version` field is a silent no-op on the next seed()
run -- the old row is never touched, no error, no diff visible anywhere
except a manual DB read.

This test queries the live DB (requires_rag) for each block_key's highest
ACTIVE version and compares its content hash against what the current
Python source (block_seed.py + react_block_seed.py's BlockSpecs, resolved
exactly as seed() would resolve them) would produce for that SAME version
number. A mismatch means: either the version was bumped in Python but
never seeded (DB stuck on the old version's content), or the DB has
content that no longer matches what today's code would write for that
version at all -- both are the drift this exists to catch, at commit
time, not a day later in production.

Generic across block owners by design (Chat Master: "covers all block
owners structurally, not just react") -- add a new seed module's
(module, resolver) pair to `_SEED_MODULES` below and it's covered with no
other test changes.
"""
from __future__ import annotations

import hashlib

import pytest

from app.db_client import db_query

pytestmark = pytest.mark.requires_rag


def _content_hash(body: str) -> str:
    return hashlib.sha256((body or "").encode()).hexdigest()


def _react_block_seed_specs() -> list[tuple[str, int, str]]:
    """(block_key, version, resolved_body) for every block
    react_block_seed.py currently defines."""
    from app.services.react_block_seed import _react_block_specs, _to_block

    return [
        (spec.block_key, spec.version, _to_block(spec).template_body)
        for spec in _react_block_specs()
    ]


# module label -> spec resolver. Add a new seed module here to cover it --
# no other change needed, the drift check below iterates this list.
#
# block_seed.py (the integrator/enricher module) is DELIBERATELY NOT included.
# Verified live: its BlockSpec has no version field at all -- _to_block()
# hardcodes version=1 unconditionally, so Python-side can never represent
# anything past v1. The DB's actual active versions for those blocks are 8
# and 14 (module.enricher / enricher.answercard_schema_and_rules), created via
# app/api/admin_prompts.py's Prompt Composition Studio -- a live-editing UI
# that writes new prompt_blocks rows directly, entirely independent of
# block_seed.py's static BLOCK_SPECS list. Comparing those against "highest
# active DB version" would be a permanent, structural false positive: they
# were never meant to track ongoing edits after the initial migration seed.
# This test's pattern (Python constant -> versioned BlockSpec -> seed()) only
# fits modules that actually work that way -- today, only react_block_seed.py.
# Add a future module here ONLY if it follows the same versioned-constant
# pattern, not just because it also defines prompt_blocks rows.
_SEED_MODULES: list[tuple[str, "callable"]] = [
    ("react_block_seed", _react_block_seed_specs),
]


def _all_current_specs() -> dict[str, tuple[int, str]]:
    """block_key -> (version, resolved_body) across every registered seed
    module. Fails loudly (not silently overwrites) on a block_key defined
    by two different modules -- that's a namespace collision, a bug in
    its own right, not something this test should paper over."""
    out: dict[str, tuple[int, str]] = {}
    seen_in: dict[str, str] = {}
    for module_name, resolver in _SEED_MODULES:
        for block_key, version, body in resolver():
            if block_key in seen_in:
                raise AssertionError(
                    f"block_key {block_key!r} is defined by both {seen_in[block_key]!r} "
                    f"and {module_name!r} -- namespace collision between seed modules."
                )
            seen_in[block_key] = module_name
            out[block_key] = (version, body)
    return out


def _highest_active_db_rows(block_keys: list[str]) -> dict[str, tuple[int, str]]:
    """block_key -> (highest active version, its template_body), read live
    from prompt_blocks. Absent from the dict = no active row at all for
    that block_key (never seeded, or fully deactivated)."""
    if not block_keys:
        return {}
    result = db_query(
        """
        SELECT DISTINCT ON (block_key) block_key, version, template_body
        FROM prompt_blocks
        WHERE block_key = ANY(:keys) AND active = true
        ORDER BY block_key, version DESC
        """,
        "chat",
        params={"keys": block_keys},
    )
    err = result.get("error") if isinstance(result, dict) else None
    if err:
        msg = err.get("message", "") if isinstance(err, dict) else str(err)
        pytest.skip(f"prompt_blocks query failed (DB unavailable?): {msg}")
    cols = result.get("columns") or []
    rows = [dict(zip(cols, r)) for r in (result.get("rows") or [])]
    return {r["block_key"]: (r["version"], r["template_body"]) for r in rows}


class TestPromptBlockSeedDrift:
    def test_every_seed_module_is_internally_consistent(self):
        """Sanity check the fixtures themselves before hitting the DB --
        a resolver returning garbage should fail here, not read as a false
        "drift" positive below."""
        specs = _all_current_specs()
        assert specs, "no block specs found across any registered seed module"
        for block_key, (version, body) in specs.items():
            assert isinstance(version, int) and version >= 1, block_key
            assert isinstance(body, str) and body.strip(), \
                f"{block_key} resolved to an empty/non-string body"

    def test_db_highest_active_version_matches_current_python_source(self):
        """The actual drift guard. For every block_key Python currently
        defines, the DB's highest ACTIVE version must be the SAME version
        number Python defines AND have byte-identical content -- if either
        differs, either the version was bumped without re-seeding (the
        react.critical_rules incident) or the DB has drifted from source
        some other way. Both are failures worth blocking on."""
        current = _all_current_specs()
        db_rows = _highest_active_db_rows(list(current.keys()))

        never_seeded = []
        version_behind = []
        content_mismatch = []

        for block_key, (py_version, py_body) in current.items():
            if block_key not in db_rows:
                never_seeded.append(block_key)
                continue
            db_version, db_body = db_rows[block_key]
            if db_version != py_version:
                version_behind.append(
                    f"{block_key}: python version={py_version}, DB highest active version={db_version}"
                )
                continue
            if _content_hash(db_body) != _content_hash(py_body):
                content_mismatch.append(
                    f"{block_key} v{py_version}: DB content hash {_content_hash(db_body)[:12]} != "
                    f"python-resolved hash {_content_hash(py_body)[:12]} -- same version number, "
                    f"different content. Body edited without a version bump?"
                )

        problems = []
        if version_behind:
            problems.append(
                "SEED DRIFT -- Python bumped a block's version but it was never re-seeded to the DB "
                "(run the module's seed() against the target DB):\n  " + "\n  ".join(version_behind)
            )
        if content_mismatch:
            problems.append(
                "CONTENT DRIFT -- same (block_key, version) pair, different content between Python "
                "and the DB. A version number must be bumped whenever body content changes "
                "(prompt_blocks is append-only, ON CONFLICT DO NOTHING silently keeps stale content "
                "otherwise):\n  " + "\n  ".join(content_mismatch)
            )
        assert not problems, "\n\n".join(problems)

        if never_seeded:
            # Not a failure -- a block defined in Python but never seeded at all is
            # normal for in-progress work (not yet deployed), distinct from DRIFT
            # (stale existing content). Surfaced so it's visible, not silently ignored.
            print(f"\n[prompt_block_seed_drift] never seeded to this DB yet: {never_seeded}")
