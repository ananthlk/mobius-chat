"""Seed conversion for prompt_templates (migration 049) — the str.format → Jinja2
converter and AC-6 byte-parity check.

Part of the LLMManager seed tooling. Spec: docs/SPEC_LLM_MANAGER.md §4.

Finding A (verified): existing prompts split cleanly into two render classes —
  * SYSTEM prompts are used RAW (never .format()'d) and the yaml contains ZERO
    {{ / }} sequences, so a single { in their literal JSON schemas is literal in
    Jinja. They render byte-identical with NO conversion.  → to_jinja_system = identity
  * USER templates ARE .format()'d with named placeholders and contain no literal
    JSON braces, so converting {name} → {{ name }} is byte-faithful.
    → to_jinja_user_template

AC-6: for every seeded prompt, the Jinja render with the same vars must equal the
current .format()/raw output BYTE-FOR-BYTE. assert_parity() is that gate.
"""
from __future__ import annotations

import re

import jinja2

# autoescape=False (prompts are not HTML, §3.1). keep_trailing_newline=True is
# load-bearing for AC-6: Jinja's default strips a single trailing newline, but
# the yaml block scalars / .format() output keep it — dropping it is a 1-byte
# change to every prompt. (Caught by the parity harness on first run.)
_ENV = jinja2.Environment(autoescape=False, keep_trailing_newline=True)

# Jinja-special openers. If a "system" (identity) prompt contained any of these
# it would NOT be safe as-is — assert_parity catches it, but we also guard here.
_JINJA_SPECIAL = ("{{", "{%", "{#")


def to_jinja_system(body: str) -> str:
    """System prompts render as-is. Guard: they must contain no Jinja-special
    opener, else identity rendering would reinterpret them."""
    for tok in _JINJA_SPECIAL:
        if tok in body:
            raise ValueError(
                f"to_jinja_system: body contains Jinja-special {tok!r}; not safe as identity. "
                f"This prompt needs explicit escaping — do not seed it blind."
            )
    return body


def to_jinja_user_template(body: str, placeholders: list[str]) -> str:
    """Convert exactly the named ``{placeholder}`` tokens to ``{{ placeholder }}``.

    Only the given placeholders are converted; any other brace run is left alone
    (and, per Finding A, user templates have none). Fails if the body already
    contains a Jinja-special opener (would collide with the conversion output).
    """
    for tok in _JINJA_SPECIAL:
        if tok in body:
            raise ValueError(f"to_jinja_user_template: body already contains Jinja-special {tok!r}")
    out = body
    for name in placeholders:
        # word-exact placeholder — {name} with no surrounding brace noise
        out = out.replace("{" + name + "}", "{{ " + name + " }}")
    # Sanity: no bare single-brace placeholders of the converted names remain.
    for name in placeholders:
        if re.search(r"(?<!\{)\{" + re.escape(name) + r"\}(?!\})", out):
            raise ValueError(f"to_jinja_user_template: unconverted {{{name}}} remains")
    return out


def render(jinja_body: str, **vars) -> str:
    return _ENV.from_string(jinja_body).render(**vars)


def assert_parity(original: str, jinja_body: str, sample_vars: dict) -> None:
    """AC-6 gate: Jinja render == the current output byte-for-byte.

    For system prompts pass sample_vars={} and jinja_body=to_jinja_system(original)
    (expected == original). For user templates pass the .format() vars — expected
    is original.format(**sample_vars).
    """
    expected = original.format(**sample_vars) if sample_vars else original
    got = render(jinja_body, **sample_vars)
    if got != expected:
        raise AssertionError(
            "AC-6 parity FAILED.\n"
            f"--- expected (.format/raw) ---\n{expected!r}\n"
            f"--- got (jinja) ---\n{got!r}"
        )
