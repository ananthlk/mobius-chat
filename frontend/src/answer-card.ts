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
export type SectionFormat =
  | "bullets" | "table" | "steps" | "stats" | "bars" | "conditions"
  // Appeals Agent typed sections (2026-08-06). Emitted as pre_built_sections from the
  // appeals_lookup_rules / appeals_get_playbook MCP tools; copied verbatim into sections[]
  // by the integrator. `data` is the tool output as-is — see AppealsRulesData/AppealsPlaybookData.
  | "appeals_rules" | "appeals_playbook";

/** data payload for a `format: "appeals_rules"` section — appeals_lookup_rules output, verbatim. */
export interface AppealsRuleVariant { payor?: string; note?: string }
export interface AppealsRule {
  rule_id?: string;
  rule_name?: string;
  rule_statement?: string;
  triggers_when?: string;
  appeal_argument?: string;   // the assertion to make in the letter — the highlighted key field
  requires?: string[];
  authority_notes?: string;
  payor_variants?: Array<AppealsRuleVariant | string>;
}
export interface AppealsRulesData {
  carc?: string;
  carc_title?: string;
  archetype?: string;
  payor?: string;
  rules_found?: number;
  rules?: AppealsRule[];
  admin_url?: string;   // deep link to the appeals rules-library admin page (footer chip)
}
/** data payload for a `format: "appeals_playbook"` section — appeals_get_playbook output, verbatim. */
export interface AppealsPlaybookDoc { doc?: string; required?: boolean }
export interface AppealsPlaybookLevel { level?: number | string; name?: string; deadline_days?: number }
export interface AppealsPlaybookData {
  found?: boolean;
  message?: string;           // shown when found === false (no empty card)
  payor?: string;
  carc_group?: string;
  deadline_appeal_days?: number;
  submission_method?: string;
  portal_url?: string;
  docs_required?: AppealsPlaybookDoc[];
  appeal_levels?: AppealsPlaybookLevel[];
}
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
  // Enricher deliverable classification (integrate.py allowlist). Values are deliverable
  // TYPES — read/report/email/sms/emr/appeal/payor_report — not answer formats; may be
  // absent (None end-to-end). Task #10 surfaces this as a read-only format chip.
  output_intent?: string;
  display_summary?: string;
  // 2-4 sentence verdict (integrator card JSON). Leads the Answer tab above display_summary;
  // hidden when empty. Distinct from display_summary (prose lead) and thread_summary (sidebar).
  tldr_summary?: string;
  // ReAct's own synthesis, persisted into the card JSON (integrate.py, 2026-08-07) so the Summary
  // tab shows it on history reload the same way the live path shows the draft_ready stream.
  react_draft?: string;
  // Per-round react reasoning ledger — one entry per round, in order. FLAT shape (integrate.py runs
  // ctx.reasoning_trace through _build_reasoning_ledger, which flattens each round; no "enrichment"
  // wrapper): {round, tool?, learned?, running_answer, gaps_closed?, gaps_open?}. Empty-content
  // rounds are already skipped + fields capped server-side. The FE shows the progression
  // (rd-1 → rd-last) in the collapsible "First pass". Rides the card JSON allowlist, like react_draft.
  reasoning_trace?: Array<{
    round?: number;
    running_answer?: string;
    learned?: string;
    gaps_open?: string[];
  }>;
  // Factual correction from the integrator (chat_config.py): a specific wrong claim from the draft
  // and its accurate replacement. null/absent unless the critic flagged a direct contradiction.
  // Rendered as an INLINE redline in the answer (Ananth 2026-08-07), not a tab.
  correction?: { original: string; corrected: string };
  // Inline-footnote sources (integrator merge step, final_parallel.py — LLM Agent 2026-08-08).
  // POSITIONALLY numbered: the prose carries inline [N] markers where N is the rag_chunks 1-based
  // index the fact came from; sources[N-1] is that chunk's provenance. Built from rag_chunks metadata
  // at merge time (NOT the critic's citations[], which is a deduped subset and doesn't align 1:1 to
  // marker positions). When present, the FE renders [N] as superscript footnotes → this numbered
  // bottom list and drops the separate Sources tab. Absent → legacy citations[] behavior unchanged.
  sources?: Array<{ document_name?: string; doc_title?: string; locator?: string; snippet?: string }>;
  // Backend escalation hint (Task: Try-with-Think-mode). true when the answer was quality-flagged
  // and re-running in Think/agentic mode is suggested; ABSENT otherwise (never false). Suppressed
  // server-side when the request was already agentic. Drives the "⚡ Try with Think mode" button.
  suggest_escalate?: boolean;
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
      const VALID_FORMATS: SectionFormat[] = ["bullets", "table", "steps", "stats", "bars", "conditions", "appeals_rules", "appeals_playbook"];
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
        // Task #10: the enricher's deliverable classification. parseOne is a positive filter, so
        // these MUST be copied through explicitly or the format chip never sees them (they were
        // present in the JSON but dropped here — the live "no chip on any turn" bug, 2026-08-05).
        output_intent: typeof data.output_intent === "string" ? data.output_intent : undefined,
        display_summary: typeof data.display_summary === "string" ? data.display_summary : undefined,
        // Answer-tab lead; positive filter, copy through explicitly (same class as the output_intent-drop bug).
        tldr_summary: typeof data.tldr_summary === "string" ? data.tldr_summary : undefined,
        react_draft: typeof data.react_draft === "string" ? data.react_draft : undefined,
        reasoning_trace: Array.isArray(data.reasoning_trace)
          ? (data.reasoning_trace as AnswerCard["reasoning_trace"]) : undefined,
        correction: (() => {
          const c = data.correction as Record<string, unknown> | null | undefined;
          if (c && typeof c === "object" && typeof c.original === "string" && typeof c.corrected === "string"
              && c.original.trim() && c.corrected.trim()) {
            return { original: c.original as string, corrected: c.corrected as string };
          }
          return undefined;
        })(),
        // Inline-footnote sources — positive filter, copy through explicitly (same class as the
        // output_intent/reasoning_trace drop bugs). Only keep well-formed entries so a bad row
        // can't shift the positional [N]→sources[N-1] mapping.
        sources: Array.isArray(data.sources)
          ? (data.sources as Array<Record<string, unknown>>).map((s) => ({
              document_name: typeof s?.document_name === "string" ? s.document_name : undefined,
              doc_title: typeof s?.doc_title === "string" ? s.doc_title : undefined,
              locator: typeof s?.locator === "string" ? s.locator : undefined,
              snippet: typeof s?.snippet === "string" ? s.snippet : undefined,
            }))
          : undefined,
        // Escalation hint — copied through explicitly (parseOne is a positive filter). Backend
        // sends it only when true (absent otherwise), so a strict true check is correct.
        suggest_escalate: data.suggest_escalate === true ? true : undefined,
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
 * Parallel-integrator progressive streaming (SPEC_PARALLEL_INTEGRATOR_STREAMING, #74): build a
 * synthetic AnswerCard from ONE `integrator_partial` part so the shared renderer can fill that
 * part's panel early (before "completed"). Runs the fields through tryParseAnswerCard so sections
 * get the same format/bullets normalization as a full card — a partial must render identically to
 * the final. Returns null when the part carries nothing renderable (caller skips). `enrichment`
 * (next_questions_for_user) is NOT handled here — those ride renderAnswerCard's opts.nextQuestions,
 * not a card field. A placeholder direct_answer satisfies tryParseAnswerCard's min-valid anchor;
 * the caller transplants only the target panel, never the direct answer, so it never surfaces.
 */
export function buildPartialCard(part: string, payload: Record<string, unknown>): AnswerCard | null {
  if (part === "core") {
    const sections = Array.isArray(payload.sections) ? payload.sections : [];
    const ds = typeof payload.display_summary === "string" ? payload.display_summary : "";
    if (sections.length === 0 && !ds.trim()) return null;
    return tryParseAnswerCard(JSON.stringify({
      direct_answer: (typeof payload.direct_answer === "string" && payload.direct_answer.trim()) ? payload.direct_answer : "…",
      mode: typeof payload.mode === "string" ? payload.mode : undefined,
      sections,
      display_summary: ds || undefined,
    }));
  }
  if (part === "citations") {
    const citations = Array.isArray(payload.citations) ? payload.citations : [];
    if (citations.length === 0) return null;
    return tryParseAnswerCard(JSON.stringify({ direct_answer: "…", citations }));
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
