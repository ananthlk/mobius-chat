#!/usr/bin/env python3
"""Test chat pipeline and verify AnswerCard formatting in response message.

Usage:
  PYTHONPATH=mobius-chat python mobius-chat/scripts/test_card_format.py
"""
from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path

os.environ.setdefault("QUEUE_TYPE", "memory")

CHAT_ROOT = Path(__file__).resolve().parent.parent
if str(CHAT_ROOT) not in sys.path:
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

os.environ["QUEUE_TYPE"] = "memory"


def is_clean_answer_card(msg: str) -> tuple[bool, str]:
    """Check if message is valid AnswerCard with human-readable direct_answer.
    Returns (ok, reason).
    """
    if not msg or not msg.strip():
        return False, "empty"
    raw = msg.strip()
    if raw.lower().startswith("json "):
        raw = raw[5:].strip()
    if raw.startswith("```"):
        lines = raw.split("\n")
        if lines and lines[0].strip().startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        raw = "\n".join(lines).strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return False, "invalid json"
    if not isinstance(data, dict):
        return False, "not dict"
    da = data.get("direct_answer")
    if not isinstance(da, str):
        return False, "missing direct_answer"
    if da.strip().startswith("```json") or (da.strip().startswith("{") and "resolutions" in da[:200]):
        return False, "direct_answer contains raw nested JSON"
    if not isinstance(data.get("sections"), list):
        return False, "sections not array"
    mode = data.get("mode")
    if mode not in ("FACTUAL", "CANONICAL", "BLENDED"):
        return False, f"invalid mode {mode!r}"
    return True, "ok"


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--message", "-m", default="What can you do?", help="Chat message to send")
    args = parser.parse_args()

    from app.pipeline.orchestrator import run_pipeline
    from app.queue import get_queue

    correlation_id = str(uuid.uuid4())
    message = args.message
    print(f"Running pipeline: {message!r}")
    try:
        run_pipeline(correlation_id, message, None)
    except Exception as e:
        print(f"[CRASH] {e}")
        return 1

    q = get_queue()
    resp = q.get_response(correlation_id)
    if resp is None:
        from app.storage import get_response as storage_get_response
        resp = storage_get_response(correlation_id)
    if resp is None:
        print("[FAIL] No response published")
        return 1

    status = resp.get("status")
    body = resp.get("message") or resp.get("body") or ""
    print(f"status={status}")
    print(f"message length={len(body)}")
    if body:
        preview = body[:500] + ("..." if len(body) > 500 else "")
        print(f"message preview:\n{preview}\n")

    ok, reason = is_clean_answer_card(body)
    if ok:
        print(f"[OK] AnswerCard format is clean ({reason})")
        return 0
    print(f"[FAIL] AnswerCard format issue: {reason}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
