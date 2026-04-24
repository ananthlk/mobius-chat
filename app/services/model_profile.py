"""Model profiles — per-stage model pinning with runtime switchability.

Sprint 2 #0 (2026-04-24). Solves two concrete problems:

  1. Demo unpredictability — Thompson sampling occasionally picks
     slow/untested models for exploration, which makes live demos
     feel janky. A pinned profile eliminates that variance.

  2. Provider throttling — Groq's daily token quota has bitten us
     3+ times this session. A profile that excludes Groq entirely
     (or pins to Gemini/Anthropic only) sidesteps the problem
     structurally instead of waiting for the bandit's tpd_tracker
     to learn it after the first 429.

Contract
--------
The active profile name is resolved in priority order:

    1. Runtime override      — set via POST /chat/admin/model-profile
    2. Env var               — ``MOBIUS_MODEL_PROFILE`` at startup
    3. Fallback              — ``default`` (bandit active, nothing pinned)

Profiles themselves live in ``config/model_profiles.yaml``. Each
profile carries optional per-stage model pins + an optional
``fallback_model`` + an optional ``exclude_providers`` list. See the
YAML header for the full shape.

Pin resolution (``resolve_pinned_model(stage)``):

    * If profile pins this stage to a model present in MODEL_ROSTER
      → return that spec, meta carries ``model_profile`` and
      ``profile_pin=True``.
    * If the pinned model isn't in MODEL_ROSTER → fall back to
      ``profile.fallback_model`` if defined.
    * Otherwise → return None; caller (the bandit) handles selection.

Switching at runtime
--------------------
``set_active_profile(name)`` updates an in-process global. Takes
effect on the next ``select_model_for_stage`` call. Single-instance
dev (minScale=1) sees the change instantly. Multi-instance would
need Postgres-backed state — deferred until we split the worker
service in Sprint 2 #2.
"""
from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# ── Module-level state ────────────────────────────────────────────────

# The loaded YAML dict. Populated on first access via _load().
_PROFILES: dict[str, dict[str, Any]] | None = None
_LOAD_LOCK = threading.Lock()

# Runtime-overridable active profile. None = use env var / default.
_ACTIVE_PROFILE_OVERRIDE: str | None = None
_OVERRIDE_LOCK = threading.Lock()


# ── Loader ────────────────────────────────────────────────────────────


def _config_path() -> Path:
    """Resolve the YAML path. Env ``MOBIUS_MODEL_PROFILE_FILE`` overrides
    for tests; default is ``<repo>/config/model_profiles.yaml``."""
    override = (os.environ.get("MOBIUS_MODEL_PROFILE_FILE") or "").strip()
    if override:
        return Path(override)
    # __file__ = app/services/model_profile.py → chat root is 2 levels up
    return Path(__file__).resolve().parents[2] / "config" / "model_profiles.yaml"


def _load() -> dict[str, dict[str, Any]]:
    """Load + cache the YAML. Safe to call repeatedly — loads once."""
    global _PROFILES
    if _PROFILES is not None:
        return _PROFILES
    with _LOAD_LOCK:
        if _PROFILES is not None:  # another thread beat us to it
            return _PROFILES
        path = _config_path()
        if not path.exists():
            logger.warning(
                "model_profile: config not found at %s; all profiles empty", path,
            )
            _PROFILES = {"default": {}}
            return _PROFILES
        try:
            import yaml
            with open(path, "r") as f:
                data = yaml.safe_load(f) or {}
            profiles = (data.get("profiles") or {}) if isinstance(data, dict) else {}
            if not isinstance(profiles, dict):
                logger.warning("model_profile: 'profiles' not a dict; using default only")
                profiles = {}
            # Always include a ``default`` entry even if the file omits it.
            profiles.setdefault("default", {})
            _PROFILES = profiles
            logger.info(
                "model_profile: loaded %d profile(s) from %s",
                len(profiles), path,
            )
            return _PROFILES
        except Exception as exc:
            logger.exception("model_profile: load failed; falling back to default-only: %s", exc)
            _PROFILES = {"default": {}}
            return _PROFILES


def _reset_for_tests() -> None:
    """Test hook — clears the cache so a new YAML path takes effect."""
    global _PROFILES, _ACTIVE_PROFILE_OVERRIDE
    with _LOAD_LOCK:
        _PROFILES = None
    with _OVERRIDE_LOCK:
        _ACTIVE_PROFILE_OVERRIDE = None


# ── Active profile resolution ─────────────────────────────────────────


def get_active_profile_name() -> str:
    """Current active profile name. Runtime override wins; then env;
    then ``default``."""
    with _OVERRIDE_LOCK:
        override = _ACTIVE_PROFILE_OVERRIDE
    if override:
        return override
    env = (os.environ.get("MOBIUS_MODEL_PROFILE") or "").strip()
    return env or "default"


