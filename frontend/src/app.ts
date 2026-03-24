import {
  createAuthService,
  localStorageAdapter,
  createAuthModal,
  AUTH_STYLES,
} from "@mobius/auth";

/** Subset of auth profile for sidebar + answer insights gating */
interface MobiusChatUserProfile {
  greeting_name?: string;
  activities?: string[];
}

/** Clarification option: clickable choice for slot fill */
interface ClarificationOption {
  slot: string;
  label: string;
  selection_mode: string;
  choices: Array<{ value: string; label: string }>;
}

/** Roster/credentialing step output (CSV for validation) */
interface RosterStepOutput {
  step_id: string;
  step_num?: number;
  label: string;
  csv_content: string;
  row_count: number;
  /** Formatted markdown for display (e.g. NPI profile cards) */
  markdown_content?: string;
  /** JSON string for download (e.g. npi_profile.json) */
  json_content?: string;
}

/** Quality control / eval adjudication stamp for the assistant turn */
interface QcAuditInfo {
  passed: boolean;
  /** Canonical rubric verdict (PASS / PARTIAL / FAIL); ``passed`` is true for PASS and PARTIAL. */
  adjudication_verdict?: string;
  reason?: string;
  source?: string;
  audited_at?: string;
  /** Post-run / eval automated score 0–1 */
  automated_score?: number;
  /** Human override 0–1 (persisted in chat_turns.qc_audit) */
  user_score?: number;
  user_score_comment?: string | null;
  user_score_updated_at?: string;
  score?: number;
  /** Rubric dimension → 0–1 (post-run JSON adjudicator or eval POST) */
  sub_scores?: Record<string, number>;
  adjudicator_full_response?: string;
  adjudicator_model?: string;
  adjudicator_llm_call_id?: string;
}

/** Map qc_audit to UI labels and badge styling (three-way verdict). */
function adjudicationVerdictUi(qc: QcAuditInfo): {
  shortLabel: string;
  verdictBadgeText: string;
  badgeVariant: "pass" | "partial" | "fail";
} {
  const raw = (qc.adjudication_verdict || "").toString().trim().toUpperCase();
  if (raw === "PARTIAL") {
    return {
      shortLabel: "PARTIAL",
      verdictBadgeText: "Verdict: PARTIAL (acceptable)",
      badgeVariant: "partial",
    };
  }
  if (raw === "PASS") {
    return { shortLabel: "PASS", verdictBadgeText: "Verdict: PASS", badgeVariant: "pass" };
  }
  if (raw === "FAIL") {
    return { shortLabel: "FAIL", verdictBadgeText: "Verdict: FAIL", badgeVariant: "fail" };
  }
  return qc.passed
    ? { shortLabel: "PASS", verdictBadgeText: "Verdict: PASS", badgeVariant: "pass" }
    : { shortLabel: "FAIL", verdictBadgeText: "Verdict: FAIL", badgeVariant: "fail" };
}

/** Persisted thumbs for technical panels (from GET …/response DB enrich). */
interface TechnicalFeedback {
  llm_performance?: { rating: string; comment?: string | null } | null;
  adjudication?: { rating: string; comment?: string | null } | null;
}

/** One LLM step in the answer pipeline — LLM performance table. */
interface AnswerInsightRow {
  stage: string;
  step_label?: string;
  display_stage?: string;
  model: string;
  provider: string;
  input_tokens: number;
  output_tokens: number;
  cost_usd?: number;
  latency_ms?: number;
  llm_call_id?: string;
  is_ab_call?: boolean;
  /** Server: ok | error (LLM call outcome) */
  call_status?: string;
  /** ModelRouter transparency (from llm_manager) */
  router_selection?: string;
  router_reason?: string;
  router_exploration_round?: boolean;
  router_circuit_relief?: boolean;
  router_candidates_eligible?: number;
  router_candidates_after_breaker?: number;
  router_avg_quality_at_pick?: number;
  router_quality_samples_at_pick?: number;
  /** Batch composite at router decision (PG row; stage-specific linear caps in app) */
  router_composite_at_pick?: number;
  router_composite_breakdown?: Record<string, number | string>;
  /** Same weights as router composite, applied to this call’s latency/cost/QA/error */
  per_call_composite?: number;
  per_call_composite_breakdown?: Record<string, number | string>;
  /** Per-call QA from post_run / llm_calls (0–1), merged after adjudication */
  quality_score?: number;
  quality_source?: string;
}

/** Aggregates for LLM performance panel (server: integrate payload). */
interface LlmPerformanceMeta {
  pipeline: string;
  primary_model?: string;
  total_latency_ms?: number;
  total_cost_usd?: number;
  config_sha?: string | null;
  jurisdiction_summary?: string | null;
  jurisdiction?: { payer?: string; state?: string; program?: string };
  top_source?: {
    document_name?: string | null;
    page_number?: number | null;
    match_score?: number | null;
    confidence?: number | null;
  } | null;
  integrator_exploration?: boolean | null;
  /** Per-call router explanations (stage, model, mode, reason) */
  router_by_stage?: Array<{
    stage?: string;
    model?: string;
    mode?: string;
    exploration?: boolean;
    circuit_relief?: boolean;
    reason?: string;
    composite_pg?: number;
    composite_call?: number;
  }>;
}

/** GET /chat/llm-router-report — hamburger menu model router report */
interface LlmRouterReportModelRow {
  stage: string;
  model: string;
  provider: string | null;
  total_calls: number;
  quality_samples: number;
  avg_quality: number | null;
  avg_latency_ms: number | null;
  p95_latency_ms: number | null;
  hard_error_rate: number;
  avg_cost_usd: number | null;
  avg_input_tokens?: number | null;
  avg_output_tokens?: number | null;
  usd_per_1k_input?: number | null;
  usd_per_1k_output?: number | null;
  avg_list_price_usd?: number | null;
  composite_score: number;
  composite_breakdown?: Record<string, number | string> | null;
  confidence: string;
}

/** Server: composite_score_api_spec() — definition + stage linear caps */
interface LlmRouterReportCompositeSpec {
  title?: string;
  summary?: string;
  formula?: string;
  weights?: Record<string, number>;
  quality?: { definition?: string };
  reliability?: { definition?: string };
  latency_term?: { definition?: string };
  cost_term?: { definition?: string };
  stage_caps?: Record<string, { latency_cap_ms: number; cost_cap_usd: number }>;
  stage_bucket_rules?: string;
  token_pricing_note?: string;
  react_deep_rounds_note?: string;
}

interface LlmRouterReportStage {
  stage: string;
  /** planner | react | other — ReAct rounds reported separately for bandit stats */
  stage_family?: string;
  react_round?: number | null;
  models: LlmRouterReportModelRow[];
}

interface LlmRouterReportResponse {
  ok: boolean;
  window_days: number;
  generated_at: string;
  warning: string | null;
  stages: LlmRouterReportStage[];
  thompson: {
    title: string;
    summary: string;
    exploration_interval_turns: number;
    circuit_breaker_hard_error_max: number;
    circuit_breaker_24h_error_max: number;
    confidence_legend: Record<string, string>;
  };
  roster_enabled: Array<{ model_id: string; display_name: string; provider: string }>;
  composite_spec?: LlmRouterReportCompositeSpec;
}

/** Chat API response when polling for completion */
interface ChatResponse {
  status: string;
  message: string | null;
  correlation_id?: string;
  plan?: unknown;
  thinking_log?: string[];
  response_source?: string;
  model_used?: string | null;
  llm_error?: string | null;
  sources?: SourceItem[];
  source_confidence_strip?: string | null;
  cited_source_indices?: number[];
  /** Per–LLM-call stats (planning, ReAct rounds, RAG, integrator, …) */
  usage_breakdown?: AnswerInsightRow[];
  /** Rollups + jurisdiction for LLM performance (admin panel). */
  llm_performance?: LlmPerformanceMeta;
  tokens_used?: { input_tokens?: number; output_tokens?: number };
  cost_usd?: number;
  open_slots?: string[];
  clarification_options?: ClarificationOption[];
  /** Suggested follow-up questions from integrator (clickable options) */
  next_questions_for_user?: string[];
  /** Last ReAct / skill tool name (server-resolved) */
  tool_fired?: string;
  /** Server-built UI envelope (v1) */
  assistant_envelope?: AssistantEnvelope;
  /** Fallback single question when next_questions_for_user is empty */
  user_ask?: string | null;
  thread_id?: string;
  /** Roster/credentialing: step outputs (CSV per step) for validation */
  roster_step_outputs?: RosterStepOutput[];
  /** Roster/credentialing: report PDF as base64 for download */
  roster_report_pdf_base64?: string | null;
  /** Roster/credentialing: final report markdown for download when PDF unavailable */
  roster_report_final_md?: string | null;
  /** Co-pilot credentialing: validate pending step via panel or chat */
  credentialing_copilot?: CredentialingCopilotPayload | null;
  /** Set when eval/QC audit posts to POST /chat/qc-audit/{id} */
  qc_audit?: QcAuditInfo;
  /** DB-backed routing + adjudicator thumbs (merged on poll for completed turns). */
  technical_feedback?: TechnicalFeedback;
}

/** Server payload for co-pilot credentialing validation UI */
interface CredentialingCopilotPayload {
  run_id: string;
  pending_step_id?: string | null;
  phase?: string;
  draft_output?: Record<string, unknown> | null;
  mode?: string;
  org_name?: string | null;
  final_report_text?: string | null;
}

/** assistant_envelope v1 (server merges authoritative + validated LLM ui_blocks) */
interface AssistantEnvelope {
  version: number;
  blocks: EnvelopeBlock[];
}

type EnvelopeBlock =
  | { type: "tool_attribution"; tool_fired: string; icon: string; label: string }
  | { type: "direct_answer"; markdown: string }
  | { type: "detail"; markdown: string; collapsed_default?: boolean }
  | { type: "chart"; title?: string; caption?: string; image_base64: string }
  | { type: "table"; headers: string[]; rows: string[][] }
  | { type: "callout"; body: string; variant?: string }
  | {
      type: "sources";
      refs: Array<{
        index: number;
        title: string;
        page?: number | null;
        snippet?: string;
        document_id?: string | null;
        open?: { kind: string; href: string };
      }>;
    }
  | { type: "next_steps"; items: string[]; collapsed_default?: boolean }
  | { type: "suggested_questions"; items: string[]; collapsed_default?: boolean }
  | { type: "markdown_report"; markdown: string }
  | { type: "attachments"; has_pdf?: boolean };

/** Single RAG source (when backend provides sources array) */
interface SourceItem {
  document_name?: string;
  document_id?: string | null;
  page_number?: number | null;
  text?: string;
  cite_text?: string | null;
  index?: number;
  open_href?: string | null;
  open_kind?: string | null;
  url?: string | null;
}

/** Parsed source from "Sources:" block or API response.sources (RAG) */
interface ParsedSource {
  index: number;
  document_name: string;
  document_id?: string | null;
  page_number: number | null;
  snippet: string;
  /** Longer excerpt for deep-link citation highlight in the document viewer */
  cite_text?: string | null;
  source_type?: string | null;
  match_score?: number | null;
  confidence?: number | null;
  /** Server-resolved open link (corpus viewer or web) */
  open_href?: string | null;
}

/** GET /chat/history/recent or most-helpful-searches */
interface HistoryTurnItem {
  correlation_id: string;
  question: string;
  created_at: string | null;
}

/** GET /chat/history/most-helpful-documents */
interface HistoryDocumentItem {
  document_name: string;
  document_id?: string | null;
  cited_in_count?: number;
}

/** Chat config API response */
interface ChatConfigResponse {
  config_sha?: string;
  prompts?: { first_gen_system?: string; first_gen_user_template?: string };
  llm?: { provider?: string; model?: string; temperature?: number };
  parser?: { patient_keywords?: string[] };
}

/** Config history entry from GET /chat/config/history */
interface ConfigHistoryEntry {
  config_sha?: string;
  created_at?: string;
  created_by?: string;
  model?: string;
  provider?: string;
  prompt_count?: number;
}

/** POST /chat response */
interface ChatPostResponse {
  correlation_id: string;
  thread_id?: string;
}

/** POST /chat/roster-upload — TurboTax-style recap payload */
interface RosterUploadAcknowledgment {
  headline: string;
  subhead: string;
  checks: { tone: string; title: string; detail: string }[];
  alerts: { tone: string; message: string }[];
  next_step: string;
  process_status?: string;
}
interface RosterUploadResponse {
  upload_id?: string;
  org_id?: string;
  org_name?: string;
  filename?: string;
  row_count?: number;
  row_count_cleansed?: number;
  row_count_resolved?: number;
  thread_id?: string;
  default_billing_npi?: string;
  matched_organization_name?: string;
  matched_practice_address?: string | null;
  process_status?: string;
  resolution_summary?: Record<string, number>;
  acknowledgment?: RosterUploadAcknowledgment | null;
}

/** Section intent for visibility rules */
const SECTION_INTENTS = ["process", "requirements", "definitions", "exceptions", "references"] as const;
type SectionIntent = (typeof SECTION_INTENTS)[number];

function isSectionIntent(s: unknown): s is SectionIntent {
  return typeof s === "string" && SECTION_INTENTS.includes(s as SectionIntent);
}

/** AnswerCard JSON from consolidator (FACTUAL / CANONICAL / BLENDED) */
interface AnswerCardSection {
  intent?: SectionIntent;
  label: string;
  bullets: string[];
}
interface AnswerCard {
  mode: "FACTUAL" | "CANONICAL" | "BLENDED";
  direct_answer: string;
  sections: AnswerCardSection[];
  required_variables?: string[];
  confidence_note?: string;
  citations?: Array<{ id: string; doc_title: string; locator: string; snippet: string }>;
  followups?: Array<{ question: string; reason: string; field: string }>;
}

const API_BASE =
  typeof window !== "undefined" &&
  window.API_BASE &&
  window.API_BASE.startsWith("http")
    ? window.API_BASE
    : "http://localhost:8000";

function renderLlmRouterReportCompositeSpec(
  parent: HTMLElement,
  spec: LlmRouterReportCompositeSpec | undefined
): void {
  if (!spec || !spec.title) return;
  const details = document.createElement("details");
  details.className = "llm-router-report-composite";
  details.open = false;
  const summ = document.createElement("summary");
  summ.textContent = spec.title;
  details.appendChild(summ);
  if (spec.summary) {
    const p = document.createElement("p");
    p.className = "llm-router-report-composite-p";
    p.textContent = spec.summary;
    details.appendChild(p);
  }
  if (spec.formula) {
    const pre = document.createElement("pre");
    pre.className = "llm-router-report-composite-formula";
    pre.textContent = spec.formula;
    details.appendChild(pre);
  }
  const w = spec.weights;
  if (w && Object.keys(w).length) {
    const wp = document.createElement("p");
    wp.className = "llm-router-report-composite-p";
    wp.textContent =
      "Weights: " +
      Object.entries(w)
        .map(([k, v]) => `${k}=${v}`)
        .join(", ");
    details.appendChild(wp);
  }
  const defs: Array<{ label: string; block?: { definition?: string } }> = [
    { label: "Quality (q)", block: spec.quality },
    { label: "Reliability (rel)", block: spec.reliability },
    { label: "Latency term", block: spec.latency_term },
    { label: "Cost term", block: spec.cost_term },
  ];
  for (const { label, block } of defs) {
    const d = block?.definition;
    if (!d) continue;
    const h = document.createElement("div");
    h.className = "llm-router-report-composite-def";
    const strong = document.createElement("strong");
    strong.textContent = label + ": ";
    h.appendChild(strong);
    h.appendChild(document.createTextNode(d));
    details.appendChild(h);
  }
  const caps = spec.stage_caps;
  if (caps && Object.keys(caps).length) {
    const hc = document.createElement("p");
    hc.className = "llm-router-report-composite-p";
    hc.innerHTML = "<strong>Linear caps by stage bucket</strong> (for latTerm / costTerm):";
    details.appendChild(hc);
    const tw = document.createElement("div");
    tw.className = "llm-router-report-table-wrap";
    const tbl = document.createElement("table");
    tbl.className = "llm-router-report-table llm-router-report-table--caps";
    tbl.innerHTML =
      "<thead><tr><th>Bucket</th><th>Latency cap (ms)</th><th>Cost cap ($)</th></tr></thead><tbody></tbody>";
    const tb = tbl.querySelector("tbody")!;
    for (const name of Object.keys(caps).sort()) {
      const c = caps[name];
      const tr = document.createElement("tr");
      tr.innerHTML = `<td>${escapeHtml(name)}</td><td>${c?.latency_cap_ms ?? "—"}</td><td>${c?.cost_cap_usd ?? "—"}</td>`;
      tb.appendChild(tr);
    }
    tw.appendChild(tbl);
    details.appendChild(tw);
  }
  if (spec.stage_bucket_rules) {
    const pr = document.createElement("p");
    pr.className = "llm-router-report-composite-p";
    pr.textContent = spec.stage_bucket_rules;
    details.appendChild(pr);
  }
  if (spec.token_pricing_note) {
    const pt = document.createElement("p");
    pt.className = "llm-router-report-composite-p";
    pt.textContent = spec.token_pricing_note;
    details.appendChild(pt);
  }
  if (spec.react_deep_rounds_note) {
    const prd = document.createElement("p");
    prd.className = "llm-router-report-composite-p";
    prd.textContent = spec.react_deep_rounds_note;
    details.appendChild(prd);
  }
  parent.appendChild(details);
}

function fmtRouterReportCompositeTerms(row: LlmRouterReportModelRow): string {
  const b = row.composite_breakdown;
  if (!b || typeof b !== "object") return "—";
  const f = (k: string): string => {
    const x = b[k];
    return typeof x === "number" && Number.isFinite(x) ? x.toFixed(2) : "—";
  };
  return [f("term_quality"), f("term_reliability"), f("term_latency"), f("term_cost")].join(" / ");
}

function routerReportTermsTooltip(row: LlmRouterReportModelRow): string {
  const b = row.composite_breakdown;
  if (!b || typeof b !== "object") return "";
  try {
    return JSON.stringify(b, null, 2).slice(0, 4000);
  } catch {
    return "";
  }
}

function setupLlmRouterReportUI(): void {
  const btn = document.getElementById("btnLlmRouterReport");
  const modal = document.getElementById("llmRouterReportModal");
  const body = document.getElementById("llmRouterReportBody");
  const closeBtn = document.getElementById("llmRouterReportClose");
  const backdrop = document.getElementById("llmRouterReportBackdrop");
  if (!btn || !modal || !body) return;

  const setOpen = (open: boolean): void => {
    modal.classList.toggle("llm-router-report-modal--open", open);
    modal.setAttribute("aria-hidden", open ? "false" : "true");
  };

  const loadReport = (): void => {
    body.innerHTML = '<p class="llm-router-report-loading">Loading…</p>';
    fetch(API_BASE + "/chat/llm-router-report?window_days=30")
      .then((r) => r.json() as Promise<LlmRouterReportResponse>)
      .then((data) => {
        renderLlmRouterReportBody(body, data);
      })
      .catch(() => {
        body.innerHTML =
          '<p class="llm-router-report-error">Could not load report. Is the API up and <code>CHAT_RAG_DATABASE_URL</code> set?</p>';
      });
  };

  btn.addEventListener("click", () => {
    setOpen(true);
    loadReport();
  });
  closeBtn?.addEventListener("click", () => setOpen(false));
  backdrop?.addEventListener("click", () => setOpen(false));
  document.addEventListener("keydown", (e: KeyboardEvent) => {
    if (e.key === "Escape" && modal.classList.contains("llm-router-report-modal--open")) setOpen(false);
  });
}

