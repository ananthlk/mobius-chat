#!/usr/bin/env python3
"""Test Chat retrieval with Express Scripts question and detailed logging.

Run (from Mobius root):
  PYTHONPATH=mobius-chat uv run python mobius-chat/scripts/test_express_scripts_retrieval.py

Requires: CHAT_RAG_DATABASE_URL
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

CHAT_ROOT = Path(__file__).resolve().parent.parent
if str(CHAT_ROOT) not in sys.path:
    sys.path.insert(0, str(CHAT_ROOT))

_root = CHAT_ROOT.parent
for env_path in (CHAT_ROOT / ".env", _root / ".env"):
    if env_path.exists():
        try:
            from dotenv import load_dotenv
            load_dotenv(env_path, override=False)
        except Exception:
            pass
        break

# Detailed logging: retriever, doc_assembly, non_patient_rag
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
)
# Reduce noise from some libs
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

QUESTION = "What phone number do pharmacies call for Express Scripts help desk?"


def main() -> int:
    print("=" * 70)
    print("Express Scripts retrieval test (detailed logging)")
    print("=" * 70)
    print(f"Question: {QUESTION}")
    print()
    db_url = os.environ.get("CHAT_RAG_DATABASE_URL", "").strip()
    if not db_url:
        print("CHAT_RAG_DATABASE_URL not set. Set it in .env and retry.")
        return 1
    print(f"CHAT_RAG_DATABASE_URL: {db_url[:50]}...")
    print()

    emitted: list[str] = []

    def emitter(msg: str) -> None:
        s = (msg or "").strip()
        if s:
            emitted.append(s)
            print(f"  [emit] {s}")

    from app.services.non_patient_rag import answer_non_patient

    print("Calling answer_non_patient...")
    print()
    answer, sources, usage = answer_non_patient(
        question=QUESTION,
        k=10,
        emitter=emitter,
    )

    print()
    print("=" * 70)
    print("Emitted messages")
    print("=" * 70)
    for m in emitted:
        print(f"  {m}")

    print()
    print("=" * 70)
    print("Sources")
    print("=" * 70)
    for i, s in enumerate(sources[:10], 1):
        print(f"  [{i}] {s.get('document_name', '?')} | score={s.get('match_score')} conf={s.get('confidence')} | {str(s.get('text', ''))[:80]}...")

    print()
    print("=" * 70)
    print("Answer")
    print("=" * 70)
    print(answer[:500] + ("..." if len(answer) > 500 else ""))

    return 0


if __name__ == "__main__":
    sys.exit(main())
