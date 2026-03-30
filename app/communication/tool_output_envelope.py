"""
Standard envelope for large tool/skill outputs (credentialing, NPPES, reconciliation, etc.).

Contract
--------
1. **Summary (internal)** — Short grounding for ReAct, planners, and logs. Same text may be
   carried in structured fields (e.g. ``result_summary``) without repeating the full markdown.
2. **Detail (user)** — Full markdown for **user display** and **user download** (same body unless
   a product later adds a separate export blob).

Big methods should return or populate:
- A concise summary string (internal / consistency).
- A full markdown string (user display & download).

When serializing to a single assistant message, use :func:`compose_mobius_tool_envelope` so
section roles are explicit in the prose.

Version bumps only when headings or semantics change (search ``MOBIUS_TOOL_OUTPUT_VERSION``).
"""
from __future__ import annotations

MOBIUS_TOOL_OUTPUT_VERSION = "v1"

PREAMBLE = (
    f"*Mobius tool output **{MOBIUS_TOOL_OUTPUT_VERSION}*** — "
    "**Summary** = internal (ReAct / consistency). "
    "**Detail** = user display & user download.\n\n"
)

# Heading lines only (no trailing newlines); compose joins with blank lines between blocks.
MARKER_SUMMARY = "### Summary _(internal · ReAct / consistency)_"
MARKER_DETAIL = "### Detail _(user display · download)_"


def compose_mobius_tool_envelope(
    summary_internal: str,
    detail_user_markdown: str,
    *,
    include_preamble: bool = True,
) -> str:
    """
    Merge summary and full markdown into one labeled user-facing string.

    Args:
        summary_internal: Short line(s) for reasoning traces; may duplicate structured summaries.
        detail_user_markdown: Full artifact for UI and exports.
        include_preamble: Set False when embedding inside another template (rare).
    """
    s = (summary_internal or "").strip()
    d = (detail_user_markdown or "").strip()
    parts: list[str] = []
    if include_preamble:
        parts.append(PREAMBLE.rstrip())
        parts.append("")
    parts.extend(
        [
            MARKER_SUMMARY,
            "",
            s if s else "_No summary._",
            "",
            MARKER_DETAIL,
            "",
            d if d else "_No detail._",
        ]
    )
    return "\n".join(parts).strip()


def split_mobius_tool_envelope(text: str) -> tuple[str, str]:
    """
    Best-effort extract (summary, detail) from an envelope composed by this module.
    If markers are missing, returns ("", stripped text).
    """
    t = (text or "").strip()
    if MARKER_SUMMARY not in t or MARKER_DETAIL not in t:
        return "", t
    try:
        after_sum = t.split(MARKER_SUMMARY, 1)[1].strip()
        summary_part, detail_rest = after_sum.split(MARKER_DETAIL, 1)
        return summary_part.strip(), detail_rest.strip()
    except (ValueError, IndexError):
        return "", t


__all__ = [
    "MOBIUS_TOOL_OUTPUT_VERSION",
    "PREAMBLE",
    "MARKER_SUMMARY",
    "MARKER_DETAIL",
    "compose_mobius_tool_envelope",
    "split_mobius_tool_envelope",
]
