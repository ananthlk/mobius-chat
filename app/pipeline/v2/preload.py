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

# ── THE KNOB THAT IS ACTUALLY ENFORCED ──────────────────────────────────────
#
# Ananth, 2026-09-12: "it is not enforced so we need a knob for it.. per fan
# out and total not to exceed".
#
# MEASURED: rag ignores token_budget_for_retrieval. Identical results with and
# without it, in both caller modes, n=2 each:
#     chat.default  budget=6000 -> 17 chunks | budget=None -> 17 chunks
#     chat.copilot  budget=2000 ->  1 chunk  | budget=None ->  1 chunk
# So the cap has to be applied on THIS side of the wire, on what came back.
#
# PER ARM, NOT GLOBAL. A global top-K makes the fan-out arms compete and the
# lowest-ranked entity loses outright -- on the three-payer question UHC has
# exactly ONE chunk against Sunshine's ten, so any global trim removes UHC
# first and the answer reads complete with a payer silently missing. Every
# chunk carries slot_id / payer / retrieval_arms, so the arm is known and does
# not have to be guessed by re-parsing the question (which would make chat a
# second author of rag's own decomposition).
#
# Tokens are the user-facing unit (Ananth's "6k per fan out"); chars are what
# we can count without a tokenizer. 4 chars/token is the standard rough
# conversion and is named here so nobody reads the char numbers as exact.
CHARS_PER_TOKEN = 4
# DEFAULTS OFF. Ananth, 2026-09-12, after seeing the measurement: "no lets
# leave it at the higher tokens".
#
#   uncapped   prompt 145,894 chars   48.2s   all three payers, full detail
#   6k/arm     prompt  60,259 chars   51.1s   all three arms, Sunshine DEGRADED
#
# The cap bought no latency (inside run-to-run noise) and cost answer quality:
# fanout_1 went 5->3 chunks and Sunshine fell back to "does not explicitly
# outline a distinct care management philosophy". Prompt size is not the
# latency lever here -- rag is ~12s and the reasoning round costs the same at
# 60k as at 146k.
#
# 0 means NO CAP. The machinery stays, tested, for the day a question genuinely
# needs bounding -- and for the posture machine, which can set these per ROUND
# with a real budget behind the decision instead of a constant guessing.
PER_FANOUT_TOKENS = int(os.environ.get("MOBIUS_V2_PRELOAD_PER_FANOUT", "0") or 0)
TOTAL_MAX_TOKENS = int(os.environ.get("MOBIUS_V2_PRELOAD_TOTAL_MAX", "0") or 0)


def _arm_of(src: dict) -> str:
    """Which fan-out arm produced this chunk.

    slot_id first -- it is rag's own identifier for the arm. `payer` is the
    fallback because on a named-entity fan-out it IS the arm, and it keeps the
    grouping meaningful when slot_id is absent. document_name last: two
    documents can belong to one arm, so it over-splits, but over-splitting is
    safe here (each gets a smaller share) where under-splitting is not (two
    entities sharing one budget is the collision we are removing).
    """
    for key in ("slot_id", "payer"):
        v = src.get(key)
        if v not in (None, "", "unknown"):
            return f"{key}:{v}"
    return "doc:" + str(src.get("document_name") or "?")


def _render_chunk(s: dict) -> str:
    """One chunk as react will read it: provenance, then text.

    React cites what it reads, so the document and page travel WITH the
    passage. Text with no provenance is how a citation marker ends up pointing
    at nothing -- which this session has already produced once.
    """
    name = str(s.get("document_name") or "?")
    pg = s.get("page_number")
    head = f"[{name}" + (f" p{pg}]" if pg is not None else "]")
    return f"{head}\n{s.get('text')}"


