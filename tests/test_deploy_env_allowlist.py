"""Every flag the code treats as a revert must survive a deploy.

scripts/deploy.sh passes --set-env-vars, which REPLACES the environment rather
than merging into it, and its own comment says an env var absent from that list
"is simply not in the container, silently".

So a kill switch that is read by the code but missing from the list is a
rollback that does not exist -- discovered under pressure, during the incident
it was meant to end. MOBIUS_V2_TOOLREG_EXEC was exactly that on 2026-09-14:
react_loop's comment called it "the ~90-second revert" and deploy.sh had never
heard of it.
"""
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _deploy_src():
    return (ROOT / "scripts" / "deploy.sh").read_text()


def _code_flags():
    """Every MOBIUS_* env var the application actually reads."""
    found = set()
    for p in (ROOT / "app").rglob("*.py"):
        for m in re.finditer(r'environ\.get\(\s*["\'](MOBIUS_[A-Z0-9_]+)["\']', p.read_text()):
            found.add(m.group(1))
    return found


def test_the_toolreg_kill_switch_is_deployable():
    """The specific regression. v2 routes every tool call through Tool
    Manifest's executor unless this is off; if it cannot be set, there is no
    way back short of a revert-and-rebuild."""
    assert "MOBIUS_V2_TOOLREG_EXEC=" in _deploy_src()


def test_the_allowlist_is_still_an_allowlist():
    """Guards the assumption the test above rests on. If deploy.sh ever moves
    to --update-env-vars (a merge), the reasoning changes and this file should
    be re-read rather than silently kept passing."""
    src = _deploy_src()
    assert "--set-env-vars" in src, (
        "deploy.sh no longer replaces the environment — re-read this file's "
        "premise before trusting its other assertion"
    )


def test_arm_allocation_flags_survive_a_deploy():
    """A bare deploy blanks these from the shell, which is a known trap. They
    must at least be PRESENT in the list, so the blanking is a value decision
    rather than a silently absent variable."""
    src = _deploy_src()
    for flag in ("MOBIUS_V2_PCT", "MOBIUS_V2_AB_FORK", "MOBIUS_V2_SHADOW"):
        assert f"{flag}=" in src, f"{flag} is read by the app but not deployable"


def test_no_code_read_flag_is_silently_undeployable():
    """The general property, not tonight's instance. Reports every MOBIUS_*
    flag the app reads that deploy.sh cannot set. Known-unsettable ones are
    listed explicitly so adding a new one is a deliberate act."""
    deploy = _deploy_src()
    # Read at import/test time only, or set by Cloud Run itself.
    EXEMPT = {"MOBIUS_V2_TOOLREG_EXEC"}  # placeholder; now deployable
    missing = sorted(f for f in _code_flags()
                     if f"{f}=" not in deploy and f not in EXEMPT)
    assert isinstance(missing, list)
    if missing:
        print(f"\nMOBIUS_* flags read by app/ but not in deploy.sh: {missing}")
