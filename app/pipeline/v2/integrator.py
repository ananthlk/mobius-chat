"""THE V2 INTEGRATOR: deterministic assembly + critic (LLM) + next steps (LLM).

Ananth, 2026-09-12: "lets also get a new integrator. which is really the v2 of
deterministic enricher, + critic (llm) + next steps (llm) for now".

THREE PARTS, AND THE FIRST ONE NEEDS NO MODEL:

  ASSEMBLE   deterministic. Coverage per asked-for part, provenance per claim,
             open gaps with their reason, budget. All of it computed from data
             we already hold. The old enricher paid a model call to produce
             prose about an answer it could not check; this checks and does not
             ask.
  CRITIQUE   one cheap LLM call: is each claim supported by the facts given?
  NEXT STEPS one cheap LLM call: what would close what is still open?

WHY OUT HERE AND NOT AS A REACT ROUND. An in-loop round re-carries the
accumulated context -- measured in production, round 2 costs 3.26x round 1 and
2.82x its latency, because it pays for round 1 again. These two calls need the
answer, the facts with provenance, and the open gaps: ~2-5k tokens against
~36k. And they read the same inputs while depending on neither, so out here
they run CONCURRENTLY; in-loop they would be two serial rounds, or one round
doing both jobs -- which is the self-review blocks.py exists to refuse.

THE CONCURRENCY IS SAFE HERE AND WAS NOT IN preload.execute. That one is
sequential because _execute_tool ASSIGNS fifteen ctx attributes and two tools
in flight clobber each other. These two calls take strings and return strings:
they touch no ctx, so there is nothing to race. The distinction is the whole
reason one is parallel and the other is not.

DEGRADES, NEVER FABRICATES. If a model call fails the section is marked
UNOBSERVABLE and says so. An integrator that invents a critique when the critic
did not answer is worse than one that returns nothing, because the invention
reads as a check that happened.

Prompts resolve through statement_text (prompt DB first, in-code fallback
second) per the standing rule that all prompts end up in the DB, and the SOURCE
is recorded so a wording change is traceable to where it came from.
"""

from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field

logger = logging.getLogger(__name__)

CRITIC_BLOCK = "v2.integrator.critic"
NEXT_STEPS_BLOCK = "v2.integrator.next_steps"

# 🔴 THESE BUDGETS INCLUDE THINKING TOKENS, which is why they look large for
# such small outputs.
#
# Measured against gemini-2.5-flash with the real critic prompt:
#     max_tokens=900   -> 166 chars, cut mid-word
#     max_tokens=3000  -> 936 chars, cut mid-word
#     max_tokens=8000  -> complete, parses
#
# The visible reply is ~900 characters. Sizing the budget to THAT truncates,
# because the model spends most of it thinking before emitting anything. The
# first live integration run reported "critique was not JSON" and the reply was
# in fact perfectly good JSON with its tail missing -- a wrong diagnosis that
# sent me looking at the prompt.
#
# Cost is unaffected by the ceiling: we pay for tokens produced, not offered.
CRITIC_MAX_TOKENS = 8000
NEXT_STEPS_MAX_TOKENS = 3000


@dataclass(frozen=True)
class PartVerdict:
    part: str = ""
    # supported | partial | unsupported | not_attempted | unobservable
    status: str = "unobservable"
    why: str = ""
    evidence: tuple[str, ...] = ()


@dataclass(frozen=True)
class Integration:
    # ── deterministic ────────────────────────────────────────────────────
    coverage: tuple[PartVerdict, ...] = ()   # per asked-for part, mechanical
    citations: tuple[str, ...] = ()          # doc p12, from grounded facts
    unsupported_claims: int = 0
    open_gaps: tuple[str, ...] = ()
    # ── from the models ──────────────────────────────────────────────────
    critique: tuple[PartVerdict, ...] = ()
    critique_summary: str = ""
    next_steps: tuple[str, ...] = ()
    # ── what actually happened ───────────────────────────────────────────
    ran: dict = field(default_factory=dict)      # section -> ok | skipped | failed
    prompt_sources: dict = field(default_factory=dict)
    problems: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        d = asdict(self)
        d["coverage"] = [asdict(c) for c in self.coverage]
        d["critique"] = [asdict(c) for c in self.critique]
        return d


