/**
 * A/B harness page — comparisons a HUMAN judges by reading renderings side by side.
 *
 * PRODUCTION ROUTES; THE HARNESS FORKS. This page renders 1..N arms that all ran on
 * identical input — nobody was served. The column count is a function of the DATA
 * (experiment.arms.length), never a flag: one arm = a single-column baseline capture,
 * two = the A/B pair, N = N-way. Same page, no mode switch.
 *
 * §0 (the one load-bearing constraint): each box renders its arm's answer through the
 * PRODUCTION renderer — the exact `renderEnvelope` the live bubble runs — never a
 * re-implementation. If an answer renders differently here than in the product, the
 * comparison is measuring this page, not the orchestrator. So box content is
 * renderEnvelope(env.blocks).answerBody + the peeled sources, and nothing else.
 *
 * The near-production view is the RESTING STATE: answers expanded, everything
 * diagnostic (trace / divergences / terms) collapsed. A new reader sees what a user
 * would see; a power reader expands the internals. Every expand reads from data already
 * in hand — no on-expand fetch, because the expanded view is the one someone opens when
 * they are looking hardest, and it must not fail differently from the collapsed one.
 *
 * Built to docs/AB_HARNESS_FE_RENDER_CONTRACT.md against app/api/ab_harness.py.
 */
import { renderEnvelope, renderSourcesList, type EnvBlock } from "./render/bubble";

const API = window.location.origin;
const LS_KEY = "ab:expandAll";

// ── types (the endpoint's shape — see get_comparison / _trace) ────────────────
interface ArmMeta { id: string; label: string; }
interface TraceRow {
  round_n: number; posture: string | null; directive: string | null;
  rationale?: string | null; tool_called?: string | null;
  round_duration_s?: number | null; gaps_opened?: string[]; gaps_closed?: string[];
  v1_directive?: string | null; v1_reason?: string | null; v1_maps_to?: string | null;
  verdict?: "agree" | "diverge" | "unmapped" | null; overran?: boolean;
}
interface ArmData {
  answer_envelope: { version?: number; blocks?: EnvBlock[] } | null;
  envelope_captured_at?: string | null;
  decision_trace: TraceRow[];
  delivered: { latency_ms: number | null; cost_usd: number | null; exit_mode: string | null; rounds: number | null };
  promised?: { latency_ms: number | null; promise_version?: string | null; tier?: string | null };
  kept: boolean | null;
  status?: string; error?: string | null;
}
export interface Comparison {
  run_id: string;
  question: { id: string; q: string; shape?: string | null; mode?: string | null };
  harness: boolean;
  experiment: { arms: ArmMeta[]; held_constant: string[]; varied: string[] };
  arms: Record<string, ArmData>;
}
interface RunSummary {
  run_id: string; set_id: string; created: string; harness: boolean;
  questions: { id: string; q: string; shape?: string | null; status: Record<string, string> }[];
}

// ── tiny DOM helpers ──────────────────────────────────────────────────────────
function el(tag: string, cls?: string, text?: string): HTMLElement {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}
/** null/undefined/"" → em-dash. NEVER "false" or "0": unset is not false, and 0 is a
 *  measured value. This is the defect family the program keeps removing. */
function dash(v: unknown): string {
  if (v === null || v === undefined || v === "") return "—";
  return String(v);
}
function ms(v: number | null | undefined): string {
  return v == null ? "—" : `${(v / 1000).toFixed(1)}s`;
}

/** A collapsible section — pure show/hide over data already in hand, no fetch on expand.
 *  Keyboard-focusable via <details>/<summary>; honors the page-level expand-all. */
function collapsible(summaryText: string, body: HTMLElement, open: boolean): HTMLDetailsElement {
  const d = document.createElement("details");
  d.className = "ab-collapsible";
  d.open = open;
  const s = document.createElement("summary");
  s.className = "ab-collapsible-summary";
  s.textContent = summaryText;
  d.appendChild(s);
  const wrap = el("div", "ab-collapsible-body");
  wrap.appendChild(body);
  d.appendChild(wrap);
  return d;
}