function renderLlmRouterReportBody(container: HTMLElement, data: LlmRouterReportResponse): void {
  container.replaceChildren();

  const meta = document.createElement("p");
  meta.className = "llm-router-report-meta";
  const gen = data.generated_at ? new Date(data.generated_at).toLocaleString() : "—";
  meta.textContent = `Rolling window: ${data.window_days} days · Generated ${gen}`;
  container.appendChild(meta);

  if (data.warning) {
    const w = document.createElement("p");
    w.className = "llm-router-report-error";
    w.textContent = data.warning;
    container.appendChild(w);
  }

  renderLlmRouterReportCompositeSpec(container, data.composite_spec);

  const th = data.thompson;
  if (th) {
    const details = document.createElement("details");
    details.className = "llm-router-report-thompson";
    details.open = true;
    const summ = document.createElement("summary");
    summ.textContent = th.title || "How routing works";
    details.appendChild(summ);
    const p = document.createElement("p");
    p.className = "llm-router-report-thompson-summary";
    p.textContent = th.summary;
    details.appendChild(p);
    const ul = document.createElement("ul");
    ul.className = "llm-router-report-thompson-list";
    const li1 = document.createElement("li");
    li1.textContent = `Forced exploration: least-sampled model every ${th.exploration_interval_turns} turns per stage.`;
    ul.appendChild(li1);
    const li2 = document.createElement("li");
    li2.textContent = `Circuit breakers: pull models above ~${(th.circuit_breaker_hard_error_max * 100).toFixed(0)}% hard failures or ~${(th.circuit_breaker_24h_error_max * 100).toFixed(0)}% errors (24h).`;
    ul.appendChild(li2);
    const leg = th.confidence_legend || {};
    const li3 = document.createElement("li");
    li3.textContent =
      "Row shading: " +
      ["low", "medium", "high", "locked"]
        .map((k) => `${k} — ${leg[k] || k}`)
        .join(" ");
    ul.appendChild(li3);
    details.appendChild(ul);
    container.appendChild(details);
  }

  const legend = document.createElement("div");
  legend.className = "llm-router-report-legend";
  legend.innerHTML =
    '<span class="llm-router-report-legend-item llm-router-report-tr--low">Low data</span>' +
    '<span class="llm-router-report-legend-item llm-router-report-tr--medium">Medium</span>' +
    '<span class="llm-router-report-legend-item llm-router-report-tr--high">High</span>' +
    '<span class="llm-router-report-legend-item llm-router-report-tr--locked">Locked-in</span>' +
    '<span class="llm-router-report-legend-note">= adjudicated sample count (quality scores)</span>';
  container.appendChild(legend);

  if (!data.stages || data.stages.length === 0) {
    const empty = document.createElement("p");
    empty.className = "llm-router-report-empty";
    empty.textContent = data.ok
      ? "No llm_calls in this window yet. Chat to populate stats."
      : "No data.";
    container.appendChild(empty);
  }

  for (const block of data.stages || []) {
    const h3 = document.createElement("h3");
    h3.className = "llm-router-report-stage-title";
    if (block.stage_family === "react" && block.react_round != null && Number.isFinite(block.react_round)) {
      h3.textContent = `ReAct reasoning · round ${block.react_round} (${block.stage})`;
    } else {
      h3.textContent = block.stage || "—";
    }
    container.appendChild(h3);

    const wrap = document.createElement("div");
    wrap.className = "llm-router-report-table-wrap";
    const table = document.createElement("table");
    table.className = "llm-router-report-table";
    const thead = document.createElement("thead");
    thead.innerHTML =
      "<tr>" +
      '<th title="Rank within stage">#</th>' +
      "<th>Model</th>" +
      "<th>Provider</th>" +
      "<th>Calls</th>" +
      "<th title='Adjudicated quality rows'>Scored</th>" +
      "<th title='Mean quality_score'>Avg Q</th>" +
      "<th title='Router composite [0,1]'>Comp</th>" +
      '<th title="q·r / r / lat / cost weighted terms (hover row for JSON)">Terms</th>' +
      '<th title="stage_bucket">Bkt</th>' +
      '<th title="p95 latency ms (success)">p95</th>' +
      '<th title="Mean cost_usd (success)">Avg $</th>' +
      '<th title="Mean input_tokens">In tok</th>' +
      '<th title="Mean output_tokens">Out tok</th>' +
      '<th title="Registered $/1K input (cost_model)">$/1K in</th>' +
      '<th title="Registered $/1K output">$/1K out</th>' +
      '<th title="(In tok/1000)×$/1K in + (Out tok/1000)×$/1K out">List $</th>' +
      '<th title="Mean latency ms">Avg ms</th>' +
      '<th title="Hard error rate">Err %</th>' +
      "</tr>";
    table.appendChild(thead);
    const tbody = document.createElement("tbody");
    (block.models || []).forEach((row, idx) => {
      const tr = document.createElement("tr");
      tr.className = "llm-router-report-tr llm-router-report-tr--" + (row.confidence || "low");
      const b = row.composite_breakdown || {};
      const bucket =
        typeof b.stage_bucket === "string" ? b.stage_bucket : "—";
      const cells: Array<{ text: string; title?: string }> = [
        { text: String(idx + 1) },
        { text: row.model || "—" },
        { text: row.provider || "—" },
        { text: String(row.total_calls ?? 0) },
        { text: String(row.quality_samples ?? 0) },
        { text: row.avg_quality != null ? Number(row.avg_quality).toFixed(3) : "—" },
        { text: row.composite_score != null ? Number(row.composite_score).toFixed(3) : "—" },
        { text: fmtRouterReportCompositeTerms(row), title: routerReportTermsTooltip(row) },
        { text: bucket },
        { text: row.p95_latency_ms != null ? String(row.p95_latency_ms) : "—" },
        {
          text:
            row.avg_cost_usd != null && Number(row.avg_cost_usd) > 0
              ? Number(row.avg_cost_usd).toFixed(4)
              : row.avg_cost_usd != null
                ? String(row.avg_cost_usd)
                : "—",
        },
        { text: row.avg_input_tokens != null ? String(row.avg_input_tokens) : "—" },
        { text: row.avg_output_tokens != null ? String(row.avg_output_tokens) : "—" },
        {
          text:
            row.usd_per_1k_input != null ? Number(row.usd_per_1k_input).toFixed(5) : "—",
        },
        {
          text:
            row.usd_per_1k_output != null ? Number(row.usd_per_1k_output).toFixed(5) : "—",
        },
        {
          text:
            row.avg_list_price_usd != null && row.avg_list_price_usd > 0
              ? Number(row.avg_list_price_usd).toFixed(4)
              : row.avg_list_price_usd != null
                ? String(row.avg_list_price_usd)
                : "—",
        },
        { text: row.avg_latency_ms != null ? String(row.avg_latency_ms) : "—" },
        {
          text:
            row.hard_error_rate != null ? (Number(row.hard_error_rate) * 100).toFixed(1) + "%" : "—",
        },
      ];
      cells.forEach(({ text, title }) => {
        const td = document.createElement("td");
        td.textContent = text;
        if (title) td.setAttribute("title", title);
        tr.appendChild(td);
      });
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    wrap.appendChild(table);
    container.appendChild(wrap);
  }

  const roster = data.roster_enabled || [];
  if (roster.length > 0) {
    const rd = document.createElement("details");
    rd.className = "llm-router-report-roster";
    const rs = document.createElement("summary");
    rs.textContent = `Currently enabled in router roster (${roster.length} models)`;
    rd.appendChild(rs);
    const pre = document.createElement("pre");
    pre.className = "llm-router-report-roster-pre";
    pre.textContent = roster.map((r) => `${r.model_id} (${r.provider}) — ${r.display_name}`).join("\n");
    rd.appendChild(pre);
    container.appendChild(rd);
  }
}

function el(id: string): HTMLElement {
  const e = document.getElementById(id);
  if (!e) throw new Error("Element not found: " + id);
  return e;
}

function normalizeMessageText(text: string): string {
  return (text ?? "").replace(/\n{2,}/g, "\n").trim();
}

const SANITIZE_BLEED_FALLBACK =
  "We couldn’t display this answer cleanly. Please try again or rephrase your question.";

/** Strip JSON bleed / fences before showing integrator output as prose (never show raw AnswerCard JSON). */
function sanitizeDisplayMessage(raw: string): string {
  const trimmed = (raw ?? "").trim();
  if (!trimmed) return "";

  const tryExtractFromJsonString = (jsonStr: string, depth: number): string | null => {
    if (depth > 4) return null;
    let s = jsonStr.trim();
    if (/^json\s*\{/i.test(s)) s = s.replace(/^json\s*/i, "").trim();
    s = s.replace(/^```json\s*/i, "").replace(/^```\s*/i, "").replace(/\s*```\s*$/i, "").trim();
    if (!s.startsWith("{") && !s.startsWith("[")) return null;
    try {
      const parsed = JSON.parse(s) as Record<string, unknown>;
      if (typeof parsed.answer === "string" && parsed.answer.trim()) {
        const inner = tryExtractFromJsonString(parsed.answer, depth + 1);
        return inner ?? parsed.answer.trim();
      }
      if (typeof parsed.direct_answer === "string" && parsed.direct_answer.trim()) {
        const inner = tryExtractFromJsonString(parsed.direct_answer, depth + 1);
        if (inner) return inner;
        const da = parsed.direct_answer.trim();
        if (!da.startsWith("{") && !da.startsWith("[")) return da;
      }
      if (typeof parsed.message === "string" && parsed.message.trim()) {
        return parsed.message.trim();
      }
      const res = parsed.resolutions;
      if (Array.isArray(res) && res.length > 0) {
        const parts: string[] = [];
        for (const item of res) {
          if (!item || typeof item !== "object") continue;
          const o = item as Record<string, unknown>;
          const r = o.resolution;
          if (typeof r === "string" && r.trim()) parts.push(r.trim());
          else if (r && typeof r === "object") {
            const rd = (r as Record<string, unknown>).direct_answer;
            if (typeof rd === "string" && rd.trim()) parts.push(rd.trim());
          }
          if (typeof o.text === "string" && o.text.trim()) parts.push(o.text.trim());
          if (typeof o.answer === "string" && o.answer.trim()) parts.push(o.answer.trim());
        }
        if (parts.length) return parts.join("\n\n");
      }
      return null;
    } catch {
      return null;
    }
  };

  let s = trimmed;
  if (/^json\s*\{/i.test(s)) s = s.replace(/^json\s*/i, "").trim();
  s = s.replace(/^```json\s*/i, "").replace(/^```\s*/i, "").replace(/\s*```\s*$/i, "").trim();

  const extracted = tryExtractFromJsonString(s, 0);
  if (extracted) return extracted;

  if (s.startsWith("{") || s.startsWith("[")) {
    try {
      JSON.parse(s);
      return SANITIZE_BLEED_FALLBACK;
    } catch {
      /* not valid JSON */
    }
  }
  if (/^\s*\{/.test(s) && /"direct_answer"\s*:/.test(s) && /"sections"\s*:/.test(s)) {
    return SANITIZE_BLEED_FALLBACK;
  }
  return s;
}

function isAllowedOpenHref(href: string): boolean {
  const t = href.trim();
  if (!t || t.toLowerCase().startsWith("javascript:")) return false;
  if (t.startsWith("/")) return true;
  return /^https?:\/\//i.test(t);
}

/** Map raw thinking log lines to short user-facing status (no step counts). */
function thinkingFriendlyStatus(line: string): string {
  const l = (line ?? "").toLowerCase();
  if (l.includes("waiting for worker") || l.includes("request sent")) return "Connecting…";
  if (l.includes("searching our materials") || l.includes("search_corpus") || l.includes("library research")) {
    return "Searching provider materials…";
  }
  if (l.includes("google") || l.includes("web search") || l.includes("web_scrape") || l.includes("web page")) {
    return "Searching the web…";
  }
  if (l.includes("npi") || l.includes("nppes") || l.includes("registry lookup")) return "Looking up provider registry…";
  if (l.includes("credentialing") || l.includes("roster_report") || l.includes("roster report")) {
    return "Running credentialing report…";
  }
  if (l.includes("draft composer") || l.includes("integrator") || l.includes("composing your answer")) {
    return "Composing your answer…";
  }
  if (l.includes("validator") || l.includes("answer card")) return "Checking answer format…";
  if (l.includes("quality") || l.includes("adjudicat")) return "Quality review…";
  if (l.includes("model:")) return "Finishing up…";
  return "Working on your answer…";
}

/** Minimal markdown to HTML for report display (headers, bold, paragraphs, images). Escapes HTML first. */
function simpleMarkdownToHtml(text: string): string {
  const s = (text ?? "").trim();
  if (!s) return "";
  const escape = (t: string) =>
    t
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  const imgs: string[] = [];
  // Match ![alt](url) - prefer data:image/ for charts; fallback to general URL
  const imgRe = /!\[([^\]]*)\]\(([^)]+)\)/g;
  let out = s.replace(imgRe, (_m, alt: string, url: string) => {
    const escapedAlt = escape(alt || "");
    const i = imgs.length;
    imgs.push(`<img src="${url}" alt="${escapedAlt}" class="report-chart" loading="lazy" />`);
    return `\uE000${i}\uE001`;
  });
  out = escape(out);
  imgs.forEach((img, i) => {
    out = out.replace(`\uE000${i}\uE001`, img);
  });
  out = out.replace(/^#### (.+)$/gm, "<h4>$1</h4>");
  out = out.replace(/^### (.+)$/gm, "<h3>$1</h3>");
  out = out.replace(/^## (.+)$/gm, "<h2>$1</h2>");
  out = out.replace(/^# (.+)$/gm, "<h1>$1</h1>");
  out = out.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
  out = out.replace(/\n\n+/g, "</p><p>");
  out = out.replace(/\n/g, "<br>\n");
  return "<p>" + out + "</p>";
}

/** Same as simpleMarkdownToHtml but does not escape HTML. Use only for trusted backend content (e.g. inside npi-profile-card). */
function simpleMarkdownToHtmlInner(text: string): string {
  const s = (text ?? "").trim();
  if (!s) return "";
  let out = s;
  out = out.replace(/^#### (.+)$/gm, "<h4>$1</h4>");
  out = out.replace(/^### (.+)$/gm, "<h3>$1</h3>");
  out = out.replace(/^## (.+)$/gm, "<h2>$1</h2>");
  out = out.replace(/^# (.+)$/gm, "<h1>$1</h1>");
  out = out.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
  out = out.replace(/^- (.+)$/gm, "<li>$1</li>");
  out = out.replace(/\n\n+/g, "</p><p>");
  out = out.replace(/\n/g, "<br>\n");
  out = "<p>" + out + "</p>";
  // Wrap consecutive <li> in <ul>
  out = out.replace(/((?:<li>[\s\S]*?<\/li>(?:<br>\s*)?)+)/g, "<ul>$1</ul>");
  return out;
}

/** Roster step markdown: preserves <div class="npi-profile-card"> and renders markdown inside it (for chat/collapsible). */
function rosterStepMarkdownToHtml(text: string): string {
  const s = (text ?? "").trim();
  if (!s) return "";
  if (!s.includes("npi-profile-card")) {
    return simpleMarkdownToHtml(s);
  }
  const cardBlocks: string[] = [];
  const placeholder = (i: number) => `\uE000CARD${i}\uE001`;
  const re = /<div class="npi-profile-card" markdown="1">\s*([\s\S]*?)<\/div>/g;
  let out = s.replace(re, (_full: string, inner: string) => {
    const i = cardBlocks.length;
    cardBlocks.push(inner);
    return placeholder(i);
  });
  out = simpleMarkdownToHtml(out);
  cardBlocks.forEach((inner, i) => {
    const cardHtml = '<div class="npi-profile-card">' + simpleMarkdownToHtmlInner(inner) + "</div>";
    out = out.replace(placeholder(i), cardHtml);
  });
  return out;
}

const MAX_SECTIONS = 4;
const MAX_BULLETS_PER_SECTION = 4;

function findMatchingCloseBrace(str: string, start: number): number {
  let depth = 0;
  let inString = false;
  let escape = false;
  let quote = "";
  for (let i = start; i < str.length; i++) {
    const c = str[i];
    if (escape) {
      escape = false;
      continue;
    }
    if (inString) {
      if (c === "\\") escape = true;
      else if (c === quote) inString = false;
      continue;
    }
    if (c === '"' || c === "'") {
      inString = true;
      quote = c;
      continue;
    }
    if (c === "{") depth++;
    else if (c === "}") {
      depth--;
      if (depth === 0) return i;
    }
  }
  return -1;
}

function tryParseAnswerCard(message: string): AnswerCard | null {
  if (!message || !message.trim()) return null;
  let raw = message.trim();
  if (raw.startsWith("```")) {
    const lines = raw.split("\n");
    if (lines[0].startsWith("```")) lines.shift();
    if (lines.length > 0 && lines[lines.length - 1].trim() === "```") lines.pop();
    raw = lines.join("\n").trim();
  }
  const parseOne = (str: string): AnswerCard | null => {
    try {
      const data = JSON.parse(str) as Record<string, unknown>;
      if (data.mode !== "FACTUAL" && data.mode !== "CANONICAL" && data.mode !== "BLENDED") return null;
      if (typeof data.direct_answer !== "string") return null;
      if (!Array.isArray(data.sections)) return null;
      const rawSections = (data.sections as Array<{ intent?: unknown; label?: string; bullets?: string[] }>).slice(0, MAX_SECTIONS);
      const sections: AnswerCardSection[] = rawSections.map((sec) => ({
        intent: isSectionIntent(sec.intent) ? sec.intent : "process",
        label: typeof sec.label === "string" ? sec.label : "",
        bullets: Array.isArray(sec.bullets) ? sec.bullets : [],
      }));
      return {
        mode: data.mode as AnswerCard["mode"],
        direct_answer: data.direct_answer as string,
        sections,
        required_variables: Array.isArray(data.required_variables) ? (data.required_variables as string[]) : undefined,
        confidence_note: typeof data.confidence_note === "string" ? data.confidence_note : undefined,
        citations: Array.isArray(data.citations) ? (data.citations as AnswerCard["citations"]) : undefined,
        followups: Array.isArray(data.followups) ? (data.followups as AnswerCard["followups"]) : undefined,
      };
    } catch {
      return null;
    }
  };
  if (raw.startsWith("{")) {
    const card = parseOne(raw);
    if (card) return card;
    const close = findMatchingCloseBrace(raw, 0);
    if (close !== -1) {
      const card2 = parseOne(raw.slice(0, close + 1));
      if (card2) return card2;
    }
    const fixed = raw.replace(/\}\]\}\],/g, "}],").replace(/\}\]\},/g, "}],");
    if (fixed !== raw) {
      const card3 = parseOne(fixed);
      if (card3) return card3;
    }
  }
  const modeRe = /["']mode["']\s*:\s*["'](FACTUAL|CANONICAL|BLENDED)["']/;
  const m = raw.match(modeRe);
  if (m) {
    const idx = raw.indexOf(m[0]);
    const start = raw.lastIndexOf("{", idx);
    if (start !== -1) {
      const end = findMatchingCloseBrace(raw, start);
      if (end !== -1) {
        const card = parseOne(raw.slice(start, end + 1));
        if (card) return card;
      }
    }
  }
  return null;
}

function splitSectionsByVisibility(
  sections: AnswerCardSection[],
  mode: AnswerCard["mode"]
): { visible: AnswerCardSection[]; hidden: AnswerCardSection[] } {
  const all = sections.slice(0, MAX_SECTIONS);
  if (mode === "FACTUAL") return { visible: [], hidden: all };
  if (mode === "CANONICAL") return { visible: all, hidden: [] };
  const requirements = all.filter((s) => (s.intent ?? "process") === "requirements");
  const hidden = all.filter((s) => {
    const i = s.intent ?? "process";
    return i === "process" || i === "definitions" || i === "exceptions" || i === "references";
  });
  return { visible: requirements, hidden };
}

function renderOneSection(sec: AnswerCardSection): HTMLElement {
  const sectionEl = document.createElement("div");
  sectionEl.className = "answer-card-section";
  const labelEl = document.createElement("div");
  labelEl.className = "answer-card-section-label";
  labelEl.textContent = sec.label || "";
  sectionEl.appendChild(labelEl);
  const bullets = (sec.bullets ?? []).slice(0, MAX_BULLETS_PER_SECTION);
  bullets.forEach((b) => {
    const li = document.createElement("div");
    li.className = "answer-card-bullet";
    li.textContent = b;
    sectionEl.appendChild(li);
  });
  if (bullets.length < (sec.bullets?.length ?? 0)) {
    const more = document.createElement("div");
    more.className = "answer-card-more";
    more.textContent = "Show more";
    more.setAttribute("aria-label", "Show more bullets");
    sectionEl.appendChild(more);
  }
  return sectionEl;
}

/** Source confidence badge variants: strip value → { label, variant } */
const CONFIDENCE_BADGE_MAP: Record<
  string,
  { label: string; variant: string; icon: string }
> = {
  approved_authoritative: {
    label: "Approved – Authoritative",
    variant: "approved_authoritative",
    icon: "check",
  },
  approved_informational: {
    label: "Approved – Informational",
    variant: "approved_informational",
    icon: "shield",
  },
  proceed_with_caution: {
    label: "Proceed with Caution",
    variant: "proceed_with_caution",
    icon: "alert-triangle",
  },
  augmented_with_google: {
    label: "Augmented with External Search",
    variant: "augmented_with_google",
    icon: "globe",
  },
  informational_only: {
    label: "Informational Only",
    variant: "informational_only",
    icon: "info",
  },
  no_sources: {
    label: "No Sources",
    variant: "no_sources",
    icon: "alert-circle",
  },
};

function renderConfidenceBadge(strip: string): HTMLElement {
  const key = strip.toLowerCase().replace(/\s+/g, "_");
  const cfg = CONFIDENCE_BADGE_MAP[key] ?? {
    label: strip.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase()),
    variant: "unverified",
    icon: "info",
  };
  const wrap = document.createElement("div");
  wrap.className = "confidence-badge-wrap";
  const badge = document.createElement("span");
  badge.className = `confidence-badge confidence-badge--${cfg.variant}`;
  badge.setAttribute("aria-label", "Source confidence: " + cfg.label);

  const iconEl = document.createElement("span");
  iconEl.className = "confidence-badge-icon";
  iconEl.setAttribute("aria-hidden", "true");
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("fill", "none");
  svg.setAttribute("stroke", "currentColor");
  svg.setAttribute("stroke-width", "2");
  svg.setAttribute("stroke-linecap", "round");
  svg.setAttribute("stroke-linejoin", "round");
  svg.setAttribute("width", "14");
  svg.setAttribute("height", "14");
  const paths: Record<string, string> = {
    check: "M20 6L9 17l-5-5",
    shield: "M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z",
    "alert-triangle": "M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z M12 9v4 M12 17h.01",
    globe: "M21 12a9 9 0 01-9 9m9-9a9 9 0 00-9-9m9 9H3m9 9a9 9 0 01-9-9m9 9c1.657 0 3-4.03 3-9s-1.343-9-3-9m0 18c-1.657 0-3-4.03-3-9s1.343-9 3-9m-9 9a9 9 0 019-9",
    info: "M12 16v-4 M12 8h.01 M22 12c0 5.523-4.477 10-10 10S2 17.523 2 12 6.477 2 12 2s10 4.477 10 10z",
    "alert-circle": "M12 8v4m0 4h.01M22 12c0 5.523-4.477 10-10 10S2 17.523 2 12 6.477 2 12 2s10 4.477 10 10z",
  };
  const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
  path.setAttribute("d", paths[cfg.icon] ?? paths.info);
  svg.appendChild(path);
  iconEl.appendChild(svg);

  const labelEl = document.createElement("span");
  labelEl.className = "confidence-badge-label";
  labelEl.textContent = cfg.label;

  badge.appendChild(iconEl);
  badge.appendChild(labelEl);
  wrap.appendChild(badge);
  return wrap;
}

/** Neutral shield icon — no semantic color (stroke only). */
function createQcSampleShieldSvg(): SVGSVGElement {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("class", "qc-audit-badge-shield-svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("width", "11");
  svg.setAttribute("height", "11");
  svg.setAttribute("aria-hidden", "true");
  const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
  path.setAttribute("fill", "none");
  path.setAttribute("stroke", "currentColor");
  path.setAttribute("stroke-width", "1.35");
  path.setAttribute("stroke-linejoin", "round");
  path.setAttribute(
    "d",
    "M12 2.5 19.5 5.2v5.8c0 3.2-2.4 6.5-7.5 8.5-5.1-2-7.5-5.3-7.5-8.5V5.2L12 2.5z"
  );
  svg.appendChild(path);
  return svg;
}

/**
 * Subtle end-user marker: post-run QA / adjudication ran on this turn (when server merges qc_audit).
 * Omits pass/fail in the strip — admins see scores in the QA / Adjudicator panel.
 */
function renderQcAuditBadge(_qc: QcAuditInfo): HTMLElement {
  void _qc;
  const wrap = document.createElement("div");
  wrap.className = "qc-audit-badge-wrap";
  wrap.setAttribute("data-qc-sample", "1");

  const row = document.createElement("div");
  row.className = "qc-audit-badge-row";

  const badge = document.createElement("span");
  badge.className = "qc-audit-badge qc-audit-badge--neutral";
  badge.setAttribute(
    "aria-label",
    "This reply was checked by an automated quality review. It does not change your answer."
  );

  const iconEl = document.createElement("span");
  iconEl.className = "qc-audit-badge-icon";
  iconEl.setAttribute("aria-hidden", "true");
  iconEl.appendChild(createQcSampleShieldSvg());

  const labelEl = document.createElement("span");
  labelEl.className = "qc-audit-badge-label";
  labelEl.textContent = "Quality review completed";

  badge.appendChild(iconEl);
  badge.appendChild(labelEl);
  row.appendChild(badge);
  wrap.appendChild(row);

  const foot = document.createElement("p");
  foot.className = "qc-audit-badge-footnote";
  foot.textContent = "Does not change your answer.";

  wrap.appendChild(foot);
  return wrap;
}

/** Insert QC badge into an already-rendered assistant turn (late eval webhook). */
function applyQcAuditToTurn(turnWrap: HTMLElement, qc: QcAuditInfo | undefined): void {
  if (!qc) return;
  refreshLlmPerformanceQuality(turnWrap, qc);
  const assistantEl =
    turnWrap.querySelector(".message--assistant:last-of-type") ??
    turnWrap.querySelector(".message--assistant");
  if (!assistantEl || assistantEl.querySelector(".qc-audit-badge-wrap")) return;
  const bubble =
    assistantEl.querySelector(".answer-card-bubble") ??
    assistantEl.querySelector(".message-bubble");
  if (!bubble) return;
  const node = renderQcAuditBadge(qc);
  bubble.appendChild(node);
}

/** After post-run QC arrives, update LLM performance one-liner + quality badge (no duplicate full re-render). */
function refreshLlmPerformanceQuality(turnWrap: HTMLElement, qc: QcAuditInfo | undefined): void {
  const panel = turnWrap.querySelector(".llm-performance");
  if (!panel) return;
  const eq = effectiveQcScore(qc);
  const qText = eq !== null ? eq.toFixed(2) : "—";
  const oneline = panel.querySelector(".llm-performance-oneline") as HTMLElement | null;
  if (oneline) {
    const m = oneline.dataset.m || "—";
    const sec = oneline.dataset.s || "0";
    const cost = oneline.dataset.c || "0";
    const leg = oneline.dataset.legacy === "1";
    oneline.textContent = `${leg ? "[LEGACY] " : ""}${m} · ${sec}s · $${cost} · quality ${qText}`;
  }
  const badgeQ = panel.querySelector("[data-llm-badge-quality]");
  if (badgeQ) badgeQ.textContent = `quality ${qText}`;
}

function renderAnswerCard(
  card: AnswerCard,
  isError?: boolean,
  opts?: {
    onFollowupClick?: (question: string) => void;
    sourceConfidenceStrip?: string;
    showConfidenceBadge?: boolean;
    suppressFollowups?: boolean;
    nextQuestions?: string[];
    qcAudit?: QcAuditInfo;
    /** When true (admin + QA fail), omit source confidence badge */
    suppressConfidenceForAdminQcFail?: boolean;
  }
): HTMLElement {
  const wrap = document.createElement("div");
  wrap.className =
    "message message--assistant answer-card answer-card--" +
    card.mode.toLowerCase() +
    (isError ? " message--error" : "");

  const bubble = document.createElement("div");
  bubble.className = "message-bubble answer-card-bubble";

  const direct = document.createElement("div");
  direct.className = "answer-card-direct";
  direct.textContent = card.direct_answer;
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
  if (metaRow.childNodes.length > 0) bubble.appendChild(metaRow);

  const { visible, hidden } = splitSectionsByVisibility(card.sections ?? [], card.mode);
  visible.forEach((sec) => bubble.appendChild(renderOneSection(sec)));

  if (hidden.length > 0) {
    const detailsBlock = document.createElement("div");
    detailsBlock.className = "answer-card-details";
    detailsBlock.setAttribute("aria-hidden", "true");
    hidden.forEach((sec) => detailsBlock.appendChild(renderOneSection(sec)));
    bubble.appendChild(detailsBlock);

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
    bubble.appendChild(toggleBtn);
  }

  if (card.confidence_note && card.confidence_note.trim()) {
    const note = document.createElement("div");
    note.className = "answer-card-confidence";
    note.textContent = card.confidence_note;
    bubble.appendChild(note);
  }

  // Suggested follow-ups inside the card (unified with next_questions_for_user)
  const followupQuestions = opts?.nextQuestions ?? [];
  if (followupQuestions.length > 0 && opts?.onFollowupClick) {
    const followupWrap = document.createElement("div");
    followupWrap.className = "answer-card-followups";
    const label = document.createElement("div");
    label.className = "answer-card-followups-label";
    label.textContent = "Follow-up questions";
    followupWrap.appendChild(label);
    const hint = document.createElement("div");
    hint.className = "answer-card-followups-hint";
    hint.textContent = "Tap a line to send it as your next message.";
    followupWrap.appendChild(hint);
    const chips = document.createElement("div");
    chips.className = "answer-card-followups-chips answer-card-followups-chips--stacked";
    followupQuestions.slice(0, 6).forEach((q) => {
      const btn = document.createElement("button");
      btn.type = "button";
      const text = q.trim() || "Ask this";
      btn.className = "answer-card-followup-chip answer-card-followup-chip--row";
      btn.textContent = text;
      btn.setAttribute("aria-label", "Send: " + text);
      btn.addEventListener("click", () => opts!.onFollowupClick!(text));
      chips.appendChild(btn);
    });
    followupWrap.appendChild(chips);
    bubble.appendChild(followupWrap);
  }

  if (opts?.qcAudit) bubble.appendChild(renderQcAuditBadge(opts.qcAudit));

  wrap.appendChild(bubble);
  return wrap;
}

/** Render assistant content: AnswerCard JSON (formatted) or prose fallback. */
function renderAssistantContent(
  body: string,
  isError?: boolean,
  opts?: {
    onFollowupClick?: (question: string) => void;
    sourceConfidenceStrip?: string;
    showConfidenceBadge?: boolean;
    suppressFollowups?: boolean;
    nextQuestions?: string[];
    /** When true, render body as markdown (e.g. credentialing report) */
    renderAsMarkdown?: boolean;
    qcAudit?: QcAuditInfo;
    suppressConfidenceForAdminQcFail?: boolean;
  }
): HTMLElement {
  const card = tryParseAnswerCard(body);
  if (card) return renderAnswerCard(card, isError, { ...opts, nextQuestions: opts?.nextQuestions });
  const trimmed = (body ?? "").trim();
  if (trimmed.startsWith("{") && trimmed.length > 10) {
    const errWrap = document.createElement("div");
    errWrap.className = "message message--assistant" + (isError ? " message--error" : "");
    const errBubble = document.createElement("div");
    errBubble.className = "message-bubble";
    if (opts?.showConfidenceBadge !== false && !opts?.suppressConfidenceForAdminQcFail) {
      errBubble.appendChild(
        renderConfidenceBadge((opts?.sourceConfidenceStrip ?? "").trim() || "informational_only")
      );
    }
    const errText = document.createElement("div");
    errText.className = "message-bubble-text";
    errText.textContent = "Answer could not be displayed. Please try again.";
    errBubble.appendChild(errText);
    if (opts?.qcAudit) errBubble.appendChild(renderQcAuditBadge(opts.qcAudit));
    errWrap.appendChild(errBubble);
    return errWrap;
  }
  const wrap = document.createElement("div");
  wrap.className = "message message--assistant" + (isError ? " message--error" : "");
  const bubble = document.createElement("div");
  bubble.className = "message-bubble";
  if (opts?.showConfidenceBadge !== false && !opts?.suppressConfidenceForAdminQcFail) {
    bubble.appendChild(
      renderConfidenceBadge((opts?.sourceConfidenceStrip ?? "").trim() || "informational_only")
    );
  }
  const textEl = document.createElement("div");
  textEl.className = "message-bubble-text";
  if (opts?.renderAsMarkdown && trimmed.length > 0) {
    textEl.innerHTML = rosterStepMarkdownToHtml(body);
  } else {
    textEl.textContent = normalizeMessageText(sanitizeDisplayMessage(body));
  }
  bubble.appendChild(textEl);
  if (opts?.qcAudit) bubble.appendChild(renderQcAuditBadge(opts.qcAudit));
  wrap.appendChild(bubble);
  return wrap;
}

/** Render roster step outputs as collapsible sections (collapsed by default). */
function renderRosterStepOutputs(stepOutputs: RosterStepOutput[]): HTMLElement {
  const wrap = document.createElement("div");
  wrap.className = "roster-step-outputs";

  const header = document.createElement("div");
  header.className = "roster-step-outputs-header";
  header.setAttribute("role", "button");
  header.setAttribute("tabindex", "0");
  header.setAttribute("aria-expanded", "false");
  const headerTitle = document.createElement("span");
  headerTitle.className = "roster-step-outputs-title";
  headerTitle.textContent = "Step outputs (for validation)";
  const headerChevron = document.createElement("span");
  headerChevron.className = "roster-step-outputs-chevron";
  headerChevron.textContent = "▶";
  header.appendChild(headerTitle);
  header.appendChild(headerChevron);

  const body = document.createElement("div");
  const hasFullReport = stepOutputs.length >= 12;
  body.className = hasFullReport
    ? "roster-step-outputs-body"
    : "roster-step-outputs-body roster-step-outputs-body--collapsed";
  if (hasFullReport) {
    header.setAttribute("aria-expanded", "true");
    headerChevron.textContent = "▼";
  }

  for (const step of stepOutputs) {
    const section = document.createElement("div");
    section.className = "roster-step-section roster-step-section--collapsed";
    const stepLabel = (step.step_num ? `Step ${step.step_num}: ` : "") + (step.label || step.step_id);
    const rowHint = step.row_count > 0 ? ` (${step.row_count} row${step.row_count !== 1 ? "s" : ""})` : "";

    const sectionHeader = document.createElement("div");
    sectionHeader.className = "roster-step-section-header";
    sectionHeader.setAttribute("role", "button");
    sectionHeader.setAttribute("tabindex", "0");
    sectionHeader.setAttribute("aria-expanded", "false");
    sectionHeader.textContent = stepLabel + rowHint;

    const sectionBody = document.createElement("div");
    sectionBody.className = "roster-step-section-body";
    const hasMarkdown = !!(step.markdown_content && step.markdown_content.trim());
    const hasJson = !!(step.json_content && step.json_content.trim());
    if (hasMarkdown) {
      const mdWrap = document.createElement("div");
      mdWrap.className = "roster-step-markdown";
      mdWrap.innerHTML = rosterStepMarkdownToHtml(step.markdown_content!.trim());
      sectionBody.appendChild(mdWrap);
      if (hasJson) {
        const dlBtn = document.createElement("button");
        dlBtn.type = "button";
        dlBtn.className = "roster-step-download-json";
        dlBtn.textContent = "Download JSON";
        dlBtn.setAttribute("aria-label", "Download NPI profile as JSON");
        dlBtn.addEventListener("click", () => {
          const blob = new Blob([step.json_content!], { type: "application/json;charset=utf-8" });
          const url = URL.createObjectURL(blob);
          const a = document.createElement("a");
          a.href = url;
          a.download = "npi_profile.json";
          a.click();
          URL.revokeObjectURL(url);
        });
        sectionBody.appendChild(dlBtn);
      }
    } else {
      const pre = document.createElement("pre");
      pre.className = "roster-step-csv";
      pre.textContent = step.csv_content || "(no data)";
      sectionBody.appendChild(pre);
    }

    sectionHeader.addEventListener("click", () => {
      section.classList.toggle("roster-step-section--collapsed");
      sectionHeader.setAttribute("aria-expanded", section.classList.contains("roster-step-section--collapsed") ? "false" : "true");
    });
    sectionHeader.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        sectionHeader.click();
      }
    });

    section.appendChild(sectionHeader);
    section.appendChild(sectionBody);
    body.appendChild(section);
  }

  header.addEventListener("click", () => {
    body.classList.toggle("roster-step-outputs-body--collapsed");
    const collapsed = body.classList.contains("roster-step-outputs-body--collapsed");
    header.setAttribute("aria-expanded", collapsed ? "false" : "true");
    headerChevron.textContent = collapsed ? "▶" : "▼";
  });
  header.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      header.click();
    }
  });

  wrap.appendChild(header);
  wrap.appendChild(body);
  return wrap;
}

