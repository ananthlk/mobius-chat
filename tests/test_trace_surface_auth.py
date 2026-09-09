"""The trace surface is admin-gated — page AND data."""
from unittest.mock import patch
import pytest
from fastapi import HTTPException


class TestTraceSurfaceIsGated:
    """Gating the page alone would be theatre: it is a thin client over the
    three endpoints, so the gate has to sit on the data."""

    def _call(self, fn, *a):
        from app.api import chat as chat_api
        return getattr(chat_api, fn)(*a)

    @pytest.mark.parametrize("fn,args", [
        ("get_node_rollup", ()),
        ("list_turn_traces", (40,)),
    ])
    def test_endpoint_404s_when_admin_disabled(self, fn, args):
        with patch("app.api.admin._admin_enabled", return_value=False):
            with pytest.raises(HTTPException) as e:
                self._call(fn, *args)
        assert e.value.status_code == 404, (
            "404 not 403 — an unauthenticated caller should not learn that an "
            "admin telemetry surface exists here"
        )

    def test_endpoints_reachable_when_admin_enabled(self):
        from app.api import chat as chat_api
        with patch("app.api.admin._admin_enabled", return_value=True), \
             patch("app.storage.turn_spans.read_spans", return_value=[]):
            out = chat_api.get_turn_spans("cid-x")
        assert out["correlation_id"] == "cid-x"

    def test_PER_TURN_spans_are_NOT_admin_env_gated(self):
        """Its consumer is the diagnostics tab, whose visibility gate is
        getShowLlmPerformance() (app.ts:5459) — a client-side check on the
        user's activities, NOT MOBIUS_ADMIN_ENABLED.

        Gating this with the admin env flag would use a different definition
        of "who may see this" than the surface consuming it, and the panel
        would render "unavailable" to a user who legitimately has the
        diagnostics activity. A gate that disagrees with its caller is the
        defect class this program exists to remove.
        """
        from app.api import chat as chat_api
        with patch("app.api.admin._admin_enabled", return_value=False), \
             patch("app.storage.turn_spans.read_spans", return_value=[]):
            out = chat_api.get_turn_spans("cid-x")
        assert out["correlation_id"] == "cid-x", (
            "per-turn spans must stay reachable for the diagnostics tab"
        )
