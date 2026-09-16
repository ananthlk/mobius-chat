// @vitest-environment jsdom
//
// CROSS-BOUNDARY TEST: the Python formatter's real output, through the real
// renderer.
//
// The fixture is written by scripts/emit_card_fixtures.py from real answer
// drafts (each copied from a live trace or an A/B panel, named by cid). This
// file feeds those cards to renderAnswerCard — bubble.ts itself, not a copy.
//
// WHY THE COPY WAS NOT ENOUGH. An earlier harness re-implemented
// _renderSectionBody in Python to render the same DOM. It was faithful and it
// found a real bug, but a faithful copy is still a copy: it cannot fail when
// bubble.ts changes, which is the one failure this seam actually has.
//
// THE FAILURE MODE THIS EXISTS FOR: a section whose `format` the renderer
// cannot fill renders BLANK. No error, no warning, no console line — the
// label appears and the body is empty. Nothing on the Python side can detect
// it, because the Python side thinks it emitted a section. That is the whole
// reason to run the two halves against each other.
import { describe, it, expect } from "vitest";
import { renderAnswerCard } from "./bubble";
import type { AnswerCard } from "../answer-card";
import cards from "../__fixtures__/formatter-cards.json";

type Case = {
  question: string;
  note: string;
  card: AnswerCard & { sections: Array<Record<string, unknown>> };
  rules: string[];
};

const cases = cards as unknown as Case[];

describe("formatter output renders in the real answer card", () => {
  it("has cases to run", () => {
    expect(cases.length).toBeGreaterThan(0);
  });

  cases.forEach((c) => {
    describe(c.note, () => {
      it("renders without throwing", () => {
        expect(() => renderAnswerCard(c.card)).not.toThrow();
      });

      it("EVERY section the formatter emitted has a non-empty body", () => {
        // The blank-section failure. A format the renderer does not handle
        // produces a label with nothing under it, silently.
        const el = renderAnswerCard(c.card);
        const labels = [...el.querySelectorAll(".answer-card-section-label")];
        expect(labels.length).toBe(c.card.sections.length);
        c.card.sections.forEach((sec, i) => {
          const section = labels[i].parentElement!;
          const body = section.querySelector(".ac-card-body") ?? section;
          // The real renderer's selectors, read from bubble.ts — NOT guessed.
          // Bullets are div.answer-card-bullet, not <ul><li>; the Python
          // harness drew <ul> and was wrong about it, which is precisely why
          // this test renders through bubble.ts instead of a copy.
          const rendered = body.querySelector(
            "table.ac-fmt-table, ol.ac-fmt-steps, .ac-fmt-stats, .ac-fmt-bars, " +
            ".ac-fmt-conditions, .answer-card-bullet");
          expect(rendered, `section ${i} (${sec.format}) rendered blank`).not.toBeNull();
          expect(body.textContent?.trim().length ?? 0).toBeGreaterThan(0);
        });
      });

      it("emits no format the renderer cannot fill", () => {
        const RENDERABLE = new Set([
          "table", "stats", "bars", "steps", "conditions", "bullets",
          "appeals_rules", "appeals_playbook",
        ]);
        c.card.sections.forEach((sec) => {
          expect(RENDERABLE.has(String(sec.format))).toBe(true);
        });
      });

      it("shows no raw markdown or unrendered tags anywhere on the card", () => {
        const text = renderAnswerCard(c.card).textContent ?? "";
        expect(text).not.toMatch(/\|\s*:?-{3,}/);   // a pipe-table separator row
        expect(text).not.toContain("<br");
        expect(text).not.toMatch(/\*\*/);            // unconverted bold markers
      });
    });
  });

  it("a table's rendered rows match the rows the formatter produced", () => {
    cases
      .filter((c) => c.card.sections.some((s) => s.format === "table"))
      .forEach((c) => {
        const el = renderAnswerCard(c.card);
        const tables = [...el.querySelectorAll("table.ac-fmt-table")];
        const expected = c.card.sections.filter((s) => s.format === "table");
        expect(tables.length).toBe(expected.length);
        tables.forEach((tbl, i) => {
          const data = expected[i].data as { rows: string[][]; headers: string[] };
          expect(tbl.querySelectorAll("tbody tr").length).toBe(data.rows.length);
          expect(tbl.querySelectorAll("thead th").length).toBe(data.headers.length);
        });
      });
  });

  it("bullets reach the renderer at the top level, not under data", () => {
    // bubble.ts reads sec.bullets specifically. Putting them under data
    // renders an empty list and nothing says so.
    cases.forEach((c) => {
      c.card.sections
        .filter((s) => s.format === "bullets")
        .forEach((s) => {
          expect(Array.isArray(s.bullets)).toBe(true);
          expect((s.bullets as string[]).length).toBeGreaterThan(0);
        });
    });
  });

  it("an abstained card renders as prose with no empty section shell", () => {
    cases
      .filter((c) => c.card.sections.length === 0)
      .forEach((c) => {
        const el = renderAnswerCard(c.card);
        expect(el.querySelectorAll(".answer-card-section-label").length).toBe(0);
      });
  });
});

// ── steps with inline markup ────────────────────────────────────────────────
// .ac-fmt-step is display:flex with a ::before number badge, so it expects
// exactly two children. Setting innerHTML on the <li> made every element
// _inlineMd produced its own FLEX ITEM: a step containing bold spans rendered
// as one narrow column per span, with "fax" squeezed to a letter per line.
//
// Only fires when a step contains bold — and react's FORMAT RULES ask for bold
// on entity names, deadlines, codes and contact info, so real steps almost
// always do. Every fixture step was plain, which is why it survived.
describe("a step's inline markup does not become columns", () => {
  const stepsCard = {
    direct_answer: "To appeal a CARC 22 denial, here is what to do:",
    sections: [{
      intent: "process", label: "Steps", format: "steps",
      data: { items: [
        { label: "**Submit an appeal within 90 days** of the denial date via their "
                 + "**Secure Provider Portal**, **fax** (1-833-504-0580), or **mail**." },
        { label: "Complete and attach the **Provider Claim Adjustment Request Form**." },
      ] },
    }],
  } as unknown as AnswerCard;

  it("each step has exactly ONE element child — the text wrapper", () => {
    const el = renderAnswerCard(stepsCard);
    const steps = [...el.querySelectorAll("li.ac-fmt-step")];
    expect(steps.length).toBe(2);
    steps.forEach((li, i) => {
      expect(li.children.length, `step ${i} has ${li.children.length} flex children`).toBe(1);
      expect(li.children[0].className).toBe("ac-fmt-step-text");
    });
  });

  it("the bold survives inside the wrapper rather than beside it", () => {
    const el = renderAnswerCard(stepsCard);
    const first = el.querySelector("li.ac-fmt-step .ac-fmt-step-text")!;
    expect(first.querySelectorAll("strong").length).toBe(4);
    expect(first.textContent).toContain("1-833-504-0580");
  });

  it("no <strong> is ever a direct child of the flex row", () => {
    const el = renderAnswerCard(stepsCard);
    expect(el.querySelectorAll("li.ac-fmt-step > strong").length).toBe(0);
  });
});
