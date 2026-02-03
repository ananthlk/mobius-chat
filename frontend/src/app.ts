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
  source_confidence_strip?: string;
}

/** Single RAG source (when backend provides sources array) */
interface SourceItem {
  document_id?: string | null;
  document_name?: string;
  page_number?: number | null;
  text?: string;
  index?: number;
}

/** Parsed source from "Sources:" block or API response.sources (RAG) */
interface ParsedSource {
  index: number;
  document_id?: string | null;
  document_name: string;
  page_number: number | null;
  snippet: string;
  source_type?: string | null;
  match_score?: number | null;
  confidence?: number | null;
}

/** Chat config API response (GET /chat/config, also after PATCH) */
interface ChatConfigResponse {
  config_sha?: string;
  prompts?: {
    first_gen_system?: string;
    first_gen_user_template?: string;
    decompose_system?: string;
    decompose_user_template?: string;
    rag_answering_user_template?: string;
    integrator_system?: string;
    integrator_user_template?: string;
    integrator_repair_system?: string;
    consolidator_factual_max?: number;
    consolidator_canonical_min?: number;
    integrator_factual_system?: string;
    integrator_canonical_system?: string;
    integrator_blended_system?: string;
  };
  llm?: {
    provider?: string;
    model?: string;
    temperature?: number;
    vertex_project_id?: string | null;
    vertex_location?: string;
    vertex_model?: string;
    ollama_base_url?: string;
    ollama_model?: string;
    ollama_num_predict?: number;
  };
  parser?: { patient_keywords?: string[]; decomposition_separators?: string[] };
}

/** POST /chat response */
interface ChatPostResponse {
  correlation_id: string;
  thread_id?: string | null;
}

/** GET /chat/history/recent or most-helpful-searches */
interface HistoryTurnItem {
  correlation_id: string;
  question: string;
  created_at: string | null;
}

/** GET /chat/history/most-helpful-documents (sorted by distinct liked turns; document_id when present) */
interface HistoryDocumentItem {
  document_name: string;
  document_id?: string | null;
  /** Number of distinct liked answers that cited this document */
  cited_in_count?: number;
}

/** GET /chat/config/history list entry */
interface ConfigHistoryEntry {
  config_sha: string;
  created_at: string;
}

/** Section intent for visibility rules; LLM only classifies, renderer decides visibility. */
const SECTION_INTENTS = ["process", "requirements", "definitions", "exceptions", "references"] as const;
type SectionIntent = (typeof SECTION_INTENTS)[number];

function isSectionIntent(s: unknown): s is SectionIntent {
  return typeof s === "string" && SECTION_INTENTS.includes(s as SectionIntent);
}

/** AnswerCard JSON from consolidator (FACTUAL / CANONICAL / BLENDED) */
interface AnswerCardSection {
  intent?: SectionIntent;
  label: string;
  bullets: string[];
}
interface AnswerCardCitation {
  id: string;
  doc_title: string;
  locator: string;
  snippet: string;
}
interface AnswerCardFollowup {
  question: string;
  reason: string;
  field: string;
}
interface AnswerCard {
  mode: "FACTUAL" | "CANONICAL" | "BLENDED";
  direct_answer: string;
  sections: AnswerCardSection[];
  required_variables?: string[];
  confidence_note?: string;
  citations?: AnswerCardCitation[];
  followups?: AnswerCardFollowup[];
}

import {
  createAuthService,
  localStorageAdapter,
  createAuthModal,
  createPreferencesModal,
  createUserMenu,
  AUTH_STYLES,
  PREFERENCES_MODAL_STYLES,
  USER_MENU_STYLES,
  STORAGE_KEYS,
} from "@mobius/auth";

/** Auth user shape used for sidebar display (matches @mobius/auth UserProfile). */
interface AuthUser {
  preferred_name?: string;
  first_name?: string;
  display_name?: string;
  email?: string;
}

const API_BASE =
  typeof window !== "undefined" &&
  window.API_BASE &&
  window.API_BASE.startsWith("http")
    ? window.API_BASE
    : "http://localhost:8000";

const apiBase = `${API_BASE}/api/v1`;
const auth = createAuthService({ apiBase, storage: localStorageAdapter });

function getAuthHeaders(): Record<string, string> {
  const token = localStorage.getItem(STORAGE_KEYS.accessToken);
  if (token) return { Authorization: `Bearer ${token}` };
  return {};
}

function el(id: string): HTMLElement {
  const e = document.getElementById(id);
  if (!e) throw new Error("Element not found: " + id);
  return e;
}

/** Normalize message text: collapse multiple newlines to single for tighter display. */
function normalizeMessageText(text: string): string {
  return (text ?? "").replace(/\n{2,}/g, "\n").trim();
}

const MAX_SECTIONS = 4;
const MAX_BULLETS_PER_SECTION = 4;

/** Find the end index of the root JSON object starting at start (brace-matching). */
function findMatchingCloseBrace(str: string, start: number): number {
  let depth = 0;
  let inString = false;
  let escape = false;
  let quote = "";
  for (let i = start; i < str.length; i++) {
    const c = str[i];
    if (escape) {
      escape = false;
      continue;
    }
    if (inString) {
      if (c === "\\") escape = true;
      else if (c === quote) inString = false;
      continue;
    }
    if (c === '"' || c === "'") {
      inString = true;
      quote = c;
      continue;
    }
    if (c === "{") depth++;
    else if (c === "}") {
      depth--;
      if (depth === 0) return i;
    }
  }
  return -1;
}

/** Try to parse message as AnswerCard JSON. Returns card or null. Extracts JSON from body if needed. */
function tryParseAnswerCard(message: string): AnswerCard | null {
  if (!message || !message.trim()) return null;
  let raw = message.trim();
  if (raw.startsWith("```")) {
    const lines = raw.split("\n");
    if (lines[0].startsWith("```")) lines.shift();
    if (lines.length > 0 && lines[lines.length - 1].trim() === "```") lines.pop();
    raw = lines.join("\n").trim();
  }
  const parseOne = (str: string): AnswerCard | null => {
    try {
      const data = JSON.parse(str) as Record<string, unknown>;
      if (data.mode !== "FACTUAL" && data.mode !== "CANONICAL" && data.mode !== "BLENDED") return null;
      if (typeof data.direct_answer !== "string") return null;
      if (!Array.isArray(data.sections)) return null;
      const rawSections = (data.sections as Array<{ intent?: unknown; label?: string; bullets?: string[] }>).slice(0, MAX_SECTIONS);
      const sections: AnswerCardSection[] = rawSections.map((sec) => ({
        intent: isSectionIntent(sec.intent) ? sec.intent : "process",
        label: typeof sec.label === "string" ? sec.label : "",
        bullets: Array.isArray(sec.bullets) ? sec.bullets : [],
      }));
      return {
        mode: data.mode,
        direct_answer: data.direct_answer,
        sections,
        required_variables: Array.isArray(data.required_variables) ? data.required_variables as string[] : [],
        confidence_note: typeof data.confidence_note === "string" ? data.confidence_note : undefined,
        citations: Array.isArray(data.citations) ? data.citations as AnswerCardCitation[] : undefined,
        followups: Array.isArray(data.followups) ? data.followups as AnswerCardFollowup[] : undefined,
      };
    } catch {
      return null;
    }
  };
  if (raw.startsWith("{")) {
    const card = parseOne(raw);
    if (card) return card;
    const close = findMatchingCloseBrace(raw, 0);
    if (close !== -1) {
      const card2 = parseOne(raw.slice(0, close + 1));
      if (card2) return card2;
    }
    const fixed = raw.replace(/\}\]\}\],/g, "}],").replace(/\}\]\},/g, "}],");
    if (fixed !== raw) {
      const card3 = parseOne(fixed);
      if (card3) return card3;
    }
  }
  const modeRe = /["']mode["']\s*:\s*["'](FACTUAL|CANONICAL|BLENDED)["']/;
  const m = raw.match(modeRe);
  if (m) {
    const idx = raw.indexOf(m[0]);
    let start = raw.lastIndexOf("{", idx);
    if (start !== -1) {
      const end = findMatchingCloseBrace(raw, start);
      if (end !== -1) {
        const card = parseOne(raw.slice(start, end + 1));
        if (card) return card;
      }
    }
  }
  return null;
}

/** Build visible vs hidden section lists by mode (authoritative visibility rules). */
function splitSectionsByVisibility(
  sections: AnswerCardSection[],
  mode: AnswerCard["mode"]
): { visible: AnswerCardSection[]; hidden: AnswerCardSection[] } {
  const all = sections.slice(0, MAX_SECTIONS);
  if (mode === "FACTUAL") {
    return { visible: [], hidden: all };
  }
  if (mode === "CANONICAL") {
    return { visible: all, hidden: [] };
  }
  const requirements = all.filter((s) => (s.intent ?? "process") === "requirements");
  const hidden = all.filter((s) => {
    const i = s.intent ?? "process";
    return i === "process" || i === "definitions" || i === "exceptions" || i === "references";
  });
  return { visible: requirements, hidden };
}

