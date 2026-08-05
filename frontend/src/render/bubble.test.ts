// @vitest-environment jsdom
import { describe, it, expect } from "vitest";
import { renderAnswerCard, buildFormatChip } from "./bubble";
import { tryParseAnswerCard } from "../answer-card";
import type { AnswerCard } from "../answer-card";

// A v2 (no-mode) card: primary section leads, detail tucks; citations light up their tab.
const card: AnswerCard = {
  direct_answer: "Yes — reimbursed at parity.",
  sections: [
    { label: "Covered codes", visibility: "primary", format: "table", bullets: [],
      data: { headers: ["Code", "Rate"], rows: [["H0031", "$149.60"]] } },
    { label: "Documentation", visibility: "primary", format: "bullets", bullets: ["Real-time A/V"] },
    { label: "Exceptions", visibility: "detail", format: "bullets", bullets: ["School-based differs"] },
  ],
  citations: [{ id: "1", doc_title: "AHCA Handbook", locator: "§59G", snippet: "parity" }],
};

describe("renderAnswerCard — DOM output (§1.4 tabbed bubble + §1.2 visibility)", () => {
  it("renders the direct answer and a v2 (no-mode) card class", () => {
    const el = renderAnswerCard(card);
    expect(el.className).toContain("answer-card--v2");
    expect(el.querySelector(".answer-card-direct")?.textContent).toContain("parity");
  });

  it("primary sections lead; detail sits behind a Show details toggle", () => {
    const el = renderAnswerCard(card);
    const summary = el.querySelector(".ac-tab-panel--summary")!;
    // primary section labels are directly in the summary panel
    const summaryText = summary.textContent ?? "";
    expect(summaryText).toContain("Covered codes");
    expect(summaryText).toContain("Documentation");
    // detail section is inside the collapsed details block, and a toggle exists
    const details = el.querySelector(".answer-card-details")!;
    expect(details).not.toBeNull();
    expect(details.textContent).toContain("Exceptions");
    expect(el.querySelector(".answer-card-show-details")?.textContent).toContain("Show details");
  });

  it("renders a tab bar with Summary + a Citations tab (count from the model-driven bar)", () => {
    const el = renderAnswerCard(card);
    const tabs = Array.from(el.querySelectorAll(".ac-tab")).map((t) => t.textContent ?? "");
    expect(tabs.some((t) => t.includes("Summary"))).toBe(true);
    expect(tabs.some((t) => t.includes("Citations"))).toBe(true);
    // the citation lands in its own panel, not stacked into Summary
    expect(el.querySelector(".ac-tab-panel--citations")?.textContent).toContain("AHCA Handbook");
  });

  it("renders the typed table envelope (§1.3)", () => {
    const el = renderAnswerCard(card);
    const table = el.querySelector("table.ac-fmt-table");
    expect(table).not.toBeNull();
    expect(table?.textContent).toContain("H0031");
  });

  it("a no-section summary-only card still renders (min-valid anchor)", () => {
    const el = renderAnswerCard({ direct_answer: "Short answer." });
    expect(el.querySelector(".answer-card-direct")?.textContent).toContain("Short answer.");
  });

  it("injected onCreateTask is the only app-state hook (DI) — renderer takes it via opts", () => {
    // The renderer must accept the handler without importing app.ts; passing it must not throw.
    let called = false;
    const el = renderAnswerCard(card, false, { onCreateTask: () => { called = true; } });
    expect(el).toBeTruthy();
    expect(called).toBe(false); // not invoked at render time, only on user click
  });
});

describe("Task #10 — output_intent format chip", () => {
  it("maps each real backend intent to an icon+label chip with a semantic class", () => {
    const cases: Array<[string, string]> = [
      ["report", "Report"], ["read", "Answer"], ["email", "Email"],
      ["sms", "Text"], ["emr", "EMR Note"], ["appeal", "Appeal"],
      ["payor_report", "Payor Report"],
    ];
    for (const [intent, label] of cases) {
      const chip = buildFormatChip(intent)!;
      expect(chip, intent).not.toBeNull();
      expect(chip.className).toContain("answer-card-format-chip--" + intent);
      expect(chip.querySelector(".answer-card-format-chip-text")?.textContent).toBe(label);
      expect(chip.getAttribute("aria-label")).toContain(label);
    }
  });

  it("is case-insensitive and trims", () => {
    expect(buildFormatChip("  REPORT ")?.className).toContain("answer-card-format-chip--report");
  });

  it("renders NO chip for absent/unknown intent — never invents a label", () => {
    expect(buildFormatChip(undefined)).toBeNull();
    expect(buildFormatChip("")).toBeNull();
    expect(buildFormatChip("banana")).toBeNull();
  });

  it("the chip appears in the rendered card only when output_intent is set", () => {
    const withIntent = renderAnswerCard({ ...card, output_intent: "report" });
    expect(withIntent.querySelector(".answer-card-format-chip--report")).not.toBeNull();
    const without = renderAnswerCard(card);
    expect(without.querySelector(".answer-card-format-chip")).toBeNull();
  });

  it("the chip's icon is aria-hidden (text carries the meaning)", () => {
    const chip = buildFormatChip("emr")!;
    expect(chip.querySelector(".answer-card-format-chip-icon")?.getAttribute("aria-hidden")).toBe("true");
  });

  // End-to-end with the REAL live card shape (mode FACTUAL + output_intent:"read"), parsed the
  // same way the app does — proves the chip renders through tryParseAnswerCard, not just when
  // output_intent is handed straight to the renderer. Guards the parser-drop regression.
  it("renders the chip for a real FACTUAL card carrying output_intent (full parse path)", () => {
    const realJson = JSON.stringify({
      mode: "FACTUAL",
      direct_answer: "The provided sources confirm eligibility details.",
      sections: [{ label: "Eligibility", visibility: "primary", format: "bullets", bullets: ["5–18 years"] }],
      output_intent: "read",
      display_summary: "A short answer.",
    });
    const card = tryParseAnswerCard(realJson)!;
    expect(card.output_intent).toBe("read");
    const el = renderAnswerCard(card);
    expect(el.querySelector(".answer-card-format-chip--read")).not.toBeNull();
    expect(el.querySelector(".answer-card-format-chip-text")?.textContent).toBe("Answer");
  });

  // Selector contract: the chip lives inside .answer-card-format-row, and that row is a direct
  // child of the card bubble (a sibling of the tab panels). The streaming→completed in-place
  // transplant relies on exactly this — it moves .answer-card-format-row into the live bubble.
  // If this selector/placement changes, streaming turns silently lose the chip again.
  it("wraps the chip in .answer-card-format-row as a direct child of the bubble (transplant contract)", () => {
    const el = renderAnswerCard({ direct_answer: "hi", output_intent: "report" } as any);
    const bubble = el.querySelector(".answer-card-bubble")!;
    const row = bubble.querySelector(":scope > .answer-card-format-row");
    expect(row).not.toBeNull();
    expect(row!.querySelector(".answer-card-format-chip--report")).not.toBeNull();
  });
});
