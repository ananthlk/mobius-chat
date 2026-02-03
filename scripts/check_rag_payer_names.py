#!/usr/bin/env python3
"""Check document_payer values in RAG (published_rag_metadata) vs payer_normalization.yaml canonicals.
Run from mobius-chat: python scripts/check_rag_payer_names.py
If documents were incorrectly named (e.g. 'Sunshine' vs 'Sunshine Health'), RAG filter won't match."""
import os
import sys
from pathlib import Path

_root = Path(__file__).resolve().parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))


def load_canonicals():
    """Return set of canonical payer names from config."""
    path = _root / "config" / "payer_normalization.yaml"
    if not path.is_file():
        return set()
    try:
        import yaml
        raw = yaml.safe_load(path.read_text()) or {}
        return {(e.get("canonical") or "").strip() for e in (raw.get("payers") or []) if (e.get("canonical") or "").strip()}
    except Exception as e:
        print(f"Warning: could not load {path}: {e}")
        return set()


def main():
    from app.chat_config import get_chat_config
    url = (get_chat_config().rag.database_url or "").strip()
    if not url:
        print("CHAT_RAG_DATABASE_URL not set. Set it in .env or mobius-config/.env")
        print("\nTo list document_payer values manually (run against chat RAG DB):")
        print('  psql "<your-CHAT_RAG_DATABASE_URL>" -c "SELECT document_payer, COUNT(*) FROM published_rag_metadata GROUP BY document_payer ORDER BY 1;"')
        print("\nCanonicals in config (filter uses these; index document_payer must match exactly):")
        for c in sorted(load_canonicals()):
            print(f"  {c!r}")
        return 1

    try:
        import psycopg2
        import psycopg2.extras
    except ImportError:
        print("psycopg2 not installed")
        return 1

    canonicals = load_canonicals()
    print("=== Canonical payers (from config/payer_normalization.yaml) ===")
    print("  These are the tokens we send to RAG filter; index document_payer must match exactly.\n")
    for c in sorted(canonicals):
        print(f"  {c!r}")
    print()

    conn = psycopg2.connect(url)
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    print("=== Distinct document_payer in published_rag_metadata ===\n")
    cur.execute("""
        SELECT document_payer, COUNT(*) AS cnt
        FROM published_rag_metadata
        GROUP BY document_payer
        ORDER BY document_payer NULLS LAST;
    """)
    rows = cur.fetchall()
    if not rows:
        print("  (no rows or table empty)")
        cur.close()
        conn.close()
        return 0

    mismatches = []
    for row in rows:
        payer = row["document_payer"]
        cnt = row["cnt"]
        if payer is None or (isinstance(payer, str) and not payer.strip()):
            label = "(empty/NULL)"
        else:
            label = repr(payer)
        if canonicals and payer not in canonicals and (payer or "").strip():
            mismatches.append((payer, cnt))
        print(f"  {label}  ->  {cnt} rows")

    if mismatches:
        print("\n*** MISMATCH: document_payer values in DB that are NOT in config canonicals ***")
        print("  Chat filters by canonical (e.g. 'Sunshine Health'). If index has 'Sunshine' or 'UnitedHealthcare', filter won't match.\n")
        for payer, cnt in mismatches:
            print(f"  In index: {payer!r} ({cnt} rows)  ->  Add to aliases for a canonical, or normalize documents to canonical and re-publish.")
        print("\n  Options:")
        print("  1. Add these strings as aliases in config/payer_normalization.yaml (canonical stays same; state→filter still uses canonical).")
        print("  2. Normalize at publish time: set Document.payer to canonical before publishing, then re-publish and re-sync.")
        print("  3. Or set document_payer in index to match canonicals (e.g. 'Sunshine' -> 'Sunshine Health') and re-sync to chat.")
    else:
        print("\n  All non-empty document_payer values match a canonical. Filter should work.")

    cur.close()
    conn.close()
    print("\nDone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
