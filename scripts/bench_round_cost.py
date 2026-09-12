"""What does an extra react round actually cost? Measured, from llm_calls.

Ananth, 2026-09-12: "we dont have to guess we can have that compared with
existing thats the benchmark."

    rounds  turns   in_tok    latency   $/turn
      1     1,124   14,863      2.6s    0.00174
      2       908   36,117      7.3s    0.00568     <- 3.26x the cost of 1
      3       697   57,133     12.6s    0.01260
      7        77  223,133     56.0s    0.13224     <- 76x a single-round turn

    66.2% of turns take 2+ rounds.

SUPERLINEAR: every round re-carries the accumulated context, so round N pays
for rounds 1..N-1 again. Preload's large round-1 prompt is therefore NOT the
expensive thing -- 36,473 tokens in ONE call is what an average 2-round turn
already spends across two, without the 3.26x multiplier.

CAVEATS:
  * latency_ms is LLM time summed over the turn's rounds, NOT wall-clock --
    retrieval and tools are not in it. Compare rows to rows, never to a
    stopwatch.
  * stage is react_1..react_N, so depth is the round the turn REACHED.
  * the jump at depth>=5 partly reflects a different model answering later
    rounds, not only more tokens.

Run: .venv/bin/python scripts/bench_round_cost.py
"""
import os, sys; sys.path.insert(0,'.')
from dotenv import load_dotenv; load_dotenv(".env")
import psycopg2
url = (os.getenv("DATABASE_URL") or os.getenv("CHAT_RAG_DATABASE_URL") or "")
url = url.replace("postgresql+asyncpg://","postgresql://").replace("postgresql+psycopg2://","postgresql://")
c = psycopg2.connect(url, connect_timeout=10); cur = c.cursor()

print("=== TURN TOTALS by how many reasoning rounds the turn took (60d, success) ===")
cur.execute("""
with rounds as (
  select correlation_id,
         (regexp_replace(stage,'react_',''))::int as rnd,
         input_tokens, output_tokens, latency_ms, cost_usd
  from llm_calls
  where ts > now() - interval '60 days'
    and stage ~ '^react_[0-9]+$' and success),
turns as (
  select correlation_id, max(rnd) as depth, count(*) as calls,
         sum(input_tokens) in_tok, sum(output_tokens) out_tok,
         sum(latency_ms) ms, sum(cost_usd) usd
  from rounds group by 1)
select depth, count(*) turns,
       round(avg(in_tok)) , round(avg(ms)), round(avg(usd)::numeric,5),
       round(percentile_cont(0.5) within group (order by ms))
from turns where depth <= 8 group by 1 order by 1""")
rows = cur.fetchall()
print("%6s %7s %10s %9s %10s %9s" % ("depth","turns","in_tok","avg ms","$/turn","p50 ms"))
for r in rows: print("%6s %7d %10s %9s %10s %9s" % r)

by = {int(r[0]): r for r in rows}
if 1 in by and 2 in by:
    a, b = by[1], by[2]
    print("\n=== MARGINAL COST OF NEEDING A SECOND ROUND (production, measured) ===")
    print("  input tokens  %8.0f -> %8.0f   %+.0f  (%.2fx)" % (a[2], b[2], b[2]-a[2], b[2]/a[2]))
    print("  latency ms    %8.0f -> %8.0f   %+.0f  (%.2fx)" % (a[3], b[3], b[3]-a[3], b[3]/a[3]))
    print("  cost $        %8.5f -> %8.5f   %+.5f  (%.2fx)" % (a[4], b[4], b[4]-a[4], b[4]/a[4]))

print("\n=== how often does a turn need more than one round? ===")
cur.execute("""
with rounds as (select correlation_id, (regexp_replace(stage,'react_',''))::int rnd
                from llm_calls where ts > now() - interval '60 days'
                  and stage ~ '^react_[0-9]+$' and success),
turns as (select correlation_id, max(rnd) depth from rounds group by 1)
select count(*) filter (where depth=1)::float/count(*),
       count(*) filter (where depth>=2)::float/count(*),
       count(*) from turns""")
one, multi, n = cur.fetchone()
print("  1 round: %.1f%%   2+ rounds: %.1f%%   (n=%d turns)" % (one*100, multi*100, n))
