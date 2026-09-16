// @vitest-environment jsdom
import { describe, expect, it } from "vitest";

import { liftBlockElements } from "./lift-block";

/** The shape renderAssistantFromEnvelope produces: an outer node containing a
 *  `.message` wrapper, which holds `.message-bubble` PLUS any deferred
 *  siblings (action chips are drained onto the wrapper, not the bubble). */
function rendered(opts: { inBubble?: string[]; asSibling?: string[] }): HTMLElement {
  const outer = document.createElement("div");
  const msg = document.createElement("div");
  msg.className = "message message--assistant answer-card";
  const bubble = document.createElement("div");
  bubble.className = "message-bubble answer-card-bubble";
  (opts.inBubble ?? []).forEach((c) => {
    const el = document.createElement("div"); el.className = c; bubble.appendChild(el);
  });
  msg.appendChild(bubble);
  (opts.asSibling ?? []).forEach((c) => {
    const el = document.createElement("div"); el.className = c; msg.appendChild(el);
  });
  outer.appendChild(msg);
  return outer;
}

describe("liftBlockElements", () => {
  it("keeps a block that lands OUTSIDE the bubble — the Appeals Agent chip", () => {
    // THE REGRESSION: action_chips drains onto the .message wrapper as a
    // sibling of the bubble. Lifting only the bubble's children dropped it,
    // and the live card lost its "Open Appeals Agent" link.
    const el = liftBlockElements(rendered({ asSibling: ["answer-card-actions"] }));
    expect(el).not.toBeNull();
    expect(el!.querySelector(".answer-card-actions")).not.toBeNull();
  });

  it("unwraps the bubble rather than nesting it", () => {
    const el = liftBlockElements(rendered({ inBubble: ["ac-next-steps"] }));
    expect(el!.querySelector(".message-bubble")).toBeNull();
    expect(el!.querySelector(".ac-next-steps")).not.toBeNull();
  });

  it("keeps BOTH when a block renders inside and outside", () => {
    const el = liftBlockElements(
      rendered({ inBubble: ["a"], asSibling: ["answer-card-actions"] }),
    );
    expect(el!.children.length).toBe(2);
  });

  it("returns null when the block rendered nothing, so it counts as dropped", () => {
    expect(liftBlockElements(rendered({}))).toBeNull();
    expect(liftBlockElements(null)).toBeNull();
  });
});
