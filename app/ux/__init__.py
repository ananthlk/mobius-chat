# app.ux — Backend-for-Frontend (BFF) layer. One read-only, presentation-only aggregator
# module per UX surface (bubble_backend, and later tasks/diagnostics/surfaces/...). Each is
# the single point its frontend surface maps to; none imports router/planner/enricher logic.
# See docs/bubble-backend-contract.md.
