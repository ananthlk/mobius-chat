"""Phase 1g — CI baseline guard.

Ensures the GitHub Actions workflow stays in place and continues to run
the phase-regression test subset. This is meta-testing: if someone
deletes `.github/workflows/ci.yml` or drops a critical test file from
its run list, these checks fail at the next invocation.

Rationale: CI is a load-bearing process guarantee. Without it, every
merge relies on the author running tests locally — which is exactly
the pattern that let pre-Phase-0.18 regressions slip through. Lock
the contract the same way we locked the hygiene guard.
"""

from __future__ import annotations

from pathlib import Path

import pytest


CHAT_REPO_ROOT = Path(__file__).parent.parent
WORKFLOW_PATH = CHAT_REPO_ROOT / ".github" / "workflows" / "ci.yml"


@pytest.fixture(scope="module")
def workflow_text() -> str:
    assert WORKFLOW_PATH.exists(), (
        f"CI workflow missing at {WORKFLOW_PATH.relative_to(CHAT_REPO_ROOT)}. "
        f"Phase 1g introduced this file; deleting it removes the only "
        f"automated gate on this repo."
    )
    return WORKFLOW_PATH.read_text()


class TestWorkflowStructure:
    def test_workflow_file_exists(self, workflow_text: str) -> None:
        # The fixture itself asserts existence; this test makes the
        # expectation explicit in the report.
        assert workflow_text.strip(), "ci.yml exists but is empty"

    def test_triggers_on_push_and_pr(self, workflow_text: str) -> None:
        """CI should run on both pushes to the integration branches AND on
        every PR, so a gatekeeping PR review can't bypass tests.
        """
        assert "on:" in workflow_text
        assert "push:" in workflow_text
        assert "pull_request:" in workflow_text

    def test_uses_pinned_python_version(self, workflow_text: str) -> None:
        """Unpinned Python (`python-version: '3'`) invites silent interpreter
        bumps that change behavior — pin the minor at least.
        """
        assert "python-version" in workflow_text
        # Accept either '3.11' or a future '3.12'/'3.13' pin — just not a
        # bare '3' or missing pin.
        assert 'python-version: "3.1' in workflow_text or "python-version: '3.1" in workflow_text, (
            "ci.yml must pin a minor Python version (e.g. '3.11'), "
            "not a bare '3' major."
        )

    def test_has_concurrency_cancel(self, workflow_text: str) -> None:
        """Without cancel-in-progress, a rapid push sequence stacks CI
        runs and burns Actions minutes. Phase 1g sets this; don't let
        it regress.
        """
        assert "cancel-in-progress: true" in workflow_text


class TestPhaseRegressionSubsetInCI:
    """Every phase-regression test file must be run by CI. If a new
    regression test lands without being wired into the workflow, this
    check fails — forcing the author to either add it to CI or
    explicitly opt out here with a comment.
    """

    # Test files that MUST be executed by ci.yml. Extend this list when
    # a new phase lands with its own regression file.
    CRITICAL_TEST_FILES: tuple[str, ...] = (
        "tests/test_api_hygiene_guard.py",
        "tests/test_api_tasks_router.py",
        "tests/test_react_retry_guard.py",
        "tests/test_react_retry_guard_exhaustion.py",
        "tests/test_rag_api_chunk_filter.py",
        "tests/test_scrape_timeout_and_repair_removed.py",
        "tests/test_ci_baseline.py",
        "tests/test_front_door.py",  # Phase 1h
        "tests/test_instant_rag_search.py",  # Phase B.1
        "tests/test_composer_attach.py",  # Phase B.1a
        "tests/test_parallel_retrieval.py",  # Phase B.4
        "tests/test_tpd_tracker.py",  # Phase 2.5b
        "tests/test_instant_rag_catalog.py",  # Phase B.1c
    )

    def test_every_critical_test_is_invoked(self, workflow_text: str) -> None:
        missing: list[str] = []
        for path in self.CRITICAL_TEST_FILES:
            if path not in workflow_text:
                missing.append(path)
        assert not missing, (
            "Phase-regression tests not wired into CI:\n"
            + "\n".join(f"  - {p}" for p in missing)
            + "\n\nAdd them to the `pytest -v ...` step in "
              ".github/workflows/ci.yml, or explicitly remove them from "
              "CRITICAL_TEST_FILES here with a justification comment."
        )

    def test_every_critical_test_file_exists_on_disk(self) -> None:
        """Double-check: the files we expect CI to run must actually be on
        disk. A stale CRITICAL_TEST_FILES entry would cause a green CI run
        against a phantom file.
        """
        missing = [
            p for p in self.CRITICAL_TEST_FILES
            if not (CHAT_REPO_ROOT / p).exists()
        ]
        assert not missing, (
            "CRITICAL_TEST_FILES references files that don't exist: "
            f"{missing}. Either restore them or remove from the tuple."
        )


class TestWorkflowDoesNotSkipSiblingInstallBlind:
    """The baseline deliberately skips `pip install -r requirements.txt`
    because it contains `-e ../mobius-retriever` (a local-path dep).
    Document that skip so a future contributor doesn't mistake it for
    an oversight and add requirements.txt blindly — which would fail CI
    at install time and rot the gate.
    """

    def test_requirements_txt_not_installed_verbatim(self, workflow_text: str) -> None:
        # Look only at executable (non-comment) lines. Matching anywhere in
        # the file trips on prose comments that *explain* why we don't do it.
        for raw_line in workflow_text.splitlines():
            line = raw_line.strip()
            if line.startswith("#") or not line:
                continue
            assert "pip install -r requirements.txt" not in line, (
                "ci.yml has an executable step running "
                "`pip install -r requirements.txt`. This includes "
                "`-e ../mobius-retriever` which isn't present in the CI "
                "checkout — install will fail. Either vendor the deps "
                "explicitly (current baseline) or add a sibling-checkout "
                "step first."
            )

    def test_install_step_names_deps_explicitly(self, workflow_text: str) -> None:
        """The install step must explicitly list the minimal deps so it's
        obvious what CI is testing against."""
        # At least these four must be named explicitly — they're what the
        # phase-regression subset actually imports.
        for pkg in ("fastapi", "pytest", "httpx", "pydantic"):
            assert pkg in workflow_text, (
                f"'{pkg}' not mentioned in ci.yml install step. The baseline "
                f"installs deps explicitly; don't drop them silently."
            )
