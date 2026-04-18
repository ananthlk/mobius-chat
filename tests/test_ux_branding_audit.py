"""2026-04-18 UX branding audit — regression locks.

The audit called out a pile of branding / copy issues that had drifted
into the codebase. This module converts the fixes into CI-enforced
invariants so none of them silently regress:

  1. No rogue ``#1A73E8`` ("Google blue") accent anywhere in CSS.
  2. All un-prefixed fallback tokens (``--surface``, ``--border``,
     ``--text-primary``) must exist in ``mobius-tokens.css``.
  3. ``--mobius-indigo`` / ``--mobius-violet`` / ``--mobius-emerald``
     semantic tokens must exist (referenced by skills cards & landing).
  4. Main header has the Mobius logo (brand presence when sidebar is
     collapsed).
  5. The five jargon strings the audit flagged don't reappear.

CSS is parsed as plain text (not a real CSS AST) — good enough to
catch the common regression patterns without pulling in a dep.
"""

from __future__ import annotations

from pathlib import Path

import pytest


REPO = Path(__file__).parent.parent
TOKENS = REPO / "frontend" / "static" / "mobius-tokens.css"
STYLES = REPO / "frontend" / "static" / "styles.css"
INDEX  = REPO / "frontend" / "index.html"


def _strip_css_comments(css: str) -> str:
    """Remove /* ... */ blocks so audit scans only check code, not docs.
    The audit fixes include comments like "// removed 1A73E8" — those
    shouldn't trip the regression guards."""
    import re
    return re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)


@pytest.fixture(scope="module")
def tokens_text() -> str:
    return _strip_css_comments(TOKENS.read_text())


@pytest.fixture(scope="module")
def styles_text() -> str:
    return _strip_css_comments(STYLES.read_text())


@pytest.fixture(scope="module")
def tokens_text_raw() -> str:
    """Un-stripped — for tests that legitimately inspect comments
    (e.g. token-name coverage)."""
    return TOKENS.read_text()


@pytest.fixture(scope="module")
def index_text() -> str:
    return INDEX.read_text()


# ── Fix 1: kill the Google-blue accent ───────────────────────────────────


class TestAccentIsBrandBlue:
    """The #1A73E8 hex (Google-brand blue) was used as a temporary
    accent months ago and got stuck. Audit flagged; this test prevents
    the hex from sneaking back in."""

    def test_no_raw_google_blue_in_styles(self, styles_text: str):
        # Case-insensitive to catch #1a73e8 too.
        assert "1A73E8" not in styles_text.upper(), (
            "styles.css contains the Google-blue hex #1A73E8 — use "
            "var(--mobius-accent) instead. The UX audit (2026-04-18) "
            "collapsed the three stray accent colors into semantic tokens."
        )

    def test_no_raw_google_blue_in_tokens(self, tokens_text: str):
        assert "1A73E8" not in tokens_text.upper()

    def test_accent_aliases_point_to_mobius_token(self, styles_text: str):
        """--accent and --accent-hover must be aliases for the mobius
        brand tokens, not raw hex literals."""
        import re
        m = re.search(r"--accent:\s*([^;]+);", styles_text)
        assert m, "--accent alias is missing from styles.css"
        value = m.group(1).strip()
        assert "var(--mobius-accent)" in value, (
            f"--accent resolves to {value!r}, not var(--mobius-accent). "
            "The audit mandates the alias points at the brand token."
        )


# ── Fix 2: fallback token aliases ────────────────────────────────────────


class TestFallbackTokensDefined:
    """Skill cards + skills modal use shorthand names (--surface,
    --border, --text-primary). Before the audit these fell through to
    browser defaults because mobius-tokens.css only defined the
    --mobius-* variants. Now the shorthand names are aliases so
    components that reference them render correctly."""

    REQUIRED_ALIASES: tuple[str, ...] = (
        "--surface",
        "--border",
        "--text-primary",
    )

    def test_all_aliases_present(self, tokens_text: str):
        missing = [t for t in self.REQUIRED_ALIASES if t not in tokens_text]
        assert not missing, (
            f"Shorthand aliases missing from mobius-tokens.css: {missing}. "
            f"Without them, components using var(--surface) etc. fall "
            f"through to browser defaults and render with invisible "
            f"borders/backgrounds."
        )

    def test_aliases_are_aliases_not_duplicate_hexes(self, tokens_text: str):
        """Each alias must reference the corresponding --mobius-* token
        via var(), not a raw hex. Otherwise we've introduced a drift
        source."""
        import re
        for alias in self.REQUIRED_ALIASES:
            m = re.search(rf"{re.escape(alias)}:\s*([^;]+);", tokens_text)
            assert m, f"{alias} not defined"
            value = m.group(1).strip()
            assert "var(--mobius-" in value, (
                f"{alias} value is {value!r} — should be var(--mobius-*) "
                f"to keep the brand tokens canonical."
            )


