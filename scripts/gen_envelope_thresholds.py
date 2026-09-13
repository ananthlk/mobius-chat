#!/usr/bin/env python3
"""Generate the frontend mirror of the envelope thresholds (R5).

    python3 scripts/gen_envelope_thresholds.py           # write the file
    python3 scripts/gen_envelope_thresholds.py --check    # CI: fail on drift

The frontend cannot import Python, so the numbers have to exist twice. The
answer is not discipline -- it is that the second copy is generated and that
CI fails when it drifts. tests/test_envelope_threshold_drift.py runs --check.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from mobius_contracts.taxonomies.envelope_thresholds import EXPORTED

OUT_PATH = Path(__file__).resolve().parent.parent / "frontend" / "src" / "generated" / "envelope-thresholds.ts"

HEADER = """// GENERATED FILE -- DO NOT EDIT BY HAND.
// Source: mobius_contracts/taxonomies/envelope_thresholds.py
// Regenerate: python3 scripts/gen_envelope_thresholds.py
// CI fails if this file drifts from the Python source (R5).
"""


def render() -> str:
    lines = [HEADER]
    for name, value in EXPORTED.items():
        lines.append(f"export const {name} = {value};")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()

    expected = render()
    if args.check:
        if not OUT_PATH.exists():
            print(f"MISSING: {OUT_PATH} -- run scripts/gen_envelope_thresholds.py", file=sys.stderr)
            return 1
        actual = OUT_PATH.read_text()
        if actual != expected:
            print(
                "DRIFT: frontend envelope thresholds are out of sync with "
                "mobius_contracts. Run scripts/gen_envelope_thresholds.py.",
                file=sys.stderr,
            )
            return 1
        return 0

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(expected)
    print(f"wrote {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
