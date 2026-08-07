import { describe, it, expect } from "vitest";
import { formatAppealsToolProgress } from "./appeals-tool-labels";

describe("formatAppealsToolProgress — before labels", () => {
  it("appeals_find_carc → generic identify line", () => {
    expect(formatAppealsToolProgress({ tool_name: "appeals_find_carc", phase: "before" }))
      .toBe("◌ Identifying denial code from description…");
  });
  it("appeals_lookup_rules → CARC from inputs (with graceful fallback)", () => {
    expect(formatAppealsToolProgress({ tool_name: "appeals_lookup_rules", phase: "before", inputs: { carc: "22" } }))
      .toBe("◌ Looking up CARC 22 rules…");
    expect(formatAppealsToolProgress({ tool_name: "appeals_lookup_rules", phase: "before" }))
      .toBe("◌ Looking up appeal rules…");
  });
  it("appeals_get_playbook → payor from inputs (with fallback)", () => {
    expect(formatAppealsToolProgress({ tool_name: "appeals_get_playbook", phase: "before", inputs: { payor: "Sunshine Health" } }))
      .toBe("◌ Checking playbook for Sunshine Health…");
    expect(formatAppealsToolProgress({ tool_name: "appeals_get_playbook", phase: "before" }))
      .toBe("◌ Checking appeal playbook…");
  });
});

describe("formatAppealsToolProgress — after labels", () => {
  it("appeals_lookup_rules → pluralized rule count + carc_title", () => {
    expect(formatAppealsToolProgress({ tool_name: "appeals_lookup_rules", phase: "after", success: true,
      result: { rules_found: 4, carc_title: "Coordination of benefits" } }))
      .toBe("✓ 4 rules for Coordination of benefits");
    // singular
    expect(formatAppealsToolProgress({ tool_name: "appeals_lookup_rules", phase: "after",
      result: { rules_found: 1, carc_title: "Precert absent" } }))
      .toBe("✓ 1 rule for Precert absent");
    // no count → fallback on inputs.carc
    expect(formatAppealsToolProgress({ tool_name: "appeals_lookup_rules", phase: "after", inputs: { carc: "197" }, result: {} }))
      .toBe("✓ Rules loaded for CARC 197");
  });

  it("appeals_get_playbook → deadline + method, or defaults notice", () => {
    expect(formatAppealsToolProgress({ tool_name: "appeals_get_playbook", phase: "after",
      result: { found: true, deadline_appeal_days: 60, submission_method: "Provider portal" } }))
      .toBe("✓ Playbook: 60d deadline, Provider portal");
    // found but no deadline → payor fallback
    expect(formatAppealsToolProgress({ tool_name: "appeals_get_playbook", phase: "after", inputs: { payor: "Humana" },
      result: { found: true } }))
      .toBe("✓ Playbook loaded for Humana");
    // not found → defaults notice
    expect(formatAppealsToolProgress({ tool_name: "appeals_get_playbook", phase: "after", result: { found: false } }))
      .toBe("✓ No playbook — using FL Medicaid defaults");
  });

  it("appeals_find_carc → top CARC + first match title", () => {
    expect(formatAppealsToolProgress({ tool_name: "appeals_find_carc", phase: "after",
      result: { top_carc: "22", matches: [{ title: "COB — other payer liable" }] } }))
      .toBe("✓ Likely CARC 22 — COB — other payer liable");
    // top but no match title
    expect(formatAppealsToolProgress({ tool_name: "appeals_find_carc", phase: "after",
      result: { top_carc: "22", matches: [{}] } }))
      .toBe("✓ Likely CARC 22");
    // nothing → generic complete
    expect(formatAppealsToolProgress({ tool_name: "appeals_find_carc", phase: "after", result: {} }))
      .toBe("✓ Denial code search complete");
  });
});

describe("formatAppealsToolProgress — returns null for tools it does not own", () => {
  it("non-appeals tool → null (caller uses the plain line)", () => {
    expect(formatAppealsToolProgress({ tool_name: "rag", phase: "before" })).toBeNull();
    expect(formatAppealsToolProgress({ tool_name: "appeals_assemble_letter", phase: "after", result: {} })).toBeNull();
  });
  it("bad input → null, never throws", () => {
    expect(formatAppealsToolProgress(null)).toBeNull();
    expect(formatAppealsToolProgress(undefined)).toBeNull();
    // @ts-expect-error — exercising the runtime guard
    expect(formatAppealsToolProgress({ phase: "before" })).toBeNull();
  });
});
