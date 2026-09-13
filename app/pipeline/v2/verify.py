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


def verifiable(facts) -> tuple[list, str]:
    """(facts worth sending, why the rest were not).

    A fact with no document_id cannot be scoped, and unscoped verification
    reaches 144s — their number. So an unscoped fact is not checked SLOWLY, it
    is not checked, and we say so.
    """
    ok, no_id, no_page = [], 0, 0
    for f in facts or ():
        if not getattr(f, "document_id", ""):
            no_id += 1
            continue
        if getattr(f, "page", None) in (None, ""):
            no_page += 1
            continue
        ok.append(f)
    why = []
    if no_id:
        why.append(f"{no_id} with no document_id (unscoped verification is "
                   f"144s, so these are NOT checked rather than checked slowly)")
    if no_page:
        why.append(f"{no_page} with no page")
    return ok[:MAX_FACTS], "; ".join(why)


def verify(facts, runner, *, bar: float = BAR) -> VerifyResult:
    """Check facts against their cited pages. `runner(tool_key, inputs)->dict`.

    NEVER RAISES, and never reports a failure to check as a failed claim. If
    the verifier could not run, `skipped` says why and `findings` is empty —
    because "we could not check" and "this is wrong" are different, and
    collapsing them is the defect this whole module exists to avoid.
    """
    sendable, excluded = verifiable(facts)
    if not sendable:
        return VerifyResult(skipped=excluded or "no facts with a document and page")

    payload = [{"fact": getattr(f, "fact", ""),
                "document": getattr(f, "document", ""),
                "page": getattr(f, "page", None)} for f in sendable]
    ids = {getattr(f, "document", ""): getattr(f, "document_id", "")
           for f in sendable if getattr(f, "document", "")}

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
    for r, src in zip(rows, sendable):
        if not isinstance(r, dict):
            continue
        verdict = str(r.get("verdict") or "").lower()
        if verdict == "supported":
            supported += 1
        elif verdict == "unverifiable":
            unverifiable += 1
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
        duration_ms=int(res.get("duration_ms") or 0),
        bar=bar,
        problems=((excluded,) if excluded else ()),
    )
