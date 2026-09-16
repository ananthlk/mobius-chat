/**
 * A block the backend emits must not be silently filed out of sight.
 *
 * Ananth, on a live card, 2026-09-15: "many information is missing and we
 * should check the ux envelope for these cards, i think they are better ..
 * when there is a tool and a preferred UX we should just use it".
 *
 * WHAT WAS HAPPENING. The completed-handler suppresses some envelope blocks
 * when the card has tabs, because the card redraws them and rendering both
 * double-prints. `next_steps` was on that list — so three next steps were
 * emitted, three showed as the "Tasks 3" badge, and the reader saw none of
 * them. Meanwhile `suggested_questions`, the same kind of content, rendered
 * inline as chips. Same card, opposite treatment.
 *
 * The suppression list is legitimate; next_steps did not belong on it.
 */
import fs from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";

/** The list as the PROGRAM holds it — comments stripped, so this cannot match
 *  its own explanation. That failure has happened here before. */
function suppressedTypes(): string[] {
  const src = fs.readFileSync(path.resolve(__dirname, "app.ts"), "utf8");
  const noLineComments = src.replace(/^\s*\/\/.*$/gm, "");
  const m = /_suppressedChrome\s*=\s*new Set\(\s*_hasTabs\s*\?\s*\[([^\]]*)\]/.exec(
    noLineComments,
  );
  if (!m) throw new Error("suppression list not found — did the guard move?");
  return [...m[1].matchAll(/"([a-z_]+)"/g)].map((x) => x[1]);
}

describe("envelope block suppression", () => {
  it("does not suppress next_steps", () => {
    expect(suppressedTypes()).not.toContain("next_steps");
  });

  it("treats next_steps and suggested_questions the same way", () => {
    // They are the same kind of content — what to do next, what to ask next.
    // One rendering inline while the other hides in a tab is the defect.
    const s = suppressedTypes();
    expect(s.includes("next_steps")).toBe(s.includes("suggested_questions"));
  });

  it("still suppresses the blocks the card genuinely redraws", () => {
    // Emptying the list entirely would bring back the duplicate print this
    // guard exists to stop, so the fix must be surgical, not a removal.
    const s = suppressedTypes();
    for (const t of ["tool_attribution", "detail", "callout", "correction"]) {
      expect(s).toContain(t);
    }
  });
});
