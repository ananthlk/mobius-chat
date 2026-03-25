"""Rule-based parsing for credentialing vs roster-reconciliation *data path* (not autopilot/copilot).

Autopilot/co-pilot mode is chosen elsewhere (UI or ReAct tool args). This module only answers:
- Should we steer toward outside-in credentialing, reconciliation (upload compare), or stay neutral?
- Did the user ask to see prior thread uploads?
- Did they hint at using the latest upload vs uploading something new?

Pure functions — safe to call from parser, blueprint, tests, or future UI wizards.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

DataPath = Literal["outside_in", "reconciliation", "unspecified"]
RosterSourceHint = Literal["latest_upload", "upload_new", "unspecified"]


class CredentialingFlowIntent(BaseModel):
    """Structured hints from the user message. All fields are conservative defaults."""

    data_path: DataPath = Field(
        default="unspecified",
        description="outside_in = no file/compare language; reconciliation = upload vs external; unspecified = let other routing decide.",
    )
    roster_source_hint: RosterSourceHint = Field(
        default="unspecified",
        description="latest_upload = use prior thread upload; upload_new = user will/did ask to attach new file; unspecified.",
    )
    request_upload_inventory: bool = Field(
        default=False,
        description="User wants to see what roster/files are already on this chat thread.",
    )


def parse_credentialing_flow_intent(message: str) -> CredentialingFlowIntent:
    """Parse user text into flow hints (keywords / light patterns only)."""
    t = (message or "").strip().lower()
    if not t:
        return CredentialingFlowIntent()

    request_upload = _wants_upload_inventory(t)
    recon = _is_reconciliation_intent(t)
    outside = _is_outside_in_intent(t)
    upload_new = _hints_upload_new(t)
    latest = _hints_use_latest_upload(t)

    data_path: DataPath = "unspecified"
    if recon and not outside:
        data_path = "reconciliation"
    elif outside and not recon:
        data_path = "outside_in"
    elif recon and outside:
        # Contradiction — stay unspecified so planner/ReAct can clarify
        data_path = "unspecified"

    roster_source: RosterSourceHint = "unspecified"
    if data_path == "reconciliation" or request_upload:
        if upload_new and not latest:
            roster_source = "upload_new"
        elif latest and not upload_new:
            roster_source = "latest_upload"

    return CredentialingFlowIntent(
        data_path=data_path,
        roster_source_hint=roster_source,
        request_upload_inventory=request_upload,
    )


def credentialing_flow_intent_for_planner(message: str) -> dict:
    """JSON-serializable dict for Mobius planner_input_json (and logs)."""
    return parse_credentialing_flow_intent(message).model_dump()


# --- internals ---


def _wants_upload_inventory(t: str) -> bool:
    phrases = (
        "what did i upload",
        "did i upload",
        "what have i uploaded",
        "list my upload",
        "list uploads",
        "files attached",
        "files on this chat",
        "uploads on this chat",
        "uploads on this thread",
        "previous roster",
        "previous rosters",
        "prior roster",
        "rosters i uploaded",
        "roster files",
        "show my upload",
        "what's attached",
        "whats attached",
        "any files attached",
        "documents attached",
    )
    return any(p in t for p in phrases)


def _is_reconciliation_intent(t: str) -> bool:
    phrases = (
        "reconciliation report",
        "reconcile roster",
        "reconcile the roster",
        "roster reconciliation",
        "reconcile my",
        "reconcile our",
        "compare my roster",
        "compare our roster",
        "compare upload",
        "upload against",
        "against my upload",
        "against our upload",
        "using my uploaded",
        "using our uploaded",
        "uploaded roster",
        "my roster file",
        "our roster file",
    )
    if any(p in t for p in phrases):
        return True
    if "reconcile" in t and ("roster" in t or "upload" in t):
        return True
    return False


def _is_outside_in_intent(t: str) -> bool:
    phrases = (
        "outside-in",
        "outside in",
        "without a roster file",
        "without uploading",
        "no roster file",
        "no file to upload",
        "don't use our roster",
        "do not use our roster",
        "standard credentialing",
        "credentialing only",
    )
    return any(p in t for p in phrases)


def _hints_upload_new(t: str) -> bool:
    phrases = (
        "upload a new",
        "upload new",
        "new file",
        "attach a new",
        "i will upload",
        "need to upload",
        "have to upload",
    )
    return any(p in t for p in phrases)


def _hints_use_latest_upload(t: str) -> bool:
    phrases = (
        "latest upload",
        "most recent upload",
        "last upload",
        "previous upload",
        "the file i uploaded",
        "already uploaded",
        "use my upload",
        "use the upload",
        "same file",
    )
    return any(p in t for p in phrases)
