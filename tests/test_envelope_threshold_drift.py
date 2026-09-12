"""R5 acceptance: the frontend's threshold mirror cannot drift from Python.

Before this, the stats cap of 4 existed as a Python comment and as a literal
`slice(0, 4)` in bubble.ts. Nothing connected them; either could move alone
and the only symptom would be stat tiles silently disappearing off the end of
a card. Now one is generated from the other and this test is the gate.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
GENERATED = REPO / "frontend" / "src" / "generated" / "envelope-thresholds.ts"


def test_generated_frontend_thresholds_are_in_sync():
    result = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "gen_envelope_thresholds.py"), "--check"],
        capture_output=True,
        text=True,
        cwd=REPO,
    )
    assert result.returncode == 0, (
        f"{result.stdout}{result.stderr}\n"
        "Run: python3 scripts/gen_envelope_thresholds.py"
    )


def test_frontend_stats_cap_uses_the_constant():
    """The specific literal that started this: bubble.ts capped stat tiles at
    a hardcoded 4. Pinning the import means a future edit that reintroduces a
    literal fails here rather than in production."""
    source = (REPO / "frontend" / "src" / "render" / "bubble.ts").read_text()
    assert "STATS_MAX_ITEMS" in source
    # Scoped to the stats renderer: an unrelated `slice(0, 4)` elsewhere in
    # the file (follow-up chips) is not this constant and must not fail here.
    assert "data.items.slice(0, 4)" not in source


def test_no_threshold_literals_in_the_classifier():
    """R5 acceptance criterion: no numeric threshold appears as a literal in
    the classifier. Bounds come from the contract or they are not bounds."""
    source = (REPO / "app" / "responder" / "envelope_classifier.py").read_text()
    body = source.split('"""', 2)[-1]
    # Strip comments and docstrings before looking for bare comparisons.
    body = re.sub(r"#.*", "", body)
    body = re.sub(r'""".*?"""', "", body, flags=re.DOTALL)
    offenders = re.findall(r"(?:len\([^)]*\)|\bweight\b)\s*(?:[<>]=?|==)\s*([0-9]+)", body)
    # `!= None`-style comparisons against 0/1 are structural, not thresholds.
    offenders = [o for o in offenders if o not in {"0", "1"}]
    assert offenders == [], f"hardcoded thresholds in classifier: {offenders}"


def test_generated_file_is_marked_generated():
    assert "DO NOT EDIT" in GENERATED.read_text()
