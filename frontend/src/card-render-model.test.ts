import { describe, it, expect } from "vitest";
import {
  SLOT_TO_TAB, TAB_ORDER, SLOT_KEYS,
  mergeSlotPhases, newlyFilledSlots, tabsWithContent,
  type SlotKey, type SlotPhase,
} from "./card-render-model";

describe("§1.4 field→tab map (AC-FE-8)", () => {
  it("gaps routes to Summary (UX ruling 2026-07-26), not Corrections", () => {
    expect(SLOT_TO_TAB.gaps).toBe("summary");
  });
  it("takeaways + confidence render in Summary as review context", () => {
    expect(SLOT_TO_TAB.takeaways).toBe("summary");
    expect(SLOT_TO_TAB.confidence).toBe("summary");
  });
  it("citations→Citations, next_questions→Follow-up, tasks→Tasks, diagnostics→Diagnostics", () => {
    expect(SLOT_TO_TAB.citations).toBe("citations");
    expect(SLOT_TO_TAB.next_questions).toBe("follow-up");
    expect(SLOT_TO_TAB.tasks).toBe("tasks");
    expect(SLOT_TO_TAB.diagnostics).toBe("diagnostics");
  });
  it("Diagnostics is the last tab (present in TAB_ORDER)", () => {
    expect(TAB_ORDER).toContain("diagnostics");
    expect(TAB_ORDER.indexOf("diagnostics")).toBe(TAB_ORDER.length - 1);
  });
  it("Answer sits between Summary and Sources/Citations (Ananth 2026-08-07)", () => {
    expect(TAB_ORDER.indexOf("answer")).toBe(1);
    expect(TAB_ORDER.indexOf("answer")).toBeGreaterThan(TAB_ORDER.indexOf("summary"));
    expect(TAB_ORDER.indexOf("answer")).toBeLessThan(TAB_ORDER.indexOf("citations"));
    expect(SLOT_TO_TAB.answer_body).toBe("answer");
  });
  it("every slot maps to a real tab", () => {
    for (const slot of SLOT_KEYS) expect(TAB_ORDER).toContain(SLOT_TO_TAB[slot]);
  });
});

describe("§1.4 tabsWithContent — bar shows only tabs with content (AC-FE-8)", () => {
  it("Summary always present, even with nothing filled", () => {
    expect(tabsWithContent(new Set())).toEqual(["summary"]);
  });
  it("gaps-only card shows just Summary (gaps lives in Summary, no extra tab)", () => {
    expect(tabsWithContent(new Set<SlotKey>(["summary_answer", "gaps"]))).toEqual(["summary"]);
  });
  it("citations + tasks light up their tabs, in fixed order", () => {
    const filled = new Set<SlotKey>(["summary_answer", "citations", "tasks"]);
    expect(tabsWithContent(filled)).toEqual(["summary", "citations", "tasks"]);
  });
  it("tab order is always TAB_ORDER regardless of fill order", () => {
    const a = tabsWithContent(new Set<SlotKey>(["tasks", "citations", "summary_answer"]));
    const b = tabsWithContent(new Set<SlotKey>(["citations", "summary_answer", "tasks"]));
    expect(a).toEqual(b);
    expect(a).toEqual(["summary", "citations", "tasks"]);
  });
});

describe("§2.1 additive merge — commutative + idempotent (AC-FE-9, synthetic phases)", () => {
  // now-slice N=2: anchor phase, then one merged enrichment phase
  const anchor: SlotPhase = { summary_answer: "the answer" };
  const enrich: SlotPhase = { sections: ["s"], citations: ["c"], takeaways: ["t"], gaps: ["g"], next_questions: ["q"], tasks: ["k"] };

  it("N=2: anchor then enrichment fills all slots", () => {
    const m = mergeSlotPhases([anchor, enrich]);
    expect(m.summary_answer).toBe("the answer");
    expect(m.citations).toEqual(["c"]);
    expect(m.gaps).toEqual(["g"]);
  });

  it("commutative: permuting arrival order yields identical result (N=2)", () => {
    expect(mergeSlotPhases([anchor, enrich])).toEqual(mergeSlotPhases([enrich, anchor]));
  });

  // synthetic N>2 (future progressive path: B and C as separate chunks) — proven before it ramps
  it("commutative across ALL permutations of N=3 disjoint phases", () => {
    const a: SlotPhase = { summary_answer: "x" };
    const b: SlotPhase = { citations: ["c"], takeaways: ["t"], gaps: ["g"] };
    const c: SlotPhase = { next_questions: ["q"], tasks: ["k"] };
    const perms: SlotPhase[][] = [
      [a, b, c], [a, c, b], [b, a, c], [b, c, a], [c, a, b], [c, b, a],
    ];
    const results = perms.map((p) => JSON.stringify(mergeSlotPhases(p)));
    expect(new Set(results).size).toBe(1); // all permutations identical
  });

  it("idempotent: a duplicated phase changes nothing", () => {
    expect(mergeSlotPhases([anchor, enrich, enrich])).toEqual(mergeSlotPhases([anchor, enrich]));
  });

  it("legacy N=1: a single all-in-one phase fills everything (degenerate case)", () => {
    const single: SlotPhase = { summary_answer: "a", sections: ["s"], citations: ["c"], gaps: ["g"] };
    expect(mergeSlotPhases([single])).toEqual(single);
  });

  it("first-write-wins on a conflicting slot (anchor summary is not clobbered)", () => {
    const m = mergeSlotPhases([{ summary_answer: "anchor" }, { summary_answer: "late" }]);
    expect(m.summary_answer).toBe("anchor");
  });
});

describe("§2.1 reconcile — no-repaint (AC-FE-9)", () => {
  it("newly-filled slots are only those not already painted", () => {
    const filled = new Set<SlotKey>(["summary_answer"]);
    const phase: SlotPhase = { summary_answer: "x", citations: ["c"], gaps: ["g"] };
    expect(newlyFilledSlots(filled, phase).sort()).toEqual(["citations", "gaps"]);
  });
  it("applying the same phase twice paints nothing the second time (reconcile, not repaint)", () => {
    const phase: SlotPhase = { citations: ["c"], tasks: ["k"] };
    const filled = new Set<SlotKey>();
    const first = newlyFilledSlots(filled, phase);
    first.forEach((s) => filled.add(s));
    expect(first.sort()).toEqual(["citations", "tasks"]);
    expect(newlyFilledSlots(filled, phase)).toEqual([]); // second pass: no-op
  });
  it("returned slots are in fixed SLOT_KEYS order (stable paint order)", () => {
    const phase: SlotPhase = { tasks: ["k"], citations: ["c"], sections: ["s"] };
    expect(newlyFilledSlots(new Set(), phase)).toEqual(["sections", "citations", "tasks"]);
  });
});
