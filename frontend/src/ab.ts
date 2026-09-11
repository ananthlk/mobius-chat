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
import { renderEnvelope, renderSourcesList, _inlineMd, type EnvBlock } from "./render/bubble";

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
  // v2 substitution telemetry (Governor). prompt_mismatch is a DECLARED known-wrong, not an
  // error: v2 chose a posture v1 has no prompt for, so v1 answered normally and the person saw
  // v1's answer. Surfaced visibly on the arm (below), never as a silent clean win.
  prompt_mismatch?: string | boolean | null;
  applied_directive?: string | null; v2_applied?: string | null;
}
interface ArmData {
  answer_envelope: { version?: number; blocks?: EnvBlock[] } | null;
  envelope_captured_at?: string | null;
  decision_trace: TraceRow[];
  delivered: { latency_ms: number | null; cost_cents: number | null; exit_mode: string | null; rounds: number | null };
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

  // prompt_mismatch — surfaced HERE, above the answer, so a substituted posture v1 couldn't
  // answer never reads as a clean win. It is a declared known-wrong, not an error tint.
  const mismatched = data.decision_trace.filter((r) => r.prompt_mismatch);
  if (mismatched.length) {
    const directives = [...new Set(mismatched.map(
      (r) => (typeof r.prompt_mismatch === "string" ? r.prompt_mismatch : null)
             || r.applied_directive || r.posture || "a posture"))].join(", ");
    box.appendChild(el("div", "ab-mismatch",
      `⚠ v2 chose ${directives} — v1 has no prompt for it, so the person saw v1's answer, not this. `
      + "Not a like-for-like comparison on those rounds."));
  }

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
    // cost_cents is CENTS (every cost in the system is — promised_cost_c, delivered_cost_c,
    // Budget.remaining_c). Rendered in $ so the unit is on the value, never a bare number
    // that reads as dollars while holding cents. null → "—", never 0 (0 is a measured cost).
    ["cost", (d) => (d.delivered.cost_cents == null ? "—" : `$${(d.delivered.cost_cents / 100).toFixed(3)}`)],
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

