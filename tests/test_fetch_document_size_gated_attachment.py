"""Task #106 (2026-08-16, Ananth, directly): fetch_document attaches a
single confidently-matched document's actual bytes (size-permitting)
instead of only a filename+download-link, and always opts out of the
golden auto-finalize (resolving WHICH document matches isn't the same as
having its content -- see react_loop.py's golden-inference comment)."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import app.skills.builtin.fetch_document as fd
from app.skills.registry import SkillCall

_SINGLE_ROW = [
    {
        "document_id": "11111111-1111-1111-1111-111111111111",
        "document_display_name": "59G-4.087 Evaluation and Management Services",
        "document_filename": "59G-4.087.pdf",
        "document_payer": "AHCA",
        "document_state": "FL",
        "document_program": "Medicaid",
        "document_authority_level": "regulation",
        "updated_at": "2026-01-01T00:00:00Z",
    },
]

_TWO_ROWS = _SINGLE_ROW + [
    {
        "document_id": "22222222-2222-2222-2222-222222222222",
        "document_display_name": "59G-4.087 Prior Version",
        "document_filename": "59G-4.087-old.pdf",
        "document_payer": "AHCA",
        "document_state": "FL",
        "document_program": "Medicaid",
        "document_authority_level": "regulation",
        "updated_at": "2025-01-01T00:00:00Z",
    },
]


def _call(query: str, ctx=None) -> SkillCall:
    return SkillCall(name="fetch_document", inputs={"query": query}, question=query, pipeline_ctx=ctx)


class TestGoldenOptOut:
    def test_single_match_always_opts_out_of_golden(self, monkeypatch):
        monkeypatch.setattr(fd, "_fetch_candidates", lambda q: list(_SINGLE_ROW))
        monkeypatch.setattr(fd, "_maybe_fetch_attachment", lambda doc_id, filename: None)
        env = fd._run_fetch_document(_call("59G-4.087 evaluation management", SimpleNamespace()))
        assert env.extra["golden"] is False

    def test_multi_match_also_opts_out_of_golden(self, monkeypatch):
        monkeypatch.setattr(fd, "_fetch_candidates", lambda q: list(_TWO_ROWS))
        env = fd._run_fetch_document(_call("59G-4.087", SimpleNamespace()))
        assert env.extra["golden"] is False


class TestAttachmentWiring:
    def test_single_match_attaches_when_fetch_succeeds(self, monkeypatch):
        monkeypatch.setattr(fd, "_fetch_candidates", lambda q: list(_SINGLE_ROW))
        fake_attachment = {"mime_type": "application/pdf", "data_b64": "ZmFrZQ==", "filename": "59G-4.087.pdf"}
        mock_fetch = MagicMock(return_value=fake_attachment)
        monkeypatch.setattr(fd, "_maybe_fetch_attachment", mock_fetch)

        env = fd._run_fetch_document(_call("59G-4.087 evaluation management", SimpleNamespace()))

        assert env.extra["attachment"] == fake_attachment
        mock_fetch.assert_called_once()
        called_doc_id = mock_fetch.call_args[0][0]
        assert called_doc_id == _SINGLE_ROW[0]["document_id"]

    def test_single_match_no_attachment_key_when_fetch_fails(self, monkeypatch):
        monkeypatch.setattr(fd, "_fetch_candidates", lambda q: list(_SINGLE_ROW))
        monkeypatch.setattr(fd, "_maybe_fetch_attachment", lambda doc_id, filename: None)
        env = fd._run_fetch_document(_call("59G-4.087 evaluation management", SimpleNamespace()))
        assert "attachment" not in env.extra

    def test_multi_match_never_attempts_attachment(self, monkeypatch):
        monkeypatch.setattr(fd, "_fetch_candidates", lambda q: list(_TWO_ROWS))
        mock_fetch = MagicMock(return_value=None)
        monkeypatch.setattr(fd, "_maybe_fetch_attachment", mock_fetch)
        env = fd._run_fetch_document(_call("59G-4.087", SimpleNamespace()))
        mock_fetch.assert_not_called()
        assert "attachment" not in env.extra

    def test_attachment_attempt_exception_degrades_gracefully(self, monkeypatch):
        """A raised exception inside the attachment attempt must never
        break the turn -- degrade to the download-card-only behavior."""
        monkeypatch.setattr(fd, "_fetch_candidates", lambda q: list(_SINGLE_ROW))
        monkeypatch.setattr(fd, "_maybe_fetch_attachment", MagicMock(side_effect=RuntimeError("boom")))
        env = fd._run_fetch_document(_call("59G-4.087 evaluation management", SimpleNamespace()))
        assert env.signal == "ok"
        assert "attachment" not in env.extra
        assert env.extra["golden"] is False


class TestMaybeFetchAttachment:
    def _fake_response(self, body: bytes, headers: dict):
        resp = MagicMock()
        resp.headers = headers
        resp.read.return_value = body
        resp.__enter__.return_value = resp
        resp.__exit__.return_value = False
        return resp

    def test_under_cap_returns_attachment(self):
        body = b"%PDF-1.4 fake small pdf"
        headers = {"Content-Length": str(len(body)), "Content-Type": "application/pdf"}
        with patch("urllib.request.urlopen", return_value=self._fake_response(body, headers)):
            out = fd._maybe_fetch_attachment("doc1", "test.pdf")
        assert out is not None
        assert out["mime_type"] == "application/pdf"
        assert out["filename"] == "test.pdf"
        import base64
        assert base64.b64decode(out["data_b64"]) == body

    def test_over_cap_by_content_length_returns_none(self):
        headers = {"Content-Length": str(fd._ATTACHMENT_MAX_BYTES + 1), "Content-Type": "application/pdf"}
        with patch("urllib.request.urlopen", return_value=self._fake_response(b"", headers)):
            out = fd._maybe_fetch_attachment("doc1", "huge.pdf")
        assert out is None

    def test_network_error_returns_none_not_raised(self):
        with patch("urllib.request.urlopen", side_effect=OSError("connection refused")):
            out = fd._maybe_fetch_attachment("doc1", "test.pdf")
        assert out is None

    def test_mime_type_inferred_from_filename_when_no_content_type_header(self):
        body = b"fake"
        headers = {"Content-Length": str(len(body))}
        with patch("urllib.request.urlopen", return_value=self._fake_response(body, headers)):
            out = fd._maybe_fetch_attachment("doc1", "handbook.pdf")
        assert out["mime_type"] == "application/pdf"
