"""Phase B.1a — composer-attach UX regression.

We can't drive the frontend JS from pytest in-process, but we can assert
that the structural hooks the JS relies on are present in the HTML + CSS
+ JS. If any of the anchor IDs, class names, or handler wiring drift,
this test fires and the next CI run catches it before it reaches the
UI team's radar.

Guards:
  1. HTML has the paperclip button + hidden file input + chip
  2. CSS has the chip + dragover styling hooks
  3. JS has the upload-then-send wrapper (uploadStagedAttachmentForInstantRag)
  4. JS wires the wrapped handler via stopImmediatePropagation so the
     original "just send" listener doesn't also fire when a file is
     staged — failing this means both handlers run and the UX double-sends.
"""

from __future__ import annotations

from pathlib import Path

import pytest


REPO = Path(__file__).parent.parent
# Served by main.py (FileResponse for "/"): the file at frontend/index.html.
# frontend/static/index.html also exists but isn't routed anywhere.
HTML = REPO / "frontend" / "index.html"
CSS = REPO / "frontend" / "static" / "styles.css"
# Source of truth: frontend/src/app.ts is compiled via esbuild to
# frontend/static/app.js. Editing the compiled artifact is a footgun
# (mstart runs `npm run build` on boot and clobbers manual edits).
# Tests assert the TS source directly + verify the build output hasn't
# drifted away from it.
JS_SRC = REPO / "frontend" / "src" / "app.ts"
JS_BUILT = REPO / "frontend" / "static" / "app.js"


@pytest.fixture(scope="module")
def html_text() -> str:
    return HTML.read_text()


@pytest.fixture(scope="module")
def css_text() -> str:
    return CSS.read_text()


@pytest.fixture(scope="module")
def js_text() -> str:
    """TypeScript source — the file developers edit."""
    return JS_SRC.read_text()


@pytest.fixture(scope="module")
def js_built() -> str:
    """Built bundle — what the browser loads. Must include the B.1a
    identifiers; if it doesn't, someone edited the TS but forgot to run
    `npm run build`, and the browser still sees the old bundle."""
    return JS_BUILT.read_text()


# ── Structural anchors in HTML ────────────────────────────────────────────


class TestComposerAttachHTML:
    def test_paperclip_button_present(self, html_text: str):
        assert 'id="composerAttach"' in html_text, (
            "Composer paperclip button (#composerAttach) missing from "
            "index.html — Phase B.1a removed the only attach affordance."
        )

    def test_hidden_file_input_present(self, html_text: str):
        assert 'id="composerAttachmentInput"' in html_text
        # Must be hidden so it doesn't render as an ugly native file input.
        assert 'id="composerAttachmentInput"' in html_text and ' hidden' in html_text.split(
            'id="composerAttachmentInput"', 1
        )[1].split(">", 1)[0] + ">"

    def test_chip_dom_present(self, html_text: str):
        for anchor in (
            'id="composerAttachmentChip"',
            'id="composerAttachmentChipName"',
            'id="composerAttachmentChipRemove"',
        ):
            assert anchor in html_text, f"Chip anchor {anchor} missing."

    def test_accepted_types_cover_the_four_ingest_formats(self, html_text: str):
        """Chat's _handle_instant_rag_upload extracts PDF / DOCX / HTML /
        TXT. The file picker must advertise those so users don't stage a
        format the skill will reject."""
        for expected in (".pdf", ".docx", ".html", ".txt"):
            assert expected in html_text, (
                f"Composer file input doesn't accept {expected!r}; users "
                f"will stage files the skill can't ingest."
            )


# ── Structural anchors in CSS ────────────────────────────────────────────


class TestComposerAttachCSS:
    def test_chip_styled(self, css_text: str):
        assert ".composer-attachment-chip" in css_text, (
            "Chip has no CSS — it'll render as an unstyled inline row. "
            "Re-add the .composer-attachment-chip block to styles.css."
        )

    def test_uploading_pulse_animation(self, css_text: str):
        assert ".composer-attachment-chip.is-uploading" in css_text
        assert "@keyframes composer-attach-pulse" in css_text

    def test_dragover_highlight(self, css_text: str):
        assert ".composer-wrap--dragover" in css_text, (
            "Drag-over visual cue missing — drag-drop onto the composer "
            "will feel dead without it."
        )