  // Live fork — run every arm on this question at the same instant and watch both fill in.
  const forkBtn = el("button", "ab-fork-btn", "▶ Run live fork") as HTMLButtonElement;
  forkBtn.title = "Run every arm on this question simultaneously and stream both answers in";
  forkBtn.addEventListener("click", () => { forkBtn.disabled = true; void startFork(cmp, root); });
  header.appendChild(forkBtn);
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

// ── live simultaneous fork ────────────────────────────────────────────────────
// Ananth: "simultaneous and onscreen rendering is important — that's how I would know
// what works and how." Sequential arms 20 min apart can't separate "the governor
// decided better" from "the model rolled differently"; running together holds corpus,
// manifest, roster, cache, load and wall-clock constant. It does NOT hold the model's
// sampling — one fork is ONE sample of two, and the page must never read as a verdict.
interface ForkResponse {
  run_id: string; question_id: string; question: string; mode: string;
  launched: Record<string, string>;      // {arm: correlation_id}
  errors?: Record<string, string> | null;
  simultaneous: boolean;
  held_constant_by_running_together: string[];
  still_varied: string[];
}
type LiveStatus = "running" | "streaming" | "done" | "error";
interface LiveArm { status: LiveStatus; thinking?: string; draft?: string; error?: string; final?: ArmData; }

/** A live box: running/streaming shows progress + streamed draft; done reuses the
 *  production render path (renderArmBox on the captured snapshot). Column order is
 *  fixed by experiment.arms — the faster arm must NEVER jump left, or every screenshot
 *  lies about which arm is which. v2 settled 22s ahead of v1 on the first fork. */
function renderLiveBox(arm: ArmMeta, live: LiveArm, expandAll: boolean): HTMLElement {
  if (live.status === "done" && live.final) return renderArmBox(arm, live.final, expandAll);

  const box = el("section", "ab-box ab-box--live");
  const head = el("header", "ab-box-head");
  head.appendChild(el("span", "ab-box-arm", arm.label));
  const badge = el("span", `ab-box-status ab-live--${live.status}`,
    live.status === "error" ? "error" : live.status === "streaming" ? "streaming…" : "running…");
  head.appendChild(badge);
  box.appendChild(head);

  if (live.status === "error") {
    box.appendChild(el("div", "ab-answer ab-answer--error", live.error || "stream error"));
    return box;
  }
  const body = el("div", "ab-answer ab-live-body");
  if (live.draft) {
    // The streamed draft, through the same inline-markdown the bubble uses. A preview —
    // the final answer swaps in on completion, rendered by the production path.
    const d = el("div", "ab-live-draft");
    d.innerHTML = _inlineMd(live.draft);
    body.appendChild(d);
  } else {
    const spin = el("div", "ab-live-spinner");
    spin.appendChild(el("span", "ab-live-dot"));
    spin.appendChild(el("span", "ab-live-word", live.thinking || "thinking…"));
    body.appendChild(spin);
  }
  box.appendChild(body);
  return box;
}

/** Open the fork, then a stream per arm. Never reorders columns. Stream first, snapshot
 *  second — reading the snapshot while a stream runs would render an empty column that
 *  looks like "no answer" instead of "still thinking". */
async function startFork(cmp: Comparison, root: HTMLElement): Promise<void> {
  let fork: ForkResponse;
  try {
    const r = await fetch(`${API}/ab/runs/${encodeURIComponent(cmp.run_id)}/q/${encodeURIComponent(cmp.question.id)}/fork`,
      { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
    if (r.status === 409) {
      const msg = await r.json().catch(() => ({}));
      alert(`Fork not enabled on this service: ${(msg as { detail?: string }).detail || "MOBIUS_V2_AB_FORK off"}`);
      return;
    }
    if (!r.ok) throw new Error(`fork: HTTP ${r.status}`);
    fork = await r.json();
  } catch (e) {
    alert(`Could not start fork: ${(e as Error).message}`);
    return;
  }

  const arms = cmp.experiment.arms;              // ORDER FROZEN here, once.
  const state: Record<string, LiveArm> = {};
  for (const a of arms) {
    state[a.id] = fork.launched[a.id] ? { status: "running" } : { status: "error", error: "not launched" };
  }
  const expandAll = readExpandAll();

  function paint(): void {
    root.textContent = "";
    root.appendChild(el("div", "ab-banner",
      "A/B HARNESS — both arms forked on identical input at the same instant. Nobody was served."));

    // The two provenance strips, from payload data (not hardcoded). still_varied stays
    // visible: one fork is one sample of two, and simultaneity removes the confound, not
    // the model's own sampling variance.
    const prov = el("div", "ab-fork-prov");
    const held = el("div", "ab-fork-held");
    held.appendChild(el("span", "ab-exp-key", "Held constant by running together"));
    held.appendChild(el("span", "ab-exp-val", fork.held_constant_by_running_together.join(" · ")));
    const varied = el("div", "ab-fork-varied");
    varied.appendChild(el("span", "ab-exp-key", "Still varied"));
    varied.appendChild(el("span", "ab-exp-val", fork.still_varied.join(" · ")));
    prov.appendChild(held);
    prov.appendChild(varied);
    root.appendChild(prov);

    root.appendChild(el("h1", "ab-question", cmp.question.q));
    const grid = el("div", "ab-grid");
    grid.style.setProperty("--ab-cols", String(arms.length));
    for (const a of arms) grid.appendChild(renderLiveBox(a, state[a.id], expandAll));
    root.appendChild(grid);
  }
  paint();

  async function captureAndFinalize(armId: string, cid: string): Promise<void> {
    try {
      await fetch(`${API}/ab/runs/${encodeURIComponent(cmp.run_id)}/q/${encodeURIComponent(cmp.question.id)}/arm/${encodeURIComponent(armId)}`,
        { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ correlation_id: cid }) });
      const cr = await fetch(`${API}/ab/runs/${encodeURIComponent(cmp.run_id)}/q/${encodeURIComponent(cmp.question.id)}`);
      const fresh: Comparison = await cr.json();
      state[armId] = { status: "done", final: fresh.arms[armId] };
    } catch (e) {
      state[armId] = { status: "error", error: `capture failed: ${(e as Error).message}` };
    }
    paint();
  }

  for (const a of arms) {
    const cid = fork.launched[a.id];
    if (!cid) continue;
    const es = new EventSource(`${API}/chat/stream/${encodeURIComponent(cid)}`);
    es.onmessage = (e: MessageEvent) => {
      let parsed: { event: string; data?: Record<string, unknown> };
      try { parsed = JSON.parse(e.data as string); } catch { return; }
      const d = parsed.data || {};
      if (parsed.event === "thinking" && d.line != null) {
        if (state[a.id].status === "running") { state[a.id].thinking = String(d.line); paint(); }
      } else if (parsed.event === "draft_ready" && d.text != null) {
        state[a.id].status = "streaming"; state[a.id].draft = String(d.text); paint();
      } else if (parsed.event === "completed") {
        try { es.close(); } catch { /* already closed */ }
        void captureAndFinalize(a.id, cid);
      } else if (parsed.event === "error" && d.message != null) {
        try { es.close(); } catch { /* already closed */ }
        state[a.id] = { status: "error", error: String(d.message) }; paint();
      }
    };
    es.onerror = () => {
      // A transport drop after completion is normal (we already closed). Only surface it
      // if the arm hadn't finished — otherwise it's the expected close.
      if (state[a.id].status === "running" || state[a.id].status === "streaming") {
        try { es.close(); } catch { /* noop */ }
        state[a.id] = { status: "error", error: "stream disconnected before completion" };
        paint();
      }
    };
  }
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
