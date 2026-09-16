/**
 * Every emitted block reaches the renderer — there is no suppression list.
 *
 * Ananth, 2026-09-15: "lets do the renderEnvelope cutover.. when there is a
 * tool and a preferred UX we should just use it IMO".
 *
 * WHAT THIS REPLACES. An earlier version of this file asserted that
 * `next_steps` was absent from `_suppressedChrome` — a set naming envelope
 * blocks the completed-handler skipped because the card had already drawn
 * them. That set, plus CARD_PROSE_CHROME, FORMAT_BLOCK_TYPES and a
 * format-by-format check of which card sections came out non-empty, were all
 * heuristics standing in for "did the shell already draw this", because two
 * renderers read one contract.
 *
 * The cutover removed the second renderer, so the right assertion is no longer
 * "next_steps is off the list" but "there is no list". A test pinned to the
 * old mechanism would have passed happily on a handler that still had it.
 */
import fs from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";

const appSrc = (): string => {
  const raw = fs.readFileSync(path.resolve(__dirname, "app.ts"), "utf8");
  // strip line comments so this cannot match its own explanation — a failure
  // this repo has produced before
  return raw.replace(/^\s*\/\/.*$/gm, "");
};

describe("renderEnvelope cutover", () => {
  it("has no block suppression list at all", () => {
    expect(appSrc()).not.toContain("_suppressedChrome");
  });

  it("has no card-vs-envelope format reconciliation", () => {
    const s = appSrc();
    for (const relic of ["cardFormatsRendered", "CARD_PROSE_CHROME"]) {
      expect(s).not.toContain(relic);
    }
  });

  it("renders the body through renderEnvelope and hands it to the shell", () => {
    const s = appSrc();
    expect(s).toContain("renderEnvelope(envBlocks");
    expect(s).toContain("envelopeBody: _envelopeBody");
  });

  it("counts blocks nothing rendered instead of dropping them silently", () => {
    // The one failure this architecture can detect for free. Before the
    // cutover a filtered-out block was invisible; that is how next_steps
    // reached readers as a "Tasks 3" badge and no text.
    const s = appSrc();
    expect(s).toContain("onUnknownBlock");
    expect(s).toMatch(/blocks dropped/);
  });
});