def fair_share(sources: list[dict], *,
               per_arm_tokens: int = 0,
               total_tokens: int = 0) -> tuple[str, list[dict], dict]:
    """Select chunks ROUND-ROBIN across fan-out arms, under two caps.

    Returns (text, kept, report). `report` names, per arm, how many chunks it
    had and how many were kept -- because "this arm was trimmed" and "this arm
    returned nothing" are different facts and the summary must not collapse
    them into a smaller number.

    Round-robin is the whole point: every arm gets its first chunk before any
    arm gets its second. Rank-ordered selection under a global cap is what
    drops a payer.
    """
    per_arm_chars = max(0, per_arm_tokens) * CHARS_PER_TOKEN
    total_chars = max(0, total_tokens) * CHARS_PER_TOKEN

    arms: dict[str, list[dict]] = {}
    for s in sources or []:
        if isinstance(s, dict) and str(s.get("text") or "").strip():
            arms.setdefault(_arm_of(s), []).append(s)
    if not arms:
        return "", [], {}

    # Arms keep rag's own ordering within themselves -- it ranked them and this
    # module does not re-rank. Two rankers would be two authors.
    order = list(arms.keys())
    kept: list[dict] = []
    used_total = 0
    used_arm = {a: 0 for a in order}
    idx = {a: 0 for a in order}

    progress = True
    while progress:
        progress = False
        for arm in order:
            i = idx[arm]
            if i >= len(arms[arm]):
                continue
            s = arms[arm][i]
            # RENDERED size, not raw text size. The caps must bound what
            # actually enters the prompt; counting only the chunk body let the
            # provenance headers push the payload past a cap the caller was
            # told was a ceiling. Measured overshoot was small (12,050 against
            # 12,000) and small is not the point -- "not to exceed" either
            # holds or it is a suggestion.
            n = len(_render_chunk(s)) + 2      # +2 for the "\n\n" join
            if per_arm_chars and used_arm[arm] + n > per_arm_chars:
                idx[arm] = len(arms[arm])      # this arm is full
                continue
            if total_chars and used_total + n > total_chars:
                # Total reached: stop entirely rather than skipping ahead to a
                # smaller chunk, which would silently prefer short passages.
                progress = False
                break
            kept.append(s)
            used_arm[arm] += n
            used_total += n
            idx[arm] = i + 1
            progress = True
        else:
            continue
        break

    parts = [_render_chunk(s) for s in kept]
    report = {a: {"had": len(arms[a]),
                  "kept": sum(1 for k in kept if _arm_of(k) == a)}
              for a in order}
    return "\n\n".join(parts), kept, report


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


# 🔴 WHAT TO CALL A TOOL WITH IS TOOL MANIFEST'S, NOT MINE.
#
# This module used to read each skill's inputs_schema and decide for itself
# whether a tool could run on the question. Ananth: "why are you doing this and
# not tool_manifest.. they have select 2 tools + rag" — and he was right. It
# was a judgement about their offer made inside a consumer, which is the exact
# thing I had told them a day earlier I would not do.
#
# ToolOffer now carries `inputs` (what to call the tool with) and
# `preload_reason` (why not, when inputs is None). Their catalogue had
# healthcare_query's key as `question` the whole time; my schema guess had no
# way to know, and my invented {"query": ...} is what made it time out on a
# policy question. The seat that chose the tool names the call.
#
# None means DO NOT EXECUTE. It is not "call it with nothing" — 28 of 29 MCP
# signatures disagree with their live inputSchema, so an empty dict would have
# us invoke a tool with no arguments and read the failure as the tool's fault.

