"""ThreadState: dataclass with apply_delta. Replaces patch-based merge."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from app.storage.threads import DEFAULT_STATE


@dataclass
class ThreadState:
    """Explicit state model. apply_delta(delta) performs explicit transitions (no arbitrary shallow merge)."""

    active: dict[str, Any] = field(default_factory=dict)
    open_slots: list[str] = field(default_factory=list)
    resolved_slots: dict[str, str] = field(default_factory=dict)
    recent_entities: list[Any] = field(default_factory=list)
    last_user_intent: str | None = None
    last_updated_turn_id: str | None = None
    safety: dict[str, Any] = field(default_factory=dict)
    refined_query: str | None = None
    master_objective: dict[str, Any] | None = None

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> ThreadState:
        """Build from storage shape. Handles legacy nested structure."""
        if not d:
            d = json.loads(json.dumps(DEFAULT_STATE))
        active = d.get("active")
        if isinstance(active, dict):
            active = dict(active)
        else:
            active = {}
        return cls(
            active=active,
            open_slots=list(d.get("open_slots") or []),
            resolved_slots=dict(d.get("resolved_slots") or {}),
            recent_entities=list(d.get("recent_entities") or []),
            last_user_intent=d.get("last_user_intent"),
            last_updated_turn_id=d.get("last_updated_turn_id"),
            safety=dict(d.get("safety") or {}),
            refined_query=d.get("refined_query"),
            master_objective=d.get("master_objective"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "active": dict(self.active),
            "open_slots": list(self.open_slots),
            "resolved_slots": dict(self.resolved_slots),
            "recent_entities": list(self.recent_entities),
            "last_user_intent": self.last_user_intent,
            "last_updated_turn_id": self.last_updated_turn_id,
            "safety": dict(self.safety),
            "refined_query": self.refined_query,
            "master_objective": self.master_objective,
        }

    def apply_delta(self, delta: dict[str, Any]) -> None:
        """Apply explicit delta. For nested dicts (e.g. active), merges at top level of that key."""
        if not delta:
            return
        for k, v in delta.items():
            if k == "active" and isinstance(v, dict):
                current = self.active
                self.active = {**current, **v}
            elif k == "open_slots":
                self.open_slots = list(v) if v else []
            elif k == "resolved_slots" and isinstance(v, dict):
                # Merge: user-provided values always win; never overwrite with None/empty
                merged = dict(self.resolved_slots)
                for slot_k, slot_v in v.items():
                    if slot_v and str(slot_v).lower() not in ("null", "none", ""):
                        merged[slot_k] = str(slot_v)
                self.resolved_slots = merged
            elif k == "recent_entities":
                self.recent_entities = list(v) if v else []
            elif k in ("last_user_intent", "last_updated_turn_id", "refined_query", "master_objective"):
                setattr(self, k, v)
            elif k == "safety" and isinstance(v, dict):
                self.safety = {**self.safety, **v}
            else:
                setattr(self, k, v)

    def clear_slots(self) -> None:
        """Clear both open_slots and resolved_slots. Called on STANDALONE route."""
        self.open_slots = []
        self.resolved_slots = {}
