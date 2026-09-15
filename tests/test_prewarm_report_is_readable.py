"""The boot diagnostic must be readable without log access.

_prewarm_worker_caches() reports db_pool, react_prompt and toolreg_warm and
wrote them to logger.info ONLY. On 2026-09-14 Tool Manifest asked whether
toolreg_warm was succeeding on the deployed revision -- a question that report
answers -- and neither of us could answer it, because gcloud logging is
PERMISSION_DENIED for this account. The one place the answer lived was
unreachable from both sides of the question.
"""
import app.main as main


def test_prewarm_report_starts_as_not_run_not_as_empty_success():
    """`not_run` and `ran with no failures` must not look alike. An API-only
    process legitimately never runs the worker prewarm, and reporting that as
    a clean warm would be the could-not-check-as-checked-false defect."""
    assert isinstance(main._PREWARM_REPORT, dict)
    assert main._PREWARM_REPORT.get("state") in ("not_run", "ran")


def test_the_endpoint_serves_the_report_and_cannot_throw():
    """It reads a dict already in memory -- no DB, no MCP, no I/O."""
    assert main.diag_prewarm() is main._PREWARM_REPORT


def test_health_stays_a_cheap_liveness_probe():
    """🔴 DO NOT FOLD THIS INTO /health.

    /health is polled at 1Hz and a failure triggers a container kill, so it
    must not grow a surface that can throw or block. If someone moves the
    prewarm report into it, this fails and they should read the reason."""
    assert main.health() == {"status": "ok"}


def test_the_report_records_failures_separately_from_the_raw_parts():
    """The one fact anybody asks is 'did anything fail'. Making a caller
    scrape 'FAIL(' out of a joined string is how a diagnostic goes unread."""
    import inspect
    src = inspect.getsource(main._prewarm_worker_caches)
    assert '"failures"' in src, "the report must surface failures as their own key"
    assert '_PREWARM_REPORT = {' in src, "the report must be recorded, not only logged"
    # Recorded BEFORE logging, so a logging misconfiguration cannot take the
    # second channel down with the first.
    assert src.index("_PREWARM_REPORT = {") < src.index('"worker-prewarm: complete'), \
        "record the report before logging it — that ordering is the point"
