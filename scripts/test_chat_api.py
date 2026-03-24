#!/usr/bin/env python3
"""Hit the chat API as the frontend does: POST /chat, poll /chat/response/:id.
Exits 0 if we get a completed/failed response without 'NoneType'/'not iterable'. Exit 1 otherwise.
Usage: python scripts/test_chat_api.py [message]
Default message: What is AHCA?
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request
from pathlib import Path

MOBIUS_ROOT = Path(__file__).resolve().parent.parent.parent
CHAT_ROOT = MOBIUS_ROOT / "mobius-chat"
BASE = "http://localhost:8000"
POLL_INTERVAL = 2
WAIT_HEALTH_S = 60
POLL_TIMEOUT_S = 120


def wait_health() -> bool:
    for _ in range(WAIT_HEALTH_S):
        try:
            req = urllib.request.Request(f"{BASE}/health", method="GET")
            with urllib.request.urlopen(req, timeout=5) as r:
                if r.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(1)
    return False


def post_chat(message: str, thread_id: str | None = None) -> dict:
    body = {"message": message, "thread_id": thread_id or ""}
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        f"{BASE}/chat",
        data=data,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode("utf-8"))


def get_response(correlation_id: str) -> dict:
    req = urllib.request.Request(f"{BASE}/chat/response/{correlation_id}", method="GET")
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode("utf-8"))


def main() -> int:
    message = (sys.argv[1:] and sys.argv[1]) or "What is AHCA?"
    print(f"Waiting for {BASE}/health (up to {WAIT_HEALTH_S}s)...")
    if not wait_health():
        print("API not ready.")
        return 1
    print("API ready. POST /chat...")
    out = post_chat(message, None)
    cid = out.get("correlation_id")
    thread_id = out.get("thread_id")
    if not cid:
        print("No correlation_id:", out)
        return 1
    print(f"correlation_id={cid}, thread_id={thread_id}. Polling...")
    start = time.monotonic()
    while time.monotonic() - start < POLL_TIMEOUT_S:
        resp = get_response(cid)
        status = resp.get("status")
        if status in ("completed", "failed"):
            msg = resp.get("message") or ""
            print(f"status={status}")
            print(f"message={msg[:500]}{'...' if len(msg) > 500 else ''}")
            if "NoneType" in msg and "not iterable" in msg:
                print("FAIL: NoneType/not iterable error in response.")
                return 1
            print("OK: no NoneType iterable error.")
            return 0
        time.sleep(POLL_INTERVAL)
    print("Timeout waiting for completed/failed.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
