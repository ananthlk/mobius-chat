"""The size-class vocabulary, READ from the seam rather than copied into it.

Deep Research measured that SIZE, not document KIND, decides which retrieval
move is available. Their two tests of kind both failed: `doc_type` is NULL on
13,642 of ~17,000 documents, and `d_tags` reaches 99.9% coverage but
`health_care_services` dominates 12,274 of 16,976 -- a near-constant separates
nothing.

🔴 WHY THIS READS A TABLE INSTEAD OF HOLDING THE TEXT.

The vocabulary is authored once, in their repo, and PUBLISHED to
`research.size_class_vocabulary`. I cannot import their module and they cannot
import mine, so pasting the words into this prompt would be exactly the
cross-repo form of the drift `plan_shape.py` exists to prevent in-repo -- two
copies of one vocabulary, diverging silently, with no gate able to see both.
The table is the seam. Edit their module and re-publish; never edit rows.

EXPECTED, NOT MEASURED. FRAME runs before anything is fetched, so it gets the
face with no counts: the model names the class a KIND of document usually
falls in, from general knowledge, and says it has not checked. Reporting a
prior as a measurement is the error this face exists to prevent, so the
rendered text says so in the words the model reads.

`unmeasured` is deliberately EXCLUDED here: it is a fact about our corpus
("holds text in tables, not yet sized"), not something answerable from general
knowledge about a genre.

KINDS ARE GENRES, NEVER DOCUMENTS. "A provider manual is usually large" is
general knowledge. A specific heading or filename would be a claim about a
document nobody opened -- which cost Deep Research a run today when three
example headings in a prompt were carried into a plan as facts about one
manual's index.

FAILS OPEN, AND ON A SHORT LEASH. This sits on the prompt path of every turn.
I reported Tool Manifest's `estimate()` today for opening a psycopg2
connection with no connect_timeout on exactly this path -- it blocked 45-75s
and was charged to the turn budget. Adding an unbounded connect of my own
would be the same defect with my name on it. So: an explicit connect_timeout,
a process-lifetime cache, and an empty string on any failure. FRAME without
the size block is FRAME as it was yesterday; FRAME behind a hanging socket is
a turn nobody gets.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

#: Seconds. Short on purpose -- see the fails-open note above.
CONNECT_TIMEOUT_S = 3

#: The classes FRAME may name, in ladder order. `unmeasured` is not here.
EXPECTED_CLASSES = ("read_whole", "section_read", "retrieval_only")

_CACHE: str | None = None


def _rows() -> list[dict]:
    import psycopg2

    url = (os.environ.get("TOOLREG_DATABASE_URL")
           or os.environ.get("MOBIUS_RAG_DATABASE_URL") or "").strip()
    if not url:
        return []
    conn = psycopg2.connect(url, connect_timeout=CONNECT_TIMEOUT_S)
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT name, rule, move, why, kinds "
            "FROM research.size_class_vocabulary")
        cols = ("name", "rule", "move", "why", "kinds")
        return [dict(zip(cols, r)) for r in cur.fetchall()]
    finally:
        conn.close()


def expected_block() -> str:
    """FRAME's face of the vocabulary, or "" when it cannot be read."""
    global _CACHE
    if _CACHE is not None:
        return _CACHE
    try:
        by_name = {r["name"]: r for r in _rows() if r.get("name")}
    except Exception as exc:
        logger.warning("[v2.size_class] vocabulary unreadable (%r) — FRAME "
                       "ships without the size block", exc)
        _CACHE = ""
        return _CACHE

    parts: list[str] = []
    for name in EXPECTED_CLASSES:
        row = by_name.get(name)
        if not row:
            continue
        head = f"  {(row.get('move') or name).strip()} — {(row.get('rule') or '').strip()}"
        body = " ".join((row.get("why") or "").split())
        kinds = [k for k in (row.get("kinds") or []) if k]
        lines = [head, f"      {body}"]
        if kinds:
            lines.append(f"      Usually: {', '.join(kinds)}.")
        parts.append("\n".join(lines))

    if not parts:
        _CACHE = ""
        return _CACHE

    _CACHE = (
        "\n\nHOW BIG IS THAT KIND OF DOCUMENT, USUALLY? Size decides which move "
        "is even available:\n\n"
        + "\n\n".join(parts)
        + "\n\n  THIS IS A PRIOR, NOT A MEASUREMENT. You are naming the class a\n"
          "  KIND of document usually falls in, from general knowledge, before\n"
          "  anything has been opened. Say which class you expect AND that you\n"
          "  have not checked."
    )
    return _CACHE


#: Which tool performs each class's move, on OUR side. A class whose tool is
#: None is a move this loop cannot make -- rendering it would teach the model
#: to plan something nobody can execute, which is the failure Deep Research's
#: `as_tool` flag exists to prevent. `section_read` is deliberately absent:
#: it needs a document id AND a section, which is not fillable from a bare
#: question (Tool Manifest: permanently `inputs_status='unfillable'`, belongs
#: in `suggest`, which does not exist yet).
TOOL_FOR_CLASS = {
    "read_whole": "fetch_document",
    "retrieval_only": "rag",
}


def escalation_block() -> str:
    """The rungs THIS loop can actually climb, for a posture that already
    holds retrieval.

    🔴 WHY THIS EXISTS AT ALL. Measured on live turn 54c3f84f, on a question
    that opens with the words "Open the Sunshine Provider Manual": the loop
    ran rag, answered from the chunks, and NEVER called fetch_document. The
    tool is offered by estimate(), its inputs are `fillable`, and react can
    dispatch it -- a small-enough document comes back as an inline attachment
    the next round reads whole. Every part works. Nothing ever asks.

    The prompt never told it the move existed. Deep Research measured
    read-whole settling at 95% against retrieval's 16%, and this loop was
    taking the 16% path on a question that NAMED the document.

    Only classes with a tool in TOOL_FOR_CLASS are rendered.
    """
    try:
        by_name = {r["name"]: r for r in _rows() if r.get("name")}
    except Exception as exc:
        logger.warning("[v2.size_class] ladder unreadable (%r)", exc)
        return ""
    parts = []
    for name, tool in TOOL_FOR_CLASS.items():
        row = by_name.get(name)
        if not row:
            continue
        why = " ".join((row.get("why") or "").split())
        parts.append(
            f"  {(row.get('move') or name).strip()} — {(row.get('rule') or '').strip()}\n"
            f"      Tool: {tool}\n"
            f"      {why}")
    if not parts:
        return ""
    return (
        "\n\nYOU ARE ALREADY HOLDING RETRIEVAL. A broad sweep ran before you\n"
        "spoke, so the question is not which move to make — it is whether to\n"
        "CLIMB OFF the one you have:\n\n"
        + "\n\n".join(parts)
        + "\n\n  IF THE QUESTION NAMES A DOCUMENT, OR YOUR EVIDENCE DOES, that is a\n"
          "  document you have not read — you are holding passages someone ranked\n"
          "  out of it. Name it and read it rather than searching the same corpus\n"
          "  again with different words."
    )