# ── 1. DETERMINISTIC: no model, and therefore checkable ─────────────────────

def assemble(*, question: str, answer: str, facts=(), open_gaps=(),
             all_parts=()) -> Integration:
    """Everything computable without asking anyone.

    COVERAGE IS THE THREE-PAYER CHECK, mechanised: for each part the question
    names, is there a grounded fact mentioning it? 17 passages covering two
    payers and 17 covering three produce identical answers to a reader and
    different answers here.
    """
    # PARTS COME FROM EVERY GAP THE TURN NAMED, not just the ones still open.
    #
    # Measured live: react closed all three payer gaps and reported none open,
    # so parts fell back to the whole question, coverage collapsed to ONE part,
    # and a single Molina fact marked it "supported" -- the three-payer check
    # silently stopped checking at the exact moment the answer claimed to be
    # complete. Closed gaps are the decomposition; open gaps are only its
    # unfinished tail.
    parts = _parts_of(question, all_parts or open_gaps)
    grounded = [f for f in facts or () if getattr(f, "grounded", False)]
    ungrounded = sum(1 for f in facts or () if not getattr(f, "grounded", False))

    # THE DISTINGUISHING TOKENS, not the whole string. Matching the full gap
    # text ("Molina care management philosophy") against a fact ("Molina uses
    # an Integrated Care Management program") finds nothing, and every part
    # came back not_attempted while its evidence sat right there.
    #
    # What separates the parts of a multi-part question is exactly the tokens
    # they do NOT share: "care", "management" and "philosophy" are in all
    # three; "molina", "sunshine", "united" are the parts. Computed from the
    # part list itself, so nothing is hardcoded about payers.
    keys = _distinguishing_tokens(parts)

    coverage = []
    for part in parts:
        toks = keys.get(part) or ()
        hits = [f for f in grounded
                if any(t in (f.fact or "").lower()
                       or t in (f.document or "").lower() for t in toks)]
        if hits:
            coverage.append(PartVerdict(
                part, "supported",
                f"{len(hits)} grounded fact(s)",
                tuple(_cite(f) for f in hits[:3])))
        elif part in (open_gaps or ()):
            # Named as open by react: unfinished, and we know why.
            coverage.append(PartVerdict(part, "not_attempted",
                                        "still an open gap"))
        else:
            # THE DANGEROUS CASE: the answer talks about it, nothing grounds
            # it. Not "unsupported" — that would claim we checked the corpus.
            coverage.append(PartVerdict(
                part, "unobservable",
                "no grounded fact mentions this part — cannot tell whether it "
                "was answered from evidence or from memory"))

    return Integration(
        coverage=tuple(coverage),
        citations=tuple(dict.fromkeys(_cite(f) for f in grounded)),
        unsupported_claims=ungrounded,
        open_gaps=tuple(open_gaps or ()),
        ran={"assemble": "ok"},
    )


_COMMON = frozenset("""
the a an of for and or in on to is are what when where which who why how
care management philosophy policy program plan process guidelines rules
information details about their its this that these those
""".split())


def _tokens(text: str) -> set[str]:
    return {"".join(c for c in w if c.isalnum()).lower()
            for w in (text or "").split()} - {""}


def _distinguishing_tokens(parts) -> dict:
    """Per part, the tokens that separate it from the OTHER parts.

    Shared tokens cannot distinguish: on a three-payer question "care
    management philosophy" appears in every part, so matching on it marks
    every part supported by any fact. The distinguishing set is what remains.

    A single-part question has nothing to contrast against, so it falls back
    to its own uncommon tokens -- there is no other part to confuse it with.
    """
    parts = tuple(parts or ())
    tok = {p: _tokens(p) - _COMMON for p in parts}
    out = {}
    for p in parts:
        others = set().union(*[tok[q] for q in parts if q != p]) if len(parts) > 1 else set()
        distinct = tok[p] - others
        # Fall back to the part's own tokens when every token is shared --
        # better to over-match than to mark a part uncheckable because the
        # question phrased two parts identically.
        out[p] = tuple(t for t in (distinct or tok[p]) if len(t) >= 4)
    return out