# 🔴 A TOOL WITH NO KNOWN WORST CASE IS NOT PRELOADABLE.
#
# Tool Manifest, 2026-09-12, after healthcare_query timed out in this set:
#     declared    49 tools  <- 46 of them have NO CEILING AT ALL
#     placeholder  8
#     observed     1        <- rag, and only since last night
#     "estimate.py computes worst case as `ceiling or p50`, so 46 offerable
#      tools are budget-checked against their TYPICAL cost, never their worst."
#
# healthcare_query declared p50=800ms against an actual 30s timeout. It was not
# an outlier; it was the one that failed while we were watching.
#
# THIS IS A SPEND DECISION, NOT A SELECTION DECISION, and that distinction is
# what makes it mine. I am not saying a tool is unhelpful — Tool Manifest ranks,
# and I do not reorder. I am saying the governor will not spend UNBOUNDED,
# UNPRICED time speculatively, before react has said anything, on the critical
# path of a turn with a latency promise. react may still call any of these
# mid-turn, where the spend follows a decision instead of preceding one.
#
# The bound is generous on purpose: this exists to exclude the 30-second
# failure mode, not to tune latency.
# 🔴 BOUND THE SPEND, DO NOT PREDICT IT.
#
# The ceiling rule refused unknown worst cases because an unbounded tool on
# the critical path is how healthcare_query spent 30s timing out before react
# spoke. That risk is real, but a DECLARATION is a promise about cost and this
# is a WALL CLOCK, which is the thing we actually need: once the sequence has
# spent this long, no further tool is started, whatever anyone declared.
#
# It bounds the sequence, not a single call -- a tool already running is not
# abandoned, because _execute_tool assigns ~15 attributes on ctx and killing
# it mid-flight would leave the turn's state half-written. So the guarantee is
# "preload starts no new work after this", which is honest and enforceable;
# "preload never exceeds this" would not be.
# ── PRICING PRELOAD AGAINST THE TIER ───────────────────────────────────────
#
# Ananth, 2026-09-13: "price the fan out width per tier".
#
# 🔴 WIDTH IS NOT A KNOB I HOLD, AND I AM NOT GOING TO BUILD ONE THAT DOES
# NOTHING. The body chat sends rag is caller_mode, token_budget_for_retrieval,
# citable_required, call_number, correlation_id. There is NO arms/width
# parameter. The fanout_0/1/2 slots are rag's OWN routing decomposition of the
# question. A `max_arms` here would be a gate with no caller -- the exact
# defect this module keeps finding elsewhere -- so what is priced is the
# DECISION TO SPEND, which I do hold: preload rag, or leave it in `suggest`
# for react to choose deliberately.
#
# THE COST CURVE IS MEASURED, not assumed. Retriever, 2026-09-13, tag_select
# alone, same query, same tag codes, post-warmup baseline, concurrency
# controlled:
#
#     width=1   4.6-5.1s          1 -> 2   +30-40%
#     width=2   5.9-6.9s          2 -> 3   +15-20%
#     width=3   6.8-7.6s
#     width=1   4.6s   -- returns to baseline the moment concurrency drops
#
# Mechanism identified and explicitly NOT the obvious one: 3 of 15 pool
# connections is nowhere near exhaustion, so it is DB-side CPU in
# tag_select's jsonb_object_keys() iteration. I would have governed against
# the wrong thing if they had stopped at "contention".
#
# THESE ARE A COMPONENT, NOT THE WHOLE CALL. My own end-to-end preload of rag
# measured 12.4s / 15.9s / 31.1s at width 3, and 13.3-27.7s after Retriever's
# embed dedup landed. So the curve gives the SHAPE (monotonic in width, ~35%
# then ~18%) and my own measurements give the LEVEL. Both are recorded so the
# next person can see which half is whose.
PRELOAD_ARM_MS: dict[int, int] = {1: 5100, 2: 6900, 3: 7600}

# What fraction of the promise preload may spend before react has said
# anything. Not a guess dressed as a constant: a turn needs at least one react
# round (measured 10-18s) plus a communicate round (measured 6-9s), so the
# share is what is left after reserving for those.
#   fast     13+/-5s  -> 18s ceiling. One round + communicate is ~16s at the
#                       low end, so preload gets almost nothing: spending 5s
#                       here would put the promise out of reach before the
#                       model speaks.
#   normal   31+/-8s  -> 39s ceiling. Observed good turns: preload 12s,
#                       round 1 ~10-14s, communicate ~6-9s.
#   thinking 95+/-25s -> depth is the point; preload is not the constraint.
PRELOAD_BUDGET_SHARE: dict[str, float] = {
    "fast": 0.40,
    "normal": 0.40,
    "thinking": 0.60,
}


