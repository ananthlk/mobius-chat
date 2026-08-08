// @vitest-environment jsdom
import { describe, it, expect } from "vitest";
import { renderAnswerCard, formatOutputIntentLabel } from "./bubble";
import type { AnswerCard } from "../answer-card";

// A v2 (no-mode) card: primary section leads, detail tucks; citations light up their tab.
const card: AnswerCard = {
  direct_answer: "Yes — reimbursed at parity.",
  sections: [
    { label: "Covered codes", visibility: "primary", format: "table", bullets: [],
      data: { headers: ["Code", "Rate"], rows: [["H0031", "$149.60"]] } },
    { label: "Documentation", visibility: "primary", format: "bullets", bullets: ["Real-time A/V"] },
    { label: "Exceptions", visibility: "detail", format: "bullets", bullets: ["School-based differs"] },
  ],
  citations: [{ id: "1", doc_title: "AHCA Handbook", locator: "§59G", snippet: "parity" }],
};

describe("renderAnswerCard — DOM output (§1.4 tabbed bubble + §1.2 visibility)", () => {
  it("renders the direct answer and a v2 (no-mode) card class", () => {
    const el = renderAnswerCard(card);
    expect(el.className).toContain("answer-card--v2");
    expect(el.querySelector(".answer-card-direct")?.textContent).toContain("parity");
  });

  it("sections render in the Answer tab (integrator output), NOT in Summary (Ananth 2026-08-07)", () => {
    const el = renderAnswerCard(card);
    // Summary is ReAct's synthesis — the integrator's section labels are NOT stacked into it.
    const summary = el.querySelector(".ac-tab-panel--summary")!;
    const summaryText = summary.textContent ?? "";
    expect(summaryText).not.toContain("Covered codes");
    expect(summaryText).not.toContain("Exceptions");
    // All sections live in the Answer panel, rendered flat (no Show-details collapse anymore).
    const answer = el.querySelector(".ac-tab-panel--answer")!;
    expect(answer.textContent).toContain("Covered codes");
    expect(answer.textContent).toContain("Documentation");
    expect(answer.textContent).toContain("Exceptions");
    expect(el.querySelector(".answer-card-show-details")).toBeNull();
  });

  it("renders a tab bar with a Draft tab (renamed from Summary) + a Sources tab (ruling b)", () => {
    const el = renderAnswerCard(card);
    const tabs = Array.from(el.querySelectorAll(".ac-tab")).map((t) => t.textContent ?? "");
    // "Summary" renamed to "Draft" (Ananth 2026-08-07) — panel key stays "summary".
    expect(tabs.some((t) => t.includes("Draft"))).toBe(true);
    expect(tabs.some((t) => t === "Summary")).toBe(false);
    // Follow-up tab dropped (chips handle it)
    expect(tabs.some((t) => t.includes("Follow-up"))).toBe(false);
    // The Citations tab was consolidated into "Sources" (label change; panel key stays citations).
    expect(tabs.some((t) => t.includes("Sources"))).toBe(true);
    expect(tabs.some((t) => t.includes("Citations"))).toBe(false);
    // the citation still lands in its own (Sources) panel, not stacked into Summary
    expect(el.querySelector(".ac-tab-panel--citations")?.textContent).toContain("AHCA Handbook");
  });

  it("renders the typed table envelope (§1.3)", () => {
    const el = renderAnswerCard(card);
    const table = el.querySelector("table.ac-fmt-table");
    expect(table).not.toBeNull();
    expect(table?.textContent).toContain("H0031");
  });

  it("a no-section summary-only card still renders (min-valid anchor)", () => {
    const el = renderAnswerCard({ direct_answer: "Short answer." });
    expect(el.querySelector(".answer-card-direct")?.textContent).toContain("Short answer.");
  });

  it("injected onCreateTask is the only app-state hook (DI) — renderer takes it via opts", () => {
    // The renderer must accept the handler without importing app.ts; passing it must not throw.
    let called = false;
    const el = renderAnswerCard(card, false, { onCreateTask: () => { called = true; } });
    expect(el).toBeTruthy();
    expect(called).toBe(false); // not invoked at render time, only on user click
  });
});

