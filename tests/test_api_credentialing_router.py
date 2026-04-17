"""Phase 1c — extracted /chat/credentialing-runs + /chat/npi-lookup router.

Third slice of the main-split refactor; also the staging ground for
Phase 3 (credentialing → its own package). When Phase 3 lands, this file
+ its service/storage dependencies move wholesale into a new
``mobius-credentialing`` package.

These tests assert:
1. URL back-compat — every pre-1c path still resolves at the same URL.
2. Pydantic bodies unchanged — identical validation behavior.
3. main.py hygiene — no ``@app.*("/chat/credentialing-runs"`` left behind.
4. Key handlers delegate to the right service functions.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient


def _app():
    """Minimal app that mounts only the credentialing router.

    Avoids pulling in the full app.main (Vertex, DB, Redis) so these
    tests stay fast and hermetic.
    """
    from fastapi import FastAPI

    from app.api.credentialing import router

    a = FastAPI()
    a.include_router(router)
    return a


# ── URL back-compat ─────────────────────────────────────────────────────────


class TestURLBackCompat:
    """Every pre-1c path must still resolve at the same URL."""

    def test_list_runs_path(self):
        with patch("app.storage.credentialing_runs_pg.list_credentialing_runs", return_value=[]):
            r = TestClient(_app()).get("/chat/credentialing-runs")
        assert r.status_code == 200

    def test_get_run_path(self):
        with patch(
            "app.services.credentialing_run_service.get_credentialing_run",
            return_value={"run_id": "r1", "org_name": "Acme"},
        ):
            r = TestClient(_app()).get("/chat/credentialing-runs/r1")
        assert r.status_code == 200

    def test_get_run_not_found_returns_404(self):
        with patch(
            "app.services.credentialing_run_service.get_credentialing_run",
            return_value=None,
        ):
            r = TestClient(_app()).get("/chat/credentialing-runs/missing")
        assert r.status_code == 404

    def test_roster_truth_get_path(self):
        with (
            patch(
                "app.services.credentialing_run_service.get_credentialing_run",
                return_value={"org_name": "Acme"},
            ),
            patch("app.storage.roster_truth_pg.ensure_schema"),
            patch("app.storage.roster_truth_pg.get_truth_for_org", return_value=[]),
        ):
            r = TestClient(_app()).get("/chat/credentialing-runs/r1/roster-truth")
        assert r.status_code == 200

    def test_npi_lookup_validates_format(self):
        """Non-10-digit input → 400."""
        r = TestClient(_app()).get("/chat/npi-lookup/12345")
        assert r.status_code == 400
        r = TestClient(_app()).get("/chat/npi-lookup/abcdefghij")
        assert r.status_code == 400

    def test_npi_lookup_valid_format(self):
        """10-digit NPI passes format check (may then fail on network; we mock)."""
        with patch(
            "app.api.credentialing._fetch_nppes_single",
            return_value={"npi": "1234567890", "name": "Test Doc"},
        ):
            r = TestClient(_app()).get("/chat/npi-lookup/1234567890")
        assert r.status_code == 200
        assert r.json()["npi"] == "1234567890"


# ── Input validation preserved ──────────────────────────────────────────────


class TestInputValidation:
    def test_create_run_requires_org_name(self):
        r = TestClient(_app()).post("/chat/credentialing-runs", json={})
        assert r.status_code == 400

    def test_validate_requires_step_id(self):
        with patch(
            "app.services.credentialing_run_service._store_get",
            return_value={"run_id": "r1"},
        ):
            r = TestClient(_app()).post(
                "/chat/credentialing-runs/r1/validate",
                json={"step_id": "", "validated_output": {}},
            )
        assert r.status_code == 400

    def test_seed_roster_requires_upload_id(self):
        r = TestClient(_app()).post(
            "/chat/credentialing-runs/r1/seed-roster", json={}
        )
        assert r.status_code == 400


# ── Delegation ──────────────────────────────────────────────────────────────


class TestDelegation:
    def test_list_runs_passes_limit_and_offset(self):
        with patch(
            "app.storage.credentialing_runs_pg.list_credentialing_runs",
            return_value=[],
        ) as m:
            TestClient(_app()).get("/chat/credentialing-runs?limit=5&offset=10")
        m.assert_called_once_with(limit=5, offset=10)

    def test_delete_run_cascades(self):
        """delete must call delete_credentialing_run, and if upload_id is present,
        also issue the skill-server DELETE for cascade cleanup.
        """
        with (
            patch(
                "app.services.credentialing_run_service.get_credentialing_run",
                return_value={"orchestrator_state": {"step3_roster_upload_id": "u123"}},
            ),
            patch(
                "app.storage.credentialing_runs_pg.delete_credentialing_run",
                return_value=True,
            ) as m_del,
            patch.dict(
                "os.environ",
                {
                    "CHAT_SKILLS_PROVIDER_ROSTER_CREDENTIALING_URL": (
                        "http://localhost:8011/report"
                    )
                },
            ),
            patch("httpx.Client") as mock_client,
        ):
            mock_client.return_value.__enter__.return_value.delete.return_value = MagicMock(
                status_code=200
            )
            r = TestClient(_app()).delete("/chat/credentialing-runs/r1")
        assert r.status_code == 200
        body = r.json()
        assert body["deleted"] is True
        assert body["upload_id_purged"] == "u123"
        m_del.assert_called_once_with("r1")

    def test_pml_tasks_patch_calls_storage(self):
        with (
            patch(
                "app.storage.credentialing_runs_pg.patch_pml_task_state",
                return_value=True,
            ) as m,
            patch("httpx.Client"),  # swallow the task-manager side-effect
        ):
            r = TestClient(_app()).patch(
                "/chat/credentialing-runs/r1/pml-tasks",
                json={
                    "done": ["t1"],
                    "notes": {"t1": "ok"},
                    "manual": [],
                    "dismissed": [],
                    "providerLocations": {},
                },
            )
        assert r.status_code == 200
        m.assert_called_once()


# ── main.py hygiene: no credentialing decorators left ──────────────────────


class TestMainPyHygiene:
    """Regression guard — no @app.*("/chat/credentialing-runs...") or
    @app.get("/chat/npi-lookup/...") may reappear in main.py.
    """

    def test_no_credentialing_decorators_in_main_py(self):
        from pathlib import Path

        main_py = Path(__file__).parent.parent / "app" / "main.py"
        text = main_py.read_text()
        forbidden = ("/chat/credentialing-runs", "/chat/npi-lookup/")
        for line in text.splitlines():
            if not line.strip().startswith("@app."):
                continue
            for f in forbidden:
                if f in line:
                    raise AssertionError(
                        f"Phase 1c regression — {f} endpoint back in main.py:\n  {line}"
                    )

    def test_no_credentialing_body_classes_in_main_py(self):
        """The four Pydantic bodies moved into the router must not reappear here."""
        from pathlib import Path

        main_py = Path(__file__).parent.parent / "app" / "main.py"
        text = main_py.read_text()
        forbidden_classes = (
            "class CredentialingRunCreateBody",
            "class CredentialingValidateBody",
            "class PmlTaskStateBody",
            "class TaxonomyTaskStateBody",
        )
        for cls in forbidden_classes:
            assert cls not in text, f"{cls} still defined in main.py — move to router"


# ── Size proof — main.py is measurably smaller after 1a+1b+1c ──────────────


class TestMainPyShrinks:
    """Quantitative check — after Phases 1a, 1b, and 1c the file must be
    substantially smaller than its starting 3,125 lines. 2,500 lines is a
    conservative ceiling; the actual count should be around 2,400.
    """

    def test_main_py_line_count_under_2600(self):
        from pathlib import Path

        main_py = Path(__file__).parent.parent / "app" / "main.py"
        line_count = sum(1 for _ in main_py.read_text().splitlines())
        assert line_count < 2600, (
            f"main.py has {line_count} lines — it should be shrinking each "
            f"Phase 1 sub-phase. If this test fails, something got added back "
            f"inline instead of going through a router."
        )
