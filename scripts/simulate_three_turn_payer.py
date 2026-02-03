#!/usr/bin/env python3
"""Simulate 3-turn chat: Sunshine -> what is their website -> United Healthcare website.
No DB or network; uses state extractor and merge/filter logic to show what payer and RAG filters would be each turn."""
import copy
import sys
from pathlib import Path

# Run from mobius-chat so app is importable
_root = Path(__file__).resolve().parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

from app.storage.threads import DEFAULT_STATE
from app.state.state_extractor import extract_state_patch
from app.payer_normalization import normalize_payer_for_rag


def merge_state(state: dict, patch: dict) -> dict:
    out = copy.deepcopy(state)
    for k, v in patch.items():
        if isinstance(out.get(k), dict) and isinstance(v, dict):
            out[k] = {**out.get(k, {}), **v}
        else:
            out[k] = v
    return out


def rag_filters_from_state(state: dict | None) -> dict:
    if not state:
        return {}
    out = {}
    active = state.get("active") or {}
    raw_payer = (active.get("payer") or "").strip()
    if raw_payer:
        payer = normalize_payer_for_rag(raw_payer)
        if payer:
            out["filter_payer"] = payer
    jur = (active.get("jurisdiction") or "").strip()
    if jur:
        out["filter_state"] = jur
    return out


def main():
    # Simulate same thread across 3 turns (no TTL, no DB)
    state = copy.deepcopy(DEFAULT_STATE)
    turns = [
        "Sunshine prior auth",
        "what is their website",
        "Do you have the website for United Healthcare",
    ]
    print("Simulating 3-turn conversation (single thread, no DB)\n")
    for i, message in enumerate(turns, 1):
        print(f"--- Turn {i} ---")
        print(f"  message: {message!r}")
        existing_payer = (state.get("active") or {}).get("payer")
        patch, reset_reason = extract_state_patch(message, state, None, None)
        new_state = merge_state(state, patch)
        patch_payer = (patch.get("active") or {}).get("payer")
        new_payer = (new_state.get("active") or {}).get("payer")
        filters = rag_filters_from_state(new_state)
        print(f"  existing_payer: {existing_payer}")
        print(f"  patch_payer:    {patch_payer}")
        print(f"  new_payer:      {new_payer}")
        print(f"  reset_reason:  {reset_reason}")
        print(f"  RAG filters:   {filters}")
        # Next turn uses this state (simulate save_state)
        state = new_state
        print()
    print("Done. If Turn 3 shows new_payer=United Healthcare and filter_payer=United Healthcare, state switch works.")


if __name__ == "__main__":
    main()
