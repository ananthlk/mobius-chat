// render/bubble — the chat-bubble surface renderer. Renders the AnswerCard the
// bubble-backend produces. Pure DOM: shared primitives come from ui-helpers, the parse/
// visibility model from answer-card, the tab/slot model from card-render-model. Its one
// app-state dependency (opening the task dialog) is INJECTED via opts.onCreateTask — no
// reach-back into app.ts. See docs/bubble-backend-contract.md (the FE half of the pair).

import type { AnswerCard, AnswerCardSection } from "../answer-card";
import { splitSectionsByVisibility } from "../answer-card";
import { TAB_ORDER, type TabKey } from "../card-render-model";
import {
  simpleMarkdownToHtml, renderConfidenceBadge, renderQcAuditBadge,
  type QcAuditInfo, type FollowupLineNormalized,
} from "../ui-helpers";

const MAX_BULLETS_PER_SECTION = 4;

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

  // Default: bullets
  const bullets = (sec.bullets ?? []).slice(0, MAX_BULLETS_PER_SECTION);
  bullets.forEach((b) => {
    const li = document.createElement("div");
    li.className = "answer-card-bullet";
    li.textContent = b;
    body.appendChild(li);
  });
  if (bullets.length < (sec.bullets?.length ?? 0)) {
    const more = document.createElement("div");
    more.className = "answer-card-more";
    more.textContent = "Show more";
    more.setAttribute("aria-label", "Show more bullets");
    body.appendChild(more);
  }
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
    corrections?: Array<{ label: string; text: string }>;
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

  const direct = document.createElement("div");
  direct.className = "answer-card-direct";
  direct.innerHTML = simpleMarkdownToHtml(card.direct_answer);
  bubble.appendChild(direct);

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

  // Build the Summary panel content (sections + meta + confidence note)
  const answerPanel = document.createElement("div");
  answerPanel.className = "ac-tab-panel ac-tab-panel--summary ac-tab-panel--active";
  answerPanel.setAttribute("role", "tabpanel");
  if (metaRow.childNodes.length > 0) answerPanel.appendChild(metaRow);

  const { visible, hidden } = splitSectionsByVisibility(card.sections ?? [], card.mode);
  visible.forEach((sec) => answerPanel.appendChild(renderOneSection(sec)));

  if (hidden.length > 0) {
    const detailsBlock = document.createElement("div");
    detailsBlock.className = "answer-card-details";
    detailsBlock.setAttribute("aria-hidden", "true");
    hidden.forEach((sec) => detailsBlock.appendChild(renderOneSection(sec)));
    answerPanel.appendChild(detailsBlock);

    const toggleBtn = document.createElement("button");
    toggleBtn.type = "button";
    toggleBtn.className = "answer-card-show-details";
    toggleBtn.textContent = "Show details";
    toggleBtn.setAttribute("aria-label", "Show details");
    toggleBtn.setAttribute("aria-expanded", "false");
    toggleBtn.addEventListener("click", () => {
      const expanded = detailsBlock.classList.toggle("answer-card-details--expanded");
      detailsBlock.setAttribute("aria-hidden", expanded ? "false" : "true");
      toggleBtn.setAttribute("aria-expanded", String(expanded));
      toggleBtn.textContent = expanded ? "Hide details" : "Show details";
      toggleBtn.setAttribute("aria-label", expanded ? "Hide details" : "Show details");
    });
    answerPanel.appendChild(toggleBtn);
  }

  if (card.confidence_note && card.confidence_note.trim()) {
    const note = document.createElement("div");
    note.className = "answer-card-confidence";
    note.textContent = card.confidence_note;
    answerPanel.appendChild(note);
  }

  // Tab data — pull from opts
  const _corrections = opts?.corrections ?? [];
  const _nextStepQuestions = opts?.nextQuestions ?? [];
  const _nextStepTasks = opts?.nextStepTasks ?? [];

  const hasCitations = Array.isArray(card.citations) && card.citations.length > 0;
  const hasCorrections = _corrections.length > 0;
  const hasNextSteps = _nextStepQuestions.length > 0;
  const hasTasks = _nextStepTasks.length > 0;
  const showTabBar = hasCitations || hasCorrections || hasNextSteps || hasTasks;

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

  // Corrections panel
  const correctionsPanel = document.createElement("div");
  correctionsPanel.className = "ac-tab-panel ac-tab-panel--corrections";
  correctionsPanel.setAttribute("role", "tabpanel");
  correctionsPanel.setAttribute("hidden", "");
  if (hasCorrections) {
    const corrList = document.createElement("div");
    corrList.className = "ac-correction-list";
    _corrections.forEach(({ label, text }) => {
      const row = document.createElement("div");
      row.className = "ac-correction-row";
      const lbl = document.createElement("div");
      lbl.className = "ac-correction-label";
      lbl.textContent = label;
      row.appendChild(lbl);
      row.appendChild(document.createTextNode(text));
      corrList.appendChild(row);
    });
    correctionsPanel.appendChild(corrList);
    // Inline callout in the Answer panel — one-sentence summary pointing to the tab
    const corrCallout = document.createElement("div");
    corrCallout.className = "ac-answer-correction-callout";
    const corrIcon = document.createElement("span");
    corrIcon.className = "ac-answer-correction-icon";
    corrIcon.textContent = "⚠";
    corrIcon.setAttribute("aria-hidden", "true");
    const corrBody = document.createElement("div");
    const corrLbl = document.createElement("div");
    corrLbl.className = "ac-answer-correction-callout-label";
    corrLbl.textContent = _corrections[0].label;
    const corrP = document.createElement("p");
    corrP.className = "ac-answer-correction-callout-text";
    corrP.appendChild(document.createTextNode(
      _corrections.length === 1
        ? _corrections[0].text.slice(0, 120) + (_corrections[0].text.length > 120 ? "…" : "") + " — "
        : `${_corrections.length} corrections noted — `
    ));
    const corrTabLink = document.createElement("button");
    corrTabLink.type = "button";
    corrTabLink.className = "ac-correction-tab-link";
    corrTabLink.textContent = "see Corrections tab";
    corrTabLink.addEventListener("click", () => {
      // Traverse live DOM — bubble may be the transplant target
      const liveBubble = corrTabLink.closest(".answer-card-bubble") ?? bubble;
      (liveBubble.querySelector('[data-panel="corrections"]') as HTMLElement | null)?.click();
    });
    corrP.appendChild(corrTabLink);
    corrP.appendChild(document.createTextNode(" for details."));
    corrBody.appendChild(corrLbl);
    corrBody.appendChild(corrP);
    corrCallout.appendChild(corrIcon);
    corrCallout.appendChild(corrBody);
    answerPanel.appendChild(corrCallout);
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
      "summary": { label: "Summary", panelKey: "summary", count: undefined },
      "citations": { label: "Citations", panelKey: "citations", count: (card.citations ?? []).length },
      "corrections": { label: "Corrections", panelKey: "corrections", count: _corrections.length },
      "follow-up": { label: "Follow-up", panelKey: "next-steps", count: _nextStepQuestions.length },
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
  bubble.appendChild(correctionsPanel);
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