# ── Fix 3: semantic accent tokens ────────────────────────────────────────


class TestSemanticAccentTokens:
    """The audit asked us to collapse three stray accent hex codes
    into named semantic tokens. mobius-tokens.css now defines them;
    this test locks the names in place."""

    REQUIRED_SEMANTIC_TOKENS: tuple[str, ...] = (
        "--mobius-indigo",
        "--mobius-violet",
        "--mobius-emerald",
    )

    def test_all_semantic_tokens_defined(self, tokens_text: str):
        missing = [t for t in self.REQUIRED_SEMANTIC_TOKENS if t not in tokens_text]
        assert not missing, (
            f"Semantic accent tokens missing from mobius-tokens.css: "
            f"{missing}. These were flagged by the 2026-04-18 audit — "
            f"skill cards, suite banner, and the sidebar suite label "
            f"reference them; without the token the fallback hex "
            f"works but there's no single source of truth."
        )


# ── Fix 4: main-header Mobius identity ───────────────────────────────────


class TestMainHeaderBrand:
    """When the sidebar is collapsed, the main page had no Mobius
    identity. Audit fix: add the logo to the main header so it's
    visible across sidebar states."""

    def test_main_header_has_logo_image(self, index_text: str):
        assert "main-header-logo" in index_text, (
            "Main header doesn't carry a .main-header-logo element — "
            "users see a brandless page when the sidebar is collapsed."
        )
        # Should reference the same logo file as the sidebar.
        assert 'class="main-header-logo"' in index_text
        assert 'static/logo.svg' in index_text


# ── Fix 5: jargon strings ────────────────────────────────────────────────


class TestJargonStringsRemovedFromTemplate:
    """The audit flagged five specific user-visible strings as too
    technical. This test asserts they don't reappear in index.html."""

    BANNED_PHRASES: tuple[str, ...] = (
        # Upload modal
        "Document upload skill",
        "Roster for reconciliation",
        "Other (RAG / reference)",
        "Document for RAG",
        # Credentialing form
        "Outside-in Medicaid NPI only",
        "Run full pipeline fresh",
        "today’s cached outside-in report",
        "today's cached outside-in report",
        # Skill cards
        "NPPES × PML × TML taxonomy coverage",
        "Step 3 (NPPES alignment)",
        "Steps 3–6 per run: NPPES",
    )

    def test_banned_phrases_absent_from_index(self, index_text: str):
        offenders: list[str] = []
        for phrase in self.BANNED_PHRASES:
            if phrase in index_text:
                offenders.append(phrase)
        assert not offenders, (
            "Developer-facing jargon reappeared in index.html:\n"
            + "\n".join(f"  - {o!r}" for o in offenders)
            + "\n\nRewrite to plain language per the 2026-04-18 UX audit."
        )

    def test_replacement_phrases_present(self, index_text: str):
        """Positive check: the user-friendly replacements the audit
        recommended are actually in the template.

        2026-04-18 disconnect narrowed the assertion: four of the five
        original replacements lived on credentialing/roster UI that was
        cut in this session. Only the instant-rag / attach flow remains
        chat-side. The deleted surfaces will get rebuilt as separate
        skill UIs with their own copy conventions.
        """
        expected = [
            # Attach a file (replacement for "Document upload skill" —
            # on the remaining upload modal)
            "Attach a",
        ]
        missing = [p for p in expected if p not in index_text]
        assert not missing, (
            f"Expected user-friendly replacements missing from index.html: "
            f"{missing}. Did the rewrite get undone?"
        )