function draftToValidatedOutput(
  draft: Record<string, unknown> | null | undefined,
  stepId: string
): Record<string, unknown> {
  const d = draft && typeof draft === "object" ? draft : {};
  if (stepId === "identify_org" && Array.isArray(d.org_npis)) {
    return { org_npis: d.org_npis };
  }
  if (stepId === "find_locations" && Array.isArray(d.locations)) {
    return { locations: d.locations };
  }
  if (stepId === "find_associated_providers") {
    const out: Record<string, unknown> = {};
    if (d.associated_providers && typeof d.associated_providers === "object") {
      out.associated_providers = d.associated_providers;
    }
    if (d.active_roster && typeof d.active_roster === "object") {
      out.active_roster = d.active_roster;
    }
    return out;
  }
  return {};
}

/** Co-pilot credentialing: edit draft JSON or accept as-is, POST /chat/credentialing-runs/.../validate */
function renderCredentialingCopilotPanel(
  cc: CredentialingCopilotPayload,
  threadId: string | null | undefined
): HTMLElement {
  const wrap = document.createElement("div");
  wrap.className = "credentialing-copilot-panel";

  const title = document.createElement("div");
  title.className = "credentialing-copilot-title";
  title.textContent = "Credentialing co-pilot — validate step";
  wrap.appendChild(title);

  const meta = document.createElement("div");
  meta.className = "credentialing-copilot-meta";
  meta.textContent = `${cc.org_name || "—"} · run ${cc.run_id.slice(0, 8)}… · ${cc.phase || "—"}`;
  wrap.appendChild(meta);

  if (cc.phase === "complete") {
    const done = document.createElement("div");
    done.className = "credentialing-copilot-complete";
    done.textContent = "All steps complete. See the message above for the report summary.";
    wrap.appendChild(done);
    return wrap;
  }

  const pending = (cc.pending_step_id || "").trim();
  if (!pending) {
    const err = document.createElement("div");
    err.className = "credentialing-copilot-error";
    err.textContent = "No pending step.";
    wrap.appendChild(err);
    return wrap;
  }

  const stepLabel = document.createElement("div");
  stepLabel.className = "credentialing-copilot-step";
  stepLabel.textContent = `Pending step: ${pending}`;
  wrap.appendChild(stepLabel);

  const ta = document.createElement("textarea");
  ta.className = "credentialing-copilot-json";
  ta.rows = 12;
  ta.spellcheck = false;
  ta.value = JSON.stringify(cc.draft_output ?? {}, null, 2);
  ta.setAttribute("aria-label", "Validated output JSON for this step");
  wrap.appendChild(ta);

  const btnRow = document.createElement("div");
  btnRow.className = "credentialing-copilot-actions";

  const acceptBtn = document.createElement("button");
  acceptBtn.type = "button";
  acceptBtn.className = "credentialing-copilot-btn credentialing-copilot-btn--secondary";
  acceptBtn.textContent = "Accept draft as-is";
  acceptBtn.addEventListener("click", () => {
    ta.value = JSON.stringify(cc.draft_output ?? {}, null, 2);
  });

  const submitBtn = document.createElement("button");
  submitBtn.type = "button";
  submitBtn.className = "credentialing-copilot-btn credentialing-copilot-btn--primary";
  submitBtn.textContent = "Continue (submit validation)";
  submitBtn.addEventListener("click", async () => {
    let validated: Record<string, unknown>;
    try {
      validated = JSON.parse(ta.value) as Record<string, unknown>;
    } catch {
      alert("Invalid JSON — fix the textarea or use Accept draft as-is.");
      return;
    }
    submitBtn.disabled = true;
    acceptBtn.disabled = true;
    try {
      const r = await fetch(
        API_BASE + "/chat/credentialing-runs/" + encodeURIComponent(cc.run_id) + "/validate",
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ step_id: pending, validated_output: validated }),
        }
      );
      const data = (await r.json()) as CredentialingCopilotPayload & {
        draft_output?: Record<string, unknown>;
        phase?: string;
        pending_step_id?: string | null;
        error?: string;
        detail?: string;
      };
      if (!r.ok) {
        throw new Error((data.detail as string) || (data.error as string) || r.statusText);
      }
      const next: CredentialingCopilotPayload = {
        run_id: data.run_id || cc.run_id,
        pending_step_id: data.pending_step_id,
        phase: data.phase,
        draft_output: data.draft_output,
        mode: data.mode || "copilot",
        org_name: data.org_name ?? cc.org_name,
        final_report_text: data.final_report_text,
      };
      const parent = wrap.parentElement;
      const replacement = renderCredentialingCopilotPanel(next, threadId);
      parent?.replaceChild(replacement, wrap);
    } catch (e) {
      alert("Validation failed: " + (e instanceof Error ? e.message : String(e)));
      submitBtn.disabled = false;
      acceptBtn.disabled = false;
    }
  });

  const quickAccept = document.createElement("button");
  quickAccept.type = "button";
  quickAccept.className = "credentialing-copilot-btn credentialing-copilot-btn--secondary";
  quickAccept.textContent = "Use curated fields only (recommended)";
  quickAccept.addEventListener("click", () => {
    const vo = draftToValidatedOutput(cc.draft_output ?? undefined, pending);
    ta.value = JSON.stringify(Object.keys(vo).length ? vo : {}, null, 2);
  });

  btnRow.appendChild(quickAccept);
  btnRow.appendChild(acceptBtn);
  btnRow.appendChild(submitBtn);
  wrap.appendChild(btnRow);

  if (threadId) {
    const tidNote = document.createElement("div");
    tidNote.className = "credentialing-copilot-hint";
    tidNote.textContent = `Thread ${threadId.slice(0, 8)}… — you can also ask the assistant to validate this step in chat.`;
    wrap.appendChild(tidNote);
  }

  return wrap;
}

/** Render report download block: PDF and/or Markdown with icons. Shown when either is present. */
function renderRosterReportDownload(pdfBase64?: string | null, reportMarkdown?: string | null): HTMLElement {
  const wrap = document.createElement("div");
  wrap.className = "roster-report-download";

  const title = document.createElement("div");
  title.className = "roster-report-download-title";
  title.textContent = "Report";
  wrap.appendChild(title);

  const btns = document.createElement("div");
  btns.className = "roster-report-download-btns";

  const downloadIcon = (): SVGSVGElement => {
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("width", "18");
    svg.setAttribute("height", "18");
    svg.setAttribute("aria-hidden", "true");
    svg.innerHTML = "<path fill='currentColor' d='M5 20h14v-2H5v2zM19 9h-4V3H9v6H5l7 7 7-7z'/>";
    return svg;
  };

  if (pdfBase64 && typeof pdfBase64 === "string" && pdfBase64.length > 0) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "roster-report-download-btn";
    btn.appendChild(downloadIcon());
    btn.appendChild(document.createTextNode(" Download report (PDF)"));
    btn.addEventListener("click", () => {
      try {
        const bytes = Uint8Array.from(atob(pdfBase64), (c) => c.charCodeAt(0));
        const blob = new Blob([bytes], { type: "application/pdf" });
        const url = URL.createObjectURL(blob);
        const a = document.createElement("a");
        a.href = url;
        a.download = "credentialing_report.pdf";
        a.click();
        URL.revokeObjectURL(url);
      } catch (e) {
        console.warn("PDF download failed:", e);
      }
    });
    btns.appendChild(btn);
  }

  if (reportMarkdown && typeof reportMarkdown === "string" && reportMarkdown.trim().length > 0) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "roster-report-download-btn";
    btn.appendChild(downloadIcon());
    btn.appendChild(document.createTextNode(" Download report (Markdown)"));
    btn.addEventListener("click", () => {
      const blob = new Blob([reportMarkdown], { type: "text/markdown;charset=utf-8" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = "credentialing_report.md";
      a.click();
      URL.revokeObjectURL(url);
    });
    btns.appendChild(btn);
  }

  wrap.appendChild(btns);
  return wrap;
}

/** Parse full message into body text and sources (from "Sources:" block). */
function parseMessageAndSources(fullMessage: string): {
  body: string;
  sources: ParsedSource[];
} {
  const raw = (fullMessage ?? "").trim();
  const sourcesIdx = raw.search(/\nSources:\s*\n/i);
  if (sourcesIdx === -1) {
    return { body: raw, sources: [] };
  }
  const body = raw.slice(0, sourcesIdx).trim();
  const afterSources = raw.slice(sourcesIdx).replace(/^\s*Sources:\s*\n/i, "").trim();
  const sources: ParsedSource[] = [];
  // Lines like "  [1] Doc Name (page 2) — snippet..."
  const lineRe = /^\s*\[\s*(\d+)\s*\]\s*(.+?)(?:\s*\(page\s+(\d+)\))?\s*[—–-]\s*(.+)$/gm;
  let m: RegExpExecArray | null;
  while ((m = lineRe.exec(afterSources)) !== null) {
    sources.push({
      index: parseInt(m[1], 10),
      document_name: m[2].trim(),
      page_number: m[3] != null ? parseInt(m[3], 10) : null,
      snippet: (m[4] ?? "").trim(),
    });
  }
  return { body, sources };
}

/** Reusable: user message bubble (right-aligned). */
function renderUserMessage(text: string): HTMLElement {
  const wrap = document.createElement("div");
  wrap.className = "message message--user";
  const bubble = document.createElement("div");
  bubble.className = "message-bubble";
  bubble.textContent = text;
  wrap.appendChild(bubble);
  return wrap;
}

/** Reusable: compact thinking line – streams in one line, collapses to summary when done.
 * Body shows max 3 lines, scrolls so last line is visible; auto-scrolls on each addLine. */
