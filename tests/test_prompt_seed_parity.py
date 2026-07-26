"""AC-6 byte-parity: the seed conversion of config/prompts_llm.yaml prompts must
render identically to the current str.format()/raw output. Guards the migration
from silently changing a prompt. Spec: docs/SPEC_LLM_MANAGER.md §4 (Finding A).
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from app.services.prompt_seed import assert_parity, to_jinja_system, to_jinja_user_template

_YAML = Path(__file__).resolve().parent.parent / "config" / "prompts_llm.yaml"

# Special-char sample values exercise autoescape=False (quotes/&/</> must pass
# through unescaped, identical to .format()).
_SPECIAL = 'A & "B" <c> {not_a_placeholder}'

# Every .format()'d user template: its placeholders + sample vars covering them.
_USER_TEMPLATES = {
    "decompose_user_template": {"context": _SPECIAL, "message": "msg <x>"},
    "decompose_user_template_mobius": {"planner_input_json": '{"k": "v & w"}'},
    "rag_answering_user_template": {"context": _SPECIAL, "question": "q?"},
    "first_gen_user_template": {"message": "m", "plan_summary": "p & q"},
    "integrator_user_template": {"consolidator_input_json": '{"a": 1}'},
}

# System prompts: used raw today → identity in Jinja (must contain no Jinja-special).
_SYSTEM_PROMPTS = [
    "decompose_system",
    "decompose_system_mobius",
    "first_gen_system",
    "integrator_system",
    "integrator_repair_system",
    "integrator_factual_system",
    "integrator_canonical_system",
    "integrator_blended_system",
]


def _prompts() -> dict:
    if not _YAML.exists():
        pytest.skip(f"prompts_llm.yaml not found at {_YAML}")
    data = yaml.safe_load(_YAML.read_text(encoding="utf-8"))
    return (data or {}).get("prompts", {}) or {}


@pytest.mark.parametrize("key", list(_USER_TEMPLATES))
def test_user_template_parity(key):
    prompts = _prompts()
    if key not in prompts:
        pytest.skip(f"{key} absent from yaml")
    original = prompts[key]
    sample = _USER_TEMPLATES[key]
    jinja_body = to_jinja_user_template(original, list(sample.keys()))
    assert_parity(original, jinja_body, sample)  # raises on any byte diff


@pytest.mark.parametrize("key", _SYSTEM_PROMPTS)
def test_system_prompt_identity_parity(key):
    prompts = _prompts()
    if key not in prompts:
        pytest.skip(f"{key} absent from yaml")
    original = prompts[key]
    jinja_body = to_jinja_system(original)  # raises if a Jinja-special opener present
    assert jinja_body == original
    assert_parity(original, jinja_body, {})  # identity render, byte-for-byte


def test_converter_rejects_double_brace_collision():
    with pytest.raises(ValueError):
        to_jinja_user_template("already {{ x }} jinja", ["x"])


def test_autoescape_off_matches_format_on_special_chars():
    # Direct proof the two renderers agree on the exact bytes for &, quotes, <>.
    body = "V: {v} end"
    jinja_body = to_jinja_user_template(body, ["v"])
    assert_parity(body, jinja_body, {"v": 'x & "y" <z>'})
