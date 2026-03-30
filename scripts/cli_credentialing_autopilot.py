#!/usr/bin/env python3
"""Run Provider Roster / Credentialing autopilot from the terminal with streamed progress.

Use this for server-side validation before UI / agentic / co-pilot work.

Track 1 — external only (no roster merge on step 3):
  cd mobius-chat && PYTHONUNBUFFERED=1 uv run python scripts/cli_credentialing_autopilot.py "David Lawrence Center" --external-only

Track 2 — same org with roster file merged (pass upload id from thread / upload API):
  PYTHONUNBUFFERED=1 uv run python scripts/cli_credentialing_autopilot.py "David Lawrence Center" --upload-id <uuid>

Requires:
  CHAT_SKILLS_PROVIDER_ROSTER_CREDENTIALING_URL (or credentials in .env)

Does not run roster *reconciliation* (upload vs BQ compare); that needs upload_id + org_id on the skill
``/roster-reconciliation-report/from-bq`` — use chat tool or HTTP after you have those.
"""
from __future__ import annotations

import argparse
import sys
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


def main() -> int:
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass
    p = argparse.ArgumentParser(description="Stream autopilot credentialing to stdout")
    p.add_argument("org_name", help='Organization name, e.g. "David Lawrence Center"')
    p.add_argument(
        "--external-only",
        action="store_true",
        help="Outside-in: no roster merge (step3 external_only=True, no roster members)",
    )
    p.add_argument("--upload-id", default="", help="Roster upload UUID to merge at step 3 (implies not external-only unless --external-only)")
    p.add_argument(
        "--output",
        "-o",
        default="",
        help="Write report markdown to this path (uses state.report_final_md when set, else final chat text)",
    )
    args = p.parse_args()

    def emit(msg: str) -> None:
        line = (msg or "").rstrip()
        if line:
            print(line, flush=True)

    roster_upload_id = (args.upload_id or "").strip() or None
    external_only = bool(args.external_only)
    include_roster = bool(roster_upload_id) and not external_only

    print("--- cli_credentialing_autopilot ---", flush=True)
    print(f"org_name={args.org_name!r}", flush=True)
    print(f"roster_upload_id={roster_upload_id!r}", flush=True)
    print(f"external_only={external_only}", flush=True)
    print(f"include_roster_members={include_roster}", flush=True)
    print("--- stream ---", flush=True)

    from app.services.roster_credentialing_orchestrator import run_orchestrator

    try:
        final_text, state = run_orchestrator(
            args.org_name,
            emit,
            roster_upload_id=roster_upload_id,
            external_only=external_only,
            include_roster_members=include_roster,
        )
    except Exception as e:
        print(f"\n--- ERROR: {e} ---", flush=True)
        raise

    print("\n--- done ---", flush=True)
    md_out = (getattr(state, "report_final_md", None) or "").strip()
    body = md_out if md_out else (final_text or "")
    print(f"report_bytes_returned={len(final_text or '')}", flush=True)
    print(f"report_final_md_bytes={len(md_out)}", flush=True)
    for s in state.steps:
        if s.status != "pending":
            print(f"  step {s.id}: {s.status} — {(s.result_summary or '')[:120]}", flush=True)
    out_path = (args.output or "").strip()
    if out_path and body:
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        Path(out_path).write_text(body, encoding="utf-8")
        print(f"wrote markdown: {out_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
