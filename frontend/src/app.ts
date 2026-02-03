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

/** GET /chat/history/most-helpful-documents (sorted by distinct liked turns; document_id when present) */
interface HistoryDocumentItem {
  document_name: string;
  document_id?: string | null;
  /** Number of distinct liked answers that cited this document */
  cited_in_count?: number;
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
  AUTH_STYLES,
  STORAGE_KEYS,
} from "@mobius/auth";
import type { UserProfile } from "@mobius/auth";

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

    closeBtn.addEventListener("click", () => {
      overlay?.classList.remove("open");
      overlay?.setAttribute("aria-hidden", "true");
    });
    overlay.addEventListener("click", (e) => {
      if (e.target === overlay) {
        overlay.classList.remove("open");
        overlay.setAttribute("aria-hidden", "true");
      }
    });
    document.body.appendChild(overlay);
  }

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

  function setSidebarCollapsed(collapsed: boolean): void {
    if (!sidebar || !mainEl) return;
    if (collapsed) {
      sidebar.classList.add("sidebar--collapsed");
      mainEl.classList.add("sidebar-collapsed");
      if (sidebarChevron) {
        sidebarChevron.setAttribute("aria-label", "Expand sidebar");
        sidebarChevron.setAttribute("title", "Expand sidebar");
      }
    } else {
      sidebar.classList.remove("sidebar--collapsed");
      mainEl.classList.remove("sidebar-collapsed");
      if (sidebarChevron) {
        sidebarChevron.setAttribute("aria-label", "Collapse sidebar");
        sidebarChevron.setAttribute("title", "Collapse sidebar");
      }
    }
  }

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
  }

  function closeDrawer(): void {
    drawer.classList.remove("open");
    drawerOverlay.classList.remove("open");
  }

  hamburger.addEventListener("click", openDrawer);
  drawerClose.addEventListener("click", closeDrawer);
  drawerOverlay.addEventListener("click", closeDrawer);

  let currentAuthUser: UserProfile | null = null;

  function updateSidebarUser(user: UserProfile | null): void {
    currentAuthUser = user;
    const nameEl = document.getElementById("sidebarUserName");
    if (nameEl)
      nameEl.textContent = user ? (user.preferred_name || user.first_name || user.display_name || user.email || "User") : "Guest";
  }

  const authModal = createAuthModal({
    auth,
    showOAuth: true,
    onSuccess: (u) => updateSidebarUser(u),
  });
  document.body.appendChild(authModal.el);
  document.head.insertAdjacentHTML("beforeend", `<style>${AUTH_STYLES}</style>`);
  (window as unknown as { onOpenPreferences?: () => void }).onOpenPreferences = openDrawer;
  auth.on((event, u) => {
    if (event === "login") updateSidebarUser(u as UserProfile);
    else if (event === "logout") updateSidebarUser(null);
  });

  const sidebarUser = document.getElementById("sidebarUser");
  sidebarUser?.addEventListener("click", () => authModal.open(currentAuthUser ? "account" : "login"));
  sidebarUser?.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); authModal.open(currentAuthUser ? "account" : "login"); }
  });
  auth.getUserProfile().then((u) => updateSidebarUser(u ?? null));

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
    let messageWrapEl: HTMLElement | null = null;
    function onStreamingMessage(_text: string): void {
      // Intentionally not rendering streaming content; progress stack only, then final answer
      scrollToBottom(messagesEl);
    }

    fetch(API_BASE + "/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json", ...getAuthHeaders() },
      body: JSON.stringify({ message }),
    })
      .then((r) => r.json() as Promise<ChatPostResponse>)
      .then((postData) => {
        progressAddLine("Request sent. Waiting for worker…");
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
        if (messageWrapEl) {
          messageWrapEl.replaceWith(contentEl);
        } else {
          turnWrap.appendChild(contentEl);
        }

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

  updateSendState();

  loadSidebarHistory();
  loadSidebarLlm();
}

run();

export {};