/** Render one section (label + bullets) into a div.answer-card-section. */
function renderOneSection(sec: AnswerCardSection): HTMLElement {
  const sectionEl = document.createElement("div");
  sectionEl.className = "answer-card-section";
  const labelEl = document.createElement("div");
  labelEl.className = "answer-card-section-label";
  labelEl.textContent = sec.label || "";
  sectionEl.appendChild(labelEl);
  const bullets = (sec.bullets ?? []).slice(0, MAX_BULLETS_PER_SECTION);
  bullets.forEach((b) => {
    const li = document.createElement("div");
    li.className = "answer-card-bullet";
    li.textContent = b;
    sectionEl.appendChild(li);
  });
  if (bullets.length < (sec.bullets?.length ?? 0)) {
    const more = document.createElement("div");
    more.className = "answer-card-more";
    more.textContent = "Show more";
    more.setAttribute("aria-label", "Show more bullets");
    sectionEl.appendChild(more);
  }
  return sectionEl;
}

/** Render AnswerCard with mode-based visibility and single "Show details" for hidden sections. */
function renderAnswerCard(card: AnswerCard, isError?: boolean, sourceConfidenceStrip?: string): HTMLElement {
  const wrap = document.createElement("div");
  wrap.className =
    "message message--assistant answer-card answer-card--" +
    card.mode.toLowerCase() +
    (isError ? " message--error" : "");

  const bubble = document.createElement("div");
  bubble.className = "message-bubble answer-card-bubble";

  const direct = document.createElement("div");
  direct.className = "answer-card-direct";
  direct.textContent = card.direct_answer;
  bubble.appendChild(direct);

  const metaRow = document.createElement("div");
  metaRow.className = "answer-card-meta-row";
  if (card.required_variables && card.required_variables.length > 0) {
    const dep = document.createElement("span");
    dep.className = "answer-card-depends";
    dep.textContent = "Depends on: " + card.required_variables.join(", ");
    metaRow.appendChild(dep);
  }
  if (card.followups && card.followups.length > 0 && metaRow.childNodes.length > 0) {
    const sep = document.createElement("span");
    sep.className = "answer-card-meta-sep";
    sep.textContent = " · ";
    metaRow.appendChild(sep);
  }
  if (card.followups && card.followups.length > 0) {
    const confirmLabel = document.createElement("span");
    confirmLabel.className = "answer-card-confirm-label";
    confirmLabel.textContent = "Confirm";
    metaRow.appendChild(confirmLabel);
    card.followups.slice(0, 2).forEach((f) => {
      const chip = document.createElement("button");
      chip.type = "button";
      chip.className = "answer-card-followup-chip";
      chip.textContent = f.question || f.reason || f.field || "";
      chip.setAttribute("aria-label", chip.textContent);
      metaRow.appendChild(chip);
    });
  }
  if (metaRow.childNodes.length > 0) bubble.appendChild(metaRow);

  if (sourceConfidenceStrip != null && sourceConfidenceStrip !== "") {
    const badgeWrap = document.createElement("div");
    badgeWrap.className = "answer-card-badge-wrap";
    badgeWrap.appendChild(renderConfidenceBadge(sourceConfidenceStrip));
    bubble.appendChild(badgeWrap);
  }

  const { visible, hidden } = splitSectionsByVisibility(card.sections ?? [], card.mode);

  visible.forEach((sec) => bubble.appendChild(renderOneSection(sec)));

  if (hidden.length > 0) {
    const detailsBlock = document.createElement("div");
    detailsBlock.className = "answer-card-details";
    detailsBlock.setAttribute("aria-hidden", "true");
    hidden.forEach((sec) => detailsBlock.appendChild(renderOneSection(sec)));
    bubble.appendChild(detailsBlock);

    const toggleBtn = document.createElement("button");
    toggleBtn.type = "button";
    toggleBtn.className = "answer-card-show-details";
    toggleBtn.textContent = "Show details";
    toggleBtn.setAttribute("aria-label", "Show details");
    toggleBtn.setAttribute("aria-expanded", "false");
    toggleBtn.addEventListener("click", () => {
      const expanded = detailsBlock.classList.toggle("answer-card-details--expanded");
      detailsBlock.setAttribute("aria-hidden", expanded ? "false" : "true");
      toggleBtn.setAttribute("aria-expanded", String(expanded));
      toggleBtn.textContent = expanded ? "Hide details" : "Show details";
      toggleBtn.setAttribute("aria-label", expanded ? "Hide details" : "Show details");
    });
    bubble.appendChild(toggleBtn);
  }

  if (card.confidence_note && card.confidence_note.trim()) {
    const note = document.createElement("div");
    note.className = "answer-card-confidence";
    note.textContent = card.confidence_note;
    bubble.appendChild(note);
  }

  wrap.appendChild(bubble);
  return wrap;
}

/** Trust badge: icon + 1–2 word label. Status chrome, not content. Doc state → badge. */
const CONFIDENCE_BADGE: Record<string, { icon: string; label: string }> = {
  approved_authoritative: { icon: "✔", label: "Approved" },
  approved_informational: { icon: "ℹ", label: "Informational" },
  pending: { icon: "⚠", label: "Unverified" },
  partial_pending: { icon: "⚠", label: "Unverified" },
  unverified: { icon: "⛔", label: "No source" },
};

