"""Orchestrator v2 — the posture machine.

Ananth ruled 2026-09-11 that the governor seat builds and owns v2.

This package holds DECISIONS ONLY. It executes nothing, reads no clock, touches
no database and imports nothing from app.pipeline.react_loop. Everything it
needs arrives as an argument.

That is not style. A pure module can be run twice and compared -- which is the
entire mechanism by which v2 is proven inert against v1 on live traffic (R0
shadow). Two independent monotonic() reads in one turn cannot be compared for
equality, so the clock is an input.
"""
