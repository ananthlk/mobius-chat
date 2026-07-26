"""turn_context — the per-turn user-steer block: sanitize + PHI-gate.

The highest-stakes block in v2: user chat text injected into the prompt
composition. Spec: docs/SPEC_LLMMANAGER_V2.md §3.3. Reviewed by the
PHI-classifier session before any validated_at is set.

Two defenses, both required (presence of one does not excuse the other):

  1. SANITIZE (PHI-C2) — SSTI + forgery.
     * SSTI is defended by the CALLER, not here: the steer is passed as a BOUND
       Jinja variable and the assembled prompt is rendered ONCE. This module never
       builds a template from user text. sanitize_steer() adds the forgery belt:
       neutralize role-markers / fence / heading tokens, collapse newlines (no
       fake block boundaries), strip control chars, and hard-cap length.
  2. PHI-GATE (PHI-C3) — fail-closed.
     * A message-derived steer reuses the ingestion verdict (no new call). Any
       transformed / non-message-derived steer gets a FRESH classifier call.
       Classifier error / timeout / indeterminate  → drop (never inject).
     * If the message was already blocked at ingestion, there is no turn to
       steer — the text is never resurrected here.

gate_steer() returns the sanitized, PHI-cleared steer string, or None. None
means: drop the turn_context block and render the turn UN-STEERED (the caller
must not fail the whole turn, and must never inject an un-gated steer).
"""
from __future__ import annotations

import re

_MAX_STEER_CHARS = 512

# Role / structural markers a steer could use to forge a boundary or authority
# line. Matched at a line start (after optional whitespace), case-insensitive.
_ROLE_MARKERS = re.compile(
    r"(?im)^\s*(system|assistant|user|tool|developer)\s*:",
)
_FENCE_OR_HEADING = re.compile(r"(?m)^\s*(`{3,}|#{1,6}|-{3,}|={3,})")
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def sanitize_steer(raw: str | None) -> str:
    """Defang a user steer so it cannot forge a block boundary or authority line.

    Does NOT defend SSTI — that is the caller's variable-binding + single-pass
    render (this string is passed as a bound var, never concatenated into a
    template source). Returns a single-line, capped, marker-neutralized string.
    """
    if not raw:
        return ""
    s = str(raw)
    s = _CONTROL_CHARS.sub("", s)                 # strip control chars
    # Neutralize role markers and fences/headings by breaking the token so it
    # can't be parsed as a boundary (keep it human-readable, don't silently drop).
    s = _ROLE_MARKERS.sub(lambda m: m.group(0).replace(":", "​:"), s)
    s = _FENCE_OR_HEADING.sub(lambda m: "​" + m.group(1), s)
    s = re.sub(r"\s+", " ", s).strip()            # collapse all whitespace → one line
    if len(s) > _MAX_STEER_CHARS:                 # hard cap
        s = s[:_MAX_STEER_CHARS].rstrip()
    return s


# gate result sentinel — None means "drop the block, render un-steered".
def gate_steer(
    raw_steer: str | None,
    *,
    message_derived: bool,
    ingestion_blocked: bool,
    ingestion_phi_clean: bool | None,
    fresh_phi_check=None,
) -> str | None:
    """Sanitize + PHI-gate a steer. Returns the safe steer, or None (drop).

    message_derived      — steer is a verbatim / trivially-cleaned slice of the
                           already-gated user message (→ may reuse the verdict).
    ingestion_blocked    — the message was blocked at the pre-farm gate.
    ingestion_phi_clean  — the ingestion verdict for the message (True = clean),
                           reused ONLY for a message-derived steer.
    fresh_phi_check      — callable(text) -> bool (True = safe/clean) for a fresh
                           classifier call on a non-message-derived steer.
                           Absent + fresh call needed = fail-closed (drop).
    """
    # Never resurrect text the ingestion gate blocked.
    if ingestion_blocked:
        return None
    if not raw_steer:
        return None

    if message_derived:
        # Reuse the ingestion verdict — no new call. Fail-closed on anything but
        # an explicit clean verdict.
        if ingestion_phi_clean is not True:
            return None
    else:
        # New exposure → fresh classifier call, fail-closed on any non-clean/error.
        if fresh_phi_check is None:
            return None
        try:
            if fresh_phi_check(raw_steer) is not True:
                return None
        except Exception:
            return None  # classifier error/timeout → treat as PHI-present

    safe = sanitize_steer(raw_steer)
    return safe or None
