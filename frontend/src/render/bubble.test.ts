// @vitest-environment jsdom
import { describe, it, expect } from "vitest";
import { renderAnswerCard, formatOutputIntentLabel, applyInlineCorrections } from "./bubble";
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
  it("renders the direct answer (in the inline final) and a v2 (no-mode) card class", () => {
    const el = renderAnswerCard(card);
    expect(el.className).toContain("answer-card--v2");
    // card has sections → the final is inline; direct_answer leads .ac-answer-final (no display_summary here).
    expect(el.querySelector(".ac-answer-final")?.textContent).toContain("parity");
  });

  it("sections render inline in the default panel (.ac-answer-final), NOT a separate Answer tab (unified view, Ananth 2026-08-07)", () => {
    const el = renderAnswerCard(card);
    // No separate Answer PANEL — the integrator's final flows into the default (summary) panel.
    expect(el.querySelector(".ac-tab-panel--answer")).toBeNull();
    // Sections live in .ac-answer-final inside the default (summary) panel.
    const answer = el.querySelector(".ac-tab-panel--summary .ac-answer-final")!;
    expect(answer).not.toBeNull();
    expect(answer.textContent).toContain("Covered codes");
    expect(answer.textContent).toContain("Documentation");
    expect(answer.textContent).toContain("Exceptions");
    expect(el.querySelector(".answer-card-show-details")).toBeNull();
  });

  it("primary tab is 'Answer' (unified default panel); Summary/Draft/Follow-up gone; Sources present", () => {
    const el = renderAnswerCard(card);
    const tabs = Array.from(el.querySelectorAll(".ac-tab")).map((t) => t.textContent ?? "");
    // The default panel is now labeled "Answer" (holds the draft→final flow); no separate answer panel.
    expect(tabs.some((t) => t.includes("Answer"))).toBe(true);
    expect(el.querySelector(".ac-tab-panel--answer")).toBeNull();
    expect(tabs.some((t) => t === "Summary" || t === "Draft")).toBe(false);
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

describe("Unified draft→answer view (Ananth 2026-08-07 — answer inline in the default panel, no Answer tab)", () => {
  it("renders the integrator final inline in .ac-answer-final with the CANONICAL badge + display_summary", () => {
    const el = renderAnswerCard({ ...card, mode: "CANONICAL", display_summary: "The authoritative final answer, in full." });
    // No separate Answer PANEL (the default tab labeled "Answer" holds it inline).
    expect(el.querySelector(".ac-tab-panel--answer")).toBeNull();
    // The final is inline in the default panel: badge + display_summary body.
    const final = el.querySelector(".ac-tab-panel--summary .ac-answer-final")!;
    expect(final).not.toBeNull();
    expect(final.querySelector(".ac-answer-mode-label")?.textContent).toBe("CANONICAL");
    expect(final.querySelector(".ac-answer-envelope-body")?.textContent).toContain("authoritative final answer");
  });

  it("renders NO mode badge for FACTUAL/BLENDED (default path — nothing to signal; Chat Master 2026-08-07)", () => {
    const factual = renderAnswerCard({ ...card, mode: "FACTUAL", display_summary: "A factual answer." });
    expect(factual.querySelector(".ac-answer-final .ac-answer-mode-label")).toBeNull();
    const blended = renderAnswerCard({ ...card, mode: "BLENDED", display_summary: "A blended answer." });
    expect(blended.querySelector(".ac-answer-final .ac-answer-mode-label")).toBeNull();
    // but the final body still renders inline
    expect(factual.querySelector(".ac-answer-final .ac-answer-envelope-body")?.textContent).toContain("factual answer");
  });

  it("display_summary now renders IN the default panel (unified view supersedes the react_draft-only Summary)", () => {
    const el = renderAnswerCard({ ...card, mode: "FACTUAL", display_summary: "ENVELOPE_TEXT_XYZ" });
    // The answer flows into the same default panel now (no separate Answer tab).
    expect(el.querySelector(".ac-tab-panel--summary .ac-answer-final")?.textContent).toContain("ENVELOPE_TEXT_XYZ");
    expect(el.querySelector(".ac-tab-panel--answer")).toBeNull();
  });

  it("renders sections inline even when display_summary is EMPTY (appeals turn, cid 4d9456e2)", () => {
    const el = renderAnswerCard({
      direct_answer: "Here's the appeal path.",
      mode: "FACTUAL",
      sections: [{ label: "CARC 22 rules", visibility: "primary", format: "bullets", bullets: ["Coordinate benefits first"] }],
    });
    expect(el.querySelector(".ac-tab-panel--answer")).toBeNull();
    const final = el.querySelector(".ac-answer-final")!;
    expect(final.textContent).toContain("CARC 22 rules");
    expect(final.textContent).toContain("Coordinate benefits first");
  });

  it("renders tldr_summary as the final lead when present; hides it when empty", () => {
    const withTldr = renderAnswerCard({ ...card, mode: "BLENDED", tldr_summary: "Two-sentence verdict here." });
    expect(withTldr.querySelector(".ac-answer-final .ac-answer-tldr")?.textContent).toContain("verdict");
    const noTldr = renderAnswerCard({ ...card, mode: "BLENDED", display_summary: "prose" });
    expect(noTldr.querySelector(".ac-answer-tldr")).toBeNull();
  });

  it("demotes react_draft to a collapsed 'First pass' when the final exists; shows it as the headline when draft-only", () => {
    // Final present (sections) → the final leads inline; react_draft demotes to the collapsed First pass.
    const withFinal = renderAnswerCard({ ...card, direct_answer: "INTEGRATOR_LINE", react_draft: "REACT_SYNTHESIS_LINE" });
    expect(withFinal.querySelector(".ac-first-pass-body")?.textContent).toContain("REACT_SYNTHESIS_LINE");
    expect(withFinal.querySelector(".ac-answer-final")?.textContent).toContain("INTEGRATOR_LINE");
    expect(withFinal.querySelector(".answer-card-direct")).toBeNull(); // no prominent draft headline once the final exists
    // Draft-only (no final) → react_draft IS the headline.
    const draftOnly = renderAnswerCard({ direct_answer: "INTEGRATOR_LINE", react_draft: "REACT_SYNTHESIS_LINE", sections: [] });
    expect(draftOnly.querySelector(".answer-card-direct")?.textContent).toContain("REACT_SYNTHESIS_LINE");
    expect(draftOnly.querySelector(".answer-card-direct")?.textContent).not.toContain("INTEGRATOR_LINE");
    expect(draftOnly.querySelector(".ac-first-pass")).toBeNull(); // no First pass when there's no final to demote under
  });

  it("renders no .ac-answer-final and no Answer tab when there is neither display_summary NOR sections", () => {
    const el = renderAnswerCard({ direct_answer: "Just a sentence.", sections: [] });
    const tabs = Array.from(el.querySelectorAll(".ac-tab")).map((t) => t.textContent ?? "");
    expect(tabs.some((t) => t.includes("Answer"))).toBe(false);
    // No inline final block when there's nothing from the integrator.
    expect(el.querySelector(".ac-answer-final")).toBeNull();
  });
});

describe("applyInlineCorrections — inline redline in the answer (Ananth 2026-08-07)", () => {
  it("redlines the corrected text: strikes original, inserts corrected, in place", () => {
    const el = document.createElement("div");
    el.innerHTML = "<p>The initial filing deadline is 180 days from the date of service.</p>";
    applyInlineCorrections(el, [{ original: "365 days", corrected: "180 days" }]);
    expect(el.querySelector(".ac-redline-del")?.textContent).toBe("365 days");
    expect(el.querySelector(".ac-redline-ins")?.textContent).toBe("180 days");
    expect(el.textContent).toContain("180 days");
    expect(el.textContent).toContain("365 days");
  });

  it("skips gracefully when the corrected text isn't found verbatim (no misplacement)", () => {
    const el = document.createElement("div");
    el.innerHTML = "<p>Something entirely unrelated to the correction.</p>";
    applyInlineCorrections(el, [{ original: "365 days", corrected: "180 days" }]);
    expect(el.querySelector(".ac-redline")).toBeNull();
    expect(el.textContent).toContain("unrelated");
  });

  it("does not double-apply inside an existing redline", () => {
    const el = document.createElement("div");
    el.innerHTML = "<p>Filed within 180 days.</p>";
    const corr = [{ original: "365 days", corrected: "180 days" }];
    applyInlineCorrections(el, corr);
    applyInlineCorrections(el, corr);
    expect(el.querySelectorAll(".ac-redline").length).toBe(1);
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
