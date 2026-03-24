#!/usr/bin/env python3
"""Simulate exactly what mstart does: load env, set MOBIUS_USE_REACT=1, run pipeline.
On error, print full traceback to find the exact line causing 'NoneType is not iterable'.

Run from repo root:
  .venv/bin/python mobius-chat/scripts/simulate_mstart_and_capture_error.py
"""
from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path

MOBIUS_ROOT = Path(__file__).resolve().parent.parent.parent
CHAT_ROOT = MOBIUS_ROOT / "mobius-chat"
if str(CHAT_ROOT) not in sys.path:
    sys.path.insert(0, str(CHAT_ROOT))

# 1) Same env load order as mstart: mobius-chat/.env then mobius-config/.env
for env_path in (CHAT_ROOT / ".env", MOBIUS_ROOT / "mobius-config" / ".env"):
    if env_path.exists():
        try:
            from dotenv import load_dotenv
            load_dotenv(env_path, override=False)
        except Exception:
            pass

# 2) Same as mstart/worker: force ReAct when not explicitly 0
if (os.environ.get("MOBIUS_USE_REACT") or "").strip().lower() not in ("0", "false", "no"):
    os.environ["MOBIUS_USE_REACT"] = "1"

os.environ.setdefault("QUEUE_TYPE", "memory")

def main() -> int:
    correlation_id = "sim-mstart-1"
    message = "Create a credentialing report for David Lawrence Center"
    thread_id = None

    print("Simulating mstart flow: run_pipeline with ReAct...")
    try:
        from app.pipeline.orchestrator import run_pipeline
        run_pipeline(correlation_id, message, thread_id)
        print("OK: pipeline completed.")
        return 0
    except TypeError as e:
        if "not iterable" in str(e) or "NoneType" in str(e):
            print("CAUGHT: argument of type 'NoneType' is not iterable")
            print()
            traceback.print_exc()
            return 1
        raise
    except Exception as e:
        print(f"Error: {e}")
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
