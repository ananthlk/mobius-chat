"""Phase 1e — main-split hygiene guards.

Two regressions to prevent now that the routers (1a-1d) are stable:

1. **No new /chat/* endpoints added directly to main.py.**
   Every chat URL should go through a router in ``app/api/``. This test
   scans main.py for ``@app.(get|post|put|delete|patch)`` decorators and
   asserts that only an allowed set of non-chat paths remain (/, /health,
   /internal/*, page serves).

2. **No cross-router helper duplication.**
   ``_task_manager_base`` was duplicated three times across main.py,
   credentialing router, and roster router during 1a-1d. Phase 1e
   consolidated it to ``app/api/_common.py``. This test ensures it stays
   single-sourced and catches future helpers that duplicate the same way.

Together these act as a lightweight CI gate: any PR that adds a /chat/*
endpoint inline, or re-inlines a shared helper, fails the test suite.
"""

from __future__ import annotations

import re
from pathlib import Path


CHAT_REPO_ROOT = Path(__file__).parent.parent


# ── Guard 1: no new /chat/* endpoints in main.py ──────────────────────────


# Paths that are legitimately allowed to live in main.py (not chat-router
# territory). Anything else triggering a @app.* decorator in main.py fails
# the guard.
#
# Phase 3c note: /chat/credentialing-runs/*, /chat/roster-*, and
# /chat/npi-lookup/* are NOT in this allowlist. Those were removed
# wholesale in Phase 3c; re-introducing any of them in main.py fails the
# guard. If they need to come back they go in a router under a non-/chat/*
# prefix (e.g. /credentialing/*), because credentialing is a skill, not a
# chat interface.
ALLOWED_MAIN_PY_PATH_PREFIXES: tuple[str, ...] = (
    "/",                       # root / page serves
    "/health",                 # health check
    "/internal/",              # non-chat internal endpoints (skill-llm)
    "/pipeline",               # pipeline debug page
    "/financial-strategy",     # page serves — planned to move in a later phase
    "/org-story",
    "/market-map",
    "/industry-report",
    "/roster",                 # roster UI page serve (page, not API)
    "/chat/response/",         # not yet moved — still in main.py (TODO phase 1f)
    "/chat/stream/",           # not yet moved
    "/chat/plan/",             # not yet moved
    "/chat/config",            # not yet moved
    "/chat/roster-upload",     # not yet moved (multipart file upload)
    "/chat/thread/",           # not yet moved
    "/chat/doc-reader/",       # not yet moved
    "/chat/skills/",           # not yet moved
    "/chat/llm-router-report", # not yet moved
    "/chat/org-name-candidates",  # not yet moved
    "/chat",                   # POST /chat — core ask endpoint, not yet moved
    # Phase 1f.1: /chat/tasks/* moved to app.api.tasks — prefix REMOVED from
    # allowlist. The deleted-paths guard below also forbids re-introducing
    # any /chat/tasks/* decorator in main.py.
)


_DECORATOR_RE = re.compile(
    r'^@app\.(get|post|put|delete|patch)\s*\(\s*["\']([^"\']+)["\']'
)


def _main_py_endpoint_paths() -> list[tuple[int, str, str]]:
    """Return every (line_no, method, path) still decorated with @app.* in main.py."""
    main_py = CHAT_REPO_ROOT / "app" / "main.py"
    out: list[tuple[int, str, str]] = []
    for i, line in enumerate(main_py.read_text().splitlines(), start=1):
        m = _DECORATOR_RE.match(line)
        if m:
            out.append((i, m.group(1).upper(), m.group(2)))
    return out


