"""Tests for Step 3 roster merge context derived from thread state + credentialing envelope."""

from __future__ import annotations

from pathlib import Path

from app.pipeline.credentialing_envelope import resolve_step3_roster_merge_context


def test_roster_upload_members_migration_file_present():
    root = Path(__file__).resolve().parent.parent
    p = root / "db" / "schema" / "018_roster_upload_members.sql"
    assert p.is_file(), "Expected mobius-chat/db/schema/018_roster_upload_members.sql"
    body = p.read_text()
    assert "CREATE TABLE IF NOT EXISTS roster_upload_members" in body
    assert "idx_roster_upload_members_upload" in body


def test_prefer_outside_in_sets_external_only_and_disables_roster_merge():
    uid, ext, inc = resolve_step3_roster_merge_context(
        {"reconciliation_upload_id": "upload-1"},
        {"prefer_outside_in": True},
    )
    assert uid == "upload-1"
    assert ext is True
    assert inc is False


def test_default_includes_roster_when_upload_id_in_state():
    uid, ext, inc = resolve_step3_roster_merge_context(
        {"reconciliation_upload_id": "u-abc"},
        {},
    )
    assert uid == "u-abc"
    assert ext is False
    assert inc is True


def test_upload_id_from_latest_reconciliation_file():
    uid, ext, inc = resolve_step3_roster_merge_context(
        {
            "uploaded_files": [
                {
                    "purpose": "roster_reconciliation",
                    "upload_id": "from-file",
                    "org_id": "1234567893",
                },
            ],
        },
        {},
    )
    assert uid == "from-file"
    assert ext is False
    assert inc is True


def test_reconciliation_upload_id_overrides_uploaded_files_order():
    uid, _, _ = resolve_step3_roster_merge_context(
        {
            "reconciliation_upload_id": "pinned",
            "uploaded_files": [
                {"purpose": "roster_reconciliation", "upload_id": "other", "org_id": "1"},
            ],
        },
        {},
    )
    assert uid == "pinned"
