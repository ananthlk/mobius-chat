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


class TestUploadProgressEmits:
    """Phase B.1a progress UX — users need visible feedback during the
    30-60s ingest pause. A silent pulsing chip felt like a hang.

    Locked in: the upload path must (a) surface phase messages via
    showChatStatusBanner on a staged timer, (b) emit a success message
    with chunks_count on completion, (c) clear the phase timers in
    both success and failure paths.
    """

    def test_phase_emits_helper_present(self, js_text: str):
        assert "startComposerUploadPhaseEmits" in js_text, (
            "startComposerUploadPhaseEmits was removed — users will see "
            "a silent pulsing chip during a 30-60s ingest. Restore the "
            "phase-emit pattern (parallels the upload-modal flow)."
        )
        assert "stopComposerUploadPhaseEmits" in js_text, (
            "Stop helper missing — phase timers leak past upload completion."
        )

    def test_phase_messages_include_filename(self, js_text: str):
        """Bare 'Extracting…' is ambiguous when the user might have
        multiple things in flight. Filename in the phase message is
        disambiguating."""
        # Look for the template literal interpolation with filename.
        assert "${filename}" in js_text

    def test_phase_emits_cover_realistic_timing(self, js_text: str):
        """Real ingests land between 3s (small HTML) and 60s+ (large PDFs
        with embedding calls). The phase schedule must run past 30s — if
        the latest emit is sub-10s, the user sees "Still processing" 20s
        before the skill actually returns and assumes it's hung."""
        # Look for a phase timer at >= 30000ms. We only assert the
        # existence of at least one large ms value in the source.
        import re
        ms_values = [int(m) for m in re.findall(r"ms:\s*(\d+)", js_text)]
        assert any(v >= 30000 for v in ms_values), (
            "No phase emit at 30s+ — long uploads will feel dead. "
            f"Found ms values: {sorted(set(ms_values))}"
        )

    def test_success_banner_is_user_friendly(self, js_text: str):
        """On success, the banner tells the user the doc is ready to
        search in plain language. 2026-04-18: reverted from the earlier
        "ingested (N chunks)" phrasing — users flagged 'publishing to
        RAG' and 'chunks' as developer jargon. The chunks_count is
        still captured from the skill response but only logged to the
        debug console, not shown to the user.

        Scoped to the uploadStagedAttachmentForInstantRag function body
        since "ingested" legitimately appears elsewhere in app.ts
        (e.g., the roster-upload modal's legacy messaging).
        """
        anchor = "uploadStagedAttachmentForInstantRag"
        idx = js_text.find(anchor)
        assert idx >= 0, "uploadStagedAttachmentForInstantRag helper missing"
        # Look at the next ~6KB — the function grew with multi-path UX
        # (background/redirect/duplicate/blocking) so 3KB was too short.
        region = js_text[idx:idx + 6000]
        # Plain-language user feedback.
        assert "is ready" in region, (
            "Success banner should say the doc is ready — any phrasing "
            "that removes this user-facing signal is a UX regression."
        )
        # "ingested" must not appear in the showChatStatusBanner call
        # inside this function (the user-visible surface). Comments,
        # console.debug, variable names in OTHER functions are fine.
        import re
        banner_calls = re.findall(
            r"showChatStatusBanner\s*\(\s*`[^`]*`", region,
        )
        for call in banner_calls:
            assert "ingested" not in call, (
                f"Banner copy still says 'ingested' — 2026-04-18 copy "
                f"revision should have removed this: {call[:120]}"
            )

    def test_phase_timers_stopped_on_failure(self, js_text: str):
        """Failure path must call stopComposerUploadPhaseEmits or the
        "still processing" message keeps flashing after the alert."""
        # Both branches (finally in uploadStagedAttachmentForInstantRag
        # AND the catch in sendMessageWithAttachment) call stop — either
        # one fires covers the failure path.
        assert js_text.count("stopComposerUploadPhaseEmits") >= 2, (
            "stopComposerUploadPhaseEmits not called in both the finally "
            "block AND the catch handler — phase timers will leak past "
            "upload failure and the banner will keep showing progress "
            "messages over the error."
        )


