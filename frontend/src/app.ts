import {
  createAuthService,
  localStorageAdapter,
  createAuthModal,
  AUTH_STYLES,
} from "@mobius/auth";

/** Clarification option: clickable choice for slot fill */
interface ClarificationOption {
  slot: string;
  label: string;
  selection_mode: string;
  choices: Array<{ value: string; label: string }>;
}

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
  source_confidence_strip?: string | null;
  cited_source_indices?: number[];
  open_slots?: string[];
  clarification_options?: ClarificationOption[];
  thread_id?: string;
}

/** Single RAG source (when backend provides sources array) */
interface SourceItem {
  document_name?: string;
  document_id?: string | null;
  page_number?: number | null;
  text?: string;
  index?: number;
}

/** Parsed source from "Sources:" block or API response.sources (RAG) */
interface ParsedSource {
  index: number;
  document_name: string;
  document_id?: string | null;
  page_number: number | null;
  snippet: string;
  source_type?: string | null;
  match_score?: number | null;
  confidence?: number | null;
}

/** GET /chat/history/recent or most-helpful-searches */
interface HistoryTurnItem {
  correlation_id: string;
  question: string;
  created_at: string | null;
}

/** GET /chat/history/most-helpful-documents */
interface HistoryDocumentItem {
  document_name: string;
  document_id?: string | null;
  cited_in_count?: number;
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
  thread_id?: string;
}

/** Section intent for visibility rules */
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
interface AnswerCard {
  mode: "FACTUAL" | "CANONICAL" | "BLENDED";
  direct_answer: string;
  sections: AnswerCardSection[];
  required_variables?: string[];
  confidence_note?: string;
  citations?: Array<{ id: string; doc_title: string; locator: string; snippet: string }>;
  followups?: Array<{ question: string; reason: string; field: string }>;
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

function normalizeMessageText(text: string): string {
  return (text ?? "").replace(/\n{2,}/g, "\n").trim();
}

const MAX_SECTIONS = 4;
const MAX_BULLETS_PER_SECTION = 4;

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
        mode: data.mode as AnswerCard["mode"],
        direct_answer: data.direct_answer as string,
        sections,
        required_variables: Array.isArray(data.required_variables) ? (data.required_variables as string[]) : undefined,
        confidence_note: typeof data.confidence_note === "string" ? data.confidence_note : undefined,
        citations: Array.isArray(data.citations) ? (data.citations as AnswerCard["citations"]) : undefined,
        followups: Array.isArray(data.followups) ? (data.followups as AnswerCard["followups"]) : undefined,
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
    const start = raw.lastIndexOf("{", idx);
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

function splitSectionsByVisibility(
  sections: AnswerCardSection[],
  mode: AnswerCard["mode"]
): { visible: AnswerCardSection[]; hidden: AnswerCardSection[] } {
  const all = sections.slice(0, MAX_SECTIONS);
  if (mode === "FACTUAL") return { visible: [], hidden: all };
  if (mode === "CANONICAL") return { visible: all, hidden: [] };
  const requirements = all.filter((s) => (s.intent ?? "process") === "requirements");
  const hidden = all.filter((s) => {
    const i = s.intent ?? "process";
    return i === "process" || i === "definitions" || i === "exceptions" || i === "references";
  });
  return { visible: requirements, hidden };
}

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

/** Source confidence badge variants: strip value → { label, variant } */
const CONFIDENCE_BADGE_MAP: Record<
  string,
  { label: string; variant: string; icon: string }
> = {
  approved_authoritative: {
    label: "Approved – Authoritative",
    variant: "approved_authoritative",
    icon: "check",
  },
  approved_informational: {
    label: "Approved – Informational",
    variant: "approved_informational",
    icon: "shield",
  },
  proceed_with_caution: {
    label: "Proceed with Caution",
    variant: "proceed_with_caution",
    icon: "alert-triangle",
  },
  augmented_with_google: {
    label: "Augmented with External Search",
    variant: "augmented_with_google",
    icon: "globe",
  },
  informational_only: {
    label: "Informational Only",
    variant: "informational_only",
    icon: "info",
  },
  no_sources: {
    label: "No Sources",
    variant: "no_sources",
    icon: "alert-circle",
  },
};

function renderConfidenceBadge(strip: string): HTMLElement {
  const key = strip.toLowerCase().replace(/\s+/g, "_");
  const cfg = CONFIDENCE_BADGE_MAP[key] ?? {
    label: strip.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase()),
    variant: "unverified",
    icon: "info",
  };
  const wrap = document.createElement("div");
  wrap.className = "confidence-badge-wrap";
  const badge = document.createElement("span");
  badge.className = `confidence-badge confidence-badge--${cfg.variant}`;
  badge.setAttribute("aria-label", "Source confidence: " + cfg.label);

