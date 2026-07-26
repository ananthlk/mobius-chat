// AnswerCard parsing + visibility model — extracted from app.ts so the pure logic is
// unit-testable (vitest) without loading the browser entrypoint. app.ts imports from here.
//
// Covers the v2 contract (SPEC_CHAT_FRONTEND_V2_UX §1.1/§1.2/§5): mode optional,
// per-section visibility, the discriminator that lets legacy + v2 cards share one path,
// and the minimum-valid-card anchor.

/** Section intent for visibility rules */
export const SECTION_INTENTS = ["process", "requirements", "definitions", "exceptions", "references"] as const;
export type SectionIntent = (typeof SECTION_INTENTS)[number];

export function isSectionIntent(s: unknown): s is SectionIntent {
  return typeof s === "string" && SECTION_INTENTS.includes(s as SectionIntent);
}

/** AnswerCard JSON from consolidator (legacy FACTUAL/CANONICAL/BLENDED + RECITAL; v2 = no mode) */
export type SectionFormat = "bullets" | "table" | "steps" | "stats" | "bars" | "conditions";
export interface SectionDataItem {
  label?: string;
  value?: string;
  note?: string;
  weight?: number;
  condition?: string;
  result?: string;
}
export interface SectionData {
  headers?: string[];
  rows?: string[][];
  items?: SectionDataItem[];
}
/** Per-section lead-vs-tuck signal (v2). Replaces mode-driven visibility. */
export type SectionVisibility = "primary" | "detail";
export interface AnswerCardSection {
  intent?: SectionIntent;
  label: string;
  format?: SectionFormat;
  visibility?: SectionVisibility;
  bullets: string[];
  data?: SectionData;
}
export interface AnswerCard {
  // v2 drops FACTUAL/CANONICAL/BLENDED — mode is optional and, when present, only ever RECITAL.
  // Legacy (pre-flag) cards still carry the old modes; both shapes render (AC-FE-7).
  mode?: "FACTUAL" | "CANONICAL" | "BLENDED" | "RECITAL";
  direct_answer: string;
  sections: AnswerCardSection[];
  recital?: {
    verbatim: string;
    document_id?: string;
    section?: string;
  };
  required_variables?: string[];
  confidence_note?: string;
  citations?: Array<{ id: string; doc_title: string; locator: string; snippet: string }>;
  followups?: Array<{ question: string; reason: string; field: string }>;
  suggested_actions?: Array<{ type: string; label: string; url: string; icon?: string }>;
  thread_summary?: string;
}

export const MAX_SECTIONS = 4;

