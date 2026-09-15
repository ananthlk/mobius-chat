"""2026-09-15: `_chat_config_from_prompts_llm()` (the config path actually
used in production, since config/prompts_llm.yaml is present) built
`vertex_project_id` with a three-tier fallback -- YAML value, then
CHAT_VERTEX_PROJECT_ID/VERTEX_PROJECT_ID env vars, then a hardcoded
default -- but `vertex_location` right below it had only two tiers,
skipping the env var check entirely: `_str(llm_d, "vertex_location") or
"us-central1"`.

Found live in production: VERTEX_LOCATION was switched to "global" on
the deployed Cloud Run service (required for newly-added Gemini 3.x
models, which only respond on location=global -- confirmed with real
generateContent calls), the service's own env var correctly showed
"global", and every draw of a new model still 404'd against
us-central1. The YAML's own stored value (also "us-central1", set
before this ever mattered) was winning every time, with no way for an
env var to override it.

This was invisible until today only because VERTEX_LOCATION had never
been changed from the code's own "us-central1" default before -- the
bug and the default happened to agree.
"""
from __future__ import annotations

from unittest.mock import patch

from app.chat_config import _chat_config_from_prompts_llm, ChatRAGConfig


def _rag_stub() -> ChatRAGConfig:
    return ChatRAGConfig(database_url="postgresql://test")


def test_env_var_overrides_when_yaml_omits_vertex_location():
    pl = {"llm": {"provider": "vertex", "model": "gemini-2.5-flash"}}
    with patch.dict("os.environ", {"VERTEX_LOCATION": "global"}, clear=False):
        cfg = _chat_config_from_prompts_llm(pl, _rag_stub())
    assert cfg.llm.vertex_location == "global"


def test_yaml_value_still_wins_over_env_when_both_present():
    # Explicit config-as-data intentionally takes precedence over env --
    # this is the SAME precedence vertex_project_id already has; the fix
    # only closes the gap where vertex_location had no env fallback AT
    # ALL, not change which value wins when both are set.
    pl = {"llm": {"provider": "vertex", "model": "gemini-2.5-flash", "vertex_location": "us-central1"}}
    with patch.dict("os.environ", {"VERTEX_LOCATION": "global"}, clear=False):
        cfg = _chat_config_from_prompts_llm(pl, _rag_stub())
    assert cfg.llm.vertex_location == "us-central1"


def test_hardcoded_default_when_neither_yaml_nor_env_set():
    pl = {"llm": {"provider": "vertex", "model": "gemini-2.5-flash"}}
    with patch.dict("os.environ", {}, clear=True):
        cfg = _chat_config_from_prompts_llm(pl, _rag_stub())
    assert cfg.llm.vertex_location == "us-central1"


def test_config_prompts_llm_yaml_itself_is_global():
    # The actual deployed file -- catches a future revert back to
    # us-central1 in the source file itself, independent of env-var
    # plumbing.
    from app.prompts_llm_config import load_prompts_llm_config

    pl, _ = load_prompts_llm_config()
    assert pl.get("llm", {}).get("vertex_location") == "global"
