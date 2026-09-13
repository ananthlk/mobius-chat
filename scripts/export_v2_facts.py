"""Export v2's UNLABELLED facts — a score distribution, not ground truth.

🔴 THIS FILE WAS TITLED "calibration ground truth" AND ITS OWN DOCSTRING SAID
THE FACTS ARE NOT LABELLED. Tool Manifest caught it: "the title says the
opposite, and it is the one a reader sees first, in ls, in an import, in a
commit message" — a name written from intent over a value wired from
availability, which is my own diagnosis applied to my own module. The title is
what someone reaches for in three weeks when they want "the ground truth file",
and they would find unlabelled facts.

WHAT THIS STREAM CANNOT DO. It cannot calibrate a bar. Calibration needs
someone saying THIS CLAIM IS SUPPORTED BY THAT PAGE, independently of the
score. Nothing here does. Feeding scores back to choose a cut between them is
circular, and would turn Deep Research's declared 0.62 into a number with a
bigger sample and the same epistemic status.

WHAT IT CAN DO, and why it is still worth running:

  1. THE SCORE DISTRIBUTION. Bimodal => the trough is a candidate cut with an
     argument behind it rather than three probes. Unimodal => also a finding,
     and a worse one: the tool does not separate these populations and no bar
     saves it.
  2. THE UNVERIFIABLE RATE. The share with no document_id measures MY
     resolution, not the verifier.
  3. PER-TOOL OUTCOME EVIDENCE for Tool Manifest's ranking, which has never had
     any.

WHAT WOULD ACTUALLY CALIBRATE: a hand-checked subset — thirty or so facts where
a person reads the cited page and says yes or no. Tool Manifest: "that is the
only thing that turns a distribution into a calibration, it does not scale, and
it does not need to." No volume substitutes for it.

SHAPE, as Tool Manifest specified: per fact, keyed on correlation_id — the same
key as selection_event, so verdicts join decisions without a second identifier
— carrying fact / document_id / page / arm.

document_id, NOT the display name: the verifier scopes by id (144s unscoped
against ~0.5s scoped), and "Sunshine Provider Manual" is a display name, not a
corpus filename — it verifies as unverifiable however true the claim is.

Rows without an id are INCLUDED and flagged. They are the population that
cannot be scoped, and their share is finding (2).

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