function renderThinkingBlock(
  initialLines: string[],
  opts?: { onExpand?: () => void }
): { el: HTMLElement; setPreview: (text: string) => void; addLine: (line: string) => void; done: (lineCount: number) => void } {
  const block = document.createElement("div");
  block.className = "thinking-block thinking-block--compact" + (initialLines.length ? "" : " collapsed");

  const preview = document.createElement("div");
  preview.className = "thinking-preview";
  preview.setAttribute("role", "button");
  preview.setAttribute("tabindex", "0");
  preview.setAttribute("aria-expanded", initialLines.length > 0 ? "true" : "false");
  const word = document.createElement("span");
  word.className = "thinking-word";
  word.textContent = "Thinking";
  const lineEl = document.createElement("span");
  lineEl.className = "thinking-rule";
  preview.appendChild(word);
  preview.appendChild(lineEl);

  const body = document.createElement("div");
  body.className = "thinking-body";
  initialLines.forEach((line) => {
    const div = document.createElement("div");
    div.className = "thinking-line";
    div.textContent = line;
    body.appendChild(div);
  });

  function collapse(): void {
    block.classList.add("collapsed");
    preview.setAttribute("aria-expanded", "false");
  }
  function toggle(): void {
    block.classList.toggle("collapsed");
    const isExp = !block.classList.contains("collapsed");
    preview.setAttribute("aria-expanded", String(isExp));
    if (isExp) opts?.onExpand?.();
  }

  preview.addEventListener("click", toggle);
  preview.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      toggle();
    }
  });

  block.appendChild(preview);
  block.appendChild(body);

  let lastStatusLine = "";

  return {
    el: block,
    setPreview(text: string) {
      preview.replaceChildren();
      const w = document.createElement("span");
      w.className = "thinking-word";
      w.textContent = thinkingFriendlyStatus(text);
      const r = document.createElement("span");
      r.className = "thinking-rule";
      preview.appendChild(w);
      preview.appendChild(r);
    },
    addLine(line: string) {
      lastStatusLine = line;
      word.textContent = thinkingFriendlyStatus(line);
      const div = document.createElement("div");
      div.className = "thinking-line";
      div.textContent = line;
      body.appendChild(div);
      block.classList.remove("collapsed");
      preview.setAttribute("aria-expanded", "true");
      body.scrollTop = body.scrollHeight;
    },
    done(_lineCount: number) {
      word.textContent = lastStatusLine ? thinkingFriendlyStatus(lastStatusLine) : "Ready";
      block.classList.add("thinking-block--done");
      setTimeout(() => {
        collapse();
      }, 2500);
    },
  };
}

/** Reusable: next questions / follow-ups (clickable — shown outside envelope on legacy turns). */
function renderNextQuestions(
  questions: string[],
  onSelect: (question: string) => void
): HTMLElement {
  if (!questions.length) return document.createElement("div");
  const wrap = document.createElement("div");
  wrap.className = "next-questions";
  const label = document.createElement("div");
  label.className = "next-questions-label";
  label.textContent = "Follow-up questions";
  wrap.appendChild(label);
  const hint = document.createElement("div");
  hint.className = "next-questions-hint";
  hint.textContent = "Tap a line to send it as your next message.";
  wrap.appendChild(hint);
  const chips = document.createElement("div");
  chips.className = "next-questions-chips next-questions-chips--stacked";
  questions.slice(0, 6).forEach((q) => {
    const btn = document.createElement("button");
    btn.type = "button";
    const text = q.trim() || "Ask this";
    btn.className = "next-questions-chip next-questions-chip--row";
    btn.textContent = text;
    btn.setAttribute("aria-label", "Send: " + text);
    btn.addEventListener("click", () => onSelect(text));
    chips.appendChild(btn);
  });
  wrap.appendChild(chips);
  return wrap;
}

/** Reusable: clarification options (buttons/chips for slot fill). */
function renderClarificationOptions(
  opts: ClarificationOption[],
  onSelect: (value: string) => void
): HTMLElement {
  const wrap = document.createElement("div");
  wrap.className = "clarification-options";
  for (const opt of opts) {
    const group = document.createElement("div");
    group.className = "clarification-option-group";
    const labelEl = document.createElement("div");
    labelEl.className = "clarification-option-label";
    labelEl.textContent = opt.label;
    group.appendChild(labelEl);
    const chips = document.createElement("div");
    chips.className = "clarification-option-chips";
    for (const c of opt.choices) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "clarification-option-chip";
      btn.textContent = c.label;
      btn.addEventListener("click", () => onSelect(c.value));
      chips.appendChild(btn);
    }
    group.appendChild(chips);
    wrap.appendChild(group);
  }
  return wrap;
}

/** Reusable: assistant message bubble (left-aligned). Always includes confidence badge. */
function renderAssistantMessage(
  text: string,
  isError?: boolean,
  opts?: { sourceConfidenceStrip?: string }
): HTMLElement {
  const wrap = document.createElement("div");
  wrap.className = "message message--assistant" + (isError ? " message--error" : "");
  const bubble = document.createElement("div");
  bubble.className = "message-bubble";
  bubble.appendChild(
    renderConfidenceBadge((opts?.sourceConfidenceStrip ?? "").trim() || "informational_only")
  );
  const textEl = document.createElement("div");
  textEl.className = "message-bubble-text";
  textEl.textContent = normalizeMessageText(text);
  bubble.appendChild(textEl);
  wrap.appendChild(bubble);
  return wrap;
}

/** Create SVG thumb icon for feedback (grey outline, ChatGPT-style). */
function createThumbIcon(type: "up" | "down"): SVGSVGElement {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("fill", "none");
  svg.setAttribute("stroke", "currentColor");
  svg.setAttribute("stroke-width", "2");
  svg.setAttribute("stroke-linecap", "round");
  svg.setAttribute("stroke-linejoin", "round");
  svg.setAttribute("width", "18");
  svg.setAttribute("height", "18");
  svg.setAttribute("aria-hidden", "true");
  const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
  path.setAttribute(
    "d",
    type === "up"
      ? "M14 9V5a3 3 0 0 0-3-3l-4 9v11h11.28a2 2 0 0 0 2-1.7l1.38-9a2 2 0 0 0-2-2.3zM7 22H4a2 2 0 0 1-2-2v-7a2 2 0 0 1 2-2h3"
      : "M10 15v4a3 3 0 0 0 3 3l4-9V2H5.72a2 2 0 0 0-2 1.7l-1.38 9a2 2 0 0 0 2 2.3zm7-13h2.67A2.31 2.31 0 0 1 22 4v7a2.31 2.31 0 0 1-2.33 2H17"
  );
  svg.appendChild(path);
  return svg;
}

/** Reusable: feedback bar (thumbs up/down, comment dialogue, copy). */
function renderFeedback(correlationId: string): HTMLElement {
  const bar = document.createElement("div");
  bar.className = "feedback";
  const left = document.createElement("div");
  left.className = "feedback-left";
  const actions = document.createElement("div");
  actions.className = "feedback-actions";

  const up = document.createElement("button");
  up.type = "button";
  up.className = "feedback-thumb";
  up.setAttribute("aria-label", "Good response");
  up.appendChild(createThumbIcon("up"));
  const down = document.createElement("button");
  down.type = "button";
  down.className = "feedback-thumb";
  down.setAttribute("aria-label", "Bad response");
  down.appendChild(createThumbIcon("down"));

  const commentArea = document.createElement("div");
  commentArea.className = "feedback-comment-area";
  commentArea.style.display = "none";

  const commentForm = document.createElement("div");
  commentForm.className = "feedback-comment-form";
  const textarea = document.createElement("textarea");
  textarea.placeholder = "What could we improve? (optional)";
  textarea.rows = 2;
  const commentBtns = document.createElement("div");
  commentBtns.className = "feedback-comment-buttons";
  const submitBtn = document.createElement("button");
  submitBtn.type = "button";
  submitBtn.textContent = "Submit";
  const cancelBtn = document.createElement("button");
  cancelBtn.type = "button";
  cancelBtn.textContent = "Cancel";
  commentBtns.appendChild(submitBtn);
  commentBtns.appendChild(cancelBtn);
  commentForm.appendChild(textarea);
  commentForm.appendChild(commentBtns);
  commentArea.appendChild(commentForm);

  function postFeedback(rating: "up" | "down", comment: string | null): void {
    fetch(API_BASE + "/chat/feedback/" + encodeURIComponent(correlationId), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ rating, comment }),
    })
      .then(() => {
        up.disabled = true;
        down.disabled = true;
        up.classList.toggle("selected", rating === "up");
        down.classList.toggle("selected", rating === "down");
        commentArea.style.display = "none";
      })
      .catch(() => {});
  }

  up.addEventListener("click", () => {
    if (up.disabled) return;
    postFeedback("up", null);
  });
  down.addEventListener("click", () => {
    if (down.disabled) return;
    commentArea.style.display = "block";
    textarea.focus();
  });
  submitBtn.addEventListener("click", () => {
    postFeedback("down", textarea.value.trim() || null);
  });
  cancelBtn.addEventListener("click", () => {
    commentArea.style.display = "none";
  });

  const copy = document.createElement("button");
  copy.type = "button";
  copy.setAttribute("aria-label", "Copy");
  copy.textContent = "Copy";
  copy.addEventListener("click", () => {
    const msg = bar.closest(".chat-turn")?.querySelector(".message--assistant .message-bubble");
    if (msg?.textContent) {
      navigator.clipboard.writeText(msg.textContent).then(() => {
        copy.textContent = "Copied";
        setTimeout(() => (copy.textContent = "Copy"), 1500);
      });
    }
  });

  left.appendChild(up);
  left.appendChild(down);
  left.appendChild(commentArea);
  actions.appendChild(copy);
  bar.appendChild(left);
  bar.appendChild(actions);
  return bar;
}

/** RAG deep-link URL for Read tab (document + optional page + optional citation text for highlight). */
function getRagDocumentUrl(
  documentId: string | null | undefined,
  pageNumber: number | null | undefined,
  citeText?: string | null
): string | null {
  const rawBase =
    typeof window !== "undefined"
      ? (window as unknown as { RAG_APP_BASE?: string }).RAG_APP_BASE
      : undefined;
  const base = typeof rawBase === "string" ? rawBase.trim() : "";
  if (!base || !documentId?.trim()) return null;
  const params = new URLSearchParams({ tab: "read", documentId: documentId.trim() });
  if (pageNumber != null) params.set("pageNumber", String(pageNumber));
  const ct = (citeText ?? "").trim().slice(0, 400);
  if (ct) params.set("citeText", ct);
  return `${base.replace(/\/$/, "")}?${params.toString()}`;
}

function resolveSourceOpenHref(s: ParsedSource): string | null {
  if (s.open_href && isAllowedOpenHref(s.open_href)) return s.open_href.trim();
  const cite = (s.cite_text ?? "").trim() || (s.snippet ?? "").trim().slice(0, 400);
  return getRagDocumentUrl(s.document_id, s.page_number, cite || null);
}

/** Open document: RAG URL in new tab if available; else no-op. */
function openDocumentOrSnippet(s: {
  document_id?: string | null;
  document_name: string;
  page_number?: number | null;
  snippet: string;
  cite_text?: string | null;
}): void {
  const cite = (s.cite_text ?? "").trim() || (s.snippet ?? "").trim().slice(0, 400);
  const url = getRagDocumentUrl(s.document_id, s.page_number, cite || null);
  if (url) {
    window.open(url, "_blank", "noopener,noreferrer");
  }
}

/** localStorage "1"/"0" overrides profile; unset → use profile activities (admin-style flags). */
const LLM_PERF_LS = "mobius_show_llm_performance";
const LEGACY_LLM_INSIGHTS_LS = "mobius_show_answer_insights";
const LLM_PERF_ACTIVITY = "llm_performance";
const LLM_PERF_ACTIVITY_ALIASES = ["answer_insights", "technical", "developer"] as const;

function getShowLlmPerformance(profile: MobiusChatUserProfile | null): boolean {
  try {
    const v = localStorage.getItem(LLM_PERF_LS) ?? localStorage.getItem(LEGACY_LLM_INSIGHTS_LS);
    if (v === "1") return true;
    if (v === "0") return false;
  } catch {
    /* ignore */
  }
  const acts = profile?.activities ?? [];
  if (acts.includes(LLM_PERF_ACTIVITY)) return true;
  return LLM_PERF_ACTIVITY_ALIASES.some((a) => acts.includes(a));
}

/** Admin + failed QA: hide source confidence (QA panel carries the verdict). */
function adminShouldSuppressConfidenceForQc(
  profile: MobiusChatUserProfile | null,
  qc: QcAuditInfo | undefined
): boolean {
  if (!getShowLlmPerformance(profile)) return false;
  if (!qc || typeof qc.passed !== "boolean") return false;
  return qc.passed === false;
}

function removeConfidenceBadgesInTurn(turnWrap: HTMLElement): void {
  turnWrap.querySelectorAll(".confidence-badge-wrap").forEach((el) => el.remove());
}

function confidenceFromStrip(strip: string | null | undefined): string {
  const s = (strip || "").toLowerCase().replace(/_/g, "_");
  if (!s) return "medium";
  if (s.includes("authoritative") || s.includes("approved") && !s.includes("caution")) return "high";
  if (s.includes("no_sources") || s.includes("informational_only")) return "low";
  if (s.includes("caution") || s.includes("augmented")) return "medium";
  return "medium";
}

function formatCostShort(n: number): string {
  if (n <= 0) return "0.000";
  if (n < 0.0001) return n.toFixed(6);
  if (n < 0.01) return n.toFixed(4);
  return n.toFixed(3);
}

/** Transparency: server sends per-call router_reason; fallback text if missing. */
function formatRouterNote(meta: LlmPerformanceMeta | undefined, rows: AnswerInsightRow[]): string {
  const fromMeta = meta?.router_by_stage;
  if (fromMeta && fromMeta.length > 0) {
    const lines: string[] = ["Why these models were picked (per LLM call):"];
    fromMeta.forEach((x) => {
      const bits: string[] = [];
      if (x.mode) bits.push(x.mode);
      if (x.exploration) bits.push("exploration round");
      if (x.circuit_relief) bits.push("circuit relief");
      const tag = bits.length ? `[${bits.join(" · ")}] ` : "";
      let comp = "";
      if (x.composite_pg != null || x.composite_call != null) {
        const pg =
          x.composite_pg != null && Number.isFinite(Number(x.composite_pg))
            ? Number(x.composite_pg).toFixed(2)
            : "—";
        const pc =
          x.composite_call != null && Number.isFinite(Number(x.composite_call))
            ? Number(x.composite_call).toFixed(2)
            : "—";
        comp = ` composite PG/call ${pg}/${pc}.`;
      }
      lines.push(
        `• ${(x.stage || "?").toString()} · ${(x.model || "?").toString()}: ${tag}${(x.reason || "—").toString()}${comp}`
      );
    });
    return lines.join("\n");
  }
  const intRow = [...rows].reverse().find((r) => r.stage === "integrator");
  const intModel = intRow?.model || meta?.primary_model || "—";
  const explore = meta?.integrator_exploration;
  const reactN = rows.filter((r) => (r.stage || "").startsWith("react_")).length;
  const conf =
    explore === true ? "medium, exploration band" : explore === false ? "building, exploitation" : "routing";
  if (meta?.pipeline === "legacy") {
    return `[LEGACY] Plan → resolve path (no ReAct tool rounds). Integrator: ${intModel}. Forced exploration (every 20 stage calls) applies on enabled pipelines.`;
  }
  let t = `Router decision — integrator: ${intModel} selected (confidence ${conf}`;
  t += explore === true ? "; model still gathering quality samples in router band." : ").";
  if (reactN > 0) {
    t += ` ReAct: ${reactN} reasoning round(s). Exploration round uses least-sampled model periodically (interval 20) for A/B calibration — compare stages in llm_calls.`;
  }
  t +=
    " Stage table “Composite PG / call”: batch score at router pick vs same formula on this call (latency, cost, QA, error). Thompson blends priors with the batch composite (not QA alone).";
  return t;
}

