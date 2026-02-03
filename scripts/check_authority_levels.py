#!/usr/bin/env python3
"""Print distinct document_authority_level values and Sunshine-related docs from published_rag_metadata.
Run from mobius-chat: python scripts/check_authority_levels.py
Uses CHAT_RAG_DATABASE_URL (same as run_migrations / turns)."""
import os
import sys

# Run from mobius-chat so app.chat_config is available
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

def main():
    from app.chat_config import get_chat_config
    url = (get_chat_config().rag.database_url or "").strip()
    if not url:
        print("CHAT_RAG_DATABASE_URL not set. Set it in .env or mobius-config/.env")
        print("\nTo run manually with psql:")
        print("  psql -h <host> -U <user> -d <dbname> -c \"SELECT DISTINCT document_authority_level, COUNT(*) FROM published_rag_metadata GROUP BY 1;\"")
        print("  psql -h <host> -U <user> -d <dbname> -c \"SELECT document_authority_level, document_display_name, document_filename FROM published_rag_metadata WHERE document_display_name ILIKE '%sunshine%' OR document_filename ILIKE '%sunshine%' LIMIT 20;\"")
        return 1

    try:
        import psycopg2
        import psycopg2.extras
    except ImportError:
        print("psycopg2 not installed")
        return 1

    conn = psycopg2.connect(url)
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    print("=== Distinct document_authority_level values (all rows) ===\n")
    cur.execute("""
        SELECT document_authority_level, COUNT(*) AS cnt
        FROM published_rag_metadata
        GROUP BY document_authority_level
        ORDER BY document_authority_level NULLS LAST;
    """)
    for row in cur.fetchall():
        val = row["document_authority_level"]
        print(f"  {repr(val)}  ->  {row['cnt']} rows")

    print("\n=== Rows matching 'sunshine' (display_name or filename) ===\n")
    cur.execute("""
        SELECT document_authority_level, document_display_name, document_filename
        FROM published_rag_metadata
        WHERE document_display_name ILIKE %s OR document_filename ILIKE %s
        LIMIT 30;
    """, ("%sunshine%", "%sunshine%"))
    rows = cur.fetchall()
    if not rows:
        print("  (no rows found)")
    else:
        for row in rows:
            print(f"  authority_level={repr(row['document_authority_level'])}  display_name={repr(row['document_display_name'])}  filename={repr(row['document_filename'])}")

    cur.close()
    conn.close()
    print("\nDone.")
    return 0

if __name__ == "__main__":
    sys.exit(main())
