#!/usr/bin/env python3
"""
Print which optional LLM providers are configured (masked) and which roster models are enabled.

Usage (from mobius-chat/):
  python scripts/check_llm_keys.py

Does not print full API keys — only length and last 4 chars.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def _mask(name: str, value: str) -> str:
    v = (value or "").strip()
    if not v:
        return f"{name}: (not set)"
    tail = v[-4:] if len(v) >= 4 else "****"
    return f"{name}: set (len={len(v)}, …{tail})"


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    try:
        from dotenv import load_dotenv

        load_dotenv(root / ".env")
    except Exception:
        pass

    keys = [
        "ANTHROPIC_API_KEY",
        "GROQ_API_KEY",
        "TOGETHER_API_KEY",
        "OPENAI_API_KEY",
    ]
    print("Environment (mobius-chat/.env or shell):\n")
    for k in keys:
        print(" ", _mask(k, os.environ.get(k, "")))
    print()

    sys.path.insert(0, str(root))
    os.chdir(root)
    from app.services.model_registry import MODEL_ROSTER, auto_enable_from_env

    auto_enable_from_env()

    anthropic_on = [m.model_id for m in MODEL_ROSTER.values() if m.provider == "anthropic" and m.enabled]
    print("Anthropic roster after auto_enable_from_env:")
    if anthropic_on:
        for mid in sorted(anthropic_on):
            print(f"  ✓ {mid}")
    else:
        print("  (none — set ANTHROPIC_API_KEY and restart, or run this script again)")
    print()
    print("All enabled models:")
    for mid in sorted(m.model_id for m in MODEL_ROSTER.values() if m.enabled):
        spec = MODEL_ROSTER[mid]
        print(f"  {mid:<45} {spec.provider}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