function escapeHtml(s: string): string {
  return (s ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function parseScoreValue(v: unknown): number | undefined {
  if (typeof v === "number" && Number.isFinite(v)) return Math.max(0, Math.min(1, v));
  if (typeof v === "string" && v.trim()) {
    const n = parseFloat(v);
    if (Number.isFinite(n)) return Math.max(0, Math.min(1, n));
  }
  return undefined;
}

/** Display score: user override wins, else automated, else PASS/FAIL → 1/0. */
function effectiveQcScore(qc: QcAuditInfo | undefined): number | null {
  if (!qc) return null;
  const u = parseScoreValue(qc.user_score as unknown);
  if (u !== undefined) return u;
  const a =
    parseScoreValue(qc.automated_score as unknown) ?? parseScoreValue(qc.score as unknown);
  if (a !== undefined) return a;
  return qc.passed ? 1 : 0;
}

function formatRubricDimensionLabel(key: string): string {
  return key
    .replace(/_/g, " ")
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

function getSubScoreEntries(qc: QcAuditInfo): [string, number][] {
  const raw = qc.sub_scores;
  if (!raw || typeof raw !== "object") return [];
  return Object.keys(raw)
    .sort()
    .map((k) => {
      const n = parseScoreValue((raw as Record<string, unknown>)[k]);
      return n !== undefined ? ([k, n] as [string, number]) : null;
    })
    .filter((x): x is [string, number] => x != null);
}

/** Matrix + rubric table + raw response — rebuilt on poll / save. */
function buildAdjudicatorDetailWrap(qc: QcAuditInfo): HTMLElement {
  const wrap = document.createElement("div");
  wrap.className = "adjudicator-scorecard-detail-wrap";

  const hSum = document.createElement("div");
  hSum.className = "adjudicator-scorecard-section-label";
  hSum.textContent = "Score summary";
  wrap.appendChild(hSum);

  const auto =
    parseScoreValue(qc.automated_score as unknown) ??
    parseScoreValue(qc.score as unknown) ??
    (qc.passed ? 1 : 0);
  const user = parseScoreValue(qc.user_score as unknown);
  const eff = effectiveQcScore(qc);

  const tbl = document.createElement("table");
  tbl.className = "adjudicator-scorecard-matrix";
  const addRow = (label: string, val: string): void => {
    const tr = document.createElement("tr");
    const th = document.createElement("th");
    th.textContent = label;
    const td = document.createElement("td");
    td.className = "adjudicator-scorecard-matrix-val";
    td.textContent = val;
    tr.appendChild(th);
    tr.appendChild(td);
    tbl.appendChild(tr);
  };
  addRow("Automated (overall)", auto.toFixed(2));
  addRow("User override", user !== undefined ? user.toFixed(2) : "—");
  addRow("Effective (displayed)", eff !== null ? eff.toFixed(2) : "—");
  if (user !== undefined) {
    const delta = user - auto;
    const sign = delta >= 0 ? "+" : "";
    addRow("Δ (user − automated)", `${sign}${delta.toFixed(2)}`);
  }
  wrap.appendChild(tbl);

  const hSub = document.createElement("div");
  hSub.className = "adjudicator-scorecard-section-label";
  hSub.textContent = "Rubric sub-scores";
  wrap.appendChild(hSub);

  const entries = getSubScoreEntries(qc);
  if (entries.length === 0) {
    const p = document.createElement("p");
    p.className = "adjudicator-scorecard-subscores-empty";
    p.textContent =
      "No rubric dimensions in this audit (older run, or adjudicator did not return JSON sub_scores).";
    wrap.appendChild(p);
  } else {
    const stbl = document.createElement("table");
    stbl.className = "adjudicator-scorecard-subscores";
    entries.forEach(([k, v]) => {
      const tr = document.createElement("tr");
      const th = document.createElement("th");
      th.textContent = formatRubricDimensionLabel(k);
      const td = document.createElement("td");
      const inner = document.createElement("div");
      inner.className = "adjudicator-scorecard-subscore-cell-inner";
      const pct = Math.round(Math.max(0, Math.min(1, v)) * 100);
      const valSpan = document.createElement("span");
      valSpan.className = "adjudicator-scorecard-subscore-val";
      valSpan.textContent = v.toFixed(2);
      const barWrap = document.createElement("span");
      barWrap.className = "adjudicator-scorecard-subscore-bar-wrap";
      const bar = document.createElement("span");
      bar.className = "adjudicator-scorecard-subscore-bar";
      bar.style.width = `${pct}%`;
      barWrap.appendChild(bar);
      inner.appendChild(valSpan);
      inner.appendChild(barWrap);
      td.appendChild(inner);
      tr.appendChild(th);
      tr.appendChild(td);
      stbl.appendChild(tr);
    });
    wrap.appendChild(stbl);
  }

  const hasTech =
    (qc.adjudicator_model && String(qc.adjudicator_model).trim()) ||
    (qc.adjudicator_llm_call_id && String(qc.adjudicator_llm_call_id).trim());
  if (hasTech) {
    const metaTech = document.createElement("div");
    metaTech.className = "adjudicator-scorecard-tech";
    if (qc.adjudicator_model && String(qc.adjudicator_model).trim()) {
      const line = document.createElement("div");
      line.className = "adjudicator-scorecard-tech-line";
      line.textContent = `Adjudicator model: ${String(qc.adjudicator_model).trim()}`;
      metaTech.appendChild(line);
    }
    if (qc.adjudicator_llm_call_id && String(qc.adjudicator_llm_call_id).trim()) {
      const line = document.createElement("div");
      line.className = "adjudicator-scorecard-tech-line adjudicator-scorecard-tech-line--mono";
      line.textContent = `Adjudicator call id: ${String(qc.adjudicator_llm_call_id).trim()}`;
      metaTech.appendChild(line);
    }
    wrap.appendChild(metaTech);
  }

  const raw = (qc.adjudicator_full_response || "").toString().trim();
  if (raw) {
    const det = document.createElement("details");
    det.className = "adjudicator-scorecard-raw-details";
    const summ = document.createElement("summary");
    summ.textContent = "Full adjudicator response (raw)";
    const pre = document.createElement("pre");
    pre.className = "adjudicator-scorecard-pre adjudicator-scorecard-pre--raw";
    pre.textContent = raw.slice(0, 8000);
    det.appendChild(summ);
    det.appendChild(pre);
    wrap.appendChild(det);
  }

  return wrap;
}

/** Detect usage_breakdown changes when row count is unchanged (e.g. per-stage QA scores merged). */
function llmUsageBreakdownPatchSig(rows: AnswerInsightRow[]): string {
  return rows
    .map(
      (r) =>
        `${r.llm_call_id ?? ""}:${r.quality_score ?? ""}:${(r.quality_source ?? "").slice(0, 32)}:${r.router_composite_at_pick ?? ""}:${r.per_call_composite ?? ""}`
    )
    .join("|");
}

function formatCompositeTooltip(
  pg: number | null,
  pgBrk: Record<string, unknown> | undefined,
  pc: number | null,
  pcBrk: Record<string, unknown> | undefined
): string {
  const lines: string[] = [
    "Composite = q×0.25 + rel×0.25 + latTerm×0.25 + costTerm×0.25.",
    "Linear caps depend on stage type (planner/rag/integrator/cheap stages, …).",
    "PG @ pick: p95 latency + avg cost vs those caps; per-call: this latency vs cap.",
    "Per-call cost term uses list $ from input/output tokens × registered $/1K when tokens > 0, else billed cost.",
    "rel=0 if call_status=error (per-call) or from batch hard_error_rate (PG).",
  ];
  if (pg !== null) {
    lines.push(`PG @ pick: ${pg.toFixed(3)}`);
    if (pgBrk && Object.keys(pgBrk).length) lines.push(JSON.stringify(pgBrk));
  } else lines.push("PG @ pick: — (no stats row yet)");
  if (pc !== null) {
    lines.push(`This call: ${pc.toFixed(3)}`);
    if (pcBrk && Object.keys(pcBrk).length) lines.push(JSON.stringify(pcBrk));
  }
  return lines.join("\n");
}

/** Build stage-breakdown rows (used on first render and when poll merges late rows e.g. adjudicator). */
function fillLlmPerformanceTbody(tbody: HTMLElement, rows: AnswerInsightRow[]): void {
  const maxLat = Math.max(1, ...rows.map((r) => Math.max(0, Number(r.latency_ms) || 0)));
  tbody.replaceChildren();
  rows.forEach((r) => {
    const tr = document.createElement("tr");
    const stageName = (r.display_stage || r.stage || "—").trim();
    const latMs = Math.max(0, Number(r.latency_ms) || 0);
    const latSec = latMs > 0 ? (latMs / 1000).toFixed(1) : "—";
    const rowCost = r.cost_usd != null && Number(r.cost_usd) > 0 ? formatCostShort(Number(r.cost_usd)) : "0.000";
    const pct = maxLat > 0 ? Math.round((latMs / maxLat) * 100) : 0;
    const rawStatus = (r.call_status || "ok").toLowerCase();
    const stClass = rawStatus === "error" ? "llm-performance-status--error" : "llm-performance-status--ok";
    const stLabel = rawStatus === "error" ? "Error" : "OK";
    const whyFull = (r.router_reason || "").trim();
    const mode = (r.router_selection || "").trim();
    const qSamples = r.router_quality_samples_at_pick;
    const qAvg = r.router_avg_quality_at_pick;
    let whyLine = "";
    if (mode) whyLine += `[${mode}] `;
    if (r.router_exploration_round) whyLine += "exploration · ";
    if (r.router_circuit_relief) whyLine += "circuit relief · ";
    if (qSamples != null && Number.isFinite(qSamples))
      whyLine += `PG samples=${qSamples}${qAvg != null && Number.isFinite(qAvg) ? ` · avgQ≈${Number(qAvg).toFixed(2)}` : ""} · `;
    whyLine += whyFull || "—";
    const whyShort = whyLine.length > 140 ? whyLine.slice(0, 137) + "…" : whyLine;
    const whyTitle = escapeHtml(whyLine.length > 200 ? whyLine.slice(0, 2000) : whyLine);
    const qRaw = r.quality_score;
    const qNum = qRaw != null && Number.isFinite(Number(qRaw)) ? Number(qRaw) : null;
    const qDisp = qNum !== null ? qNum.toFixed(2) : "—";
    const qSrc = (r.quality_source || "").trim();
    const qTitle = escapeHtml(qSrc ? qSrc.slice(0, 500) : "");
    const pgN =
      r.router_composite_at_pick != null && Number.isFinite(Number(r.router_composite_at_pick))
        ? Number(r.router_composite_at_pick)
        : null;
    const pcN =
      r.per_call_composite != null && Number.isFinite(Number(r.per_call_composite))
        ? Number(r.per_call_composite)
        : null;
    const pgBrk = r.router_composite_breakdown as Record<string, unknown> | undefined;
    const pcBrk = r.per_call_composite_breakdown as Record<string, unknown> | undefined;
    const compTitle = escapeHtml(
      formatCompositeTooltip(pgN, pgBrk, pcN, pcBrk).slice(0, 3500)
    );
    const compShort =
      (pgN !== null ? pgN.toFixed(2) : "—") + " / " + (pcN !== null ? pcN.toFixed(2) : "—");
    tr.innerHTML = `<td>${escapeHtml(stageName)}</td><td class="llm-performance-mono">${escapeHtml(
      (r.model || "—").trim()
    )}</td><td class="llm-performance-why" title="${whyTitle}">${escapeHtml(whyShort)}</td><td class="llm-performance-lat-cell"><span class="llm-performance-lat-bar-wrap"><span class="llm-performance-lat-bar" style="width:${pct}%"></span></span><span class="llm-performance-lat-num">${latSec}${
      latSec !== "—" ? "s" : ""
    }</span></td><td class="llm-performance-mono">$${rowCost}</td><td class="llm-performance-composite-cell" title="${compTitle}">${escapeHtml(
      compShort
    )}</td><td class="llm-performance-qa-cell" title="${qTitle}">${escapeHtml(
      qDisp
    )}</td><td class="llm-performance-status-cell"><span class="${stClass}">${escapeHtml(
      stLabel
    )}</span></td>`;
    tbody.appendChild(tr);
  });
}

/**
 * Adjudicator / QA scorecard — same collapsible rhythm as LLM performance (admin-gated by caller).
 */
function renderAdjudicatorScorecard(
  qc: QcAuditInfo,
  correlationId: string,
  technicalFeedback?: TechnicalFeedback | null
): HTMLElement {
  const wrap = document.createElement("div");
  wrap.className = "adjudicator-scorecard collapsed";

  const auto = parseScoreValue(qc.automated_score as unknown) ?? parseScoreValue(qc.score as unknown);
  const userS = parseScoreValue(qc.user_score as unknown);
  const effective = effectiveQcScore(qc);
  const effStr = effective !== null ? effective.toFixed(2) : "—";
  const autoStr = auto !== undefined ? auto.toFixed(2) : qc.passed ? "1.00" : "0.00";
  const vUi = adjudicationVerdictUi(qc);

  const preview = document.createElement("div");
  preview.className = "adjudicator-scorecard-preview";
  preview.setAttribute("role", "button");
  preview.setAttribute("tabindex", "0");
  preview.setAttribute("aria-expanded", "false");

  const titleEl = document.createElement("span");
  titleEl.className = "adjudicator-scorecard-title";
  titleEl.textContent = "QA / Adjudicator";

  const oneline = document.createElement("span");
  oneline.className = "adjudicator-scorecard-oneline";
  oneline.dataset.effective = effStr;
  oneline.textContent = `${vUi.shortLabel} · score ${effStr} · ${(qc.source || "—").toString().slice(0, 24)}`;

  const chev = document.createElement("span");
  chev.className = "adjudicator-scorecard-chevron";
  chev.setAttribute("aria-hidden", "true");
  chev.textContent = "▼";

  preview.appendChild(titleEl);
  preview.appendChild(oneline);
  preview.appendChild(chev);

  const body = document.createElement("div");
  body.className = "adjudicator-scorecard-body";

  const badges = document.createElement("div");
  badges.className = "adjudicator-scorecard-badges";
  const b1 = document.createElement("span");
  b1.className = `adjudicator-scorecard-badge adjudicator-scorecard-badge--${vUi.badgeVariant}`;
  b1.textContent = vUi.verdictBadgeText;
  const b2 = document.createElement("span");
  b2.className = "adjudicator-scorecard-badge adjudicator-scorecard-badge--score";
  b2.textContent = `Effective score: ${effStr}`;
  const b3 = document.createElement("span");
  b3.className = "adjudicator-scorecard-badge adjudicator-scorecard-badge--auto";
  b3.textContent = `Automated: ${autoStr}`;
  const b4 = document.createElement("span");
  b4.className = "adjudicator-scorecard-badge adjudicator-scorecard-badge--user";
  b4.textContent = userS !== undefined ? `User: ${userS.toFixed(2)}` : "User: —";
  badges.appendChild(b1);
  badges.appendChild(b2);
  badges.appendChild(b3);
  badges.appendChild(b4);
  body.appendChild(badges);

  body.appendChild(buildAdjudicatorDetailWrap(qc));

  const reasonBox = document.createElement("div");
  reasonBox.className = "adjudicator-scorecard-reason";
  reasonBox.innerHTML = `<strong>Rationale</strong><pre class="adjudicator-scorecard-pre">${escapeHtml(
    (qc.reason || "—").toString().slice(0, 4000)
  )}</pre>`;
  body.appendChild(reasonBox);

  const metaRow = document.createElement("div");
  metaRow.className = "adjudicator-scorecard-meta";
  metaRow.textContent = `Source: ${(qc.source || "—").toString()} · ${(qc.audited_at || "—").toString()}`;
  body.appendChild(metaRow);

  const editWrap = document.createElement("div");
  editWrap.className = "adjudicator-scorecard-edit";
  const editLabel = document.createElement("label");
  editLabel.className = "adjudicator-scorecard-edit-label";
  editLabel.htmlFor = `qc-user-score-${correlationId.slice(0, 8)}`;
  editLabel.textContent = "Your score (0–1, persisted)";
  const inputRow = document.createElement("div");
  inputRow.className = "adjudicator-scorecard-edit-row";
  const num = document.createElement("input");
  num.type = "number";
  num.className = "adjudicator-scorecard-score-input";
  num.id = `qc-user-score-${correlationId.slice(0, 8)}`;
  num.min = "0";
  num.max = "1";
  num.step = "0.01";
  num.value =
    userS !== undefined ? String(userS) : effective !== null ? String(Math.round(effective * 100) / 100) : "0.8";
  const saveBtn = document.createElement("button");
  saveBtn.type = "button";
  saveBtn.className = "adjudicator-scorecard-save";
  saveBtn.textContent = "Save score";
  const note = document.createElement("textarea");
  note.className = "adjudicator-scorecard-note";
  note.rows = 2;
  note.placeholder = "Optional note (persisted)";
  note.value = (qc.user_score_comment || "").toString();
  inputRow.appendChild(num);
  inputRow.appendChild(saveBtn);
  editWrap.appendChild(editLabel);
  editWrap.appendChild(inputRow);
  editWrap.appendChild(note);
  body.appendChild(editWrap);

  saveBtn.addEventListener("click", () => {
    const raw = parseFloat(num.value);
    if (Number.isNaN(raw) || raw < 0 || raw > 1) {
      saveBtn.textContent = "0–1 only";
      window.setTimeout(() => {
        saveBtn.textContent = "Save score";
      }, 1500);
      return;
    }
    saveBtn.disabled = true;
    fetch(API_BASE + "/chat/qc-user-score/" + encodeURIComponent(correlationId), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        user_score: raw,
        user_score_comment: note.value.trim() || null,
      }),
    })
      .then((r) => r.json() as Promise<{ qc_audit?: QcAuditInfo }>)
      .then((j) => {
        const nq = j.qc_audit;
        if (nq && typeof nq.passed === "boolean") {
          syncAdjudicatorScorecardDom(wrap, nq, oneline, badges);
          refreshLlmPerformanceQuality(wrap.closest(".chat-turn") as HTMLElement, nq);
        }
        saveBtn.textContent = "Saved";
      })
      .catch(() => {
        saveBtn.textContent = "Error";
      })
      .finally(() => {
        window.setTimeout(() => {
          saveBtn.disabled = false;
          if (saveBtn.textContent === "Saved") saveBtn.textContent = "Save score";
          if (saveBtn.textContent === "Error") saveBtn.textContent = "Save score";
        }, 1200);
      });
  });

  const fbRow = document.createElement("div");
  fbRow.className = "adjudicator-scorecard-feedback";
  const fbLab = document.createElement("span");
  fbLab.className = "adjudicator-scorecard-feedback-label";
  fbLab.textContent = "Adjudicator helpful?";
  const fbTh = document.createElement("div");
  fbTh.className = "adjudicator-scorecard-feedback-thumbs";
  const upF = document.createElement("button");
  upF.type = "button";
  upF.setAttribute("aria-label", "Adjudicator assessment was helpful");
  upF.appendChild(createThumbIcon("up"));
  const downF = document.createElement("button");
  downF.type = "button";
  downF.setAttribute("aria-label", "Adjudicator assessment was not helpful");
  downF.appendChild(createThumbIcon("down"));
  function postAdj(r: "up" | "down"): void {
    fetch(API_BASE + "/chat/adjudication-feedback/" + encodeURIComponent(correlationId), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ rating: r, comment: null }),
    })
      .then(() => {
        upF.disabled = true;
        downF.disabled = true;
        upF.classList.toggle("selected", r === "up");
        downF.classList.toggle("selected", r === "down");
      })
      .catch(() => {});
  }
  upF.addEventListener("click", () => postAdj("up"));
  downF.addEventListener("click", () => postAdj("down"));
  fbTh.appendChild(upF);
  fbTh.appendChild(downF);
  fbRow.appendChild(fbLab);
  fbRow.appendChild(fbTh);
  body.appendChild(fbRow);

  const adjFb = technicalFeedback?.adjudication;
  if (adjFb && (adjFb.rating === "up" || adjFb.rating === "down")) {
    upF.disabled = true;
    downF.disabled = true;
    upF.classList.toggle("selected", adjFb.rating === "up");
    downF.classList.toggle("selected", adjFb.rating === "down");
  }

  const adminNote = document.createElement("p");
  adminNote.className = "adjudicator-scorecard-admin-note";
  adminNote.textContent = "QA / adjudicator details visible to admins only.";
  body.appendChild(adminNote);

  const setExpanded = (exp: boolean): void => {
    if (exp) {
      wrap.classList.remove("collapsed");
      wrap.classList.add("adjudicator-scorecard--expanded");
    } else {
      wrap.classList.add("collapsed");
      wrap.classList.remove("adjudicator-scorecard--expanded");
    }
    preview.setAttribute("aria-expanded", String(exp));
    chev.textContent = exp ? "▲" : "▼";
    oneline.style.display = exp ? "none" : "";
  };
  const toggle = (): void => setExpanded(wrap.classList.contains("collapsed"));
  preview.addEventListener("click", toggle);
  preview.addEventListener("keydown", (e: Event) => {
    const ke = e as KeyboardEvent;
    if (ke.key === "Enter" || ke.key === " ") {
      ke.preventDefault();
      toggle();
    }
  });

  wrap.appendChild(preview);
  wrap.appendChild(body);
  return wrap;
}

/** Update scorecard DOM after user saves or poll returns richer qc_audit. */
function syncAdjudicatorScorecardDom(
  wrap: HTMLElement,
  qc: QcAuditInfo,
  oneline: HTMLElement,
  badgesWrap: HTMLElement
): void {
  const vUi = adjudicationVerdictUi(qc);
  const effective = effectiveQcScore(qc);
  const effStr = effective !== null ? effective.toFixed(2) : "—";
  const auto =
    parseScoreValue(qc.automated_score as unknown) ??
    parseScoreValue(qc.score as unknown) ??
    (qc.passed ? 1 : 0);
  oneline.textContent = `${vUi.shortLabel} · score ${effStr} · ${(qc.source || "—").toString().slice(0, 24)}`;
  oneline.dataset.effective = effStr;
  const spans = badgesWrap.querySelectorAll(".adjudicator-scorecard-badge");
  if (spans[0]) {
    spans[0].className = `adjudicator-scorecard-badge adjudicator-scorecard-badge--${vUi.badgeVariant}`;
    spans[0].textContent = vUi.verdictBadgeText;
  }
  if (spans[1]) spans[1].textContent = `Effective score: ${effStr}`;
  if (spans[2]) spans[2].textContent = `Automated: ${auto.toFixed(2)}`;
  const userS = parseScoreValue(qc.user_score as unknown);
  let userBadge = badgesWrap.querySelector(".adjudicator-scorecard-badge--user") as HTMLElement | null;
  if (!userBadge) {
    userBadge = document.createElement("span");
    userBadge.className = "adjudicator-scorecard-badge adjudicator-scorecard-badge--user";
    badgesWrap.appendChild(userBadge);
  }
  userBadge.textContent = userS !== undefined ? `User: ${userS.toFixed(2)}` : "User: —";
  const detailOld = wrap.querySelector(".adjudicator-scorecard-detail-wrap");
  if (detailOld?.parentNode) {
    detailOld.replaceWith(buildAdjudicatorDetailWrap(qc));
  }
  const pre = wrap.querySelector(".adjudicator-scorecard-reason .adjudicator-scorecard-pre");
  if (pre) pre.textContent = (qc.reason || "—").toString().slice(0, 4000);
  const note = wrap.querySelector(".adjudicator-scorecard-note") as HTMLTextAreaElement | null;
  if (note && qc.user_score_comment != null) note.value = String(qc.user_score_comment);
}

/**
 * LLM performance — same collapsible rhythm as Sources; permission-gated in app.
 * Collapsed: title + one-liner (hidden when expanded). Expanded: badges, stage table w/ latency bars, router note, footer thumbs.
 */
function renderLlmPerformance(
  rows: AnswerInsightRow[],
  meta: LlmPerformanceMeta | undefined,
  opts: {
    qc?: QcAuditInfo | undefined;
    sourceConfidenceStrip?: string | null;
    correlationId: string;
    totalCostFallback?: number;
    inputTokens?: number;
    outputTokens?: number;
    routingFeedback?: { rating: string; comment?: string | null } | null;
  }
): HTMLElement {
  const wrap = document.createElement("div");
  wrap.className = "llm-performance collapsed";

  const primary =
    (meta?.primary_model || "").trim() ||
    [...rows].reverse().find((r) => r.stage === "integrator")?.model ||
    rows[0]?.model ||
    "—";
  const totalMs = meta?.total_latency_ms ?? 0;
  const totalSec = totalMs > 0 ? (totalMs / 1000).toFixed(1) : "0.0";
  const costNum =
    meta?.total_cost_usd != null && meta.total_cost_usd > 0
      ? meta.total_cost_usd
      : opts.totalCostFallback ?? 0;
  const costStr = formatCostShort(Number(costNum) || 0);
  const qc = opts.qc;
  const eqScore = effectiveQcScore(qc ?? undefined);
  const qCollapsed = eqScore !== null ? eqScore.toFixed(2) : "—";
  const legacy = meta?.pipeline === "legacy";

  const preview = document.createElement("div");
  preview.className = "llm-performance-preview";
  preview.setAttribute("role", "button");
  preview.setAttribute("tabindex", "0");
  preview.setAttribute("aria-expanded", "false");

  const titleEl = document.createElement("span");
  titleEl.className = "llm-performance-title";
  titleEl.textContent = "LLM performance";

  const oneline = document.createElement("span");
  oneline.className = "llm-performance-oneline";
  oneline.dataset.m = primary;
  oneline.dataset.s = totalSec;
  oneline.dataset.c = costStr;
  oneline.dataset.legacy = legacy ? "1" : "0";
  oneline.textContent = `${legacy ? "[LEGACY] " : ""}${primary} · ${totalSec}s · $${costStr} · quality ${qCollapsed}`;

  const chev = document.createElement("span");
  chev.className = "llm-performance-chevron";
  chev.setAttribute("aria-hidden", "true");
  chev.textContent = "▼";

  preview.appendChild(titleEl);
  preview.appendChild(oneline);
  preview.appendChild(chev);

  const body = document.createElement("div");
  body.className = "llm-performance-body";

  const badges = document.createElement("div");
  badges.className = "llm-performance-badges";
  const confLabel = confidenceFromStrip(opts.sourceConfidenceStrip ?? null);
  const qBadge = eqScore !== null ? eqScore.toFixed(2) : "—";
  const badgeSpecs: Array<{ className: string; text: string; isQuality?: boolean }> = [
    { className: "llm-performance-badge llm-performance-badge--model", text: primary },
    { className: "llm-performance-badge llm-performance-badge--latency", text: `${totalSec}s total` },
    { className: "llm-performance-badge llm-performance-badge--cost", text: `$${costStr}` },
    {
      className: "llm-performance-badge llm-performance-badge--quality",
      text: `quality ${qBadge}`,
      isQuality: true,
    },
  ];
  badgeSpecs.forEach((b) => {
    const el = document.createElement("span");
    el.className = b.className;
    el.textContent = b.text;
    if (b.isQuality) el.setAttribute("data-llm-badge-quality", "1");
    badges.appendChild(el);
  });
  const confEl = document.createElement("span");
  confEl.className = "llm-performance-badge llm-performance-badge--confidence";
  confEl.textContent = `confidence: ${confLabel}`;
  badges.appendChild(confEl);
  body.appendChild(badges);

  const stageLabel = document.createElement("div");
  stageLabel.className = "llm-performance-section-label";
  stageLabel.textContent = "STAGE BREAKDOWN";
  body.appendChild(stageLabel);

  const tableWrap = document.createElement("div");
  tableWrap.className = "llm-performance-table-wrap";
  const table = document.createElement("table");
  table.className = "llm-performance-table";
  const thead = document.createElement("thead");
  thead.innerHTML =
    "<tr><th>Stage</th><th>Model</th><th>Why this model</th><th>Latency</th><th>Cost</th><th title=\"PG batch composite at pick / per-call composite (hover for terms)\">Composite<br><span class=\"llm-performance-th-sub\">PG / call</span></th><th>QA</th><th>Status</th></tr>";
  table.appendChild(thead);
  const tb = document.createElement("tbody");
  fillLlmPerformanceTbody(tb, rows);
  table.appendChild(tb);
  tableWrap.appendChild(table);
  body.appendChild(tableWrap);

  const tin = opts.inputTokens ?? 0;
  const tout = opts.outputTokens ?? 0;
  if (tin > 0 || tout > 0) {
    const tokFoot = document.createElement("div");
    tokFoot.className = "llm-performance-tokens-foot";
    tokFoot.textContent = `Tokens in / out: ${tin.toLocaleString()} / ${tout.toLocaleString()}`;
    body.appendChild(tokFoot);
  }

  const routerBox = document.createElement("div");
  routerBox.className = "llm-performance-router";
  routerBox.textContent = formatRouterNote(meta, rows);
  body.appendChild(routerBox);

  const j = meta?.jurisdiction;
  const payerSlug = ((j?.payer || "") || "").toLowerCase().replace(/\s+/g, "_");
  const jurisLine = j
    ? `Jurisdiction: payer=${payerSlug || "—"} · state=${(j.state || "—").toString()}`
    : meta?.jurisdiction_summary
      ? `Jurisdiction: ${meta.jurisdiction_summary}`
      : "Jurisdiction: —";
  const cfgShort = (meta?.config_sha || "—").toString().slice(0, 12);
  const top = meta?.top_source;
  const corpusBit = top?.document_name
    ? `Corpus: ${top.document_name}${top.page_number != null ? ` p.${top.page_number}` : ""}${
        top.match_score != null ? ` · match=${Number(top.match_score).toFixed(2)}` : ""
      }`
    : "Corpus: —";

  const footer = document.createElement("div");
  footer.className = "llm-performance-footer";
  const metaCol = document.createElement("div");
  metaCol.className = "llm-performance-footer-meta";
  metaCol.innerHTML = `${escapeHtml(jurisLine)}<br/>Config: ${escapeHtml(cfgShort)} · ${escapeHtml(corpusBit)}`;
  footer.appendChild(metaCol);

  const routeFb = document.createElement("div");
  routeFb.className = "llm-performance-routing-feedback";
  const rfLabel = document.createElement("span");
  rfLabel.className = "llm-performance-routing-label";
  rfLabel.textContent = "Routing correct?";
  const thumbs = document.createElement("div");
  thumbs.className = "llm-performance-routing-thumbs";
  const upB = document.createElement("button");
  upB.type = "button";
  upB.setAttribute("aria-label", "Routing was appropriate");
  upB.appendChild(createThumbIcon("up"));
  const downB = document.createElement("button");
  downB.type = "button";
  downB.setAttribute("aria-label", "Routing was not appropriate");
  downB.appendChild(createThumbIcon("down"));
  const cid = opts.correlationId;
  function postPerf(r: "up" | "down"): void {
    if (!cid) return;
    fetch(API_BASE + "/chat/llm-performance-feedback/" + encodeURIComponent(cid), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ rating: r }),
    })
      .then(() => {
        upB.disabled = true;
        downB.disabled = true;
        upB.classList.toggle("selected", r === "up");
        downB.classList.toggle("selected", r === "down");
      })
      .catch(() => {});
  }
  upB.addEventListener("click", () => postPerf("up"));
  downB.addEventListener("click", () => postPerf("down"));
  thumbs.appendChild(upB);
  thumbs.appendChild(downB);
  routeFb.appendChild(rfLabel);
  routeFb.appendChild(thumbs);
  footer.appendChild(routeFb);
  body.appendChild(footer);

  const adminNote = document.createElement("p");
  adminNote.className = "llm-performance-admin-note";
  adminNote.textContent = "LLM performance visible to admins only.";
  body.appendChild(adminNote);

  const rf = opts.routingFeedback;
  if (rf && (rf.rating === "up" || rf.rating === "down")) {
    upB.disabled = true;
    downB.disabled = true;
    upB.classList.toggle("selected", rf.rating === "up");
    downB.classList.toggle("selected", rf.rating === "down");
  }

  const setExpanded = (exp: boolean): void => {
    if (exp) {
      wrap.classList.remove("collapsed");
      wrap.classList.add("llm-performance--expanded");
    } else {
      wrap.classList.add("collapsed");
      wrap.classList.remove("llm-performance--expanded");
    }
    preview.setAttribute("aria-expanded", String(exp));
    chev.textContent = exp ? "▲" : "▼";
    oneline.style.display = exp ? "none" : "";
  };

  const toggle = (): void => {
    setExpanded(wrap.classList.contains("collapsed"));
  };
  preview.addEventListener("click", toggle);
  preview.addEventListener("keydown", (e: Event) => {
    const ke = e as KeyboardEvent;
    if (ke.key === "Enter" || ke.key === " ") {
      ke.preventDefault();
      toggle();
    }
  });

  wrap.setAttribute("data-usage-rows", String(rows.length));
  wrap.setAttribute("data-usage-sig", llmUsageBreakdownPatchSig(rows));
  wrap.appendChild(preview);
  wrap.appendChild(body);
  return wrap;
}

