// render/bubble — the chat-bubble surface renderer. Renders the AnswerCard the
// bubble-backend produces. Pure DOM: shared primitives come from ui-helpers, the parse/
// visibility model from answer-card, the tab/slot model from card-render-model. Its one
// app-state dependency (opening the task dialog) is INJECTED via opts.onCreateTask — no
// reach-back into app.ts. See docs/bubble-backend-contract.md (the FE half of the pair).

import type {
  AnswerCard, AnswerCardSection,
  AppealsRulesData, AppealsRule, AppealsPlaybookData,
} from "../answer-card";
import { MAX_SECTIONS } from "../answer-card";
import { TAB_ORDER, type TabKey } from "../card-render-model";
import {
  simpleMarkdownToHtml, renderConfidenceBadge, renderQcAuditBadge,
  type QcAuditInfo, type FollowupLineNormalized,
} from "../ui-helpers";

const MAX_BULLETS_PER_SECTION = 4;

// Task #10 — output_intent is the enricher's deliverable classification (the REAL backend enum
// in app/stages/integrate.py: read/report/email/sms/emr/appeal/payor_report). It is an INTERNAL
// telemetry signal, surfaced only as a Diagnostics row — never on the card face (Chat Master
// 2026-08-05). We validate against the known enum so a garbage value never shows.
const KNOWN_OUTPUT_INTENTS: ReadonlySet<string> = new Set([
  "read", "report", "email", "sms", "emr", "appeal", "payor_report",
]);

/** Normalize output_intent to its canonical value for the Diagnostics telemetry row, or null
 *  when absent/unknown (we never invent a value the backend didn't send). Exported for test. */
export function formatOutputIntentLabel(outputIntent?: string): string | null {
  const key = (outputIntent ?? "").trim().toLowerCase();
  return KNOWN_OUTPUT_INTENTS.has(key) ? key : null;
}

function _renderSectionBody(sec: AnswerCardSection, body: HTMLElement): void {
  const fmt = sec.format ?? "bullets";
  const data = sec.data;

  if (fmt === "table" && data?.headers && data?.rows) {
    const tbl = document.createElement("table");
    tbl.className = "ac-fmt-table";
    const thead = tbl.createTHead();
    const hRow = thead.insertRow();
    data.headers.forEach((h) => { const th = document.createElement("th"); th.textContent = h; hRow.appendChild(th); });
    const tbody = tbl.createTBody();
    data.rows.forEach((row) => {
      const tr = tbody.insertRow();
      row.forEach((cell) => { const td = tr.insertCell(); td.textContent = cell; });
    });
    // Scroll wrapper: on narrow widths the table scrolls horizontally instead of crushing
    // its columns (§1.3 "tables scroll horizontally on mobile, no body overflow").
    const scroll = document.createElement("div");
    scroll.className = "ac-fmt-table-scroll";
    scroll.appendChild(tbl);
    body.appendChild(scroll);
    return;
  }

  if (fmt === "steps" && data?.items) {
    const ol = document.createElement("ol");
    ol.className = "ac-fmt-steps";
    data.items.forEach((item) => {
      const li = document.createElement("li");
      li.className = "ac-fmt-step";
      li.textContent = typeof item === "string" ? item : (item.label ?? "");
      ol.appendChild(li);
    });
    body.appendChild(ol);
    return;
  }

  if (fmt === "stats" && data?.items) {
    const grid = document.createElement("div");
    grid.className = "ac-fmt-stats";
    data.items.slice(0, 4).forEach((item) => {
      const tile = document.createElement("div");
      tile.className = "ac-fmt-stat-tile";
      const val = document.createElement("div");
      val.className = "ac-fmt-stat-value";
      val.textContent = item.value ?? "";
      const lbl = document.createElement("div");
      lbl.className = "ac-fmt-stat-label";
      lbl.textContent = item.label ?? "";
      tile.appendChild(val);
      tile.appendChild(lbl);
      if (item.note) {
        const note = document.createElement("div");
        note.className = "ac-fmt-stat-note";
        note.textContent = item.note;
        tile.appendChild(note);
      }
      grid.appendChild(tile);
    });
    body.appendChild(grid);
    return;
  }

  if (fmt === "bars" && data?.items) {
    const list = document.createElement("div");
    list.className = "ac-fmt-bars";
    data.items.forEach((item) => {
      const row = document.createElement("div");
      row.className = "ac-fmt-bar-row";
      const lbl = document.createElement("div");
      lbl.className = "ac-fmt-bar-label";
      lbl.textContent = item.label ?? "";
      const track = document.createElement("div");
      track.className = "ac-fmt-bar-track";
      const fill = document.createElement("div");
      fill.className = "ac-fmt-bar-fill";
      const pct = Math.round(Math.min(1, Math.max(0, item.weight ?? 0)) * 100);
      fill.style.width = `${pct}%`;
      track.appendChild(fill);
      row.appendChild(lbl);
      row.appendChild(track);
      if (item.note) {
        const note = document.createElement("div");
        note.className = "ac-fmt-bar-note";
        note.textContent = item.note;
        row.appendChild(note);
      }
      list.appendChild(row);
    });
    body.appendChild(list);
    return;
  }

  if (fmt === "conditions" && data?.items) {
    const list = document.createElement("div");
    list.className = "ac-fmt-conditions";
    data.items.forEach((item) => {
      const row = document.createElement("div");
      row.className = "ac-fmt-condition-row";
      const cond = document.createElement("div");
      cond.className = "ac-fmt-condition-if";
      cond.textContent = item.condition ?? "";
      const result = document.createElement("div");
      result.className = "ac-fmt-condition-then";
      result.textContent = item.result ?? "";
      row.appendChild(cond);
      row.appendChild(result);
      list.appendChild(row);
    });
    body.appendChild(list);
    return;
  }

  if (fmt === "appeals_rules") {
    _renderAppealsRules(sec, body);
    return;
  }

  if (fmt === "appeals_playbook") {
    _renderAppealsPlaybook(sec, body);
    return;
  }

  // Default: bullets
  const allBullets = sec.bullets ?? [];
  const visibleBullets = allBullets.slice(0, MAX_BULLETS_PER_SECTION);
  const hiddenBullets = allBullets.slice(MAX_BULLETS_PER_SECTION);

  visibleBullets.forEach((b) => {
    const li = document.createElement("div");
    li.className = "answer-card-bullet";
    li.textContent = b;
    body.appendChild(li);
  });

  if (hiddenBullets.length > 0) {
    const overflow = document.createElement("div");
    overflow.className = "answer-card-bullets-overflow";
    overflow.style.display = "none";
    hiddenBullets.forEach((b) => {
      const li = document.createElement("div");
      li.className = "answer-card-bullet";
      li.textContent = b;
      overflow.appendChild(li);
    });
    body.appendChild(overflow);

    const more = document.createElement("button");
    more.type = "button";
    more.className = "answer-card-more";
    more.setAttribute("aria-label", "Show more bullets");
    more.textContent = `Show ${hiddenBullets.length} more`;
    let expanded = false;
    more.addEventListener("click", () => {
      expanded = !expanded;
      overflow.style.display = expanded ? "" : "none";
      more.textContent = expanded ? "Show less" : `Show ${hiddenBullets.length} more`;
    });
    body.appendChild(more);
  }
}

