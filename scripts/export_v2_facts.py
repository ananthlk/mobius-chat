"""Export v2's facts as calibration ground truth.

Tool Manifest and Deep Research both asked for this, and it does double duty:

  * Deep Research's verifier bar is SUPPORTED_AT = 0.62, set from three probes.
    Their own file calls it "a judgement wearing a number". A few hundred real
    facts with provenance turn it into a measurement — and tell us whether one
    bar works across payers or has to move per document type.
  * Tool Manifest has never had per-tool OUTCOME evidence for their ranking.
    Same stream, keyed the same way.

SHAPE, as Tool Manifest specified: per fact, keyed on correlation_id, carrying
fact / document_id / page / arm. correlation_id is the same key as
selection_event, so verdicts join decisions without a second identifier.

document_id, NOT the display name: the verifier scopes by id (144s unscoped
against 0.5s scoped), and a display name like "Sunshine Provider Manual" is not
a corpus filename — it verifies as unverifiable however true the claim is.

HONEST ABOUT THE SAMPLE. These facts are NOT labelled. They are what react
produced from real retrieved passages, so most should verify as supported — but
"should" is the hypothesis being tested, not an input to it. Rows where
document_id is empty are included and flagged: they are the population that
cannot be scoped, and their share is itself a finding.

Run: .venv/bin/python scripts/export_v2_facts.py [out.jsonl]
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv  # noqa: E402

load_dotenv(".env")
import psycopg2  # noqa: E402

OUT = sys.argv[1] if len(sys.argv) > 1 else "/tmp/v2_facts_calibration.jsonl"

url = (os.getenv("DATABASE_URL") or os.getenv("CHAT_RAG_DATABASE_URL") or "")
url = url.replace("postgresql+asyncpg://", "postgresql://").replace(
    "postgresql+psycopg2://", "postgresql://")
conn = psycopg2.connect(url, connect_timeout=10)
cur = conn.cursor()
cur.execute("""
    SELECT r.correlation_id, r.round_index, r.shape_seen, r.ts,
           f->>'fact'        AS fact,
           f->>'document'    AS document,
           f->>'document_id' AS document_id,
           f->>'page'        AS page
    FROM react_v2_rounds r, jsonb_array_elements(r.facts) f
    WHERE f->>'fact' IS NOT NULL AND f->>'fact' <> ''
    ORDER BY r.ts
""")
rows = cur.fetchall()

n_id = 0
with open(OUT, "w") as fh:
    for cid, rnd, shape, ts, fact, doc, doc_id, page in rows:
        n_id += 1 if doc_id else 0
        fh.write(json.dumps({
            "correlation_id": cid,
            "round_index": rnd,
            "arm": "v2",                 # only v2 produces facts
            "shape_seen": shape,
            "ts": ts.isoformat() if ts else None,
            "fact": fact,
            "document": doc,             # the display name react cited
            "document_id": doc_id or None,   # None => cannot be scoped
            "page": int(page) if (page or "").isdigit() else None,
            # Deliberately absent: any verdict. These are unlabelled.
        }, ensure_ascii=False) + "\n")

print(f"{len(rows)} facts -> {OUT}")
print(f"  with document_id : {n_id}  ({100 * n_id // max(len(rows), 1)}%) — scopable")
print(f"  without          : {len(rows) - n_id}  — will verify as unverifiable")
cur.execute("""
    SELECT f->>'document', count(*)
    FROM react_v2_rounds r, jsonb_array_elements(r.facts) f
    WHERE f->>'document' IS NOT NULL AND f->>'document' <> ''
    GROUP BY 1 ORDER BY 2 DESC LIMIT 8
""")
print("  documents:")
for d, n in cur.fetchall():
    print(f"     {str(d)[:58]:60} {n}")
