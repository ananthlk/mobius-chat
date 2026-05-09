"""Skill registry — the plug-and-play dispatch contract for chat tools.

The registry pattern replaces the hand-maintained cascade in
``app/services/tool_agent.py::_answer_tool_impl`` where every skill lives
as a bespoke ``if hint == "X": return _run_X(...)`` branch. Each skill
becomes a ``SkillSpec`` registered at import time; chat dispatches by
name via ``dispatch()``.

Design invariants (enforced by tests in ``test_skill_registry.py``):

1. **One source of truth per skill.** The ``SkillSpec.name`` is the same
   string the planner emits, the same key ``dispatch()`` looks up, and
   the same header printed in the planner manifest. No drift possible.

2. **In-process vs. remote is invisible to the dispatcher.** A handler
   can call an in-process Python helper, an MCP server via
   ``call_mcp_tool``, an HTTP skill — the dispatcher only sees
   ``SkillEnvelope`` out. An ``MCPSkillAdapter`` (future commit) builds
   ``SkillSpec``s from a remote ``list_tools`` response, so every MCP
   tool auto-registers as a chat skill.

3. **The envelope is typed.** ``SkillEnvelope`` replaces the untyped
   ``tuple[str, list[dict], dict[str, Any] | None, str]`` that every
   ``answer_tool`` caller unpacks today. The typed shape is what
   ``mobius_contracts`` will formalize next.

4. **The planner manifest is computed, not hand-maintained.** Once all
   five live skills are migrated (commits 2 + 3), ``TOOL_MANIFEST`` in
   ``app/pipeline/tool_manifest.py`` becomes ``registry.manifest_text()``
   — no more regressions where a new skill is registered but the planner
   doesn't see it.

This commit (commit 1 of the registry series) introduces the types and
migrates ``document_upload_skill`` + ``list_thread_document_uploads``
— the two skills with no MCP calls, no external state, and no error
handling to speak of. Proves the pattern. Both the old ``if
hint == "X"`` branches and the new registry path are live; the
``MOBIUS_USE_SKILL_REGISTRY`` env flag (default on) chooses which path
dispatches. Flag goes away in commit 3 once all skills are migrated.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable

logger = logging.getLogger(__name__)


# ── Envelope types ────────────────────────────────────────────────────
#
# These are plain dataclasses for now. Commit 2 / the mobius_contracts
# pass can lift them to Pydantic / tag them with the typed
# RetrievalSignal enum; the shape is already right so the lift is
# local.


@dataclass(frozen=True)
class SourceRef:
    """A citation returned by a skill. Replaces the dict-of-strings the
    old tuple return-shape carried."""

    document_name: str
    index: int = 1
    text: str = ""
    source_type: str = "external"  # external | web | internal | registry | document
    url: str | None = None
    # 2026-04-25: optional fields added so skills that return
    # corpus-document references (fetch_document, search_corpus path B)
    # can carry the document_id + page + arbitrary extras (e.g.
    # download_url, fetch_intent, payer/state tags) without forcing
    # consumers to dig into envelope.extra and correlate by index.
    document_id: str | None = None
    page_number: int | None = None
    authority: str | None = None
    extra: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        """Legacy bridge: callers that still consume ``list[dict]`` get
        the old shape. Delete once all consumers are on SourceRef."""
        out: dict[str, Any] = {
            "document_name": self.document_name,
            "index": self.index,
            "source_type": self.source_type,
        }
        if self.text:
            out["text"] = self.text
        if self.url:
            out["url"] = self.url
        if self.document_id:
            out["document_id"] = self.document_id
        if self.page_number is not None:
            out["page_number"] = self.page_number
        if self.authority:
            out["authority"] = self.authority
        if self.extra:
            # Splat extras at the top level so the frontend (which reads
            # source.download_url, source.fetch_intent, etc.) doesn't
            # need to know about an `extra` nesting.
            for k, v in self.extra.items():
                if k not in out:  # don't overwrite the canonical fields
                    out[k] = v
        return out


@dataclass(frozen=True)
class SkillEnvelope:
    """What every skill returns. Typed replacement for the 4-tuple."""

    text: str
    sources: list[SourceRef] = field(default_factory=list)
    signal: str = "no_sources"  # RETRIEVAL_SIGNAL_* string for now; enum in commit 2+
    usage: dict[str, Any] | None = None
    extra: dict[str, Any] = field(default_factory=dict)
    """``extra`` is the escape hatch for skill-specific out-of-band data
    (e.g. roster_step_outputs). Kept so the registry doesn't force a
    breaking change on consumers that read ``extra_out`` today. New
    skills should avoid ``extra`` and extend ``SkillEnvelope`` instead."""

    def to_legacy_tuple(self) -> tuple[str, list[dict[str, Any]], dict[str, Any] | None, str]:
        """Bridge to the old ``answer_tool`` return shape. Lets
        ``_answer_tool_impl`` dispatch through the registry while its
        callers still unpack a 4-tuple. Remove once
        ``_answer_tool_impl`` itself returns a ``SkillEnvelope``."""
        return (
            self.text,
            [s.to_dict() for s in self.sources],
            self.usage,
            self.signal,
        )


# ── Call context ──────────────────────────────────────────────────────


@dataclass
class SkillCall:
    """Everything a skill handler needs from chat. The dispatcher builds
    this from ``_answer_tool_impl``'s kwargs; handlers never touch
    ``_answer_tool_impl`` internals directly."""

    name: str
    inputs: dict[str, Any]
    question: str
    user_message: str | None = None
    thread_id: str | None = None
    active_context: dict[str, Any] | None = None
    mode: str = "copilot"  # copilot | agentic | quick
    emitter: Callable[[str], None] | None = None
    pipeline_ctx: Any | None = None
    extra_out: dict[str, Any] | None = None
    """``extra_out`` mirrors the current ``extra_out`` dict kwarg some
    tools mutate (roster_step_outputs, report_run_id, …). Registry-era
    handlers should prefer returning ``SkillEnvelope.extra`` and let the
    dispatcher copy it out — but during migration, ``extra_out`` stays
    to avoid a cascading rewrite."""


# ── Spec ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SkillSpec:
    """Declarative skill registration. One file per skill; each calls
    ``register(SkillSpec(...))`` at import time.

    The spec carries *everything* chat knows about a skill:
    planner-manifest text, dispatch routing, jurisdiction handling,
    follow-up behavior. This is what lets
    ``manifest_text()`` / ``entity_tools()`` / ``follow_up_capable()``
    be computed — no second source of truth to drift.
    """

    name: str
    """Canonical tool name. Must match what the planner emits and what
    handlers/tests reference. Lowercase, snake_case."""

    description: str
    """Short description used in the planner manifest. Keep to one
    sentence; the planner prompt is context-budgeted."""

    handler: Callable[[SkillCall], SkillEnvelope]
    """The function that actually runs the skill. Pure w.r.t. module
    state — everything it needs comes via ``SkillCall``."""

    inputs_schema: dict[str, Any] = field(default_factory=dict)
    """JSON schema for ``SkillCall.inputs``. Empty = no structured
    inputs; the handler reads from ``question`` / ``user_message``."""

    requires_jurisdiction: bool = False
    """False = this skill never receives active payer/state as a query
    qualifier (replaces the ``ENTITY_TOOLS`` set in tool_manifest.py).
    True = build_search_query merges jurisdiction into the tool input."""

    follow_up_capable: bool = False
    """True = chat can run this skill in a follow-up turn using context
    from the previous run (replaces ``FOLLOW_UP_CAPABLE``)."""

    supports_modes: tuple[str, ...] = ("copilot", "agentic", "quick")
    """Which chat_mode values can dispatch this skill. Restricts what
    the planner advertises per mode."""

    source: str = "builtin"
    """Where this skill came from. ``"builtin"`` = registered by a module
    under ``app.skills.builtin.*`` at chat import. ``"mcp"`` = registered
    by ``register_mcp_skills()`` from a remote MCP server's list_tools
    response. The distinction matters for the planner manifest: builtins
    occupy curated positions with hand-tuned descriptions; MCP tools
    auto-append to a dedicated section so adding a new MCP tool is a
    zero-code-change event on the chat side."""

    visible_to_planner: bool = True
    """When False, the skill is dispatchable by name (so programmatic
    callers / other skills can invoke it) but is NOT rendered into
    ``TOOL_MANIFEST`` — so the ReAct planner LLM never picks it. Use
    for internal-only tools, experimental MCP tools not ready for
    end-user-driven selection, or tools whose description is too weak
    to safely expose to the planner."""

    category: str = "general"
    """Human-facing category used for UI grouping and per-user tool policy.

    The tool-settings UI groups skills by this label so users can
    enable/disable whole themes (e.g. "web" = google_search + web_scrape).
    Known values (add more as needed — there's no closed enum):

      corpus     — curated corpus search + uploaded documents
      healthcare — payer / billing / clinical coding queries (CPT, HCPCS)
      npi        — NPPES provider registry lookups
      web        — live web search + scraping + URL ingestion
      analytics  — FL Medicaid BH market-data tools (get_top_orgs, etc.)
      documents  — document upload / download / management
      utility    — conversation helpers (vibe, transform_previous, cache)
      general    — catch-all for skills that don't map to a theme
    """

    display_name: str = ""
    """Optional user-friendly label for the tool settings UI.
    Falls back to ``name`` when empty."""


# ── Registry ──────────────────────────────────────────────────────────


_REGISTRY: dict[str, SkillSpec] = {}


def register(spec: SkillSpec) -> None:
    """Register a skill at import time. Raises if the name is already
    taken — we want loud failure, not silent shadowing. (Tests that
    need to override should use ``override()`` below.)"""
    if not spec.name or not spec.name.strip():
        raise ValueError("SkillSpec.name cannot be empty")
    if spec.name in _REGISTRY:
        existing = _REGISTRY[spec.name]
        if existing is spec:
            return  # re-import under the same module — tolerate
        raise ValueError(
            f"Skill {spec.name!r} already registered by "
            f"{existing.handler.__module__}.{existing.handler.__qualname__}; "
            f"would be shadowed by "
            f"{spec.handler.__module__}.{spec.handler.__qualname__}"
        )
    _REGISTRY[spec.name] = spec


def override(spec: SkillSpec) -> None:
    """Test-only hook. Replaces an existing registration. Never call
    this from production code — it's here so test fixtures can swap in
    mock handlers without tripping the duplicate-registration guard."""
    _REGISTRY[spec.name] = spec


def unregister(name: str) -> None:
    """Test-only teardown hook."""
    _REGISTRY.pop(name, None)


def has(name: str) -> bool:
    return name in _REGISTRY


def get(name: str) -> SkillSpec | None:
    return _REGISTRY.get(name)


def all_names() -> frozenset[str]:
    return frozenset(_REGISTRY.keys())


def dispatch(call: SkillCall) -> SkillEnvelope:
    """Run a registered skill. Wraps the handler with a single try/except
    so every skill gets uniform error shape; handlers that want to surface
    a structured failure should return ``SkillEnvelope`` with an error
    ``text`` and ``signal="no_sources"`` directly."""
    spec = _REGISTRY.get(call.name)
    if spec is None:
        return SkillEnvelope(
            text=f"Unknown skill: {call.name!r}.",
            signal="no_sources",
        )
    try:
        return spec.handler(call)
    except Exception as e:  # pragma: no cover — defensive; handlers own their errors
        logger.exception("skill %s handler failed: %s", call.name, e)
        return SkillEnvelope(
            text=f"I ran into an unexpected issue calling {call.name}. {e}. Please try again.",
            signal="no_sources",
        )


# ── Derived views (for planner + react_loop migration in commit 3) ───


def entity_tools() -> frozenset[str]:
    """Skills that DON'T take jurisdiction as a query qualifier.
    Equivalent to ``ENTITY_TOOLS`` in tool_manifest.py but derived."""
    return frozenset(s.name for s in _REGISTRY.values() if not s.requires_jurisdiction)


def names_by_source(source: str) -> frozenset[str]:
    """All registered skill names whose ``SkillSpec.source`` matches.

    Used by ``tool_manifest.py`` to render the "auto-discovered" MCP
    section separately from the curated builtin section."""
    return frozenset(s.name for s in _REGISTRY.values() if s.source == source)


def names_by_category(category: str) -> frozenset[str]:
    """All registered skill names in a given category."""
    return frozenset(s.name for s in _REGISTRY.values() if s.category == category)


def skills_catalog() -> list[dict]:
    """Return a list of skill metadata dicts for the UI tool-settings page.

    Each dict has: name, display_name, description (first line only),
    category, source, visible_to_planner. Sorted by category then name.
    Router-owned skills not in the registry (search_corpus,
    healthcare_npi_lookup, search_uploaded_document, refuse) are
    appended as synthetic entries so the UI sees a complete picture.
    """
    rows: list[dict] = []
    for spec in sorted(_REGISTRY.values(), key=lambda s: (s.category, s.name)):
        rows.append({
            "name": spec.name,
            "display_name": spec.display_name or spec.name.replace("_", " ").title(),
            "description": (spec.description or "").splitlines()[0].strip(),
            "category": spec.category,
            "source": spec.source,
            "visible_to_planner": spec.visible_to_planner,
        })
    # Append router-owned synthetic entries — these can't be SkillSpecs
    # because they dispatch directly in react_loop, not via answer_tool.
    # They still need to appear in the UI and be blockable via tool policy.
    _ROUTER_OWNED = [
        ("search_corpus", "corpus", "Search curated policy/billing corpus (hybrid BM25+vector)."),
        ("search_uploaded_document", "documents", "Search inside user-uploaded documents on this thread."),
        ("healthcare_npi_lookup", "npi", "Look up a provider by NPI number from the NPPES registry."),
        ("refuse", "utility", "Hard-stop tool used for PHI / clinical guardrails."),
    ]
    registered = {r["name"] for r in rows}
    for tool_name, cat, desc in _ROUTER_OWNED:
        if tool_name not in registered:
            rows.append({
                "name": tool_name,
                "display_name": tool_name.replace("_", " ").title(),
                "description": desc,
                "category": cat,
                "source": "builtin",
                "visible_to_planner": True,
            })
    return rows


def planner_visible_names() -> frozenset[str]:
    """All registered skill names whose ``SkillSpec.visible_to_planner``
    is True. Used when composing the planner manifest so experimental or
    internal-only skills are excluded even though they're dispatchable."""
    return frozenset(s.name for s in _REGISTRY.values() if s.visible_to_planner)


def follow_up_capable() -> frozenset[str]:
    return frozenset(s.name for s in _REGISTRY.values() if s.follow_up_capable)


def manifest_text(names: tuple[str, ...] | None = None) -> str:
    """Format registered skills as the prose block the planner LLM reads.

    Each skill renders as ``name(inputs)\\n  <description>\\n\\n`` —
    the same shape the legacy hand-maintained ``TOOL_MANIFEST`` used,
    so the planner prompt doesn't change behavior when we flip from
    hand-maintained to computed.

    ``names`` restricts output to a subset, in the given order. Useful
    because ``TOOL_MANIFEST`` in tool_manifest.py interleaves registry
    skills with non-registry tools (search_corpus, refuse,
    healthcare_npi_lookup, search_uploaded_document) — the caller
    decides ordering, we just emit the blocks we own.

    Descriptions are multi-line; ``SkillSpec.description`` carries the
    full "Use when / Do NOT use for / Returns" paragraph formerly
    hand-maintained in tool_manifest.py.
    """
    iter_names = names if names is not None else tuple(sorted(_REGISTRY.keys()))
    chunks: list[str] = []
    for name in iter_names:
        spec = _REGISTRY.get(name)
        if spec is None:
            continue
        # Signature: top-level keys in inputs_schema.properties, if any.
        props = (spec.inputs_schema or {}).get("properties") or {}
        required = set((spec.inputs_schema or {}).get("required") or [])
        if props:
            parts: list[str] = []
            for key in props.keys():
                parts.append(key if key in required else f"{key} optional")
            sig = "(" + ", ".join(parts) + ")"
        else:
            sig = "()"

        body_lines = [f"{name}{sig}"]
        for ln in (spec.description or "").strip().splitlines():
            body_lines.append(f"  {ln}" if ln.strip() else "")
        chunks.append("\n".join(body_lines))
    return "\n\n".join(chunks)


# ── Feature flag retired ──────────────────────────────────────────────
#
# ``MOBIUS_USE_SKILL_REGISTRY`` existed during commits 1+2 so the
# migration was rollback-safe. Commit 3 deleted the legacy
# ``if hint == "X"`` cascade in tool_agent.py; there's no second
# dispatch path to fall back to, so the flag has no meaning. Removed.
# ``registry_enabled()`` stays as a compatibility stub returning True
# so any caller that imported it doesn't break; commit 4+ will delete
# the stub too.


def registry_enabled() -> bool:
    """Back-compat stub. Was the migration flag; now always True.
    Remove once no caller references it."""
    return True


# Trigger skill registration. Each builtin file calls register() at
# import. We import here (not in __init__.py) so tool_agent.py only
# needs ``from app.skills import registry`` to pick everything up.
def _load_builtins() -> None:
    # Import side-effect: each module registers its skills.
    # Keep in alphabetical order; each entry is a single line so a
    # commit that adds a new builtin is a one-line diff.
    from app.skills.builtin import cached_answer  # noqa: F401
    from app.skills.builtin import corpus_search  # noqa: F401
    from app.skills.builtin import document_uploads  # noqa: F401
    from app.skills.builtin import fetch_document  # noqa: F401
    from app.skills.builtin import healthcare  # noqa: F401
    from app.skills.builtin import transform_previous  # noqa: F401
    from app.skills.builtin import vibe  # noqa: F401
    from app.skills.builtin import web  # noqa: F401
    from app.skills.builtin import web_search  # noqa: F401


_load_builtins()