/** Reusable: source citer – same look as thinking (word + line, muted, collapsed by default). Includes per-source feedback (source card). */
function renderSourceCiter(
  sources: ParsedSource[],
  citedSourceIndices?: number[],
  correlationId?: string | null
): HTMLElement {
  const wrap = document.createElement("div");
  wrap.className = "source-citer collapsed";

  const preview = document.createElement("div");
  preview.className = "source-citer-preview";
  preview.setAttribute("role", "button");
  preview.setAttribute("tabindex", "0");
  preview.setAttribute("aria-expanded", "false");
  const word = document.createElement("span");
  word.className = "source-citer-word";
  word.textContent = sources.length === 1 ? "Sources (1)" : `Sources (${sources.length})`;
  const rule = document.createElement("span");
  rule.className = "source-citer-rule";
  preview.appendChild(word);
  preview.appendChild(rule);
  preview.addEventListener("click", () => {
    wrap.classList.toggle("collapsed");
    preview.setAttribute("aria-expanded", String(!wrap.classList.contains("collapsed")));
  });
  preview.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      wrap.classList.toggle("collapsed");
      preview.setAttribute("aria-expanded", String(!wrap.classList.contains("collapsed")));
    }
  });

  const body = document.createElement("div");
  body.className = "source-citer-body";
  const citedSet = new Set((citedSourceIndices ?? []).map((n) => Number(n)));
  sources.forEach((s) => {
    const item = document.createElement("div");
    const isCited = citedSet.size > 0 && citedSet.has(Number(s.index));
    item.className = "source-item" + (isCited ? " source-item--cited" : "");
    const doc = document.createElement("div");
    doc.className = "source-doc";
    doc.textContent = `[${s.index}] ${s.document_name}` + (s.page_number != null ? ` (page ${s.page_number})` : "");
    item.appendChild(doc);
    if (s.source_type != null || s.match_score != null || s.confidence != null) {
      const metaLine = document.createElement("div");
      metaLine.className = "source-meta";
      const parts: string[] = [];
      if (s.source_type != null && s.source_type !== "") parts.push(`Type: ${s.source_type}`);
      if (s.match_score != null) parts.push(`Match: ${Number(s.match_score).toFixed(2)}`);
      if (s.confidence != null) parts.push(`Confidence: ${Number(s.confidence).toFixed(2)}`);
      metaLine.textContent = parts.join(" · ");
      item.appendChild(metaLine);
    }
    if (s.snippet) {
      const meta = document.createElement("div");
      meta.className = "source-snippet";
      meta.textContent = s.snippet;
      item.appendChild(meta);
    }
    const ragUrl = resolveSourceOpenHref(s);
    const ragApiRaw =
      typeof window !== "undefined"
        ? (window as unknown as { RAG_API_BASE?: string }).RAG_API_BASE
        : undefined;
    const ragApi = typeof ragApiRaw === "string" ? ragApiRaw.trim() : "";
    const docId = s.document_id?.trim();
    if (ragUrl || (ragApi && docId)) {
      const actions = document.createElement("div");
      actions.className = "source-doc-actions";
      if (ragUrl) {
        const link = document.createElement("a");
        link.href = ragUrl;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        link.className = "source-open-doc-link";
        link.textContent = "Open full document";
        link.addEventListener("click", (e) => e.stopPropagation());
        actions.appendChild(link);
      }
      if (ragApi && docId) {
        const dl = document.createElement("a");
        dl.href = `${ragApi.replace(/\/$/, "")}/documents/${encodeURIComponent(docId)}/download/pdf`;
        dl.target = "_blank";
        dl.rel = "noopener noreferrer";
        dl.className = "source-open-doc-link source-download-link";
        dl.textContent = "Download PDF";
        dl.addEventListener("click", (e) => e.stopPropagation());
        actions.appendChild(dl);
      }
      item.appendChild(actions);
    }

    if (correlationId) {
      const feedbackRow = document.createElement("div");
      feedbackRow.className = "source-feedback-row";
      const question = document.createElement("span");
      question.className = "source-feedback-question";
      question.textContent = "Helpful?";
      const thumbs = document.createElement("div");
      thumbs.className = "source-feedback-thumbs";
      const upBtn = document.createElement("button");
      upBtn.type = "button";
      upBtn.setAttribute("aria-label", "Helpful");
      upBtn.appendChild(createThumbIcon("up"));
      const downBtn = document.createElement("button");
      downBtn.type = "button";
      downBtn.setAttribute("aria-label", "Not helpful");
      downBtn.appendChild(createThumbIcon("down"));
      const srcIdx = s.index != null && s.index >= 1 ? s.index : sources.indexOf(s) + 1;
      function postSourceFeedback(r: "up" | "down"): void {
        const cid = correlationId ?? "";
        if (!cid) return;
        fetch(API_BASE + "/chat/source-feedback/" + encodeURIComponent(cid), {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ source_index: srcIdx, rating: r }),
        })
          .then(() => {
            upBtn.disabled = true;
            downBtn.disabled = true;
            upBtn.classList.toggle("selected", r === "up");
            downBtn.classList.toggle("selected", r === "down");
          })
          .catch(() => {});
      }
      upBtn.addEventListener("click", () => postSourceFeedback("up"));
      downBtn.addEventListener("click", () => postSourceFeedback("down"));
      thumbs.appendChild(upBtn);
      thumbs.appendChild(downBtn);
      feedbackRow.appendChild(question);
      feedbackRow.appendChild(thumbs);
      item.appendChild(feedbackRow);
    }

    body.appendChild(item);
  });

  wrap.appendChild(preview);
  wrap.appendChild(body);
  return wrap;
}

/** Render a completed turn from server assistant_envelope v1. */
function renderAssistantFromEnvelope(
  envelope: AssistantEnvelope,
  opts: {
    onFollowupClick?: (q: string) => void;
    sourceConfidenceStrip?: string;
    showConfidenceBadge?: boolean;
    qcAudit?: QcAuditInfo;
    correlationId?: string | null;
    suppressConfidenceForAdminQcFail?: boolean;
  }
): HTMLElement {
  const outer = document.createElement("div");
  outer.className = "assistant-envelope";

  const bubble = document.createElement("div");
  bubble.className = "message-bubble answer-card-bubble";

  let confidenceInjectedAfterDirectAnswer = false;

  for (const block of envelope.blocks || []) {
    if (!block || typeof block !== "object") continue;
    const t = (block as EnvelopeBlock).type;
    if (t === "tool_attribution") {
      const b = block as { label: string; icon: string };
      const chip = document.createElement("div");
      chip.className = "envelope-tool-chip";
      chip.setAttribute("data-icon", b.icon || "search");
      chip.textContent = b.label || "Research";
      bubble.appendChild(chip);
    } else if (t === "direct_answer") {
      const b = block as { markdown: string };
      const chrome = document.createElement("div");
      chrome.className = "envelope-answer-chrome";
      const el = document.createElement("div");
      el.className = "envelope-direct-answer";
      el.textContent = sanitizeDisplayMessage(b.markdown || "");
      chrome.appendChild(el);
      bubble.appendChild(chrome);
      if (opts.showConfidenceBadge !== false && !opts.suppressConfidenceForAdminQcFail) {
        chrome.appendChild(
          renderConfidenceBadge((opts.sourceConfidenceStrip ?? "").trim() || "informational_only")
        );
        confidenceInjectedAfterDirectAnswer = true;
      }
    } else if (t === "detail") {
      const b = block as { markdown: string; collapsed_default?: boolean };
      const details = document.createElement("details");
      details.className = "envelope-detail";
      details.open = b.collapsed_default === false;
      const sum = document.createElement("summary");
      sum.textContent = "Details";
      details.appendChild(sum);
      const body = document.createElement("div");
      body.className = "envelope-detail-body";
      body.innerHTML = simpleMarkdownToHtml(b.markdown || "");
      details.appendChild(body);
      bubble.appendChild(details);
    } else if (t === "chart") {
      const b = block as { title?: string; caption?: string; image_base64: string };
      const wrap = document.createElement("div");
      wrap.className = "envelope-chart";
      if (b.title) {
        const h = document.createElement("div");
        h.className = "envelope-chart-title";
        h.textContent = b.title;
        wrap.appendChild(h);
      }
      const raw = (b.image_base64 || "").trim();
      const src = raw.startsWith("data:") ? raw : "data:image/png;base64," + raw;
      const img = document.createElement("img");
      img.className = "envelope-chart-img report-chart";
      img.src = src;
      img.alt = b.title || "Chart";
      img.loading = "lazy";
      wrap.appendChild(img);
      if (b.caption) {
        const cap = document.createElement("div");
        cap.className = "envelope-chart-caption";
        cap.textContent = b.caption;
        wrap.appendChild(cap);
      }
      bubble.appendChild(wrap);
    } else if (t === "table") {
      const b = block as { headers: string[]; rows: string[][] };
      const table = document.createElement("table");
      table.className = "envelope-table";
      if (b.headers?.length) {
        const thead = document.createElement("thead");
        const tr = document.createElement("tr");
        for (const h of b.headers) {
          const th = document.createElement("th");
          th.textContent = h;
          tr.appendChild(th);
        }
        thead.appendChild(tr);
        table.appendChild(thead);
      }
      const tbody = document.createElement("tbody");
      for (const row of b.rows || []) {
        const tr = document.createElement("tr");
        for (const c of row) {
          const td = document.createElement("td");
          td.textContent = c;
          tr.appendChild(td);
        }
        tbody.appendChild(tr);
      }
      table.appendChild(tbody);
      bubble.appendChild(table);
    } else if (t === "callout") {
      const b = block as { body: string; variant?: string };
      const c = document.createElement("div");
      c.className = "envelope-callout envelope-callout--" + (b.variant || "info");
      c.textContent = b.body || "";
      bubble.appendChild(c);
    } else if (t === "sources") {
      const b = block as {
        refs: Array<{
          index: number;
          title: string;
          page?: number | null;
          snippet?: string;
          document_id?: string | null;
          open?: { kind: string; href: string };
        }>;
      };
      const parsed: ParsedSource[] = (b.refs || []).map((r) => ({
        index: r.index,
        document_name: r.title || "Source",
        document_id: r.document_id ?? null,
        page_number: r.page ?? null,
        snippet: r.snippet ?? "",
        open_href: r.open?.href ?? null,
      }));
      if (parsed.length > 0) {
        bubble.appendChild(renderSourceCiter(parsed, undefined, opts.correlationId ?? null));
      }
    } else if (t === "next_steps") {
      const b = block as { items: string[]; collapsed_default?: boolean };
      const items = (b.items || []).filter((x) => typeof x === "string" && x.trim());
      if (items.length && opts.onFollowupClick) {
        const expanded = b.collapsed_default === false;
        const disclosure = document.createElement("details");
        disclosure.className = "envelope-followups-disclosure";
        disclosure.open = expanded;
        const sum = document.createElement("summary");
        sum.className = "envelope-followups-summary envelope-followups-summary--next-steps";
        sum.textContent = expanded ? "Next steps" : "Next steps (tap to expand)";
        disclosure.appendChild(sum);
        const w = document.createElement("div");
        w.className = "envelope-next-steps";
        const hint = document.createElement("div");
        hint.className = "envelope-next-steps-hint";
        hint.textContent = "Things to try outside this chat. Tap a line to paste into your message.";
        w.appendChild(hint);
        for (const q of items) {
          const btn = document.createElement("button");
          btn.type = "button";
          btn.className = "envelope-step-chip";
          btn.textContent = q.trim();
          btn.addEventListener("click", () => opts.onFollowupClick!(q.trim()));
          w.appendChild(btn);
        }
        disclosure.appendChild(w);
        bubble.appendChild(disclosure);
      }
    } else if (t === "suggested_questions") {
      const b = block as { items: string[]; collapsed_default?: boolean };
      const items = (b.items || []).filter((x) => typeof x === "string" && x.trim());
      if (items.length && opts.onFollowupClick) {
        const expanded = b.collapsed_default === false;
        const disclosure = document.createElement("details");
        disclosure.className = "envelope-followups-disclosure";
        disclosure.open = expanded;
        const sum = document.createElement("summary");
        sum.className = "envelope-followups-summary envelope-followups-summary--suggested";
        sum.textContent = expanded ? "Follow-up questions" : "Follow-up questions (tap to expand)";
        disclosure.appendChild(sum);
        const w = document.createElement("div");
        w.className = "envelope-suggested";
        const hint = document.createElement("div");
        hint.className = "envelope-suggested-hint";
        hint.textContent = "Tap a line to send it as your next message.";
        w.appendChild(hint);
        const chips = document.createElement("div");
        chips.className = "envelope-suggested-chips";
        for (const q of items.slice(0, 6)) {
          const btn = document.createElement("button");
          btn.type = "button";
          btn.className = "envelope-suggested-chip";
          const text = q.trim();
          btn.textContent = text;
          btn.setAttribute("aria-label", "Send: " + text);
          btn.addEventListener("click", () => opts.onFollowupClick!(text));
          chips.appendChild(btn);
        }
        w.appendChild(chips);
        disclosure.appendChild(w);
        bubble.appendChild(disclosure);
      }
    } else if (t === "markdown_report") {
      const b = block as { markdown: string };
      const div = document.createElement("div");
      div.className = "envelope-markdown-report";
      div.innerHTML = rosterStepMarkdownToHtml(b.markdown || "");
      bubble.appendChild(div);
    } else if (t === "attachments") {
      const b = block as { has_pdf?: boolean };
      if (b.has_pdf) {
        const note = document.createElement("div");
        note.className = "envelope-attachments-note";
        note.textContent = "Report attachments available below.";
        bubble.appendChild(note);
      }
    }
  }

  if (
    !confidenceInjectedAfterDirectAnswer &&
    opts.showConfidenceBadge !== false &&
    !opts.suppressConfidenceForAdminQcFail
  ) {
    bubble.appendChild(
      renderConfidenceBadge((opts.sourceConfidenceStrip ?? "").trim() || "informational_only")
    );
  }
  if (opts.qcAudit) bubble.appendChild(renderQcAuditBadge(opts.qcAudit));

  const msg = document.createElement("div");
  msg.className = "message message--assistant answer-card";
  msg.appendChild(bubble);
  outer.appendChild(msg);
  return outer;
}

function scrollToBottom(container: HTMLElement): void {
  container.scrollTop = container.scrollHeight;
}

