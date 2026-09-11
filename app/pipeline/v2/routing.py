"""A/B assignment and the version banner.

PURE. Assignment is a function of the correlation_id and the split percentage --
nothing else. No clock, no random, no env read at call time.

WHY DETERMINISTIC ON correlation_id AND NOT random.random():
  * a turn cannot flip arms mid-flight
  * a retry lands on the same arm
  * the assignment is reproducible from the stored row alone, so a disputed
    result can be re-derived rather than re-run

WHY RANDOMISED CONCURRENT ARMS AND NOT PHASED ROUTING BY TIER:
  the first plan was "v2 takes single-round fast, v1 keeps everything else".
  That compares v2-on-fast against v1-on-everything -- version confounded with
  tier, with turn shape, and with whatever traffic arrived in each window. It is
  the same defect that invalidated a tier comparison on 2026-09-10, where 11 of
  16 rows were one question fanned across three tiers and the pattern was the
  denominator rather than the finding. Concurrent arms over the same traffic
  hold everything constant except the version.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

V1 = "v1"
V2 = "v2"


def assign(correlation_id: str, v2_pct: int) -> str:
    """Stable arm assignment. v2_pct=0 -> everything on v1 (R0 shadow)."""
    if v2_pct <= 0:
        return V1
    if v2_pct >= 100:
        return V2
    digest = hashlib.sha256(correlation_id.encode("utf-8")).digest()
    bucket = int.from_bytes(digest[:4], "big") % 100
    return V2 if bucket < v2_pct else V1


@dataclass(frozen=True)
class Banner:
    """The first emit of every turn.

    Ananth: "prominently display which version it is so that I can follow."

    First emit, not a footer: the version must be visible WHILE the turn runs,
    not discovered afterwards. If a turn looks wrong, line one says which
    orchestrator to blame -- which is also the cheapest guard against attributing
    a v1 problem to v2.
    """
    version: str
    tier: str
    promised_latency_s: float
    band_s: float
    postures: tuple[str, ...]

    def line(self) -> str:
        seq = " → ".join(p.upper() for p in self.postures)
        return (
            f"▣ {self.version} · {self.tier} · "
            f"promise {self.promised_latency_s:g}s ±{self.band_s:g} · {seq}"
        )


def decision_line(version: str, round_index: int, because: str) -> str:
    """Every decision emit carries the arm.

    'Emit what you decided and why you decided it -- not what you are doing.'
    'Composing answer…' is a spinner; this is the same event carrying the reason.
    """
    return f"{version} · round {round_index} · {because}"
