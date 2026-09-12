"""Where a statement's WORDING comes from — the prompt DB first, code last.

Ananth, 2026-09-12: *"make sure you are not building lines like old react.. all
prompts will eventually move into our prompt db.. so we can maintain.. if you
need to get modular please do."*

THE SPLIT, and it is the whole point of this module:

    statements.py   decides IF a statement fires   (pure: selectors, state)
    statements.py   decides WITH WHAT              (params from the ledger)
    THIS module     decides HOW IT IS WORDED       (prompt DB, then fallback)

Wording is the thing that needs iterating without a deploy, and it is the thing
a prompt seat owns. Selection is the thing that needs tests and a type checker.
They were one lambda an hour ago, and that is exactly how react_loop ended up
with 6,900 lines of prose nobody can edit without shipping Python.

FAIL-SOFT, IN ONE DIRECTION ONLY. A missing or broken block falls back to the
in-code template. The fallback is NOT the source of truth -- it is the floor,
and a statement whose DB block is missing still fires with defensible wording
rather than vanishing. A silent vanish would be the producer-with-no-consumer
defect arriving through the prompt store.

WHICH SOURCE WAS USED IS RECORDED. A turn whose wording came from the fallback
and a turn whose wording came from the DB are different experiments, and a
comparison that cannot tell them apart is measuring two things at once.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# module_key -> template. `{placeholders}` are filled from the statement's own
# params. Keep these SHORT: a statement the model skims is a statement that
# changed nothing, and five of these land per round.
FALLBACK: dict[str, str] = {
    "governor.saf_final_round":
        "This is the final round. Answer from what you have; do not request "
        "another tool call.",
    "governor.saf_ceiling":
        "The extension ceiling is reached. This is the last round regardless "
        "of what is still open.",
    "governor.ctl_budget_spent":
        "The time budget is spent. Answer from what you have and say plainly "
        "what is still open.",
    "governor.ctl_overrun_authorised":
        "You are past the time target but converging. One more round is "
        "authorised if it closes {gap!r} -- not for polish.",
    "governor.frm_parts_are_a_report":
        "Name every part this question asks for in gaps_open. That list is a "
        "REPORT of what was asked -- not a plan to do them one at a time.",
    "governor.frm_ask_as_asked":
        "Ask the question as asked: one query naming every part. rag "
        "decomposes across named entities better than asking about them one "
        "at a time.",
    "governor.frm_verify":
        "You have rounds to spend. Verify rather than accept the first "
        "plausible match.",
    "governor.evd_review_not_reask":
        "Review, do not re-ask. Compare what you now have against what was "
        "asked, and name EACH part still missing or thin as its own gap in "
        "gaps_open. A part you have already covered is not a gap.",
    "governor.evd_nothing_kept":
        "Nothing was kept from the last call. Either the query missed, or "
        "this corpus does not hold it -- say which you think it is.",
    "governor.ctl_satisfied":
        "Before is_complete=true: are you satisfied with the level of answer "
        "and evidence you have? Not 'is the answer grounded' -- 'did I do "
        "enough to get it'. A part left unanswered because you never looked "
        "is not complete.",
    "governor.ctl_dissent":
        "You marked this complete. My ledger shows {gap!r} was never searched "
        "this turn -- no query named it. Confirm complete, or spend one round "
        "on it. Your call.",
    "governor.evd_work_this_gap":
        "Work {gap!r} this round. The other open parts stay open and are not "
        "for this round.",
    "governor.str_searched_once":
        "{gap!r} was searched once and is still open. Previous query: "
        "{prior_queries}. Ask for the part it did not return.",
    "governor.str_stuck":
        "{gap!r}: several distinct levers, nothing returned. Previous "
        "queries: {prior_queries}. A reworded query returns the same evidence "
        "-- change the approach or say it cannot be closed.",
    "governor.str_closing":
        "{gap!r} is closing. Stay on it and ask for the part still missing.",
    "governor.str_falling":
        "Closure on {gap!r} fell -- the last attempt moved away from it. "
        "Return to what was working.",
    "governor.str_unreachable":
        "{gap!r} looks unreachable in this corpus. Before declaring it, try "
        "one materially different source class.",
    "governor.str_widening":
        "The open parts are growing, not shrinking. Stop widening and close "
        "one.",
    "governor.str_stagnant":
        "The last two rag calls converged on the same internal strategy and "
        "outcome. Another rag call with a similar query will not surface new "
        "information.",
    "governor.evd_do_not_repeat":
        "Do not repeat the query you just ran. It returned what it returned; "
        "this round is for what it did not.",
    "governor.frm_say_why_missing":
        "For each part you could not answer, say WHICH of these it is: "
        "(a) searched, the corpus is thin; (b) searched, nothing relevant "
        "came back; (c) not searched -- the turn ran out of budget. Do NOT "
        "render all three as \"not available in the provided documents\": "
        "they are different facts and carry opposite advice.",
}


def resolve(block_key: str, params: dict) -> tuple[str, str]:
    """(text, source). source is "db" or "fallback" and is RECORDED.

    Never raises. A wording problem must degrade the prompt, never break the
    turn -- the same posture resolve_composition_sync already takes.
    """
    try:
        from app.services.prompt_manager import resolve_composition_sync

        rc = resolve_composition_sync(block_key, conditions={},
                                      template_vars=params)
        # `.system_prompt` -- NOT `.text` and NOT `.rendered`. Guessed both
        # before and was wrong both times; this is the attribute that exists.
        text = (getattr(rc, "system_prompt", "") or "").strip() if rc else ""
        if text:
            return text, "db"
    except Exception as e:                       # pragma: no cover
        logger.debug("[v2.text] %s fell back: %s", block_key, e)

    tpl = FALLBACK.get(block_key)
    if not tpl:
        # A statement with neither a DB block nor a fallback is a registry bug,
        # and returning "" would delete it silently from the prompt.
        return f"[missing prompt block: {block_key}]", "missing"
    try:
        return tpl.format(**params), "fallback"
    except Exception:
        return tpl, "fallback"
