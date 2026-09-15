"""A green smoke probe is not proof that a deploy landed.

Cloud Run keeps the previous revision serving when a new one fails to start,
so every health check stays green while the code you deployed is not running.
Tool Manifest lost six consecutive revisions to this on 2026-09-15 and reported
stale behaviour as new-code behaviour; I spent part of the same day reading a
diagnostic off pre-fix code and concluding the fix had not worked.

The check asks the SERVING PROCESS which image it is. Not Cloud Run's config —
`gcloud run services describe` is PERMISSION_DENIED for this account, and the
inside-out question is better anyway: not "what did we ask for" but "what is
answering".
"""
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
DEPLOY = (ROOT / "scripts" / "deploy.sh").read_text()


def test_the_image_tag_is_passed_into_the_container():
    """A process that does not know its own build cannot be asked."""
    assert "MOBIUS_IMAGE_TAG=${IMAGE_TAG}" in DEPLOY


def test_the_serving_image_is_compared_and_a_mismatch_exits_nonzero():
    assert "/diag/build" in DEPLOY, "deploy never asks the process what it is"
    assert 'exit 73' in DEPLOY, "a mismatch must fail the deploy, not warn"


def test_the_check_runs_BEFORE_the_smoke():
    """🔴 Ordering is the point. Smoke after a failed landing passes against
    the OLD revision and reads as success."""
    i_verify = DEPLOY.index("Verifying the deploy landed")
    i_smoke = DEPLOY.index("Running post-deploy smoke")
    assert i_verify < i_smoke


def test_an_unreportable_image_says_CANNOT_VERIFY_rather_than_passing():
    """Absence of an answer is not an answer. An older revision predates the
    endpoint, so silence must read as 'cannot tell', never as 'fine'."""
    i = DEPLOY.index("/diag/build")
    window = DEPLOY[i:i + 1400]
    assert "CANNOT VERIFY" in window