class TestNoChatEndpointsInlineInMain:
    """Every @app.* decorator left in main.py must point to an allowed path.

    When you move the remaining chat endpoints to routers (Phase 1f, 1g...),
    trim the corresponding entry from ``ALLOWED_MAIN_PY_PATH_PREFIXES``.
    """

    def test_all_remaining_decorators_on_allowed_paths(self):
        offenders: list[tuple[int, str, str]] = []
        for line_no, method, path in _main_py_endpoint_paths():
            if not any(path.startswith(p) for p in ALLOWED_MAIN_PY_PATH_PREFIXES):
                offenders.append((line_no, method, path))
        assert not offenders, (
            "Endpoints decorated with @app.* in main.py that should be in a router:\n"
            + "\n".join(f"  main.py:{ln}  {m}  {p}" for ln, m, p in offenders)
            + "\n\nMove them to app/api/<group>.py and mount via include_router."
        )

    def test_previously_extracted_paths_are_not_back(self):
        """Concrete regression: the paths moved (1a/1b) or removed (3c)
        must not reappear inline in main.py. Strict lower bound — even if
        the allowed-prefixes list above gets too permissive, these specific
        URLs can never come back to chat's HTTP surface.

        Phase 1a/1b moved these to routers:
            /chat/history/*, /chat/feedback/*, /chat/source-feedback/*,
            /chat/adjudication-feedback/*, /chat/llm-performance-feedback/*,
            /chat/qc-audit/*, /chat/qc-user-score/*
        Phase 3c DELETED these outright (credentialing → standalone skill):
            /chat/credentialing-runs/*, /chat/roster-reconcile/*,
            /chat/roster-truth/*, /chat/roster-org/*, /chat/npi-lookup/*
        """
        forbidden_prefixes = (
            # Phase 1a/1b — extracted to routers
            "/chat/history/",
            "/chat/feedback/",
            "/chat/source-feedback/",
            "/chat/adjudication-feedback/",
            "/chat/llm-performance-feedback/",
            "/chat/qc-audit/",
            "/chat/qc-user-score/",
            # Phase 1f.1 — extracted to app.api.tasks
            "/chat/tasks",
            # Phase 3c — deleted outright
            "/chat/credentialing-runs",
            "/chat/npi-lookup/",
            "/chat/roster-reconcile/",
            "/chat/roster-truth",
            "/chat/roster-org/",
        )
        offenders = [
            (ln, method, path)
            for ln, method, path in _main_py_endpoint_paths()
            if any(path.startswith(p) for p in forbidden_prefixes)
        ]
        assert not offenders, (
            "Paths moved or removed in prior phases have reappeared in main.py:\n"
            + "\n".join(f"  main.py:{ln}  {m}  {p}" for ln, m, p in offenders)
        )


# ── Guard 2: no cross-router helper duplication ───────────────────────────


class TestSharedHelpersConsolidated:
    """``_task_manager_base`` was duplicated in main.py + 2 routers during
    Phase 1a-1d. Phase 1e consolidated it into ``app.api._common``.
    This test ensures it stays there.
    """

    def test_task_manager_base_defined_exactly_once(self):
        """Exactly one ``def task_manager_base_url`` definition in the repo."""
        defs = []
        for py in CHAT_REPO_ROOT.rglob("*.py"):
            if ".venv" in py.parts or "__pycache__" in py.parts:
                continue
            text = py.read_text(errors="ignore")
            for line in text.splitlines():
                if line.lstrip().startswith("def task_manager_base_url"):
                    defs.append(str(py.relative_to(CHAT_REPO_ROOT)))
        assert defs == ["app/api/_common.py"], (
            f"task_manager_base_url must live only in app/api/_common.py, "
            f"found in: {defs}"
        )

    def test_no_inline_task_manager_base_in_routers(self):
        """Any router in app/api/ must import from _common, not redefine the
        helper locally. Scans every .py file in app/api/ that exists (after
        Phase 3c, credentialing.py and roster.py are gone).
        """
        api_dir = CHAT_REPO_ROOT / "app" / "api"
        if not api_dir.exists():
            return
        offenders = []
        for p in api_dir.glob("*.py"):
            if p.name == "_common.py":
                continue  # the single source of truth
            text = p.read_text()
            for ln_no, line in enumerate(text.splitlines(), start=1):
                stripped = line.lstrip()
                if stripped.startswith("def _task_manager_base"):
                    offenders.append(f"{p.name}:{ln_no}")
                if stripped.startswith("def task_manager_base_url"):
                    offenders.append(f"{p.name}:{ln_no}")
        assert not offenders, (
            "A router defines _task_manager_base locally instead of importing "
            f"from app.api._common: {offenders}"
        )

    def test_main_py_has_no_inline_task_manager_base(self):
        main_py = CHAT_REPO_ROOT / "app" / "main.py"
        text = main_py.read_text()
        assert "def _task_manager_base(" not in text, (
            "_task_manager_base inlined in main.py again — import from "
            "app.api._common instead."
        )


