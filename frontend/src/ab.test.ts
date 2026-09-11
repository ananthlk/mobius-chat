// @vitest-environment jsdom
/**
 * A/B page render tests — driven by the REAL endpoint payload (src/__fixtures__/ab_q01_live.json,
 * copied verbatim from GET /ab/runs/ab-3fb1e4a6a3/q/q01 on the dev service). Not a hand-written
 * fixture: the whole point of the harness is fidelity, so the test grades the same bytes the page
 * will receive. Governor shipped two defects past nine green tests that only a live call caught —
 * this test asserts against the live shape for that reason.
 */
import { describe, it, expect, beforeEach } from "vitest";
import { renderComparison, type Comparison } from "./ab";
import live from "./__fixtures__/ab_q01_live.json";

const cmp = live as unknown as Comparison;

describe("A/B page — renders the live q01 payload", () => {
  let root: HTMLElement;
  beforeEach(() => { root = document.createElement("div"); renderComparison(cmp, root); });

  it("shows the harness banner (production cannot fork — said in the page)", () => {
    expect(root.querySelector(".ab-banner")?.textContent).toContain("Nobody was served");
  });

  it("renders the experiment header with held/varied from data", () => {
    expect(root.querySelector(".ab-exp-header")).toBeTruthy();
    const held = root.querySelector(".ab-exp-held .ab-exp-val")?.textContent || "";
    expect(held).toContain("question");
    // single-arm run: varied is empty → the honest 'nothing — single-arm capture'
    expect(root.querySelector(".ab-exp-varied .ab-exp-val")?.textContent).toContain("single-arm");
  });

  it("is SINGLE column today (arms.length === 1), not a flag", () => {
    const grid = root.querySelector(".ab-grid") as HTMLElement;
    expect(grid.style.getPropertyValue("--ab-cols")).toBe("1");
    expect(grid.querySelectorAll(".ab-box").length).toBe(1);
  });

  it("renders the answer through the PRODUCTION renderer (real envelope body present)", () => {
    // renderEnvelope emits .ac-answer-final; a re-implementation wouldn't.
    expect(root.querySelector(".ab-box .ac-answer-final")).toBeTruthy();
    // direct_answer prose made it into the body
    expect((root.querySelector(".ab-answer")?.textContent || "").length).toBeGreaterThan(20);
  });

  it("renders the tool_attribution chrome chip (same class as the bubble)", () => {
    expect(root.querySelector(".envelope-tool-chip")).toBeTruthy();
  });

  it("renders the peeled sources list from sources.refs", () => {
    const sources = root.querySelector(".ac-sources-footnotes");
    expect(sources).toBeTruthy();
    expect(sources?.querySelectorAll(".ac-source-item").length).toBeGreaterThan(0);
  });

  it("has NO divergences section (single arm has nothing to diverge from)", () => {
    const texts = [...root.querySelectorAll(".ab-collapsible-summary")].map((e) => e.textContent || "");
    expect(texts.some((t) => t.startsWith("Divergences"))).toBe(false);
  });

  it("round-by-round trace is collapsed by default (near-production resting state)", () => {
    const traceDetails = [...root.querySelectorAll(".ab-collapsible")].find(
      (d) => (d.querySelector(".ab-collapsible-summary")?.textContent || "").startsWith("Round-by-round"),
    ) as HTMLDetailsElement | undefined;
    expect(traceDetails).toBeTruthy();
    expect(traceDetails!.open).toBe(false);
  });

  it("terms show latency 21.5s and kept ✓ from the live delivered/kept fields", () => {
    const terms = root.querySelector(".ab-terms-table")?.textContent || "";
    expect(terms).toContain("21.5s");   // delivered.latency_ms 21483
    expect(terms).toContain("✓");        // kept: true
  });

  it("null cost renders '—', never '0' (unset ≠ measured-zero)", () => {
    // delivered.cost_usd is null on every row today; a 0 would be a step-1 failure shown as cheap.
    const rows = [...root.querySelectorAll(".ab-terms-table tr")];
    const costRow = rows.find((r) => (r.querySelector(".ab-terms-label")?.textContent || "") === "cost");
    expect(costRow?.querySelector(".ab-terms-val")?.textContent).toBe("—");
  });
});