// ── box 1..N: the arm's answer via the PRODUCTION renderer ────────────────────
function renderArmBox(arm: ArmMeta, data: ArmData, expandAll: boolean): HTMLElement {
  const box = el("section", "ab-box");
  box.setAttribute("aria-label", `arm ${arm.label}`);

  const head = el("header", "ab-box-head");
  head.appendChild(el("span", "ab-box-arm", arm.label));
  const st = el("span", "ab-box-status", data.status === "captured" ? "captured" : dash(data.status));
  st.classList.add(`ab-status--${data.status || "unknown"}`);
  head.appendChild(st);
  box.appendChild(head);

  // The answer — the near-production view. Answer-mode when an envelope is present;
  // trace-mode when it is null (v2 shadow today). The switch is on the DATA.
  const env = data.answer_envelope;
  if (env && Array.isArray(env.blocks) && env.blocks.length) {
    // renderExtraBlock reproduces the chrome blocks the production completed-turn path
    // renders (tool_attribution → .envelope-tool-chip, same class as app.ts:8460), so the
    // near-production view isn't missing what a user sees. Everything else is renderEnvelope's.
    const { answerBody, sources } = renderEnvelope(env.blocks, {
      renderExtraBlock: (b: EnvBlock): HTMLElement | null => {
        if (b.type === "tool_attribution") {
          const chip = el("div", "envelope-tool-chip", (b.label as string) || "Research");
          chip.setAttribute("data-icon", (b.icon as string) || "search");
          return chip;
        }
        return null;
      },
    });
    const answer = el("div", "ab-answer");
    answer.appendChild(answerBody);
    if (sources && Array.isArray((sources as { refs?: unknown[] }).refs)) {
      const refs = ((sources as unknown as { refs: Array<Record<string, unknown>> }).refs).map((r) => ({
        doc_title: r.title as string | undefined,
        page_number: (r.page as number | null | undefined) ?? null,
        snippet: r.snippet as string | undefined,
        document_id: r.document_id as string | undefined,
      }));
      const sourcesEl = renderSourcesList(refs);
      if (sourcesEl) answer.appendChild(sourcesEl);
    }
    box.appendChild(answer);
  } else if (data.error) {
    box.appendChild(el("div", "ab-answer ab-answer--error", `Arm errored: ${data.error}`));
  } else if (data.decision_trace.length) {
    // Trace-mode: this arm has no answer (shadow). The trace is its CONTENT, not a
    // collapsible extra — it is what the arm has, so it shows.
    const note = el("div", "ab-trace-mode-note", "No answer emitted — showing the decision trace (shadow arm).");
    box.appendChild(note);
    box.appendChild(renderTrace(data.decision_trace, /*inline*/ true));
  } else {
    box.appendChild(el("div", "ab-answer ab-answer--empty", "— not captured —"));
  }

  // In answer-mode, the round-by-round trace drops to a collapsed row under the answer.
  if (env && data.decision_trace.length) {
    box.appendChild(collapsible(`Round-by-round trace · ${data.decision_trace.length} rounds`,
      renderTrace(data.decision_trace, false), expandAll));
  }
  return box;
}

// ── the decision trace (round-by-round) ───────────────────────────────────────
function renderTrace(rows: TraceRow[], inline: boolean): HTMLElement {
  const wrap = el("div", inline ? "ab-trace ab-trace--inline" : "ab-trace");
  for (const r of rows) {
    const row = el("div", "ab-trace-row");
    row.appendChild(el("span", "ab-trace-round", `R${r.round_n}`));
    const label = r.posture || r.directive || "—";
    row.appendChild(el("span", "ab-trace-posture", label));
    if (r.tool_called) row.appendChild(el("span", "ab-trace-tool", r.tool_called));
    if (r.round_duration_s != null) row.appendChild(el("span", "ab-trace-dur", `${r.round_duration_s.toFixed(1)}s`));
    if (r.rationale) row.appendChild(el("span", "ab-trace-why", r.rationale));
    wrap.appendChild(row);
  }
  return wrap;
}