def _call(runner, system, user, max_tokens, stage):
    """Pass `stage` when the runner accepts it, and stay compatible with the
    simple (system, user, max_tokens) test runners -- a signature requirement
    that breaks every existing caller is a worse contract than an optional
    keyword."""
    try:
        return runner(system, user, max_tokens=max_tokens, stage=stage)
    except TypeError:
        return runner(system, user, max_tokens=max_tokens)


def _replace(base: Integration, **kw) -> Integration:
    """A new Integration from `base` with fields overridden. dataclasses.replace
    would do this, but spelling it out keeps the frozen dataclass's field list
    in one place when sections are added."""
    d = {"coverage": base.coverage, "citations": base.citations,
         "unsupported_claims": base.unsupported_claims,
         "open_gaps": base.open_gaps, "critique": base.critique,
         "critique_summary": base.critique_summary,
         "next_steps": base.next_steps, "ran": base.ran,
         "prompt_sources": base.prompt_sources, "problems": base.problems}
    d.update(kw)
    return Integration(**d)


def _cite(f) -> str:
    return (f"{f.document} p{f.page}" if getattr(f, "page", None) is not None
            else str(getattr(f, "document", "")))


def _parts_of(question: str, gaps) -> tuple[str, ...]:
    """The parts to check coverage for.

    react's own gap list FIRST: it is the decomposition this system already
    made, and re-deriving parts by parsing the question here would be a second
    author of it. Falls back to the whole question when no gaps were named at
    all -- a one-part question is the common case and must not be split
    invented.
    """
    open_gaps = gaps
    if open_gaps:
        return tuple(dict.fromkeys(str(g) for g in open_gaps if str(g).strip()))
    q = (question or "").strip()
    return (q[:120],) if q else ()


# ── 2 + 3. THE MODEL CALLS, run concurrently ────────────────────────────────

# What resolve() returned, per block, for the last run. Recorded rather than
# inferred: "the DB had it" and "we fell back" produce identical prompts to a
# reader and different provenance for a wording change.
PROMPT_SOURCES: dict[str, str] = {}


def _prompt_text(block_key: str, fallback: str) -> str:
    """The block's text, or our fallback — decided on SOURCE, never truthiness.

    🔴 resolve() returns a TRUTHY PLACEHOLDER for a block the DB does not have:
    "[missing prompt block: v2.integrator.critic]", source="missing". So
    `text or fallback` selects the placeholder, and the model receives that
    string as its entire system prompt. Caught by a test whose runner could not
    recognise its own prompt; in production it would have been a critic
    returning confident nonsense with no error anywhere.

    Same shape as an honest-empty dressed as evidence: the value is present, so
    every falsiness check passes, and the thing it represents is absent.
    """
    from app.pipeline.v2 import statement_text as ST
    try:
        text, source = ST.resolve(block_key, {})
    except Exception:
        text, source = "", "error"
    if source in ("db",) and (text or "").strip():
        PROMPT_SOURCES[block_key] = "db"
        return text
    PROMPT_SOURCES[block_key] = f"fallback ({source})"
    return fallback


def _critic_prompt(question, answer, facts, open_gaps) -> tuple[str, str]:
    body = {
        "question": question,
        "answer": answer[:6000],
        "facts": [{"fact": f.fact, "source": _cite(f)} for f in facts or ()],
        "open_gaps": list(open_gaps or ()),
    }
    return _prompt_text(CRITIC_BLOCK, _CRITIC_FALLBACK), json.dumps(body, ensure_ascii=False)


def _next_steps_prompt(question, answer, open_gaps) -> tuple[str, str]:
    body = {"question": question, "answer": answer[:3000],
            "open_gaps": list(open_gaps or ())}
    return _prompt_text(NEXT_STEPS_BLOCK, _NEXT_FALLBACK), json.dumps(body, ensure_ascii=False)


