import { describe, it, expect } from "vitest";
import { pickShadowArm, looksLikeCid, type AbComparison } from "./ab-fork";

// The REAL payload Governor ships (2026-09-11), copied from a live forked turn. The bug this
// guards: a value-type scan picked `view` (a permalink URL) as an arm because it was added
// after the contract. Read STRUCTURE from named fields, never a value's type.
const live: AbComparison = {
  thread_arm: "v2",
  shadow_arms: ["v1"],
  arms: {
    v2: { correlation_id: "d3068deb-aaaa", thread_id: "f8f18c27", served: true },
    v1: { correlation_id: "32b4154e-bbbb", thread_id: "97b41780", served: false },
  },
  shadow: { v1: { correlation_id: "32b4154e-bbbb", thread_id: "97b41780" } },
  v2: "d3068deb-aaaa",
  v1: "32b4154e-bbbb",
  view: "/ab?run=ab-4d9751848e&q=q01",
};

describe("pickShadowArm — reads structure from named fields, not a value's type", () => {
  it("picks the shadow arm and its cid from the live payload", () => {
    expect(pickShadowArm(live, "v2")).toEqual({ shadowArm: "v1", shadowCid: "32b4154e-bbbb" });
  });

  it("NEVER returns the `view` permalink as a cid (the original bug)", () => {
    const got = pickShadowArm(live, "v2");
    expect(got.shadowCid).not.toContain("/");
    expect(got.shadowArm).not.toBe("view");
  });

  it("is immune to an unrelated string field added later (mutation check)", () => {
    // Governor's regression: add a new string field; shadow_arms must still pick exactly one.
    const withNewField: AbComparison = { ...live, permalink_v2: "/x/y", note: "anything" };
    expect(pickShadowArm(withNewField, "v2")).toEqual({ shadowArm: "v1", shadowCid: "32b4154e-bbbb" });
  });

  it("does not assume v1 is the shadow — honors thread_arm/served", () => {
    // Flip which arm was served: v1 served, v2 shadow.
    const flipped: AbComparison = {
      thread_arm: "v1",
      shadow_arms: ["v2"],
      arms: {
        v1: { correlation_id: "aaa1", served: true },
        v2: { correlation_id: "bbb2", served: false },
      },
      v1: "aaa1", v2: "bbb2",
    };
    expect(pickShadowArm(flipped, "v1")).toEqual({ shadowArm: "v2", shadowCid: "bbb2" });
  });

  it("falls back to arms.served when shadow_arms is absent", () => {
    const noShadowArms: AbComparison = {
      thread_arm: "v2",
      arms: { v2: { correlation_id: "s", served: true }, v1: { correlation_id: "h", served: false } },
    };
    expect(pickShadowArm(noShadowArms, "v2")).toEqual({ shadowArm: "v1", shadowCid: "h" });
  });

  it("falls back to legacy flat keys but only cid-looking values", () => {
    // Only the flat keys + a permalink — must pick v1 (a cid), never `view`.
    const legacy: AbComparison = { thread_arm: "v2", v2: "served-cid", v1: "shadow-cid", view: "/ab?run=z" };
    expect(pickShadowArm(legacy, "v2")).toEqual({ shadowArm: "v1", shadowCid: "shadow-cid" });
  });

  it("returns empty when there is genuinely no shadow arm", () => {
    expect(pickShadowArm({ thread_arm: "v2", v2: "only" }, "v2")).toEqual({ shadowArm: "", shadowCid: "" });
  });

  it("looksLikeCid rejects URLs and whitespace, accepts a uuid-ish string", () => {
    expect(looksLikeCid("32b4154e-bbbb")).toBe(true);
    expect(looksLikeCid("/ab?run=x")).toBe(false);
    expect(looksLikeCid("has space")).toBe(false);
    expect(looksLikeCid("")).toBe(false);
    expect(looksLikeCid(undefined)).toBe(false);
  });
});