  const iconEl = document.createElement("span");
  iconEl.className = "confidence-badge-icon";
  iconEl.setAttribute("aria-hidden", "true");
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("fill", "none");
  svg.setAttribute("stroke", "currentColor");
  svg.setAttribute("stroke-width", "2");
  svg.setAttribute("stroke-linecap", "round");
  svg.setAttribute("stroke-linejoin", "round");
  svg.setAttribute("width", "14");
  svg.setAttribute("height", "14");
  const paths: Record<string, string> = {
    check: "M20 6L9 17l-5-5",
    shield: "M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z",
    "alert-triangle": "M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z M12 9v4 M12 17h.01",
    globe: "M21 12a9 9 0 01-9 9m9-9a9 9 0 00-9-9m9 9H3m9 9a9 9 0 01-9-9m9 9c1.657 0 3-4.03 3-9s-1.343-9-3-9m0 18c-1.657 0-3-4.03-3-9s1.343-9 3-9m-9 9a9 9 0 019-9",
    info: "M12 16v-4 M12 8h.01 M22 12c0 5.523-4.477 10-10 10S2 17.523 2 12 6.477 2 12 2s10 4.477 10 10z",
    "alert-circle": "M12 8v4m0 4h.01M22 12c0 5.523-4.477 10-10 10S2 17.523 2 12 6.477 2 12 2s10 4.477 10 10z",
  };
  const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
  path.setAttribute("d", paths[cfg.icon] ?? paths.info);
  svg.appendChild(path);
  iconEl.appendChild(svg);

  const labelEl = document.createElement("span");
  labelEl.className = "confidence-badge-label";
  labelEl.textContent = cfg.label;

  badge.appendChild(iconEl);
  badge.appendChild(labelEl);
  wrap.appendChild(badge);
  return wrap;
}

function renderAnswerCard(
  card: AnswerCard,
  isError?: boolean,
  opts?: { onFollowupClick?: (question: string) => void; sourceConfidenceStrip?: string; showConfidenceBadge?: boolean }
): HTMLElement {
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

  if (opts?.showConfidenceBadge !== false) {
    bubble.appendChild(
      renderConfidenceBadge((opts?.sourceConfidenceStrip ?? "").trim() || "informational_only")
    );
  }

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
    card.followups.slice(0, 4).forEach((f) => {
      const chip = document.createElement("button");
      chip.type = "button";
      chip.className = "answer-card-followup-chip";
      const questionText = f.question || f.reason || f.field || "";
      chip.textContent = questionText;
      chip.setAttribute("aria-label", questionText);
      if (opts?.onFollowupClick && questionText) {
        chip.addEventListener("click", () => opts!.onFollowupClick!(questionText));
      }
      metaRow.appendChild(chip);
    });
  }
  if (metaRow.childNodes.length > 0) bubble.appendChild(metaRow);

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

