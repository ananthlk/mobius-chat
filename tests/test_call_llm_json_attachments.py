"""Task #106 (2026-08-16, Ananth, directly): _call_llm_json threads
attachments through to llm_manager.generate() on the ctx-provided (async
Vertex) path."""
from __future__ import annotations

from unittest.mock import patch

from app.pipeline.context import PipelineContext
from app.pipeline.react.prompts import _call_llm_json


def test_attachments_reach_generate():
    # _call_llm_json manages its own event loop internally (asyncio.run),
    # so this test stays sync -- calling it from an async test would nest
    # event loops and raise.
    ctx = PipelineContext(correlation_id="c1", thread_id="t1", message="hi")
    captured = {}

    async def fake_generate(prompt, **kwargs):
        captured["attachments"] = kwargs.get("attachments")
        return ('{"ok": true}', {"model": "gemini-2.5-flash", "provider": "vertex"})

    atts = [{"mime_type": "application/pdf", "data_b64": "ZmFrZQ=="}]
    with patch("app.services.llm_manager.generate", new=fake_generate):
        out = _call_llm_json("sys", "user", ctx=ctx, stage="react_1", attachments=atts)

    assert out == '{"ok": true}'
    assert captured["attachments"] == atts


def test_no_attachments_passes_none():
    ctx = PipelineContext(correlation_id="c1", thread_id="t1", message="hi")
    captured = {}

    async def fake_generate(prompt, **kwargs):
        captured["attachments"] = kwargs.get("attachments")
        return ('{"ok": true}', {"model": "gemini-2.5-flash", "provider": "vertex"})

    with patch("app.services.llm_manager.generate", new=fake_generate):
        _call_llm_json("sys", "user", ctx=ctx, stage="react_1")

    assert captured["attachments"] is None
