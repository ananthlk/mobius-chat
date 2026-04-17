"""Phase 3a — credentialing is now an optional module.

Proves the "ship Chat without credentialing" deployment path:

1. With ``CHAT_CREDENTIALING_ENABLED=false``, chat's ``app.main`` can be
   imported and constructs a FastAPI app that:
     - mounts history + feedback routers (the always-on surface), and
     - does NOT mount credentialing or roster routers.
2. Every /chat/credentialing-runs/*, /chat/roster-*, /chat/npi-lookup/*
   path returns 404 in the gated config — the FE sees "not found" and
   can degrade gracefully.
3. When the flag is set to true (or unset), the app mounts everything
   as before. Back-compat preserved.

The test process actually spawns a subprocess with the env var set
because ``app.main`` is imported at module load and the router-mount
decision is made there — we can't meaningfully toggle it inside a
single Python process.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap


def _run_with_env(env_value: str | None) -> dict:
    """Import app.main in a fresh subprocess with CHAT_CREDENTIALING_ENABLED
    set to ``env_value`` (or unset if None). Return a dict of
    {route_paths, credentialing_mounted} for assertion.
    """
    script = textwrap.dedent("""
        import json
        import sys
        from app.main import app
        paths = sorted({r.path for r in app.routes if hasattr(r, 'path')})
        # Paths owned by the two gated routers (credentialing + roster).
        # /chat/roster-upload is a separate main.py endpoint — NOT part of
        # the roster router, must be excluded from this filter.
        gated_prefixes = (
            '/chat/credentialing-runs',
            '/chat/roster-reconcile',
            '/chat/roster-truth',
            '/chat/roster-org',
            '/chat/npi-lookup',
        )
        credentialing_paths = [p for p in paths if any(p.startswith(pfx) for pfx in gated_prefixes)]
        history_paths = [p for p in paths if p.startswith('/chat/history')]
        # Feedback router owns 6 endpoints spread across several URL stems.
        feedback_fragments = ('/chat/feedback/', '/chat/source-feedback/', '/chat/adjudication-feedback/', '/chat/llm-performance-feedback/', '/chat/qc-audit/', '/chat/qc-user-score/')
        feedback_paths = [p for p in paths if any(frag in p for frag in feedback_fragments)]
        print(json.dumps({
            'total_paths': len(paths),
            'credentialing_mounted': len(credentialing_paths) > 0,
            'credentialing_path_count': len(credentialing_paths),
            'history_path_count': len(history_paths),
            'feedback_path_count': len(feedback_paths),
        }))
    """).strip()
    import os as _os

    env = dict(_os.environ)
    if env_value is None:
        env.pop("CHAT_CREDENTIALING_ENABLED", None)
    else:
        env["CHAT_CREDENTIALING_ENABLED"] = env_value

    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    assert result.returncode == 0, (
        f"subprocess failed with CHAT_CREDENTIALING_ENABLED={env_value!r}:\n"
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )
    import json as _json

    return _json.loads(result.stdout.strip().splitlines()[-1])


class TestCredentialingGateFlag:
    def test_flag_false_skips_credentialing_routers(self):
        """``CHAT_CREDENTIALING_ENABLED=false`` → no credentialing/roster routes registered."""
        info = _run_with_env("false")
        assert info["credentialing_mounted"] is False
        assert info["credentialing_path_count"] == 0
        # Non-credentialing routers still mounted
        assert info["history_path_count"] >= 4
        assert info["feedback_path_count"] >= 6

    def test_flag_true_mounts_credentialing(self):
        """Explicit true mounts everything."""
        info = _run_with_env("true")
        assert info["credentialing_mounted"] is True
        assert info["credentialing_path_count"] >= 20, (
            f"expected ≥20 credentialing/roster paths with flag on, "
            f"got {info['credentialing_path_count']}"
        )

    def test_flag_unset_is_backwards_compatible(self):
        """Unset flag defaults to enabled (back-compat for existing deploys)."""
        info = _run_with_env(None)
        assert info["credentialing_mounted"] is True

    def test_flag_various_false_values(self):
        """Accepts common falsy spellings."""
        for val in ("false", "False", "0", "no", "off", "FALSE"):
            info = _run_with_env(val)
            assert info["credentialing_mounted"] is False, (
                f"value {val!r} should disable credentialing"
            )

    def test_disabled_still_has_always_on_surface(self):
        """Even with credentialing off, the 10 always-on router URLs still resolve
        (4 history + 6 feedback/qc).

        Proves the gated deployment path has a meaningful Chat surface.
        """
        info = _run_with_env("false")
        assert info["history_path_count"] + info["feedback_path_count"] >= 10
