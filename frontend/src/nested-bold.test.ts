import { describe, it, expect } from "vitest";
import { simpleMarkdownToHtml } from "./ui-helpers";

// Verbatim from live pinned turn e7116981 (deploy 22). v2's format rules told
// the model to bold the whole opening sentence AND to bold deadlines inside it.
// Both were obeyed. Markdown cannot nest bold.
const LIVE = "**To appeal a CARC 22 denial from Sunshine Health, submit a written dispute using the Provider Claim Adjustment Request Form within **90 days** of the determination.**";

describe("nested bold does not invert emphasis", () => {
  it("bolds the deadline rather than everything except the deadline", () => {
    const html = simpleMarkdownToHtml(LIVE);
    // The defect was not 'missing bold' -- it was emphasis landing on the
    // filler and skipping the one fact that matters. Assert the fact is bold.
    expect(html).toContain("<strong>90 days</strong>");
    expect(html).not.toMatch(/<strong>To appeal[^<]*<\/strong>90 days/);
  });

  it("keeps the sentence's words, losing none to the unwrap", () => {
    const html = simpleMarkdownToHtml(LIVE);
    const text = html.replace(/<[^>]+>/g, "");
    expect(text).toContain("To appeal a CARC 22 denial from Sunshine Health");
    expect(text).toContain("of the determination.");
  });

  it("leaves an ordinary single bold phrase untouched", () => {
    // The unwrap must fire ONLY on an outer wrapper, never on a line that is
    // simply one bold phrase -- otherwise it eats legitimate emphasis.
    expect(simpleMarkdownToHtml("**Appeal deadline**")).toContain("<strong>Appeal deadline</strong>");
  });

  it("still bolds normally when nothing is nested", () => {
    const html = simpleMarkdownToHtml("Submit within **90 days** of the date.");
    expect(html).toContain("<strong>90 days</strong>");
  });
});