class TestLargeFileConfirmGate:
    """Phase B.1a — large-file confirmation.

    Above ~500KB (user's "10-15 pages" intuition for text-heavy PDFs),
    show a modal BEFORE upload so the user chooses instant vs batch
    instead of being silently committed to a 30-60s wait. The batch
    option is surfaced disabled today (promote pipeline stubbed;
    Phase B.7 wires it up).

    These tests assert the DOM hooks, the threshold, and that the
    confirmation gate actually blocks upload until the user picks.
    """

    def test_large_upload_modal_markup(self, html_text: str):
        for anchor in (
            'id="largeUploadModal"',
            'id="largeUploadOverlay"',
            'id="largeUploadProceedInstant"',
            'id="largeUploadProceedBatch"',
            'id="largeUploadCancel"',
            'id="largeUploadModalBody"',
        ):
            assert anchor in html_text, (
                f"Large-upload confirm modal anchor {anchor} missing — "
                f"without it, showLargeUploadConfirm has no DOM to drive."
            )

    def test_batch_option_disabled_today(self, html_text: str):
        """Batch promotion is Phase B.7 — until it lands, the button
        must stay disabled so users don't silently fall into a dead path."""
        # Extract the batch button's opening tag and assert it has disabled.
        idx = html_text.find('id="largeUploadProceedBatch"')
        assert idx >= 0
        # Walk back to the opening <button ...> to check attributes.
        open_tag_end = html_text.find(">", idx)
        open_tag_start = html_text.rfind("<button", 0, idx)
        tag = html_text[open_tag_start:open_tag_end + 1]
        assert " disabled" in tag, (
            "Batch option must be disabled until Phase B.7 wires up "
            "/envelope/{id}/promote. Removing the disabled attribute "
            "without implementing the backend path will silently drop "
            "user intent."
        )

    def test_threshold_is_reasonable(self, js_text: str):
        """The threshold must be > 100KB (below that, even a short memo
        triggers the prompt) and < 5MB (above that, real docs silently
        slip past). 500KB is the calibrated sweet spot."""
        import re
        m = re.search(r"LARGE_FILE_THRESHOLD_BYTES\s*=\s*([^;]+);", js_text)
        assert m, "LARGE_FILE_THRESHOLD_BYTES constant missing or renamed"
        # Evaluate the expression — it's a simple numeric literal or arithmetic.
        value = eval(m.group(1), {"__builtins__": {}}, {})
        assert 100 * 1024 <= value <= 5 * 1024 * 1024, (
            f"LARGE_FILE_THRESHOLD_BYTES={value} is outside the reasonable "
            f"calibration range. Sub-100KB spams the prompt; above 5MB "
            f"silently accepts documents that will take 60+ seconds."
        )

    def test_confirm_called_in_send_flow(self, js_text: str):
        """The send flow must call showLargeUploadConfirm for large files.
        If it's defined but never awaited, the gate is dead code."""
        assert "showLargeUploadConfirm" in js_text
        assert "LARGE_FILE_THRESHOLD_BYTES" in js_text
        # Heuristic: in the send flow, the helper should appear near the
        # threshold check. They should both appear in the TS source.
        # (We don't check proximity because minification shuffles things.)

    def test_three_choices_returned_by_confirm(self, js_text: str):
        """The dialog must resolve to one of 'instant' | 'batch' | 'cancel'.
        A two-choice (yes/no) regression would drop the batch-promotion
        seam the dialog is pre-wiring."""
        for choice in ('"instant"', '"batch"', '"cancel"'):
            assert choice in js_text, (
                f"Confirm dialog no longer resolves {choice!r}; the "
                f"three-way contract is being relied on by the caller."
            )

    def test_escape_and_enter_handled(self, js_text: str):
        """Keyboard support: Esc cancels, Enter confirms (instant)."""
        assert '"Escape"' in js_text
        assert '"Enter"' in js_text


