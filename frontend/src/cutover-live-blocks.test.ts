// @vitest-environment jsdom
/**
 * The REAL blocks from a live turn, through the REAL renderEnvelope.
 *
 * The cutover's promise is that every emitted block renders exactly once and
 * nothing is silently dropped. This asserts that against a captured live
 * payload rather than a fixture I invented — self-made samples pass every
 * check and prove plumbing, never behaviour.
 */
import fs from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";

import { renderEnvelope, type EnvBlock } from "./render/bubble";

// A REAL captured turn, COMMITTED so this always runs. The first version
// read /tmp and returned early when absent — a silent skip that passes on
// every machine but mine, which is absence-read-as-success.
const LIVE = path.resolve(__dirname, "__fixtures__/live-envelope-blocks.json");

describe("cutover on live blocks", () => {
  it("renders every emitted block, dropping none", () => {
    const d = JSON.parse(fs.readFileSync(LIVE, "utf8"));
    const blocks = (d.assistant_envelope?.blocks ?? []) as EnvBlock[];
    expect(blocks.length).toBeGreaterThan(0);

    const dropped: string[] = [];
    // Extra blocks delegate in the real handler; here we only need to know
    // WHICH types renderEnvelope cannot draw on its own, so the stub records
    // and returns an element.
    const routed: string[] = [];
    const { answerBody, sources } = renderEnvelope(blocks, {
      onUnknownBlock: (t: string) => dropped.push(t),
      renderExtraBlock: (b: EnvBlock) => {
        routed.push(b.type);
        const el = document.createElement("div");
        el.className = "envelope-extra-block";
        el.textContent = b.type;
        return el;
      },
    });

    // Nothing may vanish: every block either drew, routed, or is `sources`
    // (peeled for its own tab).
    expect(dropped).toEqual([]);
    expect(sources).not.toBeNull();

    const emitted = blocks.map((b) => b.type).filter((t) => t !== "sources");
    const drawnDirectly = emitted.filter((t) => !routed.includes(t));
    expect(routed.length + drawnDirectly.length).toBe(emitted.length);

    // And the body must actually contain something to read.
    expect(answerBody.textContent?.trim().length ?? 0).toBeGreaterThan(50);
  });

  it("routes exactly the types renderEnvelope cannot draw itself", () => {
    const d = JSON.parse(fs.readFileSync(LIVE, "utf8"));
    const blocks = (d.assistant_envelope?.blocks ?? []) as EnvBlock[];
    const routed: string[] = [];
    renderEnvelope(blocks, {
      renderExtraBlock: (b: EnvBlock) => {
        routed.push(b.type);
        const el = document.createElement("div");
        el.textContent = b.type;
        return el;
      },
    });
    // Measured on live traffic 2026-09-15. If this set grows, a new block type
    // arrived and the delegation must still cover it.
    for (const t of routed) {
      expect([
        "action_chips", "next_steps", "suggested_questions", "tool_attribution",
        "callout", "correction", "task_list", "document_download",
        "credentialing_card", "pipeline_human_gate", "takeaways", "chart",
        "disambiguation", "attachments", "certified_answer",
      ]).toContain(t);
    }
  });
});