export function findMatchingCloseBrace(str: string, start: number): number {
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

export function tryParseAnswerCard(message: string): AnswerCard | null {
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
      const rawMode = typeof data.mode === "string" ? data.mode : undefined;
      // RECITAL: verbatim required, sections optional. Unchanged from legacy.
      if (rawMode === "RECITAL") {
        const rec = data.recital as Record<string, unknown> | undefined;
        if (!rec || typeof rec.verbatim !== "string" || !rec.verbatim.trim()) return null;
        return {
          mode: "RECITAL",
          direct_answer: typeof data.direct_answer === "string" ? data.direct_answer : "",
          sections: [],
          recital: {
            verbatim: rec.verbatim as string,
            document_id: typeof rec.document_id === "string" ? rec.document_id : undefined,
            section: typeof rec.section === "string" ? rec.section : undefined,
          },
        };
      }
      // Minimum-valid-card anchor (AC-FE-1 / Tech Health C2): dropping `mode` does NOT loosen
      // validity. `direct_answer` (non-empty string) remains the required anchor for any
      // non-RECITAL card. "Accept a no-mode card" is not "accept any JSON".
      if (typeof data.direct_answer !== "string" || !data.direct_answer.trim()) return null;
      // `mode` is kept only for the recognized legacy values; any other value (absent, or a
      // future/drifted one like "SUMMARY") is treated as absent → renders via the v2 path
      // (§5 discriminator / AC-FE-7), never null, never legacy.
      const KNOWN_LEGACY_MODES: readonly string[] = ["FACTUAL", "CANONICAL", "BLENDED"];
      const mode = rawMode && KNOWN_LEGACY_MODES.includes(rawMode)
        ? (rawMode as AnswerCard["mode"]) : undefined;
      // sections default to [] when absent — a v2 summary-only card is valid (anchored on direct_answer).
      const rawSections = Array.isArray(data.sections)
        ? (data.sections as Array<Record<string, unknown>>).slice(0, MAX_SECTIONS) : [];
      const VALID_FORMATS: SectionFormat[] = ["bullets", "table", "steps", "stats", "bars", "conditions"];
      const sections: AnswerCardSection[] = rawSections.map((sec) => ({
        intent: isSectionIntent(sec.intent) ? sec.intent as SectionIntent : "process",
        label: typeof sec.label === "string" ? sec.label : "",
        format: VALID_FORMATS.includes(sec.format as SectionFormat) ? sec.format as SectionFormat : "bullets",
        // Unknown/absent visibility → undefined; the §1.2 fallback resolves it at render time.
        visibility: (sec.visibility === "primary" || sec.visibility === "detail")
          ? sec.visibility as SectionVisibility : undefined,
        bullets: Array.isArray(sec.bullets) ? sec.bullets as string[] : [],
        data: sec.data && typeof sec.data === "object" ? sec.data as SectionData : undefined,
      }));
      return {
        mode,
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
  // Legacy + RECITAL: anchor the enclosing object on the `mode` field.
  const modeRe = /["']mode["']\s*:\s*["'](FACTUAL|CANONICAL|BLENDED|RECITAL)["']/;
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
  // v2 (AC-FE-1): a no-mode card has no `mode` to anchor on. Fall back to the required
  // `direct_answer` anchor so a v2 card embedded in surrounding text still parses.
  const daRe = /["']direct_answer["']\s*:/;
  const m2 = raw.match(daRe);
  if (m2) {
    const idx = raw.indexOf(m2[0]);
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

/**
 * Decide which sections lead (visible) vs tuck behind "Show details" (hidden).
 *
 * §5 discriminator — single source of truth, read by one renderer so legacy and v2
 * cannot drift during the flag ramp:
 *   - v2 path when ANY section carries a `visibility` flag OR `mode` is not a known legacy
 *     value (absent, or a drifted/future value like "SUMMARY" — Tech Health hole #1).
 *   - legacy path only when `mode ∈ {FACTUAL, CANONICAL, BLENDED}` AND no section carries visibility.
 *   - Collision (legacy mode + per-section visibility present): visibility wins; mode ignored.
 * §1.2 fallback — a section whose `visibility` is absent OR unrecognized (Tech Health hole #2)
 * resolves to: first section primary, the rest detail. An unknown value never dark-renders.
 * (RECITAL is handled by the caller before this is reached.)
 */
export function splitSectionsByVisibility(
  sections: AnswerCardSection[],
  mode: AnswerCard["mode"]
): { visible: AnswerCardSection[]; hidden: AnswerCardSection[] } {
  const all = sections.slice(0, MAX_SECTIONS);
  const isKnownLegacy = mode === "FACTUAL" || mode === "CANONICAL" || mode === "BLENDED";
  const anyVisibility = all.some((s) => s.visibility === "primary" || s.visibility === "detail");

  if (anyVisibility || !isKnownLegacy) {
    // v2 path — per-section visibility, with the §1.2 fallback for absent/unknown values.
    const visible: AnswerCardSection[] = [];
    const hidden: AnswerCardSection[] = [];
    all.forEach((s, i) => {
      const resolved =
        s.visibility === "primary" || s.visibility === "detail"
          ? s.visibility
          : i === 0 ? "primary" : "detail";
      (resolved === "primary" ? visible : hidden).push(s);
    });
    return { visible, hidden };
  }

  // legacy path — mode-driven visibility, unchanged.
  if (mode === "FACTUAL") return { visible: [], hidden: all };
  if (mode === "CANONICAL") return { visible: all, hidden: [] };
  // BLENDED: surface requirements, process, and definitions immediately.
  // Only exceptions and references collapse — they're supplementary.
  const visibleIntents = new Set(["definitions", "requirements", "process"]);
  const visible = all.filter((s) => visibleIntents.has(s.intent ?? "process"));
  const hidden = all.filter((s) => !visibleIntents.has(s.intent ?? "process"));
  return { visible, hidden };
}