function run(): void {
  const messagesEl = el("messages");
  const inputEl = el("input") as HTMLInputElement;
  const sendBtn = el("send") as HTMLButtonElement;
  /** Must stay in sync with server thread after upload + each /chat response. */
  let currentThreadId: string | null = null;
  const chatStatusBanner = document.getElementById("chatStatusBanner");
  const chatStatusBannerText = document.getElementById("chatStatusBannerText");
  let chatStatusBannerTimer: ReturnType<typeof setTimeout> | null = null;
  function hideChatStatusBanner(): void {
    if (chatStatusBannerTimer) {
      clearTimeout(chatStatusBannerTimer);
      chatStatusBannerTimer = null;
    }
    chatStatusBanner?.setAttribute("hidden", "");
  }
  function showChatStatusBanner(message: string, autoHideMs = 20000): void {
    if (!chatStatusBanner || !chatStatusBannerText) return;
    if (chatStatusBannerTimer) clearTimeout(chatStatusBannerTimer);
    chatStatusBannerText.textContent = message;
    chatStatusBanner.removeAttribute("hidden");
    if (autoHideMs > 0) {
      chatStatusBannerTimer = setTimeout(() => hideChatStatusBanner(), autoHideMs);
    }
  }
  document.getElementById("chatStatusBannerDismiss")?.addEventListener("click", hideChatStatusBanner);

  function hideRosterUploadReceipt(): void {
    document.getElementById("rosterReceipt")?.setAttribute("hidden", "");
  }

  function showRosterUploadReceipt(data: RosterUploadResponse): void {
    hideChatStatusBanner();
    const root = document.getElementById("rosterReceipt");
    const headline = document.getElementById("rosterReceiptHeadline");
    const sub = document.getElementById("rosterReceiptSub");
    const checksEl = document.getElementById("rosterReceiptChecks");
    const alertsEl = document.getElementById("rosterReceiptAlerts");
    const nextEl = document.getElementById("rosterReceiptNext");
    const metaEl = document.getElementById("rosterReceiptMeta");
    if (!root || !headline || !sub || !checksEl || !alertsEl || !nextEl || !metaEl) return;

    const ack = data.acknowledgment;
    if (ack && Array.isArray(ack.checks) && ack.checks.length > 0) {
      headline.textContent = ack.headline || "Your roster is linked";
      sub.textContent = ack.subhead || "";
      checksEl.replaceChildren();
      for (const c of ack.checks) {
        const li = document.createElement("li");
        const t = document.createElement("span");
        t.className = "roster-receipt__check-title";
        t.textContent = c.title;
        const d = document.createElement("span");
        d.className = "roster-receipt__check-detail";
        d.textContent = c.detail;
        li.appendChild(t);
        li.appendChild(d);
        checksEl.appendChild(li);
      }
      alertsEl.replaceChildren();
      if (ack.alerts && ack.alerts.length > 0) {
        alertsEl.removeAttribute("hidden");
        for (const a of ack.alerts) {
          const div = document.createElement("div");
          div.className =
            a.tone === "warning"
              ? "roster-receipt__alert roster-receipt__alert--warning"
              : "roster-receipt__alert roster-receipt__alert--notice";
          div.textContent = a.message;
          alertsEl.appendChild(div);
        }
      } else {
        alertsEl.setAttribute("hidden", "");
      }
      nextEl.textContent = ack.next_step || "";
    } else {
      headline.textContent = "Upload complete";
      sub.textContent = "Your file was saved to this chat.";
      checksEl.replaceChildren();
      const li = document.createElement("li");
      const t = document.createElement("span");
      t.className = "roster-receipt__check-title";
      t.textContent = "Summary";
      const d = document.createElement("span");
      d.className = "roster-receipt__check-detail";
      d.textContent = `${data.filename ?? "File"} — ${data.row_count ?? 0} row(s) for ${data.org_name ?? ""}. Billing NPI ${data.default_billing_npi || data.org_id || "—"}.`;
      li.appendChild(t);
      li.appendChild(d);
      checksEl.appendChild(li);
      alertsEl.replaceChildren();
      alertsEl.setAttribute("hidden", "");
      nextEl.textContent =
        "Press Send to run reconciliation, or wait if you turned on automatic send after upload.";
    }

    function addMeta(label: string, value: string): void {
      if (!value) return;
      const dt = document.createElement("dt");
      dt.textContent = label;
      const dd = document.createElement("dd");
      dd.textContent = value;
      metaEl.appendChild(dt);
      metaEl.appendChild(dd);
    }
    metaEl.replaceChildren();
    addMeta("File", (data.filename ?? "").trim());
    if (data.row_count_cleansed != null) addMeta("Rows after cleanup", String(data.row_count_cleansed));
    if (data.row_count_resolved != null) addMeta("Rows checked in NPI registry", String(data.row_count_resolved));
    addMeta("Billing NPI", (data.default_billing_npi || data.org_id || "").trim());
    addMeta("Matched organization (registry)", (data.matched_organization_name ?? "").trim());
    if ((data.matched_practice_address ?? "").trim())
      addMeta("Practice address on file", (data.matched_practice_address ?? "").trim());
    addMeta("Process status", (data.process_status ?? "").trim());
    addMeta("Upload ID", (data.upload_id ?? "").trim());
    addMeta("Chat thread ID", (data.thread_id ?? "").trim());
    const rs = data.resolution_summary;
    if (rs && typeof rs === "object") {
      const parts = Object.entries(rs)
        .filter(([, v]) => typeof v === "number" && v > 0)
        .map(([k, v]) => `${k}: ${v}`);
      if (parts.length) addMeta("NPI match breakdown", parts.join(", "));
    }

    const details = root.querySelector("details");
    if (details) details.open = false;

    root.removeAttribute("hidden");
    document.getElementById("chatEmpty")?.classList.add("hidden");
    window.setTimeout(() => root.scrollIntoView({ block: "nearest", behavior: "smooth" }), 80);
  }

  document.getElementById("rosterReceiptDismiss")?.addEventListener("click", hideRosterUploadReceipt);

  const drawer = el("drawer");
  const drawerOverlay = el("drawerOverlay");
  const hamburger = el("hamburger");
  const drawerClose = el("drawerClose");
  const btnConfig = document.getElementById("btnConfig");
  const sidebarUser = document.getElementById("sidebarUser");
  const sidebarUserName = document.getElementById("sidebarUserName");

  const authApiBase = `${API_BASE.replace(/\/$/, "")}/api/v1`;
  const auth = createAuthService({ apiBase: authApiBase, storage: localStorageAdapter });
  const modal = createAuthModal({ auth, showOAuth: true });
  document.body.appendChild(modal.el);
  const styleEl = document.createElement("style");
  styleEl.textContent = AUTH_STYLES;
  document.head.appendChild(styleEl);

  function updateSidebarUser(user: { greeting_name?: string } | null): void {
    if (sidebarUserName)
      sidebarUserName.textContent = user?.greeting_name ?? "Guest";
  }

  let cachedProfile: MobiusChatUserProfile | null = null;

  function syncAnswerInsightsCheckbox(): void {
    const cb = document.getElementById("prefShowAnswerInsights") as HTMLInputElement | null;
    if (!cb) return;
    cb.checked = getShowLlmPerformance(cachedProfile);
  }

  /** When poll/SSE merge adds rows or per-stage QA scores (post-run), refresh the stage table in place. */
  function mergeLlmPerformanceUsageFromPoll(turnWrap: HTMLElement, d: ChatResponse): void {
    const rows = d.usage_breakdown;
    if (!Array.isArray(rows) || rows.length === 0) return;
    if (!getShowLlmPerformance(cachedProfile)) return;
    const panel = turnWrap.querySelector(".llm-performance") as HTMLElement | null;
    if (!panel) return;
    const sig = llmUsageBreakdownPatchSig(rows as AnswerInsightRow[]);
    const prevSig = panel.getAttribute("data-usage-sig") || "";
    if (sig === prevSig) return;
    const tbody = panel.querySelector(".llm-performance-table tbody") as HTMLElement | null;
    if (tbody) fillLlmPerformanceTbody(tbody, rows as AnswerInsightRow[]);
    panel.setAttribute("data-usage-sig", sig);
    panel.setAttribute("data-usage-rows", String(rows.length));
  }

  function ensureAdjudicatorScorecard(
    turnWrap: HTMLElement,
    qc: QcAuditInfo,
    correlationId: string,
    technicalFeedback?: TechnicalFeedback | null
  ): void {
    if (!getShowLlmPerformance(cachedProfile)) return;
    const existing = turnWrap.querySelector(".adjudicator-scorecard") as HTMLElement | null;
    if (!existing) {
      const el = renderAdjudicatorScorecard(qc, correlationId, technicalFeedback ?? null);
      const perf = turnWrap.querySelector(".llm-performance");
      const fb = turnWrap.querySelector(".feedback");
      if (perf) perf.insertAdjacentElement("afterend", el);
      else if (fb) fb.insertAdjacentElement("beforebegin", el);
      else turnWrap.appendChild(el);
      return;
    }
    const oneline = existing.querySelector(".adjudicator-scorecard-oneline") as HTMLElement | null;
    const badges = existing.querySelector(".adjudicator-scorecard-badges") as HTMLElement | null;
    if (oneline && badges) syncAdjudicatorScorecardDom(existing, qc, oneline, badges);
  }

  function mergeTechnicalPanels(turnWrap: HTMLElement, d: ChatResponse): void {
    const qc = d.qc_audit;
    if (!qc || typeof (qc as QcAuditInfo).passed !== "boolean") return;
    const cid = (d.correlation_id || turnWrap.getAttribute("data-correlation-id") || "").trim();
    if (!cid) return;
    ensureAdjudicatorScorecard(turnWrap, qc as QcAuditInfo, cid, d.technical_feedback);
  }

  /** After poll returns DB-backed technical_feedback, reflect routing thumbs if user already voted. */
  function mergeLlmPerformanceRoutingHydrate(turnWrap: HTMLElement, d: ChatResponse): void {
    const lp = d.technical_feedback?.llm_performance;
    if (!lp || (lp.rating !== "up" && lp.rating !== "down")) return;
    const panel = turnWrap.querySelector(".llm-performance") as HTMLElement | null;
    if (!panel) return;
    const buttons = panel.querySelectorAll(".llm-performance-routing-thumbs button");
    const upB = buttons[0] as HTMLButtonElement | undefined;
    const downB = buttons[1] as HTMLButtonElement | undefined;
    if (!upB || !downB) return;
    upB.disabled = true;
    downB.disabled = true;
    upB.classList.toggle("selected", lp.rating === "up");
    downB.classList.toggle("selected", lp.rating === "down");
  }

  auth.on(() => {
    void auth.getUserProfile().then((p: unknown) => {
      cachedProfile = p as MobiusChatUserProfile | null;
      updateSidebarUser(p as MobiusChatUserProfile | null);
      syncAnswerInsightsCheckbox();
    });
  });
  void auth.getUserProfile().then((p: unknown) => {
    cachedProfile = p as MobiusChatUserProfile | null;
    updateSidebarUser(p as MobiusChatUserProfile | null);
    syncAnswerInsightsCheckbox();
  });

  const prefShowAnswerInsights = document.getElementById(
    "prefShowAnswerInsights"
  ) as HTMLInputElement | null;
  prefShowAnswerInsights?.addEventListener("change", () => {
    try {
      localStorage.setItem(LLM_PERF_LS, prefShowAnswerInsights.checked ? "1" : "0");
    } catch {
      /* ignore */
    }
  });

  if (sidebarUser) {
    sidebarUser.addEventListener("click", () => {
      void auth.getUserProfile().then((user: unknown) => {
        modal.open(user ? "account" : "login");
      });
    });
  }

  function openDrawer(): void {
    drawer.classList.add("open");
    drawerOverlay.classList.add("open");
    loadChatConfig();
  }

  function closeDrawer(): void {
    drawer.classList.remove("open");
    drawerOverlay.classList.remove("open");
  }

  const sidebar = document.getElementById("sidebar");
  const mainEl = document.querySelector(".main");
  const sidebarChevron = document.getElementById("sidebarChevron");

  function toggleSidebar(): void {
    if (!sidebar || !mainEl) return;
    const collapsed = sidebar.classList.toggle("sidebar--collapsed");
    mainEl.classList.toggle("sidebar-collapsed", collapsed);
    if (sidebarChevron) {
      sidebarChevron.setAttribute("aria-label", collapsed ? "Expand sidebar" : "Collapse sidebar");
      sidebarChevron.setAttribute("title", collapsed ? "Expand sidebar" : "Collapse sidebar");
    }
  }
  sidebarChevron?.addEventListener("click", toggleSidebar);

  function initSidebarCollapsibles(): void {
    document.querySelectorAll(".sidebar-section-title.sidebar-section-toggle").forEach((titleEl) => {
      const toggle = (): void => {
        const controls = titleEl.getAttribute("aria-controls") || "";
        const body = controls ? document.getElementById(controls) : null;
        if (!body) return;
        const expanded = titleEl.getAttribute("aria-expanded") !== "false";
        const next = !expanded;
        titleEl.setAttribute("aria-expanded", String(next));
        body.classList.toggle("collapsed", !next);
      };
      titleEl.addEventListener("click", (e) => {
        e.preventDefault();
        toggle();
      });
      titleEl.addEventListener("keydown", (e: Event) => {
        const ke = e as KeyboardEvent;
        if (ke.key === "Enter" || ke.key === " ") {
          ke.preventDefault();
          toggle();
        }
      });
    });
  }
  initSidebarCollapsibles();

  hamburger.addEventListener("click", openDrawer);
  drawerClose.addEventListener("click", closeDrawer);
  drawerOverlay.addEventListener("click", closeDrawer);
  const configHistoryViewClose = document.getElementById("configHistoryViewClose");
  if (configHistoryViewClose) {
    configHistoryViewClose.addEventListener("click", () => {
      const viewEl = document.getElementById("configHistoryView");
      if (viewEl) viewEl.style.display = "none";
    });
  }
  if (btnConfig) btnConfig.addEventListener("click", openDrawer);

  setupLlmRouterReportUI();

  function loadConfigHistory(): void {
    const section = document.getElementById("configHistorySection");
    const listEl = document.getElementById("configHistoryList");
    if (!section || !listEl) return;
    fetch(API_BASE + "/chat/config/history?limit=20")
      .then((r) => r.json() as Promise<ConfigHistoryEntry[]>)
      .then((entries) => {
        section.style.display = "";
        listEl.innerHTML = "";
        if (!Array.isArray(entries) || entries.length === 0) {
          listEl.innerHTML =
            '<p class="config-history-empty">No config history yet. Save config or restart the server to record a version.</p>';
          return;
        }
        entries.forEach((entry) => {
          const row = document.createElement("div");
          row.className = "config-history-row";
          const sha = (entry.config_sha ?? "").slice(0, 12);
          const date = entry.created_at ? new Date(entry.created_at).toLocaleString() : "—";
          const meta =
            [entry.model ?? "", entry.provider ?? ""].filter(Boolean).join(" · ") || "—";
          row.innerHTML =
            '<span class="config-history-sha">' +
            sha +
            '</span><span class="config-history-date">' +
            date +
            '</span><span class="config-history-meta">' +
            meta +
            '</span><button type="button" class="config-history-btn" data-sha="' +
            (entry.config_sha ?? "") +
            '" aria-label="View">View</button>';
          const btn = row.querySelector(".config-history-btn");
          if (btn && entry.config_sha) {
            btn.addEventListener("click", () => {
              fetch(API_BASE + "/chat/config/history/" + encodeURIComponent(entry.config_sha!))
                .then((r) => r.json())
                .then((config: unknown) => {
                  const viewEl = document.getElementById("configHistoryView");
                  const bodyEl = document.getElementById("configHistoryViewBody");
                  if (viewEl && bodyEl) {
                    bodyEl.textContent = JSON.stringify(config, null, 2);
                    viewEl.style.display = "";
                  }
                })
                .catch(() => {});
            });
          }
          listEl.appendChild(row);
        });
      })
      .catch(() => {
        if (section) section.style.display = "";
        if (listEl)
          listEl.innerHTML =
            '<p class="config-history-empty">Config history unavailable (e.g. database not connected).</p>';
      });
  }

  function loadChatConfig(): void {
    fetch(API_BASE + "/chat/config")
      .then((r) => r.json() as Promise<ChatConfigResponse>)
      .then((data) => {
        const p = data.prompts ?? {};
        const sysEl = document.getElementById("promptFirstGenSystem");
        const userEl = document.getElementById("promptFirstGenUser");
        if (sysEl) sysEl.textContent = p.first_gen_system ?? "—";
        if (userEl) userEl.textContent = p.first_gen_user_template ?? "—";
        const llm = data.llm ?? {};
        const llmSummary =
          "Provider: " +
          (llm.provider ?? "—") +
          ", Model: " +
          (llm.model ?? "—") +
          (llm.temperature != null ? ", Temp: " + llm.temperature : "");
        const llmEl = document.getElementById("configLlm");
        if (llmEl) llmEl.textContent = llmSummary;
        const drawerSummaryLlm = document.getElementById("drawerSummaryLlm");
        if (drawerSummaryLlm)
          drawerSummaryLlm.textContent = (llm.provider ?? "") + " / " + (llm.model ?? "—");
        const configShaValue = document.getElementById("configShaValue");
        if (configShaValue) configShaValue.textContent = data.config_sha ?? "—";
        const parser = data.parser ?? {};
        const parserEl = document.getElementById("configParser");
        if (parserEl)
          parserEl.textContent =
            "Patient keywords: " +
            (parser.patient_keywords?.length ? parser.patient_keywords.join(", ") : "—");
        const drawerSummaryParser = document.getElementById("drawerSummaryParser");
        if (drawerSummaryParser)
          drawerSummaryParser.textContent =
            parser.patient_keywords?.length
              ? parser.patient_keywords.slice(0, 3).join(", ") +
                (parser.patient_keywords.length > 3 ? "…" : "")
              : "—";
        loadConfigHistory();
      })
      .catch(() => {
        const sysEl = document.getElementById("promptFirstGenSystem");
        const llmEl = document.getElementById("configLlm");
        const drawerSummaryLlm = document.getElementById("drawerSummaryLlm");
        if (sysEl) sysEl.textContent = "Failed to load config.";
        if (llmEl) llmEl.textContent = "Failed to load config.";
        if (drawerSummaryLlm) drawerSummaryLlm.textContent = "Failed to load config.";
      });
  }

  /** Poll fallback when SSE unavailable or stream fails. */
  function pollResponse(
    correlationId: string,
    onThinking: ((line: string) => void) | null,
    onStreamingMessage?: ((text: string) => void) | null
  ): Promise<ChatResponse> {
    return new Promise((resolve, reject) => {
      // 30 min at 400ms poll = 4500 attempts (match backend CHAT_STREAM_TIMEOUT_S for credentialing reports)
      const maxAttempts = 4500;
      let attempts = 0;
      const seenLines = new Set<string>();

      function poll(): void {
        fetch(API_BASE + "/chat/response/" + correlationId)
          .then((r) => r.json() as Promise<ChatResponse>)
          .then((data) => {
            if (data.thinking_log?.length && onThinking) {
              data.thinking_log.forEach((line: string) => {
                if (!seenLines.has(line)) {
                  seenLines.add(line);
                  onThinking(line);
                }
              });
            }
            if (data.message != null && data.message !== "" && onStreamingMessage) {
              onStreamingMessage(data.message);
            }
            if (data.status === "completed" || data.status === "clarification" || data.status === "refinement_ask" || data.status === "failed") {
              resolve(data);
              return;
            }
            attempts++;
            if (attempts >= maxAttempts) {
              reject(new Error("Timeout waiting for response"));
              return;
            }
            setTimeout(poll, 400);
          })
          .catch(reject);
      }
      poll();
    });
  }

  /** Live stream via SSE; falls back to polling if EventSource unavailable or stream fails. */
  function streamResponse(
    correlationId: string,
    onThinking: ((line: string) => void) | null,
    onStreamingMessage: ((text: string) => void) | null
  ): Promise<ChatResponse> {
    if (typeof EventSource === "undefined") {
      return pollResponse(correlationId, onThinking, onStreamingMessage);
    }
    const streamUrl = API_BASE + "/chat/stream/" + encodeURIComponent(correlationId);
    return new Promise((resolve, reject) => {
      let messageSoFar = "";
      let resolved = false;
      const es = new EventSource(streamUrl);
      es.onmessage = (e: MessageEvent) => {
        try {
          const parsed = JSON.parse(e.data as string) as { event: string; data?: unknown };
          const ev = parsed.event;
          const data = (parsed.data ?? {}) as Record<string, unknown>;
          if (ev === "thinking" && data.line != null && onThinking) {
            onThinking(String(data.line));
          } else if (ev === "quality_audit" && data.line != null && onThinking) {
            onThinking(String(data.line));
          } else if (ev === "message" && data.chunk != null && onStreamingMessage) {
            messageSoFar += String(data.chunk);
            onStreamingMessage(messageSoFar);
          } else if (ev === "completed" && data) {
            resolved = true;
            es.close();
            resolve(data as unknown as ChatResponse);
          } else if (ev === "error" && data.message != null) {
            resolved = true;
            es.close();
            reject(new Error(String(data.message)));
          }
        } catch (err) {
          resolved = true;
          es.close();
          reject(err instanceof Error ? err : new Error(String(err)));
        }
      };
      es.onerror = () => {
        es.close();
        if (resolved) return;
        pollResponse(correlationId, onThinking, onStreamingMessage).then(resolve).catch(reject);
      };
    });
  }

  const chatEmpty = document.getElementById("chatEmpty");

  function sendMessage(overrideMessage?: string): void {
    const message = (overrideMessage ?? (inputEl.value ?? "").trim()).trim();
    if (!message) return;
    if (sendBtn.disabled) return;

    if (chatEmpty) chatEmpty.classList.add("hidden");

    messagesEl.querySelectorAll(".thinking-block").forEach((block) => {
      block.classList.add("collapsed");
      const p = block.querySelector(".thinking-preview");
      if (p) p.setAttribute("aria-expanded", "false");
    });

    // 1. User message
    const turnWrap = document.createElement("div");
    turnWrap.className = "chat-turn";
    turnWrap.appendChild(renderUserMessage(message));
    messagesEl.appendChild(turnWrap);
    scrollToBottom(messagesEl);

    if (!overrideMessage) inputEl.value = "";
    updateSendState();
    sendBtn.disabled = true;
    inputEl.disabled = true;

    // 2. Thinking block (compact line, streams then collapses)
    const thinkingLines: string[] = [];
    const { el: thinkingBlockEl, addLine: addThinkingLine, done: thinkingDone } = renderThinkingBlock(["Sending request…"]);
    turnWrap.appendChild(thinkingBlockEl);
    scrollToBottom(messagesEl);

    function addThinkingLineAndScroll(line: string): void {
      thinkingLines.push(line);
      addThinkingLine(line);
      scrollToBottom(messagesEl);
    }

    let messageWrapEl: HTMLElement | null = null;
    /** During stream, do not show raw JSON; show placeholder until final render (AnswerCard or prose). */
    function streamingDisplayText(text: string): string {
      const t = (text ?? "").trim();
      if (t.startsWith("{")) return "Formatting answer…";
      return normalizeMessageText(text);
    }
    function onStreamingMessage(text: string): void {
      const display = streamingDisplayText(sanitizeDisplayMessage(text));
      if (!messageWrapEl) {
        messageWrapEl = renderAssistantMessage(display);
        turnWrap.appendChild(messageWrapEl);
      } else {
        const textEl = messageWrapEl.querySelector(".message-bubble-text");
        if (textEl) textEl.textContent = display;
      }
      scrollToBottom(messagesEl);
    }

    const payload: { message: string; thread_id?: string } = { message };
    if (currentThreadId) payload.thread_id = currentThreadId;
    let activeCorrelationId = "";
    fetch(API_BASE + "/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    })
      .then((r) => r.json() as Promise<ChatPostResponse>)
      .then((data) => {
        if (data.thread_id) currentThreadId = data.thread_id;
        activeCorrelationId = data.correlation_id ?? "";
        addThinkingLineAndScroll("Request sent. Waiting for worker…");
        return streamResponse(data.correlation_id, addThinkingLineAndScroll, onStreamingMessage);
      })
      .then((data) =>
        // Refresh profile before admin-gated UI. Otherwise the first reply can render while
        // cachedProfile is still null (getUserProfile not resolved), hiding LLM performance.
        auth
          .getUserProfile()
          .then((p: unknown) => {
            cachedProfile = p as MobiusChatUserProfile | null;
            syncAnswerInsightsCheckbox();
            return data;
          })
          .catch(() => data)
      )
      .then((data) => {
        // Final thinking lines if any not yet shown
        (data.thinking_log ?? []).forEach((line: string) => {
          if (!thinkingLines.includes(line)) addThinkingLineAndScroll(line);
        });
        const fullMessage = data.message ?? "(No message)";
        const { body, sources } = parseMessageAndSources(fullMessage);

        if (data.response_source === "llm" && data.model_used) {
          addThinkingLineAndScroll("Model: " + data.model_used);
        }
        if (data.response_source === "stub" && data.llm_error) {
          addThinkingLineAndScroll("LLM failed (stub used): " + data.llm_error);
        }

        thinkingDone(thinkingLines.length);

        if (data.thread_id) currentThreadId = data.thread_id;
        const cidForTurn = (data.correlation_id || activeCorrelationId || "").trim();
        if (cidForTurn) turnWrap.setAttribute("data-correlation-id", cidForTurn);

        // 3. Next questions (unified: payload + AnswerCard followups) – computed first so we can suppress inline followups
        let nextQuestions: string[] = Array.isArray(data.next_questions_for_user)
          ? data.next_questions_for_user.filter((x: unknown): x is string => typeof x === "string" && x.trim().length > 0)
          : data.user_ask && String(data.user_ask).trim()
            ? [String(data.user_ask).trim()]
            : [];
        if (nextQuestions.length === 0) {
          const card = tryParseAnswerCard(body || "");
          if (card?.followups?.length) {
            nextQuestions = card.followups
              .map((f) => (f.question || f.reason || f.field || "").trim())
              .filter(Boolean);
          }
        }

        // 4. Assistant message: use roster_report_final_md when present (full report with charts)
        if (messageWrapEl) {
          messageWrapEl.remove();
        }
        const reportMd = data.roster_report_final_md && typeof data.roster_report_final_md === "string" ? data.roster_report_final_md.trim() : "";
        const contentToShow = reportMd.length > 0 ? reportMd : (body || "(No response)");
        const qcFromPayload =
          data.qc_audit && typeof data.qc_audit === "object" && typeof (data.qc_audit as QcAuditInfo).passed === "boolean"
            ? (data.qc_audit as QcAuditInfo)
            : undefined;

        const suppressConf = adminShouldSuppressConfidenceForQc(cachedProfile, qcFromPayload);

        const envCandidate = data.assistant_envelope;
        const useEnvelope =
          envCandidate &&
          typeof envCandidate === "object" &&
          (envCandidate as AssistantEnvelope).version === 1 &&
          Array.isArray((envCandidate as AssistantEnvelope).blocks) &&
          (envCandidate as AssistantEnvelope).blocks.length > 0;

        const envBlocks = useEnvelope ? (envCandidate as AssistantEnvelope).blocks : [];
        const envSourcesBlock = envBlocks.find((b) => (b as { type?: string }).type === "sources") as
          | { type: string; refs?: unknown[] }
          | undefined;
        const envelopeHasSources = useEnvelope && Array.isArray(envSourcesBlock?.refs) && envSourcesBlock!.refs.length > 0;

        if (useEnvelope) {
          turnWrap.appendChild(
            renderAssistantFromEnvelope(envCandidate as AssistantEnvelope, {
              onFollowupClick: (q) => sendMessage(q),
              sourceConfidenceStrip: (data.source_confidence_strip ?? "").trim() || undefined,
              showConfidenceBadge: data.status !== "clarification" && data.status !== "refinement_ask",
              qcAudit: qcFromPayload,
              correlationId: cidForTurn || null,
              suppressConfidenceForAdminQcFail: suppressConf,
            })
          );
        } else {
          turnWrap.appendChild(
            renderAssistantContent(contentToShow, !!data.llm_error, {
              onFollowupClick: (q) => sendMessage(q),
              sourceConfidenceStrip: (data.source_confidence_strip ?? "").trim() || undefined,
              showConfidenceBadge: data.status !== "clarification" && data.status !== "refinement_ask",
              suppressFollowups: nextQuestions.length > 0,
              nextQuestions,
              renderAsMarkdown: reportMd.length > 0 || !!(data.roster_report_final_md && (body || "").trim().length > 50),
              qcAudit: qcFromPayload,
              suppressConfidenceForAdminQcFail: suppressConf,
            })
          );
        }

        const mergeQc = (d: ChatResponse): void => {
          const q =
            d.qc_audit && typeof d.qc_audit === "object" && typeof (d.qc_audit as QcAuditInfo).passed === "boolean"
              ? (d.qc_audit as QcAuditInfo)
              : undefined;
          if (q) {
            applyQcAuditToTurn(turnWrap, q);
            if (adminShouldSuppressConfidenceForQc(cachedProfile, q)) removeConfidenceBadgesInTurn(turnWrap);
          }
        };
        mergeQc(data);
        // Post-run QA finishes *after* the worker publishes "completed", and SSE closes then — so qc_audit
        // is usually missing on the first payload. Poll GET /chat/response for a while (DB + Redis merge qc).
        if (activeCorrelationId) {
          const refetchMerged = (): void => {
            if (!document.body.contains(turnWrap)) return;
            fetch(API_BASE + "/chat/response/" + encodeURIComponent(activeCorrelationId))
              .then((r) => r.json() as Promise<ChatResponse>)
              .then((d) => {
                mergeQc(d);
                mergeLlmPerformanceUsageFromPoll(turnWrap, d);
                mergeTechnicalPanels(turnWrap, d);
                mergeLlmPerformanceRoutingHydrate(turnWrap, d);
              })
              .catch(() => {});
          };
          const qcRefetchDelaysMs = [800, 2500, 6000, 12000, 25000, 45000, 75000, 120000];
          qcRefetchDelaysMs.forEach((ms) => window.setTimeout(refetchMerged, ms));
        }

        // 5. Roster step outputs (collapsible, for validation)
        const rosterStepOutputs = data.roster_step_outputs;
        if (Array.isArray(rosterStepOutputs) && rosterStepOutputs.length > 0) {
          turnWrap.appendChild(renderRosterStepOutputs(rosterStepOutputs));
        }

        const credCop = data.credentialing_copilot;
        if (credCop && typeof credCop === "object" && typeof credCop.run_id === "string" && credCop.run_id.length > 0) {
          turnWrap.appendChild(renderCredentialingCopilotPanel(credCop as CredentialingCopilotPayload, data.thread_id ?? currentThreadId));
        }

        // 5b. Roster report download (PDF and/or Markdown)
        const pdfBase64 = data.roster_report_pdf_base64;
        const reportMarkdown = data.roster_report_final_md;
        if ((pdfBase64 && typeof pdfBase64 === "string" && pdfBase64.length > 0) || (reportMarkdown && typeof reportMarkdown === "string" && reportMarkdown.trim().length > 0)) {
          turnWrap.appendChild(renderRosterReportDownload(pdfBase64, reportMarkdown));
        }

        // 6. Next questions block (only when NOT an AnswerCard – card renders them inside; envelope has its own chips)
        const isCard = !!tryParseAnswerCard(body || "");
        if (nextQuestions.length > 0 && !isCard && !useEnvelope) {
          turnWrap.appendChild(
            renderNextQuestions(nextQuestions, (q) => sendMessage(q))
          );
        }

        // 7. Clarification options (clickable buttons for slot fill)
        if (data.clarification_options && data.clarification_options.length > 0) {
          turnWrap.appendChild(
            renderClarificationOptions(data.clarification_options, (value) => sendMessage(value))
          );
        }

        // 8. Sources: prefer API response.sources (from RAG) so source cards show even when integrator drops them
        const sourceList: ParsedSource[] =
          data.sources && data.sources.length > 0
            ? (data.sources as Array<{
                index?: number;
                document_name?: string;
                document_id?: string | null;
                page_number?: number | null;
                text?: string;
                cite_text?: string | null;
                source_type?: string | null;
                match_score?: number | null;
                confidence?: number | null;
                open_href?: string | null;
              }>).map((s) => ({
                index: s.index ?? 0,
                document_name: s.document_name ?? "document",
                document_id: s.document_id ?? null,
                page_number: s.page_number ?? null,
                snippet: (s.text ?? "").slice(0, 200),
                cite_text: (s.cite_text ?? s.text ?? "").trim().slice(0, 400) || null,
                source_type: s.source_type ?? null,
                match_score: s.match_score ?? null,
                confidence: s.confidence ?? null,
                open_href: s.open_href ?? null,
              }))
            : sources.length > 0
              ? sources.map((s) => ({
                  index: s.index ?? 0,
                  document_name: s.document_name ?? "document",
                  document_id: s.document_id ?? null,
                  page_number: s.page_number ?? null,
                  snippet: (s.snippet ?? "").slice(0, 120),
                  cite_text: (s.snippet ?? "").trim().slice(0, 400) || null,
                  source_type: null,
                  match_score: null,
                  confidence: null,
                }))
              : [];
        const cited = data.cited_source_indices ?? [];
        if (sourceList.length > 0 && !envelopeHasSources) {
          turnWrap.appendChild(
            renderSourceCiter(sourceList, cited, data.correlation_id ?? activeCorrelationId)
          );
        }

        const insightRows = data.usage_breakdown;
        const perfMeta = data.llm_performance;
        if (
          getShowLlmPerformance(cachedProfile) &&
          Array.isArray(insightRows) &&
          insightRows.length > 0 &&
          data.status === "completed"
        ) {
          const tin = Number(data.tokens_used?.input_tokens) || 0;
          const tout = Number(data.tokens_used?.output_tokens) || 0;
          turnWrap.appendChild(
            renderLlmPerformance(insightRows, perfMeta, {
              qc: qcFromPayload,
              sourceConfidenceStrip: data.source_confidence_strip ?? null,
              correlationId: data.correlation_id ?? activeCorrelationId,
              totalCostFallback: data.cost_usd,
              inputTokens: tin,
              outputTokens: tout,
              routingFeedback: data.technical_feedback?.llm_performance ?? null,
            })
          );
        }

        mergeTechnicalPanels(turnWrap, data);
        mergeLlmPerformanceRoutingHydrate(turnWrap, data);

        // 9. Answer-quality feedback (separate from LLM routing thumbs in performance panel)
        turnWrap.appendChild(renderFeedback(data.correlation_id ?? activeCorrelationId));

        loadSidebarHistory();
        scrollToBottom(messagesEl);
      })
      .catch((err: Error) => {
        thinkingDone(thinkingLines.length);
        turnWrap.appendChild(
          renderAssistantMessage("Error: " + (err?.message ?? String(err)), true, {})
        );
        scrollToBottom(messagesEl);
      })
      .finally(() => {
        sendBtn.disabled = false;
        inputEl.disabled = false;
        updateSendState();
      });
  }

  function updateSendState(): void {
    const hasText = (inputEl.value ?? "").trim().length > 0;
    sendBtn.classList.toggle("active", hasText);
  }

  inputEl.addEventListener("input", updateSendState);
  inputEl.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      sendMessage();
    }
  });

  sendBtn.addEventListener("click", () => sendMessage());

  /** Reset upload UI and show sheet (⋯ → Upload file). */
  function openUploadModal(): void {
    hideRosterUploadReceipt();
    const modal = document.getElementById("uploadModal");
    const overlay = document.getElementById("uploadOverlay");
    const form = document.getElementById("uploadForm");
    const st = document.getElementById("uploadStatus");
    const progressWrap = document.getElementById("uploadProgressWrap");
    form?.removeAttribute("aria-busy");
    modal?.classList.remove("upload-modal--busy");
    if (st) {
      st.textContent = "";
      st.classList.remove("upload-modal-status--working", "upload-modal-status--error");
      st.style.removeProperty("color");
    }
    progressWrap?.setAttribute("hidden", "");
    modal?.removeAttribute("hidden");
    overlay?.classList.add("open");
    (document.getElementById("uploadOrgName") as HTMLInputElement | null)?.focus();
  }

  function setupComposerOptionsMenu(): void {
    const optionsBtn = document.getElementById("composerOptions");
    const optionsMenu = document.getElementById("composerOptionsMenu");
    const uploadItem = document.getElementById("composerOptionUploadFile");
    function hideOptionsMenu(): void {
      optionsMenu?.setAttribute("hidden", "");
      optionsBtn?.setAttribute("aria-expanded", "false");
    }
    optionsBtn?.addEventListener("click", (e) => {
      e.stopPropagation();
      const isOpen = !optionsMenu?.hasAttribute("hidden");
      if (isOpen) {
        hideOptionsMenu();
      } else {
        optionsMenu?.removeAttribute("hidden");
        optionsBtn?.setAttribute("aria-expanded", "true");
      }
    });
    uploadItem?.addEventListener("click", () => {
      hideOptionsMenu();
      openUploadModal();
    });
    document.addEventListener("click", () => hideOptionsMenu());
  }
  setupComposerOptionsMenu();

  function setupUploadModal(): void {
    const uploadModal = document.getElementById("uploadModal");
    const uploadOverlay = document.getElementById("uploadOverlay");
    const uploadForm = document.getElementById("uploadForm") as HTMLFormElement | null;
    const uploadOrgName = document.getElementById("uploadOrgName") as HTMLInputElement | null;
    const uploadFile = document.getElementById("uploadFile") as HTMLInputElement | null;
    const uploadFilePurpose = document.getElementById("uploadFilePurpose") as HTMLSelectElement | null;
    const uploadCancel = document.getElementById("uploadCancel");
    const uploadSubmit = document.getElementById("uploadSubmit") as HTMLButtonElement | null;
    const uploadStatus = document.getElementById("uploadStatus");
    const uploadProgressWrap = document.getElementById("uploadProgressWrap");

    let uploadPhaseTimers: ReturnType<typeof setTimeout>[] = [];
    let uploadAbort: AbortController | null = null;

    function stopUploadPhaseEmits(): void {
      uploadPhaseTimers.forEach((id) => window.clearTimeout(id));
      uploadPhaseTimers = [];
    }

    function startUploadPhaseEmits(purpose: string): void {
      stopUploadPhaseEmits();
      const roster = purpose === "roster_reconciliation";
      const phases = roster
        ? [
            { ms: 0, text: "Step 1 of 3 — Looking up your organization (NPPES / PML)…" },
            { ms: 2800, text: "Step 2 of 3 — Sending file to the roster service…" },
            { ms: 7000, text: "Step 3 of 3 — Parsing rows and resolving NPIs (often 30s–2 min)…" },
            { ms: 45000, text: "Still working — large rosters can take a bit longer…" },
          ]
        : [{ ms: 0, text: "Uploading file…" }];

      phases.forEach(({ ms, text }) => {
        const id = window.setTimeout(() => setStatus(text, false, true), ms);
        uploadPhaseTimers.push(id);
      });
    }

    function hideUploadModal(): void {
      if (uploadAbort) {
        uploadAbort.abort();
        uploadAbort = null;
      }
      stopUploadPhaseEmits();
      uploadModal?.classList.remove("upload-modal--busy");
      uploadForm?.removeAttribute("aria-busy");
      uploadProgressWrap?.setAttribute("hidden", "");
      uploadModal?.setAttribute("hidden", "");
      uploadOverlay?.classList.remove("open");
    }

    function setStatus(msg: string, isError = false, isWorking = false): void {
      if (!uploadStatus) return;
      uploadStatus.textContent = msg;
      uploadStatus.classList.toggle("upload-modal-status--working", Boolean(isWorking) && !isError);
      uploadStatus.classList.toggle("upload-modal-status--error", isError);
      if (isError) {
        uploadStatus.style.setProperty("color", "var(--error-text, var(--error))");
      } else {
        uploadStatus.style.removeProperty("color");
      }
    }

    uploadCancel?.addEventListener("click", hideUploadModal);
    uploadOverlay?.addEventListener("click", hideUploadModal);

    function updateSubmitState(): void {
      const hasFile = !!(uploadFile?.files?.length);
      const hasOrg = !!(uploadOrgName?.value?.trim());
      if (uploadSubmit) uploadSubmit.disabled = !(hasFile && hasOrg);
    }
    uploadOrgName?.addEventListener("input", updateSubmitState);
    uploadFile?.addEventListener("change", updateSubmitState);

    uploadForm?.addEventListener("submit", (e) => {
      e.preventDefault();
      const orgName = uploadOrgName?.value?.trim();
      const file = uploadFile?.files?.[0];
      if (!orgName || !file) return;
      uploadSubmit?.setAttribute("disabled", "");
      uploadModal?.classList.add("upload-modal--busy");
      uploadForm?.setAttribute("aria-busy", "true");
      uploadProgressWrap?.removeAttribute("hidden");
      const purpose = (uploadFilePurpose?.value || "roster_reconciliation").trim();
      startUploadPhaseEmits(purpose);
      const formData = new FormData();
      formData.append("file", file);
      formData.append("org_name", orgName);
      formData.append("file_purpose", purpose);
      if (currentThreadId) formData.append("thread_id", currentThreadId);
      uploadAbort = new AbortController();
      const signal = uploadAbort.signal;
      fetch(API_BASE + "/chat/roster-upload", { method: "POST", body: formData, signal })
        .then((r) => {
          if (!r.ok) return r.json().then((d) => Promise.reject(d?.detail ?? r.statusText));
          return r.json();
        })
        .then((data: RosterUploadResponse) => {
            const org = data.org_name ?? orgName;
            if (data.thread_id) currentThreadId = data.thread_id;
            stopUploadPhaseEmits();
            uploadModal?.classList.remove("upload-modal--busy");
            uploadForm?.removeAttribute("aria-busy");
            uploadProgressWrap?.setAttribute("hidden", "");
            uploadAbort = null;
            showRosterUploadReceipt(data);
            uploadForm?.reset();
            updateSubmitState();
            inputEl.value = `Run reconciliation report for ${org}`;
            updateSendState();
            hideUploadModal();
            const auto = document.getElementById("uploadAutoSendReconciliation") as HTMLInputElement | null;
            if ((uploadFilePurpose?.value || "roster_reconciliation").trim() === "roster_reconciliation" && auto?.checked) {
              window.setTimeout(() => sendMessage(), 0);
            }
          }
        )
        .catch((err: unknown) => {
          const aborted =
            (err instanceof Error && err.name === "AbortError") ||
            (typeof DOMException !== "undefined" && err instanceof DOMException && err.name === "AbortError");
          if (aborted) {
            setStatus("Upload cancelled.", false, false);
            return;
          }
          let msg = "Upload failed";
          if (typeof err === "string") msg = err;
          else if (err && typeof err === "object" && "detail" in err && (err as { detail?: unknown }).detail != null)
            msg = String((err as { detail: unknown }).detail);
          else if (err instanceof Error) msg = err.message;
          setStatus(msg, true);
        })
        .finally(() => {
          uploadAbort = null;
          stopUploadPhaseEmits();
          uploadModal?.classList.remove("upload-modal--busy");
          uploadForm?.removeAttribute("aria-busy");
          uploadProgressWrap?.setAttribute("hidden", "");
          uploadSubmit?.removeAttribute("disabled");
        });
    });
  }
  setupUploadModal();

  const btnNewChat = document.getElementById("btnNewChat");
  if (btnNewChat) {
    btnNewChat.addEventListener("click", () => {
      currentThreadId = null;
      hideChatStatusBanner();
      hideRosterUploadReceipt();
      messagesEl.querySelectorAll(".chat-turn").forEach((n) => n.remove());
      if (chatEmpty) chatEmpty.classList.remove("hidden");
      loadSidebarHistory();
    });
  }

  function loadSidebarHistory(): void {
    const recentList = document.getElementById("recentList");
    const helpfulList = document.getElementById("helpfulList");
    const documentsList = document.getElementById("documentsList");
    if (!recentList) return;

    const snippet = (q: string, max = 80) =>
      (q ?? "").trim().slice(0, max) + ((q ?? "").length > max ? "…" : "");

    Promise.all([
      fetch(API_BASE + "/chat/history/recent?limit=20").then(
        (r) => r.json() as Promise<HistoryTurnItem[]>
      ),
      helpfulList
        ? fetch(API_BASE + "/chat/history/most-helpful-searches?limit=10").then(
            (r) => r.json() as Promise<HistoryTurnItem[]>
          )
        : Promise.resolve([] as HistoryTurnItem[]),
      documentsList
        ? fetch(API_BASE + "/chat/history/most-helpful-documents?limit=10").then(
            (r) => r.json() as Promise<HistoryDocumentItem[]>
          )
        : Promise.resolve([] as HistoryDocumentItem[]),
    ])
      .then(([recent, helpful, documents]) => {
        recentList.innerHTML = "";
        for (const t of recent) {
          const li = document.createElement("li");
          li.className = "recent-item";
          li.textContent = snippet(t.question || "(empty)");
          li.title = t.question || "";
          li.setAttribute("role", "button");
          li.setAttribute("tabindex", "0");
          li.addEventListener("click", () => {
            (inputEl as HTMLInputElement).value = t.question ?? "";
            updateSendState();
          });
          li.addEventListener("keydown", (e) => {
            if (e.key === "Enter" || e.key === " ") {
              e.preventDefault();
              (inputEl as HTMLInputElement).value = t.question ?? "";
              updateSendState();
            }
          });
          recentList.appendChild(li);
        }

        if (helpfulList) {
          helpfulList.innerHTML = "";
          for (const t of helpful) {
            const li = document.createElement("li");
            li.className = "helpful-item";
            li.textContent = snippet(t.question || "(empty)");
            li.title = t.question || "";
            li.setAttribute("role", "button");
            li.setAttribute("tabindex", "0");
            li.addEventListener("click", () => {
              (inputEl as HTMLInputElement).value = t.question ?? "";
              updateSendState();
              sendMessage();
            });
            li.addEventListener("keydown", (e) => {
              if (e.key === "Enter" || e.key === " ") {
                e.preventDefault();
                (inputEl as HTMLInputElement).value = t.question ?? "";
                updateSendState();
                sendMessage();
              }
            });
            helpfulList.appendChild(li);
          }
        }

        if (documentsList) {
          documentsList.innerHTML = "";
          for (const item of documents) {
            const li = document.createElement("li");
            li.className = "documents-item documents-item--clickable";
            const nameSpan = document.createElement("span");
            nameSpan.textContent = item.document_name;
            li.appendChild(nameSpan);
            const n = item.cited_in_count ?? 0;
            if (n > 0) {
              const citedSpan = document.createElement("span");
              citedSpan.className = "documents-item-cited";
              citedSpan.textContent =
                n === 1 ? " — Cited in 1 recent answer." : ` — Cited in ${n} recent answers.`;
              li.appendChild(citedSpan);
            }
            li.title = "View document";
            li.setAttribute("role", "button");
            li.setAttribute("tabindex", "0");
            li.addEventListener("click", () =>
              openDocumentOrSnippet({
                document_id: item.document_id ?? null,
                document_name: item.document_name,
                page_number: null,
                snippet: "",
              })
            );
            li.addEventListener("keydown", (e) => {
              if (e.key === "Enter" || e.key === " ") {
                e.preventDefault();
                openDocumentOrSnippet({
                  document_id: item.document_id ?? null,
                  document_name: item.document_name,
                  page_number: null,
                  snippet: "",
                });
              }
            });
            documentsList.appendChild(li);
          }
        }
      })
      .catch(() => {
        recentList.innerHTML = "";
        if (helpfulList) helpfulList.innerHTML = "";
        if (documentsList) documentsList.innerHTML = "";
      });
  }

  const chatEmptyLanding = document.getElementById("chatEmpty");
  chatEmptyLanding?.addEventListener("click", (e) => {
    const t = (e.target as HTMLElement).closest(".landing-try-link");
    if (!t || !(t instanceof HTMLElement)) return;
    const q = t.getAttribute("data-query")?.trim();
    if (!q) return;
    e.preventDefault();
    inputEl.value = q;
    updateSendState();
    sendMessage();
  });

  try {
    const u = new URL(window.location.href);
    const pq = u.searchParams.get("q")?.trim();
    if (pq) {
      u.searchParams.delete("q");
      const next = u.pathname + (u.search ? u.search : "") + u.hash;
      window.history.replaceState({}, "", next);
      inputEl.value = pq;
      updateSendState();
      sendMessage();
    }
  } catch {
    /* ignore */
  }

  loadSidebarHistory();

  updateSendState();
}

run();

export {};
