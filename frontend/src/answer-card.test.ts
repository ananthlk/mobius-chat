import { describe, it, expect } from "vitest";
import { tryParseAnswerCard, splitSectionsByVisibility, type AnswerCard, type AnswerCardSection } from "./answer-card";

const json = (o: unknown) => JSON.stringify(o);
const sec = (over: Partial<AnswerCardSection> = {}): Record<string, unknown> => ({
  label: "S", format: "bullets", bullets: ["b"], ...over,
});

describe("tryParseAnswerCard — AC-FE-1 (mode optional, min-valid anchor, both gate sites)", () => {
  it("v2 card with no mode + direct_answer parses (mode undefined)", () => {
    const card = tryParseAnswerCard(json({ direct_answer: "hi", sections: [sec()] }));
    expect(card).not.toBeNull();
    expect(card!.mode).toBeUndefined();
    expect(card!.direct_answer).toBe("hi");
  });

  it("legacy FACTUAL card still parses and keeps its mode", () => {
    const card = tryParseAnswerCard(json({ mode: "FACTUAL", direct_answer: "hi", sections: [] }));
    expect(card!.mode).toBe("FACTUAL");
  });

  it("RECITAL card with verbatim parses as recital", () => {
    const card = tryParseAnswerCard(json({ mode: "RECITAL", direct_answer: "x", recital: { verbatim: "quote" } }));
    expect(card!.mode).toBe("RECITAL");
    expect(card!.recital!.verbatim).toBe("quote");
  });

  it("RECITAL without verbatim returns null", () => {
    expect(tryParseAnswerCard(json({ mode: "RECITAL", direct_answer: "x", recital: {} }))).toBeNull();
  });

  // Tech Health hole #1 — unknown mode → treated as absent → v2 path, never null/legacy
  it("unrecognized mode (\"SUMMARY\") is treated as absent → parses, mode undefined", () => {
    const card = tryParseAnswerCard(json({ mode: "SUMMARY", direct_answer: "hi", sections: [sec()] }));
    expect(card).not.toBeNull();
    expect(card!.mode).toBeUndefined();
  });

  // Tech Health C2 — min-valid anchor: loosening mode does not loosen validity
  it("neither valid mode nor direct_answer → null", () => {
    expect(tryParseAnswerCard(json({ sections: [sec()] }))).toBeNull();
  });
  it("empty/whitespace direct_answer → null", () => {
    expect(tryParseAnswerCard(json({ direct_answer: "   ", sections: [] }))).toBeNull();
  });
  it("unknown mode but no direct_answer → still null (anchor holds)", () => {
    expect(tryParseAnswerCard(json({ mode: "SUMMARY", sections: [sec()] }))).toBeNull();
  });

  it("sections absent → defaults to [] (summary-only v2 card is valid)", () => {
    const card = tryParseAnswerCard(json({ direct_answer: "hi" }));
    expect(card).not.toBeNull();
    expect(card!.sections).toEqual([]);
  });

  it("sections beyond MAX_SECTIONS (4) are truncated", () => {
    const card = tryParseAnswerCard(json({ direct_answer: "hi", sections: [sec(), sec(), sec(), sec(), sec(), sec()] }));
    expect(card!.sections.length).toBe(4);
  });

  it("non-JSON garbage → null", () => {
    expect(tryParseAnswerCard("not json at all")).toBeNull();
    expect(tryParseAnswerCard("")).toBeNull();
  });

  // Gate site A: fenced code block
  it("card inside ```json fences parses", () => {
    const card = tryParseAnswerCard("```json\n" + json({ direct_answer: "hi", sections: [] }) + "\n```");
    expect(card!.direct_answer).toBe("hi");
  });

  // Gate site B (:1988 second fallback): legacy anchors on mode regex
  it("legacy card embedded in prose parses via mode-anchored fallback", () => {
    const raw = "Here you go: " + json({ mode: "CANONICAL", direct_answer: "hi", sections: [] }) + " (done)";
    expect(tryParseAnswerCard(raw)!.mode).toBe("CANONICAL");
  });

  // Gate site B for v2 — the new direct_answer-anchored fallback (AC-FE-1 second site)
  it("v2 no-mode card embedded in prose parses via direct_answer-anchored fallback", () => {
    const raw = "Sure — " + json({ direct_answer: "hi", sections: [sec()] }) + " hope that helps";
    const card = tryParseAnswerCard(raw);
    expect(card).not.toBeNull();
    expect(card!.mode).toBeUndefined();
    expect(card!.direct_answer).toBe("hi");
  });

  // Tech Health hole #2 — unknown visibility value is dropped to undefined at parse
  it("unrecognized section.visibility (\"hidden\") is dropped to undefined", () => {
    const card = tryParseAnswerCard(json({ direct_answer: "hi", sections: [sec({ visibility: "hidden" as unknown as undefined })] }));
    expect(card!.sections[0].visibility).toBeUndefined();
  });
  it("valid section.visibility is preserved", () => {
    const card = tryParseAnswerCard(json({ direct_answer: "hi", sections: [sec({ visibility: "detail" as AnswerCardSection["visibility"] })] }));
    expect(card!.sections[0].visibility).toBe("detail");
  });

  // Task #10 regression (2026-08-05 live bug: chip never rendered because parseOne, a positive
  // filter, dropped output_intent — present in the JSON but never copied onto the AnswerCard).
  it("output_intent + display_summary survive parsing (chip depends on this)", () => {
    const card = tryParseAnswerCard(json({ direct_answer: "hi", output_intent: "report", display_summary: "A short report." }));
    expect(card!.output_intent).toBe("report");
    expect(card!.display_summary).toBe("A short report.");
  });
  it("absent output_intent stays undefined (no fabricated value)", () => {
    const card = tryParseAnswerCard(json({ direct_answer: "hi" }));
    expect(card!.output_intent).toBeUndefined();
  });
  it("non-string output_intent is dropped to undefined", () => {
    const card = tryParseAnswerCard(json({ direct_answer: "hi", output_intent: 42 as unknown as string }));
    expect(card!.output_intent).toBeUndefined();
  });
});