/** Render assistant content: AnswerCard JSON (formatted) or prose fallback. */
function renderAssistantContent(
  body: string,
  isError?: boolean,
  opts?: { onFollowupClick?: (question: string) => void; sourceConfidenceStrip?: string; showConfidenceBadge?: boolean }
): HTMLElement {
  const card = tryParseAnswerCard(body);
  if (card) return renderAnswerCard(card, isError, opts);
  const trimmed = (body ?? "").trim();
  if (trimmed.startsWith("{") && trimmed.length > 10) {
    const errWrap = document.createElement("div");
    errWrap.className = "message message--assistant" + (isError ? " message--error" : "");
    const errBubble = document.createElement("div");
    errBubble.className = "message-bubble";
    if (opts?.showConfidenceBadge !== false) {
      errBubble.appendChild(
        renderConfidenceBadge((opts?.sourceConfidenceStrip ?? "").trim() || "informational_only")
      );
    }
    const errText = document.createElement("div");
    errText.className = "message-bubble-text";
    errText.textContent = "Answer could not be displayed. Please try again.";
    errBubble.appendChild(errText);
    errWrap.appendChild(errBubble);
    return errWrap;
  }
  const wrap = document.createElement("div");
  wrap.className = "message message--assistant" + (isError ? " message--error" : "");
  const bubble = document.createElement("div");
  bubble.className = "message-bubble";
  if (opts?.showConfidenceBadge !== false) {
    bubble.appendChild(
      renderConfidenceBadge((opts?.sourceConfidenceStrip ?? "").trim() || "informational_only")
    );
  }
  const textEl = document.createElement("div");
  textEl.className = "message-bubble-text";
  textEl.textContent = normalizeMessageText(body);
  bubble.appendChild(textEl);
  wrap.appendChild(bubble);
  return wrap;
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

/** Reusable: compact thinking line – streams in one line, collapses to summary when done.
 * Body shows max 3 lines, scrolls so last line is visible; auto-scrolls on each addLine. */
function renderThinkingBlock(
  initialLines: string[],
  opts?: { onExpand?: () => void }
): { el: HTMLElement; setPreview: (text: string) => void; addLine: (line: string) => void; done: (lineCount: number) => void } {
  const block = document.createElement("div");
  block.className = "thinking-block thinking-block--compact" + (initialLines.length ? "" : " collapsed");

  const preview = document.createElement("div");
  preview.className = "thinking-preview";
  preview.setAttribute("role", "button");
  preview.setAttribute("tabindex", "0");
  preview.setAttribute("aria-expanded", initialLines.length > 0 ? "true" : "false");
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
      block.classList.remove("collapsed");
      preview.setAttribute("aria-expanded", "true");
      body.scrollTop = body.scrollHeight;
    },
    done(lineCount: number) {
      word.textContent = lineCount <= 1 ? "Thinking" : `Thinking (${lineCount})`;
      block.classList.add("thinking-block--done");
      setTimeout(() => {
        collapse();
      }, 2500);
    },
  };
}

/** Reusable: clarification options (buttons/chips for slot fill). */
function renderClarificationOptions(
  opts: ClarificationOption[],
  onSelect: (value: string) => void
): HTMLElement {
  const wrap = document.createElement("div");
  wrap.className = "clarification-options";
  for (const opt of opts) {
    const group = document.createElement("div");
    group.className = "clarification-option-group";
    const labelEl = document.createElement("div");
    labelEl.className = "clarification-option-label";
    labelEl.textContent = opt.label;
    group.appendChild(labelEl);
    const chips = document.createElement("div");
    chips.className = "clarification-option-chips";
    for (const c of opt.choices) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "clarification-option-chip";
      btn.textContent = c.label;
      btn.addEventListener("click", () => onSelect(c.value));
      chips.appendChild(btn);
    }
    group.appendChild(chips);
    wrap.appendChild(group);
  }
  return wrap;
}

/** Reusable: assistant message bubble (left-aligned). Always includes confidence badge. */
function renderAssistantMessage(
  text: string,
  isError?: boolean,
  opts?: { sourceConfidenceStrip?: string }
): HTMLElement {
  const wrap = document.createElement("div");
  wrap.className = "message message--assistant" + (isError ? " message--error" : "");
  const bubble = document.createElement("div");
  bubble.className = "message-bubble";
  bubble.appendChild(
    renderConfidenceBadge((opts?.sourceConfidenceStrip ?? "").trim() || "informational_only")
  );
  const textEl = document.createElement("div");
  textEl.className = "message-bubble-text";
  textEl.textContent = normalizeMessageText(text);
  bubble.appendChild(textEl);
  wrap.appendChild(bubble);
  return wrap;
}