# ── JS wiring (string-level — we don't execute the JS in pytest) ─────────


class TestComposerAttachJSWiring:
    def test_upload_helper_present(self, js_text: str):
        assert "uploadStagedAttachmentForInstantRag" in js_text, (
            "The upload-before-send helper was removed. Without it, a "
            "staged file is silently dropped on Send."
        )

    def test_upload_targets_roster_upload_with_instant_rag_purpose(self, js_text: str):
        # The helper POSTs to /chat/roster-upload (current entry point)
        # with file_purpose=instant_rag. If either drifts, the upload
        # goes somewhere else silently.
        assert '"/chat/roster-upload"' in js_text
        assert '"instant_rag"' in js_text
        assert "file_purpose" in js_text

    def test_wrapper_uses_stop_immediate_propagation(self, js_text: str):
        """The attachment-aware click/keydown handlers MUST call
        stopImmediatePropagation when they'll handle the send. Otherwise
        the original `sendMessage()` handler registered earlier also fires
        and we get a double-send (the file-less original call hits the API
        while the upload is still in flight)."""
        assert "stopImmediatePropagation" in js_text, (
            "Phase B.1a wrapper doesn't stop the original send handler — "
            "that will cause the chat turn to fire before upload finishes."
        )

    def test_size_guard_present(self, js_text: str):
        """Chat → instant-rag has a 120s inline timeout; files over ~25MB
        routinely time out. The composer must guard size BEFORE upload
        so the user gets an immediate error, not a two-minute pause."""
        assert "25 * 1024 * 1024" in js_text or "25*1024*1024" in js_text, (
            "Composer size guard missing or the 25MB limit changed. "
            "Check and either update this test or restore the guard."
        )

    def test_drag_drop_handler_present(self, js_text: str):
        """Dragging a PDF onto the composer-wrap should stage it the same
        way as the picker. Regression: missing handler means drag-drop
        looks dead to the user."""
        for needle in ("dragover", "dragenter", "drop"):
            assert needle in js_text, (
                f"Drag-drop event {needle!r} not wired in app.js"
            )

    def test_clear_attachment_function_present(self, js_text: str):
        """The × button on the chip must actually clear the staged file,
        otherwise the user clicks × and the file still uploads on Send."""
        assert "clearComposerAttachment" in js_text
        assert "composerAttachmentChipRemove" in js_text


class TestComposerAttachBuildSync:
    """Catches the "I edited app.ts but forgot to run npm run build" failure
    mode. mstart runs `npm run build` on boot, but CI and any local dev
    that skips mstart need to surface the staleness loudly.

    Each identifier added in the TS source MUST also show up in the
    built bundle. If not, the browser is serving an outdated bundle and
    nothing works at runtime.
    """

    REQUIRED_IDENTIFIERS: tuple[str, ...] = (
        "composerStagedFile",
        "uploadStagedAttachmentForInstantRag",
        "clearComposerAttachment",
        "composerAttachmentChip",
    )

    def test_built_bundle_contains_b1a_identifiers(self, js_built: str):
        missing = [i for i in self.REQUIRED_IDENTIFIERS if i not in js_built]
        assert not missing, (
            f"frontend/static/app.js is stale — rebuild with "
            f"`cd frontend && npm run build`. Missing identifiers from "
            f"the bundle: {missing}"
        )

    def test_built_bundle_posts_to_roster_upload(self, js_built: str):
        """Bundle must POST to /chat/roster-upload. If esbuild tree-shook
        the code away (dead-code elimination when all refs go through an
        unreachable branch), this catches it."""
        assert "/chat/roster-upload" in js_built


class TestServedHTML:
    """The FastAPI app serves frontend/index.html (not frontend/static/index.html).
    If the composer anchors regress to the unused static/index.html, the
    built paperclip renders invisibly because it's not in the file the
    browser loads. These tests lock in the right file."""

    def test_main_py_serves_frontend_index_html(self):
        main_py = REPO / "app" / "main.py"
        text = main_py.read_text()
        assert 'FileResponse(_frontend / "index.html")' in text, (
            "main.py no longer serves frontend/index.html via FileResponse — "
            "if the route moved, update this test AND frontend/index.html "
            "to match the new served-file path."
        )