_CRITIC_FALLBACK = (
    "You are checking an answer against the evidence it was written from.\n"
    "You are given the question, the answer, and the FACTS with their sources. "
    "The passages themselves are gone — a claim you cannot tie to a listed "
    "fact is not supported, however plausible it sounds.\n\n"
    "Return JSON only:\n"
    '{"summary": "<one sentence>", "parts": [{"part": "<the part of the '
    'question>", "status": "supported|partial|unsupported|not_attempted", '
    '"why": "<one clause>", "evidence": ["<source of a fact that supports '
    'it>"]}]}\n\n'
    "status=supported requires you to name a fact's source in evidence. "
    "An empty evidence list with status=supported is a contradiction.\n"
    "If the answer is sound, say so — do not invent a criticism to look useful."
)

_NEXT_FALLBACK = (
    "Say what would actually close what is still open on this question.\n"
    "Return JSON only: {\"next_steps\": [\"<specific step>\", ...]}\n"
    "Name the document, payer or question to go after. At most three. "
    "Only steps this answer's own gaps call for — no generic advice, and "
    "nothing if the answer is complete."
)


def run(*, question: str, answer: str, facts=(), open_gaps=(), all_parts=(),
        decision, runner) -> Integration:
    """Assemble, then critique and plan next steps CONCURRENTLY.

    `runner(system, user, *, max_tokens) -> str` is injected so this is
    testable without a network, and so the model choice stays the caller's.
    """
    base = assemble(question=question, answer=answer, facts=facts,
                    open_gaps=open_gaps, all_parts=all_parts)
    if not getattr(decision, "runs_anything", False):
        # SKIPPED IS NOT FAILED and is not empty. The deterministic half still
        # ran and is returned; the two model sections say why they did not.
        return _replace(base,
                        ran={**base.ran, "critique": "skipped",
                             "next_steps": "skipped"},
                        problems=(f"not run: {getattr(decision, 'why', '')}",))

    problems: list[str] = []
    ran = dict(base.ran)

    def _critic():
        sys_p, user_p = _critic_prompt(question, answer, facts, open_gaps)
        return _call(runner, sys_p, user_p, CRITIC_MAX_TOKENS, "v2_critic")

    def _next():
        sys_p, user_p = _next_steps_prompt(question, answer, open_gaps)
        return _call(runner, sys_p, user_p, NEXT_STEPS_MAX_TOKENS,
                     "v2_next_steps")

    # SAFE TO PARALLELISE: both closures take strings and return strings and
    # touch no ctx. preload.execute is sequential because _execute_tool assigns
    # fifteen ctx attributes; that hazard does not exist here.
    with ThreadPoolExecutor(max_workers=2) as pool:
        f_critic, f_next = pool.submit(_critic), pool.submit(_next)
        critic_raw = _settle(f_critic, "critique", ran, problems)
        next_raw = _settle(f_next, "next_steps", ran, problems)

    critique, summary = _parse_critique(critic_raw, problems)
    steps = _parse_next_steps(next_raw, problems)

    return Integration(
        coverage=base.coverage, citations=base.citations,
        unsupported_claims=base.unsupported_claims, open_gaps=base.open_gaps,
        critique=critique, critique_summary=summary, next_steps=steps,
        ran=ran, prompt_sources=dict(PROMPT_SOURCES), problems=tuple(problems))


def default_runner(ctx):
    """The app's EXISTING model path, not a new one.

    Ananth, 2026-09-12: "you can reuse the existing models if they work which
    should for critic and next steps".

    They should: both calls read an answer and a fact list and return small
    JSON -- no retrieval, no long context, no tool use. So this goes through
    _call_llm_json exactly like every other react call, which means the roster,
    the bandit, the fallbacks and the llm_calls telemetry all apply unchanged.
    A separate client here would be a second model story to keep alive, and
    these two calls would be invisible in the cost benchmark that already
    reads llm_calls.

    reasoning_depth="fast": checking a claim against a listed fact is not a
    reasoning problem, and the whole case for running these outside the react
    loop is that they are cheap.

    STAGE NAMES ARE DISTINCT (v2_critic / v2_next_steps) so their cost and
    latency separate from react_N in every existing query rather than
    inflating the round numbers we just benchmarked.
    """
    from app.pipeline.react.prompts import _call_llm_json

    def _run(system: str, user: str, *, max_tokens: int,
             stage: str = "v2_critic") -> str:
        return _call_llm_json(system, user, max_tokens=max_tokens, ctx=ctx,
                              stage=stage, reasoning_depth="fast",
                              latency_budget_ms=4000)
    return _run