describe("splitSectionsByVisibility — AC-FE-2 / §5 discriminator / §1.2 fallback", () => {
  const s = (over: Partial<AnswerCardSection>): AnswerCardSection => ({ label: "", bullets: [], ...over });

  it("v2: explicit primary/detail split", () => {
    const { visible, hidden } = splitSectionsByVisibility(
      [s({ label: "a", visibility: "primary" }), s({ label: "b", visibility: "detail" })], undefined);
    expect(visible.map((x) => x.label)).toEqual(["a"]);
    expect(hidden.map((x) => x.label)).toEqual(["b"]);
  });

  // §1.2 fallback — absent visibility, no mode: first primary, rest detail
  it("v2: absent visibility → first primary, rest detail", () => {
    const { visible, hidden } = splitSectionsByVisibility(
      [s({ label: "a" }), s({ label: "b" }), s({ label: "c" })], undefined);
    expect(visible.map((x) => x.label)).toEqual(["a"]);
    expect(hidden.map((x) => x.label)).toEqual(["b", "c"]);
  });

  // Tech Health hole #2 — a section missing visibility among flagged ones falls to §1.2 (never dark-renders)
  it("v2: a section with no visibility among flagged siblings is placed, never dropped", () => {
    const secs = [s({ label: "a", visibility: "primary" }), s({ label: "b" }), s({ label: "c", visibility: "detail" })];
    const { visible, hidden } = splitSectionsByVisibility(secs, undefined);
    const all = [...visible, ...hidden].map((x) => x.label).sort();
    expect(all).toEqual(["a", "b", "c"]); // nothing lost
    expect(hidden.map((x) => x.label)).toContain("b"); // index>0, no flag → detail
  });

  it("legacy FACTUAL (no visibility) → all hidden", () => {
    const secs = [s({ label: "a" }), s({ label: "b" })];
    expect(splitSectionsByVisibility(secs, "FACTUAL").visible).toEqual([]);
    expect(splitSectionsByVisibility(secs, "FACTUAL").hidden.length).toBe(2);
  });
  it("legacy CANONICAL (no visibility) → all visible", () => {
    const secs = [s({ label: "a" }), s({ label: "b" })];
    expect(splitSectionsByVisibility(secs, "CANONICAL").hidden).toEqual([]);
  });
  it("legacy BLENDED (no visibility) → intent-based split", () => {
    const secs = [s({ label: "req", intent: "requirements" }), s({ label: "exc", intent: "exceptions" })];
    const { visible, hidden } = splitSectionsByVisibility(secs, "BLENDED");
    expect(visible.map((x) => x.label)).toEqual(["req"]);
    expect(hidden.map((x) => x.label)).toEqual(["exc"]);
  });

  // §5 collision — legacy mode + per-section visibility present → visibility wins
  it("collision: FACTUAL mode but sections carry visibility → visibility wins (not all-hidden)", () => {
    const secs = [s({ label: "a", visibility: "primary" }), s({ label: "b", visibility: "detail" })];
    const { visible, hidden } = splitSectionsByVisibility(secs, "FACTUAL");
    expect(visible.map((x) => x.label)).toEqual(["a"]); // FACTUAL alone would hide all
    expect(hidden.map((x) => x.label)).toEqual(["b"]);
  });

  it("empty sections → empty split", () => {
    expect(splitSectionsByVisibility([], undefined)).toEqual({ visible: [], hidden: [] });
  });
});