// ── Appeals typed sections (Appeals Agent, 2026-08-06) ──────────────────────────────
// Rendered from pre_built_sections emitted by appeals_lookup_rules / appeals_get_playbook.
// `sec.data` is the tool output verbatim; SectionData's type is too narrow for it, so we
// re-cast through unknown to the appeals shapes. All text goes in via textContent (never
// innerHTML) — appeals data is model/tool-authored and must not be trusted as markup.

function _chip(text: string, cls: string): HTMLElement {
  const el = document.createElement("span");
  el.className = cls;
  el.textContent = text;
  return el;
}

/** Only http(s) URLs are honored for tool-authored links — never javascript:/data: etc. */
function _safeHttpUrl(url: unknown): string | null {
  if (typeof url !== "string" || !url.trim()) return null;
  try {
    const u = new URL(url.trim());
    return (u.protocol === "http:" || u.protocol === "https:") ? u.href : null;
  } catch {
    return null;
  }
}

function _renderAppealsRules(sec: AnswerCardSection, body: HTMLElement): void {
  const data = (sec.data as unknown as AppealsRulesData) ?? {};
  const wrap = document.createElement("div");
  wrap.className = "ac-appeals-rules";

  // Header: CARC badge + title + archetype chip.
  if (data.carc || data.carc_title || data.archetype) {
    const head = document.createElement("div");
    head.className = "ac-appeals-head";
    if (data.carc) head.appendChild(_chip(`CARC ${data.carc}`, "ac-appeals-carc"));
    if (data.carc_title) {
      const t = document.createElement("span");
      t.className = "ac-appeals-carc-title";
      t.textContent = data.carc_title;
      head.appendChild(t);
    }
    if (data.archetype) head.appendChild(_chip(data.archetype, "ac-appeals-archetype"));
    wrap.appendChild(head);
  }

  const rules = Array.isArray(data.rules) ? data.rules : [];
  if (rules.length === 0) {
    const empty = document.createElement("div");
    empty.className = "ac-appeals-empty";
    empty.textContent = "No appeal rules on file for this CARC.";
    wrap.appendChild(empty);
    body.appendChild(wrap);
    return;
  }

  rules.forEach((rule: AppealsRule) => {
    const det = document.createElement("details");
    det.className = "ac-appeals-rule";

    // Collapsed row: rule_id chip | rule_name | triggers_when (1 line, truncated by CSS).
    const sum = document.createElement("summary");
    sum.className = "ac-appeals-rule-summary";
    if (rule.rule_id) sum.appendChild(_chip(rule.rule_id, "ac-appeals-rule-id"));
    const name = document.createElement("span");
    name.className = "ac-appeals-rule-name";
    name.textContent = rule.rule_name || "Appeal rule";
    sum.appendChild(name);
    if (rule.triggers_when) {
      const trig = document.createElement("span");
      trig.className = "ac-appeals-rule-trigger-brief";
      trig.textContent = rule.triggers_when;
      sum.appendChild(trig);
    }
    det.appendChild(sum);

    // Expanded body.
    const expand = document.createElement("div");
    expand.className = "ac-appeals-rule-body";

    if (rule.triggers_when) {
      const row = document.createElement("div");
      row.className = "ac-appeals-field ac-appeals-field--trigger";
      const lbl = _chip("Applies when", "ac-appeals-field-label");
      const val = document.createElement("div");
      val.className = "ac-appeals-field-value";
      val.textContent = rule.triggers_when;
      row.appendChild(lbl); row.appendChild(val);
      expand.appendChild(row);
    }

    // appeal_argument — the assertion to make in the letter. THE key field: highlighted.
    if (rule.appeal_argument) {
      const arg = document.createElement("div");
      arg.className = "ac-appeals-argument";
      const lbl = _chip("Appeal argument", "ac-appeals-argument-label");
      const val = document.createElement("div");
      val.className = "ac-appeals-argument-value";
      val.textContent = rule.appeal_argument;
      arg.appendChild(lbl); arg.appendChild(val);
      expand.appendChild(arg);
    }

    // rule_statement — secondary legal principle, muted, below the argument.
    if (rule.rule_statement) {
      const st = document.createElement("div");
      st.className = "ac-appeals-statement";
      st.textContent = rule.rule_statement;
      expand.appendChild(st);
    }

    // requires list.
    const requires = Array.isArray(rule.requires) ? rule.requires : [];
    if (requires.length) {
      const row = document.createElement("div");
      row.className = "ac-appeals-field ac-appeals-field--requires";
      row.appendChild(_chip("Requires", "ac-appeals-field-label"));
      const ul = document.createElement("ul");
      ul.className = "ac-appeals-requires";
      requires.forEach((r) => {
        const li = document.createElement("li");
        li.textContent = r;
        ul.appendChild(li);
      });
      row.appendChild(ul);
      expand.appendChild(row);
    }

    // authority tag.
    if (rule.authority_notes) {
      const auth = document.createElement("div");
      auth.className = "ac-appeals-authority";
      auth.appendChild(_chip("Authority", "ac-appeals-authority-label"));
      const val = document.createElement("span");
      val.className = "ac-appeals-authority-value";
      val.textContent = rule.authority_notes;
      auth.appendChild(val);
      expand.appendChild(auth);
    }

    // payor variant pills.
    const variants = Array.isArray(rule.payor_variants) ? rule.payor_variants : [];
    if (variants.length) {
      const pills = document.createElement("div");
      pills.className = "ac-appeals-variants";
      variants.forEach((v) => {
        const label = typeof v === "string"
          ? v
          : [v.payor, v.note].filter(Boolean).join(" — ");
        if (label) pills.appendChild(_chip(label, "ac-appeals-variant-pill"));
      });
      if (pills.childElementCount) expand.appendChild(pills);
    }

    det.appendChild(expand);
    wrap.appendChild(det);
  });

  // Footer: deep link to the appeals rules-library admin page (scheme-guarded).
  const adminUrl = _safeHttpUrl(data.admin_url);
  if (adminUrl) {
    const footer = document.createElement("div");
    footer.className = "ac-appeals-admin";
    const a = document.createElement("a");
    a.className = "ac-appeals-admin-link";
    a.href = adminUrl;
    a.target = "_blank";
    a.rel = "noopener noreferrer";
    a.textContent = "✏ Edit rules in Admin →";
    footer.appendChild(a);
    wrap.appendChild(footer);
  }

  body.appendChild(wrap);
}

