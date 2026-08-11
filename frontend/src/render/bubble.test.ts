// @vitest-environment jsdom
import { describe, it, expect } from "vitest";
import { renderAnswerCard, formatOutputIntentLabel, applyInlineCorrections, applyCitationFootnotes, renderSourcesList, retainStreamedDraftAsFirstPass, stripCitationMarkers, renderFormatBlock, renderModeBadge, renderFirstPass, renderEnvelope, envelopeToAnswerCard } from "./bubble";
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

  it("renders the reasoning progression (rd-1 → rd-last) from card.reasoning_trace, filtering empty rounds", () => {
    const el = renderAnswerCard({
      ...card,
      react_draft: "final draft",
      // FLAT shape — running_answer is a direct sibling of round (matches the backend's
      // _build_reasoning_ledger output), NOT nested under "enrichment".
      reasoning_trace: [
        { round: 1, running_answer: "Round 1 answer so far." },
        { round: 2, running_answer: "" },            // empty → filtered out
        { round: 3, running_answer: "Round 3 refined answer." },
      ],
    });
    const steps = Array.from(el.querySelectorAll(".ac-rd-step"));
    expect(steps.length).toBe(2); // round 2 (empty running_answer) filtered
    const labels = Array.from(el.querySelectorAll(".ac-rd-label")).map((l) => l.textContent);
    expect(labels).toEqual(["rd-1", "rd-3"]);
    expect(el.querySelector(".ac-first-pass-body")?.textContent).toContain("Round 1 answer so far");
    expect(el.querySelector(".ac-first-pass-body")?.textContent).toContain("Round 3 refined answer");
    // summary reflects the round count
    expect(el.querySelector(".ac-first-pass-summary")?.textContent).toContain("2 rounds");
  });

  it("shows a round's `learned` (as a muted thought) when it has no running_answer", () => {
    const el = renderAnswerCard({
      ...card,
      react_draft: "final draft",
      reasoning_trace: [
        { round: 1, tool: "search_corpus", learned: "Need to check the filing window." } as unknown as NonNullable<AnswerCard["reasoning_trace"]>[number],
        { round: 2, running_answer: "Filed within 180 days." },
      ],
    });
    const steps = Array.from(el.querySelectorAll(".ac-rd-step"));
    expect(steps.length).toBe(2);
    // round 1 = thought (learned, muted); round 2 = answer-so-far
    expect(el.querySelector(".ac-rd-thought")?.textContent).toContain("Need to check the filing window");
    expect(el.querySelector(".ac-rd-answer:not(.ac-rd-thought)")?.textContent).toContain("Filed within 180 days");
  });

  it("falls back to the single react_draft First pass when no reasoning_trace", () => {
    const el = renderAnswerCard({ ...card, react_draft: "just the draft" });
    expect(el.querySelectorAll(".ac-rd-step").length).toBe(0);
    expect(el.querySelector(".ac-first-pass-body")?.textContent).toContain("just the draft");
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

describe("applyCitationFootnotes — inline [N] markers → superscript refs (Task #34)", () => {
  const sources = [
    { document_name: "AHCA Handbook", locator: "§59G-4" },
    { document_name: "Provider Bulletin", locator: "p. 12" },
  ];

  it("replaces [N] with a superscript ref carrying the marker number", () => {
    const el = document.createElement("div");
    el.innerHTML = "<p>Telehealth is reimbursed at parity [1] under the state plan [2].</p>";
    applyCitationFootnotes(el, sources);
    const refs = el.querySelectorAll(".ac-cite-ref");
    expect(refs.length).toBe(2);
    expect(refs[0].textContent).toBe("1");
    expect(refs[0].getAttribute("data-cite-ref")).toBe("1");
    expect(refs[1].textContent).toBe("2");
    // The literal bracket text is gone (rendered via CSS ::before/::after now).
    expect(el.textContent).not.toContain("[1]");
    expect(el.textContent).not.toContain("[2]");
  });

  it("drops a marker whose N has no matching source (no dead ref)", () => {
    const el = document.createElement("div");
    el.innerHTML = "<p>A claim with an out-of-range marker [5] and a valid one [1].</p>";
    applyCitationFootnotes(el, sources);
    const refs = el.querySelectorAll(".ac-cite-ref");
    expect(refs.length).toBe(1);
    expect(refs[0].textContent).toBe("1");
    expect(el.textContent).not.toContain("[5]");
    expect(el.textContent).not.toContain("5");
  });

  it("is a no-op when there are no sources", () => {
    const el = document.createElement("div");
    el.innerHTML = "<p>Plain text with [1] left intact.</p>";
    applyCitationFootnotes(el, []);
    expect(el.querySelector(".ac-cite-ref")).toBeNull();
    expect(el.textContent).toContain("[1]");
  });

  it("does not re-process an existing footnote ref (idempotent)", () => {
    const el = document.createElement("div");
    el.innerHTML = "<p>Parity [1] applies.</p>";
    applyCitationFootnotes(el, sources);
    applyCitationFootnotes(el, sources);
    expect(el.querySelectorAll(".ac-cite-ref").length).toBe(1);
  });

  it("leaves bracketed numbers inside code/pre untouched", () => {
    const el = document.createElement("div");
    el.innerHTML = "<p>See <code>arr[1]</code> here [1].</p>";
    applyCitationFootnotes(el, sources);
    expect(el.querySelector("code")?.textContent).toBe("arr[1]");
    expect(el.querySelectorAll(".ac-cite-ref").length).toBe(1);
  });
});

describe("renderSourcesList — numbered bottom list (Task #34)", () => {
  it("builds an ordered list positionally aligned to the [N] markers", () => {
    const list = renderSourcesList([
      { document_name: "AHCA Handbook", locator: "§59G-4", snippet: "reimbursed at parity" },
      { document_name: "Provider Bulletin", locator: "p. 12" },
    ])!;
    const items = list.querySelectorAll(".ac-source-item");
    expect(items.length).toBe(2);
    expect(items[0].getAttribute("data-cite-src")).toBe("1");
    expect(items[0].querySelector(".ac-source-title")?.textContent).toBe("AHCA Handbook");
    expect(items[0].querySelector(".ac-source-locator")?.textContent).toBe("§59G-4");
    expect(items[0].querySelector(".ac-source-snippet")?.textContent).toBe("reimbursed at parity");
    expect(items[1].getAttribute("data-cite-src")).toBe("2");
    expect(items[1].querySelector(".ac-source-snippet")).toBeNull();
  });

  it("returns null for an empty sources array", () => {
    expect(renderSourcesList([])).toBeNull();
  });

  it("makes an item clickable → calls onSourceClick(document_id, page_number, snippet)", () => {
    const calls: Array<[string, number | null | undefined, string | null | undefined]> = [];
    const list = renderSourcesList(
      [{ document_name: "Handbook", locator: "§59G", snippet: "parity", document_id: "doc-42", page_number: 7 }],
      (id, page, cite) => calls.push([id, page, cite]),
    )!;
    const item = list.querySelector(".ac-source-item") as HTMLElement;
    expect(item.classList.contains("ac-source-item--clickable")).toBe(true);
    expect(item.getAttribute("role")).toBe("button");
    item.click();
    expect(calls).toEqual([["doc-42", 7, "parity"]]);
  });

  it("is NOT clickable without a document_id (nothing to open)", () => {
    const list = renderSourcesList(
      [{ document_name: "Handbook", locator: "§59G" }],
      () => { throw new Error("should not be called"); },
    )!;
    const item = list.querySelector(".ac-source-item") as HTMLElement;
    expect(item.classList.contains("ac-source-item--clickable")).toBe(false);
    item.click(); // no handler wired → no throw
  });

  it("is NOT clickable without an onSourceClick handler", () => {
    const list = renderSourcesList([{ document_name: "Handbook", document_id: "doc-1" }])!;
    const item = list.querySelector(".ac-source-item") as HTMLElement;
    expect(item.classList.contains("ac-source-item--clickable")).toBe(false);
  });
});

describe("renderAnswerCard — sources live in the Sources tab only (Chat Master 2026-08-08 revert)", () => {
  it("does NOT render sources inline even when card.sources is present", () => {
    const el = renderAnswerCard({
      ...card,
      display_summary: "Yes, reimbursed at parity [1] under the state plan [2].",
      sources: [
        { document_name: "AHCA Handbook", locator: "§59G-4" },
        { document_name: "Provider Bulletin", locator: "p. 12" },
      ],
    });
    // No inline footnote superscripts, no inline bottom list.
    expect(el.querySelectorAll(".ac-cite-ref").length).toBe(0);
    expect(el.querySelector(".ac-sources-list")).toBeNull();
    expect(el.querySelectorAll(".ac-source-item").length).toBe(0);
    // Raw [N] markers are stripped from the prose (Chat Master: "looks unprofessional").
    expect(el.textContent).not.toContain("[1]");
    expect(el.textContent).not.toContain("[2]");
    expect(el.textContent).toContain("reimbursed at parity");
  });

  it("keeps the Sources tab live whenever citations exist (with OR without card.sources)", () => {
    const withSources = renderAnswerCard({
      ...card,
      display_summary: "Yes [1].",
      sources: [{ document_name: "Handbook", locator: "§59G", document_id: "d1", page_number: 3 }],
    });
    const withoutSources = renderAnswerCard({ ...card, display_summary: "Yes, at parity." });
    for (const el of [withSources, withoutSources]) {
      const srcTab = Array.from(el.querySelectorAll(".ac-tab")).find(
        (t) => t.getAttribute("data-panel") === "citations",
      );
      expect(srcTab).not.toBeUndefined();          // Sources tab present
      expect(srcTab!.getAttribute("data-empty")).toBeNull();   // and live (not suppressed)
    }
  });
});

describe("stripCitationMarkers — removes raw [N] litter from prose (Chat Master 2026-08-08)", () => {
  it("deletes [N] markers and tidies the leftover space", () => {
    const el = document.createElement("div");
    el.innerHTML = "<p>Parity applies [1] under the plan [2].</p>";
    stripCitationMarkers(el);
    expect(el.textContent).toBe("Parity applies under the plan.");
  });

  it("leaves bracketed numbers inside code, pre, and table cells alone", () => {
    const el = document.createElement("div");
    el.innerHTML = "<p>See [1].</p><pre>arr[2]</pre><table><tr><td>row [3]</td></tr></table>";
    stripCitationMarkers(el);
    expect(el.querySelector("p")?.textContent).toBe("See.");
    expect(el.querySelector("pre")?.textContent).toBe("arr[2]");
    expect(el.querySelector("td")?.textContent).toBe("row [3]");
  });

  it("is a no-op on prose with no markers", () => {
    const el = document.createElement("div");
    el.innerHTML = "<p>No markers here.</p>";
    stripCitationMarkers(el);
    expect(el.textContent).toBe("No markers here.");
  });
});

describe("retainStreamedDraftAsFirstPass — draft persists on final-land (Chat Master 2026-08-08)", () => {
  // Simulate the completed handler's post-swap panel: the rendered final is present, but the card
  // dropped react_draft so renderAnswerCard built NO First pass. The streamed draft must be retained.
  function panelWithFinalNoFirstPass(): HTMLElement {
    const panel = document.createElement("div");
    panel.className = "ac-tab-panel--summary";
    const finalWrap = document.createElement("div");
    finalWrap.className = "ac-answer-final";
    finalWrap.innerHTML = "<div class='ac-answer-envelope-body'>The final answer.</div>";
    panel.appendChild(finalWrap);
    return panel;
  }

  it("synthesizes a First pass from the streamed draft when the final carried none", () => {
    const panel = panelWithFinalNoFirstPass();
    const fp = retainStreamedDraftAsFirstPass(panel, "<p>the streamed draft text</p>");
    expect(fp).not.toBeNull();
    const rendered = panel.querySelector(".ac-first-pass");
    expect(rendered).not.toBeNull();
    expect(rendered!.querySelector(".ac-first-pass-summary")?.textContent).toBe("First pass");
    expect(rendered!.querySelector(".ac-first-pass-body")?.textContent).toContain("the streamed draft text");
    // Inserted ABOVE the final (draft collapses underneath the star).
    expect(panel.firstElementChild).toBe(rendered);
  });

  it("is a no-op when the rendered card already built a First pass (react_draft survived)", () => {
    const panel = panelWithFinalNoFirstPass();
    const existing = document.createElement("div");
    existing.className = "ac-first-pass";
    existing.innerHTML = "<div class='ac-first-pass-body'>already here</div>";
    panel.insertBefore(existing, panel.firstChild);
    const fp = retainStreamedDraftAsFirstPass(panel, "<p>would-be draft</p>");
    expect(fp).toBeNull();
    expect(panel.querySelectorAll(".ac-first-pass").length).toBe(1);
    expect(panel.querySelector(".ac-first-pass-body")?.textContent).toBe("already here");
  });

  it("is a no-op when there is no streamed draft to retain", () => {
    const panel = panelWithFinalNoFirstPass();
    expect(retainStreamedDraftAsFirstPass(panel, "   ")).toBeNull();
    expect(panel.querySelector(".ac-first-pass")).toBeNull();
  });

  it("toggles open/closed on click (measured height)", () => {
    const panel = panelWithFinalNoFirstPass();
    const fp = retainStreamedDraftAsFirstPass(panel, "<p>draft</p>")!;
    const btn = fp.querySelector(".ac-first-pass-summary") as HTMLButtonElement;
    btn.click();
    expect(fp.classList.contains("ac-first-pass--open")).toBe(true);
    btn.click();
    expect(fp.classList.contains("ac-first-pass--open")).toBe(false);
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

  const mkPlaybook = (data: Record<string, unknown>) => renderAnswerCard({
    direct_answer: "Playbook.",
    sections: [{
      label: "Appeal playbook", visibility: "primary", format: "appeals_playbook", bullets: [],
      data: data as unknown as AnswerCard["sections"][number]["data"],
    }],
  });

  it("playbook (§3 rows): deadlines row, docs checklist, and levels ladder", () => {
    const el = mkPlaybook({
      found: true, payor: "Humana", carc: "197",
      deadline_appeal_days: 60, deadline_resubmit_days: 180, deadline_resubmit_note: "365 for corrected",
      submission_method: "Provider portal", portal_url: "https://example.test/appeals", fax: "1-866-000-0000",
      docs_required: [{ doc: "Auth letter", required: true }, { doc: "Chart notes", required: false }],
      appeal_levels: [{ level: 1, name: "Reconsideration", deadline_days: 30 }, { level: 2, name: "Peer review" }],
    });
    // Title composes CARC × payor.
    expect(el.querySelector(".ac-appeals-playbook-title")?.textContent).toContain("CARC 197");
    expect(el.querySelector(".ac-appeals-playbook-title")?.textContent).toContain("Humana");
    // Deadlines row carries both appeal + resubmit (with note).
    const rowsText = (el.querySelector(".ac-appeals-playbook")?.textContent) || "";
    expect(rowsText).toContain("appeal 60d");
    expect(rowsText).toContain("resubmit 180d");
    expect(rowsText).toContain("365 for corrected");
    // Submit row: concrete channels only — portal LINK + fax. submission_method ("Provider portal")
    // is NOT rendered as a separate bit when a real portal_url exists (Appeals guard 2026-08-10).
    expect(el.querySelector(".ac-appeals-portal")?.getAttribute("href")).toBe("https://example.test/appeals");
    expect(el.querySelector(".ac-appeals-portal")?.textContent).toBe("provider portal");
    expect(rowsText).toContain("1-866-000-0000");
    // Docs checklist preserved (required/optional).
    const docs = Array.from(el.querySelectorAll(".ac-appeals-doc")).map((d) => d.textContent);
    expect(docs.some((d) => d?.includes("Auth letter"))).toBe(true);
    expect(docs.some((d) => d?.includes("optional"))).toBe(true);
    // Levels ladder.
    const levels = Array.from(el.querySelectorAll(".ac-appeals-level-name")).map((l) => l.textContent);
    expect(levels).toEqual(["Reconsideration", "Peer review"]);
  });

  it("playbook: downloadable form → ⬇ link when url present, plain text when empty (Appeals 2026-08-10)", () => {
    const el = mkPlaybook({
      found: true, carc: "151",
      docs_required: [
        { doc: "Claim Adjustment Request Form", url: "https://www.sunshinehealth.com/content/dam/centene/Sunshine/pdfs/Provider-Dispute-Form.pdf", required: true },
        { doc: "Medical records", url: "", required: true },
      ],
    });
    const link = el.querySelector(".ac-appeals-doc-link") as HTMLAnchorElement;
    expect(link).not.toBeNull();
    expect(link.getAttribute("href")).toContain("Provider-Dispute-Form.pdf");
    expect(link.getAttribute("rel")).toBe("noopener noreferrer");
    expect(link.querySelector(".ac-appeals-doc-dl")?.textContent).toContain("⬇");
    // empty url → NO link, plain text row survives
    const plain = Array.from(el.querySelectorAll(".ac-appeals-doc")).find((d) => d.textContent?.includes("Medical records"));
    expect(plain?.querySelector("a")).toBeNull();
  });

  it("playbook: per-doc notes render as a muted sub-line (Appeals 2026-08-10)", () => {
    const el = mkPlaybook({
      found: true, carc: "151",
      docs_required: [{ doc: "Claim Adjustment Request Form", required: true, notes: "Must include claim number and auth number if applicable" }],
    });
    expect(el.querySelector(".ac-appeals-doc-note")?.textContent).toContain("claim number and auth number");
  });

  it("playbook: ladder shows per-level submission inline + notes sub-line (Appeals 2026-08-10)", () => {
    const el = mkPlaybook({
      found: true, carc: "151",
      appeal_levels: [
        { level: 1, name: "Internal Appeal", deadline_days: 90, submission: "Mail or Fax", notes: "Provider appeal — file within deadline from EOP/denial date." },
      ],
    });
    const lvl = el.querySelector(".ac-appeals-level")!;
    expect(lvl.querySelector(".ac-appeals-level-via")?.textContent).toContain("Mail or Fax");
    expect(lvl.querySelector(".ac-appeals-level-deadline")?.textContent).toBe("90d");
    expect(lvl.querySelector(".ac-appeals-level-note")?.textContent).toContain("from EOP/denial date");
  });

  it("playbook: what_it_usually_means renders as a muted line under the description (Appeals 2026-08-10)", () => {
    const el = mkPlaybook({
      found: true, carc: "151",
      description: "The payer is paying for fewer units than you billed.",
      what_it_usually_means: "Usually a documentation gap rather than a coverage denial.",
    });
    const wtum = el.querySelector(".ac-pb-usually-means");
    expect(wtum?.textContent).toContain("documentation gap");
    // absent → omitted
    const el2 = mkPlaybook({ found: true, carc: "151", description: "x" });
    expect(el2.querySelector(".ac-pb-usually-means")).toBeNull();
  });

  it("playbook: Submit row is field-driven — renders fax+mail when submission_method+portal empty (Appeals guard 2026-08-10)", () => {
    // CARC 151 × Sunshine: submission_method pruned to "", portal_url empty, but real fax + mail.
    const el = mkPlaybook({
      found: true, carc: "151", payor: "Sunshine Health",
      submission_method: "", portal_url: "", fax: "1-833-504-0580", mail_address: "PO Box 3070, Farmington MO 63640",
    });
    const submitText = Array.from(el.querySelectorAll(".ac-pb-row--submit")).map((r) => r.textContent).join(" ");
    expect(submitText).toContain("1-833-504-0580");   // fax survives
    expect(submitText).toContain("PO Box 3070");        // mail survives
    expect(submitText).not.toContain("portal");         // no phantom portal channel
    expect(el.querySelector(".ac-appeals-portal")).toBeNull();
  });

  it("playbook: submission_method shows only as fallback when no concrete channel exists", () => {
    const el = mkPlaybook({ found: true, carc: "151", submission_method: "Contact provider services", portal_url: "", fax: "", mail_address: "" });
    expect(el.querySelector(".ac-pb-row--submit")?.textContent).toContain("Contact provider services");
  });

  it("playbook: strategy line + numbered canonical questions with hints", () => {
    const el = mkPlaybook({
      found: true, payor: "Sunshine Health", carc: "29",
      strategy: "resubmission with proof → formal appeal if denied again",
      questions: [
        { n: 1, text: "Can you prove the original submission date?", hint: "EDI ack, clearinghouse log" },
        { text: "Is this a secondary claim?", hint: "COB resets the clock" },
      ],
    });
    const rows = Array.from(el.querySelectorAll(".ac-pb-row")).map((r) => r.textContent || "");
    expect(rows.some((r) => r.includes("Strategy:") && r.includes("formal appeal"))).toBe(true);
    expect(rows.some((r) => /1\./.test(r) && r.includes("Can you prove") && r.includes("(EDI ack"))).toBe(true);
    // Missing n falls back to positional index (2nd question → "2.").
    expect(rows.some((r) => /2\./.test(r) && r.includes("secondary claim"))).toBe(true);
  });

  it("playbook: confidence_level drives the badge; absent → omitted", () => {
    expect(mkPlaybook({ found: true, carc: "29", confidence_level: 1 }).querySelector(".ac-pb-badge--reviewed")?.textContent).toBe("REVIEWED");
    expect(mkPlaybook({ found: true, carc: "29", confidence_level: 2 }).querySelector(".ac-pb-badge--published")?.textContent).toBe("PUBLISHED");
    expect(mkPlaybook({ found: true, carc: "29", confidence_level: 3 }).querySelector(".ac-pb-badge--validated")?.textContent).toBe("VALIDATED");
    // Absent confidence_level → NO badge (never defaults to GENERATED).
    expect(mkPlaybook({ found: true, carc: "29" }).querySelector(".ac-pb-badge")).toBeNull();
  });

  it("playbook: title uses carc_codes[] when singular carc is absent (tool emits an array)", () => {
    expect(mkPlaybook({ found: true, payor: "Sunshine Health", carc_codes: [29, 218] })
      .querySelector(".ac-appeals-playbook-title")?.textContent).toContain("CARC 29, 218");
    // singular carc still wins when present
    expect(mkPlaybook({ found: true, payor: "Sunshine Health", carc: "29", carc_codes: [29, 218] })
      .querySelector(".ac-appeals-playbook-title")?.textContent).toContain("CARC 29 ×");
  });

  it("playbook: level-0 content is labeled 'Draft — not yet reviewed', never unlabeled", () => {
    const l0 = mkPlaybook({ found: true, carc: "29", confidence_level: 0 });
    expect(l0.querySelector(".ac-pb-badge--generated")?.textContent).toBe("GENERATED");
    expect(l0.querySelector(".ac-pb-draft-label")?.textContent).toContain("Draft");
    // Higher levels carry NO draft label.
    expect(mkPlaybook({ found: true, carc: "29", confidence_level: 2 }).querySelector(".ac-pb-draft-label")).toBeNull();
    expect(mkPlaybook({ found: true, carc: "29" }).querySelector(".ac-pb-draft-label")).toBeNull();
  });

  it("playbook: renders inline markdown in LLM-authored fields, escaping HTML", () => {
    const el = mkPlaybook({
      found: true, carc: "29",
      strategy: "resubmit with **proof of timely filing**",
      docs_required: [{ doc: "the **Claim Adjustment Request Form**", required: true }],
      questions: [{ n: 1, text: "Do you have the `EDI 277` ack?", hint: "clearinghouse log" }],
      submission_method: "provider portal <script>alert(1)</script>",
    });
    // **bold** → <strong>, not raw asterisks.
    expect(el.querySelector(".ac-appeals-doc-text")?.querySelector("strong")?.textContent).toBe("Claim Adjustment Request Form");
    expect(el.querySelector(".ac-appeals-doc-text")?.textContent).not.toContain("**");
    const rowsHtml = el.querySelector(".ac-appeals-playbook")?.innerHTML || "";
    expect(rowsHtml).toContain("<strong>proof of timely filing</strong>");   // strategy
    expect(rowsHtml).toContain("<code>EDI 277</code>");                       // question
    // HTML is escaped — no live script tag injected from LLM content.
    expect(el.querySelector("script")).toBeNull();
    expect(rowsHtml).toContain("&lt;script&gt;");
  });

  it("playbook: renders as labeled sections with a description lead (Ananth 2026-08-08)", () => {
    const el = mkPlaybook({
      found: true, carc: "22", payor: "Sunshine Health",
      description: "Coordination of Benefits — this care may be covered by another payer.",
      deadline_appeal_days: 90,
      guidance: [{ text: "You may still appeal within the window." }],
      docs_required: [{ doc: "Denial EOB", required: true }],
      submission_method: "Provider portal", fax: "1-833-000-0000",
    });
    // Description lead under the title
    expect(el.querySelector(".ac-pb-description")?.textContent).toContain("Coordination of Benefits");
    // Labeled sections
    const headings = Array.from(el.querySelectorAll(".ac-pb-section-heading")).map((h) => h.textContent);
    expect(headings).toEqual(["Deadlines & Appeal Strategy", "Documentation", "Submission"]);
    // Guidance lives in the Deadlines & Appeal Strategy section (not floating)
    const stratSection = el.querySelector(".ac-pb-section");
    expect(stratSection?.querySelector(".ac-pb-row--guidance")).not.toBeNull();
    expect(stratSection?.querySelector('.ac-pb-row-icon')?.textContent).toBe("⏱");   // deadlines first
    // Docs in the Documentation section, submit in Submission
    const docSection = Array.from(el.querySelectorAll(".ac-pb-section")).find((s) => s.querySelector(".ac-pb-section-heading")?.textContent === "Documentation");
    expect(docSection?.querySelector(".ac-appeals-doc")).not.toBeNull();
  });

  it("playbook: omits a section entirely when it has no content (no docs → no Documentation heading)", () => {
    const el = mkPlaybook({ found: true, carc: "22", deadline_appeal_days: 90, submission_method: "portal" });
    const headings = Array.from(el.querySelectorAll(".ac-pb-section-heading")).map((h) => h.textContent);
    expect(headings).not.toContain("Documentation");   // no docs_required → section omitted
    expect(headings).toContain("Deadlines & Appeal Strategy");
    expect(headings).toContain("Submission");
  });

  it("playbook: guidance[] renders as statements and is preferred over questions[]", () => {
    const el = mkPlaybook({
      found: true, carc: "29",
      questions: [{ n: 1, text: "What is the filing limit?" }],
      guidance: [
        { text: "Sunshine's filing limit is **180 days** from DOS", detail: "90 days for appeals" },
        { text: "You can still succeed if the claim was submitted on time" },
      ],
    });
    const gRows = Array.from(el.querySelectorAll(".ac-pb-row--guidance"));
    expect(gRows.length).toBe(2);
    expect(el.querySelector(".ac-pb-guide-text")?.querySelector("strong")?.textContent).toBe("180 days");
    expect(el.querySelector(".ac-pb-guide-detail")?.textContent).toContain("90 days for appeals");
    // questions[] is NOT rendered when guidance[] is present (no interrogative rows)
    expect(el.textContent).not.toContain("What is the filing limit?");
  });

  it("playbook: falls back to questions[] when guidance[] is absent", () => {
    const el = mkPlaybook({ found: true, carc: "29", questions: [{ n: 1, text: "Prove the submission date?" }] });
    expect(el.querySelector(".ac-pb-row--guidance")).toBeNull();
    expect(el.textContent).toContain("Prove the submission date?");
  });

  it("playbook: docs sort required-first regardless of emit order", () => {
    const el = mkPlaybook({
      found: true, carc: "29",
      docs_required: [{ doc: "Medical records", required: false }, { doc: "Denial EOB", required: true }, { doc: "Original claim", required: true }],
    });
    const docTexts = Array.from(el.querySelectorAll(".ac-appeals-doc-text")).map((d) => d.textContent);
    // required items come before the optional one
    expect(docTexts[docTexts.length - 1]).toContain("optional");
    expect(docTexts.slice(0, -1).every((t) => !t?.includes("optional"))).toBe(true);
  });

  it("playbook: appeal ladder caps at 3 with a '+N more' toggle", () => {
    const el = mkPlaybook({
      found: true, carc: "29",
      appeal_levels: [1, 2, 3, 4, 5, 6].map((n) => ({ level: n, name: `Level ${n}`, deadline_days: 90 })),
    });
    // 3 visible in the primary list (triage-actionable levels)
    const primary = el.querySelector(".ac-appeals-levels-list:not(.ac-appeals-levels-extra)");
    expect(primary?.querySelectorAll(".ac-appeals-level").length).toBe(3);
    const more = el.querySelector(".ac-appeals-levels-more") as HTMLButtonElement | null;
    expect(more?.textContent).toBe("+3 more");
    // extra levels exist but hidden until toggled
    const extra = el.querySelector(".ac-appeals-levels-extra") as HTMLElement | null;
    expect(extra?.querySelectorAll(".ac-appeals-level").length).toBe(3);
    expect(extra?.style.display).toBe("none");
    more!.click();
    expect(extra!.style.display).toBe("");
    expect(more!.textContent).toBe("Show fewer");
  });

  it("playbook: mail_address renders in the Submit row", () => {
    const el = mkPlaybook({ found: true, carc: "29", submission_method: "Provider portal", fax: "1-833-000-0000", mail_address: "PO Box 3070, Farmington MO 63640" });
    const rowsText = el.querySelector(".ac-appeals-playbook")?.textContent || "";
    expect(rowsText).toContain("mail PO Box 3070, Farmington MO 63640");
    expect(rowsText).toContain("fax 1-833-000-0000");
  });

  it("playbook: admin chip allows the appeals-service host, rejects an arbitrary https host", () => {
    // Cross-origin appeals host → allowed.
    const ok = mkPlaybook({ found: true, carc: "29", admin_url: "https://mobius-appeals-prototype-xyz.a.run.app/admin/rules-library?carc=29&tab=playbook" });
    expect((ok.querySelector(".ac-appeals-admin-link") as HTMLAnchorElement | null)?.getAttribute("href")).toContain("mobius-appeals");
    // Arbitrary https host → chip suppressed (not rendered to an off-allowlist site).
    const evil = mkPlaybook({ found: true, carc: "29", admin_url: "https://evil.example.com/steal?carc=29" });
    expect(evil.querySelector(".ac-appeals-admin-link")).toBeNull();
  });

  it("playbook: admin edit chip builds the scheme-guarded deep link to the Playbook tab", () => {
    const el = mkPlaybook({ found: true, carc: "29", payor: "Sunshine Health", admin_edit: { carc: "29", payor: "Sunshine Health" } });
    const link = el.querySelector(".ac-appeals-admin-link") as HTMLAnchorElement | null;
    expect(link).not.toBeNull();
    expect(link!.getAttribute("href")).toContain("/admin/rules-library?");
    expect(link!.getAttribute("href")).toContain("carc=29");
    expect(link!.getAttribute("href")).toContain("tab=playbook");
    expect(link!.getAttribute("href")).toContain("payor=Sunshine+Health");
  });

  it("playbook: no admin chip when neither admin_url nor admin_edit is present", () => {
    const el = mkPlaybook({ found: true, carc: "29" });
    expect(el.querySelector(".ac-appeals-admin-link")).toBeNull();
  });

  it("playbook: degrades to deadlines/docs-only when questions + review_status absent (W1/W2 not shipped)", () => {
    const el = mkPlaybook({ found: true, carc: "29", payor: "Sunshine Health", deadline_appeal_days: 90, docs_required: [{ doc: "Denial EOB", required: true }] });
    expect(el.querySelector(".ac-pb-badge")).toBeNull();                 // no badge
    expect(el.querySelectorAll(".ac-pb-row").length).toBeGreaterThan(0);  // still renders
    expect((el.querySelector(".ac-appeals-playbook")?.textContent || "")).toContain("appeal 90d");
    // no questions rows (nothing numbered) — card is coherent without them
    expect(el.querySelector(".ac-appeals-playbook-title")?.textContent).toContain("Playbook");
  });
});

describe("Enricher fast-path shapes render (LLM Agent's exact deterministic_format() output, task #35)", () => {
  // These are the LITERAL section payloads deterministic_format.py emits on the bypass path.
  // Rendering them through the real renderer proves the FE handles exactly what the backend sends,
  // closing the loop without a flaky live cid (natural bypass+shape turns are hard to force).
  const renderSec = (sec: unknown) => renderAnswerCard({
    direct_answer: "Answer.",
    sections: [{ visibility: "primary", ...(sec as object) } as AnswerCard["sections"][number]],
  });

  it("bullets → .answer-card-bullet rows", () => {
    const el = renderSec({ format: "bullets", label: "Key Points", bullets: ["Completed claim form", "Proof of timely filing", "Cover letter explaining the delay"] });
    const bullets = Array.from(el.querySelectorAll(".answer-card-bullet")).map((b) => b.textContent);
    expect(bullets).toContain("Completed claim form");
    expect(bullets).toContain("Proof of timely filing");
  });

  it("bullets render inline markdown (not raw **)", () => {
    const el = renderSec({ format: "bullets", label: "Key Points", bullets: ["File within **90 days** of denial", "Use the `provider portal`"] });
    const rows = Array.from(el.querySelectorAll(".answer-card-bullet"));
    expect(rows[0].querySelector("strong")?.textContent).toBe("90 days");
    expect(rows[0].textContent).not.toContain("**");
    expect(rows[1].querySelector("code")?.textContent).toBe("provider portal");
  });

  it("steps → ol.ac-fmt-steps ordered list", () => {
    const el = renderSec({ format: "steps", label: "Steps", data: { items: [{ label: "Gather your documentation" }, { label: "Complete the appeal form" }, { label: "Submit via the provider portal" }] } });
    const ol = el.querySelector("ol.ac-fmt-steps");
    expect(ol).not.toBeNull();
    const steps = Array.from(ol!.querySelectorAll("li.ac-fmt-step")).map((s) => s.textContent);
    expect(steps).toEqual(["Gather your documentation", "Complete the appeal form", "Submit via the provider portal"]);
  });

  it("stats → .ac-fmt-stat-tile tiles (value + label)", () => {
    const el = renderSec({ format: "stats", label: "Key Facts", data: { items: [{ label: "Initial filing", value: "180 days" }, { label: "Resubmission", value: "90 days" }, { label: "Copay", value: "$25" }] } });
    const tiles = Array.from(el.querySelectorAll(".ac-fmt-stat-tile"));
    expect(tiles.length).toBe(3);
    expect(tiles[0].querySelector(".ac-fmt-stat-value")?.textContent).toBe("180 days");
    expect(tiles[0].querySelector(".ac-fmt-stat-label")?.textContent).toBe("Initial filing");
  });

  it("table → table.ac-fmt-table with headers + rows", () => {
    const el = renderSec({ format: "table", label: "Details", data: { headers: ["Code", "Description", "Rate"], rows: [["90834", "Individual therapy", "$85.00"], ["90837", "Extended therapy", "$120.00"]] } });
    const tbl = el.querySelector("table.ac-fmt-table");
    expect(tbl).not.toBeNull();
    expect(Array.from(tbl!.querySelectorAll("thead th")).map((t) => t.textContent)).toEqual(["Code", "Description", "Rate"]);
    expect(tbl!.querySelector("tbody")?.textContent).toContain("Individual therapy");
    // wide/many-column tables get the horizontal-scroll wrapper (no body overflow)
    expect(el.querySelector(".ac-fmt-table-scroll")).not.toBeNull();
  });

  // Accordion cards (Ananth 2026-08-10): elevated structured cards collapse/expand.
  it("structured card → collapsible header (button + chevron) wrapping a body", () => {
    const el = renderSec({ format: "stats", label: "Key Facts", data: { items: [{ label: "Initial", value: "180 days" }] } });
    const sec = el.querySelector(".answer-card-section--stats")!;
    const toggle = sec.querySelector("button.ac-card-toggle");
    expect(toggle).not.toBeNull();
    expect(toggle!.getAttribute("aria-expanded")).toBe("true");   // primary default = expanded
    expect(toggle!.querySelector(".ac-card-chevron")).not.toBeNull();
    // body wraps the actual content (tiles live inside .ac-card-body)
    expect(sec.querySelector(".ac-card-body .ac-fmt-stat-tile")).not.toBeNull();
    expect(sec.classList.contains("ac-card--collapsed")).toBe(false);
  });

  it("visibility:detail card starts collapsed; click expands", () => {
    const el = renderSec({ format: "table", label: "Appendix", visibility: "detail", data: { headers: ["A"], rows: [["1"]] } });
    const sec = el.querySelector(".answer-card-section--table")!;
    expect(sec.classList.contains("ac-card--collapsed")).toBe(true);
    const toggle = sec.querySelector("button.ac-card-toggle") as HTMLButtonElement;
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    toggle.click();
    expect(sec.classList.contains("ac-card--collapsed")).toBe(false);
    expect(toggle.getAttribute("aria-expanded")).toBe("true");
  });

  it("bullets stay FLAT (no accordion toggle — answer-body continuation)", () => {
    const el = renderSec({ format: "bullets", label: "Points", bullets: ["one", "two"] });
    expect(el.querySelector(".ac-card-toggle")).toBeNull();
    expect(el.querySelector(".answer-card-bullet")).not.toBeNull();
  });
});

describe("renderEnvelope block renderers (Task #36 — single-contract, LLM Agent's real block shapes)", () => {
  // These call the envelope-block leaf renderers DIRECTLY with the exact top-level shapes
  // build_assistant_envelope_v1 emits (verified live: cid e5f721cc). No card, no dual-read.

  it("renderFormatBlock: table → table.ac-fmt-table (headers/rows at block top level)", () => {
    const el = renderFormatBlock({ type: "table", label: "Deadlines", headers: ["Payor", "Days"], rows: [["Sunshine", "180"], ["Humana", "90"]] });
    const tbl = el.querySelector("table.ac-fmt-table");
    expect(tbl).not.toBeNull();
    expect(Array.from(tbl!.querySelectorAll("thead th")).map((t) => t.textContent)).toEqual(["Payor", "Days"]);
    expect(tbl!.querySelector("tbody")?.textContent).toContain("Sunshine");
  });

  it("renderFormatBlock: stats → tiles (items at block top level, unified item shape)", () => {
    const el = renderFormatBlock({ type: "stats", label: "Facts", items: [{ label: "Initial", value: "180 days" }, { label: "Resubmit", value: "90 days", note: "with note" }] });
    const tiles = Array.from(el.querySelectorAll(".ac-fmt-stat-tile"));
    expect(tiles.length).toBe(2);
    expect(tiles[0].querySelector(".ac-fmt-stat-value")?.textContent).toBe("180 days");
    expect(tiles[1].querySelector(".ac-fmt-stat-note")?.textContent).toBe("with note");
  });

  it("renderFormatBlock: bullets → .answer-card-bullet with inline markdown (items = string[])", () => {
    const el = renderFormatBlock({ type: "bullets", label: "Points", items: ["File within **90 days**", "Use the `portal`"] });
    const rows = Array.from(el.querySelectorAll(".answer-card-bullet"));
    expect(rows.length).toBe(2);
    expect(rows[0].querySelector("strong")?.textContent).toBe("90 days");
    expect(rows[0].textContent).not.toContain("**");
  });

  it("renderFormatBlock: table/stats/conditions cells render inline markdown (not raw **)", () => {
    const t = renderFormatBlock({ type: "table", headers: ["**Provider**"], rows: [["**Participating**"]] });
    expect(t.querySelector("th strong")?.textContent).toBe("Provider");
    expect(t.querySelector("td strong")?.textContent).toBe("Participating");
    expect(t.textContent).not.toContain("**");
    const s = renderFormatBlock({ type: "stats", items: [{ label: "**Days**", value: "**180**" }] });
    expect(s.querySelector(".ac-fmt-stat-value strong")?.textContent).toBe("180");
    const c = renderFormatBlock({ type: "conditions", items: [{ condition: "**late**", result: "**deny**" }] });
    expect(c.querySelector(".ac-fmt-condition-if strong")?.textContent).toBe("late");
  });

  it("renderFormatBlock: bars → weighted fill (item.weight 0-1, not max)", () => {
    const el = renderFormatBlock({ type: "bars", label: "Mix", items: [{ label: "Denials", weight: 0.75 }, { label: "Paid", weight: 0.25 }] });
    const fills = Array.from(el.querySelectorAll(".ac-fmt-bar-fill")) as HTMLElement[];
    expect(fills[0].style.width).toBe("75%");
    expect(fills[1].style.width).toBe("25%");
  });

  it("renderFormatBlock: conditions → if/then rows (condition/result keys)", () => {
    const el = renderFormatBlock({ type: "conditions", label: "Rules", items: [{ condition: "Late filing", result: "Denied CO-29" }] });
    expect(el.querySelector(".ac-fmt-condition-if")?.textContent).toBe("Late filing");
    expect(el.querySelector(".ac-fmt-condition-then")?.textContent).toBe("Denied CO-29");
  });

  it("renderFormatBlock: steps → ordered list (items with label)", () => {
    const el = renderFormatBlock({ type: "steps", label: "How", items: [{ label: "Gather docs" }, { label: "Submit" }] });
    const steps = Array.from(el.querySelectorAll("li.ac-fmt-step")).map((s) => s.textContent);
    expect(steps).toEqual(["Gather docs", "Submit"]);
  });

  it("renderFormatBlock: domain_card variant=appeals_playbook → appeals renderer (nested data passthrough)", () => {
    const el = renderFormatBlock({ type: "domain_card", variant: "appeals_playbook", data: { payor: "Sunshine", carc_codes: ["29"], deadline_appeal_days: 90, strategy: "Prove timely filing" } });
    // reuses the appeals playbook renderer (its root class), proving verbatim data passthrough by variant
    expect(el.querySelector(".ac-appeals-playbook, .answer-card-section--appeals_playbook")).not.toBeNull();
    expect(el.textContent).toContain("Sunshine");
  });

  it("renderModeBadge: CANONICAL/RECITAL show, FACTUAL/BLENDED silent (Chat Master 2026-08-10)", () => {
    expect(renderModeBadge("CANONICAL")?.textContent).toBe("CANONICAL");
    expect(renderModeBadge("RECITAL")?.textContent).toBe("RECITAL");
    expect(renderModeBadge("FACTUAL")).toBeNull();
    expect(renderModeBadge("BLENDED")).toBeNull();
    expect(renderModeBadge(undefined)).toBeNull();
  });

  it("renderFirstPass: rd-progression from trace_rounds (mirrors card path)", () => {
    const el = renderFirstPass({ type: "first_pass", draft_markdown: "draft", trace_rounds: [
      { round: 1, running_answer: "first cut" },
      { round: 2, learned: "found a gap", gaps_open: ["x"] },
    ] });
    expect(el).not.toBeNull();
    expect(el!.querySelector(".ac-first-pass-summary")?.textContent).toBe("First pass · 2 rounds");
    const steps = Array.from(el!.querySelectorAll(".ac-rd-step"));
    expect(steps.length).toBe(2);
    expect(steps[0].querySelector(".ac-rd-label")?.textContent).toBe("rd-1");
    expect(steps[1].querySelector(".ac-rd-answer")?.classList.contains("ac-rd-thought")).toBe(true); // learned-only = thought
  });

  it("renderFirstPass: null when nothing to show", () => {
    expect(renderFirstPass({ type: "first_pass" })).toBeNull();
    expect(renderFirstPass({ type: "first_pass", draft_markdown: "  ", trace_rounds: [] })).toBeNull();
  });
});

describe("envelopeToAnswerCard (Task #36 cutover — envelope is the single source)", () => {
  // The real live block shape (cid 31a418cb): a full-table turn.
  const ENV = [
    { type: "mode_badge", mode: "FACTUAL" },
    { type: "tool_attribution", label: "Research", icon: "search" },
    { type: "direct_answer", markdown: "Sunshine Health requires filing within **180 days**." },
    { type: "table", label: "Sunshine Health Timely Filing Deadlines", headers: ["Filing Type", "Deadline"], rows: [["Initial", "180 days"], ["Corrected", "90 days"]] },
    { type: "first_pass", draft_markdown: "draft", trace_rounds: [{ round: 1, running_answer: "cut" }] },
    { type: "takeaways", items: ["a", "b"] },
    { type: "sources", refs: [{ index: 1 }] },
    { type: "suggested_questions", items: ["What about Humana?"] },
  ];

  it("maps blocks → AnswerCard body fields (mode/prose/section/first-pass/followups)", () => {
    const card = envelopeToAnswerCard(ENV)!;
    expect(card).not.toBeNull();
    expect(card.mode).toBe("FACTUAL");
    expect(card.display_summary).toContain("180 days");
    expect(card.direct_answer).toContain("180 days");
    expect(card.sections.length).toBe(1);
    expect(card.sections[0].format).toBe("table");
    expect(card.sections[0].label).toBe("Sunshine Health Timely Filing Deadlines");
    expect((card.sections[0].data as { rows: string[][] }).rows.length).toBe(2);
    expect(card.react_draft).toBe("draft");
    expect(card.reasoning_trace?.length).toBe(1);
    expect(card.followups?.[0].question).toBe("What about Humana?");
  });

  it("round-trips through renderAnswerCard — the derived card renders the table once", () => {
    const card = envelopeToAnswerCard(ENV)!;
    const el = renderAnswerCard(card);
    const tables = el.querySelectorAll(".answer-card-section--table table");
    expect(tables.length).toBe(1);
    expect(el.querySelector(".ac-fmt-table tbody")?.textContent).toContain("180 days");
  });

  it("returns null when the envelope carries no body content (→ caller falls back to message card)", () => {
    expect(envelopeToAnswerCard([{ type: "tool_attribution", label: "x" }, { type: "sources", refs: [] }])).toBeNull();
    expect(envelopeToAnswerCard([])).toBeNull();
  });

  it("bullets block → bullets section (items on sec.bullets, not data)", () => {
    const card = envelopeToAnswerCard([{ type: "direct_answer", markdown: "hi" }, { type: "bullets", label: "Points", items: ["one", "two"] }])!;
    const sec = card.sections.find((s) => s.format === "bullets")!;
    expect(sec.bullets).toEqual(["one", "two"]);
  });

  it("MERGE: envelope wins on body, base keeps card-only fields (suggest_escalate/output_intent)", () => {
    // base = message card with a stale/empty table section + card-only fields the envelope can't carry
    const base = {
      direct_answer: "old", display_summary: "old",
      sections: [{ label: "Deadlines", format: "table", visibility: "primary", bullets: [], data: { headers: ["A"], rows: [] } }],
      suggest_escalate: true, output_intent: "read", correction: { original: "x", corrected: "y" },
    } as unknown as Parameters<typeof renderAnswerCard>[0];
    const env = [
      { type: "direct_answer", markdown: "new answer" },
      { type: "table", label: "Deadlines", headers: ["Payor", "Days"], rows: [["Sunshine", "180"]] },
    ];
    const merged = envelopeToAnswerCard(env, base)!;
    // envelope body wins
    expect(merged.display_summary).toBe("new answer");
    expect((merged.sections[0].data as { rows: string[][] }).rows.length).toBe(1); // the FULL table, not the empty base one
    // card-only fields survive
    expect(merged.suggest_escalate).toBe(true);
    expect(merged.output_intent).toBe("read");
    expect(merged.correction?.corrected).toBe("y");
  });

  it("MERGE: empty envelope + base → returns base unchanged (fallback safety)", () => {
    const base = { direct_answer: "keep", sections: [] } as unknown as Parameters<typeof renderAnswerCard>[0];
    expect(envelopeToAnswerCard([{ type: "tool_attribution", label: "x" }], base)?.direct_answer).toBe("keep");
  });
});

describe("renderEnvelope assembler (Task #36 — single consumer, order + peel + drop)", () => {
  // The real live block order (cid d401de73): [mode_badge, tool_attribution, direct_answer, bullets, first_pass, sources]
  it("dispatches in backend order, peels sources, renders each once", () => {
    const chromeSeen: string[] = [];
    const { answerBody, sources, dropped } = renderEnvelope([
      { type: "mode_badge", mode: "CANONICAL" },
      { type: "tool_attribution", label: "Research", icon: "search" },
      { type: "direct_answer", markdown: "The deadline is **180 days**." },
      { type: "bullets", label: "Key Points", items: ["File on time", "Keep proof"] },
      { type: "first_pass", trace_rounds: [{ round: 1, running_answer: "cut" }] },
      { type: "sources", refs: [{ n: 1 }] },
    ], {
      renderExtraBlock: (b) => { chromeSeen.push(b.type); const d = document.createElement("div"); d.className = "chrome-" + b.type; return d; },
    });
    // sources peeled out of the body, handed back for the Sources tab
    expect(sources?.type).toBe("sources");
    expect(answerBody.querySelector("[class*='sources']")).toBeNull();
    // order preserved: mode badge, then chrome tool_attribution, then direct answer, then bullets, then first pass
    const kids = Array.from(answerBody.children).map((c) => (c as HTMLElement).className);
    expect(kids[0]).toContain("ac-answer-mode-label");     // CANONICAL badge
    expect(kids[1]).toBe("chrome-tool_attribution");        // injected chrome, in place
    expect(kids[2]).toBe("ac-answer-envelope-body");        // direct_answer prose
    expect(answerBody.querySelector("strong")?.textContent).toBe("180 days"); // markdown-rendered, not raw
    expect(kids[3]).toContain("answer-card-section");       // bullets format block
    expect(kids[4]).toBe("ac-first-pass");                  // first pass
    expect(chromeSeen).toEqual(["tool_attribution"]);
    expect(dropped).toEqual([]);
  });

  it("drops off-contract blocks and counts them", () => {
    const counted: string[] = [];
    const { answerBody, dropped } = renderEnvelope([
      { type: "direct_answer", markdown: "hi" },
      { type: "wat_is_this", payload: 1 },
      { type: "also_unknown" },
    ], { onUnknownBlock: (t) => counted.push(t) });
    expect(dropped).toEqual(["wat_is_this", "also_unknown"]);
    expect(counted).toEqual(["wat_is_this", "also_unknown"]);
    expect(answerBody.children.length).toBe(1); // only the direct_answer survived
  });

  it("mode_badge/first_pass returning null is NOT a drop", () => {
    const counted: string[] = [];
    const { dropped } = renderEnvelope([
      { type: "mode_badge", mode: "FACTUAL" },   // silent, returns null
      { type: "first_pass" },                     // nothing to show, returns null
    ], { onUnknownBlock: (t) => counted.push(t) });
    expect(dropped).toEqual([]);
    expect(counted).toEqual([]);
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