// ── divergences: a FILTERED VIEW of the shadow arm's trace, keyed on stored verdict ──
// Multi-arm only. The verdict is READ, never recomputed by diffing postures FE-side —
// that would be a second mapping that misclassifies `extend` (which resolves by reason,
// not name). `unmapped` ≠ `diverge`: it is a finding about the mapping, not evidence
// against the arm, so it renders distinctly and stays out of any "where it differs" frame.
function findShadowArm(cmp: Comparison): ArmData | null {
  for (const id of Object.keys(cmp.arms)) {
    if (cmp.arms[id].decision_trace.some((r) => r.verdict != null)) return cmp.arms[id];
  }
  return null;
}
function renderDivergences(shadow: ArmData, expandAll: boolean): HTMLElement | null {
  const rows = shadow.decision_trace.filter((r) => r.verdict && r.verdict !== "agree");
  if (!rows.length) {
    const nored = el("div", "ab-diverge-none", "No divergences — the arms agreed on every mapped round.");
    return collapsible("Divergences · 0", nored, false);
  }
  const body = el("div", "ab-diverge");
  let diverge = 0, unmapped = 0;
  for (const r of rows) {
    const row = el("div", `ab-diverge-row ab-diverge--${r.verdict}`);
    row.appendChild(el("span", "ab-diverge-round", `R${r.round_n}`));
    if (r.verdict === "unmapped") {
      unmapped++;
      row.appendChild(el("span", "ab-diverge-tag", "mapping gap"));
      row.appendChild(el("span", "ab-diverge-detail",
        `v1 "${dash(r.v1_directive)}" — not covered by the mapping`));
    } else {
      diverge++;
      row.appendChild(el("span", "ab-diverge-tag ab-diverge-tag--real", "diverge"));
      row.appendChild(el("span", "ab-diverge-detail",
        `v1 "${dash(r.v1_maps_to || r.v1_directive)}" vs v2 "${dash(r.posture || r.directive)}"`
        + (r.rationale ? ` — ${r.rationale}` : "")));
    }
    body.appendChild(row);
  }
  // Count shown beside the rate, and `unmapped` excluded from any denominator (it does
  // not answer the same question as diverge). We never show a rate; we show the counts.
  const summary = `Divergences · ${diverge} diverge` + (unmapped ? ` · ${unmapped} mapping-gap (not a disagreement)` : "");
  return collapsible(summary, body, expandAll);
}

// ── terms: diagnostic, subordinate. Never a judgement, never aggregated. ──────
function renderTerms(cmp: Comparison, arms: ArmMeta[], expandAll: boolean): HTMLElement {
  const table = el("table", "ab-terms-table") as HTMLTableElement;
  const head = el("tr");
  head.appendChild(el("th", "", ""));
  for (const a of arms) head.appendChild(el("th", "", a.label));
  table.appendChild(head);
  const rowDefs: Array<[string, (d: ArmData) => string]> = [
    ["latency", (d) => ms(d.delivered.latency_ms)],
    ["promised", (d) => ms(d.promised?.latency_ms)],
    ["kept", (d) => (d.kept == null ? "—" : d.kept ? "✓" : "missed")],
    ["exit", (d) => dash(d.delivered.exit_mode)],
    ["rounds", (d) => dash(d.delivered.rounds)],
    ["cost", (d) => (d.delivered.cost_usd == null ? "—" : String(d.delivered.cost_usd))],
  ];
  for (const [label, fn] of rowDefs) {
    const tr = el("tr");
    tr.appendChild(el("td", "ab-terms-label", label));
    for (const a of arms) tr.appendChild(el("td", "ab-terms-val", fn(cmp.arms[a.id])));
    table.appendChild(tr);
  }
  const note = el("div", "ab-terms-note",
    "Diagnostic only — not a judgement. 20 comparisons are zero data points for any exit criterion and twenty for human reading.");
  const body = el("div");
  body.appendChild(table);
  body.appendChild(note);
  return collapsible("Terms · latency · cost · exit · rounds · kept", body, expandAll);
}

