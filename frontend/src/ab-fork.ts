/**
 * A/B fork comparison-block helpers — PURE, so they can be unit-tested without importing
 * app.ts (which runs side effects on load). The comparison block ships on a forked /chat turn.
 *
 * The load-bearing rule (Governor, 2026-09-11): read STRUCTURE from NAMED fields, never infer
 * it from a value's TYPE. Scanning for "string-valued keys" once picked up `view` (a permalink
 * URL) as if it were an arm — because the producer added a field after the contract was sent.
 * `shadow_arms` + `arms[id]` are authoritative and immune to any field added next.
 */

export interface AbComparison {
  thread_arm: string;
  shadow_arms?: string[];
  arms?: { [arm: string]: { correlation_id?: string; thread_id?: string; served?: boolean } };
  shadow?: { [arm: string]: { correlation_id?: string; thread_id?: string } };
  view?: string;
  [k: string]: unknown;
}

/** A correlation id has no whitespace and no "/". A permalink ("/ab?run=…") is NOT a cid. */
export function looksLikeCid(v: unknown): v is string {
  return typeof v === "string" && v.length > 0 && !/[\s/]/.test(v);
}

/**
 * Pick the shadow arm + its correlation_id from named fields, in order of authority:
 *   1) `shadow_arms` + `arms[id].correlation_id`
 *   2) `arms` entry with `served === false`
 *   3) legacy `shadow[id].correlation_id`
 *   4) legacy flat `v1`/`v2` keys — but ONLY cid-looking values (so `view`/`thread_arm` can't win)
 */
export function pickShadowArm(
  comparison: AbComparison,
  servedArm: string,
): { shadowArm: string; shadowCid: string } {
  const cidFor = (arm: string): string => {
    const fromArms = comparison.arms?.[arm]?.correlation_id;
    if (looksLikeCid(fromArms)) return fromArms;
    const fromShadow = comparison.shadow?.[arm]?.correlation_id;
    if (looksLikeCid(fromShadow)) return fromShadow;
    const flat = comparison[arm];
    return looksLikeCid(flat) ? flat : "";
  };

  if (Array.isArray(comparison.shadow_arms) && comparison.shadow_arms.length) {
    const arm = String(comparison.shadow_arms[0]);
    return { shadowArm: arm, shadowCid: cidFor(arm) };
  }
  if (comparison.arms) {
    const arm = Object.keys(comparison.arms).find((k) => comparison.arms![k]?.served === false);
    if (arm) return { shadowArm: arm, shadowCid: cidFor(arm) };
  }
  if (comparison.shadow) {
    const arm = Object.keys(comparison.shadow).find((k) => k !== servedArm);
    if (arm) return { shadowArm: arm, shadowCid: cidFor(arm) };
  }
  const arm = Object.keys(comparison).find(
    (k) => k !== servedArm && k !== "thread_arm" && looksLikeCid(comparison[k]),
  );
  return arm ? { shadowArm: arm, shadowCid: cidFor(arm) } : { shadowArm: "", shadowCid: "" };
}