function _renderAppealsPlaybook(sec: AnswerCardSection, body: HTMLElement): void {
  const data = (sec.data as unknown as AppealsPlaybookData) ?? {};
  const wrap = document.createElement("div");
  wrap.className = "ac-appeals-playbook";

  // No playbook on file → soft empty state, not an empty card.
  if (data.found === false) {
    const empty = document.createElement("div");
    empty.className = "ac-appeals-playbook-empty";
    empty.textContent = data.message || "No appeal playbook on file for this payor.";
    wrap.appendChild(empty);
    body.appendChild(wrap);
    return;
  }

  // Header line: payor + carc_group.
  if (data.payor || data.carc_group) {
    const head = document.createElement("div");
    head.className = "ac-appeals-playbook-head";
    if (data.payor) head.appendChild(_chip(data.payor, "ac-appeals-playbook-payor"));
    if (data.carc_group) {
      const g = document.createElement("span");
      g.className = "ac-appeals-playbook-group";
      g.textContent = data.carc_group;
      head.appendChild(g);
    }
    wrap.appendChild(head);
  }

  // Meta row: deadline badge · submission method · portal link.
  const meta = document.createElement("div");
  meta.className = "ac-appeals-playbook-meta";
  if (typeof data.deadline_appeal_days === "number") {
    meta.appendChild(_chip(`${data.deadline_appeal_days}-day deadline`, "ac-appeals-deadline"));
  }
  if (data.submission_method) {
    meta.appendChild(_chip(data.submission_method, "ac-appeals-method"));
  }
  const portalUrl = _safeHttpUrl(data.portal_url);
  if (portalUrl) {
    const a = document.createElement("a");
    a.className = "ac-appeals-portal";
    a.href = portalUrl;
    a.target = "_blank";
    a.rel = "noopener noreferrer";
    a.textContent = "Submission portal";
    meta.appendChild(a);
  }
  if (meta.childElementCount) wrap.appendChild(meta);

  // Docs checklist (required vs optional).
  const docs = Array.isArray(data.docs_required) ? data.docs_required : [];
  if (docs.length) {
    const dl = document.createElement("div");
    dl.className = "ac-appeals-docs";
    dl.appendChild(_chip("Documents", "ac-appeals-docs-label"));
    const ul = document.createElement("ul");
    ul.className = "ac-appeals-docs-list";
    docs.forEach((d) => {
      const li = document.createElement("li");
      li.className = d.required === false ? "ac-appeals-doc ac-appeals-doc--optional" : "ac-appeals-doc ac-appeals-doc--required";
      const mark = document.createElement("span");
      mark.className = "ac-appeals-doc-mark";
      mark.textContent = d.required === false ? "○" : "●";
      const txt = document.createElement("span");
      txt.className = "ac-appeals-doc-text";
      txt.textContent = (d.doc || "") + (d.required === false ? " (optional)" : "");
      li.appendChild(mark); li.appendChild(txt);
      ul.appendChild(li);
    });
    dl.appendChild(ul);
    wrap.appendChild(dl);
  }

  // Appeal levels ladder.
  const levels = Array.isArray(data.appeal_levels) ? data.appeal_levels : [];
  if (levels.length) {
    const ladder = document.createElement("div");
    ladder.className = "ac-appeals-levels";
    ladder.appendChild(_chip("Appeal levels", "ac-appeals-levels-label"));
    const ol = document.createElement("ol");
    ol.className = "ac-appeals-levels-list";
    levels.forEach((lv) => {
      const li = document.createElement("li");
      li.className = "ac-appeals-level";
      const name = document.createElement("span");
      name.className = "ac-appeals-level-name";
      name.textContent = lv.name || (lv.level != null ? `Level ${lv.level}` : "Level");
      li.appendChild(name);
      if (typeof lv.deadline_days === "number") {
        li.appendChild(_chip(`${lv.deadline_days}d`, "ac-appeals-level-deadline"));
      }
      ol.appendChild(li);
    });
    ladder.appendChild(ol);
    wrap.appendChild(ladder);
  }

  body.appendChild(wrap);
}

