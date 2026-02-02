/** Chat API response when polling for completion */
interface ChatResponse {
  status: string;
  message: string | null;
  plan?: unknown;
  thinking_log?: string[];
  response_source?: string;
  model_used?: string | null;
  llm_error?: string | null;
  sources?: SourceItem[];
}

/** Single RAG source (when backend provides sources array) */
interface SourceItem {
  document_name?: string;
  page_number?: number | null;
  text?: string;
  index?: number;
}

/** Parsed source from "Sources:" block or API response.sources (RAG) */
interface ParsedSource {
  index: number;
  document_name: string;
  page_number: number | null;
  snippet: string;
  source_type?: string | null;
  match_score?: number | null;
  confidence?: number | null;
}

/** Chat config API response */
interface ChatConfigResponse {
  prompts?: { first_gen_system?: string; first_gen_user_template?: string };
  llm?: { provider?: string; model?: string; temperature?: number };
  parser?: { patient_keywords?: string[] };
}

/** POST /chat response */
interface ChatPostResponse {
  correlation_id: string;
}

/** GET /chat/history/recent or most-helpful-searches */
interface HistoryTurnItem {
  correlation_id: string;
  question: string;
  created_at: string | null;
}

/** GET /chat/history/most-helpful-documents (sorted by distinct liked turns, no counts shown) */
interface HistoryDocumentItem {
  document_name: string;
}

const API_BASE =
  typeof window !== "undefined" &&
  window.API_BASE &&
  window.API_BASE.startsWith("http")
    ? window.API_BASE
    : "http://localhost:8000";

function el(id: string): HTMLElement {
  const e = document.getElementById(id);
  if (!e) throw new Error("Element not found: " + id);
  return e;
}

/** Normalize message text: collapse multiple newlines to single for tighter display. */
function normalizeMessageText(text: string): string {
  return (text ?? "").replace(/\n{2,}/g, "\n").trim();
}

