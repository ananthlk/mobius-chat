#!/usr/bin/env python3
"""Run roster reconciliation only (upload vs external / BigQuery) — NOT the 11-step credentialing pipeline.

The skill path is POST ``/roster-reconciliation-report/from-bq`` (+ optional ``/stream`` for progress).
It does **not** run credentialing steps 5+ (org benchmark, services, historic billing, PML, opportunity, full report).

Requires:
  CHAT_SKILLS_PROVIDER_ROSTER_CREDENTIALING_URL
  org_name, upload_id (from roster upload), org_id (billing NPI, 10 digits)

Examples:
  cd mobius-chat && PYTHONUNBUFFERED=1 uv run python scripts/cli_roster_reconciliation.py \\
    "David Lawrence Center" --upload-id <uuid> --org-id 1234567893

  # Or set env and omit flags:
  export ROSTER_RECON_UPLOAD_ID=...
  export ROSTER_RECON_ORG_ID=...
  uv run python scripts/cli_roster_reconciliation.py "David Lawrence Center" --output ./recon_report.md
"""
from __future__ import annotations

import argparse
import os
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

    p = argparse.ArgumentParser(description="Stream roster reconciliation report (skill /from-bq only)")
    p.add_argument("org_name", help="Organization display name")
    p.add_argument("--upload-id", default="", help="Roster upload UUID (or env ROSTER_RECON_UPLOAD_ID)")
    p.add_argument("--org-id", default="", help="Billing org NPI, 10 digits (or env ROSTER_RECON_ORG_ID)")
    p.add_argument(
        "--output",
        "-o",
        default="",
        help="Write full markdown answer to this file for review",
    )
    args = p.parse_args()

    upload_id = (args.upload_id or os.environ.get("ROSTER_RECON_UPLOAD_ID") or "").strip()
    org_id = (args.org_id or os.environ.get("ROSTER_RECON_ORG_ID") or "").strip()
    org_name = (args.org_name or "").strip()

    print("--- cli_roster_reconciliation (skill /roster-reconciliation-report/from-bq) ---", flush=True)
    print(f"org_name={org_name!r}", flush=True)
    print(f"upload_id={upload_id!r}", flush=True)
    print(f"org_id={org_id!r}", flush=True)
    print("--- stream (no credentialing steps 5–11) ---", flush=True)

    def emit(msg: str) -> None:
        line = (msg or "").rstrip()
        if line:
            print(line, flush=True)

    from app.services.doc_assembly import RETRIEVAL_SIGNAL_ROSTER_COMPLETE
    from app.services.tool_agent import answer_tool

    text, sources, _usage, sig = answer_tool(
        org_name,
        emitter=emit,
        tool_hint_override="roster_reconciliation",
        reconciliation_upload_id=upload_id or None,
        reconciliation_org_id=org_id or None,
    )

    print("\n--- done ---", flush=True)
    print(f"retrieval_signal={sig}", flush=True)
    print(f"answer_chars={len(text or '')}", flush=True)

    out_path = (args.output or "").strip()
    if sig == RETRIEVAL_SIGNAL_ROSTER_COMPLETE and out_path and text:
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        Path(out_path).write_text(text, encoding="utf-8")
        print(f"wrote {out_path}", flush=True)

    if sig != RETRIEVAL_SIGNAL_ROSTER_COMPLETE or not (text or "").strip():
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
