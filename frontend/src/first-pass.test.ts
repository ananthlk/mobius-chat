// @vitest-environment jsdom
/**
 * The working notes start closed, and a round that changed nothing is not
 * shown twice.
 *
 * Ananth, on a live card: "IT STILL FEELS BADLY FORMATTED, NOT CARDS.. THINK
 * ABOUT A FRONT END USER, THEY ARE NOT PROGRAMMER".
 *
 * Two defects fed that:
 *
 *  1. `collapsed_default` was DECLARED on the block type, PRODUCED by the
 *     backend (true), and never read by the renderer — which also never set an
 *     initial max-height. So the model's reasoning rendered OPEN, and the
 *     first thing a reader met was rd-1/rd-2 working notes rather than their
 *     answer.
 *
 *  2. Consecutive rounds carrying the SAME running_answer were both printed —
 *     measured on a live card, rd-2 and rd-3 byte-identical. The reader saw
 *     one paragraph twice under two labels.
 */
import { describe, expect, it } from "vitest";

import { renderFirstPass } from "./render/bubble";

const round = (n: number, ans: string) => ({ round: n, running_answer: ans });

describe("first pass", () => {
  it("starts COLLAPSED when the backend says so", () => {
    const el = renderFirstPass({
      type: "first_pass", collapsed_default: true,
      trace_rounds: [round(1, "a"), round(2, "b")],
    })!;
    const body = el.querySelector(".ac-first-pass-body") as HTMLElement;
    expect(body.style.maxHeight).toBe("0px");
    expect(el.classList.contains("ac-first-pass--open")).toBe(false);
  });

  it("defaults to collapsed when the field is absent", () => {
    // Absent must not mean open — an unread field is how this shipped wrong.
    const el = renderFirstPass({
      type: "first_pass", trace_rounds: [round(1, "a")],
    })!;
    expect((el.querySelector(".ac-first-pass-body") as HTMLElement).style.maxHeight)
      .toBe("0px");
  });

  it("opens only when explicitly told to", () => {
    const el = renderFirstPass({
      type: "first_pass", collapsed_default: false,
      trace_rounds: [round(1, "a")],
    })!;
    expect(el.classList.contains("ac-first-pass--open")).toBe(true);
  });

  it("does not print the same round twice", () => {
    const same = "Sunshine Health appeals have a 90-day deadline.";
    const el = renderFirstPass({
      type: "first_pass",
      trace_rounds: [round(1, "looking it up"), round(2, same), round(3, same)],
    })!;
    expect(el.querySelectorAll(".ac-rd-step")).toHaveLength(2);
    expect(el.querySelector(".ac-first-pass-summary")!.textContent)
      .toContain("2 rounds");
  });

  it("keeps a genuine progression", () => {
    const el = renderFirstPass({
      type: "first_pass",
      trace_rounds: [round(1, "first"), round(2, "second"), round(3, "third")],
    })!;
    expect(el.querySelectorAll(".ac-rd-step")).toHaveLength(3);
  });
});
