const API_BASE = typeof window !== "undefined" &&
    window.API_BASE &&
    window.API_BASE.startsWith("http")
    ? window.API_BASE
    : "http://localhost:8000";
function el(id) {
    const e = document.getElementById(id);
    if (!e)
        throw new Error("Element not found: " + id);
    return e;
}
/** Parse full message into body text and sources (from "Sources:" block). */
function parseMessageAndSources(fullMessage) {
    const raw = (fullMessage ?? "").trim();
    const sourcesIdx = raw.search(/\nSources:\s*\n/i);
    if (sourcesIdx === -1) {
        return { body: raw, sources: [] };
    }
    const body = raw.slice(0, sourcesIdx).trim();
    const afterSources = raw.slice(sourcesIdx).replace(/^\s*Sources:\s*\n/i, "").trim();
    const sources = [];
    // Lines like "  [1] Doc Name (page 2) — snippet..."
    const lineRe = /^\s*\[\s*(\d+)\s*\]\s*(.+?)(?:\s*\(page\s+(\d+)\))?\s*[—–-]\s*(.+)$/gm;
    let m;
    while ((m = lineRe.exec(afterSources)) !== null) {
        sources.push({
            index: parseInt(m[1], 10),
            document_name: m[2].trim(),
            page_number: m[3] != null ? parseInt(m[3], 10) : null,
            snippet: (m[4] ?? "").trim(),
        });
    }
    return { body, sources };
}
/** Reusable: user message bubble (right-aligned). */
function renderUserMessage(text) {
    const wrap = document.createElement("div");
    wrap.className = "message message--user";
    const bubble = document.createElement("div");
    bubble.className = "message-bubble";
    bubble.textContent = text;
    wrap.appendChild(bubble);
    return wrap;
}
/** Reusable: compact thinking line – streams in one line, collapses to summary when done. */
function renderThinkingBlock(initialLines, opts) {
    const block = document.createElement("div");
    block.className = "thinking-block thinking-block--compact collapsed";
    const preview = document.createElement("div");
    preview.className = "thinking-preview";
    preview.setAttribute("role", "button");
    preview.setAttribute("tabindex", "0");
    preview.setAttribute("aria-expanded", "false");
    const word = document.createElement("span");
    word.className = "thinking-word";
    word.textContent = "Thinking";
    const lineEl = document.createElement("span");
    lineEl.className = "thinking-rule";
    preview.appendChild(word);
    preview.appendChild(lineEl);
    const body = document.createElement("div");
    body.className = "thinking-body";
    initialLines.forEach((line) => {
        const div = document.createElement("div");
        div.className = "thinking-line";
        div.textContent = line;
        body.appendChild(div);
    });
    function collapse() {
        block.classList.add("collapsed");
        preview.setAttribute("aria-expanded", "false");
    }
    function toggle() {
        block.classList.toggle("collapsed");
        const isExp = !block.classList.contains("collapsed");
        preview.setAttribute("aria-expanded", String(isExp));
        if (isExp)
            opts?.onExpand?.();
    }
    preview.addEventListener("click", toggle);
    preview.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            toggle();
        }
    });
    block.appendChild(preview);
    block.appendChild(body);
    /* Start expanded when we have initial lines so "Sending request…" is visible */
    if (initialLines.length > 0) {
        block.classList.remove("collapsed");
        preview.setAttribute("aria-expanded", "true");
    }
    return {
        el: block,
        setPreview(text) {
            preview.textContent = text;
        },
        addLine(line) {
            const div = document.createElement("div");
            div.className = "thinking-line";
            div.textContent = line;
            body.appendChild(div);
            word.textContent = "Thinking";
            // Keep block open while streaming; show last lines (body has max-height + overflow)
            block.classList.remove("collapsed");
            preview.setAttribute("aria-expanded", "true");
            body.scrollTop = body.scrollHeight;
        },
        done(lineCount) {
            word.textContent = lineCount <= 1 ? "Thinking" : `Thinking (${lineCount})`;
            block.classList.add("thinking-block--done");
            // Stay open for a few seconds after final message, then collapse
            setTimeout(() => {
                collapse();
            }, 3000);
        },
    };
}
/** Reusable: assistant message bubble (left-aligned). */
function renderAssistantMessage(text, isError) {
    const wrap = document.createElement("div");
    wrap.className = "message message--assistant" + (isError ? " message--error" : "");
    const bubble = document.createElement("div");
    bubble.className = "message-bubble";
    bubble.textContent = text;
    wrap.appendChild(bubble);
    return wrap;
}
/** Reusable: feedback bar (thumbs up/down, copy). */
function renderFeedback() {
    const bar = document.createElement("div");
    bar.className = "feedback";
    const up = document.createElement("button");
    up.type = "button";
    up.setAttribute("aria-label", "Good response");
    up.textContent = "👍";
    const down = document.createElement("button");
    down.type = "button";
    down.setAttribute("aria-label", "Bad response");
    down.textContent = "👎";
    const copy = document.createElement("button");
    copy.type = "button";
    copy.setAttribute("aria-label", "Copy");
    copy.textContent = "Copy";
    copy.addEventListener("click", () => {
        const msg = bar.closest(".chat-turn")?.querySelector(".message--assistant .message-bubble");
        if (msg?.textContent) {
            navigator.clipboard.writeText(msg.textContent).then(() => {
                copy.textContent = "Copied";
                setTimeout(() => (copy.textContent = "Copy"), 1500);
            });
        }
    });
    bar.appendChild(up);
    bar.appendChild(down);
    bar.appendChild(copy);
    return bar;
}
/** Reusable: source citer – same look as thinking (word + line, muted, collapsed by default). */
function renderSourceCiter(sources) {
    const wrap = document.createElement("div");
    wrap.className = "source-citer collapsed";
    const preview = document.createElement("div");
    preview.className = "source-citer-preview";
    preview.setAttribute("role", "button");
    preview.setAttribute("tabindex", "0");
    preview.setAttribute("aria-expanded", "false");
    const word = document.createElement("span");
    word.className = "source-citer-word";
    word.textContent = sources.length === 1 ? "Sources (1)" : `Sources (${sources.length})`;
    const rule = document.createElement("span");
    rule.className = "source-citer-rule";
    preview.appendChild(word);
    preview.appendChild(rule);
    preview.addEventListener("click", () => {
        wrap.classList.toggle("collapsed");
        preview.setAttribute("aria-expanded", String(!wrap.classList.contains("collapsed")));
    });
    preview.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            wrap.classList.toggle("collapsed");
            preview.setAttribute("aria-expanded", String(!wrap.classList.contains("collapsed")));
        }
    });
    const body = document.createElement("div");
    body.className = "source-citer-body";
    sources.forEach((s) => {
        const item = document.createElement("div");
        item.className = "source-item";
        const doc = document.createElement("div");
        doc.className = "source-doc";
        doc.textContent = `[${s.index}] ${s.document_name}` + (s.page_number != null ? ` (page ${s.page_number})` : "");
        item.appendChild(doc);
        if (s.source_type != null || s.match_score != null || s.confidence != null) {
            const metaLine = document.createElement("div");
            metaLine.className = "source-meta";
            const parts = [];
            if (s.source_type != null && s.source_type !== "")
                parts.push(`Type: ${s.source_type}`);
            if (s.match_score != null)
                parts.push(`Match: ${Number(s.match_score).toFixed(2)}`);
            if (s.confidence != null)
                parts.push(`Confidence: ${Number(s.confidence).toFixed(2)}`);
            metaLine.textContent = parts.join(" · ");
            item.appendChild(metaLine);
        }
        if (s.snippet) {
            const meta = document.createElement("div");
            meta.className = "source-snippet";
            meta.textContent = s.snippet;
            item.appendChild(meta);
        }
        body.appendChild(item);
    });
    wrap.appendChild(preview);
    wrap.appendChild(body);
    return wrap;
}
function scrollToBottom(container) {
    container.scrollTop = container.scrollHeight;
}
function run() {
    const messagesEl = el("messages");
    const inputEl = el("input");
    const sendBtn = el("send");
    const drawer = el("drawer");
    const drawerOverlay = el("drawerOverlay");
    const hamburger = el("hamburger");
    const drawerClose = el("drawerClose");
    const btnConfig = document.getElementById("btnConfig");
    function openDrawer() {
        drawer.classList.add("open");
        drawerOverlay.classList.add("open");
        loadChatConfig();
    }
    function closeDrawer() {
        drawer.classList.remove("open");
        drawerOverlay.classList.remove("open");
    }
    hamburger.addEventListener("click", openDrawer);
    drawerClose.addEventListener("click", closeDrawer);
    drawerOverlay.addEventListener("click", closeDrawer);
    if (btnConfig)
        btnConfig.addEventListener("click", openDrawer);
    function loadChatConfig() {
        fetch(API_BASE + "/chat/config")
            .then((r) => r.json())
            .then((data) => {
            const p = data.prompts ?? {};
            const sysEl = document.getElementById("promptFirstGenSystem");
            const userEl = document.getElementById("promptFirstGenUser");
            if (sysEl)
                sysEl.textContent = p.first_gen_system ?? "—";
            if (userEl)
                userEl.textContent = p.first_gen_user_template ?? "—";
            const llm = data.llm ?? {};
            const llmEl = document.getElementById("configLlm");
            if (llmEl)
                llmEl.textContent =
                    "Provider: " + (llm.provider ?? "—") +
                        ", Model: " + (llm.model ?? "—") +
                        (llm.temperature != null ? ", Temp: " + llm.temperature : "");
            const parser = data.parser ?? {};
            const parserEl = document.getElementById("configParser");
            if (parserEl)
                parserEl.textContent =
                    "Patient keywords: " +
                        (parser.patient_keywords?.length
                            ? parser.patient_keywords.join(", ")
                            : "—");
        })
            .catch(() => {
            const sysEl = document.getElementById("promptFirstGenSystem");
            const llmEl = document.getElementById("configLlm");
            if (sysEl)
                sysEl.textContent = "Failed to load config.";
            if (llmEl)
                llmEl.textContent = "Failed to load config.";
        });
    }
    function pollResponse(correlationId, onThinking, onStreamingMessage) {
        return new Promise((resolve, reject) => {
            const maxAttempts = 120;
            let attempts = 0;
            const seenLines = new Set();
            function poll() {
                fetch(API_BASE + "/chat/response/" + correlationId)
                    .then((r) => r.json())
                    .then((data) => {
                    if (data.thinking_log?.length && onThinking) {
                        data.thinking_log.forEach((line) => {
                            if (!seenLines.has(line)) {
                                seenLines.add(line);
                                onThinking(line);
                            }
                        });
                    }
                    if (data.message != null && data.message !== "" && onStreamingMessage) {
                        onStreamingMessage(data.message);
                    }
                    if (data.status === "completed") {
                        resolve(data);
                        return;
                    }
                    attempts++;
                    if (attempts >= maxAttempts) {
                        reject(new Error("Timeout waiting for response"));
                        return;
                    }
                    setTimeout(poll, 400);
                })
                    .catch(reject);
            }
            poll();
        });
    }
    /** Live stream via SSE; falls back to polling if EventSource unavailable or stream fails. */
    function streamResponse(correlationId, onThinking, onStreamingMessage) {
        if (typeof EventSource === "undefined") {
            return pollResponse(correlationId, onThinking, onStreamingMessage);
        }
        const streamUrl = API_BASE + "/chat/stream/" + encodeURIComponent(correlationId);
        return new Promise((resolve, reject) => {
            let messageSoFar = "";
            let resolved = false;
            const es = new EventSource(streamUrl);
            es.onmessage = (e) => {
                try {
                    const parsed = JSON.parse(e.data);
                    const ev = parsed.event;
                    const data = parsed.data;
                    // Debug: log when each SSE event arrives (ts_readable = when worker wrote it)
                    const writtenAt = data?.ts_readable ?? data?.ts;
                    if (typeof console !== "undefined") {
                        console.log(`[stream] ${ev} received_at=${new Date().toISOString().slice(11, 23)} written_at=${String(writtenAt ?? "—")}`);
                    }
                    if (ev === "thinking" && data?.line != null && onThinking) {
                        onThinking(String(data.line));
                    }
                    else if (ev === "message" && data?.chunk != null && onStreamingMessage) {
                        messageSoFar += String(data.chunk);
                        onStreamingMessage(messageSoFar);
                    }
                    else if (ev === "completed" && data != null) {
                        resolved = true;
                        es.close();
                        resolve(data);
                    }
                    else if (ev === "error" && data?.message != null) {
                        resolved = true;
                        es.close();
                        reject(new Error(String(data.message)));
                    }
                }
                catch (err) {
                    resolved = true;
                    es.close();
                    reject(err instanceof Error ? err : new Error(String(err)));
                }
            };
            es.onerror = () => {
                es.close();
                if (resolved)
                    return;
                pollResponse(correlationId, onThinking, onStreamingMessage).then(resolve).catch(reject);
            };
        });
    }
    const chatEmpty = document.getElementById("chatEmpty");
    function sendMessage() {
        const message = (inputEl.value ?? "").trim();
        if (!message)
            return;
        if (sendBtn.disabled)
            return; // already sending
        if (chatEmpty)
            chatEmpty.classList.add("hidden");
        // Collapse any previous turn's thinking blocks so only this turn's thinking is open
        messagesEl.querySelectorAll(".thinking-block").forEach((block) => {
            block.classList.add("collapsed");
            const p = block.querySelector(".thinking-preview");
            if (p)
                p.setAttribute("aria-expanded", "false");
        });
        // 1. User message
        const turnWrap = document.createElement("div");
        turnWrap.className = "chat-turn";
        turnWrap.appendChild(renderUserMessage(message));
        messagesEl.appendChild(turnWrap);
        scrollToBottom(messagesEl);
        inputEl.value = "";
        updateSendState();
        sendBtn.disabled = true;
        inputEl.disabled = true;
        // 2. Thinking block (compact line, streams then collapses)
        const thinkingLines = [];
        const { el: thinkingBlockEl, addLine: addThinkingLine, done: thinkingDone } = renderThinkingBlock(["Sending request…"]);
        turnWrap.appendChild(thinkingBlockEl);
        scrollToBottom(messagesEl);
        function addThinkingLineAndScroll(line) {
            thinkingLines.push(line);
            addThinkingLine(line);
            scrollToBottom(messagesEl);
        }
        let messageWrapEl = null;
        function onStreamingMessage(text) {
            if (!messageWrapEl) {
                messageWrapEl = renderAssistantMessage(text);
                turnWrap.appendChild(messageWrapEl);
            }
            else {
                const bubble = messageWrapEl.querySelector(".message-bubble");
                if (bubble)
                    bubble.textContent = text;
            }
            scrollToBottom(messagesEl);
        }
        fetch(API_BASE + "/chat", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ message }),
        })
            .then((r) => r.json())
            .then((data) => {
            addThinkingLineAndScroll("Request sent. Waiting for worker…");
            return streamResponse(data.correlation_id, addThinkingLineAndScroll, onStreamingMessage);
        })
            .then((data) => {
            // Final thinking lines if any not yet shown
            (data.thinking_log ?? []).forEach((line) => {
                if (!thinkingLines.includes(line))
                    addThinkingLineAndScroll(line);
            });
            const fullMessage = data.message ?? "(No message)";
            const { body, sources } = parseMessageAndSources(fullMessage);
            if (data.response_source === "llm" && data.model_used) {
                addThinkingLineAndScroll("Model: " + data.model_used);
            }
            if (data.response_source === "stub" && data.llm_error) {
                addThinkingLineAndScroll("LLM failed (stub used): " + data.llm_error);
            }
            thinkingDone(thinkingLines.length);
            // 3. Assistant message (create or update streaming bubble with final body)
            if (messageWrapEl) {
                const bubble = messageWrapEl.querySelector(".message-bubble");
                if (bubble)
                    bubble.textContent = body || "(No response)";
                if (data.llm_error)
                    messageWrapEl.classList.add("message--error");
            }
            else {
                turnWrap.appendChild(renderAssistantMessage(body || "(No response)", !!data.llm_error));
            }
            // 4. Feedback
            turnWrap.appendChild(renderFeedback());
            // 5. Sources: prefer API response.sources (from RAG) so source cards show even when integrator drops them
            const sourceList = data.sources && data.sources.length > 0
                ? data.sources.map((s) => ({
                    index: s.index ?? 0,
                    document_name: s.document_name ?? "document",
                    page_number: s.page_number ?? null,
                    snippet: (s.text ?? "").slice(0, 200),
                    source_type: s.source_type ?? null,
                    match_score: s.match_score ?? null,
                    confidence: s.confidence ?? null,
                }))
                : sources.length > 0
                    ? sources.map((s) => ({
                        index: s.index ?? 0,
                        document_name: s.document_name ?? "document",
                        page_number: s.page_number ?? null,
                        snippet: (s.snippet ?? "").slice(0, 120),
                        source_type: null,
                        match_score: null,
                        confidence: null,
                    }))
                    : [];
            if (sourceList.length > 0) {
                turnWrap.appendChild(renderSourceCiter(sourceList));
            }
            scrollToBottom(messagesEl);
        })
            .catch((err) => {
            thinkingDone(thinkingLines.length);
            turnWrap.appendChild(renderAssistantMessage("Error: " + (err?.message ?? String(err)), true));
            scrollToBottom(messagesEl);
        })
            .finally(() => {
            sendBtn.disabled = false;
            inputEl.disabled = false;
            updateSendState();
        });
    }
    function updateSendState() {
        const hasText = (inputEl.value ?? "").trim().length > 0;
        sendBtn.classList.toggle("active", hasText);
    }
    inputEl.addEventListener("input", updateSendState);
    inputEl.addEventListener("keydown", (e) => {
        if (e.key === "Enter" && !e.shiftKey) {
            e.preventDefault();
            sendMessage();
        }
    });
    sendBtn.addEventListener("click", () => sendMessage());
    updateSendState();
}
run();
export {};