def set_active_profile(name: str | None) -> dict[str, Any]:
    """Runtime switch. Pass ``None`` to clear the override and
    revert to the env-var / default resolution.

    Validates that the profile exists in the loaded YAML and returns
    a status dict suitable for the admin endpoint response. Raises
    ValueError on unknown profile name (admin endpoint surfaces as 400).
    """
    profiles = _load()
    if name is not None and name not in profiles:
        known = sorted(profiles.keys())
        raise ValueError(
            f"Unknown model profile {name!r}. Known profiles: {known}"
        )
    global _ACTIVE_PROFILE_OVERRIDE
    with _OVERRIDE_LOCK:
        _ACTIVE_PROFILE_OVERRIDE = name
    active = get_active_profile_name()
    logger.info("model_profile: active profile set to %r", active)
    return {
        "active_profile": active,
        "override_set": _ACTIVE_PROFILE_OVERRIDE is not None,
        "available_profiles": sorted(profiles.keys()),
    }


def get_active_profile() -> dict[str, Any]:
    """Return the active profile's config dict (possibly empty)."""
    profiles = _load()
    name = get_active_profile_name()
    return profiles.get(name) or {}


# ── Pin resolution ────────────────────────────────────────────────────


def pinned_model_for_stage(stage: str) -> str | None:
    """Return the model name pinned for ``stage`` by the active profile,
    or ``None`` when no pin applies."""
    profile = get_active_profile()
    # Stage pins live at the top level of the profile. Non-pin keys
    # (fallback_model, exclude_providers, etc.) are filtered.
    pin = profile.get(stage)
    if isinstance(pin, str) and pin.strip():
        return pin.strip()
    return None


def fallback_model_name() -> str | None:
    profile = get_active_profile()
    fb = profile.get("fallback_model")
    return fb.strip() if isinstance(fb, str) and fb.strip() else None


def excluded_providers() -> frozenset[str]:
    """Providers the active profile wants removed from the bandit pool.
    Applies even when no per-stage pin is set."""
    profile = get_active_profile()
    raw = profile.get("exclude_providers") or []
    if not isinstance(raw, list):
        return frozenset()
    return frozenset({str(p).strip().lower() for p in raw if str(p).strip()})


# ── Public entry-point used by model_registry ────────────────────────


def resolve_pinned_model(
    stage: str,
    *,
    phi_detected: bool = False,
) -> tuple[Any, dict[str, Any]]:
    """Return ``(spec, meta)`` when the active profile pins this stage,
    ``(None, meta)`` when no pin applies — lets model_registry fall
    through to the bandit.

    ``spec`` is a ``ModelSpec`` (imported lazily to avoid circular
    imports at module load). ``meta`` carries diagnostic fields that
    get merged into the router's meta return value so operators can
    see in ``llm_calls`` which turns were bandit-picked vs pinned.

    When ``phi_detected=True`` the pinned/fallback model must be HIPAA-
    eligible; otherwise we return (None, ...) and let the bandit pick
    from the HIPAA-safe pool. Better to route a PHI turn through a
    correct-but-not-pinned model than to leak PHI to a non-eligible one.
    """
    profile_name = get_active_profile_name()
    meta: dict[str, Any] = {"model_profile": profile_name}

    pin = pinned_model_for_stage(stage)
    if not pin:
        return None, meta

    # Look up the pinned model in MODEL_ROSTER. Lazy import because
    # model_registry imports us on its own init path eventually.
    from app.services.model_registry import MODEL_ROSTER

    spec = MODEL_ROSTER.get(pin)
    if spec is None:
        logger.warning(
            "model_profile: profile=%r pins stage=%r to unknown model=%r; trying fallback",
            profile_name, stage, pin,
        )
        fb = fallback_model_name()
        if fb:
            fb_spec = MODEL_ROSTER.get(fb)
            if fb_spec is not None:
                meta["profile_pin"] = False
                meta["profile_pin_attempted"] = pin
                meta["profile_fallback_used"] = fb
                if phi_detected and not getattr(fb_spec, "hipaa_eligible", False):
                    # PHI safety net — fallback must also be HIPAA-safe.
                    meta["profile_phi_fallback_unsafe"] = True
                    return None, meta
                return fb_spec, meta
        meta["profile_pin_missing"] = pin
        return None, meta

    # Pinned model exists.
    if phi_detected and not getattr(spec, "hipaa_eligible", False):
        # Route PHI-detected turns through the bandit instead of a
        # non-HIPAA-eligible pinned model. Correctness > predictability.
        meta["profile_phi_skip_pin"] = pin
        return None, meta

    meta["profile_pin"] = True
    meta["profile_pinned_model"] = pin
    return spec, meta
