#!/usr/bin/env python3
"""Debug: Run 'Find the NPI for Lifestream, whose website is https://www.lsbc.net/' with full emissions."""

import os
import sys
from pathlib import Path

CHAT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(CHAT_ROOT))
_root = CHAT_ROOT.parent
for env_path in (CHAT_ROOT / ".env", _root / "mobius-config" / ".env", _root / ".env"):
    if env_path.exists():
        try:
            from dotenv import load_dotenv
            load_dotenv(env_path, override=False)
        except Exception:
            pass
        break

def emit(msg: str) -> None:
    print(f"  [emit] {msg}")

def main() -> int:
    user_message = "Find the NPI for Lifestream, whose website is https://www.lsbc.net/"
    # Simulate planner reframing (common patterns that break extraction)
    reframed_variants = [
        user_message,  # no reframe
        "Search for Lifestream NPI",
        "Find information about Lifestream's NPI",
    ]
    print("=" * 70)
    print("Testing with user_message + reframed subquestion (simulated planner output)")
    print("=" * 70)

    from app.services.tool_agent import answer_tool

    for i, reframed in enumerate(reframed_variants, 1):
        print(f"\n--- Scenario {i}: reframed_text=\"{reframed[:50]}...\"" if len(reframed) > 50 else f"\n--- Scenario {i}: reframed_text=\"{reframed}\"")
        ans, _, _, _ = answer_tool(
            reframed,
            emitter=emit,
            invoke_google_for_search_request=True,
            user_message=user_message,
        )
        ok = "NPI:" in (ans or "") or "match" in (ans or "").lower()
        print(f"   Result: {'PASS' if ok else 'FAIL'} - {ans[:200] if ans else '(empty)'}...")
    return 0

if __name__ == "__main__":
    sys.exit(main())
