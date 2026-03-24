#!/usr/bin/env python3
"""
Reset Thompson sampling history: truncate llm_calls and llm_quality_updates,
then refresh model_performance_by_stage.

Use when you want a clean slate so each model can win or lose on merit with
the new per-round adjudication scores. After this, Thompson sampling will
use benchmark priors until fresh quality data accumulates.

Requires: CHAT_RAG_DATABASE_URL
Usage: python scripts/reset_thompson_sampling.py [--yes]
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

# Load .env
_chat_root = Path(__file__).resolve().parent.parent
_config_dir = _chat_root.parent / "mobius-config"
if _config_dir.exists() and str(_config_dir) not in sys.path:
    sys.path.insert(0, str(_config_dir))
try:
    from env_helper import load_env
    load_env(_chat_root)
except ImportError:
    from dotenv import load_dotenv
    if (_chat_root / ".env").exists():
        load_dotenv(_chat_root / ".env", override=True)
    if _config_dir.exists() and (_config_dir / ".env").exists():
        load_dotenv(_config_dir / ".env", override=False)

logger = logging.getLogger(__name__)


def _get_db_url() -> str:
    return (
        os.environ.get("CHAT_RAG_DATABASE_URL")
        or os.environ.get("RAG_DATABASE_URL")
        or os.environ.get("CHAT_DATABASE_URL")
        or ""
    ).strip()


def _connect_db(url: str):
    import urllib.parse

    try:
        from sqlalchemy.engine import make_url
        parsed = make_url(url)
        return __import__("psycopg2").connect(
            host=parsed.host or "localhost",
            port=parsed.port or 5432,
            dbname=(parsed.database or "postgres").lstrip("/"),
            user=parsed.username or "postgres",
            password=parsed.password or "",
            connect_timeout=10,
        )
    except ImportError:
        parsed = urllib.parse.urlparse(url)
        netloc = parsed.netloc
        path = (parsed.path or "/").lstrip("/") or "postgres"
        userinfo, _, hostport = netloc.rpartition("@")
        if not hostport:
            return __import__("psycopg2").connect(url)
        username, _, password = userinfo.partition(":")
        password = urllib.parse.unquote_to_bytes(password).decode("utf-8", "replace")
        host, _, port_str = hostport.rpartition(":")
        port = int(port_str) if port_str.isdigit() else 5432
        return __import__("psycopg2").connect(
            host=host or "localhost",
            port=port,
            dbname=path,
            user=urllib.parse.unquote(username) if username else "postgres",
            password=password,
            connect_timeout=10,
        )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Reset Thompson sampling history (truncate llm_calls, llm_quality_updates; refresh materialized view)"
    )
    parser.add_argument(
        "--yes", "-y",
        action="store_true",
        help="Skip confirmation prompt",
    )
    args = parser.parse_args()

    url = _get_db_url()
    if not url:
        logger.error("CHAT_RAG_DATABASE_URL not set")
        return 1

    if not args.yes:
        print("This will PERMANENTLY delete:")
        print("  - All rows in llm_quality_updates")
        print("  - All rows in llm_calls")
        print("  - Refresh model_performance_by_stage (empty)")
        print("")
        print("Thompson sampling will start fresh using benchmark priors.")
        resp = input("Type 'yes' to confirm: ").strip().lower()
        if resp != "yes":
            print("Aborted.")
            return 0

    conn = _connect_db(url)
    conn.autocommit = True
    cur = conn.cursor()
    try:
        # llm_quality_updates references llm_calls; CASCADE truncates both
        cur.execute("TRUNCATE TABLE llm_calls CASCADE")
        logger.info("Truncated llm_calls (and llm_quality_updates)")

        cur.execute("REFRESH MATERIALIZED VIEW model_performance_by_stage")
        logger.info("Refreshed model_performance_by_stage")

        print("✓ Thompson sampling reset complete. Models will compete from benchmark priors.")
    except Exception as e:
        logger.exception("Reset failed")
        return 1
    finally:
        cur.close()
        conn.close()

    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    sys.exit(main())
