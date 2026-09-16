/**
 * Block-level lists in simpleMarkdownToHtml.
 *
 * Ananth, 2026-09-15, on a live card: "the formatting on the screen is off..
 * we need really good deterministic formatting, if not this goes for a toss".
 *
 * What he was looking at:
 *
 *     * **Appeal Arguments:** You can argue that the member had no other...
 *     * **Required Documents:** Make sure to include a completed...
 *
 * with the ASTERISKS VISIBLE and the bold correctly applied. simpleMarkdownToHtml
 * converted bold, headings, links and paragraphs and had NO list handling, so
 * `* item` fell through to the newline -> <br> pass and arrived as literal
 * text. Bold working while bullets did not is why it read as a content bug.
 */
import { describe, expect, it } from "vitest";

import { simpleMarkdownToHtml } from "./ui-helpers";

describe("simpleMarkdownToHtml — block lists", () => {
  it("renders the exact answer from the live card as a list, not literal asterisks", () => {
    const md = [
      "To appeal a **CARC 22** denial you must submit within **90 days**.",
      "",
      "*   **Appeal Arguments:** the member had no other coverage.",
      "*   **Required Documents:** the Claim Adjustment Request Form.",
      "*   **Submission Methods:** mail, fax, or the Secure Provider Portal.",
    ].join("\n");
    const html = simpleMarkdownToHtml(md);

    expect(html).toContain("<ul>");
    expect(html).toContain("<li><strong>Appeal Arguments:</strong>");
    expect(html.match(/<li>/g)).toHaveLength(3);
    // THE REGRESSION: a literal marker reaching the reader.
    expect(html).not.toMatch(/(^|>)\s*\*\s/);
  });

  it("does not mistake emphasis for a list marker", () => {
    // A marker must be followed by whitespace. *word* is emphasis, not a list.
    const html = simpleMarkdownToHtml("an *emphasised* word");
    expect(html).not.toContain("<ul>");
  });

  it("supports -, + and numbered lists", () => {
    expect(simpleMarkdownToHtml("- one\n- two")).toContain("<ul>");
    expect(simpleMarkdownToHtml("+ one\n+ two")).toContain("<ul>");
    const ol = simpleMarkdownToHtml("1. first\n2. second");
    expect(ol).toContain("<ol>");
    expect(ol.match(/<li>/g)).toHaveLength(2);
  });

  it("joins a wrapped bullet into ONE item", () => {
    // Long bullets arrive wrapped from the model; two <li> for one bullet is
    // the same defect wearing structure.
    const html = simpleMarkdownToHtml("* a bullet that\ncontinues here");
    expect(html.match(/<li>/g)).toHaveLength(1);
    expect(html).toContain("a bullet that continues here");
  });

  it("never wraps a list in a paragraph or splits it with <br>", () => {
    const html = simpleMarkdownToHtml("lead in\n\n* one\n* two");
    expect(html).not.toMatch(/<p>\s*<ul>/);
    expect(html).not.toMatch(/<li>[^<]*<br>/);
  });

  it("leaves prose exactly as it was", () => {
    // The change must be inert for every answer that has no list.
    const prose = "A **bold** claim.\n\nA second paragraph.";
    expect(simpleMarkdownToHtml(prose)).toBe(
      "<p>A <strong>bold</strong> claim.</p><p>A second paragraph.</p>",
    );
  });
});