function renderOneSection(sec: AnswerCardSection): HTMLElement {
  const sectionEl = document.createElement("div");
  sectionEl.className = `answer-card-section answer-card-section--${sec.format ?? "bullets"}`;
  const labelEl = document.createElement("div");
  labelEl.className = "answer-card-section-label";
  labelEl.textContent = sec.label || "";
  sectionEl.appendChild(labelEl);
  _renderSectionBody(sec, sectionEl);
  return sectionEl;
}

/**
 * Inline corrections (Ananth 2026-08-07): render each {original, corrected} pair as a redline IN
 * the answer prose — strike the original, insert the corrected in a distinct colour — instead of a
 * separate Corrections tab. Walks the container's text nodes, finds the FIRST node containing the
 * `corrected` string, and splits it into  [before] <del>original</del> <ins>corrected</ins> [after].
 * Exact-substring match only (facts/dates/numbers match cleanly); a correction whose corrected text
 * isn't found verbatim is skipped rather than misplaced. Never uses innerHTML — text nodes only.
 */
export function applyInlineCorrections(
  container: HTMLElement,
  corrections: ReadonlyArray<{ original?: string; corrected?: string }>,
): void {
  for (const c of corrections) {
    const orig = (c.original ?? "").trim();
    const corr = (c.corrected ?? "").trim();
    if (!corr) continue;
    const walker = document.createTreeWalker(container, NodeFilter.SHOW_TEXT);
    let target: Text | null = null;
    while (walker.nextNode()) {
      const t = walker.currentNode as Text;
      // Don't double-apply inside an existing redline.
      if (t.parentElement?.closest(".ac-redline")) continue;
      if (t.nodeValue && t.nodeValue.includes(corr)) { target = t; break; }
    }
    if (!target || !target.nodeValue) continue;
    const idx = target.nodeValue.indexOf(corr);
    const before = target.nodeValue.slice(0, idx);
    const after = target.nodeValue.slice(idx + corr.length);
    const frag = document.createDocumentFragment();
    if (before) frag.appendChild(document.createTextNode(before));
    const rl = document.createElement("span");
    rl.className = "ac-redline";
    if (orig) {
      const del = document.createElement("del");
      del.className = "ac-redline-del";
      del.textContent = orig;
      rl.appendChild(del);
      rl.appendChild(document.createTextNode(" "));
    }
    const ins = document.createElement("ins");
    ins.className = "ac-redline-ins";
    ins.textContent = corr;
    rl.appendChild(ins);
    frag.appendChild(rl);
    if (after) frag.appendChild(document.createTextNode(after));
    target.parentNode?.replaceChild(frag, target);
  }
}

