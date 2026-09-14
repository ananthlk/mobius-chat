#!/usr/bin/env python3
"""Emit the formatter's real output as a fixture the FRONTEND test can load.

    python3 scripts/emit_card_fixtures.py

This is the Python half of the cross-boundary test. The formatter runs here,
the real renderAnswerCard runs in vitest, and the fixture is the seam. Nothing
in between is re-implemented -- which was the weakness of rendering the DOM
from Python: a faithful COPY of bubble.ts is still a copy, and it cannot fail
when bubble.ts changes.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from app.responder.deterministic_format import deterministic_format, segment_draft  # noqa: E402
from app.responder.envelope_classifier import classify_envelope  # noqa: E402
from render_corpus import CORPUS  # noqa: E402

OUT = ROOT / "frontend" / "src" / "__fixtures__" / "formatter-cards.json"


def main() -> int:
    cases = []
    for question, draft, note in CORPUS:
        card = deterministic_format(draft)
        cases.append({
            "question": question,
            "note": note,
            "card": card,
            "rules": [classify_envelope(b).rule_id for b in segment_draft(draft).blocks],
        })
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(cases, indent=2) + "\n")
    print(f"wrote {OUT.relative_to(ROOT)} ({len(cases)} cards)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
