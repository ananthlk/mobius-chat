"""The committed frontend bundles must match their sources.

frontend/static/app.js and ab.js are COMMITTED BUILD ARTIFACTS, and nothing
builds them: the Dockerfile only COPYs, and scripts/deploy.sh has no build
step. So the bundle ships exactly as committed, and a change to src/ that
nobody rebuilt is a change that never reaches a user.

It had already happened. Found by Governor, 2026-09-15: app.js was last built
09-11, so 25 lines of committed src — bubble.ts and
generated/envelope-thresholds.ts, landed 09-13 — had never been served. The
tests passed the whole time, because the tests read src and the product reads
the artifact.

THAT IS THE SAME DEFECT SHAPE AS THREE OTHERS THIS WEEK, and it is the reason
this file exists rather than a note in a README:

    the `presentation` key         tests read the formatter, the product
                                   reads a rebuilt card through an allowlist
    the stats-tile cap             a literal in bubble.ts, a comment in Python
    this                           tests read src, the product reads a bundle

Every one of them was invisible until something compared the two paths. This
is that comparison, run in CI.

MTIME IS NOT USABLE — git does not preserve it, so a fresh clone has every
file the same age. The only honest check is to build and compare bytes.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

FRONTEND = Path(__file__).resolve().parent.parent / "frontend"

#: (committed artifact, entry point). Both are served; both are committed.
BUNDLES = [("static/app.js", "src/app.ts"), ("static/ab.js", "src/ab.ts")]


def _esbuild_available() -> bool:
    return (FRONTEND / "node_modules" / ".bin" / "esbuild").exists() or bool(
        shutil.which("esbuild"))


@pytest.mark.skipif(not _esbuild_available(),
                    reason="esbuild not installed in this checkout (npm install in frontend/)")
@pytest.mark.parametrize("artifact,entry", BUNDLES, ids=[a for a, _ in BUNDLES])
def test_the_committed_bundle_matches_a_fresh_build(artifact, entry, tmp_path):
    committed = FRONTEND / artifact
    assert committed.exists(), f"{artifact} is committed and served; it is missing"

    fresh = tmp_path / "fresh.js"
    result = subprocess.run(
        [str(FRONTEND / "node_modules" / ".bin" / "esbuild"), entry,
         "--bundle", f"--outfile={fresh}", "--format=esm", "--target=es2020"],
        cwd=FRONTEND, capture_output=True, text=True,
    )
    assert result.returncode == 0, f"esbuild failed:\n{result.stderr}"

    if fresh.read_bytes() != committed.read_bytes():
        pytest.fail(
            f"{artifact} is STALE — src has changed since it was last built, so "
            f"the change is committed but never served.\n"
            f"    fresh {len(fresh.read_bytes()):,} bytes vs committed "
            f"{len(committed.read_bytes()):,}\n"
            f"Fix: cd frontend && npm run build, then commit the artifact.\n"
            f"Nothing builds these at deploy time — the Dockerfile only COPYs."
        )