def expected_preload_ms(width: int = 3) -> int:
    """Worst-case cost of a rag preload at this width, from Retriever's curve.

    Defaults to 3 because WE CANNOT KNOW THE WIDTH BEFORE THE CALL: rag
    decomposes the question itself. Pricing the typical case and being
    surprised by the worst is how a 30-second tool passed a check priced at
    800ms — the same error this module's ceiling rule exists to refuse.
    """
    if width in PRELOAD_ARM_MS:
        return PRELOAD_ARM_MS[width]
    return max(PRELOAD_ARM_MS.values())


# 🔴 THE TIER CAPS THE WIDTH. This replaces a blunt "refuse preload entirely
# on fast" that I built an hour ago because chat had no way to ask for a
# narrower retrieval. Retriever shipped `max_arms` (d7d84e0), so the lever is
# no longer 0-or-N: a cheap tier gets ONE arm rather than nothing.
#
# Their guarantees, which are what make this safe to rely on:
#   - omitted (None) is a byte-for-byte no-op for every existing caller
#   - CEILING, NOT TARGET: a one-entity question with max_arms=3 stays at one
#   - NEVER SILENT: contract.traces.fanout_arms_dropped says how many were cut
#     whenever the cap actually bit, zero otherwise
#
# That third one is the one I asked hardest for. A narrowed retrieval that does
# not say it was narrowed is a claim about the CORPUS ("this is what there is")
# when it is really a claim about MY BUDGET — the same could-not-check /
# checked-false line this pair of modules has now crossed three times.
MAX_ARMS_BY_TIER: dict[str, int | None] = {
    "fast": 1,          # the question as asked, nothing speculative beyond it
    "normal": 3,        # what we run today; a no-op when arms <= 3 anyway
    "thinking": None,   # depth is the point; omit the cap entirely
}


def max_arms_for_tier(tier: str | None) -> int | None:
    """How many concurrent rag arms this tier may buy. None = uncapped."""
    return MAX_ARMS_BY_TIER.get((tier or "normal").strip().lower(),
                                MAX_ARMS_BY_TIER["normal"])


def affordable_for_tier(tier: str | None, promise_s: float | None,
                        elapsed_s: float | None = None,
                        width: int | None = None) -> tuple[bool, str]:
    """May preload spend on THIS tier? (ok, why_not)

    UNKNOWN PROMISE MEANS YES. A missing contract is not evidence of a tight
    budget, and refusing on it would make preload silently stop working
    wherever the promise is not wired — an absence read as a verdict, which is
    the error this file keeps cataloguing.
    """
    if promise_s is None:
        return True, ""
    t = (tier or "normal").strip().lower()
    # PRICE WHAT WE WILL ACTUALLY SPEND. Before max_arms existed this priced
    # the worst case (3 arms) on every question, because chat could not ask for
    # fewer -- so a single-payer question on `fast` was charged for a fan-out
    # it would never have run. Now the cap IS the width we buy, so that is the
    # number to charge.
    if width is None:
        _cap = max_arms_for_tier(t)
        width = _cap if _cap is not None else max(PRELOAD_ARM_MS)
    share = PRELOAD_BUDGET_SHARE.get(t, PRELOAD_BUDGET_SHARE["normal"])
    budget_ms = max(0.0, (promise_s * 1000.0) - ((elapsed_s or 0.0) * 1000.0)) * share
    cost_ms = expected_preload_ms(width)
    if cost_ms > budget_ms:
        return False, (
            f"tier={t}: preload's worst case {cost_ms}ms exceeds its "
            f"{budget_ms:.0f}ms share ({share:.0%}) of the remaining "
            f"{promise_s:.0f}s promise — OFFERED to react instead, which is a "
            f"decision rather than a guess")
    return True, ""