describe("Answer tab (Ananth 2026-08-07 — Summary=react_draft, Answer=display_summary, mode-labeled)", () => {
  it("renders an Answer tab with the CANONICAL badge and display_summary when present", () => {
    const el = renderAnswerCard({ ...card, mode: "CANONICAL", display_summary: "The authoritative final answer, in full." });
    // Answer tab button exists, positioned after Summary
    const tabs = Array.from(el.querySelectorAll(".ac-tab")).map((t) => t.textContent ?? "");
    expect(tabs.some((t) => t.includes("Answer"))).toBe(true);
    // panel carries the mode badge (CANONICAL signals authoritative content) + the display_summary body
    const panel = el.querySelector(".ac-tab-panel--answer")!;
    expect(panel).not.toBeNull();
    expect(panel.querySelector(".ac-answer-mode-label")?.textContent).toBe("CANONICAL");
    expect(panel.querySelector(".ac-answer-envelope-body")?.textContent).toContain("authoritative final answer");
  });

  it("renders NO mode badge for FACTUAL/BLENDED (default path — nothing to signal; Chat Master 2026-08-07)", () => {
    const factual = renderAnswerCard({ ...card, mode: "FACTUAL", display_summary: "A factual answer." });
    expect(factual.querySelector(".ac-tab-panel--answer .ac-answer-mode-label")).toBeNull();
    const blended = renderAnswerCard({ ...card, mode: "BLENDED", display_summary: "A blended answer." });
    expect(blended.querySelector(".ac-tab-panel--answer .ac-answer-mode-label")).toBeNull();
    // but the Answer tab + body still render
    expect(factual.querySelector(".ac-tab-panel--answer .ac-answer-envelope-body")?.textContent).toContain("factual answer");
  });

  it("keeps display_summary OUT of the Summary panel (Summary is react_draft's surface, not the envelope)", () => {
    const el = renderAnswerCard({ ...card, mode: "FACTUAL", display_summary: "ENVELOPE_ONLY_TEXT_XYZ" });
    const summary = el.querySelector(".ac-tab-panel--summary")!;
    expect(summary.textContent ?? "").not.toContain("ENVELOPE_ONLY_TEXT_XYZ");
    // it IS in the Answer panel
    expect(el.querySelector(".ac-tab-panel--answer")?.textContent).toContain("ENVELOPE_ONLY_TEXT_XYZ");
  });

  it("fires the Answer tab on sections[] even when display_summary is EMPTY (appeals turn, cid 4d9456e2)", () => {
    // Real appeals turns lead with rich sections[] and an empty display_summary — the Answer tab
    // must still render (guard is display_summary OR sections).
    const el = renderAnswerCard({
      direct_answer: "Here's the appeal path.",
      mode: "FACTUAL",
      sections: [{ label: "CARC 22 rules", visibility: "primary", format: "bullets", bullets: ["Coordinate benefits first"] }],
    });
    const tabs = Array.from(el.querySelectorAll(".ac-tab")).map((t) => t.textContent ?? "");
    expect(tabs.some((t) => t.includes("Answer"))).toBe(true);
    const answer = el.querySelector(".ac-tab-panel--answer")!;
    expect(answer.textContent).toContain("CARC 22 rules");
    expect(answer.textContent).toContain("Coordinate benefits first");
  });

  it("renders tldr_summary as the Answer lead when present; hides it when empty", () => {
    const withTldr = renderAnswerCard({ ...card, mode: "BLENDED", tldr_summary: "Two-sentence verdict here." });
    expect(withTldr.querySelector(".ac-answer-tldr")?.textContent).toContain("verdict");
    const noTldr = renderAnswerCard({ ...card, mode: "BLENDED", display_summary: "prose" });
    expect(noTldr.querySelector(".ac-answer-tldr")).toBeNull();
  });

  it("Summary (the prominent answer) shows react_draft when present, else direct_answer (reload, 60091bd)", () => {
    const withDraft = renderAnswerCard({ ...card, direct_answer: "INTEGRATOR_LINE", react_draft: "REACT_SYNTHESIS_LINE" });
    expect(withDraft.querySelector(".answer-card-direct")?.textContent).toContain("REACT_SYNTHESIS_LINE");
    expect(withDraft.querySelector(".answer-card-direct")?.textContent).not.toContain("INTEGRATOR_LINE");
    // absent react_draft → falls back to direct_answer (older turns)
    const noDraft = renderAnswerCard({ ...card, direct_answer: "INTEGRATOR_LINE" });
    expect(noDraft.querySelector(".answer-card-direct")?.textContent).toContain("INTEGRATOR_LINE");
  });

  it("shows NO Answer tab when there is neither display_summary NOR sections", () => {
    const el = renderAnswerCard({ direct_answer: "Just a sentence.", sections: [] });
    const tabs = Array.from(el.querySelectorAll(".ac-tab")).map((t) => t.textContent ?? "");
    expect(tabs.some((t) => t.includes("Answer"))).toBe(false);
    // the panel element is still built (empty, hidden) so the streaming panel-swap has a target
    const panel = el.querySelector(".ac-tab-panel--answer");
    expect(panel).not.toBeNull();
    expect((panel?.textContent ?? "").trim()).toBe("");
  });
});

