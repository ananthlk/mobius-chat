#!/usr/bin/env python3
"""Run a Medicaid NPI / credentialing report via the chat API and print emissions + result.

Prerequisites:
  1. Start Mobius: ./mstart   (from repo root; starts chat API on 8000, worker, MCP on 8006)
  2. Optional: provider-roster-credentialing API (for Step 2 find-locations and Step 3 report).
     If not running, Step 1 may still work via MCP; Step 2/3 will show errors/skip and you still see the orchestrator plan and emissions.

Usage:
  PYTHONPATH=mobius-chat python mobius-chat/scripts/run_roster_orchestrator_chat.py
  PYTHONPATH=mobius-chat python mobius-chat/scripts/run_roster_orchestrator_chat.py "Create a credentialing report for David Lawrence"

  # Use a different base URL (e.g. deployed chat)
  CHAT_API_BASE=http://localhost:8000 PYTHONPATH=mobius-chat python mobius-chat/scripts/run_roster_orchestrator_chat.py "Create a Medicaid NPI report for Aspire"
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

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

CHAT_API_BASE = (os.environ.get("CHAT_API_BASE") or "http://localhost:8000").rstrip("/")


def post_chat(message: str, thread_id: str | None = None) -> tuple[str, str]:
    """POST /chat; returns (correlation_id, thread_id)."""
    url = f"{CHAT_API_BASE}/chat"
    payload = json.dumps({"message": message, "thread_id": thread_id or None}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read().decode())
    return (data.get("correlation_id", ""), data.get("thread_id", ""))


def stream_events(correlation_id: str, timeout_s: int = 180) -> tuple[list[str], dict | None]:
    """GET /chat/stream/{id}; collect thinking lines and return (thinking_log, completed_payload)."""
    url = f"{CHAT_API_BASE}/chat/stream/{correlation_id}"
    req = urllib.request.Request(url, headers={"Accept": "text/event-stream"})
    thinking: list[str] = []
    completed: dict | None = None
    start = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            for line in resp:
                if time.monotonic() - start > timeout_s:
                    break
                line = line.decode("utf-8", errors="replace").strip()
                if line.startswith("data: "):
                    try:
                        ev = json.loads(line[6:])
                        event_type = ev.get("event")
                        data = ev.get("data") or {}
                        if event_type == "thinking":
                            chunk = data.get("line") or data.get("content") or str(data)
                            if chunk:
                                thinking.append(chunk)
                                print(f"  [thinking] {chunk}")
                        elif event_type == "completed":
                            completed = data
                            break
                    except json.JSONDecodeError:
                        pass
    except urllib.error.HTTPError as e:
        print(f"Stream error: {e.code} {e.reason}")
        if e.code == 404:
            return (thinking, None)
    except Exception as e:
        print(f"Stream error: {e}")
    return (thinking, completed)


def poll_until_done(correlation_id: str, poll_interval: float = 0.5, timeout_s: int = 120) -> dict | None:
    """Poll GET /chat/response/{id} until status is not pending/processing."""
    url = f"{CHAT_API_BASE}/chat/response/{correlation_id}"
    start = time.monotonic()
    while time.monotonic() - start < timeout_s:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())
        except urllib.error.HTTPError:
            return None
        status = data.get("status", "")
        if status in ("completed", "failed", "clarification", "refinement_ask"):
            return data
        thinking_log = data.get("thinking_log") or []
        for line in thinking_log:
            print(f"  [thinking] {line}")
        time.sleep(poll_interval)
    return None


def main() -> int:
    message = " ".join(sys.argv[1:]).strip()
    if not message:
        message = "Create a Medicaid NPI report for Aspire"

    print("=" * 72)
    print("ROSTER CREDENTIALING ORCHESTRATOR — Chat run")
    print("=" * 72)
    print(f"\nChat API: {CHAT_API_BASE}")
    print(f"Message:  {message}\n")
    print("--- Emissions (thinking stream) ---")

    try:
        cid, thread_id = post_chat(message)
    except urllib.error.URLError as e:
        print(f"Could not reach chat API at {CHAT_API_BASE}: {e}")
        print("Start Mobius with: ./mstart")
        return 1
    except Exception as e:
        print(f"POST /chat failed: {e}")
        return 1

    print(f"Correlation ID: {cid}\n")

    # Prefer SSE stream so we see emissions in real time
    thinking, completed = stream_events(cid, timeout_s=90)
    if completed is None:
        print("\n(Stream ended without completed; polling once for final response...)")
        completed = poll_until_done(cid, poll_interval=1.0, timeout_s=30)

    print("\n--- Result ---")
    if completed:
        status = completed.get("status", "?")
        thinking_log = completed.get("thinking_log") or []
        msg = completed.get("message") or ""
        print(f"Status: {status}")
        if thinking_log and not thinking:
            for line in thinking_log:
                print(f"  [thinking] {line}")
        print()
        print("Final message:")
        print("-" * 40)
        print(msg[:8000] if msg else "(empty)")
        if len(msg) > 4000:
            print("...")
        print("-" * 40)
        return 0
    else:
        print("No completed response received (timeout or error).")
        return 1


if __name__ == "__main__":
    sys.exit(main())