PRELOAD_WALL_MS = int(os.environ.get("MOBIUS_V2_PRELOAD_WALL_MS", "20000")
                      or 20000)

PRELOAD_MAX_CEILING_MS = int(
    os.environ.get("MOBIUS_V2_PRELOAD_MAX_CEILING_MS", "20000") or 0)


def affordable_to_preload(ceiling_ms, *, claim_backed: bool = False,
                          args_exact: bool = False) -> tuple[bool, str]:
    """(ok, why_not) from the tool's DECLARED WORST CASE.

    UNKNOWN IS NOT CHEAP. A missing ceiling means nobody has measured the
    failure mode, and `ceiling or p50` quietly substitutes the typical cost —
    which is how a 30-second tool passed a budget check priced at 800ms. That
    remains true and p50 is still NOT used as a proxy here.

    🔴 BUT REFUSING EVERY UNKNOWN REFUSED 46 OF 49 TOOLS.

    Measured, cid 7973c25d: once Tool Manifest made payor_fact fillable, THIS
    rule became the thing blocking it — "worst case unknown (no declared
    ceiling)" — on a 360ms lookup against the certified fact store, on a turn
    that then took three rounds and 31.5s. A guard that refuses 94% of the
    catalogue is not protecting the budget, it is replacing the feature: a
    conservative default outliving the reason it was chosen.

    The narrow exception is where the two things that made healthcare_query
    dangerous are both ABSENT:

      claim_backed  the tool SCORED against this question (slot='ranked'), so
                    running it is acting on the owner's judgement rather than
                    speculating on a default.
      args_exact    Tool Manifest supplied the exact arguments, so we are not
                    inventing {"query": <question>} for a tool that takes
                    something else — the other half of the healthcare_query
                    failure, which took `question` and was sent `query`.

    Unknown cost is then bounded rather than predicted: execute() enforces a
    wall clock across the whole sequence (see PRELOAD_WALL_MS), which is the
    requirement this rule was only ever a proxy for. A ceiling is still
    better, and a declared one still wins.
    """
    if ceiling_ms in (None, ""):
        if claim_backed and args_exact:
            return True, ""
        return False, ("worst case unknown (no declared ceiling) — "
                       "not spent speculatively before react")
    try:
        ms = int(ceiling_ms)
    except (TypeError, ValueError):
        return False, f"worst case unreadable ({ceiling_ms!r})"
    if PRELOAD_MAX_CEILING_MS and ms > PRELOAD_MAX_CEILING_MS:
        return False, (f"worst case {ms}ms exceeds the {PRELOAD_MAX_CEILING_MS}ms "
                       "preload bound")
    return True, ""


def preloadable(schema: dict | None) -> tuple[bool, str]:
    """Can this tool run on the question alone? (ok, why_not)

    UNKNOWN IS NOT YES. A tool with no declared schema -- MCP tools, which is
    what appeals_get_playbook is -- cannot be checked, so it is excluded and
    said so. Guessing that it takes a question is exactly what produced
    "Checking playbook for ?".
    """
    if not schema:
        return False, "no declared inputs — cannot tell what it needs"
    props = (schema.get("properties") or {})
    required = list(schema.get("required") or [])
    key = next((k for k in QUESTION_KEYS if k in props), None)
    if key is None:
        return False, f"takes no free-text question (props: {','.join(list(props)[:4]) or 'none'})"
    # A required input we cannot supply from the question alone makes the call
    # meaningless even when a question key exists: web_scrape needs a url.
    unmet = [r for r in required if r not in QUESTION_KEYS]
    if unmet:
        return False, f"needs {','.join(unmet)} — not derivable from the question"
    return True, key


def question_input(schema: dict | None, question: str) -> dict:
    """The question, under the key THIS tool declares.

    healthcare_query declares `question`, search_corpus declares `query`. We
    sent `query` to both, so one of them received nothing it recognised.
    """
    ok, key_or_why = preloadable(schema)
    return {key_or_why: question} if ok else {}