/** Parse full message into body text and sources (from "Sources:" block). */
function parseMessageAndSources(fullMessage: string): {
  body: string;
  sources: ParsedSource[];
} {
  const raw = (fullMessage ?? "").trim();
  const sourcesIdx = raw.search(/\nSources:\s*\n/i);
  if (sourcesIdx === -1) {
    return { body: raw, sources: [] };
  }
  const body = raw.slice(0, sourcesIdx).trim();
  const afterSources = raw.slice(sourcesIdx).replace(/^\s*Sources:\s*\n/i, "").trim();
  const sources: ParsedSource[] = [];
  // Lines like "  [1] Doc Name (page 2) — snippet..."
  const lineRe = /^\s*\[\s*(\d+)\s*\]\s*(.+?)(?:\s*\(page\s+(\d+)\))?\s*[—–-]\s*(.+)$/gm;
  let m: RegExpExecArray | null;
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
function renderUserMessage(text: string): HTMLElement {
  const wrap = document.createElement("div");
  wrap.className = "message message--user";
  const bubble = document.createElement("div");
  bubble.className = "message-bubble";
  bubble.textContent = text;
  wrap.appendChild(bubble);
  return wrap;
}

/** Reusable: compact thinking line – streams in one line, collapses to summary when done. */
function renderThinkingBlock(
  initialLines: string[],
  opts?: { onExpand?: () => void }
): { el: HTMLElement; setPreview: (text: string) => void; addLine: (line: string) => void; done: (lineCount: number) => void } {
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
  const dotsEl = document.createElement("span");
  dotsEl.className = "thinking-dots";
  dotsEl.setAttribute("aria-hidden", "true");
  dotsEl.textContent = "...";
  const lineEl = document.createElement("span");
  lineEl.className = "thinking-rule";
  preview.appendChild(word);
  preview.appendChild(dotsEl);
  preview.appendChild(lineEl);

  const body = document.createElement("div");
  body.className = "thinking-body";
  initialLines.forEach((line) => {
    const div = document.createElement("div");
    div.className = "thinking-line";
    div.textContent = line;
    body.appendChild(div);
  });

  function collapse(): void {
    block.classList.add("collapsed");
    preview.setAttribute("aria-expanded", "false");
  }
  function toggle(): void {
    block.classList.toggle("collapsed");
    const isExp = !block.classList.contains("collapsed");
    preview.setAttribute("aria-expanded", String(isExp));
    if (isExp) opts?.onExpand?.();
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
    setPreview(text: string) {
      preview.textContent = text;
    },
    addLine(line: string) {
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
    done(lineCount: number) {
      dotsEl.remove(); // stop "..." animation
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
function renderAssistantMessage(text: string, isError?: boolean): HTMLElement {
  const wrap = document.createElement("div");
  wrap.className = "message message--assistant" + (isError ? " message--error" : "");
  const bubble = document.createElement("div");
  bubble.className = "message-bubble";
  bubble.textContent = normalizeMessageText(text);
  wrap.appendChild(bubble);
  return wrap;
}

const FEEDBACK_COMMENT_MAX_LENGTH = 500;

function svgIcon(className: string, paths: string[]): SVGElement {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("class", className);
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("fill", "none");
  svg.setAttribute("stroke", "currentColor");
  svg.setAttribute("stroke-width", "2");
  svg.setAttribute("stroke-linecap", "round");
  svg.setAttribute("stroke-linejoin", "round");
  paths.forEach((d) => {
    const p = document.createElementNS("http://www.w3.org/2000/svg", "path");
    p.setAttribute("d", d);
    svg.appendChild(p);
  });
  return svg;
}

/** Thumbs up icon (Feather-style). */
function thumbsUpIcon(className: string): SVGElement {
  return svgIcon(className, [
    "M14 9V5a3 3 0 0 0-3-3l-4 9v11h11.28a2 2 0 0 0 2-1.7l1.38-9a2 2 0 0 0-2-2.3zM7 22H4a2 2 0 0 1-2-2v-7a2 2 0 0 1 2-2h3",
  ]);
}

/** Thumbs down icon (Feather-style). */
function thumbsDownIcon(className: string): SVGElement {
  return svgIcon(className, [
    "M10 15v4a3 3 0 0 0 3 3l4-9V2H5.72a2 2 0 0 0-2 1.7l-1.38 9a2 2 0 0 0 2 2.3zm7-13h2.67A2.31 2.31 0 0 1 22 4v7a2.31 2.31 0 0 1-2.33 2H17",
  ]);
}

/** Copy icon (Feather-style: two overlapping rectangles). */
function copyIcon(className: string): SVGElement {
  return svgIcon(className, [
    "M16 4h4v4h-4V4z",
    "M20 10v10a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V10a2 2 0 0 1 2-2h12a2 2 0 0 1 2 2z",
    "M6 10v10h12V10H6z",
  ]);
}

/** Reusable: feedback bar (thumbs up/down, copy). Accepts correlationId and optional initial state; returns el + updateFeedback for post-submit UI update. */
function renderFeedback(
  correlationId: string,
  options?: { initialRating?: "up" | "down"; initialComment?: string }
): { el: HTMLElement; updateFeedback: (rating: "up" | "down", comment: string) => void } {
  const bar = document.createElement("div");
  bar.className = "feedback";

  const up = document.createElement("button");
  up.type = "button";
  up.setAttribute("aria-label", "Good response");
  up.appendChild(thumbsUpIcon("feedback-icon"));
  const down = document.createElement("button");
  down.type = "button";
  down.setAttribute("aria-label", "Bad response");
  down.appendChild(thumbsDownIcon("feedback-icon"));
  const copyBtn = document.createElement("button");
  copyBtn.type = "button";
  copyBtn.setAttribute("aria-label", "Copy");
  copyBtn.appendChild(copyIcon("feedback-icon"));
  copyBtn.addEventListener("click", () => {
    const msg = bar.closest(".chat-turn")?.querySelector(".message--assistant .message-bubble");
    if (msg?.textContent) {
      navigator.clipboard.writeText(msg.textContent).then(() => {
        const label = copyBtn.getAttribute("aria-label");
        copyBtn.setAttribute("aria-label", "Copied");
        const icon = copyBtn.querySelector(".feedback-icon");
        if (icon) copyBtn.removeChild(icon);
        const span = document.createElement("span");
        span.className = "feedback-copy-label";
        span.textContent = "Copied";
        copyBtn.appendChild(span);
        setTimeout(() => {
          copyBtn.removeChild(span);
          copyBtn.appendChild(copyIcon("feedback-icon"));
          if (label) copyBtn.setAttribute("aria-label", label);
        }, 1500);
      });
    }
  });

  const commentEl = document.createElement("div");
  commentEl.className = "feedback-comment";
  commentEl.style.display = "none";

  const commentForm = document.createElement("div");
  commentForm.className = "feedback-comment-form";
  commentForm.style.display = "none";
  const commentInput = document.createElement("textarea");
  commentInput.placeholder = "What went wrong? (optional)";
  commentInput.rows = 2;
  commentInput.maxLength = FEEDBACK_COMMENT_MAX_LENGTH;
  const btnRow = document.createElement("div");
  btnRow.className = "feedback-comment-buttons";
  const submitBtn = document.createElement("button");
  submitBtn.type = "button";
  submitBtn.textContent = "Submit";
  const cancelBtn = document.createElement("button");
  cancelBtn.type = "button";
  cancelBtn.textContent = "Cancel";
  btnRow.appendChild(submitBtn);
  btnRow.appendChild(cancelBtn);
  commentForm.appendChild(commentInput);
  commentForm.appendChild(btnRow);

  function setSelected(rating: "up" | "down" | null): void {
    up.classList.toggle("selected", rating === "up");
    down.classList.toggle("selected", rating === "down");
  }

  function setCommentVisible(text: string): void {
    commentEl.textContent = text;
    commentEl.style.display = text ? "block" : "none";
  }

  function disableThumbs(): void {
    up.disabled = true;
    down.disabled = true;
  }

  function updateFeedback(rating: "up" | "down", comment: string): void {
    setSelected(rating);
    setCommentVisible(comment);
    commentForm.style.display = "none";
    disableThumbs();
  }

  if (options?.initialRating) {
    setSelected(options.initialRating);
    if (options.initialComment) setCommentVisible(options.initialComment);
    disableThumbs();
  }

  up.addEventListener("click", () => {
    if (up.disabled) return;
    fetch(API_BASE + "/chat/feedback", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ correlation_id: correlationId, rating: "up", comment: null }),
    })
      .then((r) => {
        if (r.ok) updateFeedback("up", "");
      })
      .catch(() => {});
  });

  down.addEventListener("click", () => {
    if (down.disabled) return;
    commentForm.style.display = "block";
    commentInput.value = "";
    commentInput.focus();
  });

  cancelBtn.addEventListener("click", () => {
    commentForm.style.display = "none";
  });

  commentInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      submitBtn.click();
    }
  });

  submitBtn.addEventListener("click", () => {
    const comment = commentInput.value.trim().slice(0, FEEDBACK_COMMENT_MAX_LENGTH);
    fetch(API_BASE + "/chat/feedback", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        correlation_id: correlationId,
        rating: "down",
        comment: comment || null,
      }),
    })
      .then((r) => {
        if (r.ok) updateFeedback("down", comment);
      })
      .catch(() => {});
  });

  const leftGroup = document.createElement("div");
  leftGroup.className = "feedback-left";
  leftGroup.appendChild(up);
  leftGroup.appendChild(down);
  leftGroup.appendChild(commentEl);
  leftGroup.appendChild(commentForm);

  const actionsGroup = document.createElement("div");
  actionsGroup.className = "feedback-actions";
  actionsGroup.appendChild(copyBtn);

  bar.appendChild(leftGroup);
  bar.appendChild(actionsGroup);
  return { el: bar, updateFeedback };
}