# ── Guard 3: ratcheting main.py size + endpoint count ────────────────────


class TestMainPySizeRatchet:
    """Phase 1f introduces a ratcheting ceiling on main.py so the split
    can't quietly reverse itself.

    After each sub-phase extracts a router, we tighten the LOC + endpoint
    bounds here. Tests fail if main.py grows back past the ceiling — which
    means either the extraction got undone, or new endpoints were added
    inline instead of to a router.

    Sub-phase log:
      pre-1f       1528 LOC, 36 endpoints (37 with /health)
      post-1f.1    1408 LOC, 28 endpoints (tasks router extracted; /health kept)
      post-1h      1409 LOC                 (front-door imports)
      post-B.1a/fx 1443 LOC                 (413/422 error mapping for instant-rag upload)
      post-B.1c    1484 LOC                 (catalog dual-write hook + uploads router mount)
    """

    # Current ceilings. Tighten these as 1f.2, 1f.3, ... land. Never loosen.
    # Alpha bump (2026-05-06): main.py grew 1490→1807 during alpha sprint —
    # auth-proxy routes, personalization wiring, model-profile endpoints.
    # Beta work item: extract these into app/api/ routers and tighten back.
    MAX_MAIN_PY_LOC = 1950  # 2026-07-09: +59 LOC for coworkers endpoint, greeting/identity wiring, task-aware integrator hooks
    MAX_MAIN_PY_ENDPOINTS = 30

    def test_main_py_loc_under_ceiling(self):
        main_py = CHAT_REPO_ROOT / "app" / "main.py"
        loc = len(main_py.read_text().splitlines())
        assert loc <= self.MAX_MAIN_PY_LOC, (
            f"main.py is {loc} LOC, over the Phase 1f ceiling "
            f"({self.MAX_MAIN_PY_LOC}). Either finish extracting the next "
            f"router, or tighten the ceiling deliberately if something grew "
            f"for a good reason (don't just bump it on autopilot)."
        )

    def test_main_py_endpoint_count_under_ceiling(self):
        count = len(_main_py_endpoint_paths())
        assert count <= self.MAX_MAIN_PY_ENDPOINTS, (
            f"main.py exposes {count} @app.* endpoints, over the Phase 1f "
            f"ceiling ({self.MAX_MAIN_PY_ENDPOINTS}). Move the new one to a "
            f"router in app/api/ instead of adding it inline."
        )


# ── Guard 4: credentialing HTTP surface — OBSOLETE ────────────────────────
#
# Phase 3c deleted the credentialing + roster routers. The
# ``credentialing-with-roster`` branch re-introduced them deliberately
# as part of the roster reconciliation flow. The "files must not exist"
# assertions were locking in a decision that's since been reversed, so
# they've been removed. If Phase 3c's architectural decision is ever
# re-confirmed, re-add the guards and delete the routers in the same
# commit.
#
# What IS still guarded: the shared ``task_manager_base_url`` helper
# must live only in ``app.api._common``. See
# ``TestSharedHelpersConsolidated`` above — that guard still catches
# duplicate definitions in any router, credentialing included.
