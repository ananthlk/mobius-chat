"""Local sanity replay of the DEDICATED thread summarizer prompt.

NOT a unit test (makes real model calls via the Anthropic proxy). Exercises
the actual summarizer system prompt (app.responder.thread_summarizer._SUM_SYS
+ _build_user) across the 5-turn Sunshine Health thread, feeding each turn's
``long`` forward as the next turn's PRIOR brief — exactly how the orchestrator
threads chat_threads.summary_long → previous_thread_summary.

This validates the PROMPT design quickly on Claude. The authoritative check is
the post-deploy e2e against the deployed Gemini integrator (see chat history /
memory), since Gemini is the production model.

    PYTHONPATH=. .venv/bin/python tests/manual_rolling_summary_replay.py
"""
from __future__ import annotations

import json
import os

import httpx

from app.responder.thread_summarizer import _SUM_SYS, _build_user, _parse

# (user_message, the assistant answer that turn) — faithful to the transcript.
TURNS = [
    ("How does a provider enroll with Sunshine Health?",
     "Providers enroll via Sunshine Health's online Practitioner Enrollment Requests "
     "page at sunshinehealth.com/providers/enrollment. Credentialing documents and the "
     "LOAP/roster form are required."),
    ("do you have the form to download",
     "No specific downloadable form was located in the available materials."),
    ("provider enrollment form",
     "Sunshine Health uses either its standardized application or the CAQH Provider Data "
     "Collection form. Submit with NPI, TIN, and service location details."),
    ("Do you have portal access, or do you need the enrollment contact info?",
     "No verified portal access information is available."),
    ("can you share the link to the website where i will find this form",
     "The enrollment materials live on Sunshine Health's Practitioner Enrollment Requests web page."),
]
MODELS = [os.getenv("REPLAY_MODEL") or "", "claude-sonnet-4-5-20250929", "claude-3-5-sonnet-latest"]


def call(system: str, user: str) -> str:
    key = os.environ["ANTHROPIC_API_KEY"]
    base = os.getenv("ANTHROPIC_BASE_URL", "https://api.anthropic.com").rstrip("/")
    last = None
    for model in [m for m in MODELS if m]:
        try:
            r = httpx.post(f"{base}/v1/messages",
                           headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                                    "content-type": "application/json"},
                           json={"model": model, "max_tokens": 400, "system": system,
                                 "messages": [{"role": "user", "content": user}]},
                           timeout=60)
            if r.status_code == 404:
                last = f"404 {model}"; continue
            r.raise_for_status()
            return "".join(b.get("text", "") for b in r.json()["content"])
        except Exception as e:  # noqa: BLE001
            last = f"{type(e).__name__}: {e}"
    raise RuntimeError(f"all models failed: {last}")


def main() -> int:
    prev_long = None
    shorts: list[str] = []
    print("=" * 78)
    print("DEDICATED SUMMARIZER REPLAY — Sunshine Health enrollment thread")
    print("=" * 78)
    for i, (msg, answer) in enumerate(TURNS, 1):
        user = _build_user(prev_long, msg, answer, "Florida Medicaid (Sunshine Health / Centene)")
        short, long_ = _parse(call(_SUM_SYS, user))
        prev_long = long_ or prev_long
        shorts.append(short or "")
        print(f"\nTURN {i}  user: {msg!r}")
        print(f"  short ({len(short or '')} chars): {short}")
        print(f"  long  ({len(long_ or '')} chars): {long_}")

    final = (prev_long or "").lower()
    checks = {
        "[long] jurisdiction retained (florida/fl)": ("florida" in final or " fl" in final or "(fl" in final),
        "[long] enrollment URL/page retained": ("sunshinehealth" in final or "enrollment requests" in final),
        "[long] form intent retained": "form" in final,
        "[short] every label <=70 chars": all(len(s) <= 70 for s in shorts),
        "[short] no 'user is asking' leak": all("user is asking" not in s.lower() for s in shorts),
    }
    print("\n" + "=" * 78)
    for label, ok in checks.items():
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    allok = all(checks.values())
    print("=" * 78)
    print("RESULT:", "PASS" if allok else "FAIL")
    return 0 if allok else 1


if __name__ == "__main__":
    raise SystemExit(main())
