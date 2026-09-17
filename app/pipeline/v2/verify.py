"""Deterministic claim verification: check our facts against the page they cite.

Ananth, 2026-09-13: "we will bake in a deterministic critique which we already
have (the tool that manifest developed for check a fact).. and if the critique
is really found anything then we go back to react, else we move on.. this is a
quality control, and we add good emits for it".

🔴 WE CALL THE TOOL. WE DO NOT COPY IT. Ananth, 2026-09-12: "no dont lift.. i
have asked tools manifest to own .. so it can benefit and not duplicate". Deep
Research built the verifier and recommended I lift it here; I did, and was
corrected. A verifier in the manifest has ONE owner and serves every consumer;
a copy in chat serves chat and drifts the first time either moves.

WHY DETERMINISTIC. This session watched an LLM critic mark three
correctly-grounded payers "unsupported" from an empty fact list — could-not-
check rendered as checked-false, on an answer carrying real citations. And the
promise-path critic measured 3.7s of a 10s turn while the governor's own
decision line recorded `critic=None`: a producer whose consumer was nothing.

WHAT THE VERIFIER DOES THAT SIMILARITY CANNOT. Deep Research measured their own
first version scoring "six years" at 0.943 against a source saying FIVE, and
passing it. Numbers now fail closed: every figure a claim asserts is put to the
document as a PHRASE, so a near-quote with the number changed comes back
not_supported WITH the score — which is a different instruction to react than
"nothing like this was found".
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

TOOL_KEY = "verify_claims"

# 🔴 THE BAR IS OURS TO CHOOSE, AND IT IS RECORDED RATHER THAN INHERITED.
#
# Deep Research: the default 0.62 is PERMISSIVE, tuned for a caller that
# DELETES on not_supported — which is us. Being wrong in our direction costs a
# correct claim, so a low bar loses fewer. A caller that PUBLISHES on a
# supported verdict has the opposite safe direction and must raise it. Passed
# explicitly on every call so the choice is in the request, not assumed.
BAR = float(os.environ.get("MOBIUS_V2_VERIFY_BAR", "0.62") or 0.62)

# Their ceiling is 144,000ms UNSCOPED. Scoped with document_ids it is ~1.1s for
# five facts — which is why document_ids is required below rather than optional.
MAX_FACTS = int(os.environ.get("MOBIUS_V2_VERIFY_MAX_FACTS", "12") or 12)


@dataclass(frozen=True)
class Finding:
    """One claim the cited source does not support."""
    fact: str = ""
    document: str = ""
    page: int | None = None
    verdict: str = ""
    score: float | None = None
    why: str = ""
    numbers_missing: tuple[str, ...] = ()
    citation_wrong: str = ""

    def repair(self) -> str:
        """FIX THE NUMBER and DROP THE CLAIM are not the same instruction.

        Deep Research: not_supported at 0.94 with numbers_missing tells you
        this is a near-quote with the figure changed. Handing react "this was
        not supported" for that case invites it to delete a claim that is
        nearly right when the repair is one token.
        """
        if self.numbers_missing:
            return (f"WRONG FIGURE — the source does not contain "
                    f"{', '.join(self.numbers_missing)}. The wording matches "
                    f"closely (score {self.score}); correct the number from "
                    f"{self.document} p{self.page} or drop the claim.")
        if self.citation_wrong:
            return (f"WRONG PAGE — the document says this, but at "
                    f"{self.citation_wrong}, not p{self.page}. Fix the "
                    f"citation; the claim itself stands.")
        return (f"NOT SUPPORTED at {self.document} p{self.page} "
                f"(score {self.score}) — {self.why or 'no matching text'}")

    def __str__(self) -> str:
        return f"{self.fact[:90]} — {self.repair()}"


@dataclass(frozen=True)
class VerifyResult:
    findings: tuple[Finding, ...] = ()
    checked: int = 0
    supported: int = 0
    unverifiable: int = 0
    skipped: str = ""            # why we did not check at all
    #: Why each UNVERIFIABLE fact could not be checked, most common first.
    #:
    #: 🔴 THE COUNT WITHOUT THE REASON IS A COULD-NOT-CHECK WITH NO TELL.
    #: This reported "unverifiable=8" and dropped the service's `why` on the
    #: floor, so the only way to learn WHY was to call the verifier by hand --
    #: which is what I had to do. The answer was one line ("<doc> is not in
    #: the corpus") and it had been returned on every one of those 8 rows,
    #: every turn, and thrown away each time.
    #:
    #: A reason that is returned and discarded is worse than one never
    #: produced: it makes the gap look unexplainable when it is merely
    #: unrecorded.
    unverifiable_why: tuple[tuple[str, int], ...] = field(default_factory=tuple)
    duration_ms: int = 0
    bar: float = BAR
    problems: tuple[str, ...] = field(default_factory=tuple)

    @property
    def reopen(self) -> bool:
        """Go back to react ONLY if something was actually found.

        UNVERIFIABLE IS NOT A FINDING. A claim the verifier could not check is
        not a claim it refuted, and reopening on one would spend a round on the
        verifier's blind spot. Same line this module family has drawn all
        session.
        """
        return bool(self.findings)


def _norm(name: str) -> str:
    """Document names compared the way a filesystem would, not a regex.

    The id map is keyed by document NAME, so a fact whose name differs by case
    or whitespace is sent with a map that cannot scope it — and one unscoped
    fact costs the whole batch (see SCOPING_IS_A_CLIFF).
    """
    return " ".join(str(name or "").split()).casefold()


def verifiable(facts) -> tuple[list, dict, str]:
    """(facts worth sending, their id map, why the rest were not).

    🔴 SCOPING IS A CLIFF, NOT A SLOPE. Measured against the live service,
    9 identical facts:

        with document_ids        550ms
        without document_ids  180,157ms  ->  HTTP 504 Gateway Timeout
        empty document_ids    181,716ms  ->  HTTP 504 Gateway Timeout

    So an unscoped fact does not make the call slower, it destroys it — and it
    takes the facts that COULD have been checked down with it. Our own live
    turn cost 11.5s (cid 4033e5cf), which is neither number: partial scoping.

    That is why this returns the MAP as well as the facts, built from the same
    normalised names the payload will carry. A fact whose document name has no
    entry is dropped here, with a reason, rather than sent to be attempted.
    """
    ok, ids, no_id, no_page, no_doc = [], {}, 0, 0, 0
    for f in facts or ():
        doc = str(getattr(f, "document", "") or "")
        if not doc.strip():
            no_doc += 1
            continue
        if not getattr(f, "document_id", ""):
            no_id += 1
            continue
        if getattr(f, "page", None) in (None, ""):
            no_page += 1
            continue
        ok.append(f)
        ids[doc] = getattr(f, "document_id")

    # THE ASSERTION. Every fact in the payload must be scopable by the map that
    # travels with it. Built from the same facts, so this can only fail on a
    # name that normalises differently from itself -- which is exactly the
    # 44-char truncation defect that once shipped 7 citations to documents that
    # do not exist, arriving from the other direction.
    keys = {_norm(k) for k in ids}
    scoped, unscopable = [], 0
    for f in ok:
        if _norm(getattr(f, "document", "")) in keys:
            scoped.append(f)
        else:
            unscopable += 1

    why = []
    if no_doc:
        why.append(f"{no_doc} with no document name")
    if no_id:
        # SAY WHICH KIND OF ABSENCE. A corpus fact with no id is a defect in
        # our id resolution; a CERTIFIED FACT-STORE answer has no corpus id by
        # design — payor_fact returns authority=fact_store with its own source,
        # locator and as_of. Both are unchecked, and reporting them the same
        # way turns a known design boundary into an unexplained gap.
        why.append(f"{no_id} with no corpus document_id — a certified "
                   f"fact-store answer carries its own provenance (source, "
                   f"locator, as_of) and no corpus id, so it is NOT CHECKED "
                   f"HERE rather than checked slowly; unscoped verification "
                   f"times out at 180s and takes the whole batch with it")
    if no_page:
        why.append(f"{no_page} with no page")
    if unscopable:
        why.append(f"{unscopable} whose document name is not in the id map "
                   f"(a name that cannot be scoped costs the batch, not just "
                   f"itself)")
    return scoped[:MAX_FACTS], ids, "; ".join(why)


def verify(facts, runner, *, bar: float = BAR) -> VerifyResult:
    """Check facts against their cited pages. `runner(tool_key, inputs)->dict`.

    NEVER RAISES, and never reports a failure to check as a failed claim. If
    the verifier could not run, `skipped` says why and `findings` is empty —
    because "we could not check" and "this is wrong" are different, and
    collapsing them is the defect this whole module exists to avoid.
    """
    sendable, ids, excluded = verifiable(facts)
    if not sendable:
        return VerifyResult(skipped=excluded or "no facts with a document and page")

    payload = [{"fact": getattr(f, "fact", ""),
                "document": getattr(f, "document", ""),
                "page": getattr(f, "page", None)} for f in sendable]
    # 🔴 NO "LAST GATE" HERE, AND THAT IS DELIBERATE.
    #
    # I wrote one — every payload fact must appear in the id map — and two
    # mutation checks passed with it disabled, because `verifiable()` BUILDS
    # the map from the same facts it returns. The gate could not fail. A guard
    # with no reachable failure path is the defect this seat has filed at three
    # other seats today, so it is removed rather than shipped.
    #
    # The real protection is upstream and IS reachable: verifiable() drops any
    # fact without a document name, a document_id, or a page, and says which.
    # The remaining risk is not a mismatch between our facts and our own map —
    # it is a mismatch between OUR name and the SERVICE's, which cannot be
    # detected here and shows up as a slow or timing-out call. That belongs in
    # the request contract, and is raised with the tool's owners.

    try:
        res = runner(TOOL_KEY, {"facts": payload, "document_ids": ids,
                                "bar": bar}) or {}
    except Exception as e:                       # pragma: no cover - defensive
        return VerifyResult(skipped=f"verifier call raised: {type(e).__name__}: {e}")

    if res.get("could_not_run") or res.get("outcome") == "could_not_run":
        return VerifyResult(skipped=str(res.get("reason") or "verifier could not run")[:200])

    body = res.get("payload") if isinstance(res.get("payload"), dict) else res
    rows = (body or {}).get("results") or (body or {}).get("facts") or []
    if not isinstance(rows, list):
        return VerifyResult(skipped=f"unrecognised verifier response: {type(rows).__name__}")

    found, supported, unverifiable = [], 0, 0
    why_counts: dict[str, int] = {}
    for r, src in zip(rows, sendable):
        if not isinstance(r, dict):
            continue
        verdict = str(r.get("verdict") or "").lower()
        if verdict == "supported":
            supported += 1
        elif verdict == "unverifiable":
            unverifiable += 1
            # Keep the REASON, not just the tally. Normalised to a shape that
            # groups: the document name varies per row, the failure does not.
            _w = str(r.get("why") or "").strip() or "no reason given"
            _doc = str(getattr(src, "document", "") or "").strip()
            if _doc and _doc in _w:
                _w = _w.replace(_doc, "<document>")
            why_counts[_w[:120]] = why_counts.get(_w[:120], 0) + 1
        elif verdict == "not_supported":
            found.append(Finding(
                fact=getattr(src, "fact", ""),
                document=getattr(src, "document", ""),
                page=getattr(src, "page", None),
                verdict=verdict,
                score=r.get("score"),
                why=str(r.get("why") or "")[:200],
                numbers_missing=tuple(r.get("numbers_missing") or ()),
                citation_wrong=str(r.get("citation_wrong") or ""),
            ))
    return VerifyResult(
        findings=tuple(found), checked=len(rows), supported=supported,
        unverifiable=unverifiable,
        unverifiable_why=tuple(sorted(why_counts.items(),
                                      key=lambda kv: -kv[1])),
        duration_ms=int(res.get("duration_ms") or 0),
        bar=bar,
        problems=((excluded,) if excluded else ()),
    )
