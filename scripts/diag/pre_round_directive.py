"""Can the PRE-ROUND governor probe ever finalize on the merits?

Run:  PYTHONPATH=. .venv/bin/python scripts/diag/pre_round_directive.py

WHY THIS EXISTS. Tool Manifest reported `confidence=None` on 40/40 turns and
`directive=search` on 24/24 round-1s, and concluded "a gate that can never pass
is not a threshold, it is a constant". They also said, correctly, that they were
reading a formatted log line rather than assignment sites, and asked someone to
grep the WRITERS.

Grepping the writers (AST, assignment targets and keyword arguments) finds TWO,
not zero:

  react_loop.py:5568  RoundState(proposes_complete=False,
                                 self_reported_confidence=None, ...)   <- PRE
  react_loop.py:7290  RoundState(proposes_complete=True,
                                 self_reported_confidence=decision.get(
                                     "confidence"), ...)               <- POST

So confidence IS written. The 40/40 log line is the PRE probe, where None is
not a bug: the model has not spoken yet.

The conclusion survives anyway, and this script is the evidence. The pre-round
probe is not a constant — it varies with the clock and the round counters — but
because the two MERIT fields are literals there, no combination of the varying
inputs can produce a merits-based finish. Every finalize it can emit is the
budget running out.

That matters because the pre-directive is not advisory: react_loop.py:6043 and
:6092 use it to select the agent role / composition and the reasoning depth. So
every round is prompted as though the model has not met the bar, including a
round that follows one where it proposed complete WITH a confidence — that
value is written at 7290 and never carried into the next round's pre-state.

NOT FIXED HERE. This is v1's loop; v1 stays pure for the A/B and this is not
the governor seat's file. v2's own RoundState has no confidence field at all,
deliberately — posture.py:502 records why ("self-reported confidence is the one
signal produced by the thing being judged").
"""

from collections import Counter

from app.pipeline.react.governor import ProductPromiseContract as C
from app.pipeline.react.governor import RoundState, evaluate

CONTRACTS = {
    "quick":   C(2, 1, "low",    13.0, 20.0, "quick",   "summary"),
    "copilot": C(4, 2, "medium", 31.0, 45.0, "copilot", "summary"),
    "agentic": C(6, 3, "high",   95.0, 120.0, "agentic", "full"),
}


def sweep(contract):
    reasons, merit_finishes = Counter(), []
    for rnd in range(contract.max_rounds + 1):
        for elapsed in (0.0, 1.0, 5.0, 10.0, 20.0, 30.0, 60.0, 120.0):
            for ext in range(contract.max_extension_rounds + 1):
                state = RoundState(
                    # the two literals from react_loop.py:5568
                    proposes_complete=False, self_reported_confidence=None,
                    critic_verdict=None, groundedness_passed=None,
                    elapsed_s=elapsed,
                    base_rounds_remaining=contract.max_rounds - rnd,
                    extension_rounds_available=ext,
                )
                directive, why = evaluate(contract, state)
                reasons[(directive, why)] += 1
                if directive == "finalize" and not any(
                        k in why for k in ("budget", "exhaust", "ceiling", "time")):
                    merit_finishes.append((rnd, elapsed, ext, why))
    return reasons, merit_finishes


def main() -> int:
    bad = 0
    for name, contract in CONTRACTS.items():
        reasons, merit = sweep(contract)
        total = sum(reasons.values())
        print(f"\n── {name} ({total} input combinations) "
              f"{'─' * max(0, 40 - len(name))}")
        for (directive, why), n in reasons.most_common():
            print(f"  {n:4}  {directive:9} {why}")
        if merit:
            print(f"  ✓ {len(merit)} finalize(s) on the merits")
        else:
            print("  🔴 ZERO finalizes on the merits — every early finish this "
                  "probe can reach is the budget running out")
            bad += 1
    print(f"\n{bad} of {len(CONTRACTS)} promises cannot finish on the merits "
          "from the pre-round state.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