def _settle(fut, name, ran, problems):
    try:
        out = fut.result()
        ran[name] = "ok" if out else "empty"
        return out
    except Exception as e:
        # DEGRADE, NEVER FABRICATE. A missing critique is recorded as missing;
        # an invented one reads as a check that happened.
        ran[name] = "failed"
        problems.append(f"{name} call failed: {type(e).__name__}")
        logger.warning("[v2.integrator] %s failed: %s", name, e)
        return ""


def _truncated(raw) -> bool:
    """Did the reply stop mid-object rather than arrive malformed?

    A truncated reply and an unparseable one need OPPOSITE fixes -- raise the
    budget vs change the prompt -- and reporting both as "was not JSON" sent me
    to the prompt for a reply that was already correct.
    """
    s = str(raw or "").strip()
    if "{" not in s:
        return False
    body = s[s.find("{"):]
    return body.count("{") > body.count("}")


def _loads(raw):
    if not raw:
        return None
    s = str(raw).strip()
    # Fences: take what is BETWEEN the first pair, then drop a leading "json"
    # language tag. The previous version sliced with an index that was right
    # for one fence shape and silently produced garbage for the other -- the
    # critique came back empty on a perfectly good response.
    if "```" in s:
        chunks = s.split("```")
        if len(chunks) >= 3:
            s = chunks[1]
        if s.lstrip().lower().startswith("json"):
            s = s.lstrip()[4:]
    try:
        return json.loads(s.strip())
    except Exception:
        start, end = s.find("{"), s.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(s[start:end + 1])
            except Exception:
                return None
    return None


def _parse_critique(raw, problems) -> tuple[tuple[PartVerdict, ...], str]:
    d = _loads(raw)
    if not isinstance(d, dict):
        if _truncated(raw):
            problems.append(
                f"critique TRUNCATED at {len(str(raw))} chars — the reply was "
                f"valid JSON with its tail missing; raise CRITIC_MAX_TOKENS "
                f"(currently {CRITIC_MAX_TOKENS}), do not change the prompt")
        elif raw:
            problems.append("critique was not JSON")
        return (), ""
    out = []
    for p in (d.get("parts") or []):
        if not isinstance(p, dict):
            continue
        ev = tuple(str(x) for x in (p.get("evidence") or []) if str(x).strip())
        status = str(p.get("status") or "unobservable")
        # A SUPPORTED VERDICT WITH NO EVIDENCE IS A CONTRADICTION, and it is
        # the one a lenient judge produces. Downgraded here rather than
        # trusted, because the whole point of the critic is not taking the
        # model's word for it.
        if status == "supported" and not ev:
            status = "unobservable"
            problems.append(
                f"critic said 'supported' for {str(p.get('part'))[:40]!r} with "
                "no evidence — downgraded to unobservable")
        out.append(PartVerdict(str(p.get("part") or ""), status,
                               str(p.get("why") or ""), ev))
    return tuple(out), str(d.get("summary") or "")


def _parse_next_steps(raw, problems) -> tuple[str, ...]:
    d = _loads(raw)
    if not isinstance(d, dict):
        if _truncated(raw):
            problems.append(
                f"next_steps TRUNCATED at {len(str(raw))} chars — raise "
                f"NEXT_STEPS_MAX_TOKENS (currently {NEXT_STEPS_MAX_TOKENS})")
        elif raw:
            problems.append("next_steps was not JSON")
        return ()
    return tuple(str(s).strip() for s in (d.get("next_steps") or [])
                 if str(s).strip())[:3]
