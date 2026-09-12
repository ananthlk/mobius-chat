"""Preload: run the tools BEFORE react speaks, so round 1 judges instead of guessing.

Ananth, 2026-09-12: *"tool manifest select the top x tools, preload the top 2 +
rag in the prompt... load the tool outputs + leave a generalized list of the
next 3 tool choices for react to suggest."*

WHY. Measured over 1000 turns: round 1 opens ZERO gaps and closes ZERO gaps, in
382 of 382 rounds, while calling a tool 87% of the time. It is a blind first
shot whose only product is evidence for round 2 to reason about. An LLM call
and 10-139s of wall clock spent deciding which search to run.

THE CONTRACT, three parts, and the third is the one that keeps this honest:

  EXECUTE   the top-N ranked tools plus rag, concurrently
  RENDER    their outputs as evidence react judges (frame §10)
  SUGGEST   the next few ranked tools react may ASK FOR (frame §9)

WITHOUT THE THIRD PART THIS IS A CAPABILITY REMOVAL. Today react may call
anything in the manifest; under preload it gets what we chose. The suggestion
list plus "name one in your gap report" is the escape hatch, and this session
has already shipped one capability removal that cost 50 turns -- the governor's
unilateral stop, 0 additions against 50 subtractions.

RAG IS ALWAYS EXECUTED, regardless of rank. Ananth: "no we will always do rag".
It is `tier=default` in the offer -- offered unconditionally, never ranked --
and it is the tool that answers payer-policy questions. On the three-payer
question it ranks 12th of 13 while being the only tool that finds the answer.

PURE. Offer in, plan out. Execution lives in the caller, so this module can be
tested without a network.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

# How many RANKED tools to execute alongside rag. Two, not three: each costs
# tokens in the prompt whether or not it returned anything, and the ranked tools
# are cheap-but-not-free. Deliberately small until the measurement says
# otherwise -- 57 tools at 14,271 tokens is what this product already learned.
EXECUTE_RANKED = 2

# How many further tools to NAME for react. Three is enough to offer a real
# alternative without becoming a manifest again.
SUGGEST_N = 3

# Never preload these, whatever they rank. `refuse` is a gate token, not a
# retrieval tool; the write/ingest skills have side effects and must never run
# speculatively on a question nobody asked them to act on.
NEVER_PRELOAD = frozenset({
    "refuse", "ingest_url", "document_upload_skill", "transform_previous_answer",
})

ALWAYS_PRELOAD = ("rag",)

# How many TOKENS of retrieval preload may ask rag for.
#
# Ananth, 2026-09-12: "it is really a problem now with 146K prompt largely
# because of RAG.. we can limit the K on RAG and get lower chunks".
#
# Preload inherits compute_token_budget_for_retrieval(ctx), which sizes
# retrieval to the entire context window -- correct for a round react ASKED
# for, wrong for a round nobody has asked for yet. Measured: 15 chunks,
# 141,074 characters, a 145,894-character round-1 prompt and 45.4s against a
# 31±8s promise, with rag's own retrieval the overwhelming majority of it.
#
# MEASURED, three-payer question, real rag, one run per cell:
#
#   budget   chunks  payload   rounds  elapsed  payers answered
#   2,000       1     18,592     2      75.9s   incomplete
#   6,000       3     24,081     2        --    Molina only
#  16,000      12     54,031     1      33.6s   Molina + UHC, no Sunshine
#   0 (full)   15    141,074     1      48.2s   all three
#
# CUTTING K CUTS PAYERS, not detail. At 16,000 the answer reads complete and
# confident with Sunshine Health simply absent -- the exact failure this whole
# session has been chasing, reintroduced as a performance optimisation.
#
# And it does not even save the tokens. At 6,000 react saw thin evidence and
# searched again itself, and ITS call inherits the full context-window budget:
# round 1 carried 24,081 chars, round 2 carried 143,646. The cost is displaced
# into an extra round, which is why 2,000 produced the SLOWEST turn in the set.
#
# So the default is 0: inherit the computed budget, no cap, no behaviour
# change. The knob exists to keep measuring -- a cap that is right for a
# single-entity question ("what is the timely filing limit") is wrong for a
# three-entity one, and the number that actually wants tuning is per-QUESTION,
# not per-deployment. Setting this globally trades a latency win on easy
# questions for silent omissions on hard ones.
PRELOAD_TOKEN_BUDGET = int(os.environ.get("MOBIUS_V2_PRELOAD_TOKENS", "0")
                           or 0)


@dataclass(frozen=True)
class PreloadPlan:
    """What to run, and what to offer react instead."""
    execute: tuple[str, ...] = ()
    suggest: tuple[str, ...] = ()
    # Why each excluded tool was excluded. Recorded, not dropped: a preload
    # that ran the wrong tools and a preload that ran nothing look identical in
    # an answer, and "we never asked the right tool" must be a visible verdict.
    excluded: tuple[tuple[str, str], ...] = field(default_factory=tuple)

    @property
    def is_empty(self) -> bool:
        return not self.execute


def plan(offer_tool_keys: list[str], *, execute_ranked: int = EXECUTE_RANKED,
         suggest_n: int = SUGGEST_N) -> PreloadPlan:
    """Rank-ordered offer -> (execute, suggest, excluded).

    `offer_tool_keys` is Offer.tools in the order estimate() returned them --
    rank order, which this module does NOT recompute. estimate() ranks; the
    governor supplies gaps and budget. Two rankers would be two authors, and
    tonight has already produced one silent disagreement between a decision and
    a re-derivation of it.
    """
    excluded: list[tuple[str, str]] = []
    ranked: list[str] = []
    for key in offer_tool_keys:
        if key in ALWAYS_PRELOAD:
            continue                      # handled below, never counted as ranked
        if key in NEVER_PRELOAD:
            excluded.append((key, "never preloaded: gate token or has side effects"))
            continue
        ranked.append(key)

    # ALWAYS means "whatever it ranks", NOT "whether or not it was offered".
    #
    # estimate() withholds rag when the time budget cannot carry it -- measured:
    # at budget_ms=18000 the offer comes back without rag and says so
    # ("worst case 20s against a 18s envelope"). That refusal is BINDING. Adding
    # rag back here would execute a tool the selector declined to offer, which
    # is the governor overruling Tool Manifest's own budget arithmetic with
    # nothing but a constant.
    #
    # Caught by a test asserting an empty offer plans nothing; it planned rag.
    offered = set(offer_tool_keys)
    always = [k for k in ALWAYS_PRELOAD if k in offered]
    execute = always + ranked[:execute_ranked]
    suggest = ranked[execute_ranked:execute_ranked + suggest_n]
    for key in ranked[execute_ranked + suggest_n:]:
        excluded.append((key, "ranked below the suggestion window"))

    return PreloadPlan(tuple(execute), tuple(suggest), tuple(excluded))


# ── EXECUTION ───────────────────────────────────────────────────────────────
#
# 🔴 SEQUENTIAL, DELIBERATELY. The obvious design is a ThreadPoolExecutor over
# the planned tools, and it is unsafe here: _execute_tool mutates fifteen ctx
# attributes and several are ASSIGNMENTS, not appends --
#
#     ctx.sources = ...        ctx.plan = ...       ctx.answer_set = ...
#     ctx.final_message = ...  ctx.react_bypass_integrate = ...
#
# Two tools in flight on one ctx clobber each other's sources, and a
# react_bypass_integrate=True from either short-circuits the whole turn. This
# is the same shared-mutable-state hazard Retriever flagged on their own
# AsyncSession gather, one repo over.
#
# AND THE CONCURRENCY WAS WORTH ~1 SECOND. Measured/declared: rag ~10.4s with
# fan-out, appeals_get_playbook 0.8s, healthcare_query 0.4s. Sequential ~11.6s
# against a concurrent ~10.4s. The fan-out already made rag's internal work
# concurrent -- which is where the real parallelism lives. Trading a race for
# 10% is the wrong trade, and this session has spent its night removing a
# mechanism that made exactly that kind of exchange invisible.
#
# If the tool mix ever changes so that two EXPENSIVE tools preload together,
# revisit -- with isolated contexts, not with a shared one.

def execute(pl: PreloadPlan, runner, question: str) -> list[dict]:
    """Run the planned tools in order. `runner(tool, inputs) -> dict` is
    injected so this stays testable without a network or a PipelineContext.

    Returns [{tool, ok, summary, payload, sources, asked}] for EVERY planned
    tool, including the ones that returned nothing and the ones that raised. A
    tool missing from this list would read to react as never-attempted.

    🔴 THE PAYLOAD MUST SURVIVE THIS FUNCTION. It rebuilt its own dict from
    three fields and dropped everything else the runner returned -- so a
    141,074-character retrieval arrived here and left as a 170-character
    summary. Round 1 then received document names and page numbers, wrote a
    confident answer from its own priors, and attached citation markers
    [1,2,3,4,5] to evidence that was never in the prompt.

    A producer with no consumer, inside the module whose job is to carry
    evidence to react, created in the same session that catalogued eleven
    others. The shape is always the same: a dict rebuilt field-by-field
    silently discards whatever the other side just started sending.
    """
    out: list[dict] = []
    for tool in pl.execute:
        try:
            _inputs = {"query": question}
            # Only rag reads this; passing it to every tool would be a
            # parameter that means nothing to most of them.
            if tool == "rag" and PRELOAD_TOKEN_BUDGET > 0:
                _inputs["token_budget_for_retrieval"] = PRELOAD_TOKEN_BUDGET
            res = runner(tool, _inputs) or {}
            ok = bool(res.get("ok", True)) and not res.get("error")
            out.append({
                "tool": tool, "ok": ok,
                # 400, not 200: the summary is now four structured parts
                # (docs, pages, and the terms nothing returned) and 200 cut it
                # mid-document-name -- silently, in the middle of the line
                # react reads to decide whether its ask was covered.
                "summary": str(res.get("summary") or "")[:400],
                # The evidence itself, uncapped. The caller seeds it as a
                # virtual tool result; capping it here would be a second,
                # invisible retrieval budget fighting the one rag already
                # applied.
                "payload": res.get("payload") or "",
                "sources": res.get("sources") or [],
                "asked": res.get("asked") or question,
            })
        except Exception as e:
            # A preload tool that raises must not take the turn with it: the
            # round still has the other tools' evidence, and "it errored" is a
            # fact react can use.
            out.append({"tool": tool, "ok": False,
                        "summary": "errored: %s" % str(e)[:80],
                        "payload": "", "sources": [], "asked": question})
    return out