describe("Appeals typed sections (appeals_rules / appeals_playbook)", () => {
  const appealsCard: AnswerCard = {
    direct_answer: "Here's the appeal path for CARC 197.",
    sections: [
      {
        label: "Appeal rules", visibility: "primary", format: "appeals_rules", bullets: [],
        // data is passed verbatim from the tool — shape is AppealsRulesData, not SectionData.
        data: {
          carc: "197", carc_title: "Precert absent", archetype: "authorization",
          rules_found: 1,
          rules: [{
            rule_id: "R-197-a", rule_name: "Retro-auth window",
            rule_statement: "Plans must honor retro-auth within 30 days.",
            triggers_when: "Service was medically urgent and auth was obtained within 30 days.",
            appeal_argument: "Auth was obtained retroactively within the plan's 30-day window; deny is improper.",
            requires: ["Auth confirmation", "Urgency note"],
            authority_notes: "FL 59G-1.010",
            payor_variants: [{ payor: "Sunshine", note: "14-day window" }, "Humana"],
          }],
        } as unknown as AnswerCard["sections"][number]["data"],
      },
    ],
  };

  it("renders the CARC badge, archetype chip, and the rule row", () => {
    const el = renderAnswerCard(appealsCard);
    const wrap = el.querySelector(".ac-appeals-rules")!;
    expect(wrap).not.toBeNull();
    expect(wrap.querySelector(".ac-appeals-carc")?.textContent).toContain("197");
    expect(wrap.querySelector(".ac-appeals-archetype")?.textContent).toContain("authorization");
    expect(wrap.querySelector(".ac-appeals-rule-id")?.textContent).toContain("R-197-a");
  });

  it("highlights appeal_argument as the key field and mutes rule_statement", () => {
    const el = renderAnswerCard(appealsCard);
    // appeal_argument sits in the highlighted block
    const arg = el.querySelector(".ac-appeals-argument-value");
    expect(arg?.textContent).toContain("retroactively");
    // rule_statement renders muted, separate from the argument block
    const stmt = el.querySelector(".ac-appeals-statement");
    expect(stmt?.textContent).toContain("30 days");
    // payor variants become pills (object + string forms both handled)
    const pills = Array.from(el.querySelectorAll(".ac-appeals-variant-pill")).map((p) => p.textContent);
    expect(pills.some((p) => p?.includes("Sunshine"))).toBe(true);
    expect(pills.some((p) => p === "Humana")).toBe(true);
  });

  it("renders the admin deep-link footer when admin_url is a safe http(s) URL", () => {
    const withAdmin: AnswerCard = {
      ...appealsCard,
      sections: [{
        ...appealsCard.sections[0],
        data: { ...(appealsCard.sections[0].data as object), admin_url: "https://appeals.test/admin/rules-library" } as unknown as AnswerCard["sections"][number]["data"],
      }],
    };
    const el = renderAnswerCard(withAdmin);
    const link = el.querySelector(".ac-appeals-admin-link") as HTMLAnchorElement | null;
    expect(link).not.toBeNull();
    expect(link!.getAttribute("href")).toBe("https://appeals.test/admin/rules-library");
    expect(link!.getAttribute("rel")).toContain("noopener");
  });

  it("drops a non-http admin_url (javascript: is never rendered as a link)", () => {
    const evil: AnswerCard = {
      ...appealsCard,
      sections: [{
        ...appealsCard.sections[0],
        data: { ...(appealsCard.sections[0].data as object), admin_url: "javascript:alert(1)" } as unknown as AnswerCard["sections"][number]["data"],
      }],
    };
    const el = renderAnswerCard(evil);
    expect(el.querySelector(".ac-appeals-admin-link")).toBeNull();
  });

  it("playbook: found:false renders the soft empty state, not a bare card", () => {
    const el = renderAnswerCard({
      direct_answer: "No playbook.",
      sections: [{
        label: "Appeal playbook", visibility: "primary", format: "appeals_playbook", bullets: [],
        data: { found: false, message: "No playbook on file for Aetna." } as unknown as AnswerCard["sections"][number]["data"],
      }],
    });
    const empty = el.querySelector(".ac-appeals-playbook-empty");
    expect(empty?.textContent).toContain("No playbook on file for Aetna.");
    // no docs/levels scaffolding when empty
    expect(el.querySelector(".ac-appeals-docs")).toBeNull();
  });

  it("playbook: renders deadline, method, docs checklist, and levels ladder", () => {
    const el = renderAnswerCard({
      direct_answer: "Playbook for Humana.",
      sections: [{
        label: "Appeal playbook", visibility: "primary", format: "appeals_playbook", bullets: [],
        data: {
          found: true, payor: "Humana", carc_group: "auth",
          deadline_appeal_days: 60, submission_method: "Provider portal",
          portal_url: "https://example.test/appeals",
          docs_required: [{ doc: "Auth letter", required: true }, { doc: "Chart notes", required: false }],
          appeal_levels: [{ level: 1, name: "Reconsideration", deadline_days: 30 }, { level: 2, name: "Peer review" }],
        } as unknown as AnswerCard["sections"][number]["data"],
      }],
    });
    expect(el.querySelector(".ac-appeals-deadline")?.textContent).toContain("60");
    expect(el.querySelector(".ac-appeals-method")?.textContent).toContain("portal");
    const docs = Array.from(el.querySelectorAll(".ac-appeals-doc")).map((d) => d.textContent);
    expect(docs.some((d) => d?.includes("Auth letter"))).toBe(true);
    expect(docs.some((d) => d?.includes("optional"))).toBe(true);
    const levels = Array.from(el.querySelectorAll(".ac-appeals-level-name")).map((l) => l.textContent);
    expect(levels).toContain("Reconsideration");
    expect(levels).toContain("Peer review");
  });
});

describe("Task #10 — output_intent (Diagnostics telemetry, NOT on the card face)", () => {
  it("formatOutputIntentLabel returns the canonical value for known intents", () => {
    for (const intent of ["read", "report", "email", "sms", "emr", "appeal", "payor_report"]) {
      expect(formatOutputIntentLabel(intent)).toBe(intent);
    }
  });

  it("is case-insensitive and trims", () => {
    expect(formatOutputIntentLabel("  REPORT ")).toBe("report");
  });

  it("returns null for absent/unknown intent — never invents a value", () => {
    expect(formatOutputIntentLabel(undefined)).toBeNull();
    expect(formatOutputIntentLabel("")).toBeNull();
    expect(formatOutputIntentLabel("banana")).toBeNull();
  });

  // The chip must NOT appear on the card face anymore (Chat Master 2026-08-05). output_intent is
  // surfaced only as a Diagnostics telemetry row, which is injected in app.ts (not the renderer).
  it("renders NOTHING on the card face even when output_intent is set", () => {
    const el = renderAnswerCard({ ...card, output_intent: "report" });
    expect(el.querySelector(".answer-card-format-chip")).toBeNull();
    expect(el.querySelector(".answer-card-format-row")).toBeNull();
  });
});