# Preconditions THIS SEAT can check and Tool Manifest cannot.
#
# Measured live: search_uploaded_document was ranked into preload on a
# payer-policy question and returned "No uploads on this thread." — a call we
# could have known was pointless, because whether the THREAD has uploads is
# turn state, and estimate() ranks from the question and the catalogue.
#
# 🔴 THIS IS NOT A JUDGEMENT ABOUT USEFULNESS. I am not saying the tool is
# unhelpful — that is selection and it is theirs. I am saying its precondition
# is knowably FALSE right now, from state only this side holds. If the thread
# had uploads it would run. And like every other preload exclusion, the tool
# stays OFFERED to react in `suggest`.
def unmet_precondition(tool: str, state: dict | None) -> str:
    """Why this tool cannot possibly return anything on THIS turn, or ""."""
    if not state:
        return ""
    if tool in ("search_uploaded_document", "list_thread_document_uploads"):
        if state.get("thread_uploads") == 0:
            return "no uploads on this thread — nothing for it to search"
    return ""


# 🔴 ONLY TOOLS THAT MADE A CLAIM ABOUT THIS QUESTION ARE EXECUTED.
#
# Tool Manifest, 2026-09-12, explaining a 15s waste neither of us had diagnosed
# correctly — I called it a ranking defect, they found it was never ranked:
#
#     refuse                    gate       —
#     appeals_get_playbook      ranked     0.0328   <- the only tools that made
#     appeals_lookup_rules      ranked     0.0109      a claim about the question
#     fetch_document            standard   —        <- no score, because no claim
#     search_uploaded_document  standard   —
#     rag                       default    —
#
# Standard-slot tools are offered IN CASE, never matched against the question,
# exactly like rag. So preload was spending 15s on two tools that had never
# said they were relevant, on a turn with a 31±8s promise.
#
# A SCORE IS A CLAIM. Executing a tool that carries none is speculating on
# somebody's default, not acting on their judgement — and their own refusal
# logic already says "the always-offered tools are not a claim about the
# question at all".
#
# rag stays, by name, via ALWAYS_PRELOAD: Ananth ruled "no we will always do
# rag", which is a product decision and not an inference from a slot.
CLAIM_SLOTS = ("ranked",)


