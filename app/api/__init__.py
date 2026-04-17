"""Phase 1 — chat HTTP API routers.

Each module in this package holds the ``APIRouter`` for one cohesive slice
of the chat surface. ``main.py`` wires them in via ``app.include_router``.

Rule (enforced by CI in Phase 1e): new chat endpoints go here, NOT in
``main.py``. The goal is to shrink ``main.py`` from a 3,125-line file
holding 86 endpoints down to a ~200-line app-wiring module.

Naming convention: one module per URL prefix / concern group.
    history   — /chat/history/*            (Phase 1a, this commit)
    chat      — /chat/*                    (Phase 1b, pending)
    credentialing — /credentialing/*       (Phase 1c, pending)
    pages     — HTML page serves           (Phase 1d, pending)
    admin     — /admin/*, debug, eval      (Phase 1d, pending)
    health    — /health, /ready            (Phase 1d, pending)
"""