/** Compact status badge (GitHub CI / Stripe style). Icon-first, muted color, content-fit. */
function renderConfidenceBadge(value: string): HTMLElement {
  const key = (value || "").trim() || "unverified";
  const { icon, label } = CONFIDENCE_BADGE[key] ?? CONFIDENCE_BADGE.unverified;
  const badge = document.createElement("span");
  badge.className = "confidence-badge confidence-badge--" + key;
  badge.setAttribute("aria-label", label);
  badge.setAttribute("role", "status");
  const iconEl = document.createElement("span");
  iconEl.className = "confidence-badge-icon";
  iconEl.setAttribute("aria-hidden", "true");
  iconEl.textContent = icon;
  const textEl = document.createElement("span");
  textEl.className = "confidence-badge-label";
  textEl.textContent = label;
  badge.appendChild(iconEl);
  badge.appendChild(textEl);
  return badge;
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

const PROGRESS_MAX_LINES = 3;

/** Vanishing progress stack: shows last 3 lines from real thinking_log. Removed from DOM when answer arrives. */
function renderProgressStack(): {
  el: HTMLElement;
  addLine: (line: string) => void;
} {
  const block = document.createElement("div");
  block.className = "progress-stack";
  block.setAttribute("aria-live", "polite");
  block.setAttribute("aria-label", "Progress");

  const linesContainer = document.createElement("div");
  linesContainer.className = "progress-stack-lines";
  const lineEls: HTMLElement[] = [];
  for (let i = 0; i < PROGRESS_MAX_LINES; i++) {
    const div = document.createElement("div");
    div.className = "progress-stack-line";
    div.textContent = "";
    lineEls.push(div);
    linesContainer.appendChild(div);
  }
  block.appendChild(linesContainer);

  const dotsEl = document.createElement("span");
  dotsEl.className = "progress-stack-dots";
  dotsEl.setAttribute("aria-hidden", "true");
  dotsEl.textContent = "...";

  const buffer: string[] = [];
  function addLine(line: string): void {
    const trimmed = (line ?? "").trim();
    if (!trimmed) return;
    buffer.push(trimmed);
    const last3 = buffer.slice(-PROGRESS_MAX_LINES);
    for (let i = 0; i < PROGRESS_MAX_LINES; i++) {
      const text = last3[i] ?? "";
      lineEls[i].textContent = text;
      lineEls[i].classList.toggle("empty", !text);
    }
    dotsEl.remove();
    const lastIdx = last3.length - 1;
    if (lastIdx >= 0) lineEls[lastIdx].appendChild(dotsEl);
  }

  return {
    el: block,
    addLine,
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

/** Render final assistant content: AnswerCard JSON or prose fallback. */
function renderAssistantContent(body: string, isError?: boolean, sourceConfidenceStrip?: string): HTMLElement {
  const card = tryParseAnswerCard(body);
  if (typeof console !== "undefined" && console.log) {
    console.log("[AnswerCard] renderAssistantContent: card=", card ? "yes (mode=" + card.mode + ")" : "no");
  }
  if (card) return renderAnswerCard(card, isError, sourceConfidenceStrip);
  const trimmed = (body ?? "").trim();
  if (trimmed.startsWith("{") && trimmed.length > 10) {
    console.warn("[AnswerCard] Invalid JSON, showing fallback. Raw:", trimmed.slice(0, 500));
    return renderAssistantMessage("Answer could not be displayed. Please try again.", isError);
  }
  if (typeof console !== "undefined" && console.log) {
    console.log("[AnswerCard] rendering as prose (plain text)");
  }
  return renderAssistantMessage(body, isError);
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
  svg.setAttribute("aria-hidden", "true");
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
      headers: { "Content-Type": "application/json", ...getAuthHeaders() },
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
      headers: { "Content-Type": "application/json", ...getAuthHeaders() },
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

/** Reusable: source citer – same look as thinking (word + line, muted, collapsed by default). Click source to open mini reader. Per-source: "Was this helpful and accurate?" thumbs + "Open document" link. */
function renderSourceCiter(
  sources: ParsedSource[],
  onSourceClick?: (s: ParsedSource) => void,
  correlationId?: string,
  initialRatings?: Record<number, "up" | "down">
): HTMLElement {
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
    item.className = "source-item" + (onSourceClick ? " source-item--clickable" : "");
    if (onSourceClick) {
      item.setAttribute("role", "button");
      item.setAttribute("tabindex", "0");
      item.title = "View document";
      item.addEventListener("click", (e) => {
        if (
          (e.target as HTMLElement).closest(".source-feedback-row") ||
          (e.target as HTMLElement).closest(".source-open-doc")
        )
          return;
        onSourceClick(s);
      });
      item.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          if (!(e.target as HTMLElement).closest(".source-feedback-row")) onSourceClick(s);
        }
      });
    }
    const doc = document.createElement("div");
    doc.className = "source-doc";
    doc.textContent = `[${s.index}] ${s.document_name}` + (s.page_number != null ? ` (page ${s.page_number})` : "");
    item.appendChild(doc);
    if (s.source_type != null || s.match_score != null || s.confidence != null) {
      const metaLine = document.createElement("div");
      metaLine.className = "source-meta";
      const parts: (string | HTMLElement)[] = [];
      if (s.source_type != null && s.source_type !== "") parts.push(`Type: ${s.source_type}`);
      if (s.match_score != null) {
        const matchNum = Number(s.match_score);
        const matchLabel = matchNum >= 0.8 ? "Strong match" : matchNum >= 0.5 ? "Moderate match" : "Weak match";
        const matchSpan = document.createElement("span");
        matchSpan.className = "source-meta-badge source-meta-badge--match";
        matchSpan.textContent = matchLabel;
        matchSpan.title = `Match: ${matchNum.toFixed(2)}`;
        parts.push(matchSpan);
      }
      if (s.confidence != null) {
        const confNum = Number(s.confidence);
        const confLabel = confNum >= 0.8 ? "High confidence" : confNum >= 0.5 ? "Medium confidence" : "Low confidence";
        const confSpan = document.createElement("span");
        confSpan.className = "source-meta-badge source-meta-badge--confidence";
        confSpan.textContent = confLabel;
        confSpan.title = `Confidence: ${confNum.toFixed(2)}`;
        parts.push(confSpan);
      }
      parts.forEach((p) => {
        if (typeof p === "string") {
          const t = document.createTextNode(p);
          metaLine.appendChild(t);
        } else {
          if (metaLine.childNodes.length) metaLine.appendChild(document.createTextNode(" · "));
          metaLine.appendChild(p);
        }
      });
      item.appendChild(metaLine);
    }
    if (s.snippet) {
      const meta = document.createElement("div");
      meta.className = "source-snippet";
      meta.textContent = s.snippet;
      item.appendChild(meta);
    }
    const sourceIndex = s.index >= 1 ? s.index : 1;
    const existingRating = initialRatings?.[sourceIndex];
    const feedbackRow = document.createElement("div");
    feedbackRow.className = "source-feedback-row";
    const question = document.createElement("span");
    question.className = "source-feedback-question";
    question.textContent = "Was this helpful and accurate?";
    const thumbsWrap = document.createElement("div");
    thumbsWrap.className = "source-feedback-thumbs";
    const upBtn = document.createElement("button");
    upBtn.type = "button";
    upBtn.setAttribute("aria-label", "Yes, helpful");
    upBtn.appendChild(thumbsUpIcon("source-feedback-icon"));
    const downBtn = document.createElement("button");
    downBtn.type = "button";
    downBtn.setAttribute("aria-label", "No, not helpful");
    downBtn.appendChild(thumbsDownIcon("source-feedback-icon"));
    thumbsWrap.appendChild(upBtn);
    thumbsWrap.appendChild(downBtn);
    feedbackRow.appendChild(question);
    feedbackRow.appendChild(thumbsWrap);
    item.appendChild(feedbackRow);
    if (existingRating) {
      upBtn.classList.toggle("selected", existingRating === "up");
      downBtn.classList.toggle("selected", existingRating === "down");
      upBtn.disabled = true;
      downBtn.disabled = true;
    } else if (correlationId) {
      upBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        e.preventDefault();
        fetch(API_BASE + "/chat/feedback/source", {
          method: "POST",
          headers: { "Content-Type": "application/json", ...getAuthHeaders() },
          body: JSON.stringify({ correlation_id: correlationId, source_index: sourceIndex, rating: "up" }),
        }).then((r) => {
          if (r.ok) {
            upBtn.classList.add("selected");
            downBtn.classList.remove("selected");
            upBtn.disabled = true;
            downBtn.disabled = true;
          }
        });
      });
      downBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        e.preventDefault();
        fetch(API_BASE + "/chat/feedback/source", {
          method: "POST",
          headers: { "Content-Type": "application/json", ...getAuthHeaders() },
          body: JSON.stringify({ correlation_id: correlationId, source_index: sourceIndex, rating: "down" }),
        }).then((r) => {
          if (r.ok) {
            downBtn.classList.add("selected");
            upBtn.classList.remove("selected");
            upBtn.disabled = true;
            downBtn.disabled = true;
          }
        });
      });
    }
    const ragUrl = onSourceClick ? getRagDocumentUrl(s.document_id, s.page_number) : null;
    if (ragUrl) {
      const linkWrap = document.createElement("div");
      linkWrap.className = "source-open-doc";
      const link = document.createElement("a");
      link.href = ragUrl;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      link.className = "source-open-doc-link";
      link.textContent = "Open full document";
      link.addEventListener("click", (e) => e.stopPropagation());
      linkWrap.appendChild(link);
      item.appendChild(linkWrap);
    }
    body.appendChild(item);
  });

  wrap.appendChild(preview);
  wrap.appendChild(body);
  return wrap;
}

/** RAG deep-link URL for Read tab (document + optional page). Same URL for "view document" and "open in new tab". */
function getRagDocumentUrl(documentId: string | null | undefined, pageNumber: number | null | undefined): string | null {
  const base = (typeof window !== "undefined" && window.RAG_APP_BASE) ? window.RAG_APP_BASE.trim() : "";
  if (!base || !documentId || !documentId.trim()) return null;
  const params = new URLSearchParams({ tab: "read", documentId: documentId.trim() });
  if (pageNumber != null) params.set("pageNumber", String(pageNumber));
  return `${base.replace(/\/$/, "")}?${params.toString()}`;
}

/** Open document: if RAG_APP_BASE and document_id, open RAG Read tab in new tab; else show snippet-only mini reader. */
function openDocumentOrSnippet(s: {
  document_id?: string | null;
  document_name: string;
  page_number?: number | null;
  snippet: string;
}): void {
  const url = getRagDocumentUrl(s.document_id, s.page_number);
  if (url) {
    window.open(url, "_blank", "noopener,noreferrer");
    return;
  }
  openMiniReaderSnippetOnly(s.document_name, s.page_number, s.snippet);
}

