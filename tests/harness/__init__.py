# Differential refactor gate — P1.1 (chat-refactor-program.md).
#
# What this is: a set of scenario-based invariant tests that must hold
# on the CURRENT checked-out code.  Run them before merging any phase
# change; if they pass on the feature branch as they did on main, the
# invariants held.
#
# What this is NOT: a record/replay of production LLM traffic.  We
# cannot recover per-call model responses from chat_turns (the DB stores
# thinking_log events, not raw model output), so both sides of a
# PRE/POST comparison get the same scripted responses — the DB seat
# confirmed this is the right fidelity claim.
#
# Assertable today (I3 and I5 deferred until deterministic replay lands):
#   I1  exactly one turn_completed envelope per turn
#   I2  the bypass set (integrator skipped) is unchanged
#   I4  rounds_used and max_rounds match the governor's mode table
#   I7  log-and-continue handler count may fall, never rise  (static)