// ── the whole comparison ──────────────────────────────────────────────────────
export function renderComparison(cmp: Comparison, root: HTMLElement): void {
  root.textContent = "";
  const expandAll = readExpandAll();
  const arms = cmp.experiment.arms;

  // Banner — shows on ANY harness run, single-arm capture included.
  const banner = el("div", "ab-banner",
    "A/B HARNESS — every arm ran on identical input. Nobody was served. Production routes to exactly one orchestrator.");
  root.appendChild(banner);

  // Experiment header — Rule 5 as data, ON the page. Held vs varied, always visible.
  const header = el("header", "ab-exp-header");
  const held = el("div", "ab-exp-held");
  held.appendChild(el("span", "ab-exp-key", "Held"));
  held.appendChild(el("span", "ab-exp-val", cmp.experiment.held_constant.join(" · ") || "—"));
  const varied = el("div", "ab-exp-varied");
  varied.appendChild(el("span", "ab-exp-key", "Varied"));
  varied.appendChild(el("span", "ab-exp-val",
    cmp.experiment.varied.length ? cmp.experiment.varied.join(" · ") : "nothing — single-arm capture"));
  header.appendChild(held);
  header.appendChild(varied);

  const toggle = el("button", "ab-expand-all", expandAll ? "Collapse all detail" : "Expand all detail") as HTMLButtonElement;
  toggle.addEventListener("click", () => { writeExpandAll(!expandAll); renderComparison(cmp, root); });
  header.appendChild(toggle);
  root.appendChild(header);

  root.appendChild(el("h1", "ab-question", cmp.question.q));
  const meta = el("div", "ab-question-meta");
  meta.appendChild(el("span", "ab-chip", cmp.question.id));
  if (cmp.question.shape) meta.appendChild(el("span", "ab-chip", cmp.question.shape));
  if (cmp.question.mode) meta.appendChild(el("span", "ab-chip", cmp.question.mode));
  root.appendChild(meta);

  // Columns = arms.length. One box per arm. Single-column is NOT a different path —
  // it is this same grid with one child.
  const grid = el("div", "ab-grid");
  grid.style.setProperty("--ab-cols", String(arms.length));
  for (const a of arms) grid.appendChild(renderArmBox(a, cmp.arms[a.id], expandAll));
  root.appendChild(grid);

  // Divergences — multi-arm only; absent (not empty) when there is no shadow arm.
  const shadow = arms.length > 1 ? findShadowArm(cmp) : null;
  if (shadow) {
    const div = renderDivergences(shadow, expandAll);
    if (div) root.appendChild(div);
  }

  root.appendChild(renderTerms(cmp, arms, expandAll));
}

// ── question navigator ────────────────────────────────────────────────────────
function renderNav(run: RunSummary, current: string, onPick: (qid: string) => void): HTMLElement {
  const nav = el("nav", "ab-nav");
  nav.appendChild(el("div", "ab-nav-run", run.run_id));
  const list = el("div", "ab-nav-list");
  for (const q of run.questions) {
    const b = el("button", "ab-nav-q" + (q.id === current ? " ab-nav-q--active" : ""), q.id) as HTMLButtonElement;
    b.title = q.q;
    b.addEventListener("click", () => onPick(q.id));
    list.appendChild(b);
  }
  nav.appendChild(list);
  return nav;
}

// ── expand-all persistence (per-viewer convenience; guarded) ──────────────────
function readExpandAll(): boolean {
  try { return localStorage.getItem(LS_KEY) === "1"; } catch { return false; }
}
function writeExpandAll(v: boolean): void {
  try { localStorage.setItem(LS_KEY, v ? "1" : "0"); } catch { /* private mode: fall through */ }
}

// ── boot ──────────────────────────────────────────────────────────────────────
async function main(): Promise<void> {
  const app = document.getElementById("ab-app");
  if (!app) return;
  const params = new URLSearchParams(location.search);
  const runId = params.get("run");
  let qid = params.get("q") || "";

  if (!runId) {
    app.appendChild(el("div", "ab-empty",
      "No run selected. Open with ?run=<run_id> (e.g. ?run=ab-3fb1e4a6a3&q=q01)."));
    return;
  }

  let run: RunSummary;
  try {
    const r = await fetch(`${API}/ab/runs/${encodeURIComponent(runId)}`);
    if (!r.ok) throw new Error(`run ${runId}: HTTP ${r.status}`);
    run = await r.json();
  } catch (e) {
    app.appendChild(el("div", "ab-empty", `Could not load run: ${(e as Error).message}`));
    return;
  }
  if (!qid && run.questions.length) qid = run.questions[0].id;

  const navHost = el("div", "ab-nav-host");
  const body = el("div", "ab-body");
  app.appendChild(navHost);
  app.appendChild(body);

  async function show(q: string): Promise<void> {
    qid = q;
    const url = new URL(location.href);
    url.searchParams.set("run", runId!);
    url.searchParams.set("q", q);
    history.replaceState(null, "", url.toString());
    navHost.textContent = "";
    navHost.appendChild(renderNav(run, q, (nq) => { void show(nq); }));
    body.textContent = "";
    body.appendChild(el("div", "ab-loading", "Loading comparison…"));
    try {
      const r = await fetch(`${API}/ab/runs/${encodeURIComponent(runId!)}/q/${encodeURIComponent(q)}`);
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const cmp: Comparison = await r.json();
      renderComparison(cmp, body);
    } catch (e) {
      body.textContent = "";
      body.appendChild(el("div", "ab-empty", `Could not load ${q}: ${(e as Error).message}`));
    }
  }
  void show(qid);
}

void main();