/** Reusable: source citer – same look as thinking (word + line, muted, collapsed by default). */
function renderSourceCiter(sources: ParsedSource[]): HTMLElement {
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
      const parts: string[] = [];
      if (s.source_type != null && s.source_type !== "") parts.push(`Type: ${s.source_type}`);
      if (s.match_score != null) parts.push(`Match: ${Number(s.match_score).toFixed(2)}`);
      if (s.confidence != null) parts.push(`Confidence: ${Number(s.confidence).toFixed(2)}`);
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

function scrollToBottom(container: HTMLElement): void {
  container.scrollTop = container.scrollHeight;
}

function run(): void {
  const messagesEl = el("messages");
  const inputEl = el("input") as HTMLInputElement;
  const sendBtn = el("send") as HTMLButtonElement;
  const drawer = el("drawer");
  const drawerOverlay = el("drawerOverlay");
  const hamburger = el("hamburger");
  const drawerClose = el("drawerClose");

  function openDrawer(): void {
    drawer.classList.add("open");
    drawerOverlay.classList.add("open");
    loadChatConfig();
  }

  function closeDrawer(): void {
    drawer.classList.remove("open");
    drawerOverlay.classList.remove("open");
  }

  hamburger.addEventListener("click", openDrawer);
  drawerClose.addEventListener("click", closeDrawer);
  drawerOverlay.addEventListener("click", closeDrawer);

  function loadChatConfig(): void {
    fetch(API_BASE + "/chat/config")
      .then((r) => r.json() as Promise<ChatConfigResponse>)
      .then((data) => {
        const p = data.prompts ?? {};
        const sysEl = document.getElementById("promptFirstGenSystem");
        const userEl = document.getElementById("promptFirstGenUser");
        if (sysEl) sysEl.textContent = p.first_gen_system ?? "—";
        if (userEl) userEl.textContent = p.first_gen_user_template ?? "—";
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
        loadSidebarLlm(data);
      })
      .catch(() => {
        const sysEl = document.getElementById("promptFirstGenSystem");
        const llmEl = document.getElementById("configLlm");
        if (sysEl) sysEl.textContent = "Failed to load config.";
        if (llmEl) llmEl.textContent = "Failed to load config.";
      });
  }

  function loadSidebarLlm(config?: ChatConfigResponse): void {
    const el = document.getElementById("sidebarLlmLabel");
    if (!el) return;
    if (config?.llm) {
      const p = config.llm.provider ?? "—";
      const m = config.llm.model ?? "—";
      el.textContent = "LLM: " + p + " / " + m;
    } else {
      fetch(API_BASE + "/chat/config")
        .then((r) => r.json() as Promise<ChatConfigResponse>)
        .then((data) => {
          if (data.llm)
            el.textContent =
              "LLM: " + (data.llm.provider ?? "—") + " / " + (data.llm.model ?? "—");
        })
        .catch(() => {
          el.textContent = "LLM: —";
        });
    }
  }

  function loadSidebarHistory(): void {
    const recentList = document.getElementById("recentList");
    const helpfulList = document.getElementById("helpfulList");
    const documentsList = document.getElementById("documentsList");
    if (!recentList || !helpfulList || !documentsList) return;

    const snippet = (q: string, max = 50) =>
      (q ?? "").trim().slice(0, max) + ((q ?? "").length > max ? "…" : "");

    Promise.all([
      fetch(API_BASE + "/chat/history/recent?limit=10").then((r) =>
        r.json() as Promise<HistoryTurnItem[]>
      ),
      fetch(API_BASE + "/chat/history/most-helpful-searches?limit=10").then((r) =>
        r.json() as Promise<HistoryTurnItem[]>
      ),
      fetch(API_BASE + "/chat/history/most-helpful-documents?limit=10").then((r) =>
        r.json() as Promise<HistoryDocumentItem[]>
      ),
    ])
      .then(([recent, helpful, documents]) => {
        recentList.innerHTML = "";
        recent.forEach((item) => {
          const li = document.createElement("li");
          li.className = "recent-item";
          li.textContent = snippet(item.question);
          li.title = item.question;
          li.setAttribute("data-correlation-id", item.correlation_id);
          li.addEventListener("click", () => {
            const q = (item.question ?? "").trim();
            if (!q) return;
            inputEl.value = q;
            updateSendState();
            sendMessage();
          });
          recentList.appendChild(li);
        });

        helpfulList.innerHTML = "";
        helpful.forEach((item) => {
          const li = document.createElement("li");
          li.className = "helpful-item";
          li.textContent = snippet(item.question);
          li.title = item.question;
          li.setAttribute("data-correlation-id", item.correlation_id);
          li.addEventListener("click", () => {
            const q = (item.question ?? "").trim();
            if (!q) return;
            inputEl.value = q;
            updateSendState();
            sendMessage();
          });
          helpfulList.appendChild(li);
        });

        documentsList.innerHTML = "";
        documents.forEach((item) => {
          const li = document.createElement("li");
          li.className = "documents-item";
          li.textContent = item.document_name;
          li.title = item.document_name;
          documentsList.appendChild(li);
        });
      })
      .catch(() => {
        recentList.innerHTML = "";
        helpfulList.innerHTML = "";
        documentsList.innerHTML = "";
      });
  }

  function pollResponse(
    correlationId: string,
    onThinking: ((line: string) => void) | null,
    onStreamingMessage?: ((text: string) => void) | null
  ): Promise<ChatResponse> {
    return new Promise((resolve, reject) => {
      const maxAttempts = 120;
      let attempts = 0;
      const seenLines = new Set<string>();

      function poll(): void {
        fetch(API_BASE + "/chat/response/" + correlationId)
          .then((r) => r.json() as Promise<ChatResponse>)
          .then((data) => {
            if (data.thinking_log?.length && onThinking) {
              data.thinking_log.forEach((line: string) => {
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
  function streamResponse(
    correlationId: string,
    onThinking: ((line: string) => void) | null,
    onStreamingMessage: ((text: string) => void) | null
  ): Promise<ChatResponse> {
    if (typeof EventSource === "undefined") {
      return pollResponse(correlationId, onThinking, onStreamingMessage);
    }
    const streamUrl = API_BASE + "/chat/stream/" + encodeURIComponent(correlationId);
    return new Promise((resolve, reject) => {
      let messageSoFar = "";
      let resolved = false;
      const es = new EventSource(streamUrl);
      es.onmessage = (e: MessageEvent) => {
        try {
          const parsed = JSON.parse(e.data as string) as { event: string; data?: unknown };
          const ev = parsed.event;
          const data = parsed.data as Record<string, unknown> | undefined;
          // Debug: log when each SSE event arrives (ts_readable = when worker wrote it)
          const writtenAt = data?.ts_readable ?? data?.ts;
          if (typeof console !== "undefined") {
            console.log(`[stream] ${ev} received_at=${new Date().toISOString().slice(11, 23)} written_at=${String(writtenAt ?? "—")}`);
          }
          if (ev === "thinking" && data?.line != null && onThinking) {
            onThinking(String(data.line));
          } else if (ev === "message" && data?.chunk != null && onStreamingMessage) {
            messageSoFar += String(data.chunk);
            onStreamingMessage(messageSoFar);
          } else if (ev === "completed" && data != null) {
            resolved = true;
            es.close();
            resolve(data as unknown as ChatResponse);
          } else if (ev === "error" && data?.message != null) {
            resolved = true;
            es.close();
            reject(new Error(String(data.message)));
          }
        } catch (err) {
          resolved = true;
          es.close();
          reject(err instanceof Error ? err : new Error(String(err)));
        }
      };
      es.onerror = () => {
        es.close();
        if (resolved) return;
        pollResponse(correlationId, onThinking, onStreamingMessage).then(resolve).catch(reject);
      };
    });
  }

  const chatEmpty = document.getElementById("chatEmpty");

  function sendMessage(): void {
    const message = (inputEl.value ?? "").trim();
    if (!message) return;
    if (sendBtn.disabled) return; // already sending

    if (chatEmpty) chatEmpty.classList.add("hidden");

    // Collapse any previous turn's thinking blocks so only this turn's thinking is open
    messagesEl.querySelectorAll(".thinking-block").forEach((block) => {
      block.classList.add("collapsed");
      const p = block.querySelector(".thinking-preview");
      if (p) p.setAttribute("aria-expanded", "false");
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
    const thinkingLines: string[] = [];
    const { el: thinkingBlockEl, addLine: addThinkingLine, done: thinkingDone } = renderThinkingBlock(["Sending request…"]);
    turnWrap.appendChild(thinkingBlockEl);
    scrollToBottom(messagesEl);

    function addThinkingLineAndScroll(line: string): void {
      thinkingLines.push(line);
      addThinkingLine(line);
      scrollToBottom(messagesEl);
    }

    let messageWrapEl: HTMLElement | null = null;
    function onStreamingMessage(text: string): void {
      if (!messageWrapEl) {
        messageWrapEl = renderAssistantMessage(text);
        turnWrap.appendChild(messageWrapEl);
      } else {
        const bubble = messageWrapEl.querySelector(".message-bubble");
        if (bubble) bubble.textContent = normalizeMessageText(text);
      }
      scrollToBottom(messagesEl);
    }

    fetch(API_BASE + "/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message }),
    })
      .then((r) => r.json() as Promise<ChatPostResponse>)
      .then((postData) => {
        addThinkingLineAndScroll("Request sent. Waiting for worker…");
        const correlationId = postData.correlation_id;
        return streamResponse(correlationId, addThinkingLineAndScroll, onStreamingMessage).then(
          (streamData) => ({ streamData, correlationId })
        );
      })
      .then(({ streamData: data, correlationId }) => {
        // Final thinking lines if any not yet shown
        (data.thinking_log ?? []).forEach((line) => {
          if (!thinkingLines.includes(line)) addThinkingLineAndScroll(line);
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
          if (bubble) bubble.textContent = normalizeMessageText(body || "(No response)");
          if (data.llm_error) messageWrapEl.classList.add("message--error");
        } else {
          turnWrap.appendChild(renderAssistantMessage(body || "(No response)", !!data.llm_error));
        }

        // 4. Feedback (pass correlation_id for persistence)
        turnWrap.appendChild(renderFeedback(correlationId).el);

        // 5. Sources: prefer API response.sources (from RAG) so source cards show even when integrator drops them
        const sourceList: ParsedSource[] =
          data.sources && data.sources.length > 0
            ? (data.sources as Array<{ index?: number; document_name?: string; page_number?: number | null; text?: string; source_type?: string | null; match_score?: number | null; confidence?: number | null }>).map((s) => ({
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
        loadSidebarHistory();
      })
      .catch((err: Error) => {
        thinkingDone(thinkingLines.length);
        turnWrap.appendChild(
          renderAssistantMessage("Error: " + (err?.message ?? String(err)), true)
        );
        scrollToBottom(messagesEl);
      })
      .finally(() => {
        sendBtn.disabled = false;
        inputEl.disabled = false;
        updateSendState();
      });
  }

  function updateSendState(): void {
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

  loadSidebarHistory();
  loadSidebarLlm();
}

run();

export {};