class TestNoDeveloperJargonInUserFacingStrings:
    """2026-04-18: user flagged "⏳ Uploading 'X' — publishing to RAG…"
    as developer jargon. Rewrote the upload phase messages + success +
    failure copy to plain language. This test locks the reversion in:
    if any of the flagged terms reappear in user-visible template
    literals, the test fails and we know copy regressed.

    The banned terms are the ones users specifically don't parse:
      RAG, chunks, embeddings, ingested, publishing to, corpus (lowercase),
      chunking, embed
    They're fine in code comments, variable names, and console.debug —
    this test only blocks them inside template literals that end up as
    user-visible strings via showChatStatusBanner, emit, or alert.
    """

    BANNED_IN_USER_STRINGS: tuple[str, ...] = (
        "publishing to RAG",
        "chunking",
        "embeddings",
        "ingested",
        "generating embedding",
        # Added 2026-04-18 after the roster-receipt leak found by the
        # second scorecard audit: the earlier guard covered only
        # showChatStatusBanner/alert/setStatus, missing the roster-upload
        # receipt UI that wrote "Document ingested for RAG" + "chunked,
        # embedded" + "chunk(s) indexed" + "Verification tier" via
        # .textContent = assignments. Adding these patterns so the
        # receipt-scan test below fires if any of them reappear.
        "Chunks indexed",
        "Verification tier",
        "RAG corpus",
    )

    def test_no_jargon_in_showchatstatusbanner_literals(self, js_text: str):
        """Scan showChatStatusBanner() calls and flag banned substrings
        inside the first argument. We look for the calls and walk the
        adjacent template literal. Not a perfect AST scan, but covers
        99% of real cases — banner args are almost always a single
        template literal on the same or next line."""
        import re
        # Find each showChatStatusBanner call and grab ~200 chars that
        # should contain the template literal argument.
        for match in re.finditer(r"showChatStatusBanner\s*\(", js_text):
            start = match.end()
            snippet = js_text[start:start + 250]
            # Only inspect the first argument — stop at the first top-level
            # comma OR the closing paren, whichever comes first. Simple
            # nesting-aware walk.
            depth = 0
            first_arg_end = len(snippet)
            for i, ch in enumerate(snippet):
                if ch in "([{":
                    depth += 1
                elif ch in ")]}":
                    if depth == 0:
                        first_arg_end = i
                        break
                    depth -= 1
                elif ch == "," and depth == 0:
                    first_arg_end = i
                    break
            first_arg = snippet[:first_arg_end]
            for banned in self.BANNED_IN_USER_STRINGS:
                assert banned not in first_arg, (
                    f"showChatStatusBanner literal contains banned jargon "
                    f"{banned!r}: {first_arg[:120]!r}. "
                    f"Users flagged this on 2026-04-18 — use plain language."
                )

    def test_no_jargon_in_setstatus_or_alert(self, js_text: str):
        """Same rule for alert() and setStatus() in the composer-attach flow."""
        import re
        for fn in ("alert", "setStatus"):
            for match in re.finditer(fn + r"\s*\(", js_text):
                start = match.end()
                snippet = js_text[start:start + 250]
                for banned in self.BANNED_IN_USER_STRINGS:
                    assert banned not in snippet, (
                        f"{fn}(...) at offset {start} contains banned jargon "
                        f"{banned!r}. 2026-04-18 UX revision."
                    )

    def test_no_jargon_in_dom_content_assignments(self, js_text: str):
        """Catches the roster-receipt leak (found 2026-04-18 in the
        second scorecard audit): strings assigned to .textContent /
        .innerHTML / .innerText are user-visible just like
        showChatStatusBanner args, but the earlier guard didn't scan
        them. Classic pattern:
            headline.textContent = "Document ingested for RAG";
            sub.textContent = "Your document has been chunked, embedded…";
        The user sees this on every successful upload; the regression
        test must fail if any banned term reappears in such an assignment.

        Limitations: only scans static string literals and the leading
        portion of template literals. Dynamic computed strings (via a
        variable or a function return) aren't analyzed — if you really
        want to hide jargon from the guard you can put it in a const.
        That tradeoff is acceptable because the common regression path
        is hardcoded copy, not computed content.
        """
        import re
        # Match `.textContent = "..."` / `.innerHTML = "..."` /
        # `.innerText = "..."` / `.textContent = \`...\``. Non-greedy to
        # the next matching quote; we don't try to handle escape
        # sequences perfectly — banned terms don't contain escapable
        # characters so the rough match is sufficient.
        pattern = re.compile(
            r"\.(?:textContent|innerHTML|innerText)\s*=\s*"
            r"(?:"
            r'"([^"]{0,500})"'        # double-quoted
            r"|'([^']{0,500})'"       # single-quoted
            r"|`([^`]{0,500})`"       # template literal
            r")",
        )
        offenders: list[tuple[int, str, str]] = []
        for m in pattern.finditer(js_text):
            text = m.group(1) or m.group(2) or m.group(3) or ""
            for banned in self.BANNED_IN_USER_STRINGS:
                if banned in text:
                    offenders.append((m.start(), banned, text[:120]))
        assert not offenders, (
            "User-visible DOM assignments contain banned jargon. Users "
            "see these strings on the page:\n"
            + "\n".join(
                f"  at offset {off}: {term!r} → {sample!r}"
                for off, term, sample in offenders[:5]
            )
            + "\n\nRewrite to plain English; if the dev-facing term must "
              "stay (e.g. for support diagnostics), move it to console.debug."
        )


