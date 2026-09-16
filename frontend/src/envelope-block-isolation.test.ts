// @vitest-environment jsdom
/**
 * One bad block must not erase the answer.
 *
 * LIVE, 2026-09-16, deploy 58e1fb8. A complete appeal answer — numbered steps,
 * two tables, a playbook card, 13 sources — was blanked by:
 *
 *     TypeError: lv.submission.trim is not a function
 *
 * The appeals card reads `appeal_levels[].submission` as a STRING; the service
 * returns a LIST. Since the renderEnvelope cutover this function is the SINGLE
 * producer of the answer body, so one leaf renderer throwing takes the whole
 * turn's content with it.
 *
 * `dropped` / onUnknownBlock already exist to report a block that cannot draw
 * itself. A block that THROWS is the same fact arriving differently.
 */
import { describe, expect, it } from "vitest";

import { renderEnvelope, renderFormatBlock, type EnvBlock } from "./render/bubble";

describe("block isolation", () => {
  it("drops a throwing block and keeps the rest of the answer", () => {
    const dropped: string[] = [];
    const { answerBody } = renderEnvelope(
      [
        { type: "direct_answer", markdown: "The deadline is **90 days**." },
        { type: "explodes" } as unknown as EnvBlock,
        { type: "direct_answer", markdown: "Submit via the portal." },
      ] as EnvBlock[],
      {
        onUnknownBlock: (t: string) => dropped.push(t),
        renderExtraBlock: (b: EnvBlock) => {
          if (b.type === "explodes") throw new TypeError("boom");
          return null;
        },
      },
    );
    expect(dropped).toContain("explodes");
    expect(answerBody.textContent).toContain("90 days");
    expect(answerBody.textContent).toContain("Submit via the portal");
  });

  it("renders the appeals card when submission is a LIST", () => {
    // The exact live payload shape from the appeals service.
    const el = renderFormatBlock({
      type: "domain_card", variant: "appeals_playbook",
      data: {
        payor: "Sunshine Health", found: true,
        deadline_appeal_days: 90, submission_method: "portal",
        appeal_levels: [{
          name: "Internal Appeal", level: 1, deadline_days: 90,
          submission: ["mail", "fax", "portal", "phone", "email"],
        }],
      },
    } as never);
    expect(el).not.toBeNull();
    expect(el!.textContent).toContain("Internal Appeal");
    expect(el!.textContent).toContain("mail, fax, portal");
  });

  it("still renders when submission is a STRING", () => {
    const el = renderFormatBlock({
      type: "domain_card", variant: "appeals_playbook",
      data: {
        payor: "Sunshine Health", found: true, deadline_appeal_days: 90,
        appeal_levels: [{ name: "Internal Appeal", submission: "portal" }],
      },
    } as never);
    expect(el!.textContent).toContain("portal");
  });
});
