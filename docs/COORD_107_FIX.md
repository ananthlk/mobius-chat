# Task #107 Fix Request — ReAct Agent

**Filed by:** Chat Master · 2026-08-16  
**Priority:** P1 — blocks #104 and #106 from being verified clean  

## Bug summary

`react_completion_critic` in `react_loop.py` extends rounds without checking wall-clock time remaining. Think-mode queries (6 base rounds, max_extension_rounds=4) routinely reach round 10–12 and hit `MOBIUS_TURN_DEADLINE_S` before the integrator can run.

**Observed in live testing:**
- 2-payer Aetna/Molina query: cut off at round 12 (first turn), consolidate forced at round 7/12 (continuation)
- Prior 3-payer query: cut off at round 10 ("Guidance mode activated (round 8, 3 rounds remaining)" confirmed in trace)
- Pattern: critic always grants extensions when coverage gaps remain, regardless of remaining wall-clock

## Root cause

In `react_loop.py`, the critic extension gate at ~line 4813/4857 (inside `if answer:` block):
- Checks `_pp_extension_rounds_used < _pp_contract.max_extension_rounds`
- Does NOT check elapsed wall-clock time before granting extension
- Result: all 4 extension rounds fire back-to-back regardless of deadline proximity

## Fix spec

Before granting each critic extension, add a wall-clock guard:

```python
import time

# At extension grant site, before incrementing _pp_extension_rounds_used:
elapsed_s = time.monotonic() - _turn_start_time  # or equivalent deadline tracking var
deadline_s = float(os.environ.get("MOBIUS_TURN_DEADLINE_S", "120"))
# Reserve enough time for integrator to run (integrator needs ~20-30s for 16k token output)
INTEGRATOR_RESERVE_S = 25
if elapsed_s + INTEGRATOR_RESERVE_S >= deadline_s:
    # Not enough time left — skip extension, proceed to synthesis
    break  # or set is_complete = True and exit loop
```

Alternatively: cap max_extension_rounds to 2 (instead of 4) as a simpler fix, reducing worst-case from 10→8 rounds. But wall-clock guard is more robust.

## Acceptance criteria

1. A think-mode query that would previously extend to 10–12 rounds completes without "This answer was cut off"
2. The integrator runs on the FIRST turn (not continuation path) and produces an Answer card
3. `rag_call_rounds` in Diagnostics tab shows ≤8 for a 2-payer comparison query
4. "consolidate — time running low" message should NOT appear for typical think-mode queries

## What unblocks

- **#104** (critic completion gate): mechanism confirmed working but needs clean first-turn completion to mark done
- **#106** (report-mode formatting): `_report_mode_instructions` on integrator_a cannot be tested when the integrator only runs on continuation path

## Note on continuation path

The continuation turn DID produce a clean answer (7/12 rounds, 211s, QA 0.8680, comparison table rendered). So the feature set is functionally working — but the first-turn cut-off is a UX regression that must be fixed before shipping think mode broadly.