export function renderAnswerCard(
  card: AnswerCard,
  isError?: boolean,
  opts?: {
    onFollowupClick?: (question: string) => void;
    sourceConfidenceStrip?: string;
    showConfidenceBadge?: boolean;
    suppressFollowups?: boolean;
    nextQuestions?: FollowupLineNormalized[];
    qcAudit?: QcAuditInfo;
    /** When true (admin + QA fail), omit source confidence badge */
    suppressConfidenceForAdminQcFail?: boolean;
    /** Corrections rows — from envelope callout/correction blocks */
    corrections?: Array<{ label: string; text: string; original?: string; corrected?: string }>;
    /** Suggested task items for the Next Steps tab */
    nextStepTasks?: Array<{ text: string; taskType: string }>;
    /** Injected: open the create-task dialog. Keeps this renderer free of app.ts state. */
    onCreateTask?: (o: { title: string; excerpt: string; sourceModule: string; onCreated?: () => void }) => void;
  }
): HTMLElement {
  const wrap = document.createElement("div");
  wrap.className =
    "message message--assistant answer-card answer-card--" +
    // v2 cards carry no mode → a stable "v2" modifier class (legacy keeps factual/canonical/blended/recital).
    (card.mode ? card.mode.toLowerCase() : "v2") +
    (isError ? " message--error" : "");

  const bubble = document.createElement("div");
  bubble.className = "message-bubble answer-card-bubble";

  // ── RECITAL mode: editorial prose with serif rendering ──
  if (card.mode === "RECITAL" && card.recital?.verbatim) {
    const attr = document.createElement("div");
    attr.className = "recital-attr";
    attr.textContent = "From the Mobius founding essay:";
    bubble.appendChild(attr);

    // Clip to first 3 paragraphs; CTA expands inline on click.
    const RECITAL_PARA_LIMIT = 3;
    const stripSeparators = (t: string) => t.replace(/^[ \t]*[-*_]{3,}[ \t]*$/gm, "").trim();
    const fullText = stripSeparators(card.recital.verbatim);
    const allParas = fullText.split(/\n\n+/);
    const clipped = allParas.length > RECITAL_PARA_LIMIT;
    const proseText = clipped ? allParas.slice(0, RECITAL_PARA_LIMIT).join("\n\n") : fullText;

    const prose = document.createElement("div");
    prose.className = "recital-prose";
    prose.innerHTML = simpleMarkdownToHtml(proseText);
    bubble.appendChild(prose);

    if (clipped) {
      const readMore = document.createElement("button");
      readMore.type = "button";
      readMore.className = "recital-read-more";
      readMore.textContent = "Read the full essay ↗";
      let expanded = false;
      readMore.addEventListener("click", () => {
        expanded = !expanded;
        prose.innerHTML = simpleMarkdownToHtml(expanded ? fullText : proseText);
        readMore.textContent = expanded ? "Collapse ↑" : "Read the full essay ↗";
        // After transplant, children are moved into messageWrapEl — `wrap` is detached.
        // Traverse up from the live button to find the actual container.
        const container = readMore.closest('.answer-card--recital') ?? wrap;
        container.classList.toggle("recital-expanded", expanded);
      });
      bubble.appendChild(readMore);
    }

    if (opts?.showConfidenceBadge !== false && !opts?.suppressConfidenceForAdminQcFail) {
      bubble.appendChild(renderConfidenceBadge("approved_authoritative"));
    }
    wrap.appendChild(bubble);
    return wrap;
  }

  // Task #10: output_intent is an internal classification signal, NOT a user-facing card element
  // (Chat Master 2026-08-05). It is surfaced only as a Diagnostics telemetry row (see
  // formatOutputIntentLabel + the Diagnostics tab). Nothing renders on the card face.

  // Unified draft→answer (Ananth 2026-08-07). The integrator's final is the star; the react_draft
  // demotes to a collapsed "First pass" once the final lands. Compute the final's presence up front.
  const _displaySummary = (card.display_summary ?? "").trim();
  const _tldrSummary = (card.tldr_summary ?? "").trim();
  const _answerSections = card.sections ?? [];
  const hasAnswerEnvelope = _displaySummary.length > 0 || _answerSections.length > 0;
  const _reactDraft = (card.react_draft ?? "").trim();

  // Draft headline above the tabs — ONLY in the draft-only state (no final yet). Once the final
  // exists it's the star (rendered in .ac-answer-final in the panel) and the draft demotes to a
  // collapsed "First pass" below it, so no prominent draft line here.
  if (!hasAnswerEnvelope) {
    const direct = document.createElement("div");
    direct.className = "answer-card-direct";
    direct.innerHTML = simpleMarkdownToHtml(_reactDraft || card.direct_answer);
    bubble.appendChild(direct);
  }

  if (opts?.showConfidenceBadge !== false && !opts?.suppressConfidenceForAdminQcFail) {
    bubble.appendChild(
      renderConfidenceBadge((opts?.sourceConfidenceStrip ?? "").trim() || "informational_only")
    );
  }

  const metaRow = document.createElement("div");
  metaRow.className = "answer-card-meta-row";
  if (card.required_variables && card.required_variables.length > 0) {
    const dep = document.createElement("span");
    dep.className = "answer-card-depends";
    dep.textContent = "Depends on: " + card.required_variables.join(", ");
    metaRow.appendChild(dep);
  }
  if (!opts?.suppressFollowups && card.followups && card.followups.length > 0 && metaRow.childNodes.length > 0) {
    const sep = document.createElement("span");
    sep.className = "answer-card-meta-sep";
    sep.textContent = " · ";
    metaRow.appendChild(sep);
  }
  if (!opts?.suppressFollowups && card.followups && card.followups.length > 0) {
    const confirmLabel = document.createElement("span");
    confirmLabel.className = "answer-card-confirm-label";
    confirmLabel.textContent = "Confirm";
    metaRow.appendChild(confirmLabel);
    card.followups.slice(0, 4).forEach((f) => {
      const chip = document.createElement("button");
      chip.type = "button";
      chip.className = "answer-card-followup-chip";
      const questionText = f.question || f.reason || f.field || "";
      chip.textContent = questionText;
      chip.setAttribute("aria-label", questionText);
      if (opts?.onFollowupClick && questionText) {
        chip.addEventListener("click", () => opts!.onFollowupClick!(questionText));
      }
      metaRow.appendChild(chip);
    });
  }

  // Build the Summary panel content. Ananth ruling 2026-08-07: Summary is ReAct's synthesis
  // (react_draft, streamed into .ac-summary-prose on the live path / .answer-card-direct on
  // reload). The integrator's sections[] now live in the ANSWER tab (built above), NOT here —
  // Summary carries only meta (depends-on / confirm chips) + confidence note.
  const answerPanel = document.createElement("div");
  answerPanel.className = "ac-tab-panel ac-tab-panel--summary ac-tab-panel--active";
  answerPanel.setAttribute("role", "tabpanel");
  if (metaRow.childNodes.length > 0) answerPanel.appendChild(metaRow);

  if (card.confidence_note && card.confidence_note.trim()) {
    const note = document.createElement("div");
    note.className = "answer-card-confidence";
    note.textContent = card.confidence_note;
    answerPanel.appendChild(note);
  }

  // Unified draft→answer view (Ananth 2026-08-07). The integrator's final is the STAR: it renders at
  // the TOP of the default panel (.ac-answer-final), and the react_draft demotes to a collapsed
  // "First pass" right below it. No separate Answer tab. (_displaySummary/_answerSections/
  // hasAnswerEnvelope/_reactDraft computed above.)
  if (hasAnswerEnvelope) {
    const answerWrap = document.createElement("div");
    answerWrap.className = "ac-answer-final";
    // Mode badge — only CANONICAL (authoritative policy) / RECITAL (verbatim legal); FACTUAL/BLENDED
    // are the default path and signal nothing (Chat Master 2026-08-07).
    const modeLabel = (card.mode ?? "").trim().toUpperCase();
    if (modeLabel === "CANONICAL" || modeLabel === "RECITAL") {
      const lbl = document.createElement("div");
      lbl.className = "ac-answer-mode-label ac-answer-mode-label--" + modeLabel.toLowerCase();
      lbl.textContent = modeLabel;
      answerWrap.appendChild(lbl);
    }
    if (_tldrSummary) {
      const tldr = document.createElement("div");
      tldr.className = "ac-answer-tldr";
      tldr.innerHTML = simpleMarkdownToHtml(_tldrSummary);
      answerWrap.appendChild(tldr);
    }
    // Prose lead: display_summary (the fuller integrator prose) when present, else direct_answer
    // (so a sections-only turn with no display_summary — appeals-shaped — still leads with its
    // answer line rather than dropping it).
    const _lead = _displaySummary || (card.direct_answer ?? "").trim();
    if (_lead) {
      const body = document.createElement("div");
      body.className = "ac-answer-envelope-body";
      body.innerHTML = simpleMarkdownToHtml(_lead);
      answerWrap.appendChild(body);
    }
    _answerSections.slice(0, MAX_SECTIONS).forEach((sec) => answerWrap.appendChild(renderOneSection(sec)));
    // Final at the TOP of the panel (the star), above meta/confidence.
    answerPanel.insertBefore(answerWrap, answerPanel.firstChild);

    // The react work demotes to a collapsible "First pass" ABOVE the final (Ananth 2026-08-07:
    // "move the first pass up… slowly collapse that as the final answer starts to flow in"). When
    // the per-round reasoning ledger is present (card.reasoning_trace, ReAct Task #58), show the
    // PROGRESSION — rd-1 → rd-last — one step per round whose running_answer moved the answer; else
    // fall back to the single react_draft. Custom collapsible so the body max-height can ANIMATE.
    // FLAT shape (running_answer/learned are direct siblings of round — the ledger is flattened
    // server-side, no "enrichment" wrapper). Show each round's answer-so-far (running_answer) when
    // present, else its reasoning (learned) — so simple turns (a tool round with only `learned`,
    // no synthesis) still populate a progression rather than falling back to the single draft.
    const _rdRounds = (card.reasoning_trace ?? [])
      .map((r, i) => ({
        n: typeof r?.round === "number" ? r.round : i + 1,
        ans: (r?.running_answer ?? "").trim() || (r?.learned ?? "").trim(),
        isThought: !((r?.running_answer ?? "").trim()) && !!(r?.learned ?? "").trim(),
      }))
      .filter((r) => r.ans.length > 0);
    if (_reactDraft || _rdRounds.length > 0) {
      const fp = document.createElement("div");
      fp.className = "ac-first-pass";
      const sum = document.createElement("button");
      sum.type = "button";
      sum.className = "ac-first-pass-summary";
      sum.textContent = _rdRounds.length > 1 ? `First pass · ${_rdRounds.length} rounds` : "First pass";
      const fpBody = document.createElement("div");
      fpBody.className = "ac-first-pass-body";
      if (_rdRounds.length > 0) {
        _rdRounds.forEach((r) => {
          const step = document.createElement("div");
          step.className = "ac-rd-step";
          const lbl = document.createElement("span");
          lbl.className = "ac-rd-label";
          lbl.textContent = "rd-" + r.n;
          const ans = document.createElement("div");
          // Thought-only rounds (learned, no running_answer) render muted/italic to distinguish
          // reasoning from an actual answer-so-far.
          ans.className = "ac-rd-answer" + (r.isThought ? " ac-rd-thought" : "");
          ans.innerHTML = simpleMarkdownToHtml(r.ans);
          step.appendChild(lbl);
          step.appendChild(ans);
          fpBody.appendChild(step);
        });
      } else {
        fpBody.innerHTML = simpleMarkdownToHtml(_reactDraft);
      }
      // Toggle via MEASURED max-height (not a fixed cap) so open/close animates smoothly and in
      // proportion to the actual content height — no dead time collapsing empty space.
      sum.addEventListener("click", () => {
        const opening = !fp.classList.contains("ac-first-pass--open");
        fp.classList.toggle("ac-first-pass--open");
        fpBody.style.maxHeight = opening ? fpBody.scrollHeight + "px" : "0px";
      });
      fp.appendChild(sum);
      fp.appendChild(fpBody);
      answerPanel.insertBefore(fp, answerWrap);   // First pass ABOVE the final
    }
  }

  // Tab data — pull from opts
  const _corrections = opts?.corrections ?? [];
  const _nextStepQuestions = opts?.nextQuestions ?? [];
  const _nextStepTasks = opts?.nextStepTasks ?? [];

  const hasCitations = Array.isArray(card.citations) && card.citations.length > 0;
  const hasTasks = _nextStepTasks.length > 0;   // corrections render INLINE now (no tab)
  // Tab bar shows for SECONDARY surfaces only — the answer is inline in the default panel, and
  // corrections are inline redlines (no tab), so only Sources + Tasks drive the bar (Ananth 2026-08-07).
  const showTabBar = hasCitations || hasTasks;

  // Citations panel
  const citationsPanel = document.createElement("div");
  citationsPanel.className = "ac-tab-panel ac-tab-panel--citations";
  citationsPanel.setAttribute("role", "tabpanel");
  citationsPanel.setAttribute("hidden", "");
  if (hasCitations) {
    const citList = document.createElement("div");
    citList.className = "ac-citations-list";
    (card.citations ?? []).forEach((cit) => {
      const row = document.createElement("div");
      row.className = "ac-citation-row";
      const title = document.createElement("div");
      title.className = "ac-citation-title";
      title.textContent = cit.doc_title || "";
      const meta = document.createElement("div");
      meta.className = "ac-citation-meta";
      if (cit.locator) meta.textContent = cit.locator;
      const snippet = document.createElement("div");
      snippet.className = "ac-citation-snippet";
      snippet.textContent = (cit as Record<string, string>).snippet || "";
      row.appendChild(title);
      if (cit.locator) row.appendChild(meta);
      if ((cit as Record<string, string>).snippet) row.appendChild(snippet);
      citList.appendChild(row);
    });
    citationsPanel.appendChild(citList);
  }

  // Corrections are now INLINE redlines in the answer prose (Ananth 2026-08-07) — no Corrections
  // tab, panel, or callout. The primary source is card.correction ({original, corrected} from the
  // integrator); envelope correction blocks (opts.corrections) are folded in too. For the non-
  // streaming/reload render, apply here; the streaming path applies post-stream (app.ts). Only
  // lands where the corrected text matches verbatim.
  const _redlineCorrs = [
    ...(card.correction ? [card.correction] : []),
    ..._corrections.filter((c) => c.original && c.corrected),
  ];
  if (_redlineCorrs.length > 0) {
    const _finalEl = answerPanel.querySelector(".ac-answer-final") as HTMLElement | null;
    if (_finalEl) applyInlineCorrections(_finalEl, _redlineCorrs);
  }

  // Follow-up panel — suggested questions the user can ask next
  const nextStepsPanel = document.createElement("div");
  nextStepsPanel.className = "ac-tab-panel ac-tab-panel--next-steps";
  nextStepsPanel.setAttribute("role", "tabpanel");
  nextStepsPanel.setAttribute("hidden", "");
  if (_nextStepQuestions.length > 0) {
    const nsWrap = document.createElement("div");
    nsWrap.className = "ac-next-steps";
    _nextStepQuestions.forEach((q) => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "ac-next-step-question";
      btn.textContent = q.text;
      if (opts?.onFollowupClick && q.text) {
        btn.addEventListener("click", () => opts!.onFollowupClick!(q.text));
      }
      nsWrap.appendChild(btn);
    });
    nextStepsPanel.appendChild(nsWrap);
  }

  // Tasks panel — actionable items the user can assign to themselves
  const tasksPanel = document.createElement("div");
  tasksPanel.className = "ac-tab-panel ac-tab-panel--tasks";
  tasksPanel.setAttribute("role", "tabpanel");
  tasksPanel.setAttribute("hidden", "");
  if (_nextStepTasks.length > 0) {
    const tWrap = document.createElement("div");
    tWrap.className = "ac-tasks-list";
    _nextStepTasks.forEach(({ text, taskType }) => {
      const row = document.createElement("div");
      row.className = "ac-next-step-task-row";
      const taskText = document.createElement("span");
      taskText.className = "ac-next-step-task-text";
      taskText.textContent = text;
      const createBtn = document.createElement("button");
      createBtn.type = "button";
      createBtn.className = "ac-next-step-create-btn";
      createBtn.setAttribute("data-task-type", taskType || "general");
      createBtn.setAttribute("data-task-text", text);
      createBtn.textContent = "+ Add to my tasks";
      createBtn.addEventListener("click", () => {
        opts?.onCreateTask?.({
          title: text.slice(0, 60),
          excerpt: text,
          sourceModule: "next_steps",
          onCreated: () => {
            createBtn.textContent = "Added ✓";
            createBtn.disabled = true;
            createBtn.classList.add("ac-next-step-create-btn--done");
          },
        });
      });
      row.appendChild(taskText);
      row.appendChild(createBtn);
      tWrap.appendChild(row);
    });
    tasksPanel.appendChild(tWrap);
  }

  // Tab bar — rendered when any of the non-Answer tabs has content
  if (showTabBar) {
    const tabBar = document.createElement("div");
    tabBar.className = "ac-tab-bar";
    tabBar.setAttribute("role", "tablist");
    // count=undefined → Answer tab (no badge, always visible)
    // count=0 → data-empty="1" (CSS hides it)
    // count>0 → count badge shown
    // panelKey = CSS suffix for .ac-tab-panel--{panelKey}; querySelector so tab buttons
    // survive panel replaceChild in the completed handler (closure reference would break).
    const mkTab = (label: string, panelKey: string, count: number | undefined, active: boolean) => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "ac-tab" + (active ? " ac-tab--active" : "");
      btn.setAttribute("role", "tab");
      btn.setAttribute("aria-selected", String(active));
      btn.setAttribute("data-panel", panelKey);
      if (count !== undefined && count === 0) btn.setAttribute("data-empty", "1");
      if (count !== undefined && count > 0) {
        btn.appendChild(document.createTextNode(label + " "));
        const badge = document.createElement("span");
        badge.className = "ac-tab-count";
        badge.textContent = String(count);
        btn.appendChild(badge);
      } else {
        btn.textContent = label;
      }
      btn.addEventListener("click", () => {
        const liveBubble = btn.closest(".answer-card-bubble") ?? bubble;
        tabBar.querySelectorAll(".ac-tab").forEach((t) => {
          t.classList.remove("ac-tab--active");
          t.setAttribute("aria-selected", "false");
        });
        liveBubble.querySelectorAll(".ac-tab-panel").forEach((p) => {
          (p as HTMLElement).hidden = true;
          p.classList.remove("ac-tab-panel--active");
        });
        btn.classList.add("ac-tab--active");
        btn.setAttribute("aria-selected", "true");
        const targetPanel = liveBubble.querySelector(`.ac-tab-panel--${panelKey}`) as HTMLElement | null;
        if (targetPanel) { targetPanel.hidden = false; targetPanel.classList.add("ac-tab-panel--active"); }
      });
      return btn;
    };
    // Tab set + order come from the render model (single source of truth, §1.4). Iterating
    // TAB_ORDER keeps the bar consistent with the field→tab map; the Diagnostics slot (tab 6)
    // is reserved here and injected by the admin/QA path when present.
    const TAB_DOM: Partial<Record<TabKey, { label: string; panelKey: string; count: number | undefined }>> = {
      // Unified draft→answer view (Ananth 2026-08-07): the default panel holds the whole flow — the
      // draft streams in, then the integrator's final flows in below it (.ac-answer-final). Labeled
      // "Answer" since the final is the star; the panel key stays "summary".
      "summary": { label: "Answer", panelKey: "summary", count: undefined },
      // Answer tab — only listed when display_summary exists (count=undefined → no badge, always
      // visible like Draft). Omitted otherwise so the bar has no empty Answer button.
      // No "answer" tab — the answer is inline in the default panel now (Ananth 2026-08-07).
      // Chat Master ruling (b) 2026-08-06: the Citations tab is repurposed into a consolidated
      // "Sources" tab — reference chips (here) + source excerpts (snippets, here) + a collapsible
      // narrative_full_redacted section injected post-render (app.ts completed handler).
      "citations": { label: "Sources", panelKey: "citations", count: (card.citations ?? []).length },
      // Corrections tab removed (Ananth 2026-08-07) — corrections are inline redlines in the answer.
      // Follow-up tab dropped (Ananth 2026-08-07): follow-up questions render as suggestion chips
      // below the bubble, so a tab duplicated them. Tasks tab is being migrated to the feedback
      // panel (a badge + accept/reject modal) in a follow-up build; kept here until that lands.
      "tasks": { label: "Tasks", panelKey: "tasks", count: _nextStepTasks.length },
    };
    let firstTab = true;
    for (const tab of TAB_ORDER) {
      const dom = TAB_DOM[tab];
      if (!dom) continue; // diagnostics: no static DOM here (admin path injects into its TAB_ORDER slot)
      tabBar.appendChild(mkTab(dom.label, dom.panelKey, dom.count, firstTab));
      firstTab = false;
    }
    bubble.appendChild(tabBar);
  }

  bubble.appendChild(answerPanel);
  bubble.appendChild(citationsPanel);
  bubble.appendChild(nextStepsPanel);
  bubble.appendChild(tasksPanel);

  // Suggested action chips — e.g. "Open Appeals Agent ↗" for denial/appeal queries.
  if (card.suggested_actions && card.suggested_actions.length > 0) {
    const actionsWrap = document.createElement("div");
    actionsWrap.className = "answer-card-actions";
    card.suggested_actions.forEach((action) => {
      if (action.type === "external_link" && action.url && action.label) {
        const a = document.createElement("a");
        a.href = action.url;
        a.target = "_blank";
        a.rel = "noopener noreferrer";
        a.className = "answer-card-action-chip";
        a.textContent = (action.icon ? action.icon + " " : "") + action.label + " ↗";
        a.setAttribute("aria-label", action.label + " (opens in new tab)");
        actionsWrap.appendChild(a);
      }
    });
    if (actionsWrap.childNodes.length > 0) wrap.appendChild(actionsWrap);
  }

  if (opts?.qcAudit) bubble.appendChild(renderQcAuditBadge(opts.qcAudit));

  wrap.appendChild(bubble);
  return wrap;
}
