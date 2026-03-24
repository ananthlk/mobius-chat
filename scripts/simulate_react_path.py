#!/usr/bin/env python3
"""Simulate which pipeline path (ReAct vs legacy) the worker would take.

Loads env the same way mstart does (mobius-chat/.env, mobius-config/.env),
then reports USE_REACT and which emissions you would see.

Run from Mobius repo root:
  PYTHONPATH=mobius-chat python mobius-chat/scripts/simulate_react_path.py
  MOBIUS_USE_REACT=0 PYTHONPATH=mobius-chat python mobius-chat/scripts/simulate_react_path.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

CHAT_ROOT = Path(__file__).resolve().parent.parent
MOBIUS_ROOT = CHAT_ROOT.parent
if str(CHAT_ROOT) not in sys.path:
    sys.path.insert(0, str(CHAT_ROOT))

# Load env in same order as mstart (worker loads mobius-chat/.env then mobius-config/.env)
for env_path in (CHAT_ROOT / ".env", MOBIUS_ROOT / "mobius-config" / ".env"):
    if env_path.exists():
        try:
            from dotenv import load_dotenv
            load_dotenv(env_path, override=False)
        except Exception:
            pass

# Now import so USE_REACT is computed with the loaded env
from app.pipeline.orchestrator import USE_REACT

def main() -> int:
    raw = os.environ.get("MOBIUS_USE_REACT", "(not set)")
    print("=" * 60)
    print("Pipeline path simulation (same env as worker under mstart)")
    print("=" * 60)
    print(f"  MOBIUS_USE_REACT env = {raw!r}")
    print(f"  USE_REACT (orchestrator) = {USE_REACT}")
    print()
    if USE_REACT:
        print("  → ReAct path. You would see:")
        print("    - Jurisdiction line (e.g. ✓ Confirmed / ? Payer not identified)")
        print("    - \"I'm breaking down your question and choosing the right source…\"")
        print("    - \"  Step 1/4: reasoning…\"")
        print("    - \"  Using run_credentialing_report…\"")
        print("    - \"◌ Running credentialing report (this may take a minute)…\"")
        print("    - No \"I'm reading your question\", no \"My plan:\", no \"Running the Medicaid NPI report for...\"")
    else:
        print("  → Legacy path. You would see:")
        print("    - \"I'm reading your question and breaking it down.\"")
        print("    - \"? Payer not identified\" / \"Mention a specific payer...\"")
        print("    - \"My plan: 1. I'll run the Medicaid NPI / Credentialing report plan...\"")
        print("    - \"Running the Medicaid NPI report for David Lawrence Center…\"")
        print("    - \"Steps: 1. Ensure revenue metrics...\"")
    print()
    print("  To force ReAct:  export MOBIUS_USE_REACT=1  (or ensure it is not set to 0 in .env)")
    print("  To force legacy: export MOBIUS_USE_REACT=0")
    print("=" * 60)
    return 0

if __name__ == "__main__":
    sys.exit(main())