class TestSendBtnReenabledBeforeSendMessage:
    """Regression for the 2026-04-17 stuck-state bug.

    `sendMessageWithAttachment` disables sendBtn during the upload phase.
    The original `sendMessage` has `if (sendBtn.disabled) return;` as its
    first check — so if we call sendMessage() WITHOUT re-enabling first,
    the message silently drops. User sees: upload succeeded, chat turn
    never fired, button stuck disabled, input stuck disabled, no error
    anywhere.

    Evidence the fix is in place:
      - After `await uploadStagedAttachmentForInstantRag()` and BEFORE
        `sendMessage()` call, both `sendBtn.disabled = false` and
        `inputEl.disabled = false` must run.
      - Failure/catch path must also restore both controls.
    """

    def test_reenable_before_send_in_success_path(self, js_text: str):
        """Look for the specific sequence: await upload → re-enable →
        sendMessage. Mangled ordering means the send message can bail
        and the stuck state returns."""
        # Find the success path. Use a stable anchor ("await upload...")
        # and verify both re-enables appear before sendMessage() in the
        # next ~20 lines.
        anchor = "await uploadStagedAttachmentForInstantRag"
        idx = js_text.find(anchor)
        assert idx >= 0, "upload helper call site missing from send flow"
        window = js_text[idx:idx + 2000]
        # Both re-enables must appear in the window.
        assert "sendBtn.disabled = false" in window, (
            "Success path doesn't re-enable sendBtn before sendMessage() — "
            "sendMessage's early-return on sendBtn.disabled will silently "
            "drop the user's message. This is the 2026-04-17 stuck-state bug."
        )
        assert "inputEl.disabled = false" in window, (
            "Success path doesn't re-enable inputEl before sendMessage() — "
            "user sees their typed message persist in a disabled field, "
            "can't edit or retry."
        )
        # Order check: re-enables must come BEFORE the sendMessage() call.
        # Use a regex anchored to statement position ("sendMessage()" at the
        # start of a line after whitespace) so we don't match the string
        # inside the explanatory comment above the call.
        import re
        stmt_match = re.search(r"^\s+sendMessage\(\);", window, re.MULTILINE)
        assert stmt_match, "sendMessage() call missing from success path"
        send_idx = stmt_match.start()
        reen_idx = window.find("sendBtn.disabled = false")
        assert 0 <= reen_idx < send_idx, (
            f"sendBtn.disabled = false (at {reen_idx}) must appear BEFORE "
            f"sendMessage() call (at {send_idx}) — otherwise sendMessage "
            f"bails on its own disabled check."
        )

    def test_catch_path_restores_both_controls(self, js_text: str):
        """Upload failure must leave BOTH the button and the input editable
        so the user can remove the file / type a different message / retry.
        Pre-fix, the catch only restored sendBtn — inputEl stayed disabled."""
        # Look for the catch block in sendMessageWithAttachment. Window
        # widened from 1000→1500 chars because the user-friendly error
        # message strings (2026-04-18) pushed the re-enable lines past
        # the old 1000-char horizon.
        idx = js_text.find('console.error("[composer-attach] upload failed:')
        assert idx >= 0, "catch block signature missing"
        window = js_text[idx:idx + 1500]
        assert "sendBtn.disabled = false" in window, (
            "Catch path doesn't re-enable sendBtn — user stuck with no send button."
        )
        assert "inputEl.disabled = false" in window, (
            "Catch path doesn't re-enable inputEl — user can't edit / retry."
        )


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
