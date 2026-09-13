"""THE V2 REACT CONTRACT — what v2 asks react to return, declared as data.

Ananth, 2026-09-12: "lets get the contract for react structured.. this will be
immensely useful telemetry as we start to store it. over time this is gold.. we
also need it to store the facts/things it found useful vs not useful (so that
we dont have to send it again)" and then "you need to create your version
for v2".

V2'S OWN SHAPE, NOT A TYPED COPY OF V1'S. The difference that matters is how
evidence is carried back:

    v1   "keep": [1, 4, 7]        chunk NUMBERS, positional against the last
                                  tool result -- they mis-attribute the moment
                                  a round makes two tool calls, and they mean
                                  nothing once the chunks are gone.
    v2   facts: [{fact, document, page}]
                                  a STATEMENT with provenance. It survives the
                                  chunks, it can be checked against the source,
                                  and it can be re-sent in ~100 characters
                                  where the chunk it came from costs ~9,000.

That last property is the whole "so we dont have to send it again": a fact is
cheap enough to carry across turns, a passage is not. Measured this session --
one preload is 141,074 characters; the answer it supports is three facts.

WHAT THIS IS NOT. It is not a second response_shape. The WORDING that asks
react for these fields lives in app/pipeline/react/prompts.py and is the LLM
seat's. This declares the shape as a typed object so the parser, the telemetry
and the store read ONE declaration rather than each re-deriving it from prose.

READS V1 SHAPE TOO, and says so in `problems`. The live prompt still asks for
v1's shape, so a v2 contract that only accepted v2 fields would report every
real turn as malformed and teach us nothing. It accepts both and RECORDS which
it got -- the migration is then visible in the table instead of assumed.

VERSIONED, because the rows are the asset and a shape that changes without a
version makes every historical row silently incomparable.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace

CONTRACT_VERSION = 2


@dataclass(frozen=True)
class Fact:
    """One thing react learned, with where it came from.

    `document`/`page` are what make it checkable later and what let it be
    re-sent instead of the passage. A fact with no provenance is a claim, and
    this session has already shipped one answer whose citation markers pointed
    at nothing.
    """
    fact: str = ""
    document: str = ""
    page: int | None = None
    # 🔴 THE CORPUS IDENTIFIER, resolved from the turn's own sources.
    #
    # react can only cite what it can see, which is a DISPLAY NAME. The
    # deterministic verifier needs the id: Deep Research measured unscoped
    # verification at 144s and I measured 80.4s with two of three timing out,
    # against ~0.5s scoped. Tool Manifest: "the input should take the id", and
    # they explicitly refused to keep a name→id map in the catalogue because
    # that is RAG's data and a second copy is the duplication Ananth ruled
    # against.
    #
    # So it is resolved HERE, where both halves are already in hand: the fact's
    # document name, and ctx.sources carrying document_name + document_id from
    # the retrieval that produced it. Empty when the source had no id
    # (fetch_document sets it None on some paths) — which lands as
    # `unverifiable`, correctly.
    document_id: str = ""

    @property
    def grounded(self) -> bool:
        return bool(self.fact and self.document)


@dataclass(frozen=True)
class GapState:
    gap_id: str = ""
    text: str = ""
    status: str = ""          # open | closed | unreachable


@dataclass(frozen=True)
class ReactV2Response:
    thought: str = ""
    # What react understood its job to be this round. Compared against the
    # roles the frame actually sent -- the disagreement is the signal that the
    # role stack is or is not landing.
    roles_assumed: tuple[str, ...] = ()

    # EVIDENCE, as facts rather than chunk indices.
    facts: tuple[Fact, ...] = ()
    not_useful: tuple[str, ...] = ()      # sources read and rejected

    # WHICH PASSAGES REACT ACTUALLY USED, as ordinals into the numbered list it
    # was shown this round (preload._render_chunk). Small ints, because that is
    # the only kind of identifier a model reproduces reliably -- see that
    # function's header for why chunk_id and (document, page) were both
    # rejected. Resolved to real chunks by with_kept_chunks(), server-side.
    #
    # Kept-only, deliberately: rejected is derivable as served-minus-kept by
    # anyone holding the served set, so only ONE side has to be reliable.
    # Retriever asked for exactly this and it is the smaller surface.
    kept_indices: tuple[int, ...] = ()
    # Indices react named that do not exist in this round's list. Never
    # silently dropped: an out-of-range index means react is addressing a list
    # it cannot see, and that is a prompt defect worth finding, not noise.
    kept_unresolved: tuple[int, ...] = ()

    gaps: tuple[GapState, ...] = ()
    running_answer: str = ""
    answer: str = ""

    # v2 NAMES a tool; it does not call one. The preloader runs it next round.
    tool_request: str = ""
    tool_reason: str = ""

    is_complete: bool | None = None
    complete_why: str = ""
    next_round_worth_it: bool | None = None
    next_round_why: str = ""

    # Which shape actually arrived, and what could not be read. A field that
    # silently defaulted and a field react genuinely sent empty are different
    # facts, and only one of them is react's behaviour.
    shape_seen: str = "none"              # v2 | v1 | mixed | none
    problems: tuple[str, ...] = ()


def _strs(v) -> tuple[str, ...]:
    if isinstance(v, str):
        return (v,) if v.strip() else ()
    if isinstance(v, (list, tuple)):
        return tuple(str(x).strip() for x in v if str(x).strip())
    return ()


def _bool_or_none(v):
    return v if isinstance(v, bool) else None


def _ints(v) -> tuple[int, ...]:
    """Ordinals from whatever the model emitted, order preserved, deduped.

    Accepts 3, "3", and " 3 " -- a model asked for JSON ints will sometimes
    send strings, and refusing those would discard a correct answer over its
    type. Rejects anything else, and rejects <= 0: the list is 1-based, so 0 is
    not an off-by-one to be forgiven, it is a sign the model is counting from
    somewhere else.
    """
    if isinstance(v, (int, str)):
        v = [v]
    if not isinstance(v, (list, tuple)):
        return ()
    out: list[int] = []
    for x in v:
        if isinstance(x, bool):          # bool is an int subclass; not an index
            continue
        try:
            n = int(str(x).strip())
        except (TypeError, ValueError):
            continue
        if n > 0 and n not in out:
            out.append(n)
    return tuple(out)


def _facts_from_v2(raw) -> tuple[tuple[Fact, ...], list[str]]:
    out, problems = [], []
    for item in (raw if isinstance(raw, (list, tuple)) else []):
        if isinstance(item, str):
            # A bare string is a fact with no provenance. Kept, because
            # dropping it would hide that react answered ungrounded -- and
            # flagged, because that is the thing worth finding.
            out.append(Fact(fact=item.strip()))
            problems.append("fact with no document")
            continue
        if not isinstance(item, dict):
            problems.append("fact entry was not an object or string")
            continue
        pg = item.get("page")
        try:
            pg = int(pg) if pg is not None else None
        except (TypeError, ValueError):
            pg = None
        f = Fact(fact=str(item.get("fact") or "").strip(),
                 document=str(item.get("document") or "").strip(),
                 page=pg)
        if f.fact:
            out.append(f)
            if not f.document:
                problems.append("fact with no document")
    return tuple(out), problems


def with_document_ids(resp: "ReactV2Response",
                      id_by_name: dict) -> "ReactV2Response":
    """Attach corpus ids to facts, matched on the document name react cited.

    Exact match first, then a case-insensitive match, then nothing. NO FUZZY
    MATCHING: a fact attached to the wrong document verifies against the wrong
    text, and a confident wrong verdict is worse than an honest unverifiable.
    """
    if not id_by_name or not resp.facts:
        return resp
    lower = {str(k).lower(): v for k, v in id_by_name.items() if k and v}
    out = []
    for f in resp.facts:
        did = id_by_name.get(f.document) or lower.get(str(f.document).lower(), "")
        out.append(Fact(f.fact, f.document, f.page, did or ""))
    return replace(resp, facts=tuple(out))


def with_kept_chunks(resp: "ReactV2Response",
                     kept_sources: list | tuple) -> tuple["ReactV2Response", list]:
    """Resolve react's ordinals against the list it was actually shown.

    Returns (response, chunks) -- the response with unresolvable indices
    recorded, and the real chunk dicts react said it used.

    `kept_sources` MUST be the same list, in the same order, that
    preload.fair_share returned for this round: index i means kept_sources[i-1].
    Passing a differently-ordered list resolves every index to the wrong
    passage, silently, so the caller owns that pairing.

    Mirrors with_document_ids: the identifier is attached HERE from data we
    already hold, never taken from the model. An index react invents cannot
    become a real chunk id -- it lands in kept_unresolved instead.
    """
    if not resp.kept_indices:
        return resp, []
    n = len(kept_sources or ())
    good, bad, chunks = [], [], []
    for i in resp.kept_indices:
        if 1 <= i <= n:
            good.append(i)
            chunks.append(kept_sources[i - 1])
        else:
            bad.append(i)
    return replace(resp, kept_indices=tuple(good),
                   kept_unresolved=tuple(bad)), chunks


def parse(raw: dict | None) -> ReactV2Response:
    """Raw model JSON -> the v2 contract. NEVER raises.

    A parse failure must not kill a turn, and must not vanish either: the turn
    where react returned an unreadable shape is exactly the one worth finding
    later, so everything unreadable lands in `problems` and is stored with the
    rest.
    """
    if not isinstance(raw, dict):
        return ReactV2Response(problems=("response was not an object",))

    problems: list[str] = []
    saw_v2 = saw_v1 = False

    facts, fprob = _facts_from_v2(raw.get("facts"))
    problems.extend(fprob)
    if "facts" in raw:
        saw_v2 = True

    # v1 fallback: evidence_review carries running_answer/gaps and chunk
    # numbers. Chunk numbers are NOT converted into facts -- a number is not a
    # statement, and inventing a fact text for it would fabricate provenance.
    er = raw.get("evidence_review")
    if isinstance(er, dict):
        saw_v1 = True
        if not facts and er.get("keep"):
            problems.append(
                "v1 keep[] present: chunk numbers cannot become facts "
                "(no statement, no provenance) — evidence not carried forward")

    gaps: list[GapState] = []
    if isinstance(raw.get("gaps"), (list, tuple)):
        saw_v2 = True
        for g in raw["gaps"]:
            if isinstance(g, dict):
                gaps.append(GapState(str(g.get("gap_id") or ""),
                                     str(g.get("text") or ""),
                                     str(g.get("status") or "open")))
            elif isinstance(g, str) and g.strip():
                gaps.append(GapState(text=g.strip(), status="open"))
    elif isinstance(er, dict):
        for t in _strs(er.get("gaps_open")):
            gaps.append(GapState(text=t, status="open"))
        for t in _strs(er.get("gaps_closed")):
            gaps.append(GapState(text=t, status="closed"))

    ack = raw.get("ack") if isinstance(raw.get("ack"), dict) else {}
    if ack:
        saw_v1 = True

    running = str(raw.get("running_answer")
                  or (er or {}).get("running_answer") or "")
    not_useful = _strs(raw.get("not_useful"))
    if "not_useful" in raw:
        saw_v2 = True

    # `kept` is the contract name; `kept_indices` accepted because a model that
    # has seen the field name in a schema sometimes echoes the internal one.
    kept_indices = _ints(raw.get("kept", raw.get("kept_indices")))

    shape = ("mixed" if (saw_v2 and saw_v1)
             else "v2" if saw_v2 else "v1" if saw_v1 else "none")
    if shape in ("v1", "none"):
        problems.append(f"v1-shaped response ({shape}): no facts[], "
                        "no not_useful[] — nothing to carry to the next turn")

    return ReactV2Response(
        thought=str(raw.get("thought") or ""),
        roles_assumed=_strs(raw.get("roles_assumed")),
        facts=facts,
        not_useful=not_useful,
        kept_indices=kept_indices,
        gaps=tuple(gaps),
        running_answer=running,
        answer=str(raw.get("answer") or ""),
        tool_request=str(raw.get("tool_request") or raw.get("tool") or ""),
        tool_reason=str(raw.get("tool_reason") or ""),
        is_complete=_bool_or_none(raw.get("is_complete")),
        complete_why=str(raw.get("complete_why")
                         or ack.get("complete_why") or ""),
        next_round_worth_it=_bool_or_none(
            raw.get("next_round_worth_it")
            if "next_round_worth_it" in raw
            else ack.get("next_round_worth_it")),
        next_round_why=str(raw.get("next_round_why")
                           or ack.get("next_round_why") or ""),
        shape_seen=shape,
        problems=tuple(problems),
    )


def to_row(r: ReactV2Response, *, correlation_id: str, thread_id: str,
           round_index: int) -> dict:
    """The storable form. The rows ARE the asset -- this is the schema of the
    thing Ananth called gold, so it stores the claims AND what was unreadable
    about them."""
    return {
        "correlation_id": correlation_id,
        "thread_id": thread_id,
        "round_index": round_index,
        "contract_version": CONTRACT_VERSION,
        "shape_seen": r.shape_seen,
        "thought": r.thought,
        "roles_assumed": list(r.roles_assumed),
        "facts": [asdict(f) for f in r.facts],
        "not_useful": list(r.not_useful),
        "gaps": [asdict(g) for g in r.gaps],
        "running_answer": r.running_answer,
        "answer_chars": len(r.answer),
        "tool_request": r.tool_request,
        "tool_reason": r.tool_reason,
        "is_complete": r.is_complete,
        "complete_why": r.complete_why,
        "next_round_worth_it": r.next_round_worth_it,
        "next_round_why": r.next_round_why,
        "problems": list(r.problems),
    }