/** Create SVG thumb icon for feedback (grey outline, ChatGPT-style). */
function createThumbIcon(type: "up" | "down"): SVGSVGElement {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("fill", "none");
  svg.setAttribute("stroke", "currentColor");
  svg.setAttribute("stroke-width", "2");
  svg.setAttribute("stroke-linecap", "round");
  svg.setAttribute("stroke-linejoin", "round");
  svg.setAttribute("width", "18");
  svg.setAttribute("height", "18");
  svg.setAttribute("aria-hidden", "true");
  const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
  path.setAttribute(
    "d",
    type === "up"
      ? "M14 9V5a3 3 0 0 0-3-3l-4 9v11h11.28a2 2 0 0 0 2-1.7l1.38-9a2 2 0 0 0-2-2.3zM7 22H4a2 2 0 0 1-2-2v-7a2 2 0 0 1 2-2h3"
      : "M10 15v4a3 3 0 0 0 3 3l4-9V2H5.72a2 2 0 0 0-2 1.7l-1.38 9a2 2 0 0 0 2 2.3zm7-13h2.67A2.31 2.31 0 0 1 22 4v7a2.31 2.31 0 0 1-2.33 2H17"
  );
  svg.appendChild(path);
  return svg;
}

/** Reusable: feedback bar (thumbs up/down, comment dialogue, copy). */
function renderFeedback(correlationId: string): HTMLElement {
  const bar = document.createElement("div");
  bar.className = "feedback";
  const left = document.createElement("div");
  left.className = "feedback-left";
  const actions = document.createElement("div");
  actions.className = "feedback-actions";

  const up = document.createElement("button");
  up.type = "button";
  up.className = "feedback-thumb";
  up.setAttribute("aria-label", "Good response");
  up.appendChild(createThumbIcon("up"));
  const down = document.createElement("button");
  down.type = "button";
  down.className = "feedback-thumb";
  down.setAttribute("aria-label", "Bad response");
  down.appendChild(createThumbIcon("down"));

  const commentArea = document.createElement("div");
  commentArea.className = "feedback-comment-area";
  commentArea.style.display = "none";

  const commentForm = document.createElement("div");
  commentForm.className = "feedback-comment-form";
  const textarea = document.createElement("textarea");
  textarea.placeholder = "What could we improve? (optional)";
  textarea.rows = 2;
  const commentBtns = document.createElement("div");
  commentBtns.className = "feedback-comment-buttons";
  const submitBtn = document.createElement("button");
  submitBtn.type = "button";
  submitBtn.textContent = "Submit";
  const cancelBtn = document.createElement("button");
  cancelBtn.type = "button";
  cancelBtn.textContent = "Cancel";
  commentBtns.appendChild(submitBtn);
  commentBtns.appendChild(cancelBtn);
  commentForm.appendChild(textarea);
  commentForm.appendChild(commentBtns);
  commentArea.appendChild(commentForm);

  function postFeedback(rating: "up" | "down", comment: string | null): void {
    fetch(API_BASE + "/chat/feedback/" + encodeURIComponent(correlationId), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ rating, comment }),
    })
      .then(() => {
        up.disabled = true;
        down.disabled = true;
        up.classList.toggle("selected", rating === "up");
        down.classList.toggle("selected", rating === "down");
        commentArea.style.display = "none";
      })
      .catch(() => {});
  }

  up.addEventListener("click", () => {
    if (up.disabled) return;
    postFeedback("up", null);
  });
  down.addEventListener("click", () => {
    if (down.disabled) return;
    commentArea.style.display = "block";
    textarea.focus();
  });
  submitBtn.addEventListener("click", () => {
    postFeedback("down", textarea.value.trim() || null);
  });
  cancelBtn.addEventListener("click", () => {
    commentArea.style.display = "none";
  });

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

  left.appendChild(up);
  left.appendChild(down);
  left.appendChild(commentArea);
  actions.appendChild(copy);
  bar.appendChild(left);
  bar.appendChild(actions);
  return bar;
}

/** RAG deep-link URL for Read tab (document + optional page). */
function getRagDocumentUrl(documentId: string | null | undefined, pageNumber: number | null | undefined): string | null {
  const base = (typeof window !== "undefined" && (window as { RAG_APP_BASE?: string }).RAG_APP_BASE)?.trim() ?? "";
  if (!base || !documentId?.trim()) return null;
  const params = new URLSearchParams({ tab: "read", documentId: documentId.trim() });
  if (pageNumber != null) params.set("pageNumber", String(pageNumber));
  return `${base.replace(/\/$/, "")}?${params.toString()}`;
}

/** Open document: RAG URL in new tab if available; else no-op. */
function openDocumentOrSnippet(s: {
  document_id?: string | null;
  document_name: string;
  page_number?: number | null;
  snippet: string;
}): void {
  const url = getRagDocumentUrl(s.document_id, s.page_number);
  if (url) {
    window.open(url, "_blank", "noopener,noreferrer");
  }
}