def plan(offer_tool_keys: list[str], *, execute_ranked: int = EXECUTE_RANKED,
         suggest_n: int = SUGGEST_N, inputs: dict | None = None,
         reasons: dict | None = None,
         ceilings: dict | None = None,
         turn_state: dict | None = None,
         slots: dict | None = None,
         tier: str | None = None,
         promise_s: float | None = None) -> PreloadPlan:
    """Rank-ordered offer -> (execute, suggest, excluded).

    `offer_tool_keys` is Offer.tools in the order estimate() returned them --
    rank order, which this module does NOT recompute. estimate() ranks; the
    governor supplies gaps and budget. Two rankers would be two authors, and
    tonight has already produced one silent disagreement between a decision and
    a re-derivation of it.
    """
    excluded: list[tuple[str, str]] = []
    ranked: list[str] = []          # runnable NOW, on the question alone
    suggestable: list[str] = []     # react may ask for these; it can supply args
    for key in offer_tool_keys:
        if key in ALWAYS_PRELOAD:
            continue                      # handled below, never counted as ranked
        if key in NEVER_PRELOAD:
            excluded.append((key, "never preloaded: gate token or has side effects"))
            continue

        # 🔴 NOT PRELOADABLE IS NOT NOT-OFFERABLE.
        #
        # A tool that cannot run on a bare question is exactly the tool REACT
        # should be told about: react can supply the payor, the url, the carc.
        # Dropping those from `suggest` too left react with an EMPTY tool list
        # on the real offer — a capability removal, which this module's own
        # header calls the failure that cost 50 turns. Caught by reading the
        # integration output, not by a test.
        #
        # So preloadability gates EXECUTION only. `suggest` is what react may
        # ask for next round, and its arguments are react's to choose.

        # Tool Manifest's own verdict on whether the QUESTION can supply this
        # tool's arguments. Not re-derived here: re-deriving it is what made me
        # a second author, and their catalogue knows key names mine guessed at.
        if inputs is not None and inputs.get(key) is None:
            suggestable.append(key)
            excluded.append((key, (reasons or {}).get(key)
                             or "no inputs offered for this tool"))
            continue
        if ceilings is not None:
            # args_exact is knowable HERE: the inputs check immediately above
            # already `continue`d every tool whose arguments the offer could
            # not supply, so anything reaching this line has them.
            ok, why = affordable_to_preload(
                ceilings.get(key),
                claim_backed=((slots or {}).get(key) in CLAIM_SLOTS),
                args_exact=(inputs is not None and inputs.get(key) is not None))
            if not ok:
                # Unpriced for SPECULATIVE spend. react calling it later is a
                # decision, not a guess, so it stays offerable.
                suggestable.append(key)
                excluded.append((key, why))
                continue
        _unmet = unmet_precondition(key, turn_state)
        if _unmet:
            suggestable.append(key)
            excluded.append((key, _unmet))
            continue
        # Only when the caller supplied slots. A standard-slot tool is still
        # OFFERED to react -- react can decide it wants a document fetch; that
        # is a decision, where preloading it is a guess on a default.
        if slots is not None and slots.get(key) not in CLAIM_SLOTS:
            suggestable.append(key)
            excluded.append((key, f"slot={slots.get(key) or 'unknown'} — offered "
                                  "unconditionally, never scored against this "
                                  "question, so it made no claim to act on"))
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
    # Everything react may ask for next: the runnable ones we did not execute,
    # then the ones only react can supply arguments for. Rank order preserved
    # within each group — estimate() ranked them and this module does not.
    _rest = ranked[execute_ranked:] + suggestable
    suggest = _rest[:suggest_n]
    for key in _rest[suggest_n:]:
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

def execute(pl: PreloadPlan, runner, question: str,
            inputs: dict | None = None,
            max_arms: int | None = None) -> list[dict]:
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
    import time as _pl_time
    _t_start = _pl_time.monotonic()
    for tool in pl.execute:
        _spent_ms = (_pl_time.monotonic() - _t_start) * 1000.0
        if PRELOAD_WALL_MS and _spent_ms > PRELOAD_WALL_MS and out:
            # NOT SILENT, AND NOT MISSING. A tool absent from this list reads
            # to react as never-attempted; this says it was not started and
            # why, so the trace can show a budget decision rather than a gap.
            out.append({
                "tool": tool, "ok": False, "summary": "",
                "payload": "", "sources": [], "asked": "",
                "not_started": (f"preload wall clock: {_spent_ms:.0f}ms of "
                                f"{PRELOAD_WALL_MS}ms already spent — "
                                f"offered to react instead"),
            })
            continue
        try:
            # EXACTLY what Tool Manifest said to call it with. Falls back to
            # {"query": question} only when no inputs were supplied at all,
            # which is the pre-existing behaviour for callers that do not pass
            # them -- and is the invention that caused the bug, so it is the
            # fallback and never the default.
            _offered = (inputs or {}).get(tool) if inputs else None
            _inputs = dict(_offered) if _offered else {"query": question}
            # Only rag reads this; passing it to every tool would be a
            # parameter that means nothing to most of them.
            if tool == "rag" and PRELOAD_TOKEN_BUDGET > 0:
                _inputs["token_budget_for_retrieval"] = PRELOAD_TOKEN_BUDGET
            # THE TIER'S CAP, on the one tool that fans out. Omitted entirely
            # when None so `thinking` is byte-for-byte today's call -- their
            # no-op guarantee only holds if we actually omit it.
            if tool == "rag" and max_arms is not None:
                _inputs["max_arms"] = int(max_arms)
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
