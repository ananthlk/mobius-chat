"""Unit tests for the PHI message gate in app/api/chat.py.

These tests focus on _phi_check_message() fail-closed behaviour — no
live network calls, no DB writes. The gate logic is simple enough that
mocking httpx is sufficient coverage.
"""
import pytest
from unittest.mock import MagicMock, patch


def _import_phi_check():
    from app.api.chat import _phi_check_message
    return _phi_check_message


class TestPhiCheckMessageFailClosed:
    """_phi_check_message must fail-closed on any check error."""

    def test_clean_message_returns_block_false(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "block": False, "gate": "clean", "phi_flag": False,
            "identifier_labels": [], "phi_evidence": [],
            "classifier_version": "test@0.0",
        }
        with patch("httpx.Client") as mock_client_cls:
            mock_client_cls.return_value.__enter__.return_value.post.return_value = mock_resp
            result = _import_phi_check()("hello world")
        assert result["block"] is False
        assert result["gate"] == "clean"

    def test_phi_message_returns_block_true(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "block": True, "gate": "phi", "phi_flag": True,
            "identifier_labels": ["Name"], "phi_evidence": [],
            "classifier_version": "test@0.0",
        }
        with patch("httpx.Client") as mock_client_cls:
            mock_client_cls.return_value.__enter__.return_value.post.return_value = mock_resp
            result = _import_phi_check()("patient Jane Doe")
        assert result["block"] is True
        assert result["gate"] == "phi"

    def test_network_error_fails_closed(self):
        """Connection refused / timeout must block, not pass."""
        import httpx
        with patch("httpx.Client") as mock_client_cls:
            mock_client_cls.return_value.__enter__.return_value.post.side_effect = (
                httpx.ConnectError("connection refused")
            )
            result = _import_phi_check()("any message")
        assert result["block"] is True, "Network error must fail-closed"
        assert result["gate"] == "indeterminate"
        assert result.get("_check_error") is True

    def test_timeout_fails_closed(self):
        import httpx
        with patch("httpx.Client") as mock_client_cls:
            mock_client_cls.return_value.__enter__.return_value.post.side_effect = (
                httpx.TimeoutException("timed out")
            )
            result = _import_phi_check()("any message")
        assert result["block"] is True, "Timeout must fail-closed"
        assert result["gate"] == "indeterminate"

    def test_non_200_response_fails_closed(self):
        """5xx from the classifier must block, not pass."""
        mock_resp = MagicMock()
        mock_resp.status_code = 503
        with patch("httpx.Client") as mock_client_cls:
            mock_client_cls.return_value.__enter__.return_value.post.return_value = mock_resp
            result = _import_phi_check()("any message")
        assert result["block"] is True, "Non-200 must fail-closed"
        assert result["gate"] == "indeterminate"

    def test_generic_exception_fails_closed(self):
        with patch("httpx.Client") as mock_client_cls:
            mock_client_cls.return_value.__enter__.return_value.post.side_effect = (
                RuntimeError("unexpected error")
            )
            result = _import_phi_check()("any message")
        assert result["block"] is True, "Any exception must fail-closed"
        assert result["gate"] == "indeterminate"