/** Reusable: source citer – same look as thinking (word + line, muted, collapsed by default). Includes per-source feedback (source card). */
function renderSourceCiter(
  sources: ParsedSource[],
  citedSourceIndices?: number[],
  correlationId?: string | null
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
  const citedSet = new Set((citedSourceIndices ?? []).map((n) => Number(n)));
  sources.forEach((s) => {
    const item = document.createElement("div");
    const isCited = citedSet.size > 0 && citedSet.has(Number(s.index));
    item.className = "source-item" + (isCited ? " source-item--cited" : "");
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
    const ragUrl = getRagDocumentUrl(s.document_id, s.page_number);
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

    if (correlationId) {
      const feedbackRow = document.createElement("div");
      feedbackRow.className = "source-feedback-row";
      const question = document.createElement("span");
      question.className = "source-feedback-question";
      question.textContent = "Helpful?";
      const thumbs = document.createElement("div");
      thumbs.className = "source-feedback-thumbs";
      const upBtn = document.createElement("button");
      upBtn.type = "button";
      upBtn.setAttribute("aria-label", "Helpful");
      upBtn.appendChild(createThumbIcon("up"));
      const downBtn = document.createElement("button");
      downBtn.type = "button";
      downBtn.setAttribute("aria-label", "Not helpful");
      downBtn.appendChild(createThumbIcon("down"));
      const srcIdx = s.index != null && s.index >= 1 ? s.index : sources.indexOf(s) + 1;
      function postSourceFeedback(r: "up" | "down"): void {
        fetch(API_BASE + "/chat/source-feedback/" + encodeURIComponent(correlationId), {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ source_index: srcIdx, rating: r }),
        })
          .then(() => {
            upBtn.disabled = true;
            downBtn.disabled = true;
            upBtn.classList.toggle("selected", r === "up");
            downBtn.classList.toggle("selected", r === "down");
          })
          .catch(() => {});
      }
      upBtn.addEventListener("click", () => postSourceFeedback("up"));
      downBtn.addEventListener("click", () => postSourceFeedback("down"));
      thumbs.appendChild(upBtn);
      thumbs.appendChild(downBtn);
      feedbackRow.appendChild(question);
      feedbackRow.appendChild(thumbs);
      item.appendChild(feedbackRow);
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
  const btnConfig = document.getElementById("btnConfig");
  const sidebarUser = document.getElementById("sidebarUser");
  const sidebarUserName = document.getElementById("sidebarUserName");

  const authApiBase = `${API_BASE.replace(/\/$/, "")}/api/v1`;
  const auth = createAuthService({ apiBase: authApiBase, storage: localStorageAdapter });
  const modal = createAuthModal({ auth, showOAuth: true });
  document.body.appendChild(modal.el);
  const styleEl = document.createElement("style");
  styleEl.textContent = AUTH_STYLES;
  document.head.appendChild(styleEl);

  function updateSidebarUser(user: { greeting_name?: string } | null): void {
    if (sidebarUserName)
      sidebarUserName.textContent = user?.greeting_name ?? "Guest";
  }

  auth.on((_event) => {
    auth.getUserProfile().then(updateSidebarUser);
  });
  auth.getUserProfile().then(updateSidebarUser);

  if (sidebarUser) {
    sidebarUser.addEventListener("click", () => {
      auth.getUserProfile().then((user) => {
        modal.open(user ? "account" : "login");
      });
    });
  }

  function openDrawer(): void {
    drawer.classList.add("open");
    drawerOverlay.classList.add("open");
    loadChatConfig();
  }

  function closeDrawer(): void {
    drawer.classList.remove("open");
    drawerOverlay.classList.remove("open");
  }

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

  function initSidebarCollapsibles(): void {
    document.querySelectorAll(".sidebar-section-title.sidebar-section-toggle").forEach((titleEl) => {
      const toggle = (): void => {
        const controls = titleEl.getAttribute("aria-controls") || "";
        const body = controls ? document.getElementById(controls) : null;
        if (!body) return;
        const expanded = titleEl.getAttribute("aria-expanded") !== "false";
        const next = !expanded;
        titleEl.setAttribute("aria-expanded", String(next));
        body.classList.toggle("collapsed", !next);
      };
      titleEl.addEventListener("click", (e) => {
        e.preventDefault();
        toggle();
      });
      titleEl.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          toggle();
        }
      });
    });
  }
  initSidebarCollapsibles();

  hamburger.addEventListener("click", openDrawer);
  drawerClose.addEventListener("click", closeDrawer);
  drawerOverlay.addEventListener("click", closeDrawer);
  if (btnConfig) btnConfig.addEventListener("click", openDrawer);

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
      })
      .catch(() => {
        const sysEl = document.getElementById("promptFirstGenSystem");
        const llmEl = document.getElementById("configLlm");
        if (sysEl) sysEl.textContent = "Failed to load config.";
        if (llmEl) llmEl.textContent = "Failed to load config.";
      });
  }

  /** Poll fallback when SSE unavailable or stream fails. */
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
            if (data.status === "completed" || data.status === "clarification" || data.status === "refinement_ask" || data.status === "failed") {
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
          const data = (parsed.data ?? {}) as Record<string, unknown>;
          if (ev === "thinking" && data.line != null && onThinking) {
            onThinking(String(data.line));
          } else if (ev === "message" && data.chunk != null && onStreamingMessage) {
            messageSoFar += String(data.chunk);
            onStreamingMessage(messageSoFar);
          } else if (ev === "completed" && data) {
            resolved = true;
            es.close();
            resolve(data as unknown as ChatResponse);
          } else if (ev === "error" && data.message != null) {
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

  function sendMessage(overrideMessage?: string): void {
    const message = (overrideMessage ?? (inputEl.value ?? "").trim()).trim();
    if (!message) return;
    if (sendBtn.disabled) return;

    if (chatEmpty) chatEmpty.classList.add("hidden");

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

    if (!overrideMessage) inputEl.value = "";
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
        const textEl = messageWrapEl.querySelector(".message-bubble-text");
        if (textEl) textEl.textContent = text;
      }
      scrollToBottom(messagesEl);
    }

    const payload: { message: string; thread_id?: string } = { message };
    if (currentThreadId) payload.thread_id = currentThreadId;
    fetch(API_BASE + "/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    })
      .then((r) => r.json() as Promise<ChatPostResponse>)
      .then((data) => {
        if (data.thread_id) currentThreadId = data.thread_id;
        addThinkingLineAndScroll("Request sent. Waiting for worker…");
        return streamResponse(data.correlation_id, addThinkingLineAndScroll, onStreamingMessage);
      })
      .then((data) => {
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

        if (data.thread_id) currentThreadId = data.thread_id;

        // 3. Assistant message: AnswerCard (formatted) or prose fallback
        if (messageWrapEl) {
          messageWrapEl.remove();
        }
        turnWrap.appendChild(
          renderAssistantContent(body || "(No response)", !!data.llm_error, {
            onFollowupClick: (q) => sendMessage(q),
            sourceConfidenceStrip: (data.source_confidence_strip ?? "").trim() || undefined,
            showConfidenceBadge: data.status !== "clarification" && data.status !== "refinement_ask",
          })
        );

        // 3b. Clarification options (clickable buttons for slot fill)
        if (data.clarification_options && data.clarification_options.length > 0) {
          turnWrap.appendChild(
            renderClarificationOptions(data.clarification_options, (value) => sendMessage(value))
          );
        }

        // 4. Feedback (thumbs + comment dialogue, POST to backend)
        turnWrap.appendChild(renderFeedback(data.correlation_id));

        // 5. Sources: prefer API response.sources (from RAG) so source cards show even when integrator drops them
        const sourceList: ParsedSource[] =
          data.sources && data.sources.length > 0
            ? (data.sources as Array<{ index?: number; document_name?: string; document_id?: string | null; page_number?: number | null; text?: string; source_type?: string | null; match_score?: number | null; confidence?: number | null }>).map((s) => ({
                index: s.index ?? 0,
                document_name: s.document_name ?? "document",
                document_id: s.document_id ?? null,
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
                  document_id: s.document_id ?? null,
                  page_number: s.page_number ?? null,
                  snippet: (s.snippet ?? "").slice(0, 120),
                  source_type: null,
                  match_score: null,
                  confidence: null,
                }))
              : [];
        const cited = data.cited_source_indices ?? [];
        if (sourceList.length > 0) {
          turnWrap.appendChild(renderSourceCiter(sourceList, cited, data.correlation_id));
        }

        loadSidebarHistory();
        scrollToBottom(messagesEl);
      })
      .catch((err: Error) => {
        thinkingDone(thinkingLines.length);
        turnWrap.appendChild(
          renderAssistantMessage("Error: " + (err?.message ?? String(err)), true, {})
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

  let currentThreadId: string | null = null;
  const btnNewChat = document.getElementById("btnNewChat");
  if (btnNewChat) {
    btnNewChat.addEventListener("click", () => {
      currentThreadId = null;
      messagesEl.querySelectorAll(".chat-turn").forEach((n) => n.remove());
      if (chatEmpty) chatEmpty.classList.remove("hidden");
      loadSidebarHistory();
    });
  }

  function loadSidebarHistory(): void {
    const recentList = document.getElementById("recentList");
    const helpfulList = document.getElementById("helpfulList");
    const documentsList = document.getElementById("documentsList");
    if (!recentList) return;

    const snippet = (q: string, max = 80) =>
      (q ?? "").trim().slice(0, max) + ((q ?? "").length > max ? "…" : "");

    Promise.all([
      fetch(API_BASE + "/chat/history/recent?limit=20").then(
        (r) => r.json() as Promise<HistoryTurnItem[]>
      ),
      helpfulList
        ? fetch(API_BASE + "/chat/history/most-helpful-searches?limit=10").then(
            (r) => r.json() as Promise<HistoryTurnItem[]>
          )
        : Promise.resolve([] as HistoryTurnItem[]),
      documentsList
        ? fetch(API_BASE + "/chat/history/most-helpful-documents?limit=10").then(
            (r) => r.json() as Promise<HistoryDocumentItem[]>
          )
        : Promise.resolve([] as HistoryDocumentItem[]),
    ])
      .then(([recent, helpful, documents]) => {
        recentList.innerHTML = "";
        for (const t of recent) {
          const li = document.createElement("li");
          li.className = "recent-item";
          li.textContent = snippet(t.question || "(empty)");
          li.title = t.question || "";
          li.setAttribute("role", "button");
          li.setAttribute("tabindex", "0");
          li.addEventListener("click", () => {
            (inputEl as HTMLInputElement).value = t.question ?? "";
            updateSendState();
          });
          li.addEventListener("keydown", (e) => {
            if (e.key === "Enter" || e.key === " ") {
              e.preventDefault();
              (inputEl as HTMLInputElement).value = t.question ?? "";
              updateSendState();
            }
          });
          recentList.appendChild(li);
        }

        if (helpfulList) {
          helpfulList.innerHTML = "";
          for (const t of helpful) {
            const li = document.createElement("li");
            li.className = "helpful-item";
            li.textContent = snippet(t.question || "(empty)");
            li.title = t.question || "";
            li.setAttribute("role", "button");
            li.setAttribute("tabindex", "0");
            li.addEventListener("click", () => {
              (inputEl as HTMLInputElement).value = t.question ?? "";
              updateSendState();
              sendMessage();
            });
            li.addEventListener("keydown", (e) => {
              if (e.key === "Enter" || e.key === " ") {
                e.preventDefault();
                (inputEl as HTMLInputElement).value = t.question ?? "";
                updateSendState();
                sendMessage();
              }
            });
            helpfulList.appendChild(li);
          }
        }

        if (documentsList) {
          documentsList.innerHTML = "";
          for (const item of documents) {
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
              openDocumentOrSnippet({
                document_id: item.document_id ?? null,
                document_name: item.document_name,
                page_number: null,
                snippet: "",
              })
            );
            li.addEventListener("keydown", (e) => {
              if (e.key === "Enter" || e.key === " ") {
                e.preventDefault();
                openDocumentOrSnippet({
                  document_id: item.document_id ?? null,
                  document_name: item.document_name,
                  page_number: null,
                  snippet: "",
                });
              }
            });
            documentsList.appendChild(li);
          }
        }
      })
      .catch(() => {
        recentList.innerHTML = "";
        if (helpfulList) helpfulList.innerHTML = "";
        if (documentsList) documentsList.innerHTML = "";
      });
  }

  loadSidebarHistory();

  updateSendState();
}

run();

export {};
