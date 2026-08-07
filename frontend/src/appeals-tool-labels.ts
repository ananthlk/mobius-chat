// Appeals tool progress labels — the UX envelope for the appeals MCP tools' before/after
// progress lines. Architecture ruling (Chat Master 2026-08-07): the adapter passes RAW fields;
// the frontend owns the label formatting. This module is the FE half — a pure function over the
// structured `tool_progress` signal the adapter emits, so it's unit-testable without the DOM/SSE.
//
// Scope: the three appeals discovery tools. Every other tool keeps its adapter-formatted string
// (this returns null for them, and the caller falls back to the plain progress line).

export interface ToolProgressSignal {
  tool_name: string;
  phase: "before" | "after";
  success?: boolean;
  /** call inputs — carc / payor / description etc. */
  inputs?: Record<string, unknown> | null;
  /** parsed tool result (present on "after"): rules_found, carc_title, found,
   *  deadline_appeal_days, submission_method, top_carc, matches[] */
  result?: Record<string, unknown> | null;
}

const _s = (o: Record<string, unknown> | null | undefined, k: string): string => {
  const v = o?.[k];
  return v == null ? "" : String(v);
};
const _n = (o: Record<string, unknown> | null | undefined, k: string): number | null => {
  const v = o?.[k];
  return typeof v === "number" && Number.isFinite(v) ? v : null;
};

/**
 * Format an appeals tool's progress label from its raw signal, or return null when the tool
 * isn't one this module owns (caller then uses the plain progress line). Never throws —
 * missing fields degrade to sensible fallbacks, mirroring the adapter's prior behavior.
 */
export function formatAppealsToolProgress(sig: ToolProgressSignal | null | undefined): string | null {
  if (!sig || typeof sig.tool_name !== "string") return null;
  const { tool_name, phase } = sig;
  const inputs = sig.inputs ?? {};
  const result = sig.result ?? {};

  if (phase === "before") {
    switch (tool_name) {
      case "appeals_find_carc":
        return "◌ Identifying denial code from description…";
      case "appeals_lookup_rules": {
        const carc = _s(inputs, "carc");
        return carc ? `◌ Looking up CARC ${carc} rules…` : "◌ Looking up appeal rules…";
      }
      case "appeals_get_playbook": {
        const payor = _s(inputs, "payor");
        return payor ? `◌ Checking playbook for ${payor}…` : "◌ Checking appeal playbook…";
      }
      default:
        return null;
    }
  }

  if (phase === "after") {
    switch (tool_name) {
      case "appeals_lookup_rules": {
        const n = _n(result, "rules_found");
        const title = _s(result, "carc_title") || (`CARC ${_s(inputs, "carc")}`).trim();
        if (n != null) return `✓ ${n} rule${n !== 1 ? "s" : ""} for ${title || "this denial"}`;
        const carc = _s(inputs, "carc");
        return carc ? `✓ Rules loaded for CARC ${carc}` : "✓ Appeal rules loaded";
      }
      case "appeals_get_playbook": {
        const found = result["found"] === true;
        if (found) {
          const days = _n(result, "deadline_appeal_days");
          const method = _s(result, "submission_method");
          if (days != null) return `✓ Playbook: ${days}d deadline${method ? `, ${method}` : ""}`;
          const payor = _s(inputs, "payor");
          return payor ? `✓ Playbook loaded for ${payor}` : "✓ Playbook loaded";
        }
        return "✓ No playbook — using FL Medicaid defaults";
      }
      case "appeals_find_carc": {
        const top = _s(result, "top_carc");
        const matches = Array.isArray(result["matches"]) ? (result["matches"] as Array<Record<string, unknown>>) : [];
        if (top && matches.length > 0) {
          const title = _s(matches[0], "title");
          return title ? `✓ Likely CARC ${top} — ${title}` : `✓ Likely CARC ${top}`;
        }
        return "✓ Denial code search complete";
      }
      default:
        return null;
    }
  }

  return null;
}
