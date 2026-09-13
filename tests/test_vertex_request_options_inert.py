"""Deep Research / Tool Manifest, 2026-09-12: `_vertex_request_options()`
builds `{"timeout": ..., "retry": ...}` kwargs meant to bound one HTTP
attempt and the SDK's total retry deadline. On the pinned SDK
(google-cloud-aiplatform 1.142.0) `GenerativeModel.generate_content`
declares neither param and no `**kwargs` -- confirmed directly with
`inspect.signature(...).bind()`, which raises TypeError for `timeout`.

Every call was therefore TypeError-ing immediately (before any network
I/O -- Python raises at argument-binding time), caught by a bare
`except TypeError`, and silently retried without the kwargs. That bare
except ALSO caught a genuine TypeError from a malformed generation_config
or content part and retried it identically -- "worked the second time"
masking a real bug.

NOTE on scope: this does NOT mean calls were unbounded. The separate
ThreadPoolExecutor + Future.result(timeout=deadline) wrapper in
_vertex_generate_sync (2026-04-27, bd07342) already caps total wall time
independent of these kwargs -- that wrapper is what fixed the 596s-retry-
storm incident this module's docstrings describe. These tests cover only
the request_options detection and the narrowed except, not that wrapper.
"""
from __future__ import annotations

import app.services.llm_provider as llm_provider


def _reset_cache():
    llm_provider._VERTEX_GENERATE_CONTENT_ACCEPTS_REQUEST_OPTIONS = None
    llm_provider._vertex_request_options_unsupported_logged = False


def test_installed_sdk_does_not_accept_request_options():
    # The actual, currently-pinned SDK -- ground truth for this repo today.
    _reset_cache()
    assert llm_provider.vertex_generate_content_accepts_request_options() is False


def test_result_is_cached_not_recomputed_every_call():
    _reset_cache()
    first = llm_provider.vertex_generate_content_accepts_request_options()
    # Corrupt the module-level cache slot directly to a sentinel that could
    # only appear if the function re-ran its detection logic.
    llm_provider._VERTEX_GENERATE_CONTENT_ACCEPTS_REQUEST_OPTIONS = first
    second = llm_provider.vertex_generate_content_accepts_request_options()
    assert second == first


def test_a_kwargs_style_signature_is_detected_as_supported():
    # A hypothetical future/older SDK build with **kwargs would legitimately
    # accept timeout/retry -- the detector must say so, not hardcode False.
    _reset_cache()

    class _FutureGenerativeModel:
        def generate_content(self, contents, generation_config=None,
                              safety_settings=None, tools=None, **kwargs):
            pass

    import vertexai.generative_models as gm
    real = gm.GenerativeModel
    gm.GenerativeModel = _FutureGenerativeModel
    try:
        assert llm_provider.vertex_generate_content_accepts_request_options() is True
    finally:
        gm.GenerativeModel = real
        _reset_cache()


def test_explicit_timeout_and_retry_params_are_detected_as_supported():
    _reset_cache()

    class _OlderGenerativeModel:
        def generate_content(self, contents, generation_config=None,
                              safety_settings=None, tools=None,
                              timeout=None, retry=None):
            pass

    import vertexai.generative_models as gm
    real = gm.GenerativeModel
    gm.GenerativeModel = _OlderGenerativeModel
    try:
        assert llm_provider.vertex_generate_content_accepts_request_options() is True
    finally:
        gm.GenerativeModel = real
        _reset_cache()


def test_import_failure_falls_back_to_unsupported_not_a_crash():
    # Matches the prior behavior of the unconditional except TypeError --
    # if we can't even introspect, assume unsupported and let the call
    # site's narrower except handle any surprise.
    _reset_cache()

    import builtins
    real_import = builtins.__import__

    def _blow_up_on_vertexai(name, *args, **kwargs):
        if name == "vertexai.generative_models" or name.startswith("vertexai"):
            raise ImportError("simulated: vertexai unavailable")
        return real_import(name, *args, **kwargs)

    builtins.__import__ = _blow_up_on_vertexai
    try:
        assert llm_provider.vertex_generate_content_accepts_request_options() is False
    finally:
        builtins.__import__ = real_import
        _reset_cache()