/** Mini reader: snippet only (no full-page fetch). Used when RAG_APP_BASE is not set. */
function openMiniReaderSnippetOnly(documentName: string, pageNumber: number | null | undefined, snippet: string): void {
  const docName = documentName || "Document";
  const title = pageNumber != null ? `${docName} (page ${pageNumber})` : docName;

  let overlay: HTMLElement | null = document.getElementById("mini-reader-overlay");
  if (!overlay) {
    overlay = document.createElement("div");
    overlay.id = "mini-reader-overlay";
    overlay.className = "mini-reader-overlay";
    overlay.setAttribute("aria-hidden", "true");
    const panel = document.createElement("div");
    panel.className = "mini-reader-panel";
    panel.setAttribute("role", "dialog");
    panel.setAttribute("aria-labelledby", "mini-reader-title");
    const header = document.createElement("div");
    header.className = "mini-reader-header";
    const titleEl = document.createElement("h2");
    titleEl.id = "mini-reader-title";
    titleEl.className = "mini-reader-title";
    const closeBtn = document.createElement("button");
    closeBtn.type = "button";
    closeBtn.className = "mini-reader-close";
    closeBtn.setAttribute("aria-label", "Close");
    closeBtn.textContent = "×";
    const contentEl = document.createElement("div");
    contentEl.className = "mini-reader-content";
    header.appendChild(titleEl);
    header.appendChild(closeBtn);
    panel.appendChild(header);
    panel.appendChild(contentEl);
    overlay.appendChild(panel);
    const overlayEl = overlay;

    closeBtn.addEventListener("click", () => {
      overlayEl.classList.remove("open");
      overlayEl.setAttribute("aria-hidden", "true");
    });
    overlayEl.addEventListener("click", (e) => {
      if (e.target === overlayEl) {
        overlayEl.classList.remove("open");
        overlayEl.setAttribute("aria-hidden", "true");
      }
    });
    document.body.appendChild(overlayEl);
  }

  if (!overlay) return;
  const titleEl = overlay.querySelector("#mini-reader-title") as HTMLElement;
  const contentEl = overlay.querySelector(".mini-reader-content") as HTMLElement;
  titleEl.textContent = title;
  contentEl.textContent = snippet || "(No snippet)";
  overlay.classList.add("open");
  overlay.setAttribute("aria-hidden", "false");
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
  const sidebar = document.getElementById("sidebar");
  const mainEl = document.querySelector(".main");
  const sidebarChevron = document.getElementById("sidebarChevron");

  function toggleSidebar(): void {
    if (!sidebar || !mainEl) return;
    const collapsed = sidebar.classList.toggle("sidebar--collapsed");
    mainEl.classList.toggle("sidebar-collapsed", collapsed);
    if (sidebarChevron) {
      sidebarChevron.setAttribute("aria-label", collapsed ? "Expand sidebar" : "Collapse sidebar");
      sidebarChevron.setAttribute("title", collapsed ? "Expand sidebar" : "Collapse sidebar");
    }
  }

  sidebarChevron?.addEventListener("click", toggleSidebar);

  function openDrawer(): void {
    drawer.classList.add("open");
    drawerOverlay.classList.add("open");
    loadChatConfig();
    loadConfigHistory();
  }

  function closeDrawer(): void {
    drawer.classList.remove("open");
    drawerOverlay.classList.remove("open");
  }

  hamburger.addEventListener("click", openDrawer);
  drawerClose.addEventListener("click", closeDrawer);
  drawerOverlay.addEventListener("click", closeDrawer);

  const drawerSaveConfig = document.getElementById("drawerSaveConfig");
  const drawerLoadConfig = document.getElementById("drawerLoadConfig");
  if (drawerSaveConfig) drawerSaveConfig.addEventListener("click", saveChatConfig);
  if (drawerLoadConfig) drawerLoadConfig.addEventListener("click", loadChatConfig);

  function getElValue(id: string): string {
    const el = getEl(id);
    if (!el || !("value" in el)) return "";
    return String((el as HTMLInputElement).value ?? "").trim();
  }

  function buildCopyTextForSection(section: string): string {
    switch (section) {
      case "parser":
        return (
          "Parser config:\npatient_keywords: " +
          getElValue("editParserKeywords") +
          "\ndecomposition_separators: " +
          getElValue("editParserSeparators")
        );
      case "planner": {
        const sys = getElValue("editDecomposeSystem");
        const user = getElValue("editDecomposeUserTemplate");
        return "--- decompose_system ---\n" + sys + "\n\n--- decompose_user_template (placeholder: {message}) ---\n" + user;
      }
      case "first_gen": {
        const sys = getElValue("editFirstGenSystem");
        const user = getElValue("editFirstGenUser");
        return "--- first_gen_system ---\n" + sys + "\n\n--- first_gen_user_template (placeholders: {message}, {plan_summary}) ---\n" + user;
      }
      case "rag_answering": {
        const t = getElValue("editRagAnsweringUserTemplate");
        return "--- rag_answering_user_template (placeholders: {context}, {question}) ---\n" + t;
      }
      case "integrator": {
        const sys = getElValue("editIntegratorSystem");
        const user = getElValue("editIntegratorUserTemplate");
        const repair = getElValue("editIntegratorRepairSystem");
        return (
          "--- integrator_system ---\n" +
          sys +
          "\n\n--- integrator_user_template (placeholder: {consolidator_input_json}) ---\n" +
          user +
          "\n\n--- integrator_repair_system ---\n" +
          repair
        );
      }
      case "consolidator": {
        const factualMax = getElValue("editConsolidatorFactualMax");
        const canonicalMin = getElValue("editConsolidatorCanonicalMin");
        const factual = getElValue("editIntegratorFactualSystem");
        const canonical = getElValue("editIntegratorCanonicalSystem");
        const blended = getElValue("editIntegratorBlendedSystem");
        return (
          "consolidator_factual_max: " +
          factualMax +
          "\nconsolidator_canonical_min: " +
          canonicalMin +
          "\n\n--- integrator_factual_system ---\n" +
          factual +
          "\n\n--- integrator_canonical_system ---\n" +
          canonical +
          "\n\n--- integrator_blended_system ---\n" +
          blended
        );
      }
      default:
        return "";
    }
  }

  document.querySelectorAll(".config-copy-prompt-btn").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      const section = (btn as HTMLElement).getAttribute("data-copy-section");
      if (!section) return;
      const text = buildCopyTextForSection(section);
      if (!text) return;
      navigator.clipboard.writeText(text).then(() => {
        const label = btn as HTMLButtonElement;
        const orig = label.textContent;
        label.textContent = "Copied";
        label.disabled = true;
        window.setTimeout(() => {
          label.textContent = orig;
          label.disabled = false;
        }, 1500);
      });
    });
  });

  function buildPayloadForSection(section: string): { llm?: Record<string, unknown>; parser?: Record<string, unknown>; prompts?: Record<string, unknown> } | null {
    const payload: { llm?: Record<string, unknown>; parser?: Record<string, unknown>; prompts?: Record<string, unknown> } = {};
    if (section === "parser") {
      const keywordsEl = getEl("editParserKeywords");
      const separatorsEl = getEl("editParserSeparators");
      payload.parser = {};
      if (keywordsEl?.value.trim())
        payload.parser.patient_keywords = (keywordsEl as HTMLInputElement).value.split(",").map((s) => s.trim()).filter(Boolean);
      if (separatorsEl?.value.trim())
        payload.parser.decomposition_separators = (separatorsEl as HTMLInputElement).value.split(/[,\n]/).map((s) => s.trim()).filter(Boolean);
      return Object.keys(payload.parser).length ? payload : null;
    }
    if (section === "planner") {
      payload.prompts = {
        decompose_system: getElValue("editDecomposeSystem"),
        decompose_user_template: getElValue("editDecomposeUserTemplate"),
      };
      return payload;
    }
    if (section === "first_gen") {
      payload.prompts = {
        first_gen_system: getElValue("editFirstGenSystem"),
        first_gen_user_template: getElValue("editFirstGenUser"),
      };
      return payload;
    }
    if (section === "rag_answering") {
      payload.prompts = { rag_answering_user_template: getElValue("editRagAnsweringUserTemplate") };
      return payload;
    }
    if (section === "integrator") {
      payload.prompts = {
        integrator_system: getElValue("editIntegratorSystem"),
        integrator_user_template: getElValue("editIntegratorUserTemplate"),
        integrator_repair_system: getElValue("editIntegratorRepairSystem"),
      };
      return payload;
    }
    if (section === "consolidator") {
      const prompts: Record<string, unknown> = {
        integrator_factual_system: getElValue("editIntegratorFactualSystem"),
        integrator_canonical_system: getElValue("editIntegratorCanonicalSystem"),
        integrator_blended_system: getElValue("editIntegratorBlendedSystem"),
      };
      const factualMax = getEl("editConsolidatorFactualMax");
      const canonicalMin = getEl("editConsolidatorCanonicalMin");
      if (factualMax?.value) {
        const v = parseFloat((factualMax as HTMLInputElement).value);
        if (!Number.isNaN(v)) prompts.consolidator_factual_max = v;
      }
      if (canonicalMin?.value) {
        const v = parseFloat((canonicalMin as HTMLInputElement).value);
        if (!Number.isNaN(v)) prompts.consolidator_canonical_min = v;
      }
      payload.prompts = prompts;
      return payload;
    }
    return null;
  }

  function saveSection(section: string, btn: HTMLButtonElement): void {
    const pl = buildPayloadForSection(section);
    if (!pl || (Object.keys(pl).length === 0)) {
      loadChatConfig();
      return;
    }
    const origText = btn.textContent;
    btn.textContent = "Saving…";
    btn.disabled = true;
    fetch(API_BASE + "/chat/config", {
      method: "PATCH",
      headers: { "Content-Type": "application/json", ...getAuthHeaders() },
      body: JSON.stringify(pl),
    })
      .then((r) => {
        if (!r.ok) throw new Error(String(r.status));
        return r.json() as Promise<ChatConfigResponse & { config_sha?: string }>;
      })
      .then((data) => {
        const shaEl = document.getElementById("configShaValue");
        if (shaEl && data.config_sha) shaEl.textContent = data.config_sha;
        btn.textContent = "Saved";
        loadChatConfig();
        loadConfigHistory();
        window.setTimeout(() => {
          btn.textContent = origText;
          btn.disabled = false;
        }, 1500);
      })
      .catch(() => {
        btn.textContent = origText ?? "Save";
        btn.disabled = false;
        loadChatConfig();
      });
  }

  document.querySelectorAll(".config-save-section-btn").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      const section = (btn as HTMLElement).getAttribute("data-save-section");
      if (!section) return;
      saveSection(section, btn as HTMLButtonElement);
    });
  });

  function getSampleInputForTest(section: string): Record<string, unknown> {
    if (section === "planner") {
      const msg = (document.getElementById("testPlannerMessage") as HTMLInputElement | null)?.value?.trim();
      return { message: msg || "What is prior authorization?" };
    }
    if (section === "first_gen") {
      const msg = (document.getElementById("testFirstGenMessage") as HTMLInputElement | null)?.value?.trim();
      const plan = (document.getElementById("testFirstGenPlanSummary") as HTMLInputElement | null)?.value?.trim();
      return { message: msg || "What is prior authorization?", plan_summary: plan || "One sub-question." };
    }
    if (section === "rag_answering") {
      const ctx = (document.getElementById("testRagContext") as HTMLTextAreaElement | null)?.value?.trim();
      const q = (document.getElementById("testRagQuestion") as HTMLInputElement | null)?.value?.trim();
      return {
        context: ctx || "(Sample context: Prior authorization is required for certain services.)",
        question: q || "What is prior authorization?",
      };
    }
    if (section === "integrator" || section === "consolidator") {
      return {
        consolidator_input_json: JSON.stringify({
          user_message: "What is prior authorization?",
          subquestions: [{ id: "sq1", text: "What is prior authorization?" }],
          answers: [{ sq_id: "sq1", answer: "Prior authorization is a process where your doctor gets approval from your health plan before certain services." }],
        }, null, 2),
      };
    }
    return {};
  }

  function getPromptKeyForTest(section: string): string {
    if (section === "integrator" || section === "consolidator") {
      const modeEl = document.getElementById(section === "integrator" ? "testIntegratorMode" : "testConsolidatorMode") as HTMLSelectElement | null;
      return modeEl?.value || "integrator_factual";
    }
    return section;
  }

  function getResultElForTest(section: string): HTMLElement | null {
    const id =
      section === "planner"
        ? "testResultPlanner"
        : section === "first_gen"
          ? "testResultFirstGen"
          : section === "rag_answering"
            ? "testResultRagAnswering"
            : section === "integrator"
              ? "testResultIntegrator"
              : section === "consolidator"
                ? "testResultConsolidator"
                : null;
    return id ? document.getElementById(id) : null;
  }

  function runPromptTest(section: string, btn: HTMLButtonElement): void {
    const promptKey = getPromptKeyForTest(section);
    const sampleInput = getSampleInputForTest(section);
    const resultEl = getResultElForTest(section);
    if (!resultEl) return;
    const origText = btn.textContent;
    btn.textContent = "Running…";
    btn.disabled = true;
    resultEl.textContent = "";
    resultEl.className = "config-test-result";
    fetch(API_BASE + "/chat/config/test-prompt", {
      method: "POST",
      headers: { "Content-Type": "application/json", ...getAuthHeaders() },
      body: JSON.stringify({ prompt_key: promptKey, sample_input: sampleInput }),
    })
      .then((r) => {
        if (!r.ok) throw new Error(String(r.status));
        return r.json() as Promise<{ output?: string; model_used?: string | null; duration_ms?: number; error?: string }>;
      })
      .then((data) => {
        if (data.error) {
          resultEl.textContent = `Error: ${data.error}`;
          resultEl.classList.add("config-test-result--error");
        } else {
          const out = (data.output ?? "").trim();
          const meta = [data.model_used, data.duration_ms != null ? `${data.duration_ms} ms` : ""].filter(Boolean).join(" · ");
          resultEl.innerHTML = meta ? `<div class="config-test-meta">${escapeHtml(meta)}</div><pre class="config-test-output">${escapeHtml(out || "(empty)")}</pre>` : `<pre class="config-test-output">${escapeHtml(out || "(empty)")}</pre>`;
          resultEl.classList.add("config-test-result--ok");
        }
        btn.textContent = origText ?? "Run test";
        btn.disabled = false;
      })
      .catch(() => {
        resultEl.textContent = "Request failed.";
        resultEl.classList.add("config-test-result--error");
        btn.textContent = origText ?? "Run test";
        btn.disabled = false;
      });
  }

  document.querySelectorAll(".config-run-test-btn").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      const section = (btn as HTMLElement).getAttribute("data-test-section");
      if (!section) return;
      runPromptTest(section, btn as HTMLButtonElement);
    });
  });

  // Load config on page load so summary/sidebar show real data or error (with 8s timeout)
  loadChatConfig();

  const configSummaryRow = document.getElementById("configSummaryRow");
  const configPreferencesExpanded = document.getElementById("configPreferencesExpanded");
  const configPrefArrow = document.getElementById("configPrefArrow");
  const configHistorySection = document.getElementById("configHistorySection");
  const configTestSection = document.getElementById("configTestSection");
  const configNamedRunsSection = document.getElementById("configNamedRunsSection");
  if (configSummaryRow && configPreferencesExpanded && configPrefArrow) {
    configSummaryRow.addEventListener("click", () => {
      const show = !configPreferencesExpanded.classList.contains("show");
      configPreferencesExpanded.classList.toggle("show", show);
      configPrefArrow.textContent = show ? "▲" : "▼";
      configSummaryRow.setAttribute("aria-expanded", String(show));
      if (configHistorySection) configHistorySection.style.display = show ? "block" : "none";
      if (configTestSection) configTestSection.style.display = show ? "block" : "none";
      if (configNamedRunsSection) configNamedRunsSection.style.display = show ? "block" : "none";
      if (show) {
        loadConfigHistory();
        loadNamedRuns();
      }
    });
    configSummaryRow.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        configSummaryRow.click();
      }
    });
  }
  const editLlmModelSelect = document.getElementById("editLlmModel") as HTMLSelectElement | null;
  const editLlmModelCustom = document.getElementById("editLlmModelCustom") as HTMLInputElement | null;
  if (editLlmModelSelect && editLlmModelCustom) {
    editLlmModelSelect.addEventListener("change", () => {
      const isCustom = editLlmModelSelect.value === "__custom__";
      editLlmModelCustom.style.display = isCustom ? "block" : "none";
      if (!isCustom) editLlmModelCustom.value = "";
    });
  }
  document.querySelectorAll(".config-section-title.config-section-toggle, .config-subsection-title.config-section-toggle").forEach((el) => {
    el.addEventListener("click", () => {
      const body = el.nextElementSibling;
      if (body?.classList.contains("config-section-body") || body?.classList.contains("config-subsection-body")) {
        body.classList.toggle("collapsed");
        el.classList.toggle("collapsed");
      }
    });
  });

  let currentAuthUser: AuthUser | null = null;

  function updateSidebarUser(user: AuthUser | null): void {
    currentAuthUser = user;
    const nameEl = document.getElementById("sidebarUserName");
    if (nameEl)
      nameEl.textContent = user ? (user.preferred_name || user.first_name || user.display_name || user.email || "User") : "Guest";
  }

  const authModal = createAuthModal({
    auth,
    showOAuth: true,
    onSuccess: (u: AuthUser) => updateSidebarUser(u),
  });
  document.body.appendChild(authModal.el);
  document.head.insertAdjacentHTML("beforeend", `<style>${AUTH_STYLES}</style>`);
  document.head.insertAdjacentHTML("beforeend", `<style id="mobius-prefs-styles">${PREFERENCES_MODAL_STYLES}</style>`);
  document.head.insertAdjacentHTML("beforeend", `<style id="mobius-user-menu-styles">${USER_MENU_STYLES}</style>`);
  const preferencesModal = createPreferencesModal(apiBase, auth, {
    onSave: () => auth.getUserProfile().then((u: AuthUser | null) => updateSidebarUser(u ?? null)),
  });
  (window as unknown as { onOpenPreferences?: () => void }).onOpenPreferences = () => preferencesModal.open();
  const userMenu = createUserMenu({
    auth,
    onOpenPreferences: () => preferencesModal.open(),
    onSignOut: () => updateSidebarUser(null),
    onSwitchAccount: () => {
      updateSidebarUser(null);
      authModal.open("login");
    },
  });
  auth.on((event: string, u: AuthUser | null) => {
    if (event === "login") updateSidebarUser(u as AuthUser);
    else if (event === "logout") updateSidebarUser(null);
  });

  const sidebarUser = document.getElementById("sidebarUser");
  sidebarUser?.addEventListener("click", () => {
    if (currentAuthUser) userMenu.show(sidebarUser as HTMLElement);
    else authModal.open("login");
  });
  sidebarUser?.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      if (currentAuthUser) userMenu.show(sidebarUser as HTMLElement);
      else authModal.open("login");
    }
  });
  auth.getCurrentUser().then((u: AuthUser | null) => updateSidebarUser(u ?? null));

  const MODEL_OPTIONS = ["gemini-2.5-flash", "gemini-2.0-flash", "llama3.1:8b"];

  function setEl(id: string, value: string, attr: "value" | "textContent" = "value"): void {
    const el = document.getElementById(id) as HTMLInputElement | HTMLTextAreaElement | HTMLElement | null;
    if (!el) return;
    if (attr === "value" && "value" in el) (el as HTMLInputElement).value = value;
    else el.textContent = value;
  }
  function getEl(id: string): HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement | null {
    return document.getElementById(id) as HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement | null;
  }

  const CONFIG_FETCH_TIMEOUT_MS = 8000;

  function loadChatConfig(): void {
    setEl("drawerSummaryLlm", "Loading…", "textContent");
    const timeoutPromise = new Promise<never>((_, reject) => {
      window.setTimeout(() => reject(new Error("CONFIG_TIMEOUT")), CONFIG_FETCH_TIMEOUT_MS);
    });
    const fetchPromise = fetch(API_BASE + "/chat/config")
      .then((r) => {
        if (!r.ok) throw new Error(`Config failed (${r.status})`);
        return r.json() as Promise<ChatConfigResponse>;
      });
    Promise.race([fetchPromise, timeoutPromise])
      .then((data) => {
        const shaEl = document.getElementById("configShaValue");
        if (shaEl) shaEl.textContent = data.config_sha && data.config_sha.trim() ? data.config_sha : "—";
        const llm = data.llm ?? {};
        const parser = data.parser ?? {};
        const p = data.prompts ?? {};
        const summaryLlm = (llm.provider ?? "—") + " / " + (llm.model ?? "—");
        const summaryParser = parser.patient_keywords?.length
          ? String(parser.patient_keywords.length) + " keywords"
          : "—";
        setEl("drawerSummaryLlm", summaryLlm, "textContent");
        setEl("drawerSummaryParser", summaryParser, "textContent");
        const editProvider = getEl("editLlmProvider") as HTMLSelectElement | null;
        const editModel = getEl("editLlmModel") as HTMLSelectElement | null;
        const editModelCustom = getEl("editLlmModelCustom");
        const editTemp = getEl("editLlmTemperature");
        if (editProvider) editProvider.value = (llm.provider ?? "vertex").toLowerCase();
        const modelVal = (llm.model ?? "").trim();
        if (editModel && editModelCustom) {
          if (MODEL_OPTIONS.includes(modelVal)) {
            editModel.value = modelVal;
            editModelCustom.style.display = "none";
            editModelCustom.value = "";
          } else {
            editModel.value = "__custom__";
            editModelCustom.style.display = "block";
            editModelCustom.value = modelVal;
          }
        }
        if (editTemp) editTemp.value = llm.temperature != null ? String(llm.temperature) : "0.1";
        setEl("editParserKeywords", (parser.patient_keywords ?? []).join(", "));
        setEl("editParserSeparators", (parser.decomposition_separators ?? [" and ", " also ", " then "]).join(", "));
        setEl("editDecomposeSystem", p.decompose_system ?? "");
        setEl("editDecomposeUserTemplate", p.decompose_user_template ?? "");
        setEl("editFirstGenSystem", p.first_gen_system ?? "");
        setEl("editFirstGenUser", p.first_gen_user_template ?? "");
        setEl("editRagAnsweringUserTemplate", p.rag_answering_user_template ?? "");
        setEl("editIntegratorSystem", p.integrator_system ?? "");
        setEl("editIntegratorUserTemplate", p.integrator_user_template ?? "");
        setEl("editIntegratorRepairSystem", p.integrator_repair_system ?? "");
        const editFactualMax = getEl("editConsolidatorFactualMax");
        const editCanonicalMin = getEl("editConsolidatorCanonicalMin");
        if (editFactualMax) editFactualMax.value = p.consolidator_factual_max != null ? String(p.consolidator_factual_max) : "0.4";
        if (editCanonicalMin) editCanonicalMin.value = p.consolidator_canonical_min != null ? String(p.consolidator_canonical_min) : "0.6";
        setEl("editIntegratorFactualSystem", p.integrator_factual_system ?? "");
        setEl("editIntegratorCanonicalSystem", p.integrator_canonical_system ?? "");
        setEl("editIntegratorBlendedSystem", p.integrator_blended_system ?? "");
        loadSidebarLlm(data);
      })
      .catch((err: unknown) => {
        setEl("configShaValue", "—", "textContent");
        const msg =
          err instanceof Error && err.message === "CONFIG_TIMEOUT"
            ? "Timeout — click Load from server to retry"
            : err instanceof Error
              ? err.message
              : "Failed to load";
        setEl("drawerSummaryLlm", msg, "textContent");
        setEl("drawerSummaryParser", "—", "textContent");
      });
  }

  function saveChatConfig(): void {
    const editProvider = getEl("editLlmProvider") as HTMLSelectElement | null;
    const editModel = getEl("editLlmModel") as HTMLSelectElement | null;
    const editModelCustom = getEl("editLlmModelCustom");
    const editTemp = getEl("editLlmTemperature");
    const modelVal = editModel?.value === "__custom__" ? (editModelCustom?.value ?? "").trim() : (editModel?.value ?? "").trim();
    const payload: { llm?: Record<string, unknown>; parser?: Record<string, unknown>; prompts?: Record<string, unknown> } = {};
    const llm: Record<string, unknown> = {};
    if (editProvider?.value.trim()) llm.provider = editProvider.value.trim();
    if (modelVal) llm.model = modelVal;
    if (editTemp?.value.trim()) {
      const t = parseFloat(editTemp.value);
      if (!Number.isNaN(t)) llm.temperature = t;
    }
    if (Object.keys(llm).length) payload.llm = llm;
    const keywordsEl = getEl("editParserKeywords");
    const separatorsEl = getEl("editParserSeparators");
    if (keywordsEl?.value.trim() || separatorsEl?.value.trim()) {
      payload.parser = {};
      if (keywordsEl?.value.trim())
        payload.parser.patient_keywords = keywordsEl.value.split(",").map((s) => s.trim()).filter(Boolean);
      if (separatorsEl?.value.trim())
        payload.parser.decomposition_separators = separatorsEl.value.split(/[,\n]/).map((s) => s.trim()).filter(Boolean);
    }
    const prompts: Record<string, unknown> = {};
    const promptIds: [string, string][] = [
      ["editDecomposeSystem", "decompose_system"],
      ["editDecomposeUserTemplate", "decompose_user_template"],
      ["editFirstGenSystem", "first_gen_system"],
      ["editFirstGenUser", "first_gen_user_template"],
      ["editRagAnsweringUserTemplate", "rag_answering_user_template"],
      ["editIntegratorSystem", "integrator_system"],
      ["editIntegratorUserTemplate", "integrator_user_template"],
      ["editIntegratorRepairSystem", "integrator_repair_system"],
      ["editIntegratorFactualSystem", "integrator_factual_system"],
      ["editIntegratorCanonicalSystem", "integrator_canonical_system"],
      ["editIntegratorBlendedSystem", "integrator_blended_system"],
    ];
    for (const [id, key] of promptIds) {
      const el = getEl(id);
      if (el && "value" in el && (el as HTMLInputElement).value !== undefined) prompts[key] = (el as HTMLInputElement).value;
    }
    const factualMax = getEl("editConsolidatorFactualMax");
    const canonicalMin = getEl("editConsolidatorCanonicalMin");
    if (factualMax?.value) {
      const v = parseFloat(factualMax.value);
      if (!Number.isNaN(v)) prompts.consolidator_factual_max = v;
    }
    if (canonicalMin?.value) {
      const v = parseFloat(canonicalMin.value);
      if (!Number.isNaN(v)) prompts.consolidator_canonical_min = v;
    }
    if (Object.keys(prompts).length) payload.prompts = prompts;
    if (Object.keys(payload).length === 0) {
      loadChatConfig();
      return;
    }
    fetch(API_BASE + "/chat/config", {
      method: "PATCH",
      headers: { "Content-Type": "application/json", ...getAuthHeaders() },
      body: JSON.stringify(payload),
    })
      .then((r) => {
        if (!r.ok) throw new Error(String(r.status));
        return r.json() as Promise<ChatConfigResponse & { config_sha?: string }>;
      })
      .then((data) => {
        const shaEl = document.getElementById("configShaValue");
        if (shaEl && data.config_sha) shaEl.textContent = data.config_sha;
        loadChatConfig();
        loadConfigHistory();
      })
      .catch(() => {
        loadChatConfig();
      });
  }

  function loadConfigHistory(): void {
    const listEl = document.getElementById("configHistoryList");
    if (!listEl) return;
    fetch(API_BASE + "/chat/config/history?limit=50")
      .then((r) => r.json() as Promise<ConfigHistoryEntry[]>)
      .then((entries) => {
        listEl.innerHTML = "";
        if (!entries.length) {
          listEl.textContent = "No history yet. Save config to create an entry.";
          return;
        }
        const formatDate = (iso: string) => {
          try {
            const d = new Date(iso);
            return Number.isNaN(d.getTime()) ? iso : d.toLocaleString();
          } catch {
            return iso;
          }
        };
        entries.forEach((entry) => {
          const row = document.createElement("div");
          row.className = "config-history-row";
          const shaShort = (entry.config_sha || "").slice(0, 8);
          row.innerHTML = `<span class="config-history-sha" title="${escapeHtml(entry.config_sha)}">${escapeHtml(shaShort)}</span> <span class="config-history-date">${escapeHtml(formatDate(entry.created_at))}</span> <button type="button" class="config-history-btn config-history-view-btn">View</button> <button type="button" class="config-history-btn config-history-restore-btn">Restore</button>`;
          const viewBtn = row.querySelector(".config-history-view-btn");
          const restoreBtn = row.querySelector(".config-history-restore-btn");
          viewBtn?.addEventListener("click", () => {
            fetch(API_BASE + "/chat/config/history/" + encodeURIComponent(entry.config_sha))
              .then((r) => r.json())
              .then((data: { config_sha?: string; config?: unknown }) => {
                const viewPanel = document.getElementById("configHistoryView");
                const viewBody = document.getElementById("configHistoryViewBody");
                if (viewPanel && viewBody) {
                  viewBody.textContent = JSON.stringify(data.config ?? data, null, 2);
                  viewPanel.style.display = "block";
                }
              })
              .catch(() => {
                const viewBody = document.getElementById("configHistoryViewBody");
                if (viewBody) viewBody.textContent = "Failed to load snapshot.";
                const viewPanel = document.getElementById("configHistoryView");
                if (viewPanel) viewPanel.style.display = "block";
              });
          });
          restoreBtn?.addEventListener("click", () => {
            if (!confirm("Restore this config version? Current form will be replaced.")) return;
            fetch(API_BASE + "/chat/config/restore", {
              method: "POST",
              headers: { "Content-Type": "application/json", ...getAuthHeaders() },
              body: JSON.stringify({ config_sha: entry.config_sha }),
            })
              .then((r) => {
                if (!r.ok) throw new Error(String(r.status));
                return r.json() as Promise<ChatConfigResponse & { config_sha?: string }>;
              })
              .then((data) => {
                const shaEl = document.getElementById("configShaValue");
                if (shaEl && data.config_sha) shaEl.textContent = data.config_sha;
                loadChatConfig();
                loadConfigHistory();
              })
              .catch(() => {
                loadConfigHistory();
              });
          });
          listEl.appendChild(row);
        });
      })
      .catch(() => {
        listEl.textContent = "Failed to load history.";
      });
  }

  interface NamedRunEntry {
    id: string;
    name: string;
    description: string;
    config_sha: string;
    message_snippet: string;
    reply_snippet: string;
    created_at: string;
  }

  function loadNamedRuns(): void {
    const listEl = document.getElementById("configNamedRunsList");
    if (!listEl) return;
    fetch(API_BASE + "/chat/config/test-runs?limit=50")
      .then((r) => r.json() as Promise<NamedRunEntry[]>)
      .then((entries) => {
        listEl.innerHTML = "";
        if (!entries.length) {
          listEl.textContent = "No named runs yet. Run a test with a version name to save one.";
          return;
        }
        const formatDate = (iso: string) => {
          try {
            const d = new Date(iso);
            return Number.isNaN(d.getTime()) ? iso : d.toLocaleString();
          } catch {
            return iso;
          }
        };
        entries.forEach((entry) => {
          const row = document.createElement("div");
          row.className = "config-named-run-row";
          const name = (entry.name || "").trim() || "Unnamed";
          const desc = (entry.description || "").trim();
          const shaShort = (entry.config_sha || "").slice(0, 8);
          row.innerHTML =
            `<span class="config-named-run-name" title="${escapeHtml(name)}">${escapeHtml(name)}</span>` +
            (desc ? ` <span class="config-named-run-desc">${escapeHtml(desc)}</span>` : "") +
            ` <span class="config-named-run-meta">${escapeHtml(shaShort)} · ${escapeHtml(formatDate(entry.created_at))}</span>` +
            ` <button type="button" class="config-history-btn config-named-run-view-btn">View</button>`;
          const viewBtn = row.querySelector(".config-named-run-view-btn");
          viewBtn?.addEventListener("click", () => {
            fetch(API_BASE + "/chat/config/test-runs/" + encodeURIComponent(entry.id))
              .then((r) => {
                if (!r.ok) throw new Error("Not found");
                return r.json();
              })
              .then((data: Record<string, unknown>) => {
                const viewPanel = document.getElementById("configNamedRunView");
                const viewTitle = document.getElementById("configNamedRunViewTitle");
                const viewBody = document.getElementById("configNamedRunViewBody");
                if (!viewPanel || !viewBody) return;
                if (viewTitle) viewTitle.textContent = (data.name as string) || "Run";
                viewBody.innerHTML = "";
                const addBlock = (label: string, content: string) => {
                  const block = document.createElement("div");
                  block.className = "config-named-run-view-block";
                  const h4 = document.createElement("h4");
                  h4.textContent = label;
                  const pre = document.createElement("pre");
                  pre.textContent = content;
                  block.appendChild(h4);
                  block.appendChild(pre);
                  viewBody.appendChild(block);
                };
                if (data.message != null) addBlock("Message", String(data.message));
                if (data.reply != null) addBlock("Reply", String(data.reply));
                if (data.config_sha != null) addBlock("Config SHA", String(data.config_sha));
                if (data.model_used != null) addBlock("Model", String(data.model_used));
                if (data.duration_ms != null) addBlock("Duration (ms)", String(data.duration_ms));
                if (data.stages != null && typeof data.stages === "object") {
                  addBlock("Stages", JSON.stringify(data.stages, null, 2));
                }
                viewPanel.style.display = "block";
              })
              .catch(() => {
                const viewBody = document.getElementById("configNamedRunViewBody");
                if (viewBody) viewBody.textContent = "Failed to load run.";
                const viewPanel = document.getElementById("configNamedRunView");
                if (viewPanel) viewPanel.style.display = "block";
              });
          });
          listEl.appendChild(row);
        });
      })
      .catch(() => {
        listEl.textContent = "Failed to load named runs.";
      });
  }

  function escapeHtml(s: string): string {
    const div = document.createElement("div");
    div.textContent = s;
    return div.innerHTML;
  }

  const configHistoryViewClose = document.getElementById("configHistoryViewClose");
  const configHistoryView = document.getElementById("configHistoryView");
  if (configHistoryViewClose && configHistoryView) {
    configHistoryViewClose.addEventListener("click", () => {
      configHistoryView.style.display = "none";
    });
  }

  const configNamedRunViewClose = document.getElementById("configNamedRunViewClose");
  const configNamedRunView = document.getElementById("configNamedRunView");
  if (configNamedRunViewClose && configNamedRunView) {
    configNamedRunViewClose.addEventListener("click", () => {
      configNamedRunView.style.display = "none";
    });
  }

  interface ConfigTestStages {
    planner?: unknown;
    rag_answers?: Array<{ sq_id?: string; kind?: string; text?: string; answer_preview?: string }>;
    integrator_raw?: string;
    final_answer?: string;
  }
  interface ConfigTestData {
    reply?: string;
    config_sha?: string;
    model_used?: string;
    duration_ms?: number;
    detail?: string;
    stages?: ConfigTestStages;
    run_id?: string;
    name?: string;
  }

  const configTestRun = document.getElementById("configTestRun");
  const configTestMessage = document.getElementById("configTestMessage") as HTMLTextAreaElement | null;
  const configTestVersionName = document.getElementById("configTestVersionName") as HTMLInputElement | null;
  const configTestDescription = document.getElementById("configTestDescription") as HTMLInputElement | null;
  const configTestSavedAs = document.getElementById("configTestSavedAs");
  const configTestResult = document.getElementById("configTestResult");
  if (configTestRun && configTestResult) {
    configTestRun.addEventListener("click", () => {
      const message = (configTestMessage?.value ?? "").trim() || "What is prior authorization?";
      const name = (configTestVersionName?.value ?? "").trim();
      const description = (configTestDescription?.value ?? "").trim();
      if (configTestSavedAs) {
        configTestSavedAs.style.display = "none";
        configTestSavedAs.textContent = "";
      }
      configTestResult.textContent = "Running test…";
      configTestResult.classList.remove("config-test-error");
      fetch(API_BASE + "/chat/config/test", {
        method: "POST",
        headers: { "Content-Type": "application/json", ...getAuthHeaders() },
        body: JSON.stringify({ message, name: name || undefined, description: description || undefined }),
      })
        .then((r) => r.json().then((data: ConfigTestData) => ({ ok: r.ok, data })))
        .then(({ ok, data }) => {
          if (!ok) throw new Error(data.detail || "Test failed");
          if ((data.run_id || data.name) && configTestSavedAs) {
            configTestSavedAs.style.display = "block";
            configTestSavedAs.textContent = "Saved as: " + (data.name || data.run_id);
            loadNamedRuns();
          }
          const stages = data.stages;
          if (stages) {
            configTestResult.innerHTML = "";
            const wrap = document.createElement("div");
            wrap.className = "config-test-stages";

            const meta = document.createElement("div");
            meta.className = "config-test-meta";
            const metaParts: string[] = [];
            if (data.model_used != null) metaParts.push(`Model: ${data.model_used}`);
            if (data.config_sha != null) metaParts.push(`Config: ${data.config_sha}`);
            if (data.duration_ms != null) metaParts.push(`${data.duration_ms} ms`);
            meta.textContent = metaParts.join(" · ");
            wrap.appendChild(meta);

            const addSection = (title: string, content: string, collapsed = false) => {
              const block = document.createElement("div");
              block.className = "config-test-stage-block";
              const h4 = document.createElement("h4");
              h4.className = "config-section-title config-section-toggle config-test-stage-title";
              h4.setAttribute("role", "button");
              h4.setAttribute("tabindex", "0");
              h4.innerHTML = `${title} <span class="config-toggle-arrow">▼</span>`;
              const body = document.createElement("div");
              body.className = "config-section-body" + (collapsed ? " collapsed" : "");
              if (collapsed) h4.classList.add("collapsed");
              const pre = document.createElement("pre");
              pre.textContent = content;
              pre.className = "config-test-stage-content";
              body.appendChild(pre);
              block.appendChild(h4);
              block.appendChild(body);
              h4.addEventListener("click", () => {
                body.classList.toggle("collapsed");
                h4.classList.toggle("collapsed");
              });
              h4.addEventListener("keydown", (e) => {
                if (e.key === "Enter" || e.key === " ") {
                  e.preventDefault();
                  h4.click();
                }
              });
              wrap.appendChild(block);
            };

            if (stages.planner != null) {
              addSection(
                "Planner (subquestions)",
                typeof stages.planner === "string" ? stages.planner : JSON.stringify(stages.planner, null, 2),
                false
              );
            }
            if (stages.rag_answers != null && stages.rag_answers.length > 0) {
              const lines = stages.rag_answers.map(
                (a) => `[${a.sq_id ?? "?"}] ${a.kind ?? "—"}\n  Q: ${(a.text ?? "").trim() || "—"}\n  A: ${(a.answer_preview ?? "").trim() || "—"}`
              );
              addSection("RAG answers (per subquestion)", lines.join("\n\n"), false);
            }
            if (stages.integrator_raw != null) {
              addSection("Integrator (raw output)", stages.integrator_raw, true);
            }
            if (stages.final_answer != null) {
              addSection("Final answer", stages.final_answer, false);
            }

            configTestResult.appendChild(wrap);
          } else {
            const lines: string[] = [];
            if (data.reply != null) lines.push(String(data.reply));
            if (data.model_used != null) lines.push(`\nModel: ${data.model_used}`);
            if (data.config_sha != null) lines.push(`Config: ${data.config_sha}`);
            if (data.duration_ms != null) lines.push(`Duration: ${data.duration_ms} ms`);
            configTestResult.textContent = lines.join("\n") || "No reply.";
          }
        })
        .catch((err) => {
          configTestResult.textContent = "Test failed: " + (err?.message || String(err));
          configTestResult.classList.add("config-test-error");
        });
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
        documents.forEach((item: HistoryDocumentItem) => {
          const li = document.createElement("li");
          li.className = "documents-item documents-item--clickable";
          const nameSpan = document.createElement("span");
          nameSpan.textContent = item.document_name;
          li.appendChild(nameSpan);
          const n = item.cited_in_count ?? 0;
          if (n > 0) {
            const citedSpan = document.createElement("span");
            citedSpan.className = "documents-item-cited";
            citedSpan.textContent =
              n === 1 ? " — Cited in 1 recent answer." : ` — Cited in ${n} recent answers.`;
            li.appendChild(citedSpan);
          }
          li.title = "View document";
          li.setAttribute("role", "button");
          li.setAttribute("tabindex", "0");
          li.addEventListener("click", () =>
            openDocumentOrSnippet({ document_id: item.document_id ?? null, document_name: item.document_name, page_number: null, snippet: "" })
          );
          li.addEventListener("keydown", (e) => {
            if (e.key === "Enter" || e.key === " ") {
              e.preventDefault();
              openDocumentOrSnippet({ document_id: item.document_id ?? null, document_name: item.document_name, page_number: null, snippet: "" });
            }
          });
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
  let threadId: string | null = null;

  function sendMessage(): void {
    const message = (inputEl.value ?? "").trim();
    if (!message) return;
    if (sendBtn.disabled) return; // already sending

    if (chatEmpty) chatEmpty.classList.add("hidden");

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

    // 2. Vanishing progress stack (last 3 lines from real thinking_log; removed when answer arrives)
    const { el: progressStackEl, addLine: progressAddLine } = renderProgressStack();
    turnWrap.appendChild(progressStackEl);
    scrollToBottom(messagesEl);

    function onThinkingLine(line: string): void {
      progressAddLine(line);
      scrollToBottom(messagesEl);
    }

    // Stream is still consumed (we get full message at completion) but we don't show raw stream – only progress stack, then vanish and show answer
    function onStreamingMessage(_text: string): void {
      // Intentionally not rendering streaming content; progress stack only, then final answer
      scrollToBottom(messagesEl);
    }

    const body: { message: string; thread_id?: string } = { message };
    if (threadId != null && threadId !== "") body.thread_id = threadId;
    fetch(API_BASE + "/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json", ...getAuthHeaders() },
      body: JSON.stringify(body),
    })
      .then((r) => r.json() as Promise<ChatPostResponse>)
      .then((postData) => {
        progressAddLine("Request sent. Waiting for worker…");
        if (postData.thread_id != null && postData.thread_id !== "") threadId = postData.thread_id;
        const correlationId = postData.correlation_id;
        return streamResponse(correlationId, onThinkingLine, onStreamingMessage).then(
          (streamData) => ({ streamData, correlationId })
        );
      })
      .then(({ streamData: data, correlationId }) => {
        if (typeof console !== "undefined" && console.log) {
          console.log("[AnswerCard] stream completed, processing final message…");
        }
        const fullMessage = data.message ?? "(No message)";
        const { body, sources } = parseMessageAndSources(fullMessage);

        if (typeof console !== "undefined" && console.log) {
          console.log("[AnswerCard] fullMessage length:", fullMessage.length, "starts:", (fullMessage || "").slice(0, 120));
          console.log("[AnswerCard] body length:", (body || "").length, "starts:", (body || "").slice(0, 120));
        }

        // Phase 2: Remove progress UI entirely so the answer is "the only thing that ever mattered"
        progressStackEl.remove();

        // 3. Assistant message: AnswerCard JSON or prose fallback
        const finalBody = body || "(No response)";
        const parsedCard = tryParseAnswerCard(finalBody);
        if (typeof console !== "undefined" && console.log) {
          console.log("[AnswerCard] tryParseAnswerCard:", parsedCard ? "card (mode=" + parsedCard.mode + ")" : "null");
        }
        const stripValue = data.source_confidence_strip ?? "unverified";
        const contentEl = renderAssistantContent(finalBody, !!data.llm_error, stripValue);
        turnWrap.appendChild(contentEl);

        // 4. Feedback (pass correlation_id for persistence)
        turnWrap.appendChild(renderFeedback(correlationId).el);

        // 5. Sources: prefer API response.sources (from RAG) so source cards show even when integrator drops them
        const sourceList: ParsedSource[] =
          data.sources && data.sources.length > 0
            ? (data.sources as Array<{ index?: number; document_id?: string | null; document_name?: string; page_number?: number | null; text?: string; source_type?: string | null; match_score?: number | null; confidence?: number | null }>).map((s) => ({
                index: s.index ?? 0,
                document_id: s.document_id ?? null,
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
                  document_id: s.document_id ?? null,
                  document_name: s.document_name ?? "document",
                  page_number: s.page_number ?? null,
                  snippet: (s.snippet ?? "").slice(0, 120),
                  source_type: null,
                  match_score: null,
                  confidence: null,
                }))
              : [];
        if (sourceList.length > 0) {
          const appendSourceCiter = (ratings: Record<number, "up" | "down">) => {
            turnWrap.appendChild(
              renderSourceCiter(
                sourceList,
                (s) =>
                  openDocumentOrSnippet({
                    document_id: s.document_id,
                    document_name: s.document_name,
                    page_number: s.page_number,
                    snippet: s.snippet,
                  }),
                correlationId,
                ratings
              )
            );
          };
          fetch(API_BASE + "/chat/feedback/source/" + encodeURIComponent(correlationId))
            .then((r) => (r.ok ? r.json() : { ratings: [] }))
            .then((data: { ratings?: Array<{ source_index: number; rating: string }> }) => {
              const ratings: Record<number, "up" | "down"> = {};
              (data.ratings || []).forEach((x) => {
                if (x.rating === "up" || x.rating === "down") ratings[x.source_index] = x.rating;
              });
              appendSourceCiter(ratings);
            })
            .catch(() => appendSourceCiter({}));
        }

        scrollToBottom(messagesEl);
        loadSidebarHistory();
      })
      .catch((err: Error) => {
        progressStackEl.remove();
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

  const composerOptionsBtn = document.getElementById("composerOptions");
  const composerWrap = document.querySelector(".composer-wrap");
  let composerOptionsMenu: HTMLElement | null = null;
  function closeComposerOptionsMenu(): void {
    if (composerOptionsMenu) composerOptionsMenu.hidden = true;
  }
  function openComposerOptionsMenu(): void {
    if (!composerOptionsMenu && composerWrap) {
      composerOptionsMenu = document.createElement("div");
      composerOptionsMenu.className = "composer-options-menu";
      composerOptionsMenu.setAttribute("role", "menu");
      composerOptionsMenu.hidden = true;
      composerOptionsMenu.innerHTML = `
        <button type="button" class="composer-option-item" data-action="new-chat" role="menuitem">New chat</button>
        <button type="button" class="composer-option-item" data-action="chat-config" role="menuitem">Chat config</button>
        <button type="button" class="composer-option-item" data-action="preferences" role="menuitem">Preferences</button>
      `;
      composerOptionsMenu.querySelectorAll(".composer-option-item").forEach((item) => {
        item.addEventListener("click", () => {
          const action = (item as HTMLElement).dataset.action;
          closeComposerOptionsMenu();
          if (action === "new-chat") {
            threadId = null;
            if (messagesEl && chatEmpty) {
              messagesEl.innerHTML = "";
              messagesEl.appendChild(chatEmpty);
              chatEmpty.classList.remove("hidden");
            }
            loadSidebarHistory();
          } else if (action === "chat-config") openDrawer();
          else if (action === "preferences") preferencesModal.open();
        });
      });
      composerWrap.appendChild(composerOptionsMenu);
    }
    if (composerOptionsMenu) composerOptionsMenu.hidden = false;
  }
  composerOptionsBtn?.addEventListener("click", (e) => {
    e.stopPropagation();
    if (composerOptionsMenu?.hidden !== false) openComposerOptionsMenu();
    else closeComposerOptionsMenu();
  });
  document.addEventListener("click", (e: MouseEvent) => {
    const target = e.target as Node;
    if (composerOptionsMenu && !composerOptionsMenu.contains(target) && !composerOptionsBtn?.contains(target))
      closeComposerOptionsMenu();
  });

  const btnNewChat = document.getElementById("btnNewChat");
  btnNewChat?.addEventListener("click", () => {
    threadId = null;
    if (messagesEl && chatEmpty) {
      messagesEl.innerHTML = "";
      messagesEl.appendChild(chatEmpty);
      chatEmpty.classList.remove("hidden");
    }
    loadSidebarHistory();
  });

  updateSendState();

  loadSidebarHistory();
  loadSidebarLlm();
}

run();

export {};
