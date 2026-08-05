// @vitest-environment jsdom
import { describe, it, expect } from "vitest";
import { renderAnswerCard, formatOutputIntentLabel } from "./bubble";
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

describe("Task #10 — output_intent (Diagnostics telemetry, NOT on the card face)", () => {
  it("formatOutputIntentLabel returns the canonical value for known intents", () => {
    for (const intent of ["read", "report", "email", "sms", "emr", "appeal", "payor_report"]) {
      expect(formatOutputIntentLabel(intent)).toBe(intent);
    }
  });

  it("is case-insensitive and trims", () => {
    expect(formatOutputIntentLabel("  REPORT ")).toBe("report");
  });

  it("returns null for absent/unknown intent — never invents a value", () => {
    expect(formatOutputIntentLabel(undefined)).toBeNull();
    expect(formatOutputIntentLabel("")).toBeNull();
    expect(formatOutputIntentLabel("banana")).toBeNull();
  });

  // The chip must NOT appear on the card face anymore (Chat Master 2026-08-05). output_intent is
  // surfaced only as a Diagnostics telemetry row, which is injected in app.ts (not the renderer).
  it("renders NOTHING on the card face even when output_intent is set", () => {
    const el = renderAnswerCard({ ...card, output_intent: "report" });
    expect(el.querySelector(".answer-card-format-chip")).toBeNull();
    expect(el.querySelector(".answer-card-format-row")).toBeNull();
  });
});
