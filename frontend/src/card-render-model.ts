// Pure render model for the tabbed answer bubble — the §1.4 field→tab map and the §2.1
// additive-merge contract, extracted so both are unit-testable (vitest) with synthetic
// phase signals, independent of the DOM. app.ts drives the DOM from these functions.
//
// Why pure: Tech Health's AC-FE-9 needs the merge proven commutative + idempotent under
// permuted/duplicated phase arrival. The renderer runs at N=2 today (anchor stream + one
// merged card) but the model is defined for N phases so the future progressive path
// (N>2, post-ReAct migration) is proven before it ramps. legacy single-paint = N=1.

/** The tabs of the answer bubble, in fixed display order (§1.4). */
export const TAB_ORDER = ["summary", "citations", "corrections", "follow-up", "tasks", "diagnostics"] as const;
export type TabKey = (typeof TAB_ORDER)[number];

/**
 * A content slot within the card. Slots have FIXED positions (owned by the shell mounted
 * at anchor time) — arrival order never moves them, which is what makes the merge commutative.
 */
export const SLOT_KEYS = [
  "summary_answer", // A: direct_answer (the streamed anchor)
  "sections",       // A: typed section bodies
  "takeaways",      // B: review, renders in Summary
  "gaps",           // B: review, renders in Summary — UX ruling 2026-07-26 (supersedes Corrections)
  "confidence",     // B: source/authority badge, Summary
  "citations",      // B: Citations tab
  "corrections",    // server-injected corrections, Corrections tab
  "next_questions", // C: Follow-up tab
  "tasks",          // C: Tasks tab
  "diagnostics",    // telemetry/bandit/calibration, Diagnostics tab (admin/QA)
] as const;
export type SlotKey = (typeof SLOT_KEYS)[number];

/** §1.4 field→tab map. gaps→Summary per the UX ruling (2026-07-26). */
export const SLOT_TO_TAB: Readonly<Record<SlotKey, TabKey>> = {
  summary_answer: "summary",
  sections: "summary",
  takeaways: "summary",
  gaps: "summary",
  confidence: "summary",
  citations: "citations",
  corrections: "corrections",
  next_questions: "follow-up",
  tasks: "tasks",
  diagnostics: "diagnostics",
};

/** A phase delivers a partial set of slots (any subset). Anchor phase = { summary_answer }. */
export type SlotPhase = Partial<Record<SlotKey, unknown>>;

/**
 * §2.1 additive merge — fill-your-slot, first-write-wins per slot.
 * Commutative: permuting DISJOINT phases yields an identical result (each slot is owned by
 * one phase, so first-write == only-write). Idempotent: a duplicated phase changes nothing.
 * This is the property AC-FE-9 asserts; the DOM layer paints each newly-filled slot once.
 */
export function mergeSlotPhases(phases: SlotPhase[]): Partial<Record<SlotKey, unknown>> {
  const out: Partial<Record<SlotKey, unknown>> = {};
  // Outer loop over SLOT_KEYS (fixed slot positions) so the result's key order is
  // independent of phase-arrival order — identical DOM regardless of permutation.
  // Inner loop takes the first phase (in sequence) that owns the slot: first-write-wins.
  for (const key of SLOT_KEYS) {
    for (const phase of phases) {
      if (key in phase && phase[key] !== undefined) {
        out[key] = phase[key];
        break;
      }
    }
  }
  return out;
}

/**
 * §2.1 reconcile (no-repaint): given the slots already painted, return only the slots in
 * `phase` that are newly filled. Already-filled slots are no-ops — the completed handler
 * reconciles, it does not repaint. Applying the same phase twice returns [] the second time.
 */
export function newlyFilledSlots(alreadyFilled: ReadonlySet<SlotKey>, phase: SlotPhase): SlotKey[] {
  return SLOT_KEYS.filter((key) => key in phase && phase[key] !== undefined && !alreadyFilled.has(key));
}

/**
 * §1.4 / Tech Health chrome-at-anchor: which tabs the bar shows given which slots have
 * content. Summary is always present (the anchor lives there). The full tab STRUCTURE is
 * mounted at anchor time; this decides which buttons are non-empty so enrichment un-hides
 * rather than mounting chrome (no reflow / CLS).
 */
export function tabsWithContent(filled: ReadonlySet<SlotKey>): TabKey[] {
  const present = new Set<TabKey>(["summary"]);
  for (const slot of filled) present.add(SLOT_TO_TAB[slot]);
  return TAB_ORDER.filter((t) => present.has(t));
}
