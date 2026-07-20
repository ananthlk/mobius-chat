import {
  createAuthService,
  localStorageAdapter,
  createAuthModal,
  createPreferencesModal,
  createUserMenu,
  AUTH_STYLES,
  PREFERENCES_MODAL_STYLES,
} from "@mobius/auth";

/** Subset of auth profile for sidebar + answer insights gating */
interface MobiusChatUserProfile {
  greeting_name?: string;
  display_name?: string;
  first_name?: string;
  preferred_name?: string;
  email?: string;
  activities?: string[];
  // TODO(hardening): User Manager will add roles[] once the field shape is settled.
  // Roles that gate corpus promotion: "corpus_curator" | "rag_admin"
  roles?: string[];
}

/** Clarification option: server-authored choices (jurisdiction, NPI pick, future workflows) */
interface ClarificationOption {
  slot: string;
  label: string;
  selection_mode: string;
  choices: Array<{ value: string; label: string; choice_id?: string }>;
  min_choices?: number;
  max_choices?: number;
  context_type?: string;
  /** When not false, UI explains that the composer can be used without chips (default: true). */
  allow_free_text?: boolean;
  /** Shown under chips; client uses a short fallback if omitted and allow_free_text is not false. */
  free_text_hint?: string;
}

/** Live chip state merged into the next composer Send (see buildWorkflowSelectionPreface). */
interface ClarificationDraftGroup {
  slot: string;
  mode: "single" | "multiple";
  multiSelected: Set<string>;
  singleSelected: string | null;
  minChoices: number;
  maxChoices: number;
}

let activeClarificationDraft: ClarificationDraftGroup[] | null = null;

function buildWorkflowSelectionPreface(): string | null {
  if (!activeClarificationDraft?.length) {
    return null;
  }
  const blocks: string[] = [];
  for (const g of activeClarificationDraft) {
    if (g.mode === "multiple") {
      const n = g.multiSelected.size;
      if (n < g.minChoices || n > g.maxChoices) {
        continue;
      }
      const lines = [...g.multiSelected].map((v) => `• ${v}`);
      blocks.push(`[Mobius workflow_selection slot="${g.slot}"]\n` + lines.join("\n"));
    } else {
      const v = (g.singleSelected || "").trim();
      if (v) {
        blocks.push(v);
      }
    }
  }
  if (!blocks.length) {
    return null;
  }
  return blocks.join("\n\n");
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
/** Sprint A.1 (2026-04-19): the structured emit envelope shape the
 *  backend writes into thinking_log. Typed minimally — we only need
 *  `signal` (for future signal-specific rendering) and `note` (for
 *  the display string fallback). Full envelope has more fields (data,
 *  step_id, round, task_type, etc.) but the FE doesn't consume them
 *  yet. */
interface ThinkingEnvelope {
  signal: string;
  note?: string;
  step_id?: string;
  round?: number;
  data?: Record<string, unknown>;
  // … other fields ignored by the FE today
}

/** Normalize a thinking_log entry (legacy string or new envelope dict)
 *  into the display string the chat UI renders. */
function thinkingLineFromEntry(entry: string | ThinkingEnvelope | unknown): string {
  if (typeof entry === "string") {
    return entry;
  }
  if (entry && typeof entry === "object" && "signal" in entry) {
    const env = entry as ThinkingEnvelope;
    return (env.note ?? "").trim() || `[${env.signal}]`;
  }
  // Unknown shape — stringify as a last resort so the line doesn't
  // silently disappear. Shouldn't happen in practice.
  try {
    return JSON.stringify(entry);
  } catch {
    return String(entry);
  }
}

interface ChatResponse {
  status: string;
  message: string | null;
  correlation_id?: string;
  plan?: unknown;
  /** Sprint A.1 (2026-04-19): thinking_log became a mixed array — legacy
   *  string emits alongside new EmitEnvelope dicts. The normalizer
   *  thinkingLineFromEntry() converts either shape to a display string. */
  thinking_log?: (string | ThinkingEnvelope)[];
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
  /** Suggested follow-up questions; string or { text, clickable? } — see normalizeFollowupLineList */
  next_questions_for_user?: unknown[];
  /** Next steps outside chat; string or { text, clickable? } — strings default non-clickable on server */
  next_steps?: unknown[];
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
  /** PDF/MD download filenames: reconciliation vs 11-step credentialing waterfall */
  roster_report_attachments_kind?: "reconciliation" | "credentialing";
  /** Co-pilot credentialing: validate pending step (duplicate of envelope gate when present) */
  credentialing_copilot?: CredentialingCopilotPayload | null;
  /** Set when eval/QC audit posts to POST /chat/qc-audit/{id} */
  qc_audit?: QcAuditInfo;
  /** DB-backed routing + adjudicator thumbs (merged on poll for completed turns). */
  technical_feedback?: TechnicalFeedback;
  /** product_feedback skill: editable confirmation card returned after inline capture. */
  capture_card?: {
    feedback_id: string;
    category: string;
    categories: string[];
    sentiment: string;
    tidied: string;
    editable: boolean;
    mode?: string;        // "confirm" when skill pre-persisted; edits go to update_url
    update_url?: string;  // "/chat/product-feedback/update" — PATCH the existing row
  };
  /** Planner-driven periodic survey chip (NPS / CSAT / open). */
  offer_feedback?: {
    kind: string;
    trigger: string;
    survey_type?: string;                                    // "nps" | "csat"
    prompt?: string;                                         // question text from server
    scale?: { min: number; max: number; min_label: string; max_label: string };
    post_to?: string;                                        // endpoint for score/submit
    cta?: string;                                            // label for generic CTA button
  };
  /** Product Awareness: interactive demo tour from the Interact engine. */
  demo?: {
    script_id: string;
    title: string;
  };
}

/** One line in envelope next_steps / suggested_questions blocks */
interface FollowupEnvelopeItem {
  text: string;
  clickable: boolean;
}

/** Normalized follow-up line from API payload */
interface FollowupLineNormalized {
  text: string;
  clickable: boolean;
}

function normalizeFollowupLineItem(raw: unknown, defaultClickable: boolean): FollowupLineNormalized | null {
  if (typeof raw === "string") {
    const t = raw.trim();
    return t ? { text: t, clickable: defaultClickable } : null;
  }
  if (raw && typeof raw === "object") {
    const o = raw as Record<string, unknown>;
    const text = String(o.text ?? o.label ?? o.line ?? "").trim();
    if (!text) return null;
    let clickable = defaultClickable;
    if (typeof o.clickable === "boolean") clickable = o.clickable;
    else if (typeof o.tap_to_send === "boolean") clickable = o.tap_to_send;
    return { text, clickable };
  }
  return null;
}

function normalizeFollowupLineList(raw: unknown, defaultClickable: boolean): FollowupLineNormalized[] {
  if (!Array.isArray(raw)) return [];
  const out: FollowupLineNormalized[] = [];
  for (const x of raw) {
    const n = normalizeFollowupLineItem(x, defaultClickable);
    if (n) out.push(n);
  }
  return out;
}

function followupListHintLines(items: FollowupLineNormalized[]): string {
  if (!items.length) return "";
  const anyClick = items.some((i) => i.clickable);
  const allStatic = !anyClick;
  if (allStatic) return "Reference only—not sent as a message unless you copy or type below.";
  if (items.every((i) => i.clickable)) return "Tap a line to send it as your next message, or type below.";
  return "Tap lines marked as actions to send; others are for reference only.";
}

/** Env checks for roster DB + skills (see credentialing_gate_event.get_credentialing_prerequisites_status) */
interface CredentialingPrerequisitesStatus {
  chat_database_configured?: boolean;
  provider_roster_url_configured?: boolean;
  redis_configured?: boolean;
  ready_for_credentialing_api?: boolean;
  ready_for_persisted_copilot_runs?: boolean;
  recommendations?: string[];
}

/** Per-step workflow notes from server (user + system), for tracking follow-ups */
interface CredentialingWorkflowStepRow {
  step_id?: string | null;
  workflow_follow_ups?: Array<Record<string, unknown>>;
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
  gate_events?: Array<Record<string, unknown>>;
  last_gate_event?: Record<string, unknown> | null;
  credentialing_prerequisites?: CredentialingPrerequisitesStatus;
  workflow_follow_ups_by_step?: CredentialingWorkflowStepRow[] | null;
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
  | { type: "next_steps"; items: FollowupEnvelopeItem[]; collapsed_default?: boolean }
  | { type: "suggested_questions"; items: FollowupEnvelopeItem[]; collapsed_default?: boolean }
  | { type: "markdown_report"; markdown: string }
  | { type: "attachments"; has_pdf?: boolean }
  | { type: "document_download"; documents: DocumentDownloadEntry[]; query?: string }
  | { type: "pipeline_human_gate"; version?: number; gate: CredentialingCopilotPayload & { plan_kind?: string; thread_id?: string | null } };

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
  /** thread_id added 2026-05-05 so sidebar can re-open the existing
   * thread on click instead of re-running the question as a fresh turn.
   * Optional because older rows may not have it backfilled. */
  thread_id?: string | null;
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

/** POST /chat — optional envelope fields (mobius-chat ChatRequest) */
interface CredentialingOptionsPayload {
  org_name: string;
  mode: "autopilot" | "copilot";
  force_refresh: boolean;
  /** True = outside-in Medicaid NPI pipeline even when a roster exists on the thread */
  prefer_outside_in?: boolean;
  /** True = skip same-day cached outside-in credentialing report and run full pipeline */
  prefer_fresh_report?: boolean;
}

interface SendMessageOpts {
  credentialing_options?: CredentialingOptionsPayload;
  /** When true, do not intercept with credentialing envelope */
  skipCredentialingEnvelope?: boolean;
  use_react?: boolean;
  /** When true, user acknowledged the PHI gate warning and is proceeding. */
  phi_override?: boolean;
}

/** Aligned with mobius-chat/app/services/tool_agent.py roster_triggers + roster_triggers_new */
const CREDENTIALING_ROSTER_TRIGGERS: string[] = [
  "provider roster",
  "credentialing report",
  "roster report",
  "roster reconciliation",
  "reconciliation report",
  "medicaid roster",
  "roster for",
  "medicaid npi report",
  "create a medicaid npi report",
  "create medicaid npi report",
  "create a credentialing report",
  "create credentialing report",
  "i want to create a medicaid npi report",
  "i want to create a credentialing report",
];

const CREDENTIALING_ORG_PREFIXES: string[] = [
  "run roster reconciliation report for",
  "roster reconciliation report for",
  "reconciliation report for",
  "run reconciliation report for",
  "provider roster for",
  "credentialing report for",
  "roster report for",
  "medicaid roster for",
  "roster for",
  "create a medicaid npi report for",
  "create medicaid npi report for",
  "create a credentialing report for",
  "create credentialing report for",
  "i want to create a medicaid npi report for",
  "i want to create a credentialing report for",
  "medicaid npi report for",
];

function isCredentialingReportIntent(text: string): boolean {
  const lower = (text || "").trim().toLowerCase();
  const wantsNewReport = [
    "run roster reconciliation report for",
    "roster reconciliation report for",
    "reconciliation report for",
    "run reconciliation report for",
    "provider roster for",
    "credentialing report for",
    "roster report for",
    "medicaid roster for",
    "roster for",
    "create a medicaid npi report for",
    "create medicaid npi report for",
    "create a credentialing report for",
    "create credentialing report for",
    "medicaid npi report for",
  ];
  if (wantsNewReport.some((t) => lower.includes(t))) return true;
  return CREDENTIALING_ROSTER_TRIGGERS.some((t) => lower.includes(t));
}

/** Match org hint to roster upload row (same heuristic as server classify_org_vs_uploads). */
function orgHintMatchesUploadOrg(orgHint: string, uploadOrg: string): boolean {
  const a = (orgHint || "").trim().toLowerCase();
  const b = (uploadOrg || "").trim().toLowerCase();
  if (!a || !b) return false;
  return a.includes(b) || b.includes(a);
}

function extractCredentialingOrgHint(text: string): string {
  const rosterLower = text.trim().toLowerCase();
  const rosterCheckText = text.trim();
  for (const t of CREDENTIALING_ORG_PREFIXES) {
    if (rosterLower.includes(t)) {
      return rosterCheckText
        .slice(rosterLower.indexOf(t) + t.length)
        .trim()
        .replace(/[?.,;!]+$/, "");
    }
  }
  return "";
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

/** From provider skill GET /roster-uploads/{id} and merged into POST /chat/roster-upload */
interface RosterPipelineStage {
  id: string;
  label: string;
  done: boolean;
  detail: string;
}
interface RosterPipelineProgress {
  summary?: string;
  current_stage_id?: string;
  reconciliation_ready?: boolean;
  warehouse_loaded?: boolean;
  stages?: RosterPipelineStage[];
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
  pipeline_progress?: RosterPipelineProgress | null;
  reconciliation_upload_id?: string | null;
  reconciliation_ui_url?: string | null;
}

/** Section intent for visibility rules */
const SECTION_INTENTS = ["process", "requirements", "definitions", "exceptions", "references"] as const;
type SectionIntent = (typeof SECTION_INTENTS)[number];

function isSectionIntent(s: unknown): s is SectionIntent {
  return typeof s === "string" && SECTION_INTENTS.includes(s as SectionIntent);
}

/** AnswerCard JSON from consolidator (FACTUAL / CANONICAL / BLENDED / RECITAL) */
type SectionFormat = "bullets" | "table" | "steps" | "stats" | "bars" | "conditions";
interface SectionDataItem {
  label?: string;
  value?: string;
  note?: string;
  weight?: number;
  condition?: string;
  result?: string;
}
interface SectionData {
  headers?: string[];
  rows?: string[][];
  items?: SectionDataItem[];
}
interface AnswerCardSection {
  intent?: SectionIntent;
  label: string;
  format?: SectionFormat;
  bullets: string[];
  data?: SectionData;
}
interface AnswerCard {
  mode: "FACTUAL" | "CANONICAL" | "BLENDED" | "RECITAL";
  direct_answer: string;
  sections: AnswerCardSection[];
  recital?: {
    verbatim: string;
    document_id?: string;
    section?: string;
  };
  required_variables?: string[];
  confidence_note?: string;
  citations?: Array<{ id: string; doc_title: string; locator: string; snippet: string }>;
  followups?: Array<{ question: string; reason: string; field: string }>;
  suggested_actions?: Array<{ type: string; label: string; url: string; icon?: string }>;
  thread_summary?: string;
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

// ── Model profile picker (Sprint 2 #0) ────────────────────────────
// Tiny header control that lets operators flip the active model
// profile (bandit / optimal / gemini / anthropic / default) without
// a redeploy. Hidden automatically when admin endpoints return 404
// (i.e. MOBIUS_ADMIN_ENABLED=0, e.g. prod).
function initModelProfilePicker(): void {
  const wrap = document.getElementById("modelProfileWrap") as HTMLElement | null;
  const sel = document.getElementById("modelProfileSelect") as HTMLSelectElement | null;
  const status = document.getElementById("modelProfileStatus") as HTMLElement | null;
  if (!wrap || !sel) return;
  const setStatus = (text: string, kind: "ok" | "err" | null) => {
    if (!status) return;
    status.textContent = text || "";
    status.className = "sidebar-llm-status" + (kind ? " sidebar-llm-status--" + kind : "");
  };
  // 2026-04-27: rename ``default`` / ``bandit`` → ``auto`` in the
  // picker. Both YAML profiles are empty maps (Thompson-bandit fully
  // in charge); ``default`` doesn't read parallel with ``optimal`` /
  // ``gemini`` / ``anthropic``, and ``bandit`` is implementation
  // jargon. ``auto`` describes the experience and matches industry
  // convention (auto-router, auto-scaling).
  //
  // Backend keeps the deprecated names so MOBIUS_MODEL_PROFILE env
  // and the admin API remain stable. We just hide the duplicates from
  // the user-facing dropdown and remap the active label when one of
  // the legacy names comes back from /chat/admin/model-profile.
  const HIDDEN_PROFILES = new Set(["default", "bandit"]);
  const LEGACY_TO_DISPLAY: Record<string, string> = {
    default: "auto",
    bandit:  "auto",
  };

  const render = (data: any) => {
    const profilesRaw: string[] = (data && data.available_profiles) || [];
    const activeRaw: string = (data && data.active_profile) || "default";
    // Build the display list: drop legacy aliases, ensure ``auto`` is
    // present (the YAML may still emit only ``default`` / ``bandit``
    // until that change ships).
    const seen = new Set<string>();
    const display: string[] = [];
    if (profilesRaw.includes("auto") || profilesRaw.includes("default") || profilesRaw.includes("bandit")) {
      display.push("auto"); seen.add("auto");
    }
    for (const p of profilesRaw) {
      if (HIDDEN_PROFILES.has(p) || p === "auto") continue;
      if (!seen.has(p)) { display.push(p); seen.add(p); }
    }
    const activeDisplay = LEGACY_TO_DISPLAY[activeRaw] || activeRaw;
    sel.innerHTML = "";
    display.forEach((p) => {
      const opt = document.createElement("option");
      opt.value = p;
      opt.textContent = p;
      if (p === activeDisplay) opt.selected = true;
      sel.appendChild(opt);
    });
  };
  const load = () => {
    fetch(API_BASE + "/chat/admin/model-profile")
      .then((r) => {
        if (r.status === 404) { wrap.hidden = true; return null; }
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      })
      .then((d) => { if (d) render(d); })
      .catch((e) => { console.warn("model-profile load failed:", e); wrap.hidden = true; });
  };
  sel.addEventListener("change", () => {
    const val = sel.value;
    setStatus("…", null);
    fetch(API_BASE + "/chat/admin/model-profile", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ profile: val }),
    })
      .then((r) => r.json().then((d: any) => ({ ok: r.ok, d })))
      .then(({ ok, d }) => {
        if (!ok) { setStatus(d && d.detail ? "!" : "err", "err"); return; }
        render(d);
        setStatus("✓", "ok");
        setTimeout(() => setStatus("", null), 1500);
      })
      .catch((e) => { console.warn("model-profile switch failed:", e); setStatus("err", "err"); });
  });
  load();
}

// ── Chat-skills chips (Sprint 2 #0.5, 2026-04-25) ─────────────────
//
// Sidebar Skills section. Two surfaces in one block:
//   1. Suite buttons (Roster, Credentialing) — already in HTML, route
//      to product surfaces. Untouched here.
//   2. "Chat tools" chips — drop a templated prompt into the composer
//      so the user can edit + send. Pulls from a small curated list
//      keyed to registered skills.
//
// Collapsed-state rail icons (Sprint 2 #0.5, 2026-04-25). The sidebar
// has a narrow rail visible when collapsed; rail icons let users jump
// to a section without expanding the whole panel manually. Click →
// expand sidebar AND scroll to the section. Counts feed from the same
// data source the expanded sections render from.
function initSidebarRailIcons(authService?: { getAuthHeader?: () => Promise<Record<string, string> | null> | Record<string, string> }): void {
  const sidebar = document.getElementById("sidebar");
  if (!sidebar) return;
  const icons = Array.from(sidebar.querySelectorAll<HTMLButtonElement>(".sidebar-rail-icon"));
  if (!icons.length) return;

  icons.forEach((btn) => {
    btn.addEventListener("click", (e) => {
      e.preventDefault();
      e.stopPropagation();
      const targetId = btn.dataset.target || "";
      // Always expand on icon click — the rail icons are only useful
      // when collapsed; clicking one signals "open this section."
      if (sidebar.classList.contains("sidebar--collapsed")) {
        sidebar.classList.remove("sidebar--collapsed");
        const main = document.querySelector(".main");
        if (main) main.classList.remove("sidebar-collapsed");
      }
      if (!targetId) return;
      // Scroll the section into view + briefly highlight so the user
      // sees what they jumped to.
      requestAnimationFrame(() => {
        const target = document.getElementById(targetId);
        if (!target) return;
        const section = target.closest(".sidebar-recent, .sidebar-needs-answer, .sidebar-skills, .sidebar-toast-master") as HTMLElement | null;
        if (section) {
          section.scrollIntoView({ behavior: "smooth", block: "start" });
          section.classList.add("sidebar-section--flash");
          setTimeout(() => section.classList.remove("sidebar-section--flash"), 1200);
        }
      });
    });
  });

  // Wire the recent-count badge so collapsed-state shows "5" etc.
  // Reuses the same /chat/history/recent fetch the expanded list does.
  const updateRecentBadge = (): void => {
    const badge = document.getElementById("railBadgeRecent");
    if (!badge) return;
    void Promise.resolve(authService?.getAuthHeader?.() ?? {}).then((hdrs) =>
    fetch(API_BASE + "/chat/history/recent?limit=20", { headers: hdrs ?? {} }))
      .then((r) => (r.ok ? r.json() : []))
      .then((rows: unknown[]) => {
        const n = Array.isArray(rows) ? rows.length : 0;
        if (n > 0) {
          badge.textContent = String(n > 99 ? "99+" : n);
          badge.hidden = false;
        } else {
          badge.hidden = true;
        }
      })
      .catch(() => { /* leave hidden */ });
  };
  updateRecentBadge();
}


/* ── Queries-dump UI (drawer entry → modal). 2026-05-05.
   Reads GET /chat/admin/queries — see app/storage/queries_dump.py.
   Reuses the .llm-router-report-modal__* shell + adds .queries-dump-* styles.
*/

interface QueryDumpRow {
  correlation_id: string;
  created_at: string;
  user_id: string | null;
  thread_id: string | null;
  question_preview: string;
  total_latency_ms: number | null;
  llm_call_count: number;
  input_tokens: number;
  output_tokens: number;
  cost_usd: number;
  models_used: string | null;
  llm_error_count: number;
  last_error_type: string | null;
  retrieval_runs_count: number;
  chunks_assembled: number;
  cache_mode: string | null;
  cache_top_similarity: number | null;
  feedback_rating: string | null;
  feedback_comment: string | null;
}
interface QueryDumpResponse {
  rows: QueryDumpRow[];
  count: number;
  warning: string | null;
}

const QD_AUTO_REFRESH_MS = 30_000;
const QD_SINCE_DELTAS: Record<string, number | null> = {
  "1h":  60 * 60 * 1000,
  "24h": 24 * 60 * 60 * 1000,
  "7d":  7  * 24 * 60 * 60 * 1000,
  "30d": 30 * 24 * 60 * 60 * 1000,
  "all": null,
};

function setupQueriesDumpUI(): void {
  const launch = document.getElementById("drawerQueriesDumpLaunch");
  const btn    = document.getElementById("btnQueriesDump");
  const modal  = document.getElementById("queriesDumpModal");
  const body   = document.getElementById("queriesDumpBody");
  const closeBtn = document.getElementById("queriesDumpClose");
  const backdrop = document.getElementById("queriesDumpBackdrop");
  const summary  = document.getElementById("queriesDumpSummary");
  const status   = document.getElementById("queriesDumpStatus");
  const fSince   = document.getElementById("qdSince") as HTMLSelectElement | null;
  const fUser    = document.getElementById("qdUser") as HTMLInputElement | null;
  const fErr     = document.getElementById("qdHasError") as HTMLInputElement | null;
  const fFb      = document.getElementById("qdHasFeedback") as HTMLInputElement | null;
  const fLimit   = document.getElementById("qdLimit") as HTMLSelectElement | null;
  const btnApply = document.getElementById("qdApply");
  const btnReset = document.getElementById("qdReset");
  const btnPrev  = document.getElementById("qdPrev") as HTMLButtonElement | null;
  const btnNext  = document.getElementById("qdNext") as HTMLButtonElement | null;
  const jsonLink = document.getElementById("qdJson") as HTMLAnchorElement | null;
  const autoRefresh = document.getElementById("queriesDumpAutoRefresh") as HTMLInputElement | null;
  if (!launch || !btn || !modal || !body || !fSince || !fLimit) return;

  let offset = 0;
  let lastCount = 0;
  let refreshTimer: number | null = null;

  const setOpen = (open: boolean): void => {
    modal.classList.toggle("llm-router-report-modal--open", open);
    modal.setAttribute("aria-hidden", open ? "false" : "true");
    if (!open && refreshTimer !== null) {
      window.clearInterval(refreshTimer);
      refreshTimer = null;
    }
    if (open) scheduleAutoRefresh();
  };

  const buildParams = (): URLSearchParams => {
    const p = new URLSearchParams();
    const limit = Math.max(1, Math.min(1000, parseInt(fLimit.value, 10) || 100));
    p.set("limit", String(limit));
    p.set("offset", String(offset));
    const sinceKey = fSince.value;
    const delta = QD_SINCE_DELTAS[sinceKey];
    if (delta !== null && delta !== undefined) {
      p.set("since", new Date(Date.now() - delta).toISOString());
    }
    const u = (fUser?.value || "").trim();
    if (u) p.set("user_id", u);
    if (fErr?.checked) p.set("has_error", "true");
    if (fFb?.checked) p.set("has_feedback", "true");
    return p;
  };

  const updateJsonLink = (): void => {
    if (!jsonLink) return;
    const p = buildParams();
    p.set("format", "json");
    jsonLink.href = API_BASE + "/chat/admin/queries?" + p.toString();
  };

  const load = (): void => {
    body.innerHTML = '<p class="llm-router-report-loading" style="padding:1rem">Loading…</p>';
    if (status) status.textContent = "loading…";
    updateJsonLink();
    const p = buildParams();
    fetch(API_BASE + "/chat/admin/queries?" + p.toString(), {
      headers: { Accept: "application/json" },
    })
      .then((r) => {
        if (r.status === 404) {
          throw new Error("Endpoint disabled (set MOBIUS_ADMIN_ENABLED=1).");
        }
        return r.json() as Promise<QueryDumpResponse>;
      })
      .then((data) => {
        renderQueriesDumpBody(body, summary, data);
        lastCount = data.count;
        if (status) {
          const limit = parseInt(fLimit.value, 10) || 100;
          status.textContent = `rows ${offset + 1}–${offset + data.count} (limit ${limit})`;
        }
        if (btnPrev) btnPrev.disabled = offset === 0;
        if (btnNext) btnNext.disabled = data.count < (parseInt(fLimit.value, 10) || 100);
      })
      .catch((err) => {
        body.innerHTML =
          '<p class="llm-router-report-error" style="padding:1rem">Could not load: ' +
          (err && err.message ? String(err.message) : "request failed") + '</p>';
        if (status) status.textContent = "error";
      });
  };

  const scheduleAutoRefresh = (): void => {
    if (refreshTimer !== null) {
      window.clearInterval(refreshTimer);
      refreshTimer = null;
    }
    if (autoRefresh?.checked && modal.classList.contains("llm-router-report-modal--open")) {
      refreshTimer = window.setInterval(load, QD_AUTO_REFRESH_MS);
    }
  };

  btn.addEventListener("click", () => {
    offset = 0;
    setOpen(true);
    load();
  });
  closeBtn?.addEventListener("click", () => setOpen(false));
  backdrop?.addEventListener("click", () => setOpen(false));
  document.addEventListener("keydown", (e: KeyboardEvent) => {
    if (e.key === "Escape" && modal.classList.contains("llm-router-report-modal--open")) setOpen(false);
  });

  btnApply?.addEventListener("click", () => { offset = 0; load(); });
  btnReset?.addEventListener("click", () => {
    offset = 0;
    fSince.value = "24h";
    if (fUser) fUser.value = "";
    if (fErr) fErr.checked = false;
    if (fFb) fFb.checked = false;
    fLimit.value = "100";
    load();
  });
  btnPrev?.addEventListener("click", () => {
    const limit = parseInt(fLimit.value, 10) || 100;
    offset = Math.max(0, offset - limit);
    load();
  });
  btnNext?.addEventListener("click", () => {
    const limit = parseInt(fLimit.value, 10) || 100;
    if (lastCount < limit) return;
    offset = offset + limit;
    load();
  });
  autoRefresh?.addEventListener("change", scheduleAutoRefresh);
  fUser?.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { offset = 0; load(); }
  });
}

function renderQueriesDumpBody(
  container: HTMLElement,
  summaryEl: HTMLElement | null,
  data: QueryDumpResponse,
): void {
  const rows = data.rows || [];

  if (summaryEl) {
    if (rows.length === 0) {
      summaryEl.hidden = true;
    } else {
      const totalCost = rows.reduce((s, r) => s + (Number(r.cost_usd) || 0), 0);
      const totalIn   = rows.reduce((s, r) => s + (r.input_tokens || 0), 0);
      const totalOut  = rows.reduce((s, r) => s + (r.output_tokens || 0), 0);
      const errCount  = rows.reduce((s, r) => s + (r.llm_error_count > 0 ? 1 : 0), 0);
      const fbUp      = rows.filter((r) => r.feedback_rating === "up").length;
      const fbDown    = rows.filter((r) => r.feedback_rating === "down").length;
      const lats = rows
        .map((r) => r.total_latency_ms || 0)
        .filter((n) => n > 0)
        .sort((a, b) => a - b);
      const pct = (arr: number[], p: number): number =>
        arr.length === 0 ? 0 : arr[Math.min(arr.length - 1, Math.floor(arr.length * p))] || 0;
      const p50 = pct(lats, 0.5);
      const p95 = pct(lats, 0.95);

      summaryEl.innerHTML = [
        `<div class="qd-stat"><span class="qd-n">${rows.length}</span><span class="qd-label">turns</span></div>`,
        `<div class="qd-stat"><span class="qd-n">${formatMs(p50)}</span><span class="qd-label">p50 latency</span></div>`,
        `<div class="qd-stat"><span class="qd-n">${formatMs(p95)}</span><span class="qd-label">p95 latency</span></div>`,
        `<div class="qd-stat"><span class="qd-n">$${totalCost.toFixed(4)}</span><span class="qd-label">total cost</span></div>`,
        `<div class="qd-stat"><span class="qd-n">${formatTok(totalIn + totalOut)}</span><span class="qd-label">total tokens</span></div>`,
        `<div class="qd-stat"><span class="qd-n">${errCount}</span><span class="qd-label">errors</span></div>`,
        `<div class="qd-stat"><span class="qd-n">${fbUp} / ${fbDown}</span><span class="qd-label">feedback ↑/↓</span></div>`,
      ].join("");
      summaryEl.hidden = false;
    }
  }

  const escapeHtml = (s: string): string =>
    s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c] as string));

  const fbPill = (rating: string | null): string => {
    if (rating === "up") return '<span class="qd-pill qd-pill-up">↑</span>';
    if (rating === "down") return '<span class="qd-pill qd-pill-down">↓</span>';
    return "";
  };

  const formatTime = (iso: string): string => {
    try { return new Date(iso).toLocaleString(); } catch { return iso; }
  };

  if (rows.length === 0) {
    container.innerHTML =
      data.warning
        ? `<p class="llm-router-report-error" style="padding:1rem">${escapeHtml(data.warning)}</p>`
        : '<p class="llm-router-report-meta" style="padding:1rem">No turns match the current filters.</p>';
    return;
  }

  const renderRow = (r: QueryDumpRow): string => {
    const ms = r.total_latency_ms || 0;
    const slowCls = ms >= 2000 ? " qd-slow" : "";
    const errDot = r.llm_error_count > 0
      ? `<span class="qd-err-dot" title="${escapeHtml(r.last_error_type || 'error')}"></span>`
      : "";
    const cost = Number(r.cost_usd || 0).toFixed(4);
    const userLabel = r.user_id || "—";
    const question = r.question_preview || "(no question)";
    const fb = fbPill(r.feedback_rating);

    const detailRows: string[] = [
      `<dt>question</dt><dd class="qd-full-q">${escapeHtml(question)}</dd>`,
    ];
    if (r.thread_id) {
      detailRows.push(`<dt>thread</dt><dd><span class="qd-mono-dim">${escapeHtml(String(r.thread_id))}</span></dd>`);
    }
    if (r.models_used) {
      detailRows.push(`<dt>models</dt><dd>${escapeHtml(r.models_used)}</dd>`);
    }
    detailRows.push(`<dt>llm calls</dt><dd>${r.llm_call_count}</dd>`);
    detailRows.push(
      `<dt>tokens</dt><dd>${Number(r.input_tokens || 0).toLocaleString()} in <span class="qd-mono-dim">·</span> ${Number(r.output_tokens || 0).toLocaleString()} out</dd>`,
    );
    detailRows.push(
      `<dt>rag</dt><dd>${r.chunks_assembled} chunk${r.chunks_assembled === 1 ? "" : "s"} <span class="qd-mono-dim">·</span> ${r.retrieval_runs_count} run${r.retrieval_runs_count === 1 ? "" : "s"}</dd>`,
    );
    if (r.cache_mode) {
      const sim = r.cache_top_similarity != null
        ? ` <span class="qd-mono-dim">sim ${Number(r.cache_top_similarity).toFixed(2)}</span>`
        : "";
      detailRows.push(
        `<dt>cache</dt><dd><span class="qd-pill qd-pill-cache-${escapeHtml(r.cache_mode)}">${escapeHtml(r.cache_mode)}</span>${sim}</dd>`,
      );
    }
    if (r.llm_error_count > 0) {
      detailRows.push(
        `<dt>errors</dt><dd class="qd-err-line">${r.llm_error_count}${r.last_error_type ? " (" + escapeHtml(r.last_error_type) + ")" : ""}</dd>`,
      );
    }
    if (r.feedback_comment) {
      detailRows.push(
        `<dt>feedback</dt><dd>${fb} ${escapeHtml(r.feedback_comment)}</dd>`,
      );
    }
    detailRows.push(
      `<dt>correlation</dt><dd><span class="qd-mono-dim">${escapeHtml(r.correlation_id)}</span></dd>`,
    );

    return `
      <details class="qd-row">
        <summary>
          <span class="qd-col-time">${escapeHtml(formatTime(r.created_at))}</span>
          <span class="qd-col-user">${escapeHtml(userLabel)}</span>
          <span class="qd-col-q">${errDot}${escapeHtml(question)}</span>
          <span class="qd-col-ms${slowCls}">${formatMs(ms)}</span>
          <span class="qd-col-cost">$${cost}</span>
          <span class="qd-col-fb">${fb}</span>
          <span class="qd-col-chev">▶</span>
        </summary>
        <dl class="qd-row-detail">${detailRows.join("")}</dl>
      </details>`;
  };

  const warn = data.warning
    ? `<div class="llm-router-report-error" style="padding:0.5rem 1rem">DB warning: ${escapeHtml(data.warning)}</div>`
    : "";

  container.innerHTML = warn + rows.map(renderRow).join("");
}

function formatMs(ms: number): string {
  if (!ms) return "—";
  if (ms < 1000) return `${ms} ms`;
  return `${(ms / 1000).toFixed(2)} s`;
}
function formatTok(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}K`;
  return String(n);
}

/** Visibility gate: only show the "Recent queries" drawer entry when
    the user has the llm_performance flag/override on, mirroring how
    the LLM-performance UI bits are conditionally rendered. */
function syncQueriesDumpVisibility(profile: MobiusChatUserProfile | null): void {
  const launch = document.getElementById("drawerQueriesDumpLaunch");
  if (!launch) return;
  launch.hidden = !getShowLlmPerformance(profile);
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
function thinkingFriendlyStatus(line: unknown): string {
  // Defensive: ``line`` is typed string, but rehydrated thinking_log
  // entries can be dicts (signal events) — coerce so a non-string
  // never crashes the chain via .toLowerCase().
  const raw = typeof line === "string" ? line : (line == null ? "" : String(line));
  const l = raw.toLowerCase();
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

  // Stash links/emails/phones before escaping so special chars in URLs survive.
  // Uses PUA codepoints \uE010/\uE011 (distinct from image stash \uE000/\uE001).
  const links: string[] = [];
  const stashLink = (html: string): string => {
    const i = links.length;
    links.push(html);
    return `\uE010${i}\uE011`;
  };
  // Only Mobius agent URLs (mobius-*.run.app) become purple pill buttons.
  // All other URLs remain plain text — no linkification.
  const MOBIUS_URL_RE = /https:\/\/mobius-[a-z0-9\-]+\.(?:a\.run\.app|us-central1\.run\.app)[^\s"'<>()[\]]*[^\s"'<>()[\].,!?;:]/g;
  const EMAIL_RE = /[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}/g;
  const PHONE_RE = /(?:\+?1[\s.\-]?)?\(?[2-9]\d{2}\)?[\s.\-]\d{3}[\s.\-]\d{4}/g;
  // Markdown [text](url): agent URLs → pill button; external URLs → just the link text
  out = out.replace(/\[([^\]]+)\]\((https:\/\/[^)]+)\)/g, (_m, linkText: string, url: string) => {
    if (/^https:\/\/mobius-/.test(url)) {
      return stashLink(`<a href="${url}" class="chat-link chat-link--url" target="_blank" rel="noopener noreferrer" title="${url}">${linkText} ↗</a>`);
    }
    return linkText;
  });
  out = out.replace(MOBIUS_URL_RE, (url: string) => {
    let display = url;
    try {
      display = new URL(url).hostname
        .replace(/\.(?:a\.run|us-central1\.run)\.app$/, "")
        .replace(/^mobius-/, "")
        .replace(/-[a-z0-9]+-uc$/, "");
    } catch { display = url.length > 40 ? url.slice(0, 39) + "…" : url; }
    return stashLink(`<a href="${url}" class="chat-link chat-link--url" target="_blank" rel="noopener noreferrer" title="${url}">${display} ↗</a>`);
  });
  out = out.replace(EMAIL_RE, (email: string) =>
    stashLink(`<a href="mailto:${email}" class="chat-link chat-link--email">${email}</a>`)
  );
  out = out.replace(PHONE_RE, (raw: string) => {
    const digits = raw.replace(/[^\d+]/g, "");
    return stashLink(`<a href="tel:${digits}" class="chat-link chat-link--tel">${raw}</a>`);
  });

  out = escape(out);
  imgs.forEach((img, i) => {
    out = out.replace(`\uE000${i}\uE001`, img);
  });
  links.forEach((html, i) => {
    out = out.replace(`\uE010${i}\uE011`, html);
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
      if (data.mode !== "FACTUAL" && data.mode !== "CANONICAL" && data.mode !== "BLENDED" && data.mode !== "RECITAL") return null;
      if (typeof data.direct_answer !== "string") return null;
      // RECITAL mode: sections optional, recital.verbatim required
      if (data.mode === "RECITAL") {
        const rec = data.recital as Record<string, unknown> | undefined;
        if (!rec || typeof rec.verbatim !== "string" || !rec.verbatim.trim()) return null;
        return {
          mode: "RECITAL",
          direct_answer: data.direct_answer as string,
          sections: [],
          recital: {
            verbatim: rec.verbatim as string,
            document_id: typeof rec.document_id === "string" ? rec.document_id : undefined,
            section: typeof rec.section === "string" ? rec.section : undefined,
          },
        };
      }
      if (!Array.isArray(data.sections)) return null;
      const rawSections = (data.sections as Array<{ intent?: unknown; label?: string; bullets?: string[] }>).slice(0, MAX_SECTIONS);
      const VALID_FORMATS: SectionFormat[] = ["bullets", "table", "steps", "stats", "bars", "conditions"];
      const sections: AnswerCardSection[] = (rawSections as Array<Record<string, unknown>>).map((sec) => ({
        intent: isSectionIntent(sec.intent) ? sec.intent as SectionIntent : "process",
        label: typeof sec.label === "string" ? sec.label : "",
        format: VALID_FORMATS.includes(sec.format as SectionFormat) ? sec.format as SectionFormat : "bullets",
        bullets: Array.isArray(sec.bullets) ? sec.bullets as string[] : [],
        data: sec.data && typeof sec.data === "object" ? sec.data as SectionData : undefined,
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
  const modeRe = /["']mode["']\s*:\s*["'](FACTUAL|CANONICAL|BLENDED|RECITAL)["']/;
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
  // BLENDED: surface requirements, process, and definitions immediately.
  // Only exceptions and references collapse — they're supplementary.
  const visibleIntents = new Set(["definitions", "requirements", "process"]);
  const visible = all.filter((s) => visibleIntents.has(s.intent ?? "process"));
  const hidden = all.filter((s) => !visibleIntents.has(s.intent ?? "process"));
  return { visible, hidden };
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
    body.appendChild(tbl);
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
    nextQuestions?: FollowupLineNormalized[];
    qcAudit?: QcAuditInfo;
    /** When true (admin + QA fail), omit source confidence badge */
    suppressConfidenceForAdminQcFail?: boolean;
    /** Corrections rows — from envelope callout/correction blocks */
    corrections?: Array<{ label: string; text: string }>;
    /** Suggested task items for the Next Steps tab */
    nextStepTasks?: Array<{ text: string; taskType: string }>;
  }
): HTMLElement {
  const wrap = document.createElement("div");
  wrap.className =
    "message message--assistant answer-card answer-card--" +
    card.mode.toLowerCase() +
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
        openCreateTaskDialog({
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
    tabBar.appendChild(mkTab("Summary", "summary", undefined, true));
    tabBar.appendChild(mkTab("Citations", "citations", (card.citations ?? []).length, false));
    tabBar.appendChild(mkTab("Corrections", "corrections", _corrections.length, false));
    tabBar.appendChild(mkTab("Follow-up", "next-steps", _nextStepQuestions.length, false));
    tabBar.appendChild(mkTab("Tasks", "tasks", _nextStepTasks.length, false));
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

/** Render assistant content: AnswerCard JSON (formatted) or prose fallback. */
function renderAssistantContent(
  body: string,
  isError?: boolean,
  opts?: {
    onFollowupClick?: (question: string) => void;
    sourceConfidenceStrip?: string;
    showConfidenceBadge?: boolean;
    suppressFollowups?: boolean;
    nextQuestions?: FollowupLineNormalized[];
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

/** Safe filename for a roster step CSV download (from step_id). */
function rosterStepCsvDownloadName(stepId: string): string {
  const raw = (stepId || "roster_step").trim().replace(/[/\\]+/g, "_");
  const base = raw.replace(/[^a-zA-Z0-9._-]+/g, "_").replace(/_+/g, "_").replace(/^_|_$/g, "") || "roster_step";
  return base.toLowerCase().endsWith(".csv") ? base : `${base}.csv`;
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
  const onlyLoc =
    stepOutputs.length === 1 && (stepOutputs[0].step_id || "").trim() === "find_locations";
  headerTitle.textContent = onlyLoc
    ? "Practice locations (expand for full list)"
    : "Step outputs (for validation)";
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
      const csvRaw = (step.csv_content || "").trim();
      if (csvRaw.length > 0) {
        const csvBtn = document.createElement("button");
        csvBtn.type = "button";
        csvBtn.className = "roster-step-download-csv";
        csvBtn.textContent = "Download CSV";
        csvBtn.setAttribute(
          "aria-label",
          `Download ${rosterStepCsvDownloadName(step.step_id || step.label || "step")}`,
        );
        csvBtn.addEventListener("click", () => {
          const blob = new Blob([step.csv_content || ""], { type: "text/csv;charset=utf-8" });
          const url = URL.createObjectURL(blob);
          const a = document.createElement("a");
          a.href = url;
          a.download = rosterStepCsvDownloadName(step.step_id || step.label || "step");
          a.click();
          URL.revokeObjectURL(url);
        });
        sectionBody.appendChild(csvBtn);
      }
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

function workflowFollowUpsDraftToLines(raw: unknown): string {
  if (!Array.isArray(raw)) return "";
  const lines: string[] = [];
  for (const x of raw) {
    if (typeof x === "string" && x.trim()) lines.push(x.trim());
    else if (x && typeof x === "object" && typeof (x as Record<string, unknown>).text === "string") {
      const t = String((x as Record<string, unknown>).text).trim();
      if (t) lines.push(t);
    }
  }
  return lines.join("\n");
}

function parseFollowUpLines(text: string): string[] {
  return text
    .split("\n")
    .map((l) => l.trim())
    .filter((l) => l.length > 0);
}

/** Omit workflow fields from main JSON editor (separate textarea). */
function draftJsonForTextarea(draft: Record<string, unknown> | null | undefined): string {
  const d = draft && typeof draft === "object" ? { ...draft } : {};
  delete d.workflow_follow_ups;
  delete d.workflow_follow_ups_hint;
  return JSON.stringify(d, null, 2);
}

function attachWorkflowFromDraft(base: Record<string, unknown>, draft: Record<string, unknown>): Record<string, unknown> {
  const wf = draft.workflow_follow_ups;
  if (Array.isArray(wf) && wf.length > 0) {
    return { ...base, workflow_follow_ups: wf };
  }
  return base;
}

function draftToValidatedOutput(
  draft: Record<string, unknown> | null | undefined,
  stepId: string
): Record<string, unknown> {
  const d = draft && typeof draft === "object" ? draft : {};
  let result: Record<string, unknown> = {};
  if (stepId === "identify_org" && Array.isArray(d.org_npis)) {
    result = { org_npis: d.org_npis };
  } else if (stepId === "find_locations" && Array.isArray(d.locations)) {
    result = { locations: d.locations };
  } else if (stepId === "find_associated_providers") {
    const out: Record<string, unknown> = {};
    if (d.associated_providers && typeof d.associated_providers === "object") {
      out.associated_providers = d.associated_providers;
    }
    if (d.active_roster && typeof d.active_roster === "object") {
      out.active_roster = d.active_roster;
    }
    if (d.use_autopilot_active_cutoff === true) {
      out.use_autopilot_active_cutoff = true;
    }
    if (d.allow_empty_active_roster === true) {
      out.allow_empty_active_roster = true;
    }
    if (Array.isArray(d.roster_line_items)) {
      out.roster_line_items = d.roster_line_items;
    }
    result = out;
  }
  return attachWorkflowFromDraft(result, d as Record<string, unknown>);
}

function appendCredentialingWorkflowByStepSection(wrap: HTMLElement, cc: CredentialingCopilotPayload): void {
  const rows = cc.workflow_follow_ups_by_step;
  if (!Array.isArray(rows) || rows.length === 0) return;
  const lines: string[] = [];
  for (const row of rows) {
    if (!row || typeof row !== "object") continue;
    const sid = String(row.step_id ?? "").trim();
    const wfu = row.workflow_follow_ups;
    if (!Array.isArray(wfu) || wfu.length === 0) continue;
    for (const item of wfu) {
      if (item && typeof item === "object" && typeof (item as Record<string, unknown>).text === "string") {
        const src = String((item as Record<string, unknown>).source ?? "").trim();
        const tag = src ? ` [${src}]` : "";
        lines.push(`${sid}: ${String((item as Record<string, unknown>).text)}${tag}`);
      }
    }
  }
  if (!lines.length) return;
  const det = document.createElement("details");
  det.className = "credentialing-copilot-gates";
  const sum = document.createElement("summary");
  sum.textContent = "Workflow follow-ups by step";
  det.appendChild(sum);
  const ul = document.createElement("ul");
  ul.className = "credentialing-copilot-gates-list";
  for (const ln of lines.slice(0, 80)) {
    const li = document.createElement("li");
    li.textContent = ln;
    ul.appendChild(li);
  }
  det.appendChild(ul);
  wrap.appendChild(det);
}

type AssocProviderRow = Record<string, unknown>;

/** Build active_roster map from per-location NPI checkboxes (copilot confirm). */
function buildActiveRosterFromPicks(
  associated: Record<string, AssocProviderRow[]>,
  picked: Map<string, Set<string>>
): Record<string, AssocProviderRow[]> {
  const out: Record<string, AssocProviderRow[]> = {};
  for (const [locId, rows] of Object.entries(associated)) {
    const want = picked.get(locId);
    const acc: AssocProviderRow[] = [];
    for (const r of rows || []) {
      const npi = String(r.npi ?? "")
        .trim()
        .padStart(10, "0");
      if (!npi || npi.length !== 10) continue;
      if (want?.has(npi)) {
        const c = { ...r };
        c.roster_status = "active";
        acc.push(c);
      }
    }
    out[locId] = acc;
  }
  return out;
}

/** Roster review UI for find_associated_providers: checkboxes + sync JSON textarea. */
function renderFindAssociatedRosterEditor(
  draft: Record<string, unknown>,
  ta: HTMLTextAreaElement
): HTMLElement {
  const wrap = document.createElement("div");
  wrap.className = "roster-review-editor";

  const assoc = (draft.associated_providers || {}) as Record<string, AssocProviderRow[]>;
  const cutoff = Number(draft.active_roster_cutoff ?? 50) || 50;
  const picked = new Map<string, Set<string>>();

  const syncTextarea = (flags?: { useCutoff?: boolean; allowEmpty?: boolean }) => {
    const active = buildActiveRosterFromPicks(assoc, picked);
    const payload: Record<string, unknown> = {
      associated_providers: assoc,
      active_roster: active,
    };
    if (flags?.useCutoff) payload.use_autopilot_active_cutoff = true;
    if (flags?.allowEmpty) payload.allow_empty_active_roster = true;
    ta.value = JSON.stringify(payload, null, 2);
  };

  const intro = document.createElement("p");
  intro.className = "roster-review-intro";
  intro.textContent =
    "Select providers to include in the active panel for downstream steps. In copilot mode the server starts with evidence only; your selection becomes active_roster on Continue.";
  wrap.appendChild(intro);

  for (const [locId, rows] of Object.entries(assoc)) {
    if (!rows?.length) continue;
    const sec = document.createElement("div");
    sec.className = "roster-review-location";

    const h = document.createElement("div");
    h.className = "roster-review-location-title";
    h.textContent = `Location ${locId.slice(0, 12)}… (${rows.length} candidates)`;
    sec.appendChild(h);

    const tbl = document.createElement("table");
    tbl.className = "roster-review-table";
    const thead = document.createElement("thead");
    thead.innerHTML =
      "<tr><th>Active</th><th>NPI</th><th>Name</th><th>Score</th><th>Basis</th><th>Status</th></tr>";
    tbl.appendChild(thead);
    const tb = document.createElement("tbody");
    const setForLoc = new Set<string>();
    picked.set(locId, setForLoc);

    for (const r of rows) {
      const npi = String(r.npi ?? "")
        .trim()
        .padStart(10, "0");
      if (npi.length !== 10) continue;
      const score = Number(r.association_likelihood ?? 0);
      const rs = String(r.roster_status ?? "");
      const defaultOn = rs === "active" || (rs === "pending_review" && score >= cutoff);
      if (defaultOn) setForLoc.add(npi);

      const tr = document.createElement("tr");
      const td0 = document.createElement("td");
      const cb = document.createElement("input");
      cb.type = "checkbox";
      cb.checked = defaultOn;
      cb.addEventListener("change", () => {
        if (cb.checked) setForLoc.add(npi);
        else setForLoc.delete(npi);
        syncTextarea();
      });
      td0.appendChild(cb);
      tr.appendChild(td0);
      const tdNpi = document.createElement("td");
      tdNpi.textContent = npi;
      tr.appendChild(tdNpi);
      const tdName = document.createElement("td");
      tdName.textContent = String(r.name ?? "");
      tr.appendChild(tdName);
      const tdSc = document.createElement("td");
      tdSc.textContent = String(score);
      tr.appendChild(tdSc);
      const tdBasis = document.createElement("td");
      tdBasis.textContent = String(r.basis_user ?? r.match_type ?? "");
      tr.appendChild(tdBasis);
      const tdSt = document.createElement("td");
      tdSt.textContent = rs || "—";
      tr.appendChild(tdSt);
      tb.appendChild(tr);
    }
    tbl.appendChild(tb);
    sec.appendChild(tbl);
    wrap.appendChild(sec);
  }

  const toolbar = document.createElement("div");
  toolbar.className = "roster-review-toolbar";

  const btnCutoff = document.createElement("button");
  btnCutoff.type = "button";
  btnCutoff.className = "credentialing-copilot-btn credentialing-copilot-btn--secondary";
  btnCutoff.textContent = `Check all with score ≥ ${cutoff}`;
  btnCutoff.addEventListener("click", () => {
    for (const [locId, rows] of Object.entries(assoc)) {
      const setForLoc = picked.get(locId);
      if (!setForLoc) continue;
      setForLoc.clear();
      for (const r of rows || []) {
        const npi = String(r.npi ?? "")
          .trim()
          .padStart(10, "0");
        if (npi.length !== 10) continue;
        const score = Number(r.association_likelihood ?? 0);
        if (score >= cutoff) setForLoc.add(npi);
      }
    }
    wrap.querySelectorAll("tbody tr").forEach((tr) => {
      const tds = tr.querySelectorAll("td");
      const cb = tds[0]?.querySelector("input") as HTMLInputElement | undefined;
      const sc = Number(tds[3]?.textContent ?? "");
      if (cb) cb.checked = sc >= cutoff;
    });
    syncTextarea();
  });

  const btnAll = document.createElement("button");
  btnAll.type = "button";
  btnAll.className = "credentialing-copilot-btn credentialing-copilot-btn--secondary";
  btnAll.textContent = "Check all candidates";
  btnAll.addEventListener("click", () => {
    for (const [locId, rows] of Object.entries(assoc)) {
      const setForLoc = picked.get(locId);
      if (!setForLoc) continue;
      setForLoc.clear();
      for (const r of rows || []) {
        const npi = String(r.npi ?? "")
          .trim()
          .padStart(10, "0");
        if (npi.length === 10) setForLoc.add(npi);
      }
    }
    wrap.querySelectorAll<HTMLInputElement>("input[type=checkbox]").forEach((cb) => {
      cb.checked = true;
    });
    syncTextarea();
  });

  const btnNone = document.createElement("button");
  btnNone.type = "button";
  btnNone.className = "credentialing-copilot-btn credentialing-copilot-btn--secondary";
  btnNone.textContent = "Clear all";
  btnNone.addEventListener("click", () => {
    picked.forEach((s) => s.clear());
    wrap.querySelectorAll<HTMLInputElement>("input[type=checkbox]").forEach((cb) => {
      cb.checked = false;
    });
    syncTextarea();
  });

  toolbar.appendChild(btnCutoff);
  toolbar.appendChild(btnAll);
  toolbar.appendChild(btnNone);
  wrap.appendChild(toolbar);

  syncTextarea();
  return wrap;
}

function appendCredentialingPrerequisitesSection(wrap: HTMLElement, cc: CredentialingCopilotPayload): void {
  const pr = cc.credentialing_prerequisites;
  if (!pr || typeof pr !== "object") return;
  const recs = Array.isArray(pr.recommendations)
    ? pr.recommendations.filter((x): x is string => typeof x === "string" && x.trim().length > 0)
    : [];
  const det = document.createElement("details");
  det.className = "credentialing-copilot-env";
  const sum = document.createElement("summary");
  sum.textContent = "Environment — what you need to run this";
  det.appendChild(sum);
  const body = document.createElement("div");
  body.className = "credentialing-copilot-env-body";
  if (recs.length) {
    const ul = document.createElement("ul");
    for (const r of recs) {
      const li = document.createElement("li");
      li.textContent = r;
      ul.appendChild(li);
    }
    body.appendChild(ul);
  } else {
    const ok = document.createElement("p");
    ok.className = "credentialing-copilot-env-ok";
    if (pr.ready_for_persisted_copilot_runs) {
      ok.textContent =
        "Roster skill URL and chat database look configured; co-pilot runs should persist across API and worker.";
    } else if (pr.ready_for_credentialing_api) {
      ok.textContent =
        "Roster skill URL is set. Add CHAT_RAG_DATABASE_URL (or RAG_DATABASE_URL) if you need persistence and DB-backed assertions.";
    } else {
      ok.textContent = "Set CHAT_SKILLS_PROVIDER_ROSTER_CREDENTIALING_URL before org/location/provider steps can call the skill API.";
    }
    body.appendChild(ok);
  }
  det.appendChild(body);
  wrap.appendChild(det);
}

function appendCredentialingGateTimeline(wrap: HTMLElement, cc: CredentialingCopilotPayload): void {
  const evs = cc.gate_events;
  if (!Array.isArray(evs) || evs.length === 0) return;
  const det = document.createElement("details");
  det.className = "credentialing-copilot-gates";
  const sum = document.createElement("summary");
  sum.textContent = `Recent credentialing gates (${evs.length})`;
  det.appendChild(sum);
  const ol = document.createElement("ol");
  ol.className = "credentialing-copilot-gates-list";
  for (const raw of evs) {
    if (!raw || typeof raw !== "object") continue;
    const o = raw as Record<string, unknown>;
    const li = document.createElement("li");
    const sid = String(o.step_id ?? "").trim();
    const code = String(o.reason_code ?? "").trim();
    const detail = String(o.detail ?? "").trim();
    const head = [sid, code].filter(Boolean).join(" — ");
    li.textContent = head ? (detail ? `${head}. ${detail}` : head) : detail || "(gate)";
    ol.appendChild(li);
  }
  det.appendChild(ol);
  wrap.appendChild(det);
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

  appendCredentialingPrerequisitesSection(wrap, cc);
  appendCredentialingGateTimeline(wrap, cc);
  appendCredentialingWorkflowByStepSection(wrap, cc);

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
  ta.rows = pending === "find_associated_providers" ? 6 : 12;
  ta.spellcheck = false;
  ta.value = draftJsonForTextarea(cc.draft_output ?? undefined);
  ta.setAttribute("aria-label", "Validated output JSON for this step");

  if (pending === "find_associated_providers") {
    wrap.appendChild(
      renderFindAssociatedRosterEditor((cc.draft_output ?? {}) as Record<string, unknown>, ta)
    );
  }

  wrap.appendChild(ta);

  const followHint = document.createElement("div");
  followHint.className = "credentialing-copilot-meta";
  const hintText = String((cc.draft_output as { workflow_follow_ups_hint?: string } | null)?.workflow_follow_ups_hint ?? "").trim();
  followHint.textContent =
    hintText ||
    "Follow-up / next steps (optional, one per line) — stored on this step when you continue.";
  wrap.appendChild(followHint);

  const followTa = document.createElement("textarea");
  followTa.className = "credentialing-copilot-json credentialing-copilot-followups";
  followTa.rows = 3;
  followTa.spellcheck = false;
  followTa.value = workflowFollowUpsDraftToLines(cc.draft_output?.workflow_follow_ups);
  followTa.setAttribute("aria-label", "Workflow follow-up lines for this step");
  wrap.appendChild(followTa);

  const inlineErr = document.createElement("div");
  inlineErr.className = "credentialing-copilot-error credentialing-copilot-inline-err";
  inlineErr.style.display = "none";
  wrap.appendChild(inlineErr);

  const btnRow = document.createElement("div");
  btnRow.className = "credentialing-copilot-actions";

  const acceptBtn = document.createElement("button");
  acceptBtn.type = "button";
  acceptBtn.className = "credentialing-copilot-btn credentialing-copilot-btn--secondary";
  acceptBtn.textContent = "Accept draft as-is";
  acceptBtn.addEventListener("click", () => {
    ta.value = draftJsonForTextarea(cc.draft_output ?? undefined);
    followTa.value = workflowFollowUpsDraftToLines(cc.draft_output?.workflow_follow_ups);
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
      inlineErr.textContent = "Invalid JSON — fix the textarea or use Accept draft as-is.";
      inlineErr.style.display = "";
      return;
    }
    inlineErr.style.display = "none";
    const fuLines = parseFollowUpLines(followTa.value);
    if (fuLines.length) validated.workflow_follow_ups = fuLines;
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
        gate_events: Array.isArray(data.gate_events) ? (data.gate_events as Array<Record<string, unknown>>) : cc.gate_events,
        last_gate_event:
          data.last_gate_event && typeof data.last_gate_event === "object"
            ? (data.last_gate_event as Record<string, unknown>)
            : data.last_gate_event === null
              ? null
              : cc.last_gate_event,
        credentialing_prerequisites:
          data.credentialing_prerequisites && typeof data.credentialing_prerequisites === "object"
            ? (data.credentialing_prerequisites as CredentialingPrerequisitesStatus)
            : cc.credentialing_prerequisites,
        workflow_follow_ups_by_step: Array.isArray(data.workflow_follow_ups_by_step)
          ? (data.workflow_follow_ups_by_step as CredentialingWorkflowStepRow[])
          : cc.workflow_follow_ups_by_step,
      };
      const parent = wrap.parentElement;
      const replacement = renderCredentialingCopilotPanel(next, threadId);
      parent?.replaceChild(replacement, wrap);
    } catch (e) {
      inlineErr.textContent = "Submission failed — please try again or accept the draft as-is.";
      inlineErr.style.display = "";
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
    const merged = { ...(cc.draft_output ?? {}), ...vo };
    ta.value = draftJsonForTextarea(merged);
    followTa.value = workflowFollowUpsDraftToLines(merged.workflow_follow_ups);
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
function renderRosterReportDownload(
  pdfBase64?: string | null,
  reportMarkdown?: string | null,
  attachmentsKind?: "reconciliation" | "credentialing" | null,
): HTMLElement {
  const wrap = document.createElement("div");
  wrap.className = "roster-report-download";

  const title = document.createElement("div");
  title.className = "roster-report-download-title";
  title.textContent =
    attachmentsKind === "reconciliation"
      ? "Roster alignment with NPPES (Phase 1)"
      : "Credentialing report";
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

  const pdfName =
    attachmentsKind === "reconciliation" ? "roster_reconciliation_report.pdf" : "credentialing_report.pdf";
  const mdName =
    attachmentsKind === "reconciliation" ? "roster_reconciliation_report.md" : "credentialing_report.md";

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
        a.download = pdfName;
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
      a.download = mdName;
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

/** First streamed assistant text that is not JSON placeholder → Answering phase. */
function thinkingStreamSuggestsAnswering(raw: string): boolean {
  const t = (raw ?? "").trim();
  const sanitized = sanitizeDisplayMessage(raw);
  const display = t.startsWith("{") ? "Formatting answer…" : normalizeMessageText(sanitized);
  return display.trim().length > 0 && display !== "Formatting answer…";
}

/** Reusable: user message bubble (right-aligned). */
const MODE_LABELS: Record<string, string> = {
  quick:   "⚡ Fast",
  copilot: "◉ Normal",
  agentic: "✦ Thinking",
};

function renderUserMessage(text: string, mode?: string): HTMLElement {
  const wrap = document.createElement("div");
  wrap.className = "message message--user";
  const bubble = document.createElement("div");
  bubble.className = "message-bubble";
  bubble.textContent = text;
  wrap.appendChild(bubble);
  if (mode && MODE_LABELS[mode]) {
    const badge = document.createElement("div");
    badge.className = "msg-mode-badge";
    badge.textContent = MODE_LABELS[mode];
    wrap.appendChild(badge);
  }
  return wrap;
}

/** Reusable: compact thinking line – streams in one line, collapses to summary when done.
 * Request phase (Queued → Working → Answering → Done) lives in the preview row with the pulsing dot — no separate rail.
 * Body shows emit lines; auto-scrolls on each addLine. */
function renderThinkingBlock(
  initialLines: string[],
  opts?: { onExpand?: () => void }
): {
  el: HTMLElement;
  setPreview: (text: string) => void;
  addLine: (line: string) => void;
  done: (lineCount: number) => void;
  onRequestCorrelationId: () => void;
  onRequestStreamChunk: (accumulatedRaw: string) => void;
  markRequestFailed: () => void;
} {
  const block = document.createElement("div");
  block.className = "thinking-block thinking-block--compact" + (initialLines.length ? "" : " collapsed");
  block.setAttribute("aria-busy", "true");

  const preview = document.createElement("div");
  preview.className = "thinking-preview";
  preview.setAttribute("role", "button");
  preview.setAttribute("tabindex", "0");
  preview.setAttribute("aria-expanded", initialLines.length > 0 ? "true" : "false");

  const phaseRow = document.createElement("span");
  phaseRow.className = "thinking-phase thinking-phase--live";
  phaseRow.setAttribute("aria-hidden", "true");
  const phaseDot = document.createElement("span");
  phaseDot.className = "thinking-phase-dot";
  const phaseLabel = document.createElement("span");
  phaseLabel.className = "thinking-phase-label";
  phaseLabel.textContent = "Queued";
  phaseRow.appendChild(phaseDot);
  phaseRow.appendChild(phaseLabel);

  const statusWord = document.createElement("span");
  statusWord.className = "thinking-word";
  statusWord.textContent = "Thinking";

  const lineEl = document.createElement("span");
  lineEl.className = "thinking-rule";

  preview.appendChild(phaseRow);
  preview.appendChild(statusWord);
  preview.appendChild(lineEl);

  const announcer = document.createElement("span");
  announcer.className = "thinking-phase-announcer";
  announcer.setAttribute("aria-live", "polite");
  announcer.setAttribute("aria-atomic", "true");

  const body = document.createElement("div");
  body.className = "thinking-body";
  initialLines.forEach((line) => {
    const div = document.createElement("div");
    div.className = "thinking-line";
    div.textContent = line;
    body.appendChild(div);
  });

  let lastStatusLine = "";
  let requestPhase: 0 | 1 | 2 | 3 = 0;
  let failedRequest = false;

  const PHASE_ARIA = [
    "Request queued",
    "Working on your request",
    "Composing answer",
    "Complete",
  ] as const;

  function announcePhase(): void {
    if (failedRequest) {
      announcer.textContent = "Request ended with an error";
      return;
    }
    announcer.textContent = PHASE_ARIA[Math.min(requestPhase, 3)] ?? "";
  }

  function syncPhaseRow(): void {
    phaseRow.classList.remove("thinking-phase--live", "thinking-phase--done", "thinking-phase--error");
    if (failedRequest) {
      phaseRow.classList.add("thinking-phase--error");
      phaseLabel.textContent = "Error";
    } else if (requestPhase >= 3) {
      phaseRow.classList.add("thinking-phase--done");
      phaseLabel.textContent = "Done";
    } else {
      phaseRow.classList.add("thinking-phase--live");
      const labels = ["Queued", "Working", "Answering"] as const;
      phaseLabel.textContent = labels[Math.min(requestPhase, 2)] ?? "Queued";
    }
    announcePhase();
  }

  syncPhaseRow();

  if (initialLines.length) {
    lastStatusLine = initialLines[initialLines.length - 1] ?? "";
    if (lastStatusLine) statusWord.textContent = thinkingFriendlyStatus(lastStatusLine);
  }

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
  block.appendChild(announcer);
  block.appendChild(body);

  return {
    el: block,
    setPreview(text: string) {
      lastStatusLine = text;
      statusWord.textContent = thinkingFriendlyStatus(text);
      syncPhaseRow();
    },
    addLine(line: string) {
      lastStatusLine = line;
      statusWord.textContent = thinkingFriendlyStatus(line);
      const div = document.createElement("div");
      div.className = "thinking-line";
      div.textContent = line;
      body.appendChild(div);
      block.classList.remove("collapsed");
      preview.setAttribute("aria-expanded", "true");
      body.scrollTop = body.scrollHeight;
    },
    done(_lineCount: number) {
      if (!failedRequest) requestPhase = 3;
      syncPhaseRow();
      statusWord.textContent = lastStatusLine ? thinkingFriendlyStatus(lastStatusLine) : "Ready";
      block.setAttribute("aria-busy", "false");
      block.classList.add("thinking-block--done");
      setTimeout(() => {
        collapse();
      }, 2500);
    },
    onRequestCorrelationId(): void {
      if (failedRequest || requestPhase >= 1) return;
      requestPhase = 1;
      syncPhaseRow();
    },
    onRequestStreamChunk(accumulatedRaw: string): void {
      if (failedRequest || requestPhase >= 2) return;
      if (thinkingStreamSuggestsAnswering(accumulatedRaw)) {
        requestPhase = 2;
        syncPhaseRow();
      }
    },
    markRequestFailed(): void {
      failedRequest = true;
      block.setAttribute("aria-busy", "false");
      syncPhaseRow();
    },
  };
}

/** Replace the #chat-suggestions slot with chips for the latest answer's follow-ups. */
function followupChipToQuery(text: string): string {
  const t = text.trim().replace(/\?$/, "");
  let m: RegExpMatchArray | null;
  m = t.match(/^Would you like (?:me )?to (.+)$/i);
  if (m) return "Please " + m[1].charAt(0).toLowerCase() + m[1].slice(1) + ".";
  m = t.match(/^Do you want (?:me )?to (.+)$/i);
  if (m) return "Please " + m[1].charAt(0).toLowerCase() + m[1].slice(1) + ".";
  m = t.match(/^Shall I (?:show|walk) you (.+)$/i);
  if (m) return "Please show me " + m[1] + ".";
  m = t.match(/^(?:Can|Shall) I help you with (.+)$/i);
  if (m) return "Help me with " + m[1] + ".";
  return text.trim();
}

function updateChatSuggestions(
  questions: FollowupLineNormalized[],
  onSelect: (q: string) => void
): void {
  const slot = document.getElementById("chat-suggestions") as HTMLElement | null;
  if (!slot) return;
  slot.innerHTML = "";
  const clickable = questions.filter((q) => q.clickable && q.text.trim());
  if (!clickable.length) {
    slot.hidden = true;
    return;
  }
  const chips = document.createElement("div");
  chips.className = "chat-suggestions-chips";
  for (const q of clickable.slice(0, 4)) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "chat-suggestions-chip";
    btn.textContent = q.text.trim();
    btn.setAttribute("aria-label", "Ask: " + q.text.trim());
    btn.addEventListener("click", () => {
      slot.innerHTML = "";
      slot.hidden = true;
      onSelect(followupChipToQuery(q.text));
    });
    chips.appendChild(btn);
  }
  slot.appendChild(chips);
  slot.hidden = false;
}

/** Reusable: next questions / follow-ups (clickable per item — legacy non-envelope turns). */
function renderNextQuestions(
  questions: FollowupLineNormalized[],
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
  hint.textContent = followupListHintLines(questions);
  wrap.appendChild(hint);
  const chips = document.createElement("div");
  chips.className = "next-questions-chips next-questions-chips--stacked";
  questions.slice(0, 6).forEach((line) => {
    const text = line.text.trim() || "Ask this";
    if (line.clickable) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "next-questions-chip next-questions-chip--row";
      btn.textContent = text;
      btn.setAttribute("aria-label", "Send: " + text);
      btn.addEventListener("click", () => onSelect(text));
      chips.appendChild(btn);
    } else {
      const row = document.createElement("div");
      row.className = "next-questions-line next-questions-line--static";
      row.textContent = text;
      chips.appendChild(row);
    }
  });
  wrap.appendChild(chips);
  return wrap;
}

function clarificationSelectionIsMultiple(opt: ClarificationOption): boolean {
  const m = (opt.selection_mode || "single").toLowerCase();
  return m === "multiple" || m === "multi";
}

const CLARIFICATION_FREE_TEXT_FALLBACK =
  "You can also type your own answer in the box below (optional), then press Send.";

function clarificationShowsFreeTextHint(opt: ClarificationOption): boolean {
  return opt.allow_free_text !== false;
}

/** Line to show under chip groups; null when chips-only (allow_free_text === false). */
function clarificationFreeTextHintLine(opt: ClarificationOption): string | null {
  if (!clarificationShowsFreeTextHint(opt)) {
    return null;
  }
  const h = (opt.free_text_hint || "").trim();
  return h || CLARIFICATION_FREE_TEXT_FALLBACK;
}

/** Multi-select: toggle chips; user presses main Send to submit selection + composer text. */
function renderClarificationMultiGroup(opt: ClarificationOption): HTMLElement {
  const group = document.createElement("div");
  group.className = "clarification-option-group clarification-option-group--multi";
  const labelEl = document.createElement("div");
  labelEl.className = "clarification-option-label";
  labelEl.textContent = opt.label;
  group.appendChild(labelEl);

  const n = opt.choices.length;
  let minC = opt.min_choices != null ? Math.max(0, opt.min_choices) : 1;
  let maxC = opt.max_choices != null ? Math.max(0, opt.max_choices) : n;
  minC = Math.min(minC, n);
  maxC = Math.min(maxC, n);
  if (maxC < minC) {
    maxC = minC;
  }

  const selected = new Set<string>();
  const chips = document.createElement("div");
  chips.className = "clarification-option-chips clarification-option-chips--multi";

  const hint = document.createElement("div");
  hint.className = "clarification-option-multi-hint";

  const slot = (opt.slot || "workflow_selection").trim();
  const draft: ClarificationDraftGroup = {
    slot,
    mode: "multiple",
    multiSelected: selected,
    singleSelected: null,
    minChoices: minC,
    maxChoices: maxC,
  };
  if (activeClarificationDraft) {
    activeClarificationDraft.push(draft);
  }

  function syncHintOnly() {
    if (minC === maxC) {
      hint.textContent = `Select exactly ${minC} option(s), add a message in the box below if you like, then press Send.`;
    } else {
      hint.textContent = `Select ${minC}–${maxC} option(s), type below (optional), then press Send.`;
    }
  }

  function toggleChoice(value: string, btn: HTMLButtonElement) {
    if (selected.has(value)) {
      selected.delete(value);
      btn.classList.remove("clarification-option-chip--selected");
      btn.setAttribute("aria-pressed", "false");
    } else {
      if (selected.size >= maxC) {
        return;
      }
      selected.add(value);
      btn.classList.add("clarification-option-chip--selected");
      btn.setAttribute("aria-pressed", "true");
    }
    syncHintOnly();
  }

  for (const c of opt.choices) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "clarification-option-chip clarification-option-chip--toggle";
    btn.textContent = c.label;
    btn.setAttribute("aria-pressed", "false");
    const val = c.value;
    btn.addEventListener("click", () => toggleChoice(val, btn));
    chips.appendChild(btn);
  }

  group.appendChild(chips);

  const footer = document.createElement("div");
  footer.className = "clarification-option-multi-footer";
  footer.appendChild(hint);
  group.appendChild(footer);
  syncHintOnly();
  return group;
}

/** Reusable: clarification chips; selections merge into the next composer Send. */
function renderClarificationOptions(opts: ClarificationOption[]): HTMLElement {
  activeClarificationDraft = [];
  const wrap = document.createElement("div");
  wrap.className = "clarification-options";
  for (const opt of opts) {
    if (clarificationSelectionIsMultiple(opt)) {
      wrap.appendChild(renderClarificationMultiGroup(opt));
      continue;
    }
    const group = document.createElement("div");
    group.className = "clarification-option-group";
    const labelEl = document.createElement("div");
    labelEl.className = "clarification-option-label";
    labelEl.textContent = opt.label;
    group.appendChild(labelEl);
    const chips = document.createElement("div");
    chips.className = "clarification-option-chips";
    group.appendChild(chips);

    const slot = (opt.slot || "workflow_selection").trim();
    const draft: ClarificationDraftGroup = {
      slot,
      mode: "single",
      multiSelected: new Set(),
      singleSelected: null,
      minChoices: 0,
      maxChoices: 1,
    };
    if (activeClarificationDraft) {
      activeClarificationDraft.push(draft);
    }

    for (const c of opt.choices) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "clarification-option-chip clarification-option-chip--toggle";
      btn.setAttribute("aria-pressed", "false");
      btn.textContent = c.label;
      btn.addEventListener("click", () => {
        chips.querySelectorAll("button.clarification-option-chip").forEach((b) => {
          b.classList.remove("clarification-option-chip--selected");
          b.setAttribute("aria-pressed", "false");
        });
        btn.classList.add("clarification-option-chip--selected");
        btn.setAttribute("aria-pressed", "true");
        draft.singleSelected = c.value;
      });
      chips.appendChild(btn);
    }
    const hintSingle = document.createElement("div");
    hintSingle.className = "clarification-option-free-text-hint";
    const freeLn = clarificationFreeTextHintLine(opt);
    hintSingle.textContent =
      freeLn || "Tap a choice, then press Send.";
    group.appendChild(hintSingle);
    wrap.appendChild(group);
  }
  if (!activeClarificationDraft.length) {
    activeClarificationDraft = null;
  }
  return wrap;
}

/** Reusable: assistant message bubble (left-aligned). Always includes confidence badge. */
function renderAssistantMessage(
  text: string,
  isError?: boolean,
  opts?: { sourceConfidenceStrip?: string; variant?: "warn" | "error" }
): HTMLElement {
  const variantClass = opts?.variant === "warn"
    ? " message--warn"
    : (isError || opts?.variant === "error") ? " message--error" : "";
  const wrap = document.createElement("div");
  wrap.className = "message message--assistant" + variantClass;
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
  up.dataset.tourId = "msg-thumbs-up";
  up.appendChild(createThumbIcon("up"));
  const down = document.createElement("button");
  down.type = "button";
  down.className = "feedback-thumb";
  down.setAttribute("aria-label", "Bad response");
  down.dataset.tourId = "msg-thumbs-down";
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
        // Refresh sidebar so the most-helpful lists update immediately
        // after a thumbs-up without waiting for the next page load.
        if (rating === "up") {
          window.dispatchEvent(new CustomEvent("mobiusFeedbackUp"));
        }
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

  // Email button — opens dialog to email the thread or last exchange.
  // Uses POST /chat/thread/{id}/email which proxies to mobius-skills/email.
  const emailBtn = document.createElement("button");
  emailBtn.type = "button";
  emailBtn.setAttribute("aria-label", "Email this conversation");
  emailBtn.dataset.tourId = "msg-email";
  emailBtn.textContent = "Email";
  emailBtn.addEventListener("click", () => {
    const tid = window.__mobiusChatThreadId || null;
    if (!tid) {
      _showToast("No active thread to email");
      return;
    }
    openEmailThreadDialog(tid);
  });

  // Task button — opens the Tasks modal prefilled from this message so a
  // follow-up can be logged without leaving the thread.
  const taskActionBtn = document.createElement("button");
  taskActionBtn.type = "button";
  taskActionBtn.setAttribute("aria-label", "Create or review tasks");
  taskActionBtn.textContent = "Task";
  taskActionBtn.addEventListener("click", () => {
    const msg = bar.closest(".chat-turn")?.querySelector(".message--assistant .message-bubble");
    const excerpt = (msg?.textContent || "").trim().slice(0, 400);
    openCreateTaskDialog(excerpt ? { excerpt, title: excerpt.slice(0, 60), sourceModule: "chat_action" } : undefined);
  });

  left.appendChild(up);
  left.appendChild(down);
  left.appendChild(commentArea);
  actions.appendChild(copy);
  actions.appendChild(emailBtn);
  actions.appendChild(taskActionBtn);
  bar.appendChild(left);
  bar.appendChild(actions);
  return bar;
}


// ─── Product-feedback UI components ──────────────────────────────────────────

const _PF_CATEGORY_LABELS: Record<string, string> = {
  accuracy_trust: "Accuracy",
  coverage_gap: "Coverage gap",
  bug: "Bug",
  speed: "Speed",
  usability: "Usability",
  feature_request: "Feature request",
  praise: "Praise",
  other: "Other",
  docs_gap: "Docs gap",
  doc_stale: "Stale doc",
};

/** Confirmation card shown after the product_feedback skill captures inline feedback.
 *  Lets the user optionally edit the tidied text / category before dismissing. */
function renderCaptureCard(
  card: NonNullable<ChatResponse["capture_card"]>,
  meta: { threadId?: string; correlationId?: string }
): HTMLElement {
  const wrap = document.createElement("div");
  wrap.className = "pf-capture-card";
  wrap.dataset.tourId = "msg-capture-card";

  const header = document.createElement("div");
  header.className = "pf-capture-card__header";
  const title = document.createElement("span");
  title.innerHTML = '<span class="pf-capture-card__check">✓</span> Feedback captured';
  const xBtn = document.createElement("button");
  xBtn.type = "button";
  xBtn.className = "pf-capture-card__x";
  xBtn.setAttribute("aria-label", "Dismiss");
  xBtn.textContent = "✕";
  header.appendChild(title);
  header.appendChild(xBtn);
  wrap.appendChild(header);

  const body = document.createElement("div");
  body.className = "pf-capture-card__body";

  // Category pill chips
  const catChips = document.createElement("div");
  catChips.className = "pf-capture-card__cat-chips";
  let selectedCat = card.category;
  for (const c of card.categories) {
    const chip = document.createElement("button");
    chip.type = "button";
    chip.className = "pf-cat-chip" + (c === selectedCat ? " pf-cat-chip--active" : "");
    chip.textContent = _PF_CATEGORY_LABELS[c] ?? c;
    chip.dataset.cat = c;
    if (!card.editable) chip.disabled = true;
    chip.addEventListener("click", () => {
      selectedCat = c;
      catChips.querySelectorAll(".pf-cat-chip").forEach((b) => b.classList.remove("pf-cat-chip--active"));
      chip.classList.add("pf-cat-chip--active");
    });
    catChips.appendChild(chip);
  }
  body.appendChild(catChips);

  // Tidied text
  const ta = document.createElement("textarea");
  ta.className = "pf-capture-card__text";
  ta.value = card.tidied;
  ta.rows = 3;
  ta.readOnly = !card.editable;
  body.appendChild(ta);

  // Buttons
  const btnRow = document.createElement("div");
  btnRow.className = "pf-capture-card__btns";
  const doneBtn = document.createElement("button");
  doneBtn.type = "button";
  doneBtn.className = "pf-capture-card__done";
  doneBtn.textContent = "Done";
  btnRow.appendChild(doneBtn);
  body.appendChild(btnRow);
  wrap.appendChild(body);

  let _isDirty = false;
  ta.addEventListener("input", () => { _isDirty = true; });

  function pfEvent(action: string): void {
    fetch(API_BASE + "/chat/product-feedback/event", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        trigger: "inline", action,
        feedback_id: card.feedback_id,
        thread_id: meta.threadId,
      }),
    }).catch(() => {});
  }

  if (card.editable) {
    const updateBtn = document.createElement("button");
    updateBtn.type = "button";
    updateBtn.className = "pf-capture-card__update";
    updateBtn.textContent = "Update";
    updateBtn.style.display = "none";
    btnRow.insertBefore(updateBtn, doneBtn);
    ta.addEventListener("input", () => { updateBtn.style.display = ""; });
    updateBtn.addEventListener("click", () => {
      const txt = ta.value.trim();
      if (!txt) return;
      const url = card.update_url ?? "/chat/product-feedback/update";
      fetch(API_BASE + url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          feedback_id: card.feedback_id,
          category: selectedCat,
          tidied: txt,
        }),
      }).catch(() => {});
      wrap.remove();
    });
  }

  function dismiss(): void { pfEvent("dismissed"); wrap.remove(); }
  doneBtn.addEventListener("click", dismiss);
  xBtn.addEventListener("click", dismiss);

  pfEvent("shown");
  return wrap;
}

/** "▶ Show me" demo chip from the Product Awareness / Interact engine. */
function renderDemoChip(
  demo: NonNullable<ChatResponse["demo"]>,
  meta: { correlationId?: string }
): HTMLElement {
  const wrap = document.createElement("div");
  wrap.className = "demo-chip";

  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "demo-chip__btn";
  btn.textContent = "▶ Show me";
  btn.title = demo.title;
  wrap.appendChild(btn);

  const INTERACT_BASE = "https://mobius-interact-ortabkknqa-uc.a.run.app";

  btn.addEventListener("click", () => {
    btn.disabled = true;
    btn.textContent = "Loading…";
    fetch(INTERACT_BASE + "/scripts/" + encodeURIComponent(demo.script_id))
      .then((r) => {
        if (!r.ok) throw new Error("script fetch " + r.status);
        return r.json();
      })
      .then((script) => {
        const MI = (window as unknown as Record<string, unknown>)["MobiusInteract"] as {
          run: (script: unknown, opts: {
            onAbort?: () => void;
            onDone?: () => void;
            correlationId?: string;
          }) => void
        } | undefined;
        if (!MI) throw new Error("MobiusInteract runner not loaded");
        btn.textContent = "▶ Show me";
        btn.disabled = false;
        // Expand collapsed sidebar before running any tour so sidebar-targeting steps are visible.
        const _sb = document.getElementById("sidebar");
        if (_sb?.classList.contains("sidebar--collapsed")) {
          _sb.classList.remove("sidebar--collapsed");
          document.querySelector<HTMLElement>(".main")?.classList.remove("sidebar-collapsed");
        }
        MI.run(script, {
          correlationId: meta.correlationId,
          onAbort: () => { btn.disabled = false; },
          onDone:  () => { btn.disabled = false; },
        });
      })
      .catch(() => {
        btn.textContent = "▶ Show me";
        btn.disabled = false;
      });
  });

  return wrap;
}

/** Periodic survey chip surfaced by the planner (NPS 0-10, CSAT 1-5, or open text). */
function renderOfferFeedback(
  offer: NonNullable<ChatResponse["offer_feedback"]>,
  meta: { threadId?: string; correlationId?: string }
): HTMLElement {
  const wrap = document.createElement("div");
  wrap.className = "pf-offer-chip";

  // Server-supplied prompt wins; fall back to sensible defaults.
  const FALLBACK_PROMPTS: Record<string, string> = {
    nps:           "How likely are you to recommend Mobius to a colleague?",
    csat:          "How satisfied are you with this answer?",
    targeted_miss: "What were you trying to find?",
    generic:       "Any feedback for us?",
  };
  const promptText = offer.prompt ?? FALLBACK_PROMPTS[offer.kind] ?? "Any feedback?";

  const header = document.createElement("div");
  header.className = "pf-offer-chip__header";
  const q = document.createElement("span");
  q.textContent = promptText;
  const xBtn = document.createElement("button");
  xBtn.type = "button";
  xBtn.className = "pf-offer-chip__x";
  xBtn.setAttribute("aria-label", "No thanks");
  xBtn.textContent = "✕";
  header.appendChild(q);
  header.appendChild(xBtn);
  wrap.appendChild(header);

  const body = document.createElement("div");
  body.className = "pf-offer-chip__body";
  wrap.appendChild(body);

  function pfEvent(action: string, score?: number): void {
    fetch(API_BASE + "/chat/product-feedback/event", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        trigger: offer.trigger, action, kind: offer.kind,
        score, thread_id: meta.threadId,
      }),
    }).catch(() => {});
  }

  function showThanks(): void {
    body.innerHTML = "";
    const t = document.createElement("span");
    t.className = "pf-offer-chip__thanks";
    t.textContent = "Thanks for your feedback!";
    body.appendChild(t);
    xBtn.remove();
    setTimeout(() => wrap.remove(), 2500);
  }

  /** After scoring, optionally show a reason box using followup_prompt from the score response. */
  function showFollowup(followupPrompt: string, parentFeedbackId: string): void {
    body.innerHTML = "";
    const ta = document.createElement("textarea");
    ta.className = "pf-offer-chip__text";
    ta.rows = 2;
    ta.placeholder = followupPrompt;
    const row = document.createElement("div");
    row.className = "pf-offer-chip__followup-row";
    const skip = document.createElement("button");
    skip.type = "button";
    skip.className = "pf-offer-chip__skip";
    skip.textContent = "Skip";
    const submit = document.createElement("button");
    submit.type = "button";
    submit.className = "pf-offer-chip__submit";
    submit.textContent = "Send";
    row.appendChild(skip);
    row.appendChild(submit);
    body.appendChild(ta);
    body.appendChild(row);
    submit.addEventListener("click", () => {
      const txt = ta.value.trim();
      if (!txt) { showThanks(); return; }
      fetch(API_BASE + "/chat/product-feedback", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          verbatim: txt, category: "other", trigger: offer.trigger,
          parent_feedback_id: parentFeedbackId,
          thread_id: meta.threadId, correlation_id: meta.correlationId,
        }),
      }).catch(() => {});
      showThanks();
    });
    skip.addEventListener("click", showThanks);
  }

  const isNumeric = offer.kind === "nps" || offer.kind === "csat";

  if (isNumeric) {
    // Data-driven scale — server supplies min/max/labels; fall back to convention.
    const sc = offer.scale ?? (offer.kind === "nps"
      ? { min: 0, max: 10, min_label: "Not likely", max_label: "Very likely" }
      : { min: 1, max: 5, min_label: "Poor", max_label: "Great" });
    const scaleEl = document.createElement("div");
    scaleEl.className = "pf-offer-chip__scale";
    for (let i = sc.min; i <= sc.max; i++) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "pf-offer-chip__score-btn";
      btn.textContent = String(i);
      btn.addEventListener("click", () => {
        pfEvent("scored", i);
        const postTo = offer.post_to ?? "/chat/product-feedback/score";
        fetch(API_BASE + postTo, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            survey_type: offer.survey_type ?? offer.kind,
            score: i, trigger: offer.trigger,
            thread_id: meta.threadId, correlation_id: meta.correlationId,
          }),
        })
          .then((r) => r.json())
          .then((data: { feedback_id?: string; followup_prompt?: string }) => {
            if (data.followup_prompt && data.feedback_id) {
              showFollowup(data.followup_prompt, data.feedback_id);
            } else {
              showThanks();
            }
          })
          .catch(showThanks);
      });
      scaleEl.appendChild(btn);
    }
    body.appendChild(scaleEl);
    const lbl = document.createElement("div");
    lbl.className = "pf-offer-chip__scale-labels";
    const lo = document.createElement("span"); lo.textContent = sc.min_label;
    const hi = document.createElement("span"); hi.textContent = sc.max_label;
    lbl.appendChild(lo); lbl.appendChild(hi);
    body.appendChild(lbl);
  } else {
    // generic / targeted_miss — CTA button expands to a textarea.
    const ctaBtn = document.createElement("button");
    ctaBtn.type = "button";
    ctaBtn.className = "pf-offer-chip__cta";
    ctaBtn.textContent = offer.cta ?? "Share feedback";
    body.appendChild(ctaBtn);
    ctaBtn.addEventListener("click", () => {
      body.innerHTML = "";
      pfEvent("opened");
      const ta = document.createElement("textarea");
      ta.className = "pf-offer-chip__text";
      ta.rows = 2;
      ta.placeholder = "Your feedback…";
      const submitBtn = document.createElement("button");
      submitBtn.type = "button";
      submitBtn.className = "pf-offer-chip__submit";
      submitBtn.textContent = "Submit";
      submitBtn.addEventListener("click", () => {
        const txt = ta.value.trim();
        if (!txt) return;
        const postTo = offer.post_to ?? "/chat/product-feedback";
        fetch(API_BASE + postTo, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            verbatim: txt, category: "other", trigger: offer.trigger,
            thread_id: meta.threadId, correlation_id: meta.correlationId,
          }),
        }).catch(() => {});
        pfEvent("submitted");
        showThanks();
      });
      body.appendChild(ta);
      body.appendChild(submitBtn);
      ta.focus();
    });
  }

  xBtn.addEventListener("click", () => { pfEvent("dismissed"); wrap.remove(); });

  pfEvent("shown");
  return wrap;
}

// ─────────────────────────────────────────────────────────────────────────────

/** Email-thread dialog: recipient + scope + mode → Preview → Send.
 *
 * Two-step flow:
 *   1. Preview → POST with confirm_before_send=true → renders drafted
 *      subject+body in a read-only preview pane.
 *   2. Send    → POST with confirm_before_send=false (same key, replays
 *      the pending_confirm row and releases via the email-skill chokepoint).
 */
function openEmailThreadDialog(threadId: string): void {
  // Don't double-open if one is already mounted
  if (document.querySelector(".email-thread-dialog")) return;

  const overlay = document.createElement("div");
  overlay.className = "email-thread-dialog-overlay";
  Object.assign(overlay.style, {
    position: "fixed", inset: "0", background: "rgba(0,0,0,0.4)",
    display: "flex", alignItems: "center", justifyContent: "center",
    zIndex: "10000",
  });

  const dialog = document.createElement("div");
  dialog.className = "email-thread-dialog";
  dialog.setAttribute("role", "dialog");
  dialog.setAttribute("aria-modal", "true");
  dialog.setAttribute("aria-label", "Email this conversation");
  Object.assign(dialog.style, {
    background: "var(--main-bg, #fff)",
    color: "var(--main-text, #111)",
    borderRadius: "8px",
    padding: "20px",
    width: "min(560px, 92vw)",
    maxHeight: "92vh",
    overflowY: "auto",
    boxShadow: "0 8px 32px rgba(0,0,0,0.18)",
    fontFamily: "inherit",
  });

  const title = document.createElement("h3");
  title.textContent = "Email this conversation";
  Object.assign(title.style, { margin: "0 0 12px 0", fontSize: "1.05rem" });
  dialog.appendChild(title);

  // Recipient
  const toLabel = document.createElement("label");
  toLabel.textContent = "Send to";
  Object.assign(toLabel.style, { display: "block", fontSize: "0.85rem",
                                  marginBottom: "4px", color: "var(--sidebar-text-muted, #555)" });
  const toInput = document.createElement("input");
  toInput.type = "email";
  toInput.placeholder = "name@example.com";
  toInput.required = true;
  Object.assign(toInput.style, {
    width: "100%", boxSizing: "border-box", padding: "8px 10px",
    border: "1px solid var(--border, #ccc)", borderRadius: "4px",
    fontSize: "0.95rem", marginBottom: "14px",
  });

  // Scope
  const scopeLabel = document.createElement("div");
  scopeLabel.textContent = "What to include";
  Object.assign(scopeLabel.style, { fontSize: "0.85rem", marginBottom: "4px",
                                     color: "var(--sidebar-text-muted, #555)" });
  const scopeWrap = document.createElement("div");
  Object.assign(scopeWrap.style, { display: "flex", gap: "16px", marginBottom: "14px" });
  const scopeThread = _radio("scope", "thread", "Whole thread", true);
  const scopeLast = _radio("scope", "last", "Last exchange", false);
  scopeWrap.appendChild(scopeThread.wrap);
  scopeWrap.appendChild(scopeLast.wrap);

  // Mode
  const modeLabel = document.createElement("div");
  modeLabel.textContent = "How to format";
  Object.assign(modeLabel.style, { fontSize: "0.85rem", marginBottom: "4px",
                                    color: "var(--sidebar-text-muted, #555)" });
  const modeWrap = document.createElement("div");
  Object.assign(modeWrap.style, { display: "flex", gap: "16px", marginBottom: "14px" });
  const modeSummary = _radio("mode", "summary", "Summarize (LLM)", true);
  const modeFull = _radio("mode", "full", "Full transcript", false);
  modeWrap.appendChild(modeSummary.wrap);
  modeWrap.appendChild(modeFull.wrap);

  // Preview area (initially hidden)
  const preview = document.createElement("div");
  preview.className = "email-thread-preview";
  Object.assign(preview.style, {
    display: "none", border: "1px solid var(--border, #ccc)", borderRadius: "4px",
    padding: "10px 12px", marginBottom: "12px", background: "var(--thinking-bg, #fafafa)",
    maxHeight: "260px", overflowY: "auto", whiteSpace: "pre-wrap",
    fontSize: "0.85rem",
  });

  // Status line
  const status = document.createElement("div");
  Object.assign(status.style, { fontSize: "0.85rem", marginBottom: "10px",
                                 color: "var(--sidebar-text-muted, #666)", minHeight: "18px" });

  // Buttons
  const btnRow = document.createElement("div");
  Object.assign(btnRow.style, { display: "flex", gap: "8px", justifyContent: "flex-end" });

  const cancelBtn = document.createElement("button");
  cancelBtn.type = "button";
  cancelBtn.textContent = "Cancel";
  Object.assign(cancelBtn.style, _btnStyle("secondary"));

  const previewBtn = document.createElement("button");
  previewBtn.type = "button";
  previewBtn.textContent = "Preview";
  Object.assign(previewBtn.style, _btnStyle("primary"));

  const sendBtn = document.createElement("button");
  sendBtn.type = "button";
  sendBtn.textContent = "Send";
  Object.assign(sendBtn.style, _btnStyle("primary"));
  sendBtn.style.display = "none";  // shown after preview succeeds

  btnRow.appendChild(cancelBtn);
  btnRow.appendChild(previewBtn);
  btnRow.appendChild(sendBtn);

  dialog.appendChild(toLabel);
  dialog.appendChild(toInput);
  dialog.appendChild(scopeLabel);
  dialog.appendChild(scopeWrap);
  dialog.appendChild(modeLabel);
  dialog.appendChild(modeWrap);
  dialog.appendChild(preview);
  dialog.appendChild(status);
  dialog.appendChild(btnRow);
  overlay.appendChild(dialog);
  document.body.appendChild(overlay);

  setTimeout(() => toInput.focus(), 50);

  const close = () => overlay.remove();
  cancelBtn.addEventListener("click", close);
  overlay.addEventListener("click", (ev) => { if (ev.target === overlay) close(); });

  let lockedPayload: { to: string[]; scope: string; mode: string } | null = null;

  const setBusy = (busy: boolean) => {
    previewBtn.disabled = busy;
    sendBtn.disabled = busy;
    toInput.disabled = busy;
    [scopeThread.input, scopeLast.input, modeSummary.input, modeFull.input]
      .forEach((el) => { el.disabled = busy; });
  };

  previewBtn.addEventListener("click", async () => {
    const to = (toInput.value || "").trim();
    if (!to || !to.includes("@")) {
      status.textContent = "Enter a valid email address.";
      status.style.color = "#c0392b";
      return;
    }
    const scope = scopeThread.input.checked ? "thread" : "last";
    const mode = modeSummary.input.checked ? "summary" : "full";
    status.textContent = "Drafting…";
    status.style.color = "var(--sidebar-text-muted, #666)";
    setBusy(true);
    try {
      const res = await fetch(`${API_BASE}/chat/thread/${encodeURIComponent(threadId)}/email`, {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ to: [to], scope, mode, confirm_before_send: true }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        status.textContent = `Failed: ${(data && (data.detail?.message || data.detail)) || res.statusText}`;
        status.style.color = "#c0392b";
        return;
      }
      const draft = data.draft || {};
      preview.style.display = "block";
      preview.textContent =
        `To: ${(draft.to || []).join(", ")}\n` +
        `Subject: ${draft.subject || ""}\n\n` +
        `${draft.body || ""}`;
      status.textContent = "Review the draft, then click Send.";
      status.style.color = "var(--sidebar-text-muted, #666)";
      sendBtn.style.display = "";
      previewBtn.textContent = "Re-draft";
      lockedPayload = { to: [to], scope, mode };
    } catch (err: any) {
      status.textContent = `Error: ${err?.message || err}`;
      status.style.color = "#c0392b";
    } finally {
      setBusy(false);
    }
  });

  sendBtn.addEventListener("click", async () => {
    if (!lockedPayload) return;
    setBusy(true);
    status.textContent = "Sending…";
    status.style.color = "var(--sidebar-text-muted, #666)";
    try {
      const res = await fetch(`${API_BASE}/chat/thread/${encodeURIComponent(threadId)}/email`, {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ...lockedPayload, confirm_before_send: false }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok || !data.sent) {
        status.textContent = `Send failed: ${(data && (data.error || data.detail?.message || data.detail)) || res.statusText}`;
        status.style.color = "#c0392b";
        sendBtn.disabled = false;
        return;
      }
      _showToast("Email sent");
      close();
    } catch (err: any) {
      status.textContent = `Error: ${err?.message || err}`;
      status.style.color = "#c0392b";
      setBusy(false);
    }
  });
}


function _radio(name: string, value: string, label: string, checked: boolean): {
  wrap: HTMLLabelElement; input: HTMLInputElement;
} {
  const wrap = document.createElement("label");
  Object.assign(wrap.style, { display: "flex", alignItems: "center", gap: "6px",
                               fontSize: "0.9rem", cursor: "pointer" });
  const input = document.createElement("input");
  input.type = "radio";
  input.name = name;
  input.value = value;
  input.checked = checked;
  const span = document.createElement("span");
  span.textContent = label;
  wrap.appendChild(input);
  wrap.appendChild(span);
  return { wrap, input };
}


function _btnStyle(variant: "primary" | "secondary"): Partial<CSSStyleDeclaration> {
  const base: Partial<CSSStyleDeclaration> = {
    padding: "8px 14px", borderRadius: "4px", border: "1px solid",
    fontSize: "0.9rem", cursor: "pointer",
  };
  if (variant === "primary") {
    base.background = "var(--primary, #2563eb)";
    base.color = "#fff";
    base.borderColor = "var(--primary, #2563eb)";
  } else {
    base.background = "transparent";
    base.color = "var(--foreground, #111)";
    base.borderColor = "var(--border, #ccc)";
  }
  return base;
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

/* ═══════════════════════════════════════════════════════════════════════════
   Doc Reader Panel — embeds @mobius/document-viewer via RAG frontend iframe
   ═══════════════════════════════════════════════════════════════════════════ */

// 2026-04-25: restored the original in-page inline reader (was replaced
// with a RAG-iframe panel in commit 324bf5a — operator preferred the
// inline experience). The panel calls /chat/doc-reader/read on the chat
// service (which proxies to mobius-doc-reader) and renders sections as
// expandable markdown cards with a TOC nav, citations, and the existing
// text-selection toolbar (copy/bookmark/cite).

interface DocReaderCitation {
  display?: string;
  page?: number | string;
  snippet?: string;
}
interface DocReaderSection {
  section_id?: string;
  heading?: string;
  depth?: number;
  page_start?: number | null;
  page_end?: number | null;
  body_markdown?: string;
  citations?: DocReaderCitation[];
}
interface DocReaderTocItem {
  section_id?: string;
  heading?: string;
  depth?: number;
  page_range?: string;
}
interface DocReaderEnvelope {
  document_id?: string;
  display_name?: string;
  payer?: string;
  authority_level?: string;
  toc?: DocReaderTocItem[];
  sections?: DocReaderSection[];
}

function _ensureDocReaderDOM(): void {
  if (document.getElementById("doc-reader-panel")) return;
  const overlay = document.createElement("div");
  overlay.id = "doc-reader-overlay";
  overlay.addEventListener("click", closeDocReaderPanel);
  document.body.appendChild(overlay);

  const panel = document.createElement("div");
  panel.id = "doc-reader-panel";
  panel.innerHTML =
    '<div class="doc-reader-header">' +
      '<span class="doc-reader-title">Loading…</span>' +
      '<span class="doc-reader-meta"></span>' +
      '<div class="doc-reader-header-actions">' +
        '<button class="bookmarks-btn" title="Bookmarks">Bookmarks <span class="bm-count">0</span></button>' +
        '<a class="doc-reader-rag-link" href="#" target="_blank" rel="noopener noreferrer">Open in RAG &#8599;</a>' +
        '<button class="doc-reader-close" title="Close">&times;</button>' +
      '</div>' +
    '</div>' +
    '<div class="doc-reader-body">' +
      '<nav class="doc-reader-toc"></nav>' +
      '<div class="doc-reader-content"></div>' +
    '</div>';
  panel.querySelector(".doc-reader-close")!.addEventListener("click", closeDocReaderPanel);
  const bmBtn = panel.querySelector(".bookmarks-btn") as HTMLButtonElement;
  bmBtn.addEventListener("click", () => _toggleBookmarksDrawer(bmBtn));
  document.body.appendChild(panel);
}

function _updateBookmarksBadge(panel: HTMLElement): void {
  try {
    const bm = JSON.parse(localStorage.getItem(_BOOKMARKS_KEY) || "[]") as unknown[];
    const badge = panel.querySelector(".bm-count");
    if (badge) badge.textContent = String(bm.length);
  } catch { /* no-op */ }
}

function openDocReaderPanel(documentId: string, pageNumber?: number | null, citeText?: string | null): void {
  if (!documentId) return;
  _ensureDocReaderDOM();
  const panel = document.getElementById("doc-reader-panel")!;
  const overlay = document.getElementById("doc-reader-overlay")!;
  const content = panel.querySelector(".doc-reader-content") as HTMLElement;
  const tocEl = panel.querySelector(".doc-reader-toc") as HTMLElement;
  const titleEl = panel.querySelector(".doc-reader-title") as HTMLElement;
  const metaEl = panel.querySelector(".doc-reader-meta") as HTMLElement;
  const ragLink = panel.querySelector(".doc-reader-rag-link") as HTMLAnchorElement;

  requestAnimationFrame(() => { overlay.classList.add("open"); panel.classList.add("open"); });

  content.innerHTML = '<div class="doc-reader-loading">Loading document\u2026</div>';
  tocEl.innerHTML = "";
  titleEl.textContent = "Loading\u2026";
  metaEl.textContent = "";

  const ragUrl = getRagDocumentUrl(documentId, pageNumber, citeText ?? null);
  if (ragUrl) { ragLink.href = ragUrl; ragLink.style.display = ""; }
  else { ragLink.style.display = "none"; }

  _updateBookmarksBadge(panel);

  const apiBase = (typeof API_BASE === "string" ? API_BASE : "").replace(/\/$/, "");
  fetch(apiBase + "/chat/doc-reader/read", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ document_id: documentId, view: "full" }),
  })
    .then((r) => {
      if (!r.ok) throw new Error(String(r.status));
      return r.json() as Promise<DocReaderEnvelope>;
    })
    .then((env) => _renderDocReaderEnvelope(env, pageNumber ?? null, citeText ?? null))
    .catch((err: Error) => {
      content.innerHTML = '<div class="doc-reader-error">Failed to load: ' + err.message + '</div>';
      titleEl.textContent = "Error";
    });
}

function _renderDocReaderEnvelope(
  env: DocReaderEnvelope,
  scrollToPage: number | string | null,
  highlightText: string | null,
): void {
  const panel = document.getElementById("doc-reader-panel");
  if (!panel) return;
  const content = panel.querySelector(".doc-reader-content") as HTMLElement;
  const tocEl = panel.querySelector(".doc-reader-toc") as HTMLElement;
  const titleEl = panel.querySelector(".doc-reader-title") as HTMLElement;
  const metaEl = panel.querySelector(".doc-reader-meta") as HTMLElement;

  titleEl.textContent = env.display_name || "Document";
  const parts: string[] = [];
  if (env.payer) parts.push(env.payer);
  if (env.authority_level) parts.push(env.authority_level);
  if (env.sections) parts.push(env.sections.length + " sections");
  metaEl.textContent = parts.join(" \u00b7 ");
  panel.dataset.docId = env.document_id || "";
  panel.dataset.docName = env.display_name || "";

  // TOC
  tocEl.innerHTML = "";
  (env.toc || []).forEach((t) => {
    const a = document.createElement("a");
    a.className = "doc-reader-toc-item" + ((t.depth || 0) > 1 ? " depth-" + t.depth : "");
    a.textContent = t.heading || "(untitled)";
    a.title = t.page_range || "";
    a.addEventListener("click", () => {
      const target = content.querySelector('[data-section-id="' + (t.section_id ?? "") + '"]') as HTMLElement | null;
      if (target) target.scrollIntoView({ behavior: "smooth", block: "start" });
      tocEl.querySelectorAll(".active").forEach((el) => el.classList.remove("active"));
      a.classList.add("active");
    });
    tocEl.appendChild(a);
  });

  // Sections (expandable cards with markdown body)
  content.innerHTML = "";
  let scrollTarget: HTMLElement | null = null;
  (env.sections || []).forEach((sec) => {
    const card = document.createElement("div");
    card.className = "doc-reader-section";
    card.dataset.sectionId = sec.section_id || "";
    card.dataset.pageStart = sec.page_start != null ? String(sec.page_start) : "";

    const header = document.createElement("div");
    header.className = "doc-reader-section-header";
    const hs = document.createElement("span");
    hs.textContent = sec.heading || "Section";
    const ps = document.createElement("span");
    ps.className = "doc-reader-section-page";
    ps.textContent = sec.page_start != null ? "p." + sec.page_start : "";
    header.appendChild(hs);
    header.appendChild(ps);

    const body = document.createElement("div");
    body.className = "doc-reader-section-body";
    let html = simpleMarkdownToHtml(sec.body_markdown || "");
    if (highlightText && highlightText.trim()) {
      const esc = highlightText.trim().replace(/[.*+?^${}()|[\]\\]/g, "\\$&").slice(0, 100);
      try {
        html = html.replace(new RegExp("(" + esc + ")", "gi"), '<mark class="doc-reader-highlight">$1</mark>');
      } catch { /* regex compile failed → render without highlight */ }
    }
    body.innerHTML = html;
    header.addEventListener("click", () => {
      body.style.display = body.style.display === "none" ? "" : "none";
    });
    card.appendChild(header);
    card.appendChild(body);

    if (sec.citations && sec.citations.length > 0) {
      const cr = document.createElement("div");
      cr.className = "doc-reader-section-citations";
      sec.citations.forEach((c) => {
        const badge = document.createElement("span");
        badge.className = "doc-reader-cite-badge";
        badge.textContent = c.display || ("p." + (c.page ?? ""));
        badge.title = (c.snippet || "").slice(0, 150);
        cr.appendChild(badge);
      });
      card.appendChild(cr);
    }

    content.appendChild(card);
    if (scrollToPage != null && String(sec.page_start) === String(scrollToPage)) {
      scrollTarget = card;
    }
  });

  if (scrollTarget) {
    setTimeout(() => (scrollTarget as HTMLElement).scrollIntoView({ behavior: "smooth", block: "start" }), 100);
  }
}

function closeDocReaderPanel(): void {
  const panel = document.getElementById("doc-reader-panel");
  const overlay = document.getElementById("doc-reader-overlay");
  if (panel) panel.classList.remove("open");
  if (overlay) overlay.classList.remove("open");
}

/* ── Roster Side Panel ─────────────────────────────────────────────────── */

function openRosterPanel(url: string): void {
  let overlay = document.getElementById("roster-panel-overlay");
  let panel = document.getElementById("roster-panel");

  if (!overlay) {
    overlay = document.createElement("div");
    overlay.id = "roster-panel-overlay";
    overlay.addEventListener("click", closeRosterPanel);
    document.body.appendChild(overlay);
  }

  if (!panel) {
    panel = document.createElement("div");
    panel.id = "roster-panel";
    panel.innerHTML =
      '<div class="roster-panel-header">' +
        '<span class="roster-panel-title">Roster</span>' +
        '<div class="roster-panel-header-actions">' +
          '<a class="roster-panel-external" href="#" target="_blank" rel="noopener noreferrer" title="Open in new tab">&#8599;</a>' +
          '<button class="roster-panel-close" title="Close">&times;</button>' +
        '</div>' +
      '</div>' +
      '<iframe class="roster-panel-frame" src="" allow="same-origin" sandbox="allow-same-origin allow-scripts allow-forms allow-popups"></iframe>';
    panel.querySelector(".roster-panel-close")!.addEventListener("click", closeRosterPanel);
    document.body.appendChild(panel);
  }

  const frame = panel.querySelector(".roster-panel-frame") as HTMLIFrameElement;
  const extLink = panel.querySelector(".roster-panel-external") as HTMLAnchorElement;
  frame.src = url;
  extLink.href = url;

  requestAnimationFrame(() => {
    overlay!.classList.add("open");
    panel!.classList.add("open");
  });
}

function closeRosterPanel(): void {
  document.getElementById("roster-panel-overlay")?.classList.remove("open");
  document.getElementById("roster-panel")?.classList.remove("open");
}

function _getPageFromElement(el: HTMLElement): number | string | null {
  const card = el.closest(".doc-reader-section") as HTMLElement | null;
  if (card && card.dataset.pageStart) return card.dataset.pageStart;
  return null;
}

function _toggleBookmarksDrawer(btn: HTMLButtonElement): void {
  // Toggle: if already open, close it.
  const existing = btn.querySelector(".bookmarks-drawer");
  if (existing) { existing.remove(); return; }
  const drawer = document.createElement("div");
  drawer.className = "bookmarks-drawer";
  // Stop drawer-internal clicks from bubbling to the document close
  // handler — without this, clicking a bookmark item registers a
  // document-level click and tears the drawer down before the
  // item's own click handler runs.
  drawer.addEventListener("click", (e) => e.stopPropagation());
  let bm: any[] = [];
  try { bm = JSON.parse(localStorage.getItem(_BOOKMARKS_KEY) || "[]"); } catch { bm = []; }
  if (bm.length === 0) {
    drawer.innerHTML = '<div class="bookmarks-drawer-empty">No bookmarks yet. Select text and click Bookmark.</div>';
  } else {
    bm.forEach((b: any, idx: number) => {
      const item = document.createElement("div");
      item.className = "bookmark-item";
      const te = document.createElement("div"); te.className = "bookmark-text"; te.textContent = b.text || "";
      const me = document.createElement("div"); me.className = "bookmark-meta";
      const info = document.createElement("span");
      info.textContent = (b.documentName || "Doc") + (b.page ? ", p." + b.page : "")
        + " \u00b7 " + new Date(b.timestamp || Date.now()).toLocaleDateString();
      const del = document.createElement("button"); del.className = "bookmark-delete"; del.textContent = "Remove";
      del.addEventListener("click", (e: Event) => {
        e.stopPropagation();
        bm.splice(idx, 1);
        localStorage.setItem(_BOOKMARKS_KEY, JSON.stringify(bm));
        item.remove();
        if (bm.length === 0) drawer.innerHTML = '<div class="bookmarks-drawer-empty">No bookmarks.</div>';
        const p = document.getElementById("doc-reader-panel");
        if (p) _updateBookmarksBadge(p);
      });
      me.appendChild(info); me.appendChild(del);
      item.appendChild(te); item.appendChild(me);
      item.addEventListener("click", () => {
        if (b.documentId) openDocReaderPanel(b.documentId, b.page, (b.text || "").slice(0, 50));
        drawer.remove();
      });
      drawer.appendChild(item);
    });
  }
  // Append to the button itself — .bookmarks-btn has position:relative
  // so the drawer's `position: absolute; top: 100%; right: 0` resolves
  // against the button (not the header-actions flex container).
  btn.appendChild(drawer);
  const closeHandler = (e: Event) => {
    const t = e.target as Node;
    // Keep open when click is on the button (or its inner count span)
    // OR inside the drawer.
    if (drawer.contains(t) || btn.contains(t)) return;
    drawer.remove();
    document.removeEventListener("click", closeHandler);
  };
  setTimeout(() => document.addEventListener("click", closeHandler), 0);
}

document.addEventListener("keydown", (e: KeyboardEvent) => {
  if (e.key === "Escape") closeDocReaderPanel();
});

/* ═══════════════════════════════════════════════════════════════════════════
   Text Selection Toolbar — copy, bookmark, cite
   ═══════════════════════════════════════════════════════════════════════════ */

let _activeToolbar: HTMLElement | null = null;
const _BOOKMARKS_KEY = "mobius_bookmarks";

function _svgIcon(name: string): string {
  const icons: Record<string, string> = {
    copy: '<svg viewBox="0 0 16 16" fill="currentColor"><path d="M0 6.75C0 5.784.784 5 1.75 5h1.5a.75.75 0 010 1.5h-1.5a.25.25 0 00-.25.25v7.5c0 .138.112.25.25.25h7.5a.25.25 0 00.25-.25v-1.5a.75.75 0 011.5 0v1.5A1.75 1.75 0 019.25 16h-7.5A1.75 1.75 0 010 14.25z"/><path d="M5 1.75C5 .784 5.784 0 6.75 0h7.5C15.216 0 16 .784 16 1.75v7.5A1.75 1.75 0 0114.25 11h-7.5A1.75 1.75 0 015 9.25zm1.75-.25a.25.25 0 00-.25.25v7.5c0 .138.112.25.25.25h7.5a.25.25 0 00.25-.25v-7.5a.25.25 0 00-.25-.25z"/></svg>',
    bookmark: '<svg viewBox="0 0 16 16" fill="currentColor"><path d="M3 2.75C3 1.784 3.784 1 4.75 1h6.5c.966 0 1.75.784 1.75 1.75v11.5a.75.75 0 01-1.227.579L8 11.722l-3.773 3.107A.75.75 0 013 14.25zm1.75-.25a.25.25 0 00-.25.25v9.91l3.023-2.489a.75.75 0 01.954 0l3.023 2.49V2.75a.25.25 0 00-.25-.25z"/></svg>',
    cite: '<svg viewBox="0 0 16 16" fill="currentColor"><path d="M1.75 2h12.5c.966 0 1.75.784 1.75 1.75v8.5A1.75 1.75 0 0114.25 14H1.75A1.75 1.75 0 010 12.25v-8.5C0 2.784.784 2 1.75 2zm0 1.5a.25.25 0 00-.25.25v8.5c0 .138.112.25.25.25h12.5a.25.25 0 00.25-.25v-8.5a.25.25 0 00-.25-.25zM3.5 6.25a.75.75 0 01.75-.75h7.5a.75.75 0 010 1.5h-7.5a.75.75 0 01-.75-.75zm.75 2.25a.75.75 0 000 1.5h4a.75.75 0 000-1.5z"/></svg>',
    task: '<svg viewBox="0 0 16 16" fill="currentColor"><path d="M2.5 1.75a.25.25 0 01.25-.25h8.5a.25.25 0 01.25.25v.5h1.5v-.5A1.75 1.75 0 0011.25 0h-8.5A1.75 1.75 0 001 1.75v12.5c0 .966.784 1.75 1.75 1.75h4.5a.75.75 0 000-1.5h-4.5a.25.25 0 01-.25-.25zM4.75 4a.75.75 0 000 1.5h4.5a.75.75 0 000-1.5zm0 3a.75.75 0 000 1.5h2.5a.75.75 0 000-1.5zm10.28 2.72a.75.75 0 00-1.06-1.06L10.5 12.13l-1.47-1.47a.75.75 0 10-1.06 1.06l2 2a.75.75 0 001.06 0z"/></svg>',
  };
  return icons[name] || "";
}

function _removeToolbar(): void {
  if (_activeToolbar) { _activeToolbar.remove(); _activeToolbar = null; }
}

function _showToast(msg: string): void {
  const t = document.createElement("div");
  t.className = "tst-toast";
  t.textContent = msg;
  document.body.appendChild(t);
  setTimeout(() => t.remove(), 1800);
}

function _getDocContextFromElement(el: HTMLElement): { docName: string; docId: string } {
  // Prefer the inline doc-reader panel context when the selection is
  // inside it — that gives us the real document_id (so bookmarks can
  // reopen the same doc on click).
  const panel = el.closest("#doc-reader-panel") as HTMLElement | null;
  if (panel) {
    return {
      docName: panel.dataset.docName || "Document",
      docId: panel.dataset.docId || "",
    };
  }
  const envelope = el.closest(".assistant-envelope");
  if (envelope) {
    const sourceDoc = envelope.querySelector(".source-doc");
    if (sourceDoc) return { docName: sourceDoc.textContent || "Document", docId: "" };
  }
  return { docName: "Document", docId: "" };
}

function initTextSelectionToolbar(): void {
  document.addEventListener("mouseup", () => {
    setTimeout(() => {
      _removeToolbar();
      const sel = window.getSelection();
      const text = (sel?.toString() || "").trim();
      if (!text || text.length < 3) return;
      const anchor = sel!.anchorNode;
      if (!anchor) return;
      const container = (anchor.nodeType === 3 ? anchor.parentElement : anchor) as HTMLElement | null;
      if (!container) return;
      // 2026-04-25: also match the inline doc-reader content so the
      // toolbar (copy/bookmark/cite) works inside the restored panel.
      // 2026-07-07: also match chat message bubbles so "Create task"
      // (and copy/bookmark/cite) work on any assistant/user text —
      // EXCEPT inside interactive widgets that render within bubbles
      // (feedback capture cards, offer chips, survey widgets), where a
      // floating toolbar is noise over the widget's own controls.
      if (container.closest(".pf-capture-card") ||
          container.closest(".pf-offer-chip") ||
          container.closest(".pf-survey") ||
          container.closest(".feedback")) return;
      if (!container.closest(".envelope-detail-body") &&
          !container.closest("#doc-reader-panel .doc-reader-content") &&
          !container.closest(".message-bubble")) return;

      const range = sel!.getRangeAt(0);
      const rect = range.getBoundingClientRect();
      const ctx = _getDocContextFromElement(container);
      const page = _getPageFromElement(container);

      const toolbar = document.createElement("div");
      toolbar.className = "text-selection-toolbar";
      toolbar.style.top = (window.scrollY + rect.top - 42) + "px";
      toolbar.style.left = (window.scrollX + rect.left + rect.width / 2 - 100) + "px";

      const copyBtn = document.createElement("button");
      copyBtn.innerHTML = _svgIcon("copy") + " Copy";
      copyBtn.addEventListener("click", (ev) => {
        ev.stopPropagation();
        navigator.clipboard.writeText(text).then(() => _showToast("Copied to clipboard"));
        _removeToolbar();
      });
      toolbar.appendChild(copyBtn);

      const d1 = document.createElement("span"); d1.className = "tst-divider"; toolbar.appendChild(d1);

      const bmBtn = document.createElement("button");
      bmBtn.innerHTML = _svgIcon("bookmark") + " Bookmark";
      bmBtn.addEventListener("click", (ev) => {
        ev.stopPropagation();
        const bm: any[] = JSON.parse(localStorage.getItem(_BOOKMARKS_KEY) || "[]");
        bm.unshift({ text: text.slice(0, 500), documentName: ctx.docName, documentId: ctx.docId, page, timestamp: new Date().toISOString() });
        if (bm.length > 50) bm.length = 50;
        localStorage.setItem(_BOOKMARKS_KEY, JSON.stringify(bm));
        _showToast("Bookmarked");
        _removeToolbar();
        const p = document.getElementById("doc-reader-panel");
        if (p) _updateBookmarksBadge(p);
      });
      toolbar.appendChild(bmBtn);

      const d2 = document.createElement("span"); d2.className = "tst-divider"; toolbar.appendChild(d2);

      const citeBtn = document.createElement("button");
      citeBtn.innerHTML = _svgIcon("cite") + " Cite";
      citeBtn.addEventListener("click", (ev) => {
        ev.stopPropagation();
        const citation = "\u201c" + text.slice(0, 300) + "\u201d \u2014 " + ctx.docName;
        navigator.clipboard.writeText(citation).then(() => _showToast("Citation copied"));
        _removeToolbar();
      });
      toolbar.appendChild(citeBtn);

      const d3 = document.createElement("span"); d3.className = "tst-divider"; toolbar.appendChild(d3);

      const taskBtn = document.createElement("button");
      taskBtn.innerHTML = _svgIcon("task") + " Create task";
      taskBtn.addEventListener("click", (ev) => {
        ev.stopPropagation();
        const tid = (window as any).__mobiusChatThreadId || "";
        // Cheap stable hash of the selection for the dedup-safe source_ref.
        let h = 0;
        for (let i = 0; i < text.length; i++) { h = ((h << 5) - h + text.charCodeAt(i)) | 0; }
        openCreateTaskDialog({
          excerpt: text.slice(0, 600),
          title: text.slice(0, 60),
          sourceModule: "chat_highlight",
          sourceRef: `highlight:${tid || "nothread"}:${(h >>> 0).toString(16)}`,
        });
        _removeToolbar();
      });
      toolbar.appendChild(taskBtn);

      document.body.appendChild(toolbar);
      _activeToolbar = toolbar;
    }, 10);
  });
  document.addEventListener("mousedown", (e) => {
    if (_activeToolbar && !_activeToolbar.contains(e.target as Node)) _removeToolbar();
  });
}

if (typeof document !== "undefined") {
  if (document.readyState === "loading") { document.addEventListener("DOMContentLoaded", initTextSelectionToolbar); }
  else { initTextSelectionToolbar(); }
}

/* ═══════════════════════════════════════════════════════════════════════════
   Tasks Modal — create / review / edit / assign tasks
   Opened from: hamburger drawer, per-message task icon, selection toolbar
   ("Create task"), and task_list block Edit buttons. Talks to the
   /chat/tasks proxy (app/api/tasks.py → task-manager skill).
   ═══════════════════════════════════════════════════════════════════════════ */

let _tasksModalEl: HTMLElement | null = null;
let _tasksEscHandler: ((e: KeyboardEvent) => void) | null = null;

interface TasksModalPrefill {
  createOpen?: boolean;
  title?: string;
  text?: string;
  sourceModule?: string;
  sourceRef?: string;
  filterKind?: string;      // pre-select the kind filter (e.g. "reminder" from the nudge)
  filterAssignee?: string;  // pre-fill the assignee filter (e.g. my assignee_ref from the banner)
}

const _TASK_SEVERITIES = ["critical", "warning", "info", "low", "none"];

// ── Create Task Dialog (focused quick-create, no task list) ──────────────────

interface CreateTaskDialogOpts {
  excerpt?: string;
  title?: string;
  sourceModule?: string;
  sourceRef?: string;
  onCreated?: () => void;   // e.g. the Tasks modal refreshing its list
}

let _ctdOverlayEl: HTMLElement | null = null;
let _ctdEscHandler: ((e: KeyboardEvent) => void) | null = null;

function closeCreateTaskDialog(): void {
  if (_ctdOverlayEl) { _ctdOverlayEl.remove(); _ctdOverlayEl = null; }
  if (_ctdEscHandler) { document.removeEventListener("keydown", _ctdEscHandler); _ctdEscHandler = null; }
}

function openCreateTaskDialog(opts?: CreateTaskDialogOpts): void {
  closeCreateTaskDialog();

  const overlay = document.createElement("div");
  overlay.className = "ctd-overlay";
  overlay.addEventListener("mousedown", (e) => { if (e.target === overlay) closeCreateTaskDialog(); });
  _ctdEscHandler = (e: KeyboardEvent) => { if (e.key === "Escape") closeCreateTaskDialog(); };
  document.addEventListener("keydown", _ctdEscHandler);

  const dialog = document.createElement("div");
  dialog.className = "ctd-dialog";
  dialog.setAttribute("role", "dialog");
  dialog.setAttribute("aria-modal", "true");
  dialog.setAttribute("aria-label", "Create task");

  // Header
  const header = document.createElement("div");
  header.className = "ctd-header";
  const titleEl = document.createElement("span");
  titleEl.className = "ctd-title";
  titleEl.textContent = "Create task";
  const closeBtn = document.createElement("button");
  closeBtn.type = "button";
  closeBtn.className = "ctd-close";
  closeBtn.setAttribute("aria-label", "Close");
  closeBtn.innerHTML = "&times;";
  closeBtn.addEventListener("click", closeCreateTaskDialog);
  header.appendChild(titleEl);
  header.appendChild(closeBtn);
  dialog.appendChild(header);

  // Excerpt callout (only when triggered from text selection)
  const excerptEl = document.createElement("div");
  excerptEl.className = "ctd-excerpt";
  if (opts?.excerpt) {
    const bar = document.createElement("div");
    bar.className = "ctd-excerpt__bar";
    const txt = document.createElement("div");
    txt.className = "ctd-excerpt__text";
    txt.textContent = opts.excerpt;
    excerptEl.appendChild(bar);
    excerptEl.appendChild(txt);
  } else {
    excerptEl.hidden = true;
  }
  dialog.appendChild(excerptEl);

  // Body
  const body = document.createElement("div");
  body.className = "ctd-body";
  body.innerHTML = `
    <input type="text" class="ctd-input" data-f="title" placeholder="Task title" maxlength="160">
    <textarea class="ctd-input ctd-textarea" data-f="text" placeholder="What needs to be done?" rows="3"></textarea>
    <input type="text" class="ctd-input" data-f="org" placeholder="Organization (required)">
    <details class="ctd-advanced">
      <summary class="ctd-advanced__trigger">Advanced</summary>
      <div class="ctd-advanced__body">
        <div class="ctd-row">
          <select class="ctd-input" data-f="severity">
            ${_TASK_SEVERITIES.map((s) => `<option value="${s}" ${s === "low" ? "selected" : ""}>${s}</option>`).join("")}
          </select>
          <input type="text" class="ctd-input" data-f="assignee" placeholder="Assignee (optional)">
        </div>
        <div class="ctd-row">
          <select class="ctd-input" data-f="kind">
            <option value="work_item" selected>Task</option>
            <option value="reminder">Reminder</option>
          </select>
          <input type="date" class="ctd-input" data-f="deadline">
        </div>
      </div>
    </details>`;
  const cf = (k: string) => body.querySelector(`[data-f="${k}"]`) as HTMLInputElement;
  cf("title").value = opts?.title || (opts?.excerpt || "").slice(0, 60);
  (cf("text") as unknown as HTMLTextAreaElement).value = opts?.excerpt || "";
  cf("org").value = localStorage.getItem("lastOrg") || "";
  dialog.appendChild(body);

  // Footer
  const footer = document.createElement("div");
  footer.className = "ctd-footer";
  const errEl = document.createElement("span");
  errEl.className = "ctd-err";
  const cancelBtn = document.createElement("button");
  cancelBtn.type = "button";
  cancelBtn.className = "ctd-btn ctd-btn--cancel";
  cancelBtn.textContent = "Cancel";
  cancelBtn.addEventListener("click", closeCreateTaskDialog);
  const submitBtn = document.createElement("button");
  submitBtn.type = "button";
  submitBtn.className = "ctd-btn ctd-btn--create";
  submitBtn.textContent = "Create task";
  footer.appendChild(errEl);
  footer.appendChild(cancelBtn);
  footer.appendChild(submitBtn);
  dialog.appendChild(footer);

  submitBtn.addEventListener("click", async () => {
    const text = (cf("text") as unknown as HTMLTextAreaElement).value.trim();
    const org = cf("org").value.trim();
    if (!text || !org) { errEl.textContent = "Organization and description are required."; return; }
    errEl.textContent = "";
    submitBtn.disabled = true;
    const body2: Record<string, unknown> = {
      org_name: org,
      text,
      title: cf("title").value.trim() || text.slice(0, 60),
      severity: (cf("severity") as unknown as HTMLSelectElement).value,
      source_module: opts?.sourceModule || "manual",
      kind: (cf("kind") as unknown as HTMLSelectElement).value || "work_item",
      audience: "user",
    };
    const deadline = cf("deadline").value;
    if (deadline) body2.deadline = deadline;
    const assignee = cf("assignee").value.trim();
    if (assignee) body2.assignee = assignee;
    if (opts?.sourceRef) body2.source_ref = opts.sourceRef;
    const tid = (window as any).__mobiusChatThreadId;
    if (tid) body2.extra = { origin: { thread_id: tid } };
    try {
      const r = await apiFetch(`${API_BASE}/chat/tasks`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body2),
      });
      if (!r.ok) { errEl.textContent = `Create failed (${r.status}).`; submitBtn.disabled = false; return; }
      localStorage.setItem("lastOrg", org);
      submitBtn.classList.add("ctd-btn--success");
      submitBtn.textContent = "Created ✓";
      try { opts?.onCreated?.(); } catch { /* refresh is best-effort */ }
      setTimeout(closeCreateTaskDialog, 900);
    } catch { errEl.textContent = "Create failed — network error."; submitBtn.disabled = false; }
  });

  overlay.appendChild(dialog);
  document.body.appendChild(overlay);
  _ctdOverlayEl = overlay;
  setTimeout(() => cf("title").focus(), 50);
}

function closeTasksModal(): void {
  if (_tasksModalEl) { _tasksModalEl.remove(); _tasksModalEl = null; }
  if (_tasksEscHandler) { document.removeEventListener("keydown", _tasksEscHandler); _tasksEscHandler = null; }
}

function openTasksModal(prefill?: TasksModalPrefill): void {
  closeTasksModal();

  const overlay = document.createElement("div");
  overlay.className = "tasks-modal-overlay";
  overlay.addEventListener("mousedown", (e) => { if (e.target === overlay) closeTasksModal(); });
  _tasksEscHandler = (e: KeyboardEvent) => { if (e.key === "Escape") closeTasksModal(); };
  document.addEventListener("keydown", _tasksEscHandler);

  const panel = document.createElement("div");
  panel.className = "tasks-modal";

  // ── Header ──
  const header = document.createElement("div");
  header.className = "tasks-modal-header";
  header.innerHTML = `<span class="tasks-modal-title">${_svgIcon("task")} Tasks</span>`;
  const headerBtns = document.createElement("div");
  headerBtns.className = "tasks-modal-header-btns";
  const newBtn = document.createElement("button");
  newBtn.type = "button";
  newBtn.className = "tm-env-btn tm-env-btn--create-action";
  newBtn.textContent = "+ New task";
  const closeBtn = document.createElement("button");
  closeBtn.type = "button";
  closeBtn.className = "tasks-modal-close";
  closeBtn.innerHTML = "&times;";
  closeBtn.addEventListener("click", closeTasksModal);
  headerBtns.appendChild(newBtn);
  headerBtns.appendChild(closeBtn);
  header.appendChild(headerBtns);
  panel.appendChild(header);

  // ── Create → focused dialog (openCreateTaskDialog), not an inline form.
  // The CTD is the single create surface (selection toolbar + message row
  // already use it); the modal just opens it and refreshes on success —
  // no layout shift, no duplicated form code (UX review P1.4).
  const openCreate = () => openCreateTaskDialog({
    title: prefill?.title,
    excerpt: prefill?.text,
    sourceModule: prefill?.sourceModule,
    sourceRef: prefill?.sourceRef,
    onCreated: () => void loadList(),
  });
  newBtn.addEventListener("click", openCreate);

  // ── Preset tabs (UX review P1.1): users think "what's mine / what's
  // due", not in database dimensions. The raw selects live on under a
  // collapsed "More filters" disclosure for power users.
  const presets = document.createElement("div");
  presets.className = "tasks-modal-presets";
  const PRESET_DEFS: Array<{ key: string; label: string }> = [
    { key: "mine", label: "My open tasks" },
    { key: "due", label: "Due soon" },
    { key: "all", label: "All" },
  ];
  let activePreset = "mine";
  const presetBtns: Record<string, HTMLButtonElement> = {};
  for (const p of PRESET_DEFS) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "tasks-modal-preset";
    b.textContent = p.label;
    b.addEventListener("click", () => { void applyPreset(p.key); });
    presets.appendChild(b);
    presetBtns[p.key] = b;
  }
  panel.appendChild(presets);

  const moreFilters = document.createElement("details");
  moreFilters.className = "tasks-modal-more-filters";
  moreFilters.innerHTML = `<summary>More filters</summary>`;
  const filters = document.createElement("div");
  filters.className = "tasks-modal-filters";
  filters.innerHTML = `
    <select class="tasks-modal-input" data-f="status">
      <option value="open" selected>Open</option>
      <option value="in_progress">In progress</option>
      <option value="resolved">Resolved</option>
      <option value="dismissed">Dismissed</option>
      <option value="">All</option>
    </select>
    <select class="tasks-modal-input" data-f="audience" title="System tasks (telemetry, pipeline signals) are hidden by default">
      <option value="user" selected>My tasks</option>
      <option value="developer">System (dev)</option>
      <option value="all">All audiences</option>
    </select>
    <select class="tasks-modal-input" data-f="kind">
      <option value="" selected>Any kind</option>
      <option value="work_item">Work items</option>
      <option value="reminder">Reminders</option>
      <option value="signal">Signals</option>
    </select>
    <input type="text" class="tasks-modal-input" data-f="org" placeholder="Org filter">
    <input type="text" class="tasks-modal-input" data-f="assignee" placeholder="Assignee filter">
    <button type="button" class="tm-env-btn" data-f="apply">Apply</button>`;
  moreFilters.appendChild(filters);
  panel.appendChild(moreFilters);

  const fEl = (k: string) => filters.querySelector(`[data-f="${k}"]`) as HTMLInputElement;
  const setSelect = (k: string, v: string) => { (fEl(k) as unknown as HTMLSelectElement).value = v; };

  function markPreset(key: string | null): void {
    activePreset = key || "";
    for (const [k, b] of Object.entries(presetBtns)) {
      b.classList.toggle("tasks-modal-preset--active", k === key);
    }
  }

  async function applyPreset(key: string): Promise<void> {
    markPreset(key);
    if (key === "mine") {
      setSelect("status", "open"); setSelect("audience", "user"); setSelect("kind", "");
      fEl("org").value = "";
      const me = await _getWhoami();
      fEl("assignee").value = me ? me.assignee_ref : "";
    } else if (key === "due") {
      setSelect("status", "open"); setSelect("audience", "all"); setSelect("kind", "reminder");
      fEl("org").value = ""; fEl("assignee").value = "";
    } else {
      setSelect("status", ""); setSelect("audience", "all"); setSelect("kind", "");
      fEl("org").value = ""; fEl("assignee").value = "";
    }
    void loadList();
  }

  // ── List ──
  const listWrap = document.createElement("div");
  listWrap.className = "tasks-modal-list";
  panel.appendChild(listWrap);

  const SEV_BUCKETS: Array<{ label: string; sevs: string[] }> = [
    { label: "critical", sevs: ["critical"] },
    { label: "warning", sevs: ["warning"] },
    { label: "info", sevs: ["info", "low", "none"] },
  ];

  async function loadList(): Promise<void> {
    // Loading skeleton (UX review P3.8)
    listWrap.innerHTML =
      `<div class="tasks-modal-skeleton-row"></div>` +
      `<div class="tasks-modal-skeleton-row"></div>` +
      `<div class="tasks-modal-skeleton-row"></div>`;
    const ff = (k: string) => fEl(k).value.trim();
    const params = new URLSearchParams({ limit: "100" });
    if (ff("status")) params.set("status", ff("status"));
    params.set("audience", ff("audience") || "user"); // proxy treats "all" as no filter
    if (ff("kind")) params.set("kind", ff("kind"));
    if (ff("org")) params.set("org_name", ff("org"));
    if (ff("assignee")) params.set("assignee", ff("assignee"));
    try {
      const r = await apiFetch(`${API_BASE}/chat/tasks?${params.toString()}`);
      const data = await r.json();
      let tasks: any[] = data.tasks || [];
      // "Due soon" = reminders due within 7 days (server has no date
      // filter; the reminder set is small so client-side is fine).
      if (activePreset === "due") {
        const horizon = new Date(Date.now() + 7 * 24 * 60 * 60 * 1000).toISOString().slice(0, 10);
        tasks = tasks.filter((t) => {
          const d = String(t.deadline || t.due_at || "").slice(0, 10);
          return d && d <= horizon;
        });
      }
      listWrap.innerHTML = "";
      if (!tasks.length) {
        listWrap.innerHTML = `
          <div class="tasks-modal-empty">
            <svg class="tasks-modal-empty-icon" viewBox="0 0 16 16" fill="currentColor"><path d="M13.78 4.22a.75.75 0 010 1.06l-7.25 7.25a.75.75 0 01-1.06 0L2.22 9.28a.75.75 0 011.06-1.06L6 10.94l6.72-6.72a.75.75 0 011.06 0z"/></svg>
            <p class="tasks-modal-empty-headline">All clear</p>
            <p class="tasks-modal-empty-sub">No tasks match · try another view or create one</p>
          </div>`;
        return;
      }
      // Severity grouping (UX review P1.2): open work bucketed by
      // severity with sticky headers; closed items collapsed at the end.
      const open = tasks.filter((t) => t.status === "open" || t.status === "in_progress" || t.status === "running");
      const closed = tasks.filter((t) => !open.includes(t));
      for (const bucket of SEV_BUCKETS) {
        const rows = open.filter((t) => bucket.sevs.includes((t.severity || "low").toLowerCase()));
        if (!rows.length) continue;
        const gh = document.createElement("div");
        gh.className = "tasks-modal-group-header";
        gh.innerHTML = `<span class="tm-env-badge tm-env-badge--${bucket.label}">${bucket.label}</span>` +
          `<span class="tasks-modal-group-count">${rows.length}</span>`;
        listWrap.appendChild(gh);
        for (const t of rows) listWrap.appendChild(_taskModalRow(t, loadList));
      }
      if (closed.length) {
        const det = document.createElement("details");
        det.className = "tasks-modal-closed";
        det.innerHTML = `<summary>Closed — ${closed.length} item${closed.length > 1 ? "s" : ""}</summary>`;
        for (const t of closed) det.appendChild(_taskModalRow(t, loadList));
        listWrap.appendChild(det);
      }
    } catch {
      listWrap.innerHTML = `<div class="tasks-modal-loading">Failed to load tasks.</div>`;
    }
  }
  (filters.querySelector('[data-f="apply"]') as HTMLButtonElement).addEventListener("click", () => {
    markPreset(null); // custom filters → no preset highlighted
    void loadList();
  });

  overlay.appendChild(panel);
  document.body.appendChild(overlay);
  _tasksModalEl = overlay;

  // Prefill routing: map nudge/banner entries onto presets; unmatched
  // combinations fall through to custom filters with the disclosure open.
  if (prefill?.createOpen) {
    openCreate();
  }
  if (prefill?.filterKind === "reminder" && !prefill?.filterAssignee) {
    void applyPreset("due");
  } else if (prefill?.filterAssignee && !prefill?.filterKind) {
    markPreset("mine");
    setSelect("status", "open"); setSelect("audience", "user"); setSelect("kind", "");
    fEl("assignee").value = prefill.filterAssignee;
    void loadList();
  } else if (prefill?.filterKind || prefill?.filterAssignee) {
    markPreset(null);
    moreFilters.open = true;
    if (prefill.filterKind) setSelect("kind", prefill.filterKind);
    if (prefill.filterAssignee) fEl("assignee").value = prefill.filterAssignee;
    void loadList();
  } else {
    void applyPreset("mine");
  }
}

/* ── Reminder nudge ────────────────────────────────────────────────────
   Non-intrusive chip above the composer: "⏰ N reminder(s) due" with
   View / dismiss. Shown when the user starts a query (and once on
   load), throttled so it appears at most once per 4h; dismissing
   snoozes it for 24h. Pure frontend — no pipeline latency. */

const _NUDGE_LAST_KEY = "mobius_reminder_nudge_last";
const _NUDGE_SNOOZE_KEY = "mobius_reminder_nudge_snooze";
const _NUDGE_MIN_GAP_MS = 4 * 60 * 60 * 1000;   // 4h between nudges
const _NUDGE_SNOOZE_MS = 24 * 60 * 60 * 1000;   // 24h after dismiss
let _nudgeInFlight = false;

// Thin reference to the auth service set once initApp creates it.
// Allows _getWhoami / apiFetch (defined before auth is created) to
// attach Bearer tokens without closing over the initApp scope.
let _authRef: { getAuthHeader?: () => Promise<Record<string, string> | null> | Record<string, string> | null } | null = null;

/** fetch() with the platform Bearer token merged into headers. */
async function apiFetch(url: string, init: RequestInit = {}): Promise<Response> {
  const authHdrs = _authRef?.getAuthHeader ? await _authRef.getAuthHeader() : null;
  const merged: RequestInit = {
    ...init,
    headers: { ...(authHdrs ?? {}), ...(init.headers as Record<string, string> | undefined ?? {}) },
  };
  return fetch(url, merged);
}

// Who am I, as the task system sees me. Resolved once per page load via
// /chat/whoami (server-side mobius-user lookup); null = unknown identity
// → all per-user surfacing falls back to unscoped.
let _whoami: {
  user_id: string;
  display_name: string;
  assignee_ref: string;
  greeting?: { name: string; enabled: boolean };
} | null = null;
// false = never fetched; true = fetch in-flight or succeeded; "miss" = got
// a clean {ok:false} (no identity) — retried once on next call in case the
// token loaded late.
let _whoamiFetched: boolean | "miss" = false;

async function _getWhoami(): Promise<typeof _whoami> {
  if (_whoamiFetched === true) return _whoami;
  _whoamiFetched = true;
  try {
    const r = await apiFetch(`${API_BASE}/chat/whoami`);
    if (r.ok) {
      const d = await r.json();
      if (d.ok && d.user?.assignee_ref) { _whoami = d.user; return _whoami; }
    }
  } catch { /* unknown identity — unscoped fallback */ }
  // Got a clean miss — allow one retry in case token loads late
  _whoamiFetched = "miss";
  return _whoami;
}

async function _maybeShowReminderNudge(): Promise<void> {
  if (_nudgeInFlight || document.querySelector(".reminder-nudge")) return;
  const now = Date.now();
  const last = Number(localStorage.getItem(_NUDGE_LAST_KEY) || 0);
  const snooze = Number(localStorage.getItem(_NUDGE_SNOOZE_KEY) || 0);
  if (now - last < _NUDGE_MIN_GAP_MS || now < snooze) return;

  _nudgeInFlight = true;
  try {
    // Scope to MY reminders when identity resolves; unscoped otherwise.
    const me = await _getWhoami();
    const scope = me ? `&assignee=${encodeURIComponent(me.assignee_ref)}` : "";
    const r = await apiFetch(`${API_BASE}/chat/tasks?kind=reminder&status=open&limit=20${scope}`);
    if (!r.ok) return;
    const tasks: any[] = (await r.json()).tasks || [];
    const today = new Date().toISOString().slice(0, 10);
    const due = tasks.filter((t) => {
      const d = String(t.deadline || t.due_at || "").slice(0, 10);
      return d && d <= today;
    });
    if (!due.length) return;

    const anchor = document.querySelector(".composer-wrap");
    if (!anchor || !anchor.parentElement) return;

    localStorage.setItem(_NUDGE_LAST_KEY, String(now));

    const chip = document.createElement("div");
    chip.className = "reminder-nudge";
    const label = document.createElement("span");
    label.className = "reminder-nudge-label";
    label.innerHTML = `${_svgIcon("task")} <strong>${due.length}</strong> reminder${due.length > 1 ? "s" : ""} due — ${
      (due[0].title || due[0].text || "").slice(0, 60)}${due.length > 1 ? ", …" : ""}`;
    const viewBtn = document.createElement("button");
    viewBtn.type = "button";
    viewBtn.className = "reminder-nudge-view";
    viewBtn.textContent = "View";
    viewBtn.addEventListener("click", () => {
      chip.remove();
      openTasksModal({ filterKind: "reminder" });
    });
    const dismissBtn = document.createElement("button");
    dismissBtn.type = "button";
    dismissBtn.className = "reminder-nudge-dismiss";
    dismissBtn.setAttribute("aria-label", "Dismiss for a day");
    dismissBtn.innerHTML = "&times;";
    dismissBtn.addEventListener("click", () => {
      localStorage.setItem(_NUDGE_SNOOZE_KEY, String(Date.now() + _NUDGE_SNOOZE_MS));
      chip.remove();
    });
    chip.appendChild(label);
    chip.appendChild(viewBtn);
    chip.appendChild(dismissBtn);
    anchor.parentElement.insertBefore(chip, anchor);
    // Auto-fade after 30s if untouched — it's a nudge, not a blocker.
    setTimeout(() => chip.remove(), 30000);
  } catch {
    /* nudge is best-effort — never surface errors */
  } finally {
    _nudgeInFlight = false;
  }
}

/* ── Assignment banner ─────────────────────────────────────────────────
   "N open tasks assigned to you" on load — disjoint from the reminder
   nudge (work items only; reminders have their own chip). Same
   non-intrusive contract: throttled, dismissible, auto-fades. */

const _BANNER_LAST_KEY = "mobius_assigned_banner_last";
const _BANNER_SNOOZE_KEY = "mobius_assigned_banner_snooze";

async function _maybeShowAssignedBanner(): Promise<void> {
  if (document.querySelector(".reminder-nudge--assigned")) return;
  const now = Date.now();
  if (now - Number(localStorage.getItem(_BANNER_LAST_KEY) || 0) < _NUDGE_MIN_GAP_MS) return;
  if (now < Number(localStorage.getItem(_BANNER_SNOOZE_KEY) || 0)) return;

  const me = await _getWhoami();
  if (!me) return; // banner is per-user by definition — no identity, no banner
  try {
    const r = await apiFetch(`${API_BASE}/chat/tasks?status=open&kind=work_item&assignee=${encodeURIComponent(me.assignee_ref)}&limit=50`);
    if (!r.ok) return;
    const tasks: any[] = (await r.json()).tasks || [];
    if (!tasks.length) return;
    const anchor = document.querySelector(".composer-wrap");
    if (!anchor || !anchor.parentElement) return;

    localStorage.setItem(_BANNER_LAST_KEY, String(now));

    const chip = document.createElement("div");
    chip.className = "reminder-nudge reminder-nudge--assigned";
    const label = document.createElement("span");
    label.className = "reminder-nudge-label";
    label.innerHTML = `${_svgIcon("task")} <strong>${tasks.length}</strong> open task${tasks.length > 1 ? "s" : ""} assigned to you`;
    const viewBtn = document.createElement("button");
    viewBtn.type = "button";
    viewBtn.className = "reminder-nudge-view";
    viewBtn.textContent = "View";
    viewBtn.addEventListener("click", () => {
      chip.remove();
      openTasksModal({ filterAssignee: me.assignee_ref });
    });
    const dismissBtn = document.createElement("button");
    dismissBtn.type = "button";
    dismissBtn.className = "reminder-nudge-dismiss";
    dismissBtn.setAttribute("aria-label", "Dismiss for a day");
    dismissBtn.innerHTML = "&times;";
    dismissBtn.addEventListener("click", () => {
      localStorage.setItem(_BANNER_SNOOZE_KEY, String(Date.now() + _NUDGE_SNOOZE_MS));
      chip.remove();
    });
    chip.appendChild(label);
    chip.appendChild(viewBtn);
    chip.appendChild(dismissBtn);
    anchor.parentElement.insertBefore(chip, anchor);
    setTimeout(() => chip.remove(), 30000);
  } catch { /* best-effort */ }
}

async function _maybeShowDocReadyNudge(): Promise<void> {
  // §3.2 read side: poll for open doc_ready notification tasks and show
  // a nudge chip per unread task. Each chip dismisses via POST /chat/tasks/{id}/dismiss.
  if (document.querySelector(".rag-doc-ready-nudge")) return; // one at a time
  const me = await _getWhoami();
  if (!me) return;
  try {
    const assignee = encodeURIComponent(me.assignee_ref || "");
    const r = await apiFetch(`${API_BASE}/chat/tasks?kind=notification&status=open&limit=10${assignee ? `&assigned_to=${assignee}` : ""}`);
    if (!r.ok) return;
    const tasks: any[] = (await r.json()).tasks || [];
    const docReadyTasks = tasks.filter((t: any) => t.type === "doc_ready");
    if (!docReadyTasks.length) return;
    const anchor = document.querySelector(".composer-wrap");
    if (!anchor || !anchor.parentElement) return;

    for (const task of docReadyTasks.slice(0, 3)) {
      const detail = task.detail_payload || {};
      const fname = detail.filename || task.title || "Document";
      const docId  = detail.document_id || "";
      const tid    = detail.thread_id || "";

      const chip = document.createElement("div");
      chip.className = "reminder-nudge rag-doc-ready-nudge";
      const label = document.createElement("span");
      label.className = "reminder-nudge-label";
      label.textContent = `📄 "${fname}" is ready`;
      const askBtn = document.createElement("button");
      askBtn.type = "button";
      askBtn.className = "reminder-nudge-view";
      askBtn.textContent = "Ask now";
      askBtn.addEventListener("click", () => {
        chip.remove();
        apiFetch(`${API_BASE}/chat/tasks/${task.id}/dismiss`, { method: "POST" }).catch(() => {});
        const inputEl = document.getElementById("input") as HTMLInputElement | null;
        if (inputEl && !inputEl.value.trim()) {
          inputEl.value = `Tell me about "${fname}"`;
          inputEl.dispatchEvent(new Event("input"));
          inputEl.focus();
        }
      });
      const dismissBtn = document.createElement("button");
      dismissBtn.type = "button";
      dismissBtn.className = "reminder-nudge-dismiss";
      dismissBtn.setAttribute("aria-label", "Dismiss");
      dismissBtn.innerHTML = "&times;";
      dismissBtn.addEventListener("click", () => {
        chip.remove();
        apiFetch(`${API_BASE}/chat/tasks/${task.id}/dismiss`, { method: "POST" }).catch(() => {});
      });
      chip.appendChild(label);
      chip.appendChild(askBtn);
      chip.appendChild(dismissBtn);
      anchor.parentElement.insertBefore(chip, anchor);
      setTimeout(() => chip.remove(), 30000);
    }
  } catch { /* best-effort */ }
}

function _initReminderNudge(): void {
  // Once shortly after load… (banner slightly later so the two chips
  // don't stack in the same instant; nudge wins the tie)
  setTimeout(() => void _maybeShowReminderNudge(), 2500);
  setTimeout(() => void _maybeShowAssignedBanner(), 4000);
  setTimeout(() => void _maybeShowDocReadyNudge(), 5000); // §3.2 doc-ready notifications
  // …and when the user starts a query (throttle makes repeats free).
  document.getElementById("send")?.addEventListener("click", () => void _maybeShowReminderNudge());
  document.getElementById("input")?.addEventListener("keydown", (e) => {
    if ((e as KeyboardEvent).key === "Enter") void _maybeShowReminderNudge();
  });
}

if (typeof document !== "undefined") {
  if (document.readyState === "loading") { document.addEventListener("DOMContentLoaded", _initReminderNudge); }
  else { _initReminderNudge(); }
}

async function _maybeShowGreeting(): Promise<void> {
  const el = document.getElementById("mainHeaderTitle");
  if (!el) return;
  const me = await _getWhoami();
  // Hard requirements: no greeting for unknown identity or disabled preference.
  if (!me || !me.greeting?.enabled || !me.greeting?.name) return;
  const h = new Date().getHours();
  const salutation = h < 12 ? "Good morning" : h < 17 ? "Good afternoon" : "Good evening";
  el.textContent = `${salutation}, ${me.greeting.name}.`;
  el.classList.add("chat-greeting");
}

// _maybeShowGreeting() is called from initApp() after _authRef is set so
// the whoami fetch carries the Bearer token. Module-scope early-fire was
// removed because auth is not ready at DOMContentLoaded.

function _taskModalRow(t: any, reload: () => Promise<void>): HTMLElement {
  const row = document.createElement("div");
  row.className = "tasks-modal-row";
  const sev = (t.severity || "low").toLowerCase();
  const status = (t.status || "open").toLowerCase();
  const title = t.title || t.text || "(no title)";
  const head = document.createElement("div");
  head.className = "tasks-modal-row-head";
  const due = t.kind === "reminder" && (t.deadline || t.due_at)
    ? ` ⏰ ${String(t.deadline || t.due_at).slice(0, 10)}` : "";
  head.innerHTML = `
    <span class="tm-env-badge tm-env-badge--${sev}">${sev}</span>
    <span class="tasks-modal-row-title"></span>
    <span class="tm-env-mod-tag">${(t.source_module || "").replace(/_/g, " ")}</span>
    <span class="tasks-modal-row-status">${status}${t.assignee ? " → " + t.assignee : ""}${due}</span>`;
  (head.querySelector(".tasks-modal-row-title") as HTMLElement).textContent = title;
  row.appendChild(head);

  const actions = document.createElement("div");
  actions.className = "tasks-modal-row-actions";
  row.appendChild(actions);

  const mkBtn = (label: string, cls: string, fn: () => void) => {
    const b = document.createElement("button");
    b.type = "button";
    b.className = `tm-env-btn ${cls}`;
    b.textContent = label;
    b.addEventListener("click", fn);
    actions.appendChild(b);
    return b;
  };

  const isOpen = status === "open" || status === "in_progress";
  if (isOpen) {
    const resolveBtn = mkBtn("Resolve", "tm-env-btn--resolve", async () => {
      await apiFetch(`${API_BASE}/chat/tasks/${t.task_id}/resolve`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ resolved_by: "chat" }),
      }).catch(() => null);
      void reload();
    });
    resolveBtn.dataset.tourId = "task-resolve";
    mkBtn("Dismiss", "tm-env-btn--dismiss", async () => {
      await apiFetch(`${API_BASE}/chat/tasks/${t.task_id}/dismiss`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ dismissed_by: "chat" }),
      }).catch(() => null);
      void reload();
    });
    // "Assign to…" — ellipsis signals an input will appear; ✓ button
    // submits alongside Enter (UX review P2.7).
    mkBtn("Assign to…", "tm-env-btn--assign", () => {
      if (actions.querySelector(".tasks-modal-assign-input")) return;
      const inp = document.createElement("input");
      inp.type = "text";
      inp.className = "tasks-modal-input tasks-modal-assign-input";
      inp.placeholder = "Type a name…";
      inp.value = t.assignee || "";
      let _assignRef: string | null = null;
      let _assignDd: HTMLElement | null = null;
      const _closeAssignDd = () => { _assignDd?.remove(); _assignDd = null; };
      inp.addEventListener("input", () => {
        _assignRef = null;
        const q = inp.value.trim();
        if (!q) { _closeAssignDd(); return; }
        void apiFetch(`${API_BASE}/chat/coworkers?q=${encodeURIComponent(q)}&limit=6`).then(async (r) => {
          if (!r.ok) return;
          const d = await r.json();
          const list: Array<{display_name: string; assignee_ref: string}> = d.coworkers || [];
          _closeAssignDd();
          if (!list.length) return;
          const dd = document.createElement("div");
          dd.className = "at-mention-dropdown";
          const rect = inp.getBoundingClientRect();
          dd.style.cssText = `position:fixed;top:${rect.bottom + 2}px;left:${rect.left}px;min-width:${rect.width}px;z-index:9999;`;
          list.forEach((c) => {
            const item = document.createElement("button");
            item.type = "button";
            item.className = "at-mention-item";
            item.textContent = c.display_name;
            item.addEventListener("mousedown", (e) => {
              e.preventDefault();
              inp.value = c.display_name;
              _assignRef = c.assignee_ref;
              _closeAssignDd();
            });
            dd.appendChild(item);
          });
          document.body.appendChild(dd);
          _assignDd = dd;
        });
      });
      inp.addEventListener("blur", () => setTimeout(_closeAssignDd, 150));
      const save = async () => {
        const who = inp.value.trim();
        if (!who) return;
        _closeAssignDd();
        await apiFetch(`${API_BASE}/chat/tasks/${t.task_id}`, {
          method: "PATCH", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ assigned_to: _assignRef || who, assignee: who }),
        }).catch(() => null);
        void reload();
      };
      inp.addEventListener("keydown", (e) => {
        if (e.key === "Escape") { _closeAssignDd(); inp.remove(); okBtn.remove(); return; }
        if (e.key === "Enter") void save();
      });
      const okBtn = document.createElement("button");
      okBtn.type = "button";
      okBtn.className = "tm-env-btn";
      okBtn.title = "Save assignment";
      okBtn.textContent = "✓";
      okBtn.addEventListener("click", () => void save());
      actions.appendChild(inp);
      actions.appendChild(okBtn);
      inp.focus();
    });
    // Edit: full-row edit state — the editor REPLACES the row head +
    // actions (CSS .tasks-modal-row--editing) instead of nesting a
    // cramped mini-form below them (UX review P2.6).
    mkBtn("Edit", "", () => {
      if (row.querySelector(".tasks-modal-editor")) return;
      const ed = document.createElement("div");
      ed.className = "tasks-modal-editor";
      ed.innerHTML = `
        <div class="tasks-modal-editor-fields">
          <input type="text" class="tasks-modal-input" data-e="title" placeholder="Title">
          <div class="tasks-modal-create-row">
            <select class="tasks-modal-input" data-e="severity">
              ${_TASK_SEVERITIES.map((s) => `<option value="${s}" ${s === sev ? "selected" : ""}>${s}</option>`).join("")}
            </select>
            <input type="date" class="tasks-modal-input" data-e="deadline">
            <input type="text" class="tasks-modal-input" data-e="note" placeholder="Add note (optional)">
          </div>
        </div>
        <div class="tasks-modal-editor-actions">
          <button type="button" class="tm-env-btn tm-env-btn--create-action" data-e="save">Save</button>
          <button type="button" class="tm-env-btn" data-e="cancel">Cancel</button>
        </div>`;
      (ed.querySelector('[data-e="title"]') as HTMLInputElement).value = title;
      const closeEditor = () => { ed.remove(); row.classList.remove("tasks-modal-row--editing"); };
      (ed.querySelector('[data-e="cancel"]') as HTMLButtonElement).addEventListener("click", closeEditor);
      (ed.querySelector('[data-e="save"]') as HTMLButtonElement).addEventListener("click", async () => {
        const val = (k: string) => (ed.querySelector(`[data-e="${k}"]`) as HTMLInputElement).value.trim();
        const body: Record<string, unknown> = {};
        if (val("title") && val("title") !== title) { body.title = val("title"); body.text = val("title"); }
        if (val("severity") !== sev) body.severity = val("severity");
        if (val("deadline")) body.deadline = val("deadline");
        if (val("note")) body.note = val("note");
        if (Object.keys(body).length) {
          await apiFetch(`${API_BASE}/chat/tasks/${t.task_id}`, {
            method: "PATCH", headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body),
          }).catch(() => null);
        }
        closeEditor();
        void reload();
      });
      row.appendChild(ed);
      row.classList.add("tasks-modal-row--editing");
      (ed.querySelector('[data-e="title"]') as HTMLInputElement).focus();
    });
  }
  return row;
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

// TODO(hardening): once User Manager exposes roles[] on the profile, replace the
// getShowLlmPerformance fallback below with a real role check and remove the fallback.
// Roles that grant promote rights: "corpus_curator" | "rag_admin"
const PROMOTE_ROLES = ["corpus_curator", "rag_admin"] as const;

function canPromoteToPublic(profile: MobiusChatUserProfile | null): boolean {
  const roles = profile?.roles ?? [];
  if (roles.some((r) => (PROMOTE_ROLES as readonly string[]).includes(r))) return true;
  // Fallback: until roles field is populated, mirror the diagnostics-tab visibility gate.
  return getShowLlmPerformance(profile);
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

/** Fact-store leaf card rendered when routing.method === "fact_store". */
function _renderFactStoreLeaf(data: any, routing: any): HTMLElement {
  const predicate = String(routing.fact_predicate ?? "certified fact");
  const factScore = typeof routing.fact_score === "number" ? routing.fact_score : 1.0;
  // fact_cert_grades is a dict {retrieval, synthesis}, not an array.
  const certGrades = (routing.fact_cert_grades ?? {}) as any;
  const retrievalGrade =
    typeof certGrades.retrieval === "number" ? certGrades.retrieval.toFixed(2) : String(certGrades.retrieval ?? "—");
  const grader = "fact_check_v1";
  // All provenance lives under routing.fact_provenance (no served.* block in live payload).
  const prov = (routing.fact_provenance ?? {}) as any;
  const freshness = (prov.freshness ?? {}) as any;
  const sourceRefObj = (prov.source_ref ?? {}) as any;
  const sourceRef = String(sourceRefObj.registry_notes ?? sourceRefObj.url ?? "");
  const lastVerified = String(freshness.last_verified_at ?? "");
  const validUntil = String(freshness.valid_until ?? "");
  const certStatus = String(prov.cert_status ?? "");
  const stale = Boolean(freshness.stale);

  const fmtDate = (iso: string) => {
    if (!iso) return "—";
    try { return new Date(iso).toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" }); }
    catch { return iso; }
  };

  const wrap = document.createElement("div");
  wrap.className = "llm-performance retrieval-trace collapsed" + (stale ? " rt-fs-stale" : "");

  // ── Preview row ──────────────────────────────────────────────────
  const preview = document.createElement("div");
  preview.className = "llm-performance-preview";
  preview.setAttribute("role", "button");
  preview.setAttribute("tabindex", "0");
  preview.setAttribute("aria-expanded", "false");
  const titleEl = document.createElement("span");
  titleEl.className = "llm-performance-title";
  titleEl.textContent = "Retrieval";
  const oneline = document.createElement("span");
  oneline.className = "llm-performance-oneline";
  const fsBadge = document.createElement("span");
  fsBadge.className = "rt-fs-badge" + (stale ? " rt-fs-badge--stale" : "");
  fsBadge.textContent = "⚡ s · fact_store";
  const fsSummary = document.createElement("span");
  fsSummary.className = "rt-fs-summary";
  fsSummary.textContent = ` · ${predicate} · score ${factScore.toFixed(2)} · n_chunks=0 · direct serve`;
  oneline.appendChild(fsBadge);
  oneline.appendChild(fsSummary);
  const chev = document.createElement("span");
  chev.className = "llm-performance-chevron";
  chev.setAttribute("aria-hidden", "true");
  chev.textContent = "▼";
  preview.appendChild(titleEl);
  preview.appendChild(oneline);
  preview.appendChild(chev);

  // ── Expanded body — provenance card ─────────────────────────────
  const body = document.createElement("div");
  body.className = "llm-performance-body";
  const card = document.createElement("div");
  card.className = "rt-fs-card";

  // Header: predicate + cert pill
  const hdr = document.createElement("div");
  hdr.className = "rt-fs-header";
  const hdrLeft = document.createElement("div");
  hdrLeft.className = "rt-fs-predicate";
  hdrLeft.textContent = predicate;
  const hdrRight = document.createElement("div");
  const certPill = document.createElement("span");
  certPill.className = "rt-fs-cert-pill" + (stale ? " rt-fs-cert-pill--stale" : "");
  certPill.textContent = `${certStatus || "certified"} · score ${factScore.toFixed(2)}`;
  hdrRight.appendChild(certPill);
  hdr.appendChild(hdrLeft);
  hdr.appendChild(hdrRight);
  card.appendChild(hdr);

  // Derivation section
  const divider1 = document.createElement("div");
  divider1.className = "rt-fs-divider";
  const divLabel1 = document.createElement("span");
  divLabel1.className = "rt-fs-divider-label";
  divLabel1.textContent = "how this answer was derived";
  divider1.appendChild(divLabel1);
  card.appendChild(divider1);

  const derivGrid = document.createElement("div");
  derivGrid.className = "rt-fs-grid";
  ([
    ["extraction", "corpus-grounded", false],
    ["retrieval grade", retrievalGrade, false],
    ["synthesis grade", "n/a · direct read", true],
    ["grader", grader, false],
  ] as Array<[string, string, boolean]>).forEach(([k, v, muted]) => {
    const row = document.createElement("div");
    row.className = "rt-fs-kv";
    const kEl = document.createElement("span");
    kEl.className = "rt-fs-kv-k";
    kEl.textContent = k;
    const vEl = document.createElement("span");
    vEl.className = muted ? "rt-fs-kv-v rt-fs-muted" : "rt-fs-kv-v";
    vEl.textContent = v;
    row.appendChild(kEl);
    row.appendChild(vEl);
    derivGrid.appendChild(row);
  });
  card.appendChild(derivGrid);

  // Sources section
  const divider2 = document.createElement("div");
  divider2.className = "rt-fs-divider";
  const divLabel2 = document.createElement("span");
  divLabel2.className = "rt-fs-divider-label";
  divLabel2.textContent = "sources — watched for change";
  divider2.appendChild(divLabel2);
  card.appendChild(divider2);

  if (sourceRef) {
    const srcRow = document.createElement("div");
    srcRow.className = "rt-fs-source-row";
    const srcTitle = document.createElement("div");
    srcTitle.className = "rt-fs-source-title";
    srcTitle.textContent = "📄 " + sourceRef;
    const srcPill = document.createElement("span");
    srcPill.className = "rt-fs-pill rt-fs-pill--success";
    srcPill.textContent = "live · watched";
    srcRow.appendChild(srcTitle);
    srcRow.appendChild(srcPill);
    card.appendChild(srcRow);
  } else {
    const pendingRow = document.createElement("div");
    pendingRow.className = "rt-fs-source-row rt-fs-source-row--pending";
    const pendingTitle = document.createElement("div");
    pendingTitle.className = "rt-fs-source-title";
    pendingTitle.textContent = "📍 exact source pointer ";
    const pendingCode = document.createElement("code");
    pendingCode.className = "rt-fs-code";
    pendingCode.textContent = "doc_id · chunk_id · page";
    pendingTitle.appendChild(pendingCode);
    const pendingPill = document.createElement("span");
    pendingPill.className = "rt-fs-pill rt-fs-pill--warning";
    pendingPill.textContent = "proposed";
    pendingRow.appendChild(pendingTitle);
    pendingRow.appendChild(pendingPill);
    card.appendChild(pendingRow);
    const pendingNote = document.createElement("div");
    pendingNote.className = "rt-fs-pending-note";
    pendingNote.textContent =
      "Comparator sees the grounding chunk; persisting doc/chunk/page lets drift-watch pinpoint which source changed.";
    card.appendChild(pendingNote);
  }

  // Freshness tiles
  const divider3 = document.createElement("div");
  divider3.className = "rt-fs-divider";
  card.appendChild(divider3);

  const tiles = document.createElement("div");
  tiles.className = "rt-fs-tiles";
  const freshStatus = stale ? "stale" : (lastVerified ? "fresh" : "—");
  ([
    ["last verified", fmtDate(lastVerified)],
    ["valid until", fmtDate(validUntil)],
    ["status", freshStatus],
  ] as Array<[string, string]>).forEach(([label, value]) => {
    const tile = document.createElement("div");
    tile.className = "rt-fs-tile";
    const tLabel = document.createElement("div");
    tLabel.className = "rt-fs-tile-label";
    tLabel.textContent = label;
    const tValue = document.createElement("div");
    let valCls = "rt-fs-tile-value";
    if (label === "status") {
      valCls += stale ? " rt-fs-stale-text" : (lastVerified ? " rt-fs-fresh-text" : "");
    }
    tValue.className = valCls;
    tValue.textContent = value;
    tile.appendChild(tLabel);
    tile.appendChild(tValue);
    tiles.appendChild(tile);
  });
  card.appendChild(tiles);

  // Drift-watch section
  const divider4 = document.createElement("div");
  divider4.className = "rt-fs-divider";
  const divLabel4 = document.createElement("span");
  divLabel4.className = "rt-fs-divider-label";
  divLabel4.textContent = "drift watch — §8 re-verify loop";
  divider4.appendChild(divLabel4);
  card.appendChild(divider4);

  const driftDiv = document.createElement("div");
  driftDiv.className = "rt-fs-drift";
  const driftVerb = certStatus === "drift" ? "drift" : (certStatus ? certStatus : "confirm");
  const line1 = document.createElement("div");
  line1.className = "rt-fs-drift-line";
  line1.textContent = "last re-verify: ";
  const driftSpan = document.createElement("span");
  driftSpan.className = driftVerb === "drift" ? "rt-fs-warn-text" : "rt-fs-primary-text";
  driftSpan.textContent = driftVerb;
  line1.appendChild(driftSpan);
  if (driftVerb !== "drift") {
    const extra = document.createTextNode(` — re-derived from live sources, still grounds at ${factScore.toFixed(2)}`);
    line1.appendChild(extra);
  }
  const line2 = document.createElement("div");
  line2.className = "rt-fs-drift-line";
  line2.textContent = "next check: auto at 80% of TTL, or on demand";
  const line3 = document.createElement("div");
  line3.className = "rt-fs-drift-line";
  line3.textContent = "if a watched source changes so the fact no longer grounds → ";
  const driftWarnSpan = document.createElement("span");
  driftWarnSpan.className = "rt-fs-warn-text";
  driftWarnSpan.textContent = "drift";
  const driftThen = document.createTextNode(" → status ");
  const staleSpan = document.createElement("span");
  staleSpan.className = "rt-fs-primary-text";
  staleSpan.textContent = "stale";
  const driftEnd = document.createTextNode(" → re-cert queue");
  line3.appendChild(driftWarnSpan);
  line3.appendChild(driftThen);
  line3.appendChild(staleSpan);
  line3.appendChild(driftEnd);
  driftDiv.appendChild(line1);
  driftDiv.appendChild(line2);
  driftDiv.appendChild(line3);
  card.appendChild(driftDiv);

  body.appendChild(card);
  wrap.appendChild(preview);
  wrap.appendChild(body);

  const toggle = () => {
    const collapsed = wrap.classList.toggle("collapsed");
    preview.setAttribute("aria-expanded", collapsed ? "false" : "true");
    chev.textContent = collapsed ? "▼" : "▲";
  };
  preview.addEventListener("click", toggle);
  preview.addEventListener("keydown", (e: KeyboardEvent) => {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggle(); }
  });
  return wrap;
}

// ── DiagnosticsCard helpers ───────────────────────────────────────────────────

function _dcKV(container: HTMLElement, key: string, val: string): void {
  const row = document.createElement("div");
  row.className = "rt-kv";
  row.innerHTML = `<span class="rt-kv-k">${rtEscapeAttr(key)}</span><span class="rt-kv-v">${rtEscapeAttr(val)}</span>`;
  container.appendChild(row);
}

function _dcSection(
  title: string,
  status: "ok" | "warn" | "gray",
  summary: string,
  build: (body: HTMLElement) => void,
): HTMLElement {
  const sec = document.createElement("div");
  sec.className = `dc-section dc-section--${status}`;
  const hdr = document.createElement("div");
  hdr.className = "dc-section-hdr";
  hdr.setAttribute("role", "button");
  hdr.setAttribute("tabindex", "0");
  hdr.setAttribute("aria-expanded", "true");
  hdr.innerHTML =
    `<span class="dc-dot dc-dot--${status}"></span>` +
    `<span class="dc-section-title">${rtEscapeAttr(title)}</span>` +
    `<span class="dc-section-sum">${rtEscapeAttr(summary)}</span>` +
    `<span class="dc-chev" aria-hidden="true">▲</span>`;
  const bdy = document.createElement("div");
  bdy.className = "dc-section-body";
  build(bdy);
  const toggle = () => {
    const hidden = bdy.classList.toggle("dc-section-body--hidden");
    hdr.setAttribute("aria-expanded", hidden ? "false" : "true");
    hdr.querySelector<HTMLElement>(".dc-chev")!.textContent = hidden ? "▼" : "▲";
  };
  hdr.addEventListener("click", toggle);
  hdr.addEventListener("keydown", (e: KeyboardEvent) => {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggle(); }
  });
  sec.appendChild(hdr);
  sec.appendChild(bdy);
  return sec;
}

function _dcLeaf(
  title: string,
  status: "ok" | "warn" | "gray",
  summary: string,
  build?: (body: HTMLElement) => void,
): HTMLElement {
  const leaf = document.createElement("div");
  leaf.className = "dc-leaf";
  const hdr = document.createElement("div");
  hdr.className = "dc-leaf-hdr";
  if (build) {
    hdr.setAttribute("role", "button");
    hdr.setAttribute("tabindex", "0");
    hdr.setAttribute("aria-expanded", "false");
  }
  hdr.innerHTML =
    `<span class="dc-dot dc-dot--${status}"></span>` +
    `<span class="dc-leaf-title">${rtEscapeAttr(title)}</span>` +
    `<span class="dc-leaf-sum">${rtEscapeAttr(summary)}</span>` +
    (build ? `<span class="dc-chev dc-chev-leaf" aria-hidden="true">▾</span>` : "");
  leaf.appendChild(hdr);
  if (build) {
    const bdy = document.createElement("div");
    bdy.className = "dc-leaf-body dc-leaf-body--hidden";
    build(bdy);
    leaf.appendChild(bdy);
    const toggle = () => {
      const hidden = bdy.classList.toggle("dc-leaf-body--hidden");
      hdr.setAttribute("aria-expanded", hidden ? "false" : "true");
      hdr.querySelector<HTMLElement>(".dc-chev-leaf")!.textContent = hidden ? "▾" : "▴";
    };
    hdr.addEventListener("click", toggle);
    hdr.addEventListener("keydown", (e: KeyboardEvent) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggle(); }
    });
  }
  return leaf;
}

function _dcReasonSection(data: any, routing: any): HTMLElement {
  const qp = (data.query_profile ?? {}) as any;
  const qtype = String(qp.query_type ?? "");
  const cov = typeof qp.coverage === "number" ? (qp.coverage as number).toFixed(2) : "?";
  const scores = (routing.scores ?? {}) as Record<string, number>;
  const topEntry = Object.entries(scores).sort(([, a], [, b]) => b - a)[0] as [string, number] | undefined;
  const topSt = topEntry?.[0] ?? "?";
  const topSc = typeof topEntry?.[1] === "number" ? (topEntry[1] as number).toFixed(2) : "?";
  const sum = `${qtype || "?"} · coverage ${cov} · ${topSt} wins ${topSc}`;

  return _dcSection("1 · REASON", "ok", sum, (body) => {
    // gate
    const gate = (data.gate ?? {}) as any;
    const gatePassed = gate.passed !== false;
    body.appendChild(_dcLeaf("gate", gatePassed ? "ok" : "warn",
      gatePassed ? "passed" : `fired · ${gate.reason ?? "?"}`));

    // cleanup
    const anchors: string[] = qp.literal_anchors ?? [];
    const tagMatches: string[] = qp.tag_matches ?? [];
    const untagged: string[] = qp.untagged_meaningful ?? [];
    const dropped: string[] = qp.dropped ?? [];
    const nIn = anchors.length + tagMatches.length + untagged.length + dropped.length || "?";
    const nKept = anchors.length + tagMatches.length + untagged.length || "?";
    body.appendChild(_dcLeaf("cleanup", "ok", `${nIn} tokens → ${nKept} kept`, (b) => {
      if (anchors.length) _dcKV(b, "literal anchors", anchors.join(" · "));
      if (tagMatches.length) _dcKV(b, "tag matches", tagMatches.join("  "));
      if (untagged.length) _dcKV(b, "untagged meaningful", untagged.join("  "));
      if (dropped.length) _dcKV(b, "dropped (noise)", dropped.join("  "));
    }));

    // rewrite
    const qps = (data.queries_per_strategy ?? {}) as Record<string, string>;
    const qpsKeys = Object.keys(qps);
    body.appendChild(_dcLeaf("rewrite", "ok", `${qpsKeys.length || 3} per-strategy variants`,
      qpsKeys.length ? (b) => {
        for (const [k, v] of Object.entries(qps)) _dcKV(b, k, String(v).slice(0, 120));
      } : undefined));

    // classify
    body.appendChild(_dcLeaf("classify", "ok", `${qtype || "?"} · coverage ${cov}`, (b) => {
      _dcKV(b, "query_type", qtype || "—");
      _dcKV(b, "coverage", cov);
      const dt: string[] = qp.d_tags ?? qp.domain_tags ?? [];
      const jt: string[] = qp.j_tags ?? qp.jurisdiction_tags ?? [];
      const pt: string[] = qp.p_tags ?? qp.process_tags ?? [];
      if (dt.length) _dcKV(b, "domain_tags", dt.join("  "));
      if (jt.length) _dcKV(b, "jurisdiction_tags", jt.join("  "));
      if (pt.length) _dcKV(b, "process_tags", pt.join("  "));
      const cf = (routing.classify_flags ?? {}) as Record<string, unknown>;
      const activeFlags = Object.entries(cf).filter(([, v]) => v).map(([k]) => k);
      if (activeFlags.length) _dcKV(b, "flags", activeFlags.join("  "));
      const sc = qp.semantic_core ?? "";
      if (sc) _dcKV(b, "semantic_core", String(sc));
    }));

    // scorer
    const sb = (routing.score_breakdown ?? {}) as any;
    const fv = (routing.feature_vector ?? {}) as Record<string, unknown>;
    const sa = (routing.self_assessments ?? {}) as Record<string, any>;
    const withdrawn: string[] = routing.withdrawn ?? [];
    const micConsidered = Boolean(routing.multi_invoke_considered);
    body.appendChild(_dcLeaf("scorer", "ok",
      `${topSt} wins ${topSc} argmax${micConsidered ? " · multi_invoke considered" : ""}`,
      (b) => {
        if (Object.keys(scores).length) {
          const tbl = document.createElement("table");
          tbl.className = "rt-score-table rt-mono";
          tbl.innerHTML = "<thead><tr><th>strat</th><th>score</th><th>accuracy</th><th>recall</th><th>speed</th><th>shape</th></tr></thead>";
          const tb = document.createElement("tbody");
          for (const [s, score] of Object.entries(scores)) {
            const bd = sb[s] ?? {};
            const fmt = (x: any) => typeof x === "number" ? x.toFixed(2) : (x?.contrib !== undefined ? (x.contrib as number).toFixed(2) : "·");
            const tr = document.createElement("tr");
            const isW = s === topSt;
            const isWd = withdrawn.includes(s);
            if (isW) tr.className = "rt-score-winner";
            else if (isWd) tr.className = "rt-sa-withdrawn";
            tr.innerHTML =
              `<td>${isW ? "★ " : ""}${rtEscapeAttr(s)}${isWd ? " ⊘" : ""}</td>` +
              `<td><b>${typeof score === "number" ? score.toFixed(2) : "·"}</b></td>` +
              `<td>${fmt(bd.accuracy)}</td><td>${fmt(bd.recall)}</td><td>${fmt(bd.speed)}</td><td>${fmt(bd.shape)}</td>`;
            tb.appendChild(tr);
          }
          tbl.appendChild(tb);
          b.appendChild(tbl);
        }
        const fvKeys = Object.keys(fv);
        if (fvKeys.length) {
          _dcKV(b, "feature_vector",
            fvKeys.map((k) => `${k}=${typeof fv[k] === "number" ? (fv[k] as number).toFixed(2) : fv[k]}`).join("  "));
        }
        for (const [s, v] of Object.entries(sa)) {
          const est = typeof v?.est_recall === "number" ? v.est_recall : (Array.isArray(v) ? v[0] : null);
          if (est !== null) _dcKV(b, `${s} est_recall`, typeof est === "number" ? est.toFixed(2) : String(est));
        }
      }));
  });
}

function _dcActRetrieveContent(container: HTMLElement, st: any, data: any, routing: any): void {
  const strat = String(st?.strategy ?? routing.strategy ?? routing.executed_strategy ?? "a");
  const arms = (st?.arms ?? {}) as any;
  if (strat === "b") {
    const themes: any[] = data.themes ?? [];
    _dcKV(container, "wide→themes→narrow", `${themes.length} themes`);
    const stb = (data.telemetry ?? {}) as any;
    const sb = stb.strategy_b ?? data.theme_diagnostic ?? {};
    if (sb.wide_hits !== undefined) _dcKV(container, "wide_hits", String(sb.wide_hits));
    const parts: string[] = [];
    if (sb.wide_ms) parts.push(`wide ${sb.wide_ms}ms`);
    if (sb.themes_ms) parts.push(`themes ${sb.themes_ms}ms`);
    if (sb.narrow_ms) parts.push(`narrow ${sb.narrow_ms}ms`);
    if (parts.length) _dcKV(container, "timings", parts.join(" · "));
    themes.slice(0, 5).forEach((th: any) => {
      const topR = th?.top_chunks?.[0]?.rerank_score;
      const row = document.createElement("div");
      row.className = "rt-kv";
      row.innerHTML = `<span class="rt-kv-k">${rtEscapeAttr(String(th?.label ?? "?"))}</span>` +
        `<span class="rt-kv-v">${th?.n_chunks_seen ?? "?"} chunks${typeof topR === "number" ? ` · top ${(topR as number).toFixed(2)}` : ""}</span>`;
      container.appendChild(row);
    });
  } else if (strat === "c") {
    const vc: any[] = data.validated_citations ?? [];
    _dcKV(container, "reverse-RAG", `${vc.length} citations verified`);
    vc.slice(0, 4).forEach((c: any) => {
      const row = document.createElement("div");
      row.className = "rt-kv";
      row.innerHTML = `<span class="rt-kv-k">${rtEscapeAttr(String(c?.url ?? c?.title ?? "?").slice(0, 40))}</span>` +
        `<span class="rt-kv-v">${rtEscapeAttr(String(c?.outcome ?? "?"))}</span>`;
      container.appendChild(row);
    });
  } else if (strat === "d") {
    _dcKV(container, "external search", `${st?.n_chunks ?? 0} results`);
    const hint = document.createElement("div");
    hint.className = "rt-expansion-hint";
    hint.textContent = "per-URL fetch breakdown — not captured yet";
    container.appendChild(hint);
    _dcKV(container, "caller_id", "not captured yet");
  } else {
    // a · hybrid (default)
    _dcKV(container, "BM25 pool", String(arms.bm25_pool_hits ?? arms.bm25_hits ?? arms.bm25 ?? 0));
    _dcKV(container, "vector pool", String(arms.vector_pool_hits ?? arms.vec_hits ?? arms.vector ?? 0));
    const rb = (arms.result_breakdown ?? {}) as any;
    if (Object.keys(rb).length) {
      _dcKV(container, "result split", `bm25=${rb.bm25_only ?? 0} vec=${rb.vector_only ?? 0} both=${rb.both ?? 0}`);
    }
    const tm = (arms.timing_ms ?? {}) as Record<string, unknown>;
    const tmParts = Object.entries(tm)
      .filter(([, v]) => typeof v === "number" && (v as number) > 0)
      .map(([k, v]) => `${k} ${Math.round(v as number)}ms`);
    if (tmParts.length) _dcKV(container, "timings", tmParts.join(" · "));
    if (st?.n_chunks !== undefined) _dcKV(container, "chunks returned", String(st.n_chunks));
    if (typeof st?.top_rerank === "number") _dcKV(container, "top_rerank", (st.top_rerank as number).toFixed(2));
  }
}

function _dcActSection(data: any, routing: any, isFactStore: boolean, chainLabel: string): HTMLElement {
  const strategies: any[] = data.strategies_tried ?? [];
  const strategy = String(routing.executed_strategy ?? routing.strategy ?? (isFactStore ? "s" : "a"));
  const nChunks: number = data.n_chunks ?? strategies.reduce((a: number, s: any) => a + (s.n_chunks ?? 0), 0) ?? 0;
  const conf = String(data.confidence ?? "—");
  const answerSnip = String(data.llm_answer ?? "").slice(0, 50);
  const sum = `${chainLabel} · ${nChunks} chunks · ${conf}${answerSnip ? " · " + answerSnip + "…" : ""}`;

  return _dcSection("2 · ACT", "ok", sum, (body) => {
    // ── retrieve ──
    const retrieveEl = document.createElement("div");
    retrieveEl.className = "dc-leaf";
    const retrieveHdr = document.createElement("div");
    retrieveHdr.className = "dc-leaf-hdr";
    retrieveHdr.setAttribute("role", "button");
    retrieveHdr.setAttribute("tabindex", "0");
    retrieveHdr.setAttribute("aria-expanded", "false");
    const retrieveSum = isFactStore ? "⚡ s · fact_store · direct serve" : `${strategy} · ${nChunks} chunks`;
    retrieveHdr.innerHTML =
      `<span class="dc-dot dc-dot--ok"></span>` +
      `<span class="dc-leaf-title">retrieve</span>` +
      `<span class="dc-leaf-sum">${rtEscapeAttr(retrieveSum)}</span>` +
      `<span class="dc-chev dc-chev-leaf" aria-hidden="true">▾</span>`;
    retrieveEl.appendChild(retrieveHdr);

    const retrieveBody = document.createElement("div");
    retrieveBody.className = "dc-leaf-body dc-leaf-body--hidden";

    if (isFactStore) {
      // Clone the provenance card content from _renderFactStoreLeaf without the outer collapse shell.
      const fsWrap = _renderFactStoreLeaf(data, routing);
      const fsInner = fsWrap.querySelector<HTMLElement>(".llm-performance-body");
      if (fsInner) retrieveBody.appendChild(fsInner.cloneNode(true));
    } else if (strategies.length > 1) {
      const tabBar = document.createElement("div");
      tabBar.className = "dc-tab-bar";
      const panels: HTMLElement[] = [];
      strategies.forEach((st, i) => {
        const stLabel = String(st?.strategy ?? i);
        const tab = document.createElement("button");
        tab.type = "button";
        tab.className = `dc-tab${i === 0 ? " dc-tab--active" : ""}`;
        tab.textContent = stLabel;
        const panel = document.createElement("div");
        panel.className = `dc-tab-panel${i === 0 ? "" : " dc-tab-panel--hidden"}`;
        _dcActRetrieveContent(panel, st, data, routing);
        panels.push(panel);
        tab.addEventListener("click", () => {
          tabBar.querySelectorAll(".dc-tab").forEach((t) => t.classList.remove("dc-tab--active"));
          panels.forEach((p) => p.classList.add("dc-tab-panel--hidden"));
          tab.classList.add("dc-tab--active");
          panel.classList.remove("dc-tab-panel--hidden");
        });
        tabBar.appendChild(tab);
      });
      retrieveBody.appendChild(tabBar);
      panels.forEach((p) => retrieveBody.appendChild(p));
    } else {
      _dcActRetrieveContent(retrieveBody, strategies[0] ?? { strategy }, data, routing);
    }

    retrieveEl.appendChild(retrieveBody);
    const retrieveToggle = () => {
      const hidden = retrieveBody.classList.toggle("dc-leaf-body--hidden");
      retrieveHdr.setAttribute("aria-expanded", hidden ? "false" : "true");
      retrieveHdr.querySelector<HTMLElement>(".dc-chev-leaf")!.textContent = hidden ? "▾" : "▴";
    };
    retrieveHdr.addEventListener("click", retrieveToggle);
    retrieveHdr.addEventListener("keydown", (e: KeyboardEvent) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); retrieveToggle(); }
    });
    body.appendChild(retrieveEl);

    // ── rerank — scoring_trace lives inside the matched strategies_tried entry ──
    if (isFactStore) {
      body.appendChild(_dcLeaf("rerank", "gray", "not executed · direct serve"));
    } else {
      const primarySt = strategies.find((s) => s.strategy === strategy) ?? strategies[0] ?? {};
      const sc: any[] = primarySt.scoring_trace ?? [];
      // top_rerank on the entry; sim_raw/authority_raw are the confirmed per-chunk fields (no rerank_score)
      const topRR = typeof primarySt.top_rerank === "number" ? primarySt.top_rerank as number : null;
      body.appendChild(_dcLeaf("rerank", "ok", `top ${typeof topRR === "number" ? topRR.toFixed(2) : "—"}`,
        sc.length ? (b) => {
          sc.slice(0, 6).forEach((c: any) => {
            const row = document.createElement("div");
            row.className = "rt-kv";
            const sim = typeof c?.sim_raw === "number" ? (c.sim_raw as number).toFixed(2) : "·";
            const auth = typeof c?.authority_raw === "number" ? ` auth=${(c.authority_raw as number).toFixed(2)}` : "";
            const cov = typeof c?.coverage_raw === "number" ? ` cov=${(c.coverage_raw as number).toFixed(2)}` : "";
            row.innerHTML =
              `<span class="rt-kv-k">${rtEscapeAttr(String(c?.document_name ?? c?.doc_name ?? "?").slice(0, 30))}</span>` +
              `<span class="rt-kv-v">sim=${sim}${auth}${cov}</span>`;
            b.appendChild(row);
          });
        } : undefined));
    }

    // ── assemble ──
    if (isFactStore) {
      body.appendChild(_dcLeaf("assemble", "gray", "not executed · direct serve"));
    } else {
      const asm = (data.assembly ?? {}) as any;
      body.appendChild(_dcLeaf("assemble", "ok",
        `${asm.total_selected ?? nChunks}/${data.k ?? "?"} · ${asm.strategy ?? data.mode ?? "corpus"}`,
        Object.keys(asm).length ? (b) => {
          if (asm.canonical_ratio !== undefined) _dcKV(b, "canonical_ratio", Number(asm.canonical_ratio).toFixed(2));
          if (asm.total_selected !== undefined) _dcKV(b, "total_selected", String(asm.total_selected));
          if (asm.strategy) _dcKV(b, "strategy", String(asm.strategy));
        } : undefined));
    }

    // ── synthesize ──
    if (isFactStore) {
      body.appendChild(_dcLeaf("synthesize", "gray", "not executed · direct serve"));
    } else {
      const tel = (data.telemetry ?? {}) as any;
      const llmMs: number = tel.llm_ms ?? 0;
      body.appendChild(_dcLeaf("synthesize", "ok",
        `answer built · ${conf}${llmMs ? ` · ${llmMs}ms` : ""}`,
        (b) => {
          if (tel.model) _dcKV(b, "model", String(tel.model));
          if (llmMs) _dcKV(b, "llm_ms", String(llmMs));
          if (tel.n_passages_offered) _dcKV(b, "passages_offered", String(tel.n_passages_offered));
          const used: unknown[] = tel.used_passages ?? [];
          if (used.length) _dcKV(b, "used_passages", used.join(", "));
        }));
    }
  });
}

function _dcObserveSection(data: any, routing: any, isFactStore: boolean): HTMLElement {
  return _dcSection("3 · OBSERVE", "ok", "retrieval —/synth grading… · 1 row", (body) => {
    body.appendChild(_dcLeaf("retrieval_grade", "gray",
      isFactStore ? "certified · see provenance card" : "n/a · no gold at inference"));
    body.appendChild(_dcLeaf("synthesis_grade", "gray", "grading…"));
    body.appendChild(_dcLeaf("per_claim_ledger", "gray", "not available in prod"));
    // routing_decision_id: RAG sends at top-level; fact_telemetry_id aliases for strategy-s
    const decId = String(
      data.routing_decision_id ?? routing.routing_decision_id ??
      routing.fact_telemetry_id ?? data.telemetry?.routing_decision_id ?? ""
    );
    body.appendChild(_dcLeaf("decision_row", "ok",
      decId ? `id=${decId.slice(0, 12)}` : "row pending…",
      (b) => {
        _dcKV(b, "decision_id", decId || "pending…");
        _dcKV(b, "caller_id", "not captured yet");
        if (data.priors_version) _dcKV(b, "priors_version", String(data.priors_version));
        if (data.corpus_version) {
          _dcKV(b, "corpus_version", String(data.corpus_version));
          if (Number(data.corpus_version) === 1) {
            const note = document.createElement("div");
            note.className = "rt-expansion-hint";
            note.textContent = "bump not wired";
            b.appendChild(note);
          }
        }
      }));
  });
}

function _dcDecideSection(data: any, routing: any, chainArr: string[]): HTMLElement {
  const fe = (data.fast_exit ?? {}) as any;
  const fastExitFired = Boolean(fe.fired);
  const escalated = Boolean(data.escalated);
  const micConsidered = Boolean(routing.multi_invoke_considered);
  const chainStr = chainArr.length > 1 ? chainArr.join("→") : "";
  const sum =
    `${chainArr.length > 1 ? `${chainStr} · ${chainArr.length}-try` : "single"}` +
    `${fastExitFired ? " · fast-exit fired" : ""}` +
    `${escalated && !chainStr ? " · escalated" : ""}` +
    ` · bandit not wired`;

  return _dcSection("4 · DECIDE", "warn", sum, (body) => {
    body.appendChild(_dcLeaf("multi_invoke", micConsidered ? "ok" : "gray",
      micConsidered ? "considered" : "not triggered"));
    // escalate leaf: show real chain (e.g. "b→d · corpus_exhausted") not generic count
    const escalReason = data.escalation_reason ?? (data.strategies_tried as any[])?.slice(-1)[0]?.escalation_reason ?? "";
    const escalSum = chainArr.length > 1
      ? `${chainStr}${escalReason ? " · " + escalReason : ""}`
      : (escalated ? "escalated" : "single attempt");
    body.appendChild(_dcLeaf("escalate", (escalated || chainArr.length > 1) ? "warn" : "ok", escalSum));
    body.appendChild(_dcLeaf("fast_exit", fastExitFired ? "warn" : "ok",
      fastExitFired ? `fired · ${fe.reason ?? "?"}` : "not triggered"));
    body.appendChild(_dcLeaf("bandit", "gray", "not built · loop open"));
  });
}

/** Canonical reason→act→observe→decide diagnostics card. Replaces the old
 *  raw PARSER/ROUTER/RERANKING/THEMES dump for ALL strategies. */
function renderDiagnosticsCard(
  thinkingLog: ReadonlyArray<unknown> | null | undefined,
): HTMLElement | null {
  if (!Array.isArray(thinkingLog) || thinkingLog.length === 0) return null;
  // Collect all retrieval_trace envelopes (multiple if planner did several rounds).
  const traces: Array<{ data: any; step_id?: string }> = [];
  for (const entry of thinkingLog) {
    if (entry && typeof entry === "object" && (entry as any).signal === "retrieval_trace") {
      const e = entry as any;
      traces.push({ data: (e.data as any) ?? {}, step_id: e.step_id });
    }
  }
  if (traces.length === 0) return null;

  const last = traces[traces.length - 1];
  const data = last.data ?? {};
  const routing = (data.routing ?? {}) as any;
  // routing.method="fact_store" fires even on a miss; require fact_predicate/telemetry_id
  // as proof that a fact was actually matched and served (not just attempted).
  const isFactStore = String(routing.method ?? "") === "fact_store"
    && (Boolean(routing.fact_predicate) || Boolean(routing.fact_telemetry_id));
  // executed_strategy = what actually ran (authoritative); strategy = scorer's pick (may differ)
  const strategy = String(routing.executed_strategy ?? routing.strategy ?? (isFactStore ? "s" : "?"));
  const chainArr: string[] = data.strategy_chain ?? [];
  // chainLabel: "b→d" on escalation, plain strategy otherwise
  const chainLabel = chainArr.length > 1 ? chainArr.join("→") : strategy;
  const totalMs = Number(data.total_ms ?? (data.timing ?? {}).total_ms ?? 0);
  const conf = String(data.confidence ?? "");

  const wrap = document.createElement("div");
  wrap.className = "llm-performance retrieval-trace collapsed";

  // ── Glance bar (preview row) ─────────────────────────────────────
  const preview = document.createElement("div");
  preview.className = "llm-performance-preview";
  preview.setAttribute("role", "button");
  preview.setAttribute("tabindex", "0");
  preview.setAttribute("aria-expanded", "false");
  const titleEl = document.createElement("span");
  titleEl.className = "llm-performance-title";
  titleEl.textContent = "Retrieval";
  const oneline = document.createElement("span");
  oneline.className = "llm-performance-oneline";
  if (isFactStore) {
    const pred = String(routing.fact_predicate ?? "");
    const score = typeof routing.fact_score === "number" ? (routing.fact_score as number).toFixed(2) : "1.00";
    oneline.textContent = `⚡ s · fact_store · ${pred || "certified fact"} · score ${score}`;
  } else {
    const qtype = String((data.query_profile ?? {}).query_type ?? "");
    oneline.textContent =
      `→ ${chainLabel}${qtype ? " · " + qtype : ""}${conf ? " · " + conf : ""}` +
      `${totalMs > 0 ? " · " + (totalMs / 1000).toFixed(2) + "s" : ""}` +
      `${traces.length > 1 ? ` · ${traces.length} rounds` : ""}`;
  }
  const chev = document.createElement("span");
  chev.className = "llm-performance-chevron";
  chev.setAttribute("aria-hidden", "true");
  chev.textContent = "▼";
  preview.appendChild(titleEl);
  preview.appendChild(oneline);
  preview.appendChild(chev);

  // ── Expanded body — 4 accordion sections ─────────────────────────
  const body = document.createElement("div");
  body.className = "llm-performance-body";
  body.appendChild(_dcReasonSection(data, routing));
  body.appendChild(_dcActSection(data, routing, isFactStore, chainLabel));
  body.appendChild(_dcObserveSection(data, routing, isFactStore));
  body.appendChild(_dcDecideSection(data, routing, chainArr));

  wrap.appendChild(preview);
  wrap.appendChild(body);

  const toggle = () => {
    const collapsed = wrap.classList.toggle("collapsed");
    preview.setAttribute("aria-expanded", collapsed ? "false" : "true");
    chev.textContent = collapsed ? "▼" : "▲";
  };
  preview.addEventListener("click", toggle);
  preview.addEventListener("keydown", (e: KeyboardEvent) => {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggle(); }
  });

  return wrap;
}

/** Create a collapsible section div matching the RAG UI's stp-section pattern.
 *  Returns { el, body } where body is the content container to append into. */
function rtMakeSection(
  title: string,
  badge: string,
  collapsed = false,
): { el: HTMLElement; body: HTMLElement } {
  const el = document.createElement("div");
  el.className = "rt-section" + (collapsed ? " rt-section--collapsed" : "");

  const hdr = document.createElement("button");
  hdr.type = "button";
  hdr.className = "rt-section-hdr";
  hdr.setAttribute("aria-expanded", String(!collapsed));

  const chev = document.createElement("span");
  chev.className = "rt-section-chev";
  chev.setAttribute("aria-hidden", "true");
  chev.textContent = collapsed ? "▶" : "▼";

  const titleEl = document.createElement("span");
  titleEl.className = "rt-section-title";
  titleEl.textContent = title;

  const badgeEl = document.createElement("span");
  badgeEl.className = "rt-section-badge";
  badgeEl.textContent = badge;

  hdr.appendChild(chev);
  hdr.appendChild(titleEl);
  hdr.appendChild(badgeEl);

  const body = document.createElement("div");
  body.className = "rt-section-body";
  if (collapsed) body.style.display = "none";

  hdr.addEventListener("click", () => {
    const isCollapsed = el.classList.toggle("rt-section--collapsed");
    body.style.display = isCollapsed ? "none" : "";
    chev.textContent = isCollapsed ? "▶" : "▼";
    hdr.setAttribute("aria-expanded", String(!isCollapsed));
  });

  el.appendChild(hdr);
  el.appendChild(body);
  return { el, body };
}

function rtEscapeAttr(s: string): string {
  return s
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}
function rtFormatSig(v: unknown): string {
  if (typeof v !== "number") return "—";
  return v.toFixed(3);
}

/** Render a horizontal bar + numeric value for a rerank signal (sim/auth/jpd).
 * Bar width is the value as a percent of 1.0 (clamped). Color is chosen
 * per signal so a row reads at a glance: sim = purple, auth = green,
 * jpd = teal. Empty/zero values render as a faint baseline + dash. */
function rtBar(value: number, kind: "sim" | "auth" | "jpd"): string {
  if (!Number.isFinite(value) || value <= 0) {
    return '<span class="rt-bar rt-bar--empty">—</span>';
  }
  const pct = Math.max(0, Math.min(100, value * 100));
  return (
    `<span class="rt-bar rt-bar--${kind}">` +
      `<span class="rt-bar-track">` +
        `<span class="rt-bar-fill" style="width:${pct.toFixed(1)}%"></span>` +
      `</span>` +
      `<span class="rt-bar-val">${value.toFixed(3)}</span>` +
    `</span>`
  );
}

/** Confidence label as a small pill — high/med/low/—. Lets the viewer
 * scan a column of confidences without parsing text. */
function rtConfBadge(label: unknown): string {
  if (typeof label !== "string" || !label) return "—";
  const lc = label.toLowerCase();
  let cls = "rt-conf";
  if (lc === "high") cls += " rt-conf--high";
  else if (lc === "medium" || lc === "med") cls += " rt-conf--med";
  else if (lc === "low") cls += " rt-conf--low";
  return `<span class="${cls}">${rtEscapeAttr(label)}</span>`;
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
      if (docId) {
        const readerLink = document.createElement("a");
        readerLink.href = "#";
        readerLink.className = "source-open-doc-link";
        readerLink.textContent = "Open document";
        readerLink.addEventListener("click", (e) => {
          e.preventDefault();
          e.stopPropagation();
          openDocReaderPanel(docId, s.page_number, (s.cite_text ?? s.snippet ?? "").slice(0, 100));
        });
        actions.appendChild(readerLink);
      }
      if (ragUrl) {
        const link = document.createElement("a");
        link.href = ragUrl;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        link.className = "source-open-doc-link";
        link.textContent = "Open in RAG \u2197";
        link.style.opacity = "0.6";
        link.style.fontSize = "11px";
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

/** One resolved document inside a document_download envelope block. */
interface DocumentDownloadEntry {
  document_id: string;
  title: string;
  download_url: string;
  fallback_download_url?: string;
  filename?: string;
  host?: string;
  payer?: string;
  state?: string;
  program?: string;
  authority_level?: string;
  resolved_via?: string;
}

/** Fetch the document bytes and save them via a blob anchor.
 * Tries the original-file endpoint first; a 404 there means the doc is
 * scraped/text-only, so retry the reconstructed-PDF fallback. If fetch
 * itself is blocked (dev CORS), fall back to a plain new-tab open —
 * navigation downloads are CORS-exempt. */
async function downloadDocumentFile(d: DocumentDownloadEntry, btn: HTMLButtonElement): Promise<void> {
  const idleLabel = btn.textContent || "Download";
  btn.disabled = true;
  btn.textContent = "Downloading…";
  // blocked=true means fetch itself threw (CORS / network) — a plain
  // navigation may still succeed, and retrying the fallback URL via
  // fetch would just be blocked the same way.
  // Attach the platform token ONLY on same-origin (relative) URLs —
  // chat's own /chat/uploads/…/download and /chat/download-proxy need
  // it under required-auth; sending it to RAG or source sites would
  // leak the token cross-origin.
  const sameOriginAuthHeaders = (url: string): Record<string, string> => {
    if (!url.startsWith("/")) return {};
    try {
      const tok = localStorage.getItem("mobius.auth.accessToken");
      return tok ? { Authorization: "Bearer " + tok } : {};
    } catch {
      return {};
    }
  };
  const tryFetch = async (url: string): Promise<{ blob: Blob | null; blocked: boolean }> => {
    try {
      const r = await fetch(url, { headers: sameOriginAuthHeaders(url) });
      if (!r.ok) return { blob: null, blocked: false };
      return { blob: await r.blob(), blocked: false };
    } catch {
      return { blob: null, blocked: true };
    }
  };
  let name = (d.filename || d.title || "document").trim() || "document";
  const first = await tryFetch(d.download_url);
  let blob = first.blob;
  if (!blob && !first.blocked && d.fallback_download_url) {
    // Original 404'd (scraped/text-only doc) — reconstructed PDF instead.
    blob = (await tryFetch(d.fallback_download_url)).blob;
    if (blob && !/\.pdf$/i.test(name)) name = name.replace(/\.[A-Za-z0-9]+$/, "") + ".pdf";
  }
  if (blob) {
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = name;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 30_000);
    btn.textContent = "Downloaded ✓";
    setTimeout(() => {
      btn.textContent = idleLabel;
      btn.disabled = false;
    }, 4000);
  } else {
    // CORS-blocked → original URL (navigation is CORS-exempt);
    // otherwise the original 404'd/failed, so the fallback is the best bet.
    const openUrl = first.blocked ? d.download_url : d.fallback_download_url || d.download_url;
    window.open(openUrl, "_blank", "noopener");
    btn.textContent = idleLabel;
    btn.disabled = false;
  }
}

/** Render the document_download envelope block: one card per resolved
 * document with title, metadata chips, and a Download action. */
function renderDocumentDownloadBlock(entries: DocumentDownloadEntry[]): HTMLElement {
  const wrap = document.createElement("div");
  wrap.className = "doc-download-block";
  for (const d of entries || []) {
    if (!d || !d.download_url || !d.title) continue;
    const card = document.createElement("div");
    card.className = "doc-download-card";

    const icon = document.createElement("div");
    icon.className = "doc-download-icon";
    icon.innerHTML =
      '<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
      '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"></path>' +
      '<polyline points="14 2 14 8 20 8"></polyline>' +
      '<line x1="12" y1="12" x2="12" y2="18"></line>' +
      '<polyline points="9 15 12 18 15 15"></polyline></svg>';

    const info = document.createElement("div");
    info.className = "doc-download-info";
    const title = document.createElement("div");
    title.className = "doc-download-title";
    title.textContent = d.title;
    info.appendChild(title);
    const metaParts = [d.filename, d.host, d.payer, d.state, d.program, d.authority_level].filter(
      (x): x is string => typeof x === "string" && x.trim() !== "" && x !== d.title
    );
    if (metaParts.length) {
      const meta = document.createElement("div");
      meta.className = "doc-download-meta";
      meta.textContent = metaParts.join(" · ");
      info.appendChild(meta);
    }

    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "doc-download-btn";
    btn.textContent = "Download";
    btn.addEventListener("click", () => {
      void downloadDocumentFile(d, btn);
    });

    card.appendChild(icon);
    card.appendChild(info);
    card.appendChild(btn);
    wrap.appendChild(card);
  }
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
    threadId?: string | null;
  }
): HTMLElement {
  const outer = document.createElement("div");
  outer.className = "assistant-envelope";

  const bubble = document.createElement("div");
  bubble.className = "message-bubble answer-card-bubble";

  let confidenceInjectedAfterDirectAnswer = false;
  const pendingActionChips: HTMLElement[] = [];

  for (const block of envelope.blocks || []) {
    if (!block || typeof block !== "object") continue;
    const t = (block as EnvelopeBlock).type;
    if (t === "correction") {
      const b = block as { original: string; corrected: string };
      const orig = (b.original || "").trim();
      const fixed = (b.corrected || "").trim();
      if (orig && fixed) {
        const line = document.createElement("div");
        line.className = "envelope-correction-inline";
        const icon = document.createElement("span");
        icon.className = "envelope-correction-inline-icon";
        icon.textContent = "⚠";
        const origSpan = document.createElement("span");
        origSpan.className = "envelope-correction-inline-orig";
        origSpan.textContent = orig;
        const arrow = document.createElement("span");
        arrow.className = "envelope-correction-inline-arrow";
        arrow.textContent = " → ";
        const fixedSpan = document.createElement("span");
        fixedSpan.className = "envelope-correction-inline-fixed";
        fixedSpan.textContent = fixed;
        line.appendChild(icon);
        line.appendChild(document.createTextNode(" "));
        line.appendChild(origSpan);
        line.appendChild(arrow);
        line.appendChild(fixedSpan);
        bubble.appendChild(line);
      }
    } else if (t === "takeaways") {
      const b = block as { items: string[] };
      if (Array.isArray(b.items) && b.items.length > 0) {
        const wrap = document.createElement("div");
        wrap.className = "envelope-takeaways";
        const hdr = document.createElement("div");
        hdr.className = "envelope-takeaways-header";
        hdr.textContent = "Key takeaways";
        wrap.appendChild(hdr);
        const ul = document.createElement("ul");
        ul.className = "envelope-takeaways-list";
        for (const item of b.items) {
          const li = document.createElement("li");
          li.textContent = item;
          ul.appendChild(li);
        }
        wrap.appendChild(ul);
        bubble.appendChild(wrap);
      }
    } else if (t === "tool_attribution") {
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
    } else if (t === "document_download") {
      const b = block as { documents?: DocumentDownloadEntry[] };
      if (Array.isArray(b.documents) && b.documents.length) {
        bubble.appendChild(renderDocumentDownloadBlock(b.documents));
      }
    } else if (t === "task_list") {
      const b = block as {
        tasks: Array<{
          task_id: string; text: string; detail?: string; status: string;
          severity: string; source_module?: string; provider_name?: string;
          npi?: string; assignee?: string; deadline?: string;
          created_at?: string; org_name?: string; dim?: string; type?: string;
        }>;
        filters?: Record<string, string>;
        operation?: string;
        allow_create?: boolean;
        allow_resolve?: boolean;
        allow_edit?: boolean;
        allow_assign?: boolean;
        allow_dismiss?: boolean;
      };

      // ── helpers ────────────────────────────────────────────────────────────
      const SEV_LABEL: Record<string, string> = { critical: "Critical", warning: "Warning", info: "Info", low: "Low", none: "None" };
      const SEV_ORDER: Record<string, number> = { critical: 0, warning: 1, info: 2, low: 3, none: 4 };
      const MOD_LABEL: Record<string, string> = {
        roster_open: "Roster", roster_recon: "Reconciliation",
        credentialing: "Credentialing", manual: "Manual",
      };

      // Parse detail: if JSON, extract readable recommendation + issues
      function parseDetail(raw: string | undefined): { summary: string; lines: string[] } | null {
        if (!raw) return null;
        try {
          const d = JSON.parse(raw);
          const rec: string = d.recommendation || "";
          const issues: string[] = (d.issues || []).map((x: unknown) => String(x));
          const warns: string[] = (d.warnings || []).map((x: unknown) => String(x));
          const lines = [...issues, ...warns].filter(Boolean).slice(0, 6);
          return { summary: rec || lines[0] || raw.slice(0, 120), lines };
        } catch {
          return { summary: raw.slice(0, 200), lines: [] };
        }
      }

      function fmtModule(s: string): string {
        return MOD_LABEL[s] || s.replace(/_/g, " ").replace(/\b\w/g, c => c.toUpperCase());
      }

      const tasks = (b.tasks || []).slice().sort(
        (a, b2) => (SEV_ORDER[a.severity] ?? 3) - (SEV_ORDER[b2.severity] ?? 3)
      );

      const wrap = document.createElement("div");
      wrap.className = "tm-envelope-wrap";

      // ── Header ─────────────────────────────────────────────────────────────
      const hdr = document.createElement("div");
      hdr.className = "tm-env-header";

      const hdrLeft = document.createElement("div");
      hdrLeft.className = "tm-env-header-left";
      const hdrTitle = document.createElement("span");
      hdrTitle.className = "tm-env-title";
      hdrTitle.textContent = "Tasks";
      hdrLeft.appendChild(hdrTitle);
      // Severity summary chips
      const sevCounts: Record<string, number> = {};
      for (const tk of tasks) sevCounts[tk.severity || "low"] = (sevCounts[tk.severity || "low"] || 0) + 1;
      for (const sev of ["critical", "warning", "info", "low"] as const) {
        if (!sevCounts[sev]) continue;
        const chip = document.createElement("span");
        chip.className = `tm-env-sev-chip tm-env-sev-chip--${sev}`;
        chip.textContent = `${sevCounts[sev]} ${SEV_LABEL[sev]}`;
        hdrLeft.appendChild(chip);
      }
      hdr.appendChild(hdrLeft);

      const hdrRight = document.createElement("div");
      hdrRight.className = "tm-env-header-right";
      hdrRight.textContent = `${tasks.length} task${tasks.length !== 1 ? "s" : ""}`;
      hdr.appendChild(hdrRight);
      wrap.appendChild(hdr);

      // ── Filter strip ────────────────────────────────────────────────────────
      const activeFilters = Object.entries(b.filters || {})
        .filter(([, v]) => v != null && v !== "")
        .map(([k, v]) => `${k}: ${v}`);
      if (activeFilters.length) {
        const strip = document.createElement("div");
        strip.className = "tm-env-filter-strip";
        strip.textContent = `Filtered by: ${activeFilters.join(" · ")}`;
        wrap.appendChild(strip);
      }

      // ── Task list ───────────────────────────────────────────────────────────
      if (tasks.length === 0) {
        const empty = document.createElement("div");
        empty.className = "tm-env-empty";
        empty.textContent = "No tasks found.";
        wrap.appendChild(empty);
      } else {
        const list = document.createElement("div");
        list.className = "tm-env-list";

        for (const task of tasks) {
          const sev = task.severity || "low";
          const status = task.status || "open";
          const card = document.createElement("div");
          card.className = `tm-env-card tm-env-sev-${sev} tm-env-status-${status}`;
          card.setAttribute("data-task-id", task.task_id);

          // Left accent bar (severity colour)
          const accent = document.createElement("div");
          accent.className = `tm-env-accent tm-env-accent--${sev}`;
          card.appendChild(accent);

          // Card inner
          const inner = document.createElement("div");
          inner.className = "tm-env-card-inner";

          // ── Top row: severity badge + module tag + status ─────────────────
          const topRow = document.createElement("div");
          topRow.className = "tm-env-top-row";

          const sevBadge = document.createElement("span");
          sevBadge.className = `tm-env-badge tm-env-badge--${sev}`;
          sevBadge.textContent = SEV_LABEL[sev] || sev;
          topRow.appendChild(sevBadge);

          if (task.source_module) {
            const modTag = document.createElement("span");
            modTag.className = "tm-env-mod-tag";
            modTag.textContent = fmtModule(task.source_module);
            topRow.appendChild(modTag);
          }

          if (task.dim) {
            const dimTag = document.createElement("span");
            dimTag.className = "tm-env-dim-tag";
            dimTag.textContent = task.dim.replace(/_/g, " ");
            topRow.appendChild(dimTag);
          }

          const spacer = document.createElement("span");
          spacer.style.flex = "1";
          topRow.appendChild(spacer);

          const statusDot = document.createElement("span");
          statusDot.className = `tm-env-status-dot tm-env-status-dot--${status}`;
          statusDot.title = status === "in_progress" ? "In Progress" : status.charAt(0).toUpperCase() + status.slice(1);
          topRow.appendChild(statusDot);

          inner.appendChild(topRow);

          // ── Task title ────────────────────────────────────────────────────
          const title = document.createElement("div");
          title.className = "tm-env-card-title";
          title.textContent = task.text || "(no title)";
          inner.appendChild(title);

          // ── Provider / NPI row ────────────────────────────────────────────
          if (task.provider_name || task.npi) {
            const provRow = document.createElement("div");
            provRow.className = "tm-env-prov-row";
            if (task.provider_name) {
              const icon = document.createElement("span");
              icon.className = "tm-env-prov-icon";
              icon.textContent = "person";  // material icon name resolved via CSS
              provRow.appendChild(icon);
              const nameSpan = document.createElement("span");
              nameSpan.textContent = task.provider_name;
              provRow.appendChild(nameSpan);
            }
            if (task.npi) {
              const npiSpan = document.createElement("span");
              npiSpan.className = "tm-env-npi";
              npiSpan.textContent = `NPI ${task.npi}`;
              provRow.appendChild(npiSpan);
            }
            if (task.assignee) {
              const aSpan = document.createElement("span");
              aSpan.className = "tm-env-assignee";
              aSpan.textContent = `→ ${task.assignee}`;
              provRow.appendChild(aSpan);
            }
            inner.appendChild(provRow);
          }

          // ── Detail disclosure (parse JSON detail cleanly) ─────────────────
          const parsed = parseDetail(task.detail);
          if (parsed) {
            const det = document.createElement("details");
            det.className = "tm-env-detail";
            // summary = first 100 chars of recommendation
            const sum = document.createElement("summary");
            sum.className = "tm-env-detail-summary";
            const summaryText = parsed.summary.length > 100
              ? parsed.summary.slice(0, 100) + "…"
              : parsed.summary;
            sum.textContent = summaryText || "Detail";
            det.appendChild(sum);

            // Full detail body
            const detBody = document.createElement("div");
            detBody.className = "tm-env-detail-body";
            if (parsed.lines.length) {
              const ul = document.createElement("ul");
              ul.className = "tm-env-detail-list";
              for (const line of parsed.lines) {
                const li = document.createElement("li");
                li.textContent = line;
                ul.appendChild(li);
              }
              detBody.appendChild(ul);
              // Full recommendation below issues list
              if (parsed.summary && parsed.lines.length) {
                const rec = document.createElement("p");
                rec.className = "tm-env-detail-rec";
                rec.textContent = parsed.summary;
                detBody.appendChild(rec);
              }
            } else {
              detBody.textContent = parsed.summary;
            }
            det.appendChild(detBody);
            inner.appendChild(det);
          }

          card.appendChild(inner);

          // ── Action buttons (Resolve / Dismiss / Assign / Edit) ────────────
          // Gated per-envelope by allow_* flags; only open-ish tasks act.
          if (status === "open" || status === "in_progress") {
            const actions = document.createElement("div");
            actions.className = "tm-env-card-actions";

            const settle = (newStatus: string) => {
              card.classList.remove("tm-env-status-open", "tm-env-status-in_progress");
              card.classList.add(`tm-env-status-${newStatus}`);
              statusDot.className = `tm-env-status-dot tm-env-status-dot--${newStatus}`;
              actions.remove();
            };

            if (b.allow_resolve !== false) {
              const resolveBtn = document.createElement("button");
              resolveBtn.type = "button";
              resolveBtn.className = "tm-env-btn tm-env-btn--resolve";
              resolveBtn.textContent = "Resolve";
              resolveBtn.addEventListener("click", async (e) => {
                e.stopPropagation();
                resolveBtn.disabled = true;
                resolveBtn.textContent = "…";
                try {
                  await apiFetch(`${API_BASE}/chat/tasks/${task.task_id}/resolve`, {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ resolved_by: "chat" }),
                  });
                  settle("resolved");
                } catch {
                  resolveBtn.disabled = false;
                  resolveBtn.textContent = "Resolve";
                }
              });
              actions.appendChild(resolveBtn);
            }

            if (b.allow_dismiss !== false) {
              const dismissBtn = document.createElement("button");
              dismissBtn.type = "button";
              dismissBtn.className = "tm-env-btn tm-env-btn--dismiss";
              dismissBtn.textContent = "Dismiss";
              dismissBtn.addEventListener("click", async (e) => {
                e.stopPropagation();
                dismissBtn.disabled = true;
                dismissBtn.textContent = "…";
                try {
                  await apiFetch(`${API_BASE}/chat/tasks/${task.task_id}/dismiss`, {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ dismissed_by: "chat" }),
                  });
                  settle("dismissed");
                } catch {
                  dismissBtn.disabled = false;
                  dismissBtn.textContent = "Dismiss";
                }
              });
              actions.appendChild(dismissBtn);
            }

            if (b.allow_assign !== false) {
              const assignBtn = document.createElement("button");
              assignBtn.type = "button";
              assignBtn.className = "tm-env-btn tm-env-btn--assign";
              assignBtn.textContent = "Assign";
              assignBtn.addEventListener("click", (e) => {
                e.stopPropagation();
                if (actions.querySelector(".tm-env-assign-input")) return;
                const inp = document.createElement("input");
                inp.type = "text";
                inp.className = "tm-env-assign-input";
                inp.placeholder = "assignee — Enter to save";
                inp.addEventListener("click", (ev) => ev.stopPropagation());
                inp.addEventListener("keydown", async (ev) => {
                  if (ev.key === "Escape") { inp.remove(); return; }
                  if (ev.key !== "Enter") return;
                  const who = inp.value.trim();
                  if (!who) return;
                  inp.disabled = true;
                  try {
                    await apiFetch(`${API_BASE}/chat/tasks/${task.task_id}`, {
                      method: "PATCH",
                      headers: { "Content-Type": "application/json" },
                      body: JSON.stringify({ assigned_to: who, assignee: who }),
                    });
                    inp.remove();
                    assignBtn.textContent = `→ ${who}`;
                    assignBtn.disabled = true;
                  } catch {
                    inp.disabled = false;
                  }
                });
                actions.appendChild(inp);
                inp.focus();
              });
              actions.appendChild(assignBtn);
            }

            if (b.allow_edit !== false) {
              const editBtn = document.createElement("button");
              editBtn.type = "button";
              editBtn.className = "tm-env-btn";
              editBtn.textContent = "Edit";
              editBtn.addEventListener("click", (e) => {
                e.stopPropagation();
                openTasksModal(); // full editor lives in the Tasks modal
              });
              actions.appendChild(editBtn);
            }

            if (actions.childElementCount) card.appendChild(actions);
          }

          list.appendChild(card);
        }
        wrap.appendChild(list);
      }

      // ── Footer ──────────────────────────────────────────────────────────────
      const footer = document.createElement("div");
      footer.className = "tm-env-footer";
      const countNote = document.createElement("span");
      countNote.className = "tm-env-footer-note";
      countNote.textContent = tasks.length >= 50 ? `Showing first 50 · more may exist` : `${tasks.length} task${tasks.length !== 1 ? "s" : ""} total`;
      footer.appendChild(countNote);
      const exportLink = document.createElement("a");
      exportLink.href = "/chat/tasks/export";
      exportLink.className = "tm-env-view-all";
      exportLink.target = "_blank";
      exportLink.rel = "noopener";
      exportLink.textContent = "↓ Export CSV";
      footer.appendChild(exportLink);
      wrap.appendChild(footer);

      bubble.appendChild(wrap);
    } else if (t === "callout") {
      const b = block as { body: string; variant?: string };
      const c = document.createElement("div");
      c.className = "envelope-callout envelope-callout--" + (b.variant || "info");
      c.innerHTML = simpleMarkdownToHtml(b.body || "");
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
      const b = block as { items: unknown[]; collapsed_default?: boolean };
      const items = normalizeFollowupLineList(b.items || [], false);
      if (items.length) {
        const disclosure = document.createElement("details");
        disclosure.className = "envelope-followups-disclosure";
        disclosure.open = false;
        const sum = document.createElement("summary");
        sum.className = "envelope-followups-summary envelope-followups-summary--next-steps";
        sum.textContent = "Next steps (tap to expand)";
        disclosure.appendChild(sum);
        const w = document.createElement("div");
        w.className = "envelope-next-steps";
        const hint = document.createElement("div");
        hint.className = "envelope-next-steps-hint";
        hint.textContent = "Suggested actions — not auto-sent.";
        w.appendChild(hint);
        for (const line of items.slice(0, 8)) {
          const text = line.text.trim();
          if (!text) continue;
          if (line.clickable && opts.onFollowupClick) {
            const btn = document.createElement("button");
            btn.type = "button";
            btn.className = "envelope-step-chip";
            btn.textContent = text;
            btn.addEventListener("click", () => opts.onFollowupClick!(text));
            w.appendChild(btn);
          } else {
            const row = document.createElement("div");
            row.className = "envelope-step-line envelope-step-line--static";
            row.textContent = text;
            w.appendChild(row);
          }
        }
        disclosure.appendChild(w);
        bubble.appendChild(disclosure);
      }
    } else if (t === "suggested_questions") {
      // Suggestions go to #chat-suggestions above composer; don't render inline in bubble
      const b = block as { items: unknown[]; collapsed_default?: boolean };
      const items = normalizeFollowupLineList(b.items || [], true);
      if (items.length && opts.onFollowupClick) {
        const onSelect = opts.onFollowupClick;
        updateChatSuggestions(items, onSelect);
      }
    } else if (t === "pipeline_human_gate") {
      const b = block as { gate?: CredentialingCopilotPayload & { thread_id?: string | null } };
      const g = b.gate;
      if (g && typeof g.run_id === "string" && g.run_id.length > 0) {
        const tid = (g.thread_id || opts.threadId || "").trim() || null;
        bubble.appendChild(renderCredentialingCopilotPanel(g, tid));
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
    } else if (t === "action_chips") {
      const b = block as { chips: Array<{ type: string; label: string; url: string; icon?: string }> };
      if (Array.isArray(b.chips) && b.chips.length > 0) {
        const actionsWrap = document.createElement("div");
        actionsWrap.className = "answer-card-actions";
        for (const action of b.chips) {
          if (action.type === "external_link" && action.url && action.label) {
            const a = document.createElement("a");
            a.href = action.url;
            a.target = "_blank";
            a.rel = "noopener noreferrer";
            a.className = "answer-card-action-chip";
            a.textContent = (action.icon ? action.icon + " " : "") + action.label + " ↗";
            actionsWrap.appendChild(a);
          }
        }
        if (actionsWrap.childNodes.length > 0) pendingActionChips.push(actionsWrap);
      }
    } else if (t === "credentialing_card") {
      const b = block as {
        npi?: string; provider_name?: string; org?: string; status?: string;
        flags?: Array<{ text: string; severity?: string }>;
        action_url?: string; org_summary?: boolean;
      };
      const card = document.createElement("div");
      card.className = "cred-card" + (b.org_summary ? " cred-card--org-summary" : "");

      const header = document.createElement("div");
      header.className = "cred-card-header";
      const nameEl = document.createElement("div");
      nameEl.className = "cred-card-name";
      const displayName = b.org_summary
        ? (b.provider_name || (b.org ?? "").replace(/_/g, " ").replace(/\b\w/g, c => c.toUpperCase()) || "Organization")
        : (b.provider_name ?? "Provider");
      nameEl.textContent = displayName;
      const statusKey = (b.status ?? "unknown").toLowerCase();
      const statusLabel: Record<string, string> = {
        enrolled: "Enrolled", pending: "Pending", flagged: "Flagged",
        not_enrolled: "Not Enrolled", unknown: "Unknown",
      };
      const statusEl = document.createElement("span");
      statusEl.className = `cred-card-status cred-card-status--${statusKey}`;
      statusEl.textContent = statusLabel[statusKey] ?? b.status ?? "Unknown";
      header.appendChild(nameEl);
      header.appendChild(statusEl);
      card.appendChild(header);

      if (b.npi || b.org) {
        const meta = document.createElement("div");
        meta.className = "cred-card-meta";
        if (b.npi) {
          const npiEl = document.createElement("span");
          npiEl.className = "cred-card-npi";
          npiEl.textContent = "NPI " + b.npi;
          meta.appendChild(npiEl);
        }
        if (b.org) {
          const orgEl = document.createElement("span");
          orgEl.className = "cred-card-org";
          orgEl.textContent = b.org;
          meta.appendChild(orgEl);
        }
        card.appendChild(meta);
      }

      if (Array.isArray(b.flags) && b.flags.length > 0) {
        const flagList = document.createElement("ul");
        flagList.className = "cred-card-flags";
        b.flags.forEach((f) => {
          const li = document.createElement("li");
          li.className = `cred-card-flag cred-card-flag--${f.severity ?? "info"}`;
          const dot = document.createElement("span");
          dot.className = "cred-flag-dot";
          dot.setAttribute("aria-hidden", "true");
          li.appendChild(dot);
          li.appendChild(document.createTextNode(f.text));
          flagList.appendChild(li);
        });
        card.appendChild(flagList);
      }

      if (b.action_url) {
        const link = document.createElement("a");
        link.href = b.action_url;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        link.className = "cred-card-action";
        link.textContent = "View full report ↗";
        card.appendChild(link);
      }

      bubble.appendChild(card);
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
  pendingActionChips.forEach((el) => msg.appendChild(el));
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
  /** Must stay in sync with server thread after upload + each /chat response.
   * Each assignment also mirrors to window.__mobiusChatThreadId so module-level
   * code (email-thread feedback button) can read it without scope crossing. */
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
    const pipelineWrap = document.getElementById("rosterReceiptPipelineWrap");
    const pipelineSummaryEl = document.getElementById("rosterReceiptPipelineSummary");
    const pipelineListEl = document.getElementById("rosterReceiptPipeline");
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
      const isRAG = (data as any).file_purpose === "instant_rag" || (data as any).verification_tier === "instant";
      // 2026-04-18 copy revision: the earlier wording ("Document ingested
      // for RAG", "chunked, embedded", "chunk(s) indexed. Verification
      // tier: instant (7-day TTL)") leaked developer jargon into the
      // upload-success receipt that every user sees after a successful
      // upload. Rewritten to plain English; filename + a kept-for-N-days
      // note is enough signal for the user.
      headline.textContent = isRAG ? "Document ready" : "Upload complete";
      sub.textContent = isRAG
        ? "Your document is ready to search in this chat."
        : "Your file was saved to this chat.";
      checksEl.replaceChildren();
      const li = document.createElement("li");
      const t = document.createElement("span");
      t.className = "roster-receipt__check-title";
      t.textContent = "Summary";
      const d = document.createElement("span");
      d.className = "roster-receipt__check-detail";
      d.textContent = isRAG
        ? `${data.filename ?? "File"} — ready to search. Kept for 7 days.`
        : `${data.filename ?? "File"} — ${data.row_count ?? 0} row(s) for ${data.org_name ?? ""}. Billing NPI ${data.default_billing_npi || data.org_id || "—"}.`;
      li.appendChild(t);
      li.appendChild(d);
      checksEl.appendChild(li);
      alertsEl.replaceChildren();
      alertsEl.setAttribute("hidden", "");
      nextEl.textContent = isRAG
        ? "Ask a question about this document — it's ready now."
        : "Press Send to run reconciliation, or wait if you turned on automatic send after upload.";
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
    const _isRAG = (data as any).file_purpose === "instant_rag" || (data as any).verification_tier === "instant";
    addMeta("File", (data.filename ?? "").trim());
    if (_isRAG) {
      // 2026-04-18: replaced developer-facing rows ("Chunks indexed",
      // "Verification tier", "Envelope ID", raw "live" status) with
      // one user-meaningful row. The internal fields are still useful
      // for support — log them to the debug console for ops but don't
      // display in the receipt.
      addMeta("Status", "Ready to search");
      console.debug("[upload-receipt] instant-rag meta:", {
        chunks_count: (data as any).chunks_count ?? data.row_count ?? 0,
        verification_tier: (data as any).verification_tier ?? "instant",
        envelope_id: (data as any).envelope_id,
        document_id: (data as any).document_id,
      });
    } else {
      if (data.row_count_cleansed != null) addMeta("Rows after cleanup", String(data.row_count_cleansed));
      if (data.row_count_resolved != null) addMeta("Rows checked in NPI registry", String(data.row_count_resolved));
      addMeta("Billing NPI", (data.default_billing_npi || data.org_id || "").trim());
      addMeta("Matched organization (registry)", (data.matched_organization_name ?? "").trim());
      if ((data.matched_practice_address ?? "").trim())
        addMeta("Practice address on file", (data.matched_practice_address ?? "").trim());
      addMeta("Process status", (data.process_status ?? "").trim());
    }
    addMeta("Upload ID", (data.upload_id ?? "").trim());
    addMeta("Chat thread ID", (data.thread_id ?? "").trim());
    const rs = data.resolution_summary;
    if (rs && typeof rs === "object") {
      const parts = Object.entries(rs)
        .filter(([, v]) => typeof v === "number" && v > 0)
        .map(([k, v]) => `${k}: ${v}`);
      if (parts.length) addMeta("NPI match breakdown", parts.join(", "));
    }

    const pipe = data.pipeline_progress;
    const stages = pipe?.stages;
    if (
      pipelineWrap &&
      pipelineSummaryEl &&
      pipelineListEl &&
      Array.isArray(stages) &&
      stages.length > 0
    ) {
      pipelineWrap.removeAttribute("hidden");
      pipelineSummaryEl.textContent = (pipe.summary ?? "").trim() || "Pipeline status";
      pipelineListEl.replaceChildren();
      const cur = (pipe.current_stage_id ?? "").trim();
      for (const s of stages) {
        const li = document.createElement("li");
        const isDone = Boolean(s.done);
        li.className = isDone
          ? "roster-receipt__pipeline--done"
          : "roster-receipt__pipeline--pending";
        if (!isDone && cur && s.id === cur) {
          li.classList.add("roster-receipt__pipeline--current");
        }
        const lab = document.createElement("span");
        lab.className = "roster-receipt__pipeline-stage";
        lab.textContent = s.label || s.id;
        const det = document.createElement("span");
        det.className = "roster-receipt__pipeline-detail";
        det.textContent = s.detail || "";
        li.appendChild(lab);
        li.appendChild(det);
        pipelineListEl.appendChild(li);
      }
    } else {
      pipelineWrap?.setAttribute("hidden", "");
      pipelineSummaryEl?.replaceChildren();
      pipelineListEl?.replaceChildren();
    }

    // Reconciliation UI deep-link
    const rcWrap = document.getElementById("rosterReceiptReconciliationWrap");
    const rcLink = document.getElementById("rosterReceiptReconciliationLink") as HTMLAnchorElement | null;
    const rcUrlData = (data as RosterUploadResponse).reconciliation_ui_url;
    if (rcWrap && rcLink && rcUrlData) {
      rcLink.href = rcUrlData;
      rcWrap.removeAttribute("hidden");
    } else {
      rcWrap?.setAttribute("hidden", "");
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

  // 2026-05-06: full mobius-user wire-up per Mobius-user/SPEC.md.
  //
  //   1. Bootstrap fetch /api/v1/public-config → google_client_id (proxied
  //      by chat to mobius-user). Without this, AuthModal renders the
  //      Google button as a placeholder that 401s on submission.
  //   2. createAuthService + createAuthModal as before, now with
  //      googleClientId so OAuth actually works.
  //   3. createPreferencesModal — first-run onboarding + post-onboarding
  //      edits. Same apiBase as auth (proxied to mobius-user).
  //   4. window.onOpenPreferences bridge — AuthModal's "Set up
  //      preferences" button (welcome panel) and "Preferences" link
  //      (account view) call into this; host wires the destination.
  //
  // googleClientId arrives async, but createAuthModal accepts it at
  // construction time only. We start the modal with showOAuth=false,
  // then re-create it once the config lands. The race window is the
  // few hundred ms between page load and the public-config response —
  // sidebar user button is hidden during that window so no click can
  // reach the wrong modal.
  const authApiBase = `${API_BASE.replace(/\/$/, "")}/api/v1`;
  const auth = createAuthService({ apiBase: authApiBase, storage: localStorageAdapter });
  _authRef = auth; // expose to apiFetch / _getWhoami (defined at module scope)
  void _maybeShowGreeting(); // auth is ready — fetch identity with token

  // Auth gate — blocks the UI until sign-in is confirmed.
  // The gate element starts visible (class auth-gate--visible) in HTML
  // so there is zero flash of chat content for unauthenticated users.
  const authGateEl = document.getElementById("authGate");
  const appLayoutEl = document.querySelector(".app-layout") as HTMLElement | null;
  function _setAuthGate(visible: boolean): void {
    if (!authGateEl) return;
    authGateEl.classList.toggle("auth-gate--visible", visible);
    // Make the gate itself inert when hidden (tabs can't land on it).
    (authGateEl as HTMLElement & { inert?: boolean }).inert = !visible;
    // Make the chat layout inert while the gate is up so keyboard users
    // can't bypass the sign-in wall by tabbing into the background.
    if (appLayoutEl) {
      (appLayoutEl as HTMLElement & { inert?: boolean }).inert = visible;
    }
  }
  // Wire the "Sign in" button on the gate to open the auth modal.
  // (modal variable declared below — gate btn listener deferred until after modal is built.)

  // Style injection happens once, before either modal is built — both
  // share the same overlay/panel CSS classes.
  const _authStyleEl = document.createElement("style");
  _authStyleEl.textContent = AUTH_STYLES + (PREFERENCES_MODAL_STYLES || "");
  document.head.appendChild(_authStyleEl);

  // Mutable handle so the public-config fetch can swap in a Google-enabled
  // modal without breaking call sites that hold a stale reference.
  let modal = createAuthModal({ auth, showOAuth: false });
  document.body.appendChild(modal.el);
  // Gate "Sign in" button opens the auth modal in login mode.
  document.getElementById("authGateBtn")?.addEventListener("click", () => {
    modal.open("login");
  });

  // Alpha release banner + release notes modal.
  const alphaBanner = document.getElementById("alphaBanner");
  const alphaModal  = document.getElementById("alphaModal");

  const openAlphaModal  = (): void => { if (alphaModal) alphaModal.hidden = false; };
  const closeAlphaModal = (): void => { if (alphaModal) alphaModal.hidden = true; };

  // Dismiss banner permanently.
  if (alphaBanner) {
    if (localStorage.getItem("alpha_banner_dismissed") === "1") {
      alphaBanner.hidden = true;
    } else {
      document.getElementById("alphaBannerDismiss")?.addEventListener("click", () => {
        alphaBanner.hidden = true;
        localStorage.setItem("alpha_banner_dismissed", "1");
      });
    }
  }

  // "🚀 Alpha" tag + "What's new ↗" link both open the modal.
  document.getElementById("alphaBannerTag")?.addEventListener("click", openAlphaModal);
  document.getElementById("alphaBannerTagLink")?.addEventListener("click", openAlphaModal);

  // Close modal via ✕ button or clicking the backdrop.
  document.getElementById("alphaModalClose")?.addEventListener("click", closeAlphaModal);
  alphaModal?.addEventListener("click", (e) => {
    if (e.target === alphaModal) closeAlphaModal();
  });

  // Escape key closes the modal.
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && alphaModal && !alphaModal.hidden) closeAlphaModal();
  });

  // PreferencesModal — instant; doesn't depend on public-config.
  // Note: createPreferencesModal returns { open, close } only — it
  // manages its own DOM mount lazily when open() is first called.
  // Don't try to appendChild a (.el) here — that property doesn't
  // exist on this modal (vs. createAuthModal which DOES expose .el).
  // 2026-05-06: register an onSave callback so the chat-side
  // ``cachedUserProfileNested`` refreshes immediately after a
  // preferences save. The PUT response on mobius-user includes the
  // fresh profile (with re-rendered rendered_prompt) — re-fetch /me
  // here so the next chat POST sends the updated shape.
  const prefsModal = createPreferencesModal(authApiBase, auth, {
    onSave: () => {
      void _fetchNestedUserProfile();
    },
  });
  (window as unknown as { onOpenPreferences?: () => void }).onOpenPreferences = () => {
    void prefsModal.open();
  };
  // Onboarding nudge — shown when user is signed in but hasn't completed setup.
  // Clicking it opens PreferencesModal so they can finish and unlock personalization.
  document.getElementById("onboardingNudge")?.addEventListener("click", (e) => {
    e.stopPropagation();
    void prefsModal.open();
  });

  // Public-config bootstrap. Best-effort: if it fails, AuthModal stays
  // in email/password-only mode and the user can still sign up.
  fetch(`${authApiBase}/public-config`, { method: "GET" })
    .then((r) => (r.ok ? r.json() : null))
    .then((cfg) => {
      const gid = (cfg && cfg.google_client_id) ? String(cfg.google_client_id).trim() : "";
      if (!gid) return;
      // Re-create modal with Google enabled. Replace the DOM node in
      // place so any cached references stay valid for the next click.
      const oldEl = modal.el;
      modal = createAuthModal({ auth, showOAuth: true, googleClientId: gid });
      if (oldEl.parentNode) oldEl.parentNode.replaceChild(modal.el, oldEl);
      else document.body.appendChild(modal.el);
    })
    .catch((e) => {
      console.warn("[auth] public-config fetch failed; Google sign-in disabled:", e);
    });

  function updateSidebarUser(user: { greeting_name?: string; display_name?: string; first_name?: string; preferred_name?: string; email?: string } | null): void {
    if (!sidebarUserName) return;
    const name =
      user?.greeting_name ||
      user?.preferred_name ||
      user?.first_name ||
      user?.display_name ||
      (user?.email ? user.email.split("@")[0] : null) ||
      "Guest";
    sidebarUserName.textContent = name;
  }

  function _syncOnboardingNudge(isOnboarded: boolean): void {
    const nudge = document.getElementById("onboardingNudge");
    if (nudge) nudge.hidden = isOnboarded;
  }

  // ── Training mode (v2) ────────────────────────────────────────────────
  // Single-init guard: once training mode has been displayed this page load,
  // don't re-init on subsequent auth callbacks. Force=true bypasses this and
  // the session-skip flag (used by /training and /welcome cheat codes).
  let _tmShownThisSession = false;

  function _showTrainingMode(name: string, arrival: string, force = false): void {
    const wrap = document.getElementById("trainingMode");
    if (!wrap) return;
    if (!force) {
      if (_tmShownThisSession) return;
      if (sessionStorage.getItem("_tm_skip") === "1") return;
    }
    _tmShownThisSession = true;

    const esc = (s: string) => String(s).replace(/[&<>"']/g, c =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c] as string
    );

    const SCENARIOS = [
      { k: "transport", label: "🚌 A patient can't get to appointments", act: "patient_outreach",
        q: "Does Medicaid cover rides to behavioral-health appointments?",
        tones: [
          ["professional", "Yes. Florida Medicaid covers non-emergency medical transportation to behavioral-health appointments. Rides are arranged through the plan's contracted transportation broker, and most plans require one to three days' advance notice. I can retrieve this patient's plan and its booking procedure."],
          ["friendly", "Yes, and this one's easy — Medicaid rides are free for covered visits 🚌 You just book through the plan's ride line a couple of days ahead. Want me to grab the booking number for this patient's plan?"],
          ["concise", "Covered (NEMT). Plan broker, 1–3d notice. Number?"],
        ],
        depths: [
          ["beginner", "Yes, covered ✅ Every FL Medicaid plan includes free rides to covered appointments — it's called NEMT. To set one up, call the plan's ride line ideally 3 days ahead. Want me to pull this patient's plan, get the booking number, and walk you through it?"],
          ["regular", "Yes — NEMT is covered ✅ Book through the plan's broker ~1–3 days ahead. Want the plan-specific booking line?"],
          ["expert", "Covered ✅ NEMT via plan broker; 1–3d notice; standing-order option for recurring visits. Pull member plan → broker line?"],
        ],
      },
      { k: "pcp", label: "🩺 A patient needs a PCP assigned or switched", act: "check_in_patients",
        q: "How does a member change their PCP?",
        tones: [
          ["professional", "Members may change their primary care provider through the plan's member portal or by calling member services. Changes take effect on the first of the following month; urgent assignments for unassigned members can be expedited."],
          ["friendly", "Happens all the time — quick fix! The member calls member services or uses the portal, picks the new PCP, and it kicks in on the 1st of next month. No PCP at all? The plan can rush it."],
          ["concise", "Portal or member services. Effective 1st next month. Expedite if unassigned. [src]"],
        ],
        depths: [
          ["beginner", "Here's the whole path ✅ The member calls the plan's member-services line (or uses the portal) and requests the change; it usually takes effect the 1st of the following month. If urgent — like no PCP at all — plans can expedite. Want me to look up the plan's number?"],
          ["regular", "Plan portal or member services; effective 1st of next month; expedite path for unassigned ✅ Want the plan's number?"],
          ["expert", "Portal/MS line; eff. 1st next mo.; expedite path for unassigned members. Registry has the MS number. Pull it?"],
        ],
      },
      { k: "denial", label: "❌ A claim came back denied — now what?", act: "rework_denials",
        q: "Why was this claim denied and how do I fix it?",
        tones: [
          ["professional", "This claim was denied with CARC 197: prior authorization not on file. The denial is typically recoverable through a retroactive authorization request, where the payer permits it, or a formal appeal supported by medical-necessity documentation. I can prepare the appeal letter."],
          ["friendly", "Okay, decoded it — the payer says nobody got prior auth first (code 197). Don't worry, this one's usually saveable: retro-auth or appeal. I can draft the letter with you 💪"],
          ["concise", "CARC 197 — no PA. Retro-auth or appeal. Draft?"],
        ],
        depths: [
          ["beginner", "The code (CARC 197) means the payer didn't find a prior authorization ✅ Two ways forward — ask for a retroactive auth (some payers allow it) or appeal with documentation. I can check this payer's exact rules and draft the appeal with you. Start there?"],
          ["regular", "CARC 197 — missing prior auth ✅ This payer allows retro-auth requests; otherwise appeal. Draft the letter?"],
          ["expert", "197 · no PA on file. Retro window per payer playbook; else appeal w/ med-nec. appeals_assemble_letter ready."],
        ],
      },
      { k: "newprov", label: "🪪 A new clinician needs to start billing", act: "credentialing",
        q: "Is our new clinician enrolled with Medicaid yet?",
        tones: [
          ["professional", "The clinician does not yet appear on the state's Provider Master List; enrollment remains pending. Claims submitted before the effective date will be denied. I will monitor the roster and notify you when the status changes."],
          ["friendly", "Checked — they're not on the state roster quite yet, so hold their claims for now (billing early = automatic denials). I'll keep an eye on it and ping you the day they flip to payable!"],
          ["concise", "Not on PML. Hold claims. Watching; will notify on flip."],
        ],
        depths: [
          ["beginner", "Not enrolled yet ✅ Until the state lists them (the PML), any claim under their NPI will deny. Hold their claims — I'll watch the roster and tell you the day they're payable. Want me to show you their full credentialing card?"],
          ["regular", "Pending — not on PML yet ✅ Hold claims; I'll notify on the flip. Want the credentialing card?"],
          ["expert", "PML: absent. NPPES: active. Hold claims; watcher set on status flip. check_provider_credentialing for full panel view."],
        ],
      },
      { k: "coverage", label: "📄 Not sure what a payer actually covers", act: "submit_claims",
        q: "Does this payer cover this service via telehealth?",
        tones: [
          ["professional", "Yes. This payer covers the service when delivered via telehealth, subject to the telehealth modifier requirement, and reimburses at parity with in-person delivery. Source: payer telehealth policy, page 12."],
          ["friendly", "Good news — covered over telehealth! One gotcha: the claim needs the telehealth modifier or it'll bounce. Want me to pin the policy page so your team has the receipt?"],
          ["concise", "Covered via telehealth. Modifier req'd. Parity. [src p.12]"],
        ],
        depths: [
          ["beginner", "Yes, it's covered via telehealth ✅ One thing to get right: the claim needs a telehealth modifier or it may deny. Here's the policy page as your receipt. Want me to note which of your common services have telehealth quirks?"],
          ["regular", "Covered via telehealth, modifier required ✅ Policy page attached. Want the full telehealth rules for this payer?"],
          ["expert", "Covered; parity; GT/95 modifier req. Source pinned. Cross-payer telehealth matrix available on ask."],
        ],
      },
      { k: "rates", label: "📈 Are we getting paid fairly?", act: "strategy",
        q: "What's the market rate for this service code?",
        tones: [
          ["professional", "Your realized rate for this code is at the 34th percentile of comparable providers. Closing the gap to the market median would represent a material per-unit increase. I can quantify the annualized difference and identify the peer group used for comparison."],
          ["friendly", "Honest answer? You're leaving money on the table here — 34th percentile for this code. The median would mean real dollars per visit. Want the yearly number? It tends to get people's attention 😉"],
          ["concise", "P34 vs peer P50. Gap material. Annualized number?"],
        ],
        depths: [
          ["beginner", "You're being paid below market on this code ✅ Comparable providers get more per unit — you're at the 34th percentile, from real claims data. Want me to show the annual dollar gap and which peer group I used?"],
          ["regular", "P34 vs peers on this code ✅ Median would mean more per unit. Annualized gap + peer group on request."],
          ["expert", "P34 realized vs peer P50; claims-level basis; get_org_rate_gap for annualized + get_rate_trends for trajectory."],
        ],
      },
    ];

    const AUTONOMY = [
      { k: "automatic",    b: "Just handle it",       s: "I'll act on routine things and tell you after." },
      { k: "confirm_first", b: "Show me before you act", s: "I'll line it up, you press go." },
      { k: "manual",       b: "Walk me through it",   s: "We do it together, step by step." },
    ];

    const HESITATIONS = [
      { k: "wrong",   b: "It'll get things wrong",   emo: "😬", fearQ: "“What if it’s wrong — it’s my name on this.”",    fearA: "No source, no claim. Every answer shows receipts — click any citation. Unsure? I say so out loud." },
      { k: "phi",     b: "Patient data safety",       emo: "🔒", fearQ: "“Is patient information safe in here?”",                          fearA: "Uploads get scanned for PHI automatically and stay private by default. Nothing shares itself." },
      { k: "complex", b: "Too complicated for me",    emo: "🤯", fearQ: "“This looks complicated…”",                                  fearA: "You just did the hardest part — clicking buttons. Say “show me” anytime and I’ll walk you through it." },
      { k: "none",    b: "Honestly? Nothing 😎", emo: "😎", fearQ: "“Impress me.”",                                             fearA: "Open the platform schematic — 30 modules, honest live/planned status on every one. Then ask me anything on it." },
    ];

    const PERSONAS: Record<string, { hook: string; tryits: string[] }> = {
      patient_outreach: { hook: "Ask freely. The compliance worrying is my job.",            tryits: ["Upload this document and tell me what’s in it", "What’s the prior auth rule for outpatient?", "Who can see my uploads?"] },
      check_in_patients: { hook: "The coworker who always knows — and never sighs.",  tryits: ["What does code H0019 mean?", "Show me how to change my answer style", "Where did my last conversation go?"] },
      rework_denials:  { hook: "Denials, codes, timely filing — answered with receipts.", tryits: ["What’s the timely filing rule for Sunshine?", "Is Dr. Chen enrolled with Medicaid?", "Remind me to rework that H0019 denial"] },
      credentialing:   { hook: "“Is this provider payable?” — one question, whole answer.", tryits: ["How is our panel doing?", "NPPES errors for Acme Health?", "Show me the credentialing report"] },
      submit_claims:   { hook: "Denials, codes, timely filing — answered with receipts.", tryits: ["Does this payer cover telehealth?", "What’s the timely filing rule here?", "Draft an appeal for CARC 197"] },
      strategy:        { hook: "Real claims data. Real benchmarks. Zero slideware.",          tryits: ["How big is the FL Medicaid BH market?", "Benchmark my organization", "Where are we underpaid?"] },
      default:         { hook: "Ask me anything about payers, policies, or your documents.", tryits: ["What can Mobius do for me?", "What does code H0019 mean?", "Show me around"] },
    };

    // Mutable state for the current flow
    let step = 0;
    let acts: string[] = [];
    let toneKey: string | null = null;
    let autoKey: string | null = null;
    let expLevel: string | null = null;
    let hesList: string[] = [];

    function _writePrefs(body: Record<string, unknown>): void {
      void apiFetch(`${authApiBase}/auth/preferences`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ...body, source: "training_mode" }),
      }).catch(() => {});
    }

    function _sendTrainingEvent(
      eventType: string,
      source?: string,
      text?: string,
    ): void {
      void apiFetch(`${API_BASE}/chat/training-event`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ event_type: eventType, source, text }),
      }).catch(() => {});
    }

    function _finishOnboarding(): void {
      void apiFetch(`${authApiBase}/auth/onboarding`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({}),
      }).then(() => {
        // Refresh the profile cache so the graduation question goes out with
        // the freshly-rendered_prompt from the onboarding PUT above — not the
        // stale profile from session boot.
        void _fetchNestedUserProfile();
      }).catch(() => {});
      _syncOnboardingNudge(true);
    }

    function _dismiss(permanent: boolean): void {
      wrap.hidden = true;
      wrap.innerHTML = "";
      if (permanent) {
        _sendTrainingEvent("training_dismissed");
        _finishOnboarding();
      } else {
        _sendTrainingEvent("training_skipped");
        sessionStorage.setItem("_tm_skip", "1");
      }
    }

    function prog(n: number): string {
      return `<div class="tm-prog">${[0,1,2,3,4].map(i =>
        `<span class="tm-prog__dot${i < n ? " tm-prog__dot--on" : ""}"></span>`
      ).join("")}</div>`;
    }

    function mainScenario() { return SCENARIOS.find(s => s.k === acts[0]) ?? SCENARIOS[2]; }

    function _bindX(): void { wrap.querySelector(".tm-x")?.addEventListener("click", () => _dismiss(true)); }

    function _render(): void {
      if (step === 0) {
        wrap.innerHTML = `<div class="tm-card">
          <button class="tm-x" aria-label="Don't show again">&times;</button>
          ${prog(0)}
          <h2 class="tm-h2">Hey ${esc(name)} 👋 I'm Mobius.</h2>
          <p class="tm-sub">${arrival === "invited" ? "Your org set you up — zero forms." : "Welcome in."} Give me <strong>90 seconds</strong>: you click, I learn how you like to work. Retrain me anytime.</p>
          <div class="tm-row">
            <button class="tm-primary" data-go>Let’s go →</button>
            <button class="tm-ghost" data-skip>skip — I’ll explore on my own</button>
          </div></div>`;
        _bindX();
        wrap.querySelector("[data-go]")?.addEventListener("click", () => { step = 1; _render(); });
        wrap.querySelector("[data-skip]")?.addEventListener("click", () => _dismiss(false));
      } else if (step === 1) {
        wrap.innerHTML = `<div class="tm-card">
          <button class="tm-x" aria-label="Don't show again">&times;</button>
          ${prog(1)}
          <h2 class="tm-h2">What walked through your door this week?</h2>
          <p class="tm-sub">Pick the situations you actually deal with — first pick becomes the thread we use to tune everything.</p>
          <div class="tm-grid">${SCENARIOS.map(a => `<button class="tm-act${acts.includes(a.k) ? " tm-act--on" : ""}" data-k="${esc(a.k)}">${esc(a.label)}${acts[0] === a.k ? '<span class="tm-act__star">★ your main thing</span>' : ""}</button>`).join("")}</div>
          <div class="tm-row"><button class="tm-primary" data-next${acts.length ? "" : " disabled"}>That’s me →</button></div></div>`;
        _bindX();
        wrap.querySelectorAll(".tm-act").forEach(b => b.addEventListener("click", () => {
          const k = (b as HTMLElement).dataset.k as string;
          acts = acts.includes(k) ? acts.filter(x => x !== k) : [...acts, k];
          _render();
        }));
        wrap.querySelector("[data-next]")?.addEventListener("click", () => {
          if (!acts.length) return;
          _writePrefs({ activities: acts.map(k => SCENARIOS.find(s => s.k === k)?.act ?? k) });
          step = 2; _render();
        });
      } else if (step === 2) {
        const sc = mainScenario();
        wrap.innerHTML = `<div class="tm-card">
          <button class="tm-x" aria-label="Don't show again">&times;</button>
          ${prog(2)}
          <h2 class="tm-h2">Your situation. Three ways to answer it.</h2>
          <p class="tm-sub">You asked: <strong>“${esc(sc.q)}”</strong> — no labels, no right answer. Tap the reply you’d rather read:</p>
          ${sc.tones.map(t => `<button class="tm-tone" data-k="${esc(t[0])}"><p>${esc(t[1])}</p></button>`).join("")}</div>`;
        _bindX();
        wrap.querySelectorAll(".tm-tone").forEach(b => b.addEventListener("click", () => {
          toneKey = (b as HTMLElement).dataset.k as string;
          _writePrefs({ tone: toneKey });
          step = 3; _render();
        }));
      } else if (step === 3 && !autoKey) {
        wrap.innerHTML = `<div class="tm-card">
          <button class="tm-x" aria-label="Don't show again">&times;</button>
          ${prog(3)}
          <h2 class="tm-h2">A denial needs reworking.</h2>
          <p class="tm-sub">Real scenario — this is <strong>sensitive</strong> territory (billing). I found the fix. What should I do?</p>
          ${AUTONOMY.map(a => `<button class="tm-bigchip" data-k="${esc(a.k)}"><strong>${esc(a.b)}</strong><span>${esc(a.s)}</span></button>`).join("")}</div>`;
        _bindX();
        wrap.querySelectorAll(".tm-bigchip").forEach(b => b.addEventListener("click", () => {
          autoKey = (b as HTMLElement).dataset.k as string;
          _writePrefs({ autonomy_sensitive: autoKey });
          _render();
        }));
      } else if (step === 3 && autoKey) {
        const sc = mainScenario();
        const autoLabel = AUTONOMY.find(a => a.k === autoKey)?.b ?? "show me first";
        wrap.innerHTML = `<div class="tm-card">
          <button class="tm-x" aria-label="Don't show again">&times;</button>
          ${prog(3)}
          <h2 class="tm-h2">Deal: “${esc(autoLabel)}” for sensitive work.</h2>
          <p class="tm-sub">One more — <strong>how much should I explain?</strong> Same question, three depths. Tap what you’d actually want:</p>
          ${sc.depths.map(d => `<button class="tm-tone" data-k="${esc(d[0])}"><p>${esc(d[1])}</p></button>`).join("")}</div>`;
        _bindX();
        wrap.querySelectorAll(".tm-tone").forEach(b => b.addEventListener("click", () => {
          expLevel = (b as HTMLElement).dataset.k as string;
          _writePrefs({ experience_level: expLevel });
          step = 4; _render();
        }));
      } else if (step === 4) {
        wrap.innerHTML = `<div class="tm-card">
          <button class="tm-x" aria-label="Don't show again">&times;</button>
          ${prog(4)}
          <h2 class="tm-h2">Last one. Anything make you hesitant?</h2>
          <p class="tm-sub">Pick all that apply — honest answers get honest features. (Optional.)</p>
          ${HESITATIONS.map(h => `<button class="tm-bigchip${hesList.includes(h.k) ? " tm-bigchip--on" : ""}" data-k="${esc(h.k)}"><strong>${esc(h.b)}${hesList.includes(h.k) ? " ✓" : ""}</strong></button>`).join("")}
          <div class="tm-row">
            <button class="tm-primary" data-done>${hesList.length ? "That’s them →" : "Nothing, honestly →"}</button>
            <button class="tm-ghost" data-skiph>skip this one</button>
          </div></div>`;
        _bindX();
        wrap.querySelectorAll(".tm-bigchip").forEach(b => b.addEventListener("click", () => {
          const k = (b as HTMLElement).dataset.k as string;
          hesList = hesList.includes(k) ? hesList.filter(x => x !== k) : [...hesList, k];
          _render();
        }));
        const advance = () => {
          if (hesList.length) _writePrefs({ hesitations: hesList });
          _sendTrainingEvent("training_completed");
          _finishOnboarding();
          step = 5; _render();
        };
        wrap.querySelector("[data-done]")?.addEventListener("click", advance);
        wrap.querySelector("[data-skiph]")?.addEventListener("click", advance);
      } else {
        _renderGraduation();
      }
    }

    function _renderGraduation(): void {
      const sc = mainScenario();
      const actKey = SCENARIOS.find(s => s.k === acts[0])?.act ?? "default";
      const pa = PERSONAS[actKey] ?? PERSONAS["default"];
      const hes = HESITATIONS.find(h => h.k === hesList[0]) ?? HESITATIONS[0];
      const autoLabel = AUTONOMY.find(a => a.k === autoKey)?.b;
      const learned = [
        acts.length ? `★ ${esc(acts[0])}${acts.length > 1 ? ` +${acts.length - 1}` : ""}` : "explorer",
        toneKey ? `🗣 ${esc(toneKey)}` : "🗣 professional",
        autoLabel ? `🎚 ${esc(autoLabel)}` : "🎚 show me first",
        expLevel ? `🧠 ${esc(expLevel)}` : null,
        hesList.length ? `😬 ${esc(HESITATIONS.find(h => h.k === hesList[0])?.b ?? hesList[0])}${hesList.length > 1 ? ` +${hesList.length - 1}` : ""}` : null,
      ].filter((x): x is string => x !== null);
      const tryIts = acts.length
        ? acts.slice(0, 3).map(k => SCENARIOS.find(s => s.k === k)?.q ?? "").filter(Boolean)
        : pa.tryits.slice(0, 3);

      wrap.innerHTML = `<div class="tm-card tm-card--graduation">
        <button class="tm-x" aria-label="Close">&times;</button>
        ${prog(5)}
        <h2 class="tm-h2">Trained. Here’s your Mobius, ${esc(name)} 🎓</h2>
        <div class="tm-learned">${learned.map(t => `<span>${t}</span>`).join("")}</div>
        <p class="tm-edit-note">Edit any of it in Preferences, or retrain by sending <code>/training</code>.</p>
        <p class="tm-hook">${esc(pa.hook)}</p>
        <div class="tm-flip" data-flipped="false">
          <div class="tm-flip-inner">
            <div class="tm-face tm-face--q">${esc(hes.emo)} ${esc(hes.fearQ)}<span class="tm-tap">tap to flip</span></div>
            <div class="tm-face tm-face--a">✅ ${esc(hes.fearA)}</div>
          </div>
        </div>
        <div class="tm-tryits">${tryIts.map(q => `<button class="tm-try" data-q="${esc(q)}">${esc(q)}</button>`).join("")}</div>
        <div class="tm-composer">
          <input id="tmInput" class="tm-composer-input" placeholder="ask what you came for — or tap a starter above">
          <button class="tm-composer-send" id="tmSend">➤</button>
        </div></div>`;

      wrap.querySelector(".tm-x")?.addEventListener("click", () => { wrap.hidden = true; wrap.innerHTML = ""; });
      wrap.querySelector(".tm-flip")?.addEventListener("click", e => {
        const f = e.currentTarget as HTMLElement;
        f.dataset.flipped = f.dataset.flipped === "true" ? "false" : "true";
      });
      let _fromChip = false;
      wrap.querySelectorAll(".tm-try").forEach(b => b.addEventListener("click", () => {
        const ci = document.getElementById("tmInput") as HTMLInputElement | null;
        if (ci) { ci.value = (b as HTMLElement).dataset.q as string; _fromChip = true; }
      }));
      (document.getElementById("tmInput") as HTMLInputElement | null)
        ?.addEventListener("input", () => { _fromChip = false; });
      const fire = () => {
        const ci = document.getElementById("tmInput") as HTMLInputElement | null;
        const v = (ci?.value ?? "").trim();
        if (!v) return;
        const src = _fromChip ? "chip" : "typed";
        wrap.hidden = true; wrap.innerHTML = "";
        _sendTrainingEvent("graduation_question_fired", src, v);
        if (src === "typed") {
          // Typed first-question is fresh intent — route to PA's gap writer.
          const _gradAreaTags: Record<string, string> = {
            rework_denials: "appeals", credentialing: "credentialing",
          };
          const _gradTag = _gradAreaTags[actKey] ?? "rag";
          void apiFetch(`${API_BASE}/chat/product-feedback`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ verbatim: v, category: "feature_request", trigger: "graduation", area_tags: [_gradTag] }),
          }).catch(() => {});
        }
        sendMessage(v);
      };
      document.getElementById("tmSend")?.addEventListener("click", fire);
      (document.getElementById("tmInput") as HTMLInputElement | null)?.addEventListener("keydown", (e: KeyboardEvent) => {
        if (e.key === "Enter") fire();
      });
    }

    wrap.hidden = false;
    _render();
  }
  // ── end training mode ─────────────────────────────────────────────────

  // ── Grand-reveal overlay ───────────────────────────────────────────────
  // Replaces the training card for first-run (is_onboarded=false) users.
  // Picks arm from A/C/D (B is stub until UX ships it), logs to training_events,
  // wires real prefs writes + graduation → sendMessage dissolve.
  function _showRevealOverlay(): void {
    if (_tmShownThisSession) return;
    if (sessionStorage.getItem("_tm_skip") === "1") return;
    _tmShownThisSession = true;

    const ACTIVE_ARMS = ["A", "B", "C", "D"] as const;
    const arm = ACTIVE_ARMS[Math.floor(Math.random() * ACTIVE_ARMS.length)];

    // Log arm assignment on the first training_completed/skipped row (stored
    // as reveal_version). We capture it in closure for the callback handlers.
    const _revealTrainingEvent = (eventType: string, source?: string, text?: string) => {
      void apiFetch(`${API_BASE}/chat/training-event`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ event_type: eventType, source, text, reveal_version: arm }),
      }).catch(() => {});
    };

    const overlay = document.getElementById("grandRevealOverlay") as HTMLDivElement | null;
    if (!overlay) return;

    // Skip button: sits above the iframe so it's always clickable
    const skipBtn = document.createElement("button");
    skipBtn.id = "revealSkipBtn";
    skipBtn.textContent = "I'll explore on my own";
    skipBtn.setAttribute("type", "button");

    const _dissolve = (fast = false) => {
      overlay.style.opacity = "0";
      skipBtn.style.display = "none";
      setTimeout(() => {
        overlay.hidden = true;
        overlay.innerHTML = "";
        overlay.style.opacity = "";
        skipBtn.remove();
        delete (window as unknown as Record<string, unknown>).__revealCallbacks;
      }, fast ? 320 : 650);
    };

    skipBtn.addEventListener("click", () => {
      _revealTrainingEvent("training_skipped");
      sessionStorage.setItem("_tm_skip", "1");
      _dissolve(true);
    });
    document.body.appendChild(skipBtn);

    // Callbacks the iframe calls via window.parent.__revealCallbacks
    (window as unknown as Record<string, unknown>).__revealCallbacks = {
      arm,
      onPick: (field: string, value: unknown) => {
        // Write the preference immediately; same PUT contract as training card
        void apiFetch(`${authApiBase}/auth/preferences`, {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ [field]: value, source: "training_mode" }),
        }).catch(() => {});
      },
      onGraduate: (question: string | null) => {
        // User completed the reveal (typed a question or clicked "Open Mobius →").
        // Dissolve + sendMessage FIRST — user-visible payoff must never be blocked by API failures.
        _dissolve();
        if (question) {
          setTimeout(() => sendMessage(question), 660);
        }
        // Fire-and-forget events + onboarding flip (failures cannot block the dissolve above).
        _revealTrainingEvent("training_completed");
        if (question) {
          _revealTrainingEvent("graduation_question_fired", "typed", question);
          void apiFetch(`${API_BASE}/chat/product-feedback`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ verbatim: question, category: "feature_request", trigger: "graduation", area_tags: ["rag"] }),
          }).catch(() => {});
        }
        // Inline _finishOnboarding (defined inside _showTrainingMode, out of scope here).
        void apiFetch(`${authApiBase}/auth/onboarding`, {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({}),
        }).then(() => { void _fetchNestedUserProfile(); }).catch(() => {});
        _syncOnboardingNudge(true);
      },
      onSkip: () => {
        _revealTrainingEvent("training_skipped");
        sessionStorage.setItem("_tm_skip", "1");
        _dissolve(true);
      },
    };

    // Mount the iframe
    const iframe = document.createElement("iframe");
    iframe.src = "/static/grand-reveal.html";
    iframe.setAttribute("title", "Mobius first-run experience");
    iframe.setAttribute("allowtransparency", "true");
    iframe.addEventListener("load", () => { iframe.style.opacity = "1"; });
    overlay.hidden = false;
    overlay.appendChild(iframe);
  }
  // ── end grand-reveal overlay ───────────────────────────────────────────

  let cachedProfile: MobiusChatUserProfile | null = null;

  function syncAnswerInsightsCheckbox(): void {
    const cb = document.getElementById("prefShowAnswerInsights") as HTMLInputElement | null;
    if (!cb) return;
    cb.checked = getShowLlmPerformance(cachedProfile);
    syncQueriesDumpVisibility(cachedProfile);
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
      // Prefer inserting into the Diagnostics tab panel if present
      const diagPanel = turnWrap.querySelector(".ac-tab-panel--diagnostics") as HTMLElement | null;
      const perf = turnWrap.querySelector(".llm-performance");
      const fb = turnWrap.querySelector(".feedback");
      if (perf) perf.insertAdjacentElement("afterend", el);
      else if (diagPanel) diagPanel.appendChild(el);
      else if (fb) fb.insertAdjacentElement("beforebegin", el);
      else turnWrap.appendChild(el);
      return;
    }
    const oneline = existing.querySelector(".adjudicator-scorecard-oneline") as HTMLElement | null;
    const badges = existing.querySelector(".adjudicator-scorecard-badges") as HTMLElement | null;
    if (oneline && badges) syncAdjudicatorScorecardDom(existing, qc, oneline, badges);
  }

  function _injectDiagnosticsTab(
    bubble: HTMLElement,
    opts: {
      insightRows: unknown[];
      perfMeta: unknown;
      thinkingLog: unknown;
      qc: QcAuditInfo | null | undefined;
      sourceConfidenceStrip: string | null;
      correlationId: string;
      totalCostFallback: unknown;
      inputTokens: number;
      outputTokens: number;
      routingFeedback: unknown;
      hipaaDiagnostics?: {
        gate: string; phi_flag: boolean;
        evidence_categories: string[]; identifier_labels: string[];
        hipaa_mode_allowed: boolean; action_taken: string;
        reason: string; transaction_id: string; document_name: string;
      } | null;
      msgPhiGate?: {
        gate: string; phi_flag: boolean; identifier_labels: string[]; action: string;
      } | null;
    }
  ): void {
    if (bubble.querySelector(".ac-tab-panel--diagnostics")) return; // idempotent

    // Build the panel
    const diagPanel = document.createElement("div");
    diagPanel.className = "ac-tab-panel ac-tab-panel--diagnostics";
    diagPanel.setAttribute("role", "tabpanel");
    diagPanel.setAttribute("hidden", "");

    // Section 1: LLM performance breakdown
    if (opts.insightRows.length > 0) {
      const perfEl = renderLlmPerformance(
        opts.insightRows as AnswerInsightRow[],
        opts.perfMeta as LlmPerformanceMeta | null | undefined,
        {
          qc: opts.qc ?? undefined,
          sourceConfidenceStrip: opts.sourceConfidenceStrip,
          correlationId: opts.correlationId,
          totalCostFallback: opts.totalCostFallback as number | null | undefined,
          inputTokens: opts.inputTokens,
          outputTokens: opts.outputTokens,
          routingFeedback: opts.routingFeedback as { rating: string; comment?: string | null } | null,
        }
      );
      diagPanel.appendChild(perfEl);
    }

    // Section 2: RAG retrieval trace
    const traceEl = renderDiagnosticsCard(
      opts.thinkingLog as ReadonlyArray<unknown> | null | undefined
    );
    if (traceEl) diagPanel.appendChild(traceEl);

    // Section 3: HIPAA gate audit (if this turn followed an instant-RAG upload)
    if (opts.hipaaDiagnostics) {
      const hd = opts.hipaaDiagnostics;
      const hipaaSection = document.createElement("div");
      hipaaSection.className = "diag-hipaa-section collapsed";

      const gateLabel = hd.gate === "clean" ? "clean" : hd.gate === "indeterminate" ? "indeterminate" : "phi";
      const gateColor = hd.gate === "clean" ? "#22c55e" : hd.gate === "indeterminate" ? "#f59e0b" : "#ef4444";
      const isPublicEligible = hd.action_taken === "published";

      // ceiling classification from action_taken
      let ceilingLabel = "—";
      if (hd.action_taken === "published") ceilingLabel = "public-eligible";
      else if (hd.action_taken === "published_private") ceilingLabel = "private (PHI suspected)";
      else if (hd.action_taken === "blocked") ceilingLabel = "blocked";
      else if (hd.action_taken === "blocked_indeterminate") ceilingLabel = "blocked (indeterminate)";

      // Collapsible header (collapsed by default — summary line visible)
      const header = document.createElement("button");
      header.type = "button";
      header.className = "diag-hipaa-toggle";
      header.innerHTML = `
        <span class="diag-hipaa-chevron">▶</span>
        <span class="diag-hipaa-title">HIPAA Screening</span>
        <span class="diag-hipaa-gate" style="color:${gateColor};">${gateLabel.toUpperCase()}</span>
        <span class="diag-hipaa-summary-pill">${escapeHtml(ceilingLabel)}</span>`;
      header.addEventListener("click", () => {
        const collapsed = hipaaSection.classList.toggle("collapsed");
        header.querySelector(".diag-hipaa-chevron")!.textContent = collapsed ? "▶" : "▼";
      });

      // Expandable body (hidden when collapsed)
      const body = document.createElement("div");
      body.className = "diag-hipaa-body";

      const table = document.createElement("table");
      table.className = "diag-hipaa-table";
      table.innerHTML = `
        <tr><td class="diag-hipaa-key">Document</td><td class="diag-hipaa-val">${escapeHtml(hd.document_name)}</td></tr>
        <tr><td class="diag-hipaa-key">PHI detected</td><td class="diag-hipaa-val">${hd.phi_flag ? "Yes" : "No"}</td></tr>
        <tr><td class="diag-hipaa-key">Classification ceiling</td><td class="diag-hipaa-val">${escapeHtml(ceilingLabel)}</td></tr>
        <tr><td class="diag-hipaa-key">HIPAA mode</td><td class="diag-hipaa-val">${hd.hipaa_mode_allowed ? "ON" : "OFF"}</td></tr>
        ${hd.identifier_labels.length ? `<tr><td class="diag-hipaa-key">Identifiers</td><td class="diag-hipaa-val">${hd.identifier_labels.map(l => `<span class="diag-hipaa-pill">${escapeHtml(l)}</span>`).join(" ")}</td></tr>` : ""}
        <tr><td class="diag-hipaa-key">Transaction</td><td class="diag-hipaa-val diag-hipaa-mono">${escapeHtml(hd.transaction_id || "—")}</td></tr>`;
      body.appendChild(table);

      // "Make public" action zone — gated on ceiling=public-eligible AND phi_flag===false
      // AND canPromote from auth context (cachedProfile roles, NOT the diagnostics payload).
      // Keeping authz out of hipaa_diagnostics matters: that payload is re-read from the
      // audit row on the dedup-cached-verdict path and must be auth-free.
      const canPromote = canPromoteToPublic(cachedProfile);
      if (isPublicEligible && !hd.phi_flag && canPromote) {
        const docIdForPromote = (hd as any).document_id || "";
        const promoteRow = document.createElement("div");
        promoteRow.className = "diag-hipaa-promote-row";
        const promoteBtn = document.createElement("button");
        promoteBtn.type = "button";
        promoteBtn.className = "diag-hipaa-promote-btn";
        promoteBtn.textContent = "Make public →";
        promoteBtn.title = "Promote this document to the shared corpus";
        promoteBtn.addEventListener("click", async () => {
          if (!docIdForPromote) {
            showChatStatusBanner("Cannot promote — document ID unknown.", 4000);
            return;
          }
          promoteBtn.disabled = true;
          promoteBtn.textContent = "Promoting…";
          try {
            const token = (window as any).__mobiusAuthToken || "";
            const res = await fetch(`/chat/documents/${encodeURIComponent(docIdForPromote)}/promote`, {
              method: "POST",
              headers: { "Authorization": `Bearer ${token}`, "Content-Type": "application/json" },
              body: JSON.stringify({ visibility: "public" }),
            });
            if (res.ok) {
              promoteBtn.textContent = "✓ Public";
              promoteBtn.classList.add("diag-hipaa-promote-btn--done");
            } else {
              const err = await res.json().catch(() => ({}));
              promoteBtn.textContent = "Make public";
              promoteBtn.disabled = false;
              showChatStatusBanner(`Promote failed: ${(err as any).detail || res.status}`, 5000);
            }
          } catch (_e) {
            promoteBtn.textContent = "Make public";
            promoteBtn.disabled = false;
            showChatStatusBanner("Promote request failed — check connection.", 4000);
          }
        });
        promoteRow.appendChild(promoteBtn);
        body.appendChild(promoteRow);
      }

      hipaaSection.appendChild(header);
      hipaaSection.appendChild(body);
      diagPanel.appendChild(hipaaSection);
    }

    // Section 4: PHI message gate verdict
    if (opts.msgPhiGate) {
      const mg = opts.msgPhiGate;
      const row = document.createElement("div");
      row.className = "diag-phi-msg-row";

      let icon = "✓";
      let label = "HIPAA checked · no PHI";
      let stateClass = "diag-phi-msg-row--clean";
      if (mg.action === "overridden") {
        icon = "⚠";
        label = "PHI detected · user override";
        stateClass = "diag-phi-msg-row--override";
      } else if (mg.gate === "indeterminate") {
        icon = "⚠";
        label = "PHI check unavailable · blocked (fail-closed)";
        stateClass = "diag-phi-msg-row--indeterminate";
      } else if (mg.gate === "phi" || mg.phi_flag) {
        icon = "✕";
        label = "PHI detected · blocked";
        stateClass = "diag-phi-msg-row--blocked";
      }

      row.classList.add(stateClass);
      const labelParts = [
        `<span class="diag-phi-msg-icon">${icon}</span>`,
        `<span class="diag-phi-msg-label">${label}</span>`,
      ];
      if (mg.identifier_labels.length) {
        const pills = mg.identifier_labels
          .map(l => `<span class="diag-phi-msg-pill">${escapeHtml(l)}</span>`)
          .join(" ");
        labelParts.push(`<span class="diag-phi-msg-pills">${pills}</span>`);
      }
      row.innerHTML = labelParts.join("");
      diagPanel.appendChild(row);
    }

    // Wire tab button into the tab bar
    const tabBar = bubble.querySelector(".ac-tab-bar") as HTMLElement | null;
    if (tabBar) {
      const diagBtn = document.createElement("button");
      diagBtn.type = "button";
      diagBtn.className = "ac-tab ac-tab--diagnostics";
      diagBtn.setAttribute("role", "tab");
      diagBtn.setAttribute("aria-selected", "false");
      diagBtn.setAttribute("data-panel", "diagnostics");
      diagBtn.textContent = "Diagnostics";
      diagBtn.addEventListener("click", () => {
        const liveBubble = diagBtn.closest(".answer-card-bubble") ?? bubble;
        tabBar.querySelectorAll(".ac-tab").forEach((t) => {
          t.classList.remove("ac-tab--active");
          t.setAttribute("aria-selected", "false");
        });
        liveBubble.querySelectorAll(".ac-tab-panel").forEach((p) => {
          (p as HTMLElement).hidden = true;
          p.classList.remove("ac-tab-panel--active");
        });
        diagBtn.classList.add("ac-tab--active");
        diagBtn.setAttribute("aria-selected", "true");
        diagPanel.hidden = false;
        diagPanel.classList.add("ac-tab-panel--active");
      });
      tabBar.appendChild(diagBtn);
    }

    bubble.appendChild(diagPanel);
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

  // 2026-05-06: cache the FULL nested user.profile from /me (the
  // canonical mobius-user shape with rendered_prompt + communication +
  // autonomy + tasks). The AuthService's getUserProfile() flattens
  // these fields and drops rendered_prompt during normalizeUser, so
  // we fetch /me ourselves to keep the spec-conformant nested object.
  // Sent on every chat POST; backend pipeline splices into 5 stage
  // system prompts (Mobius-user/CONSUMER_RECIPE_PROFILE.md).
  let cachedUserProfileNested: Record<string, unknown> | null = null;

  async function _fetchNestedUserProfile(): Promise<void> {
    try {
      const headers = await auth.getAuthHeader?.();
      if (!headers) {
        cachedUserProfileNested = null;
        return;
      }
      const r = await fetch(`${authApiBase}/auth/me`, { headers });
      if (!r.ok) {
        cachedUserProfileNested = null;
        return;
      }
      const data = (await r.json()) as {
        ok?: boolean;
        user?: {
          profile?: Record<string, unknown> | null;
          is_onboarded?: boolean;
          preferred_name?: string;
          first_name?: string;
          display_name?: string;
          email?: string;
        };
      };
      const user = (data && data.user) ? data.user : null;
      const p = (user && user.profile) || null;
      cachedUserProfileNested = (p && typeof p === "object") ? p : null;
      if (user) {
        // Update sidebar name from /me when the auth-service normalized profile
        // hasn't populated it yet (un-onboarded accounts, page-load race, etc.).
        const nameFromMe =
          user.preferred_name ||
          user.first_name ||
          user.display_name ||
          (user.email ? user.email.split("@")[0] : null);
        if (sidebarUserName && (!sidebarUserName.textContent || sidebarUserName.textContent === "Guest")) {
          if (nameFromMe) sidebarUserName.textContent = nameFromMe;
        }
        // Show reveal overlay for first-run users; retrain via /training still uses training card.
        const tmName = nameFromMe ?? "there";
        if (user.is_onboarded === false) {
          _showRevealOverlay();
        } else if (new URL(location.href).searchParams.get("welcome") === "1") {
          _showTrainingMode(tmName, "invited", true);
        }
        // Show/hide onboarding setup nudge for signed-in but un-onboarded users.
        _syncOnboardingNudge(user.is_onboarded !== false);
      }
    } catch {
      cachedUserProfileNested = null;
    }
  }

  auth.on(() => {
    void auth.getUserProfile().then((p: unknown) => {
      cachedProfile = p as MobiusChatUserProfile | null;
      updateSidebarUser(p as MobiusChatUserProfile | null);
      syncAnswerInsightsCheckbox();
      // Show/hide auth gate based on sign-in status.
      _setAuthGate(!p);
      // Hide nudge immediately on sign-out; _fetchNestedUserProfile will
      // show it again if the signed-in user is un-onboarded.
      if (!p) _syncOnboardingNudge(true);
      // Reload sidebar history whenever auth state settles — the initial
      // loadSidebarHistory() at startup fires before the token resolves
      // so headers are empty and the server returns []. Re-fetch now that
      // we have (or lost) a valid token.
      loadSidebarHistory();
    });
    void _fetchNestedUserProfile();
  });
  void auth.getUserProfile().then((p: unknown) => {
    cachedProfile = p as MobiusChatUserProfile | null;
    updateSidebarUser(p as MobiusChatUserProfile | null);
    syncAnswerInsightsCheckbox();
    // Resolve gate on initial page load.
    _setAuthGate(!p);
    // Also reload sidebar on the initial resolution path (covers the case
    // where auth.on fires synchronously before loadSidebarHistory runs).
    if (p) loadSidebarHistory();
  });
  void _fetchNestedUserProfile();

  const prefShowAnswerInsights = document.getElementById(
    "prefShowAnswerInsights"
  ) as HTMLInputElement | null;
  prefShowAnswerInsights?.addEventListener("change", () => {
    try {
      localStorage.setItem(LLM_PERF_LS, prefShowAnswerInsights.checked ? "1" : "0");
    } catch {
      /* ignore */
    }
    syncQueriesDumpVisibility(cachedProfile);
  });

  if (sidebarUser) {
    const userMenu = createUserMenu({
      auth,
      onOpenPreferences: () => { prefsModal.open(); },
      onSignOut: () => { modal.open("login"); },
    });
    sidebarUser.addEventListener("click", () => {
      void auth.getUserProfile().then((user: unknown) => {
        if (user) { void userMenu.show(sidebarUser); }
        else { modal.open("login"); }
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
  initModelProfilePicker();
  initSidebarRailIcons(auth);

  hamburger.addEventListener("click", openDrawer);
  // Tasks modal launcher (drawer entry) — closes the drawer so the modal
  // isn't stacked under the overlay.
  document.getElementById("btnTasksModal")?.addEventListener("click", () => {
    closeDrawer();
    openTasksModal();
  });
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
  setupQueriesDumpUI();
  syncQueriesDumpVisibility(cachedProfile);

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
      // Stall bailout: if no new progress (thinking line / message growth / status change)
      // for STALL_MS, treat as orphaned turn and reject. Protects against backend
      // jobs lost mid-flight (BRPOP-without-ack pattern in queue/redis_queue.py) so the
      // user isn't stuck in a "Thinking…" forever poll loop.
      const STALL_MS = 90_000;
      let attempts = 0;
      const seenLines = new Set<string>();
      let lastMessageLen = 0;
      let lastStatus: string | undefined;
      let lastProgressMs = Date.now();

      function poll(): void {
        fetch(API_BASE + "/chat/response/" + correlationId)
          .then((r) => r.json() as Promise<ChatResponse>)
          .then((data) => {
            let progressed = false;
            if (data.thinking_log?.length && onThinking) {
              data.thinking_log.forEach((entry) => {
                // Mixed array (Sprint A.1): string OR envelope dict.
                const line = thinkingLineFromEntry(entry);
                if (!seenLines.has(line)) {
                  seenLines.add(line);
                  onThinking(line);
                  progressed = true;
                }
              });
            }
            if (data.message != null && data.message !== "" && onStreamingMessage) {
              onStreamingMessage(data.message);
              if (data.message.length !== lastMessageLen) {
                lastMessageLen = data.message.length;
                progressed = true;
              }
            }
            if (data.status && data.status !== lastStatus) {
              lastStatus = data.status;
              progressed = true;
            }
            if (progressed) {
              lastProgressMs = Date.now();
            }
            if (data.status === "completed" || data.status === "clarification" || data.status === "refinement_ask" || data.status === "failed") {
              resolve(data);
              return;
            }
            // Stall check: no new thinking lines, no message growth, no status change for STALL_MS.
            // Backend likely lost the job (instance scale-in, crash, deploy) — abort so the
            // user can retry instead of spinning forever.
            if (Date.now() - lastProgressMs > STALL_MS) {
              reject(new Error(
                "Request appears to have been lost (no progress for " +
                Math.round(STALL_MS / 1000) +
                "s). Please retry."
              ));
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
    onStreamingMessage: ((text: string) => void) | null,
    onDraftReady?: ((text: string, modeHint?: string) => void) | null
  ): Promise<ChatResponse> {
    if (typeof EventSource === "undefined") {
      return pollResponse(correlationId, onThinking, onStreamingMessage);
    }
    const streamUrl = API_BASE + "/chat/stream/" + encodeURIComponent(correlationId);
    return new Promise((resolve, reject) => {
      let messageSoFar = "";
      let resolved = false;
      let draftEmitted = false;
      // Stall bailout (mirrors pollResponse): if SSE delivers no events for STALL_MS,
      // treat as orphaned turn. Protects against the backend losing the job silently.
      const STALL_MS = 90_000;
      let lastEventMs = Date.now();
      const es = new EventSource(streamUrl);
      const stallTimer = window.setInterval(() => {
        if (resolved) return;
        if (Date.now() - lastEventMs > STALL_MS) {
          resolved = true;
          es.close();
          window.clearInterval(stallTimer);
          reject(new Error(
            "Request appears to have been lost (no progress for " +
            Math.round(STALL_MS / 1000) +
            "s). Please retry."
          ));
        }
      }, 5000);
      es.onmessage = (e: MessageEvent) => {
        lastEventMs = Date.now();
        try {
          const parsed = JSON.parse(e.data as string) as { event: string; data?: unknown };
          const ev = parsed.event;
          const data = (parsed.data ?? {}) as Record<string, unknown>;
          if (ev === "thinking" && data.line != null && onThinking) {
            onThinking(String(data.line));
          } else if (ev === "quality_audit" && data.line != null && onThinking) {
            onThinking(String(data.line));
          } else if (ev === "draft_ready" && data.text != null) {
            draftEmitted = true;
            if (onDraftReady) onDraftReady(String(data.text), data.mode_hint ? String(data.mode_hint) : undefined);
          } else if (ev === "message" && data.chunk != null && !draftEmitted && onStreamingMessage) {
            messageSoFar += String(data.chunk);
            onStreamingMessage(messageSoFar);
          } else if (ev === "completed" && data) {
            resolved = true;
            es.close();
            window.clearInterval(stallTimer);
            resolve(data as unknown as ChatResponse);
          } else if (ev === "error" && data.message != null) {
            resolved = true;
            es.close();
            window.clearInterval(stallTimer);
            reject(new Error(String(data.message)));
          }
        } catch (err) {
          resolved = true;
          es.close();
          window.clearInterval(stallTimer);
          reject(err instanceof Error ? err : new Error(String(err)));
        }
      };
      es.onerror = () => {
        es.close();
        if (resolved) return;
        window.clearInterval(stallTimer);
        pollResponse(correlationId, onThinking, onStreamingMessage).then(resolve).catch(reject);
      };
    });
  }

  const chatEmpty = document.getElementById("chatEmpty");

  let credentialingPendingMessage: string | null = null;
  /** When set, successful roster upload re-opens the credentialing modal with this message. */
  let credentialingReopenMessage: string | null = null;

  function hideCredentialingEnvelope(): void {
    credentialingPendingMessage = null;
    document.getElementById("credentialingModal")?.setAttribute("hidden", "");
    document.getElementById("credentialingOverlay")?.classList.remove("open");
  }

  interface ThreadUploadsRosterRow {
    upload_id?: string;
    org_id?: string;
    org_name?: string;
    filename?: string;
    purpose?: string;
    row_count?: number;
    uploaded_at?: string | null;
  }

  type RosterThreadFreshnessApi = "fresh" | "stale" | "none";
  type RosterThreadSignalVariant = RosterThreadFreshnessApi | "muted";

  function normalizeRosterFreshness(raw: unknown): RosterThreadFreshnessApi {
    const s = typeof raw === "string" ? raw.trim().toLowerCase() : "";
    if (s === "fresh" || s === "stale" || s === "none") return s;
    return "none";
  }

  function formatRosterUploadInstant(iso: string | null | undefined): string {
    if (!iso || typeof iso !== "string") return "";
    try {
      const d = new Date(iso.trim().replace(/Z$/, "+00:00"));
      if (Number.isNaN(d.getTime())) return "";
      return d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
    } catch {
      return "";
    }
  }

  function rosterLatestRowPresent(row: ThreadUploadsRosterRow | null | undefined): boolean {
    return !!(row && (row.upload_id || "").trim() && (row.org_id || "").trim());
  }

  function messageForRosterThreadSignal(
    freshness: RosterThreadFreshnessApi,
    latest: ThreadUploadsRosterRow | null | undefined,
    thresholdDays: number
  ): string {
    const org = (latest?.org_name || "").trim();
    const fn = (latest?.filename || "").trim();
    const when = formatRosterUploadInstant(latest?.uploaded_at ?? undefined);
    const th = thresholdDays > 0 ? thresholdDays : 14;

    if (freshness === "none") {
      return (
        "No roster on this chat yet — upload one to compare your file against external data, " +
        "or continue with outside-in Medicaid NPI."
      );
    }
    if (freshness === "fresh") {
      const parts = ["Recent roster on this chat"];
      if (when) parts.push(`(${when})`);
      if (org) parts.push(`— ${org}`);
      parts.push("— you can run reconciliation without uploading again.");
      return parts.join(" ");
    }
    if (!when) {
      return (
        `A roster is linked${org ? ` (${org})` : ""}` +
        (fn ? ` — ${fn}` : "") +
        ", but the upload date is missing — re-upload if the file may be outdated."
      );
    }
    return (
      `Last roster upload ${when}${org ? ` · ${org}` : ""} — older than ${th} days. ` +
      "You can still use it or upload a newer file."
    );
  }

  function setRosterThreadSignalBanner(
    root: HTMLElement | null,
    variant: RosterThreadSignalVariant,
    text: string
  ): void {
    if (!root) return;
    root.classList.remove(
      "roster-thread-signal--fresh",
      "roster-thread-signal--stale",
      "roster-thread-signal--none",
      "roster-thread-signal--muted"
    );
    root.classList.add(`roster-thread-signal--${variant}`);
    const p = root.querySelector(".roster-thread-signal__text");
    if (p) p.textContent = text;
    root.removeAttribute("hidden");
  }

  function refreshCredentialingRosterUi(): void {
    const panel = document.getElementById("credentialingRosterPanel");
    const signalEl = document.getElementById("credentialingRosterSignal");
    const titleEl = document.getElementById("credentialingRosterTitle");
    const listEl = document.getElementById("credentialingRosterList");
    const hintEl = document.getElementById("credentialingRosterHint");
    const outsideWrap = document.getElementById("credentialingPreferOutsideInWrap");
    const outsideCb = document.getElementById("credentialingPreferOutsideIn") as HTMLInputElement | null;
    const freshWrap = document.getElementById("credentialingPreferFreshWrap");
    const freshCb = document.getElementById("credentialingPreferFresh") as HTMLInputElement | null;
    const orgEl = document.getElementById("credentialingOrgName") as HTMLInputElement | null;
    if (!panel || !titleEl || !listEl || !hintEl || !outsideWrap || !outsideCb || !freshWrap || !freshCb) return;

    const orgHint = (orgEl?.value ?? "").trim();

    const tid = (currentThreadId || "").trim();
    if (!tid) {
      panel.removeAttribute("hidden");
      setRosterThreadSignalBanner(
        signalEl,
        "muted",
        "No chat thread yet — send a message first so roster uploads can attach here. Until then we treat this as outside-in Medicaid NPI only."
      );
      titleEl.textContent = "Roster files on this chat";
      listEl.innerHTML = "";
      listEl.setAttribute("hidden", "");
      hintEl.textContent =
        "No thread yet — send once so uploads attach to this chat. Without a roster file we run the outside-in Medicaid NPI pipeline.";
      hintEl.hidden = false;
      outsideWrap.setAttribute("hidden", "");
      freshWrap.removeAttribute("hidden");
      return;
    }

    fetch(API_BASE + "/chat/thread/" + encodeURIComponent(tid) + "/uploads")
      .then(
        (r) =>
          r.json() as Promise<{
            roster_reconciliation_files?: ThreadUploadsRosterRow[];
            uploaded_files?: Array<{ purpose?: string; upload_id?: string; org_id?: string }>;
            reconciliation_upload_id?: string | null;
            reconciliation_org_id?: string | null;
            reconciliation_org_name?: string | null;
            latest_roster_reconciliation?: ThreadUploadsRosterRow | null;
            roster_freshness?: string;
            roster_fresh_days_threshold?: number;
          }>
      )
      .then((data) => {
        let rows: ThreadUploadsRosterRow[] = Array.isArray(data.roster_reconciliation_files)
          ? [...data.roster_reconciliation_files]
          : [];
        const hasTop = !!(data.reconciliation_upload_id && data.reconciliation_org_id);
        const files = Array.isArray(data.uploaded_files) ? data.uploaded_files : [];
        const hasFile = files.some(
          (u) =>
            (u.purpose || "").trim() === "roster_reconciliation" &&
            !!(u.upload_id || "").trim() &&
            !!(u.org_id || "").trim()
        );
        const hasRoster = rows.length > 0 || hasTop || hasFile;
        if (rows.length === 0 && hasTop) {
          const rn = (data.reconciliation_org_name || "").trim();
          const rup = (data.reconciliation_upload_id || "").trim();
          const rid = (data.reconciliation_org_id || "").trim();
          if (rup && rn) {
            rows = [{ upload_id: rup, org_id: rid, org_name: rn, filename: "", purpose: "roster_reconciliation" }];
          }
        }

        const th =
          typeof data.roster_fresh_days_threshold === "number" && data.roster_fresh_days_threshold > 0
            ? data.roster_fresh_days_threshold
            : 14;
        let latestRow: ThreadUploadsRosterRow | null =
          data.latest_roster_reconciliation && rosterLatestRowPresent(data.latest_roster_reconciliation)
            ? data.latest_roster_reconciliation
            : null;
        if (!latestRow && rows.length > 0 && rosterLatestRowPresent(rows[0])) {
          latestRow = rows[0];
        }
        const apiFresh = normalizeRosterFreshness(data.roster_freshness);
        const effectiveFresh: RosterThreadFreshnessApi =
          hasRoster && latestRow ? apiFresh : "none";
        setRosterThreadSignalBanner(
          signalEl,
          effectiveFresh,
          messageForRosterThreadSignal(effectiveFresh, latestRow, th)
        );

        const recName = (data.reconciliation_org_name || "").trim();
        let classification: "matched" | "ambiguous" | "no_files" = "no_files";
        if (!hasRoster) {
          classification = "no_files";
        } else if (!orgHint) {
          classification = "ambiguous";
        } else {
          let matches = 0;
          for (const u of rows) {
            if (orgHintMatchesUploadOrg(orgHint, u.org_name || "")) matches += 1;
          }
          if (recName && orgHintMatchesUploadOrg(orgHint, recName)) matches += 1;
          classification = matches >= 1 ? "matched" : "ambiguous";
        }

        panel.removeAttribute("hidden");
        listEl.innerHTML = "";
        if (rows.length > 0) {
          listEl.removeAttribute("hidden");
          for (const u of rows) {
            const li = document.createElement("li");
            const fn = (u.filename || "").trim() || "upload";
            const on = (u.org_name || "").trim() || "—";
            const match = orgHint ? orgHintMatchesUploadOrg(orgHint, on) : false;
            if (match) li.classList.add("credentialing-roster-list__match");
            li.textContent = `${fn} — ${on}`;
            listEl.appendChild(li);
          }
        } else {
          listEl.setAttribute("hidden", "");
        }

        if (classification === "no_files") {
          titleEl.textContent = "No roster file on this chat";
          hintEl.textContent =
            "We will run the outside-in Medicaid NPI pipeline. Upload a roster below if you want reconciliation (your file vs external data), or use the 📎 paperclip to attach a file.";
        } else if (classification === "matched") {
          titleEl.textContent = "Roster files linked to this chat";
          hintEl.textContent =
            "Matching rows are highlighted. Default run is roster reconciliation unless you check “Outside-in Medicaid NPI only” below.";
        } else {
          titleEl.textContent = "Roster files on this chat";
          hintEl.textContent =
            "No upload row matches the organization name above (or it is empty). Upload a roster or run with the server’s latest reconciliation upload — we will pick the latest when appropriate.";
        }
        hintEl.hidden = false;
        if (hasRoster) {
          outsideWrap.removeAttribute("hidden");
        } else {
          outsideWrap.setAttribute("hidden", "");
        }
        const outsideInPath = !hasRoster || outsideCb.checked;
        if (outsideInPath) {
          freshWrap.removeAttribute("hidden");
        } else {
          freshWrap.setAttribute("hidden", "");
          freshCb.checked = false;
        }
      })
      .catch(() => {
        panel.removeAttribute("hidden");
        setRosterThreadSignalBanner(
          signalEl,
          "muted",
          "Could not load roster status from the server — reconciliation vs outside-in still follows thread state when you run."
        );
        titleEl.textContent = "Roster status";
        listEl.innerHTML = "";
        listEl.setAttribute("hidden", "");
        hintEl.textContent =
          "Could not load upload status; the server still chooses reconciliation vs outside-in from thread state.";
        hintEl.hidden = false;
        outsideWrap.setAttribute("hidden", "");
        freshWrap.setAttribute("hidden", "");
        freshCb.checked = false;
      });
  }

  function openCredentialingEnvelope(message: string): void {
    credentialingPendingMessage = message;
    const orgEl = document.getElementById("credentialingOrgName") as HTMLInputElement | null;
    const modal = document.getElementById("credentialingModal");
    const overlay = document.getElementById("credentialingOverlay");
    if (!orgEl || !modal || !overlay) {
      sendMessage(message, { skipCredentialingEnvelope: true });
      return;
    }
    const hint = extractCredentialingOrgHint(message);
    orgEl.value = hint;
    const ap = document.querySelector('input[name="credentialingMode"][value="autopilot"]') as HTMLInputElement | null;
    if (ap) ap.checked = true;
    const fr = document.getElementById("credentialingForceRefresh") as HTMLInputElement | null;
    if (fr) fr.checked = false;
    const po = document.getElementById("credentialingPreferOutsideIn") as HTMLInputElement | null;
    if (po) po.checked = false;
    const pf = document.getElementById("credentialingPreferFresh") as HTMLInputElement | null;
    if (pf) pf.checked = false;
    refreshCredentialingRosterUi();
    modal.removeAttribute("hidden");
    overlay.classList.add("open");
    orgEl.focus();
  }

  // ── @-mention coworker autocomplete ─────────────────────────────────
  // Triggered by "@" in the composer. Fetches /chat/coworkers (org-scoped,
  // server-derives org from the caller's identity — no org in the URL).
  // Picked mentions are carried in the chat payload as `mentions` so the
  // planner can use exact assignee_refs instead of re-resolving free text.

  let _pendingMentions: Array<{ display_name: string; assignee_ref: string }> = [];
  let _pendingHipaaDiagnostics: {
    gate: string; phi_flag: boolean;
    evidence_categories: string[]; identifier_labels: string[];
    hipaa_mode_allowed: boolean; action_taken: string;
    reason: string; transaction_id: string; document_name: string;
  } | null = null;
  // PHI message-gate verdict for the next assistant turn's diagnostics tab.
  let _pendingMsgPhiGate: {
    gate: string; phi_flag: boolean; identifier_labels: string[]; action: string;
  } | null = null;
  let _coworkerFetchTimer: ReturnType<typeof setTimeout> | null = null;
  let _coworkerDropdown: HTMLElement | null = null;

  async function _fetchCoworkers(q: string): Promise<Array<{ display_name: string; email?: string; assignee_ref: string }>> {
    try {
      const params = q.trim() ? `?q=${encodeURIComponent(q.trim())}&limit=8` : "?limit=8";
      const r = await apiFetch(`${API_BASE}/chat/coworkers${params}`);
      if (!r.ok) return [];
      const d = await r.json();
      return Array.isArray(d.coworkers) ? d.coworkers : [];
    } catch { return []; }
  }

  function _closeAtDropdown(): void {
    _coworkerDropdown?.remove();
    _coworkerDropdown = null;
  }

  function _openAtDropdown(
    anchor: HTMLElement,
    coworkers: Array<{ display_name: string; email?: string; assignee_ref: string; is_agent?: boolean }>,
    atStart: number,
    atEnd: number,
    query: string,
  ): void {
    _closeAtDropdown();
    if (!coworkers.length && !query.trim()) return;
    const dd = document.createElement("div");
    dd.className = "at-mention-dropdown";
    dd.setAttribute("role", "listbox");
    const rect = anchor.getBoundingClientRect();
    dd.style.cssText = `position:fixed;bottom:${window.innerHeight - rect.top + 4}px;left:${rect.left}px;min-width:220px;max-width:320px;z-index:9999;`;
    if (!coworkers.length) {
      const empty = document.createElement("div");
      empty.className = "at-mention-empty";
      empty.textContent = "No matches";
      dd.appendChild(empty);
    } else {
      coworkers.forEach((c, i) => {
        const item = document.createElement("button");
        item.type = "button";
        item.className = "at-mention-item";
        item.setAttribute("role", "option");
        const agentBadge = c.is_agent ? `<span class="at-mention-badge">agent</span>` : "";
        item.innerHTML = `<span class="at-mention-name">${escapeHtml(c.display_name)}${agentBadge}</span>${c.email ? `<span class="at-mention-email">${escapeHtml(c.email)}</span>` : ""}`;
        item.addEventListener("mousedown", (e) => {
          e.preventDefault();
          const val = inputEl.value;
          const inserted = `@${c.display_name} `;
          inputEl.value = val.slice(0, atStart) + inserted + val.slice(atEnd);
          inputEl.selectionStart = inputEl.selectionEnd = atStart + inserted.length;
          _pendingMentions.push({ display_name: c.display_name, assignee_ref: c.assignee_ref });
          _closeAtDropdown();
          inputEl.focus();
        });
        if (i === 0) item.classList.add("at-mention-item--focused");
        dd.appendChild(item);
      });
    }
    document.body.appendChild(dd);
    _coworkerDropdown = dd;
  }

  inputEl.addEventListener("input", () => {
    const val = inputEl.value;
    const pos = inputEl.selectionStart ?? val.length;
    const before = val.slice(0, pos);
    const atMatch = before.match(/@(\w*)$/);
    if (!atMatch) { _closeAtDropdown(); return; }
    const q = atMatch[1];
    const atStart = pos - atMatch[0].length;
    if (_coworkerFetchTimer) clearTimeout(_coworkerFetchTimer);
    _coworkerFetchTimer = setTimeout(async () => {
      const results = await _fetchCoworkers(q);
      _openAtDropdown(inputEl, results, atStart, pos, q);
    }, 120);
  });

  inputEl.addEventListener("keydown", (e) => {
    if (!_coworkerDropdown) return;
    const items = Array.from(_coworkerDropdown.querySelectorAll<HTMLButtonElement>(".at-mention-item"));
    const focused = _coworkerDropdown.querySelector<HTMLButtonElement>(".at-mention-item--focused");
    const idx = focused ? items.indexOf(focused) : -1;
    if (e.key === "ArrowDown") {
      e.preventDefault();
      focused?.classList.remove("at-mention-item--focused");
      items[Math.min(idx + 1, items.length - 1)]?.classList.add("at-mention-item--focused");
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      focused?.classList.remove("at-mention-item--focused");
      items[Math.max(idx - 1, 0)]?.classList.add("at-mention-item--focused");
    } else if (e.key === "Enter" && focused) {
      e.preventDefault();
      focused.dispatchEvent(new MouseEvent("mousedown", { bubbles: true }));
    } else if (e.key === "Escape") {
      _closeAtDropdown();
    }
  });

  document.addEventListener("click", (e) => {
    if (_coworkerDropdown && !_coworkerDropdown.contains(e.target as Node)) _closeAtDropdown();
  });

  const _PHI_GATE_URL = "https://mobius-phi-classifier-ortabkknqa-uc.a.run.app";

  function _phiHighlightHtml(
    message: string,
    evidence: Array<{category: string; redacted_span: string; offset: number; length: number}>
  ): string {
    function esc(s: string) { return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;"); }
    const chars = Array.from(message);
    const spans = [...evidence].sort((a, b) => a.offset - b.offset);
    let pos = 0;
    const parts: string[] = [];
    for (const sp of spans) {
      if (sp.offset > pos) parts.push(esc(chars.slice(pos, sp.offset).join("")));
      const spanText = chars.slice(sp.offset, sp.offset + sp.length).join("");
      parts.push(`<mark class="phi-hl phi-hl--${esc(sp.category.toLowerCase())}">${esc(spanText)}</mark>`);
      pos = sp.offset + sp.length;
    }
    if (pos < chars.length) parts.push(esc(chars.slice(pos).join("")));
    return parts.join("");
  }

  function _showPhiGateCard(
    message: string,
    phiResult: {phi_evidence?: Array<{category: string; redacted_span: string; offset: number; length: number}>; identifier_labels?: string[]}
  ): Promise<"edit" | "override" | "dismiss"> {
    return new Promise((resolve) => {
      function esc(s: string) { return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;"); }
      const evidence = phiResult.phi_evidence || [];
      const labels = phiResult.identifier_labels || [];
      const labelStr = labels.length ? labels.join(", ") : "protected health information";
      const card = document.createElement("div");
      card.className = "phi-gate-card";
      card.innerHTML = `
        <div class="phi-gate-header">
          <span class="phi-gate-icon">🔒</span>
          <span class="phi-gate-title">PHI detected — message not sent</span>
          <button type="button" class="phi-gate-dismiss" aria-label="Dismiss">✕</button>
        </div>
        <p class="phi-gate-desc">This message appears to contain <strong>${esc(labelStr)}</strong>. Edit or remove the sensitive information before sending.</p>
        <div class="phi-gate-preview">${_phiHighlightHtml(message, evidence)}</div>
        <div class="phi-gate-actions">
          <button type="button" class="phi-gate-btn phi-gate-btn--edit">Edit message</button>
          <button type="button" class="phi-gate-btn phi-gate-btn--override">Send anyway</button>
        </div>`;
      card.querySelector(".phi-gate-dismiss")!.addEventListener("click", () => { card.remove(); resolve("dismiss"); });
      card.querySelector(".phi-gate-btn--edit")!.addEventListener("click", () => { card.remove(); resolve("edit"); });
      card.querySelector(".phi-gate-btn--override")!.addEventListener("click", () => { card.remove(); resolve("override"); });
      messagesEl.appendChild(card);
      scrollToBottom(messagesEl);
    });
  }

  function sendMessage(overrideMessage?: string, opts?: SendMessageOpts): void { void _sendMessageAsync(overrideMessage, opts); }
  async function _sendMessageAsync(overrideMessage?: string, opts?: SendMessageOpts): Promise<void> {
    let message = (overrideMessage ?? (inputEl.value ?? "").trim()).trim();
    if (overrideMessage !== undefined && overrideMessage !== null) {
      activeClarificationDraft = null;
    } else if (activeClarificationDraft?.length) {
      const preface = buildWorkflowSelectionPreface();
      if (preface && message) {
        message = `${preface}\n\n${message}`;
      } else if (preface && !message) {
        message = preface;
      }
    }
    if (!message) return;

    // Pre-router: /training and /welcome open training mode unconditionally —
    // never go through the planner (planner paraphrase drops intent; recite rule).
    if (message === "/training" || message === "/welcome") {
      inputEl.value = "";
      const tmName = (cachedProfile as Record<string, unknown> | null)?.["preferred_name"] as string
        || (cachedProfile as Record<string, unknown> | null)?.["first_name"] as string
        || (sidebarUserName?.textContent ?? "there");
      _showTrainingMode(tmName.trim() || "there", "invited", true);
      return;
    }

    if (sendBtn.disabled) return;
    activeClarificationDraft = null;

    if (
      !opts?.credentialing_options &&
      !opts?.skipCredentialingEnvelope &&
      isCredentialingReportIntent(message)
    ) {
      openCredentialingEnvelope(message);
      return;
    }

    // PHI pre-send gate — UX layer before DOM render. Backend re-checks authoritatively.
    // Fail-open on network error so a dead classifier never blocks the user.
    if (!opts?.phi_override) {
      let _phiResult: {block?: boolean; phi_evidence?: Array<{category: string; redacted_span: string; offset: number; length: number}>; identifier_labels?: string[]} | null = null;
      try {
        sendBtn.disabled = true;
        const _r = await fetch(`${_PHI_GATE_URL}/message-check`, {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({text: message, thread_id: currentThreadId}),
        });
        if (_r.ok) _phiResult = await _r.json();
      } catch { /* fail open */ } finally {
        sendBtn.disabled = false;
      }
      if (_phiResult?.block) {
        const gateAction = await _showPhiGateCard(message, _phiResult);
        if (gateAction === "edit") {
          if (!overrideMessage) { inputEl.value = message; inputEl.focus(); }
          return;
        }
        if (gateAction === "dismiss") return;
        // gateAction === "override" — re-enter with override flag so backend accepts
        _pendingMsgPhiGate = {
          gate: _phiResult.gate ?? "phi",
          phi_flag: true,
          identifier_labels: _phiResult.identifier_labels ?? [],
          action: "overridden",
        };
        inputEl.value = ""; // clear composer — normal send flow skips this for override re-entry
        sendMessage(message, {...(opts || {}), phi_override: true});
        return;
      }
      // Message passed the gate clean — stash verdict for diagnostics tab
      if (_phiResult) {
        _pendingMsgPhiGate = {
          gate: (_phiResult as {gate?: string}).gate ?? "clean",
          phi_flag: (_phiResult as {phi_flag?: boolean}).phi_flag ?? false,
          identifier_labels: _phiResult.identifier_labels ?? [],
          action: "passed",
        };
      }
    }

    if (chatEmpty) chatEmpty.classList.add("hidden");
    document.body.classList.remove("landing-state");

    // Auto-dismiss the alpha banner on first query — no need to keep
    // it in the way once the user is actively working.
    if (alphaBanner && !alphaBanner.hidden) {
      alphaBanner.hidden = true;
      localStorage.setItem("alpha_banner_dismissed", "1");
    }

    // Read mode before rendering user message (badge depends on it)
    const selectedMode = (
      localStorage.getItem("_mobiusChatMode")
      || "copilot"
    ) as "quick" | "copilot" | "agentic";

    messagesEl.querySelectorAll(".thinking-block").forEach((block) => {
      block.classList.add("collapsed");
      const p = block.querySelector(".thinking-preview");
      if (p) p.setAttribute("aria-expanded", "false");
    });

    // 1. User message; phase + pulse live in thinking preview row (see renderThinkingBlock).
    const turnWrap = document.createElement("div");
    turnWrap.className = "chat-turn";
    turnWrap.appendChild(renderUserMessage(message, selectedMode));
    messagesEl.appendChild(turnWrap);
    scrollToBottom(messagesEl);

    // Clear bottom suggestion chips immediately so prior-turn chips don't
    // linger during the new query's loading state.
    const _sugSlot = document.getElementById("chat-suggestions");
    if (_sugSlot) { _sugSlot.innerHTML = ""; _sugSlot.hidden = true; }

    if (!overrideMessage) inputEl.value = "";
    updateSendState();
    sendBtn.disabled = true;
    inputEl.disabled = true;

    // 2. Thinking block (compact line, streams then collapses)
    const thinkingLines: string[] = [];
    const {
      el: thinkingBlockEl,
      addLine: addThinkingLine,
      done: thinkingDone,
      onRequestCorrelationId,
      onRequestStreamChunk,
      markRequestFailed,
    } = renderThinkingBlock(["Sending request…"]);
    turnWrap.appendChild(thinkingBlockEl);
    scrollToBottom(messagesEl);

    function addThinkingLineAndScroll(line: string): void {
      thinkingLines.push(line);
      addThinkingLine(line);
      scrollToBottom(messagesEl);
    }

    let messageWrapEl: HTMLElement | null = null;
    let draftStreamCancel: (() => void) | null = null;
    let composerReleased = false;
    function releaseComposer() {
      if (composerReleased) return;
      composerReleased = true;
      sendBtn.disabled = false;
      inputEl.disabled = false;
      updateSendState();
    }
    /** During stream, do not show raw JSON; show placeholder until final render (AnswerCard or prose). */
    function streamingDisplayText(text: string): string {
      const t = (text ?? "").trim();
      if (t.startsWith("{")) return "Formatting answer…";
      return normalizeMessageText(text);
    }
    function onStreamingMessage(text: string): void {
      onRequestStreamChunk(text);
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
    function onDraftReady(text: string, modeHint?: string): void {
      // Replace any interim plain bubble (thinking text) with the card shell
      if (messageWrapEl) { messageWrapEl.remove(); messageWrapEl = null; }

      // RECITAL mode: serif prose shell — no tab bar needed
      if (modeHint === "RECITAL") {
        const wrap = document.createElement("div");
        wrap.className = "message message--assistant answer-card answer-card--recital is-streaming";
        const bubble = document.createElement("div");
        bubble.className = "message-bubble answer-card-bubble";
        const attr = document.createElement("div");
        attr.className = "recital-attr";
        attr.textContent = "From the Mobius founding essay:";
        bubble.appendChild(attr);
        const prose = document.createElement("div");
        prose.className = "recital-prose";
        const cursor = document.createElement("span");
        cursor.className = "ac-streaming-cursor";
        cursor.setAttribute("aria-hidden", "true");
        bubble.appendChild(prose);
        bubble.appendChild(cursor);
        wrap.appendChild(bubble);
        messageWrapEl = wrap;
        turnWrap.appendChild(messageWrapEl);
        releaseComposer();
        const words = text.split(" ");
        let wi = 0;
        let cancelled = false;
        draftStreamCancel = () => {
          cancelled = true;
          prose.innerHTML = simpleMarkdownToHtml(sanitizeDisplayMessage(text));
          cursor.remove();
          scrollToBottom(messagesEl);
        };
        function recitalStreamStep() {
          if (cancelled) return;
          wi = Math.min(wi + 5, words.length);
          prose.innerHTML = simpleMarkdownToHtml(words.slice(0, wi).join(" "));
          scrollToBottom(messagesEl);
          if (wi < words.length) window.setTimeout(recitalStreamStep, 18);
          else { draftStreamCancel = null; cursor.remove(); }
        }
        recitalStreamStep();
        return;
      }

      const wrap = document.createElement("div");
      wrap.className = "message message--assistant answer-card answer-card--blended is-streaming";

      const bubble = document.createElement("div");
      bubble.className = "message-bubble answer-card-bubble";

      // Tab bar — Summary active; other tabs hidden until data arrives on completed
      const streamTabBar = document.createElement("div");
      streamTabBar.className = "ac-tab-bar";
      streamTabBar.setAttribute("role", "tablist");
      const _mkStreamBtn = (label: string, panel: string, active: boolean) => {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "ac-tab" + (active ? " ac-tab--active" : "");
        btn.setAttribute("role", "tab");
        btn.setAttribute("aria-selected", String(active));
        btn.setAttribute("data-panel", panel);
        if (!active) btn.setAttribute("data-empty", "1");
        btn.textContent = label;
        btn.addEventListener("click", () => {
          const lb = btn.closest(".answer-card-bubble") ?? bubble;
          streamTabBar.querySelectorAll(".ac-tab").forEach((t) => { t.classList.remove("ac-tab--active"); t.setAttribute("aria-selected", "false"); });
          lb.querySelectorAll(".ac-tab-panel").forEach((p) => { (p as HTMLElement).hidden = true; p.classList.remove("ac-tab-panel--active"); });
          btn.classList.add("ac-tab--active");
          btn.setAttribute("aria-selected", "true");
          const tp = lb.querySelector(`.ac-tab-panel--${panel}`) as HTMLElement | null;
          if (tp) { tp.hidden = false; tp.classList.add("ac-tab-panel--active"); }
        });
        return btn;
      };
      streamTabBar.appendChild(_mkStreamBtn("Summary", "summary", true));
      streamTabBar.appendChild(_mkStreamBtn("Citations", "citations", false));
      streamTabBar.appendChild(_mkStreamBtn("Corrections", "corrections", false));
      streamTabBar.appendChild(_mkStreamBtn("Follow-up", "next-steps", false));
      streamTabBar.appendChild(_mkStreamBtn("Tasks", "tasks", false));
      bubble.appendChild(streamTabBar);

      // Summary panel — prose streams in here; cursor follows
      const summaryPanel = document.createElement("div");
      summaryPanel.className = "ac-tab-panel ac-tab-panel--summary ac-tab-panel--active";
      summaryPanel.setAttribute("role", "tabpanel");
      const prose = document.createElement("div");
      prose.className = "ac-summary-prose";
      const cursor = document.createElement("span");
      cursor.className = "ac-streaming-cursor";
      cursor.setAttribute("aria-hidden", "true");
      summaryPanel.appendChild(prose);
      summaryPanel.appendChild(cursor);

      // Cycling status line while streaming
      const statusEl = document.createElement("span");
      statusEl.className = "ac-streaming-status";
      const _statusPhrases = ["Searching sources…", "Refining answer…", "Checking accuracy…", "Summarizing…"];
      let _statusIdx = 0;
      statusEl.textContent = _statusPhrases[0];
      summaryPanel.appendChild(statusEl);
      const _statusInterval = window.setInterval(() => {
        statusEl.classList.add("ac-status-fade");
        window.setTimeout(() => {
          _statusIdx = (_statusIdx + 1) % _statusPhrases.length;
          statusEl.textContent = _statusPhrases[_statusIdx];
          statusEl.classList.remove("ac-status-fade");
        }, 400);
      }, 3000);
      bubble.dataset.statusInterval = String(_statusInterval);

      bubble.appendChild(summaryPanel);

      // Empty placeholder panels — filled in-place on completed
      (["citations", "corrections", "next-steps", "tasks"] as const).forEach((p) => {
        const panel = document.createElement("div");
        panel.className = `ac-tab-panel ac-tab-panel--${p}`;
        panel.setAttribute("role", "tabpanel");
        panel.setAttribute("hidden", "");
        bubble.appendChild(panel);
      });

      wrap.appendChild(bubble);
      messageWrapEl = wrap;
      turnWrap.appendChild(messageWrapEl);
      releaseComposer();

      // Word-stream into prose
      const words = text.split(" ");
      let wi = 0;
      let cancelled = false;
      draftStreamCancel = () => {
        cancelled = true;
        prose.innerHTML = simpleMarkdownToHtml(sanitizeDisplayMessage(text));
        cursor.remove();
        scrollToBottom(messagesEl);
      };
      function streamStep() {
        if (cancelled) return;
        wi = Math.min(wi + 5, words.length);
        prose.innerHTML = simpleMarkdownToHtml(words.slice(0, wi).join(" "));
        scrollToBottom(messagesEl);
        if (wi < words.length) window.setTimeout(streamStep, 18);
        else { draftStreamCancel = null; cursor.remove(); }
      }
      streamStep();
    }

    const payload: {
      message: string;
      thread_id?: string;
      credentialing_options?: CredentialingOptionsPayload;
      use_react?: boolean;
      chat_mode?: "copilot" | "agentic" | "quick";
      model_profile?: string;
    } = { message };
    if (currentThreadId) payload.thread_id = currentThreadId;
    if (opts?.credentialing_options) {
      payload.credentialing_options = opts.credentialing_options;
    }
    payload.chat_mode = selectedMode;
    // 2026-04-20: all modes default to ReAct. The old copilot =
    // legacy-planner mapping has been retired — the pipeline's
    // hardening (per-request deadline, PHI audit on both sides,
    // critic + adjudicator) is only exercised on the ReAct path.
    // Explicit override still honored for internal callers.
    if (opts?.use_react !== undefined) {
      payload.use_react = opts.use_react;
    }
    if (opts?.phi_override) {
      (payload as Record<string, unknown>).phi_override = true;
    }
    // 2026-04-27: include the model_profile dropdown selection in the
    // request payload. Previously the dropdown only POSTed to
    // /chat/admin/model-profile which sets a per-instance global —
    // fragile across the 4 Cloud Run instances (the LB picks a
    // different instance for the chat POST than for the admin POST).
    // Sending model_profile here makes the worker apply it for THIS
    // turn via profile_override(...), regardless of which instance
    // picks up the job.
    {
      const sel = document.getElementById("modelProfileSelect") as HTMLSelectElement | null;
      const v = (sel && sel.value || "").trim();
      if (v) payload.model_profile = v;
    }
    // 2026-05-06: send the nested user.profile from mobius-user so the
    // pipeline can splice rendered_prompt + read autonomy on every
    // stage system prompt. Cached at session boot + auth-state-change;
    // refreshed on PreferencesModal save (PUT /api/v1/auth/preferences
    // returns the fresh profile so we just re-fetch /me afterwards).
    if (cachedUserProfileNested) {
      (payload as Record<string, unknown>).profile = cachedUserProfileNested;
    }
    if (_pendingMentions.length) {
      (payload as Record<string, unknown>).mentions = _pendingMentions.slice();
      _pendingMentions = [];
    }
    let activeCorrelationId = "";
    const _chatAuthHeaders = await auth.getAuthHeader?.() ?? {};
    fetch(API_BASE + "/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json", ..._chatAuthHeaders },
      body: JSON.stringify(payload),
    })
      .then((r) => r.json() as Promise<ChatPostResponse>)
      .then((data) => {
        if (data.thread_id) currentThreadId = data.thread_id; window.__mobiusChatThreadId = currentThreadId;
        activeCorrelationId = data.correlation_id ?? "";
        if ((data.correlation_id || "").trim()) {
          onRequestCorrelationId();
        }
        addThinkingLineAndScroll("Request sent. Waiting for worker…");
        return streamResponse(data.correlation_id, addThinkingLineAndScroll, onStreamingMessage, onDraftReady);
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
        // Final thinking lines if any not yet shown. Mixed array
        // (Sprint A.1): string OR envelope dict; normalize first.
        (data.thinking_log ?? []).forEach((entry) => {
          const line = thinkingLineFromEntry(entry);
          if (!thinkingLines.includes(line)) addThinkingLineAndScroll(line);
        });
        // raw_text is the bypass-integrate path (refuse, task mode); message is the normal path
        const fullMessage = data.message ?? data.raw_text ?? "";
        const { body, sources } = parseMessageAndSources(fullMessage);

        if (data.response_source === "llm" && data.model_used) {
          addThinkingLineAndScroll("Model: " + data.model_used);
        }
        if (data.response_source === "stub" && data.llm_error) {
          addThinkingLineAndScroll("LLM failed (stub used): " + data.llm_error);
        }

        thinkingDone(thinkingLines.length);

        // Finish any in-progress word-stream; detect streaming-card path
        if (draftStreamCancel) { draftStreamCancel(); draftStreamCancel = null; }
        const isStreamingCard = !!messageWrapEl?.classList.contains("is-streaming");

        if (data.thread_id) currentThreadId = data.thread_id; window.__mobiusChatThreadId = currentThreadId;
        const cidForTurn = (data.correlation_id || activeCorrelationId || "").trim();
        if (cidForTurn) turnWrap.setAttribute("data-correlation-id", cidForTurn);

        // 3. Next questions (unified: payload + AnswerCard followups) – computed first so we can suppress inline followups
        let nextQuestions: FollowupLineNormalized[] = normalizeFollowupLineList(
          data.next_questions_for_user,
          true
        );
        if (nextQuestions.length === 0 && data.user_ask && String(data.user_ask).trim()) {
          nextQuestions = [{ text: String(data.user_ask).trim(), clickable: true }];
        }
        if (nextQuestions.length === 0) {
          const card = tryParseAnswerCard(body || "");
          if (card?.followups?.length) {
            nextQuestions = card.followups
              .map((f) => (f.question || f.reason || f.field || "").trim())
              .filter(Boolean)
              .map((text) => ({ text, clickable: true }));
          }
        }

        // 4. Assistant message: use roster_report_final_md when present (full report with charts)
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
        const envelopeHasPipelineGate =
          useEnvelope &&
          envBlocks.some((b) => (b as { type?: string }).type === "pipeline_human_gate");

        if (isStreamingCard && messageWrapEl) {
          // In-place fill: streaming card shell already in DOM — no transplant needed.
          messageWrapEl.classList.remove("is-streaming");
          const existingBubble = messageWrapEl.querySelector(".answer-card-bubble") as HTMLElement | null;

          // Clear cycling status interval and remove the element
          if (existingBubble?.dataset.statusInterval) {
            window.clearInterval(Number(existingBubble.dataset.statusInterval));
            existingBubble.querySelector(".ac-streaming-status")?.remove();
          }

          // Extract corrections and next-step tasks from envelope blocks
          const _extractedCorrections: Array<{ label: string; text: string }> = [];
          const _extractedNextStepTasks: Array<{ text: string; taskType: string }> = [];
          if (useEnvelope) {
            for (const _eb of (envCandidate as AssistantEnvelope).blocks || []) {
              const _ebt = (_eb as EnvelopeBlock).type;
              if (_ebt === "callout") {
                const _cb = _eb as { body: string; variant?: string };
                const _cbText = (_cb.body || "").trim();
                if (_cbText) _extractedCorrections.push({
                  label: _cb.variant === "warning" ? "Warning" : _cb.variant === "error" ? "Error" : "Note",
                  text: _cbText,
                });
              } else if (_ebt === "correction") {
                const _cb = _eb as { original: string; corrected: string };
                const _orig = (_cb.original || "").trim();
                const _fixed = (_cb.corrected || "").trim();
                if (_orig && _fixed) _extractedCorrections.push({ label: "Correction", text: _orig + " → " + _fixed });
              } else if (_ebt === "next_steps") {
                const _cb = _eb as { items: unknown[] };
                normalizeFollowupLineList(_cb.items || [], false).forEach((item) => {
                  if (item.text) _extractedNextStepTasks.push({ text: item.text, taskType: "follow_up" });
                });
              }
            }
          }

          const fullCard = tryParseAnswerCard(fullMessage);
          const _isRecitalShell = !!existingBubble?.querySelector(".recital-prose");
          if (fullCard && existingBubble) {
            // Update the mode class (was placeholder --blended or --recital; confirm correct mode)
            messageWrapEl.classList.remove("answer-card--blended");
            if (!_isRecitalShell) messageWrapEl.classList.remove("answer-card--recital");
            messageWrapEl.classList.add(`answer-card--${fullCard.mode.toLowerCase()}`);

            if (_isRecitalShell) {
              // RECITAL shell: prose already streamed into .recital-prose — just add expand CTA if clipped
              const prose = existingBubble.querySelector(".recital-prose") as HTMLElement | null;
              if (prose && fullCard.recital?.verbatim) {
                const PARA_LIMIT = 3;
                const stripped = fullCard.recital.verbatim.replace(/^[ \t]*[-*_]{3,}[ \t]*$/gm, "").trim();
                const allParas = stripped.split(/\n\n+/);
                if (allParas.length > PARA_LIMIT) {
                  const clippedText = allParas.slice(0, PARA_LIMIT).join("\n\n");
                  prose.innerHTML = simpleMarkdownToHtml(clippedText);
                  const readMore = document.createElement("button");
                  readMore.type = "button";
                  readMore.className = "recital-read-more";
                  readMore.textContent = "Read the full essay ↗";
                  let expanded = false;
                  readMore.addEventListener("click", () => {
                    expanded = !expanded;
                    prose.innerHTML = simpleMarkdownToHtml(expanded ? stripped : clippedText);
                    readMore.textContent = expanded ? "Collapse ↑" : "Read the full essay ↗";
                    (readMore.closest(".answer-card--recital") ?? messageWrapEl!).classList.toggle("recital-expanded", expanded);
                  });
                  existingBubble.appendChild(readMore);
                }
              }
            } else {
              // Tab-card shell: render full card off-DOM and fill panels in-place
              const renderedCard = renderAnswerCard(fullCard, false, {
                onFollowupClick: (q) => sendMessage(q),
                sourceConfidenceStrip: (data.source_confidence_strip ?? "").trim() || undefined,
                showConfidenceBadge: data.status !== "clarification" && data.status !== "refinement_ask",
                suppressFollowups: nextQuestions.length > 0,
                nextQuestions,
                qcAudit: qcFromPayload,
                suppressConfidenceForAdminQcFail: suppressConf,
                corrections: _extractedCorrections,
                nextStepTasks: _extractedNextStepTasks,
              });
              const renderedBubble = renderedCard.querySelector(".answer-card-bubble");

              if (renderedBubble) {
                // Swap streaming tab bar with fully-built one (count badges, correct empty state)
                const streamingTabBar = existingBubble.querySelector(".ac-tab-bar");
                const renderedTabBar = renderedBubble.querySelector(".ac-tab-bar");
                if (streamingTabBar && renderedTabBar) {
                  existingBubble.replaceChild(renderedTabBar, streamingTabBar);
                }

                // Summary panel: keep streaming prose, append sections/meta/confidence from rendered card.
                // mkTab now uses querySelector (not closure ref), so existingSummaryPanel can stay in place.
                const existingSummaryPanel = existingBubble.querySelector(".ac-tab-panel--summary") as HTMLElement | null;
                const renderedSummaryPanel = renderedBubble.querySelector(".ac-tab-panel--summary") as HTMLElement | null;
                if (existingSummaryPanel && renderedSummaryPanel) {
                  Array.from(renderedSummaryPanel.children).forEach((child) => {
                    existingSummaryPanel.appendChild(child);
                  });
                }

                // Swap Citations, Corrections, Follow-up, Tasks panels in-place
                (["citations", "corrections", "next-steps", "tasks"] as const).forEach((panelName) => {
                  const existing = existingBubble.querySelector(`.ac-tab-panel--${panelName}`) as HTMLElement | null;
                  const rendered = renderedBubble.querySelector(`.ac-tab-panel--${panelName}`) as HTMLElement | null;
                  if (existing && rendered) existingBubble.replaceChild(rendered, existing);
                });

                // Hoist answer-card-actions to turn level
                const actionsEl = renderedCard.querySelector(".answer-card-actions");
                if (actionsEl) turnWrap.appendChild(actionsEl);

                // Hoist any inline action chips that ended up inside the bubble
                // (e.g. from direct_answer text or sections, not from suggested_actions)
                const inlineChips = Array.from(existingBubble.querySelectorAll(".answer-card-action-chip"));
                if (inlineChips.length > 0) {
                  let hoistWrap = turnWrap.querySelector(".answer-card-actions") as HTMLElement | null;
                  if (!hoistWrap) {
                    hoistWrap = document.createElement("div");
                    hoistWrap.className = "answer-card-actions";
                    turnWrap.appendChild(hoistWrap);
                  }
                  inlineChips.forEach((chip) => hoistWrap!.appendChild(chip));
                }
              }
            }

          } else if (existingBubble) {
            // No AnswerCard (error, clarify, stub) — demote card shell to plain bubble.
            // Strip card classes so it renders like a normal assistant message.
            messageWrapEl.classList.remove("answer-card");
            Array.from(messageWrapEl.classList)
              .filter((c) => c.startsWith("answer-card--"))
              .forEach((c) => messageWrapEl!.classList.remove(c));
            existingBubble.classList.remove("answer-card-bubble");
            existingBubble.querySelector(".ac-tab-bar")?.remove();
            // If prose shows placeholder "Formatting answer…" text, replace with actual content
            const prose = existingBubble.querySelector(".ac-summary-prose") as HTMLElement | null;
            if (prose && contentToShow && contentToShow !== "Formatting answer…") {
              prose.innerHTML = simpleMarkdownToHtml(sanitizeDisplayMessage(contentToShow));
            }
            if (data.status !== "clarification" && !suppressConf) {
              const badgeEl = renderConfidenceBadge((data.source_confidence_strip ?? "").trim() || "informational_only");
              existingBubble.insertBefore(badgeEl, existingBubble.firstChild);
            }
          }

          // Envelope blocks — functional blocks (task_list, document_download, etc.)
          // Tab-chrome blocks already captured above; suppress them here
          if (useEnvelope && existingBubble) {
            const _hasTabs = !!(fullCard && (
              (fullCard.citations && fullCard.citations.length > 0) ||
              _extractedCorrections.length > 0 ||
              _extractedNextStepTasks.length > 0 ||
              nextQuestions.length > 0
            ));
            const _suppressedChrome = new Set(
              _hasTabs ? ["tool_attribution", "detail", "callout", "correction", "next_steps"] : []
            );
            const toolBlocks = (envCandidate as AssistantEnvelope).blocks.filter((b) => {
              const bt = (b as EnvelopeBlock).type;
              return bt !== "direct_answer" && bt !== "sources" && !_suppressedChrome.has(bt);
            });
            if (toolBlocks.length > 0) {
              const toolEnv: AssistantEnvelope = { ...(envCandidate as AssistantEnvelope), blocks: toolBlocks };
              const toolRendered = renderAssistantFromEnvelope(toolEnv, {
                onFollowupClick: (q) => sendMessage(q),
                sourceConfidenceStrip: (data.source_confidence_strip ?? "").trim() || undefined,
                showConfidenceBadge: false,
                qcAudit: qcFromPayload,
                correlationId: cidForTurn || null,
                suppressConfidenceForAdminQcFail: suppressConf,
                threadId: data.thread_id ?? currentThreadId ?? null,
              });
              const innerBubble = toolRendered.querySelector(".message-bubble");
              if (innerBubble) {
                Array.from(innerBubble.children).forEach((child) => existingBubble.appendChild(child));
              }
            }
          }

          messageWrapEl.querySelectorAll(".envelope-takeaways").forEach((el) => el.remove());
          turnWrap.classList.add("turn-meta-revealing");
          window.setTimeout(() => turnWrap.classList.remove("turn-meta-revealing"), 1200);
        } else {
          if (messageWrapEl) messageWrapEl.remove();
          if (useEnvelope) {
            turnWrap.appendChild(
              renderAssistantFromEnvelope(envCandidate as AssistantEnvelope, {
                onFollowupClick: (q) => sendMessage(q),
                sourceConfidenceStrip: (data.source_confidence_strip ?? "").trim() || undefined,
                showConfidenceBadge: data.status !== "clarification" && data.status !== "refinement_ask",
                qcAudit: qcFromPayload,
                correlationId: cidForTurn || null,
                suppressConfidenceForAdminQcFail: suppressConf,
                threadId: data.thread_id ?? currentThreadId ?? null,
              })
            );
          } else if (data.response_source === "content_filtered") {
            // Content-safety block: amber notice, no answer card, no sources.
            turnWrap.appendChild(
              renderAssistantMessage(contentToShow, false, { variant: "warn" })
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
        if (
          !envelopeHasPipelineGate &&
          credCop &&
          typeof credCop === "object" &&
          typeof credCop.run_id === "string" &&
          credCop.run_id.length > 0
        ) {
          turnWrap.appendChild(renderCredentialingCopilotPanel(credCop as CredentialingCopilotPayload, data.thread_id ?? currentThreadId));
        }

        // 5b. Roster report download (PDF and/or Markdown)
        const pdfBase64 = data.roster_report_pdf_base64;
        const reportMarkdown = data.roster_report_final_md;
        const attachmentsKind: "reconciliation" | "credentialing" | undefined =
          data.roster_report_attachments_kind === "reconciliation"
            ? "reconciliation"
            : data.roster_report_attachments_kind === "credentialing"
              ? "credentialing"
              : undefined;
        if ((pdfBase64 && typeof pdfBase64 === "string" && pdfBase64.length > 0) || (reportMarkdown && typeof reportMarkdown === "string" && reportMarkdown.trim().length > 0)) {
          turnWrap.appendChild(renderRosterReportDownload(pdfBase64, reportMarkdown, attachmentsKind));
        }

        // 6. Follow-up suggestions: always go to #chat-suggestions above composer (never inline)
        const isCard = !!tryParseAnswerCard(body || "");
        if (nextQuestions.length > 0) {
          updateChatSuggestions(nextQuestions, (q) => sendMessage(q));
        }

        // 7. Clarification options (clickable buttons for slot fill)
        if (data.clarification_options && data.clarification_options.length > 0) {
          turnWrap.appendChild(renderClarificationOptions(data.clarification_options));
        } else {
          activeClarificationDraft = null;
        }

        // Hoist answer-card-actions to turn level (outside bubble, before sources row)
        const hoistAct = turnWrap.querySelector(".answer-card-actions");
        if (hoistAct) turnWrap.appendChild(hoistAct);

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
        if (sourceList.length > 0 && (!envelopeHasSources || isStreamingCard)) {
          turnWrap.appendChild(
            renderSourceCiter(sourceList, cited, data.correlation_id ?? activeCorrelationId)
          );
        }

        const insightRows = data.usage_breakdown;
        const perfMeta = data.llm_performance;
        // Consume pending HIPAA/PHI diagnostics for this turn regardless of admin mode
        const hipaaForTab = _pendingHipaaDiagnostics;
        _pendingHipaaDiagnostics = null;
        const msgPhiGateForTab = _pendingMsgPhiGate;
        _pendingMsgPhiGate = null;
        if (
          getShowLlmPerformance(cachedProfile) &&
          data.status === "completed"
        ) {
          const tin = Number(data.tokens_used?.input_tokens) || 0;
          const tout = Number(data.tokens_used?.output_tokens) || 0;
          const cardBubble = messageWrapEl?.querySelector(".answer-card-bubble") as HTMLElement | null;

          if (isStreamingCard && cardBubble) {
            // Admin path A: inject Diagnostics tab into the answer card
            _injectDiagnosticsTab(cardBubble, {
              insightRows: Array.isArray(insightRows) ? insightRows : [],
              perfMeta,
              thinkingLog: data.thinking_log,
              qc: qcFromPayload,
              sourceConfidenceStrip: data.source_confidence_strip ?? null,
              correlationId: data.correlation_id ?? activeCorrelationId,
              totalCostFallback: data.cost_usd,
              inputTokens: tin,
              outputTokens: tout,
              routingFeedback: data.technical_feedback?.llm_performance ?? null,
              hipaaDiagnostics: hipaaForTab,
              msgPhiGate: msgPhiGateForTab,
            });
          } else if (Array.isArray(insightRows) && insightRows.length > 0) {
            // Admin path B: non-card turn — keep panels below the bubble as before
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
            const retrievalPanel = renderDiagnosticsCard(data.thinking_log);
            if (retrievalPanel) turnWrap.appendChild(retrievalPanel);
          }
        }

        mergeTechnicalPanels(turnWrap, data);
        mergeLlmPerformanceRoutingHydrate(turnWrap, data);

        // 9. Answer-quality feedback (separate from LLM routing thumbs in performance panel)
        turnWrap.appendChild(renderFeedback(data.correlation_id ?? activeCorrelationId));

        // 10. product_feedback capture-card (inline skill fired this turn)
        if (data.capture_card) {
          turnWrap.appendChild(renderCaptureCard(data.capture_card, {
            threadId: data.thread_id,
            correlationId: data.correlation_id ?? activeCorrelationId,
          }));
        }

        // 11. Planner-driven periodic survey chip (NPS / CSAT / open)
        if (data.offer_feedback) {
          turnWrap.appendChild(renderOfferFeedback(data.offer_feedback, {
            threadId: data.thread_id,
            correlationId: data.correlation_id ?? activeCorrelationId,
          }));
        }

        // 12. Product Awareness interactive demo chip
        if (data.demo) {
          turnWrap.appendChild(renderDemoChip(data.demo, {
            correlationId: data.correlation_id ?? activeCorrelationId,
          }));
        }

        loadSidebarHistory();
        scrollToBottom(messagesEl);
      })
      .catch((err: Error) => {
        markRequestFailed();
        thinkingDone(thinkingLines.length);
        turnWrap.appendChild(
          renderAssistantMessage("Error: " + (err?.message ?? String(err)), true, {})
        );
        scrollToBottom(messagesEl);
      })
      .finally(() => {
        releaseComposer(); // no-op if draft already released; handles polling fallback + clarifications
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

  // ─────────────────────────────────────────────────────────────────────
  // Phase B.1a — inline attach-to-send
  //
  // Staging-then-send pattern: clicking the paperclip stages a file
  // (shows chip), the actual upload happens when Send is pressed. The
  // upload and the user's question fire as one perceived interaction.
  // The existing upload endpoint handles ingest inline; by the time it
  // resolves, the document_id is already in thread state so the next
  // chat turn will auto-resolve it via search_uploaded_document.
  // ─────────────────────────────────────────────────────────────────────
  let composerStagedFile: File | null = null;
  const composerAttachBtn = document.getElementById("composerAttach") as HTMLButtonElement | null;
  const composerAttachmentInput = document.getElementById("composerAttachmentInput") as HTMLInputElement | null;
  const composerAttachmentChip = document.getElementById("composerAttachmentChip") as HTMLElement | null;
  const composerAttachmentChipName = document.getElementById("composerAttachmentChipName") as HTMLElement | null;
  const composerAttachmentChipRemove = document.getElementById("composerAttachmentChipRemove") as HTMLButtonElement | null;

  function showComposerAttachment(file: File): void {
    composerStagedFile = file;
    if (composerAttachmentChipName) composerAttachmentChipName.textContent = file.name;
    if (composerAttachmentChip) composerAttachmentChip.hidden = false;
    if (composerAttachBtn) composerAttachBtn.setAttribute("aria-pressed", "true");
  }
  function clearComposerAttachment(): void {
    composerStagedFile = null;
    if (composerAttachmentChip) {
      composerAttachmentChip.hidden = true;
      composerAttachmentChip.classList.remove("is-uploading");
    }
    if (composerAttachmentInput) composerAttachmentInput.value = "";
    if (composerAttachBtn) composerAttachBtn.removeAttribute("aria-pressed");
  }

  composerAttachBtn?.addEventListener("click", () => {
    composerAttachmentInput?.click();
  });

  // Peek the first line of a CSV to detect roster-like column headers.
  function _looksLikeRosterCsv(firstLine: string): boolean {
    const ROSTER_COLS = ["npi", "provider_name", "license_type", "license_number", "specialty", "taxonomy"];
    const lower = firstLine.toLowerCase();
    return ROSTER_COLS.filter((c) => lower.includes(c)).length >= 2;
  }

  composerAttachmentInput?.addEventListener("change", (e) => {
    const f = (e.target as HTMLInputElement).files?.[0];
    if (!f) {
      clearComposerAttachment();
      return;
    }
    // Server hard cap is 100 MB. Soft-warn at 25 MB — processing may be
    // slow (>15s) and will fall to background notify, not foreground bar.
    const WARN_BYTES = 25 * 1024 * 1024;
    const MAX_BYTES = 100 * 1024 * 1024;
    if (f.size > MAX_BYTES) {
      alert(`File too large (${Math.round(f.size / 1024 / 1024)} MB). Maximum is 100 MB.`);
      clearComposerAttachment();
      return;
    }
    if (f.size > WARN_BYTES) {
      const ok = window.confirm(
        `This file is ${Math.round(f.size / 1024 / 1024)} MB — processing will take ~${Math.round(f.size / (1024*1024*2))} min in the background. Continue?`,
      );
      if (!ok) { clearComposerAttachment(); return; }
    }
    // CSV roster detection — read first line to check for known roster cols.
    const isCsv = f.name.toLowerCase().endsWith(".csv") || f.type === "text/csv";
    if (isCsv) {
      const reader = new FileReader();
      reader.onload = (ev) => {
        const firstLine = ((ev.target?.result as string) || "").split(/\r?\n/)[0] || "";
        if (_looksLikeRosterCsv(firstLine)) {
          const chip = document.getElementById("composerAttachmentChip");
          if (chip) {
            let hint = chip.querySelector<HTMLElement>(".composer-attach-roster-hint");
            if (!hint) {
              hint = document.createElement("span");
              hint.className = "composer-attach-roster-hint";
              chip.appendChild(hint);
            }
            hint.textContent = "Looks like a roster — use Credentialing to reconcile.";
          }
        }
      };
      reader.readAsText(f.slice(0, 512));
    }
    showComposerAttachment(f);
    inputEl?.focus();
  });

  composerAttachmentChipRemove?.addEventListener("click", () => clearComposerAttachment());

  // Drag-and-drop onto the composer-wrap stages the first file, routed
  // through the same change handler so size-guard + chip rendering
  // stays in one place.
  const composerWrap = document.querySelector(".composer-wrap") as HTMLElement | null;
  if (composerWrap) {
    const stop = (e: Event) => { e.preventDefault(); e.stopPropagation(); };
    (["dragenter", "dragover"] as const).forEach((evt) =>
      composerWrap.addEventListener(evt, (e) => {
        stop(e);
        composerWrap.classList.add("composer-wrap--dragover");
      }),
    );
    (["dragleave", "drop"] as const).forEach((evt) =>
      composerWrap.addEventListener(evt, (e) => {
        stop(e);
        composerWrap.classList.remove("composer-wrap--dragover");
      }),
    );
    composerWrap.addEventListener("drop", (e) => {
      const f = (e as DragEvent).dataTransfer?.files?.[0];
      if (!f) return;
      if (composerAttachmentInput) {
        const dt = new DataTransfer();
        dt.items.add(f);
        composerAttachmentInput.files = dt.files;
        composerAttachmentInput.dispatchEvent(new Event("change"));
      }
    });
  }

  // ── Large-file confirm gate ───────────────────────────────────────────
  //
  // Above this size, show a modal before upload so the user can choose
  // between instant (wait 30-60s) and batch (queued, coming soon in B.7).
  // 500KB ≈ 10-15 pages of text-heavy PDF — matches the user's intuition
  // of what counts as "large enough to warrant a prompt." Image-heavy
  // PDFs trip this at fewer pages, but that's fine: the prompt is a
  // heads-up, not a hard rejection.
  const LARGE_FILE_THRESHOLD_BYTES = 500 * 1024;

  // Rough page estimate for the prompt body. Users grok "N pages" better
  // than "N bytes." 4KB/page is the instant-rag skill's chunking unit;
  // for PDFs the real extracted text is ~3-8KB per page but this gives
  // a defensible ballpark for the prompt.
  function estimatePageCount(file: File): number {
    const bytesPerPage = 4 * 1024;
    return Math.max(1, Math.round(file.size / bytesPerPage));
  }

  function showLargeUploadConfirm(file: File): Promise<"instant" | "batch" | "cancel"> {
    return new Promise((resolve) => {
      const overlay = document.getElementById("largeUploadOverlay") as HTMLElement | null;
      const modal = document.getElementById("largeUploadModal") as HTMLElement | null;
      const bodyEl = document.getElementById("largeUploadModalBody") as HTMLElement | null;
      const proceedInstant = document.getElementById("largeUploadProceedInstant") as HTMLButtonElement | null;
      const proceedBatch = document.getElementById("largeUploadProceedBatch") as HTMLButtonElement | null;
      const cancelBtn = document.getElementById("largeUploadCancel") as HTMLButtonElement | null;
      // Defensive: if the modal DOM is missing (older cached HTML), fall
      // through to instant without blocking the user.
      if (!modal || !overlay || !proceedInstant || !cancelBtn) {
        resolve("instant");
        return;
      }
      const sizeMb = (file.size / (1024 * 1024)).toFixed(1);
      const pages = estimatePageCount(file);
      if (bodyEl) {
        bodyEl.innerHTML =
          `"<strong>${file.name}</strong>" is <strong>${sizeMb} MB</strong> ` +
          `(roughly <strong>${pages} pages</strong>). "Upload now" gets it ` +
          `ready to search in this chat — typically ` +
          `<strong>30 to 60 seconds</strong> for a document this size.` +
          `<br><br>` +
          `"Queue for batch processing" adds the doc to your permanent ` +
          `library so it's searchable from any chat. Coming soon.`;
      }
      const cleanup = () => {
        modal.setAttribute("hidden", "");
        overlay.classList.remove("open");
        proceedInstant.removeEventListener("click", onInstant);
        proceedBatch?.removeEventListener("click", onBatch);
        cancelBtn.removeEventListener("click", onCancel);
        overlay.removeEventListener("click", onCancel);
        document.removeEventListener("keydown", onKey);
      };
      const onInstant = () => { cleanup(); resolve("instant"); };
      const onBatch = () => { cleanup(); resolve("batch"); };
      const onCancel = () => { cleanup(); resolve("cancel"); };
      const onKey = (e: KeyboardEvent) => {
        if (e.key === "Escape") onCancel();
        if (e.key === "Enter") onInstant();
      };
      proceedInstant.addEventListener("click", onInstant);
      proceedBatch?.addEventListener("click", onBatch);
      cancelBtn.addEventListener("click", onCancel);
      overlay.addEventListener("click", onCancel);
      document.addEventListener("keydown", onKey);
      modal.removeAttribute("hidden");
      overlay.classList.add("open");
      // Focus the primary action so Enter confirms.
      proceedInstant.focus();
    });
  }

  // Phase-emit timers for the composer upload. Parallels the upload-modal
  // progression the user already sees in ⋯ → Upload file, but routed
  // through the chat status banner instead of the modal's status field.
  // §4 foreground progress strip — UX-authored design, wired to live SSE bridge.
  const FOREGROUND_CUTOFF_S = 12;       // UX-finalized value
  let _ragProgressEs: EventSource | null = null;
  let _ragProgressCutoffTimer: ReturnType<typeof setTimeout> | null = null;

  const _STAGE_MICROCOPY: Record<string, string> = {
    queued:     "Queued…",
    extracting: "Extracting pages…",
    chunking:   "Splitting into chunks…",
    embedding:  "Indexing…",
    publishing: "Almost ready…",
    ready:      "Ready ✓",
  };

  function _stageMicrocopy(stage: string, chunks_done?: number, chunks_total?: number): string {
    if (stage === "chunking" && typeof chunks_done === "number" && typeof chunks_total === "number" && chunks_total > 0) {
      return `Chunking · ${chunks_done}/${chunks_total}`;
    }
    return _STAGE_MICROCOPY[stage] ?? stage;
  }

  function _closeRagProgressStrip(): void {
    if (_ragProgressEs) { _ragProgressEs.close(); _ragProgressEs = null; }
    if (_ragProgressCutoffTimer !== null) { clearTimeout(_ragProgressCutoffTimer); _ragProgressCutoffTimer = null; }
    document.getElementById("ragProgressStrip")?.classList.add("rag-progress-strip--collapsed");
  }

  function _showHipaaDiagnosticsBubble(d: {
    gate: string; phi_flag: boolean;
    evidence_categories: string[]; identifier_labels: string[];
    hipaa_mode_allowed: boolean; action_taken: string;
    reason: string; transaction_id: string; document_name: string;
  }): void {
    const anchor = document.querySelector(".composer-wrap");
    if (!anchor || !anchor.parentElement) return;

    const bubble = document.createElement("div");
    const isBlocked = d.action_taken === "blocked_phi" || d.action_taken === "blocked_indeterminate";
    const isPrivate = d.action_taken === "published_private";
    bubble.className = "hipaa-diag-bubble" +
      (isBlocked && d.gate === "phi" ? " hipaa-diag-bubble--phi" : "") +
      (isBlocked && d.gate === "indeterminate" ? " hipaa-diag-bubble--indeterminate" : "") +
      (isPrivate ? " hipaa-diag-bubble--private" : "");

    const icon = document.createElement("span");
    icon.className = "hipaa-diag-icon";
    icon.setAttribute("aria-hidden", "true");
    icon.textContent = isBlocked && d.gate === "phi" ? "🛡✗" : isBlocked ? "⚠" : "🔒";

    const body = document.createElement("div");
    body.className = "hipaa-diag-body";

    const title = document.createElement("div");
    title.className = "hipaa-diag-title";
    if (isBlocked && d.gate === "phi") {
      title.textContent = `"${d.document_name}" contains PHI — not stored`;
    } else if (isBlocked) {
      title.textContent = `"${d.document_name}" couldn't be verified — not stored`;
    } else {
      title.textContent = `"${d.document_name}" stored in your private vault`;
    }
    body.appendChild(title);

    if (isBlocked) {
      const msg = document.createElement("div");
      msg.className = "hipaa-diag-msg";
      if (d.gate === "phi") {
        msg.textContent = "This document contains protected health information and cannot be processed in the current mode. It was not stored.";
      } else {
        msg.textContent = "We couldn't verify this document's safety right now. It was not stored. Please try again shortly.";
      }
      body.appendChild(msg);
    } else if (isPrivate) {
      const msg = document.createElement("div");
      msg.className = "hipaa-diag-msg";
      msg.textContent = "PHI found — stored privately (not shared to the corpus).";
      body.appendChild(msg);
    }

    // Evidence pills (masked category labels — never raw values)
    const labels = d.identifier_labels.length ? d.identifier_labels : d.evidence_categories;
    if (labels.length > 0 && d.gate === "phi") {
      const pills = document.createElement("div");
      pills.className = "hipaa-diag-pills";
      labels.slice(0, 8).forEach((lbl) => {
        const pill = document.createElement("span");
        pill.className = "hipaa-diag-pill";
        pill.textContent = lbl;
        pills.appendChild(pill);
      });
      body.appendChild(pills);
    }

    // Diagnostics chrome (gate badge, HIPAA mode, txn id)
    const chrome = document.createElement("div");
    chrome.className = "hipaa-diag-chrome";
    const gateBadge = document.createElement("span");
    gateBadge.className = `hipaa-diag-gate hipaa-diag-gate--${d.gate}`;
    gateBadge.textContent = `gate: ${d.gate}`;
    chrome.appendChild(gateBadge);
    const modeBadge = document.createElement("span");
    modeBadge.className = "hipaa-diag-mode";
    modeBadge.textContent = `HIPAA mode: ${d.hipaa_mode_allowed ? "ON" : "OFF"}`;
    chrome.appendChild(modeBadge);
    if (d.transaction_id) {
      const txn = document.createElement("span");
      txn.className = "hipaa-diag-txn";
      txn.textContent = `txn ${d.transaction_id.slice(0, 8)}`;
      chrome.appendChild(txn);
    }
    body.appendChild(chrome);

    const dismiss = document.createElement("button");
    dismiss.type = "button";
    dismiss.className = "hipaa-diag-dismiss";
    dismiss.setAttribute("aria-label", "Dismiss");
    dismiss.innerHTML = "&times;";
    dismiss.addEventListener("click", () => bubble.remove());

    bubble.appendChild(icon);
    bubble.appendChild(body);
    bubble.appendChild(dismiss);
    anchor.parentElement.insertBefore(bubble, anchor);

    if (!isBlocked) setTimeout(() => bubble.remove(), 30_000);
  }

  function _showPhiRecommendationCard(filename: string, documentId: string): void {
    if (document.querySelector(".phi-rec-card")) return;
    const anchor = document.querySelector(".composer-wrap");
    if (!anchor?.parentElement) return;

    const card = document.createElement("div");
    card.className = "phi-rec-card phi-rec-card--checking";

    const label = document.createElement("span");
    label.className = "phi-rec-card__label";
    label.textContent = "Checking document sensitivity…";

    const actions = document.createElement("span");
    actions.className = "phi-rec-card__actions";

    const dismissBtn = document.createElement("button");
    dismissBtn.type = "button";
    dismissBtn.className = "reminder-nudge-dismiss";
    dismissBtn.setAttribute("aria-label", "Dismiss");
    dismissBtn.innerHTML = "&times;";
    dismissBtn.addEventListener("click", () => card.remove());

    card.appendChild(label);
    card.appendChild(actions);
    card.appendChild(dismissBtn);
    anchor.parentElement.insertBefore(card, anchor);

    function _render(row: Record<string, unknown>): void {
      const phiFlag = Boolean(row["phi_flag"]);
      const vis = String(row["suggested_visibility"] || "private");
      const evidence = (row["phi_evidence"] as Array<{category: string}> | null) || [];

      card.className = "phi-rec-card";

      if (phiFlag || vis === "private") {
        card.classList.add("phi-rec-card--phi");
        label.textContent = "⚠ Contains patient information — kept private.";
        if (evidence.length > 0) {
          const chips = document.createElement("span");
          chips.className = "phi-rec-card__chips";
          const seen = new Set<string>();
          for (const ev of evidence.slice(0, 6)) {
            const cat = String((ev as {category: string}).category || "").replace(/_/g, " ");
            if (!cat || seen.has(cat)) continue;
            seen.add(cat);
            const chip = document.createElement("span");
            chip.className = "phi-rec-card__chip";
            chip.textContent = cat;
            chips.appendChild(chip);
          }
          card.insertBefore(chips, actions);
        }
        const keepBtn = document.createElement("button");
        keepBtn.type = "button";
        keepBtn.className = "phi-rec-card__action phi-rec-card__action--primary";
        keepBtn.textContent = "Keep private";
        keepBtn.addEventListener("click", () => card.remove());
        actions.appendChild(keepBtn);

      } else if (vis === "org") {
        card.classList.add("phi-rec-card--org");
        label.textContent = "🏢 Shareable with your org.";
        const keepBtn = document.createElement("button");
        keepBtn.type = "button";
        keepBtn.className = "phi-rec-card__action phi-rec-card__action--secondary";
        keepBtn.textContent = "Keep private";
        keepBtn.addEventListener("click", () => card.remove());
        const shareBtn = document.createElement("button");
        shareBtn.type = "button";
        shareBtn.className = "phi-rec-card__action phi-rec-card__action--primary";
        shareBtn.textContent = "Share with org";
        shareBtn.setAttribute("disabled", "");
        shareBtn.title = "Coming soon";
        actions.appendChild(shareBtn);
        actions.appendChild(keepBtn);

      } else {
        card.classList.add("phi-rec-card--clean");
        label.textContent = "✓ No sensitive info found — safe to share.";
        const shareBtn = document.createElement("button");
        shareBtn.type = "button";
        shareBtn.className = "phi-rec-card__action phi-rec-card__action--primary";
        shareBtn.textContent = "Make public";
        shareBtn.setAttribute("disabled", "");
        shareBtn.title = "Coming soon — promote actions in P2";
        const keepBtn = document.createElement("button");
        keepBtn.type = "button";
        keepBtn.className = "phi-rec-card__action phi-rec-card__action--secondary";
        keepBtn.textContent = "Keep private";
        keepBtn.addEventListener("click", () => card.remove());
        actions.appendChild(shareBtn);
        actions.appendChild(keepBtn);
      }

      setTimeout(() => card.remove(), 60_000);
    }

    let attempts = 0;
    async function _poll(): Promise<void> {
      attempts++;
      try {
        const resp = await apiFetch(`${API_BASE}/chat/uploads/${documentId}`);
        if (resp.ok) {
          const row = await resp.json() as Record<string, unknown>;
          if (row["classified_at"]) { _render(row); return; }
        }
      } catch (_e) { /* ignore */ }
      if (attempts >= 10) { card.remove(); return; }
      setTimeout(() => { void _poll(); }, 3000);
    }
    setTimeout(() => { void _poll(); }, 3000);
  }

  function _showReadyNudge(filename: string, documentId: string, threadId: string): void {
    if (document.querySelector(".rag-ready-nudge")) return; // already showing
    const anchor = document.querySelector(".composer-wrap");
    if (!anchor || !anchor.parentElement) return;

    const chip = document.createElement("div");
    chip.className = "reminder-nudge rag-ready-nudge";
    const label = document.createElement("span");
    label.className = "reminder-nudge-label";
    label.textContent = `📄 "${filename}" is ready`;
    const askBtn = document.createElement("button");
    askBtn.type = "button";
    askBtn.className = "reminder-nudge-view";
    askBtn.textContent = "Ask now";
    askBtn.addEventListener("click", () => {
      chip.remove();
      if (threadId && currentThreadId !== threadId) {
        // Navigate to origin thread then populate — simplified for P0
      }
      const inputEl = document.getElementById("input") as HTMLInputElement | null;
      if (inputEl && !inputEl.value.trim()) {
        inputEl.value = `Tell me about "${filename}"`;
        inputEl.dispatchEvent(new Event("input"));
        inputEl.focus();
      }
    });
    const dismissBtn = document.createElement("button");
    dismissBtn.type = "button";
    dismissBtn.className = "reminder-nudge-dismiss";
    dismissBtn.setAttribute("aria-label", "Dismiss");
    dismissBtn.innerHTML = "&times;";
    dismissBtn.addEventListener("click", () => chip.remove());

    chip.appendChild(label);
    chip.appendChild(askBtn);
    chip.appendChild(dismissBtn);
    anchor.parentElement.insertBefore(chip, anchor);
  }

  function _openRagProgressStrip(filename: string, progressChannel: string, documentId: string, threadId: string): void {
    const strip = document.getElementById("ragProgressStrip");
    const bar   = document.getElementById("ragProgressBar") as HTMLElement | null;
    const name  = document.getElementById("ragProgressName");
    const stage = document.getElementById("ragProgressStage");
    const action = document.getElementById("ragProgressAction") as HTMLButtonElement | null;
    const closeBtn = document.getElementById("ragProgressClose") as HTMLButtonElement | null;
    if (!strip) return;

    // Reset
    if (bar)   { bar.style.width = "0%"; bar.className = "rag-progress-strip__bar"; }
    if (name)  name.textContent = `📄 ${filename}`;
    if (stage) stage.textContent = "Queued…";
    if (action) { action.setAttribute("hidden", ""); action.onclick = null; }
    strip.classList.remove("rag-progress-strip--collapsed");

    const _escape = (toBackground: boolean) => {
      _closeRagProgressStrip();
      if (toBackground) _showToast(`"${filename}" is processing — I'll let you know when it's ready`);
    };

    if (closeBtn) {
      closeBtn.onclick = () => _escape(true);
    }

    const es = new EventSource(API_BASE + progressChannel);
    _ragProgressEs = es;

    es.onmessage = (evt: MessageEvent) => {
      try {
        const p = JSON.parse(evt.data) as {
          stage?: string; pct?: number; chunks_done?: number; chunks_total?: number;
          chunks_count?: number; error?: string; retryable?: boolean; terminal?: boolean;
        };
        const pct = typeof p.pct === "number" ? Math.min(100, Math.max(0, p.pct)) : null;
        if (bar && pct !== null) bar.style.width = `${pct}%`;
        if (stage) stage.textContent = _stageMicrocopy(p.stage ?? "", p.chunks_done, p.chunks_total);
        if (!p.terminal) return;

        _ragProgressEs = null;
        es.close();
        if (_ragProgressCutoffTimer !== null) { clearTimeout(_ragProgressCutoffTimer); _ragProgressCutoffTimer = null; }

        if (p.stage === "ready") {
          if (bar)   { bar.style.width = "100%"; bar.classList.add("rag-progress-strip__bar--ready"); }
          if (stage) stage.textContent = "Ready ✓";
          if (action) action.setAttribute("hidden", "");
          // Collapse strip after 400ms (bar transition) + populate composer.
          window.setTimeout(() => {
            strip.classList.add("rag-progress-strip--collapsed");
            const inputEl = document.getElementById("input") as HTMLInputElement | null;
            if (inputEl && !inputEl.value.trim()) {
              inputEl.value = `Tell me about "${filename}"`;
              inputEl.dispatchEvent(new Event("input"));
              inputEl.focus();
            }
          }, 700);
        } else {
          // failed
          if (bar) bar.classList.add("rag-progress-strip__bar--failed");
          if (stage) stage.textContent = p.error ? `Couldn't process · ${p.error}` : "Couldn't process";
          if (action) {
            action.removeAttribute("hidden");
            if (p.retryable !== false) {
              action.textContent = "Retry";
              action.onclick = async () => {
                action.setAttribute("hidden", "");
                if (stage) stage.textContent = "Retrying…";
                try {
                  const retryResp = await apiFetch(`${API_BASE}/documents/${documentId}/retry`, { method: "POST" });
                  if (!retryResp.ok) throw new Error(`${retryResp.status}`);
                  const retryData = await retryResp.json();
                  const retryChannel = String((retryData as any).progress_channel || "");
                  if (retryChannel) {
                    _openRagProgressStrip(filename, retryChannel, documentId, threadId);
                  } else {
                    _showToast(`Retry queued for "${filename}" — I'll let you know when it's ready`);
                    _closeRagProgressStrip();
                  }
                } catch (_e) {
                  if (stage) stage.textContent = "Retry failed — try again";
                  action.removeAttribute("hidden");
                }
              };
            } else {
              action.textContent = "Remove";
              action.onclick = () => { _closeRagProgressStrip(); };
            }
          }
        }
      } catch (_e) { /* ignore parse errors */ }
    };

    es.onerror = () => {
      _closeRagProgressStrip();
      _showToast(`"${filename}" is processing — I'll let you know when it's ready`);
    };

    // Mid-progress escape at cutoff.
    _ragProgressCutoffTimer = window.setTimeout(() => {
      if (_ragProgressEs) _escape(false); // silent drop — SSE replays on re-subscribe
    }, FOREGROUND_CUTOFF_S * 1000);
  }

  // Without these, the user sees only a pulsing chip and can't tell
  // whether the 30-60s pause is progress or a hang.
  let composerUploadPhaseTimers: ReturnType<typeof setTimeout>[] = [];
  function stopComposerUploadPhaseEmits(): void {
    composerUploadPhaseTimers.forEach((id) => window.clearTimeout(id));
    composerUploadPhaseTimers = [];
    hideChatStatusBanner();
  }
  function startComposerUploadPhaseEmits(filename: string): void {
    stopComposerUploadPhaseEmits();
    // Phase messages are user-facing, not developer-facing. Each one
    // answers the question a user actually has ("is this still working?")
    // without exposing implementation terms like chunks/embeddings/RAG.
    // The skill's pipeline has four stages under the hood (extract,
    // chunk, embed, publish) but users experience it as one wait — so
    // the messages collapse to a single narrative arc.
    //
    // Timing is time-gated rather than progress-driven; the skill's
    // /ingest/from-text is a blocking urlopen with no intermediate signals.
    const phases: Array<{ ms: number; text: string }> = [
      { ms: 0,     text: `⏳ Uploading "${filename}"…` },
      { ms: 4000,  text: `⏳ Reading "${filename}"…` },
      { ms: 15000, text: `⏳ Getting "${filename}" ready to search…` },
      { ms: 40000, text: `⏳ Still working on "${filename}" — larger docs take a bit longer…` },
      { ms: 75000, text: `⏳ Almost done with "${filename}"…` },
    ];
    phases.forEach(({ ms, text }) => {
      // autoHideMs=0 keeps each message up until the next phase replaces it
      // or the success/failure handler clears the banner. The real upload
      // completion always runs the cleanup path.
      const id = window.setTimeout(() => showChatStatusBanner(text, 0), ms);
      composerUploadPhaseTimers.push(id);
    });
  }

  async function uploadStagedAttachmentForInstantRag(): Promise<any | null> {
    if (!composerStagedFile) return null;
    const filename = composerStagedFile.name;
    composerAttachmentChip?.classList.add("is-uploading");
    startComposerUploadPhaseEmits(filename);
    try {
      const formData = new FormData();
      formData.append("file", composerStagedFile);
      if (currentThreadId) formData.append("thread_id", currentThreadId);
      const resp = await apiFetch(API_BASE + "/chat/upload", {
        method: "POST",
        body: formData,
      });
      if (!resp.ok) {
        const detail = await resp.json().catch(() => null as any);
        throw new Error(detail?.detail || `Upload failed (${resp.status})`);
      }
      const data = await resp.json();
      if (data.thread_id) currentThreadId = data.thread_id; window.__mobiusChatThreadId = currentThreadId;
      // Success: short user-facing confirmation ("ready — searching now").
      // chunks_count is logged at the debug console for developer
      // diagnostics, but not exposed in the banner because users don't
      // care whether the doc is 9 chunks or 287 chunks — they care that
      // it's ready.
      const chunks = typeof data.chunks_count === "number" ? data.chunks_count : 0;
      if (chunks > 0) {
        console.debug(`[composer-attach] "${filename}" ingested as ${chunks} chunk${chunks === 1 ? "" : "s"}`);
      }
      // §4 foreground vs background. Client decides based on estimated_seconds.
      const etaSecs   = Number((data as any).estimated_seconds) || 0;
      const etaMin    = Number((data as any).eta_minutes) || 0;
      const pageCount = Number((data as any).page_count) || 0;
      const redirectUrl      = String((data as any).redirect_url || "");
      const progressChannel  = String((data as any).progress_channel || "");
      const uploadedDocId    = String((data as any).document_id || "");
      const uploadedThreadId = String((data as any).thread_id || currentThreadId || "");
      const uxPath = String((data as any).ux_path || "blocking");
      const hipaaD = (data as any).hipaa_diagnostics as {
        gate: string; phi_flag: boolean;
        evidence_categories: string[]; identifier_labels: string[];
        hipaa_mode_allowed: boolean; action_taken: string;
        reason: string; transaction_id: string; document_name: string;
      } | undefined;

      // HIPAA gate: blocked path — hard-stop, no PHI card, no progress strip.
      if (uxPath === "blocked" || (data as any).status === "blocked") {
        _showHipaaDiagnosticsBubble(hipaaD ?? {
          gate: (data as any).gate || "indeterminate",
          phi_flag: true,
          evidence_categories: [],
          identifier_labels: [],
          hipaa_mode_allowed: false,
          action_taken: (data as any).action_taken || "blocked_indeterminate",
          reason: "",
          transaction_id: "",
          document_name: filename,
        });
        return data;
      }

      // PHI card: skip when gate already ran (hipaa_diagnostics present) —
      // we have the synchronous verdict; no need to poll. Fall back to the
      // polling card for duplicate/legacy paths that don't run the gate.
      // When hipaaD is present, evict any lingering phi-rec-card from a
      // prior upload so we don't show two promote affordances in one session.
      if (hipaaD) {
        document.querySelector(".phi-rec-card")?.remove();
      }
      if (uploadedDocId && !redirectUrl && !hipaaD) {
        _showPhiRecommendationCard(filename, uploadedDocId);
      } else if (hipaaD && hipaaD.action_taken === "published_private") {
        _showHipaaDiagnosticsBubble(hipaaD);
        // Also surface in the next turn's Diagnostics tab for the full audit trail
        _pendingHipaaDiagnostics = hipaaD;
      } else if (hipaaD && hipaaD.gate === "clean") {
        showChatStatusBanner(`✓ "${filename}" screened — no PHI detected.`, 4000);
        // Surface the full audit in the next turn's Diagnostics tab
        _pendingHipaaDiagnostics = hipaaD;
      }

      if (uxPath === "duplicate") {
        showChatStatusBanner(`✓ "${filename}" is ready — already in our corpus.`, 5000);
      } else if (redirectUrl) {
        const sub = pageCount ? `${pageCount}-page document — ~${etaMin} min` : `~${etaMin} min`;
        showChatStatusBanner(
          `"${filename}" is large (${sub}). Open Mobius RAG → ` +
          `<a href="${redirectUrl}" target="_blank" rel="noopener">${redirectUrl}</a>`,
          20000,
        );
      } else if (progressChannel) {
        // Always open the SSE strip — every upload deserves visible progress.
        // Foreground (small/fast) vs background (large/slow) only governs whether
        // the strip is prominent (blocks composer) or compact (non-blocking).
        // The 12s cutoff timer inside _openRagProgressStrip escapes to background
        // automatically for slow docs — no separate toast path needed.
        stopComposerUploadPhaseEmits();
        _openRagProgressStrip(filename, progressChannel, uploadedDocId, uploadedThreadId);
      } else if (!uploadedDocId) {
        _showToast(`"${filename}" is processing — I'll let you know when it's ready`);
      }
      return data;
    } finally {
      stopComposerUploadPhaseEmits();
      composerAttachmentChip?.classList.remove("is-uploading");
    }
  }

  // Attachment-aware send: when a file is staged, upload first (awaited),
  // synthesize a default question if the input is empty, then fall through
  // to the normal sendMessage flow. The capturing listener below
  // stopImmediatePropagation()s so the bare-send listener registered
  // earlier doesn't also fire and cause a double-send race.
  async function sendMessageWithAttachment(): Promise<void> {
    if (!composerStagedFile) {
      sendMessage();
      return;
    }
    // Large-file gate: prompt BEFORE the upload starts so the user can
    // cancel or defer to a (future) batch path. Small files skip the
    // prompt entirely — the common "small doc + quick ask" flow stays
    // one-click.
    if (composerStagedFile.size > LARGE_FILE_THRESHOLD_BYTES) {
      const choice = await showLargeUploadConfirm(composerStagedFile);
      if (choice === "cancel") {
        // User backed out. Leave the chip in place so they can adjust
        // (pick a different doc, type a different question, or ×).
        return;
      }
      if (choice === "batch") {
        // Batch path is stubbed — the instant-rag skill's
        // /envelope/{id}/promote endpoint returns "promote not yet
        // connected to batch pipeline" today (Phase B.7 future work).
        // Until then, tell the user it's coming and don't proceed.
        showChatStatusBanner(
          `Batch processing isn't available yet. Use "Upload now" to ` +
          `search "${composerStagedFile.name}" in this chat right now.`,
          15000,
        );
        return;
      }
      // choice === "instant" → fall through to the normal upload path.
    }
    // Disable only for the upload phase, NOT for the subsequent sendMessage
    // call. The original sendMessage bails early with `if (sendBtn.disabled)
    // return;` — leaving the button disabled here causes the classic
    // "upload succeeded but chat turn never fired" stuck state (2026-04-17).
    // sendMessage() re-disables both itself during the actual chat turn.
    sendBtn.disabled = true;
    inputEl.disabled = true;
    try {
      const uploadedName = composerStagedFile.name;
      const uploadResult = await uploadStagedAttachmentForInstantRag();
      clearComposerAttachment();
      // HIPAA gate hard-stop: blocked upload must NOT proceed to a chat turn.
      // PHI must never reach the LLM composer — abort here, block bubble already shown.
      if ((uploadResult as any)?.blocked || (uploadResult as any)?.status === "blocked") {
        sendBtn.disabled = false;
        inputEl.disabled = false;
        return;
      }
      const typed = (inputEl.value ?? "").trim();
      const effective = typed || `I just uploaded "${uploadedName}" — what does it say?`;
      if (!typed) inputEl.value = effective;
      // CRITICAL: re-enable both BEFORE calling sendMessage — it has an
      // early return on sendBtn.disabled that would silently drop the
      // user's message. sendMessage() re-disables them itself for the
      // actual in-flight chat turn.
      sendBtn.disabled = false;
      inputEl.disabled = false;
      sendMessage();
    } catch (err: any) {
      console.error("[composer-attach] upload failed:", err);
      // Stop the phase timers so the "still processing" message doesn't
      // flash after the error. Then put the failure in the banner with
      // a longer dwell so the user can read it before it auto-hides.
      stopComposerUploadPhaseEmits();
      const msg = err?.message || String(err);
      showChatStatusBanner(`✗ Couldn't upload "${composerStagedFile?.name ?? 'the document'}": ${msg}`, 20000);
      // Keep the alert too — the banner can be dismissed or missed if
      // the user is looking elsewhere, and upload failure is a hard
      // block that deserves an interrupt.
      alert(`Couldn't upload the document: ${msg}`);
      // Restore BOTH controls — the user needs to be able to edit the
      // message, remove the staged file, and retry. Restoring only the
      // send button but leaving inputEl disabled was the 2026-04-17
      // stuck-state bug that prompted this fix.
      sendBtn.disabled = false;
      inputEl.disabled = false;
    }
  }

  // Capturing listeners that intercept Send/Enter only when a file is
  // staged. Otherwise they no-op and the original non-attach handlers
  // (registered above) run unchanged.
  sendBtn.addEventListener(
    "click",
    (e) => {
      if (!composerStagedFile) return;
      e.stopImmediatePropagation();
      e.preventDefault();
      void sendMessageWithAttachment();
    },
    { capture: true },
  );
  inputEl.addEventListener(
    "keydown",
    (e) => {
      if (e.key !== "Enter" || e.shiftKey) return;
      if (!composerStagedFile) return;
      e.stopImmediatePropagation();
      e.preventDefault();
      void sendMessageWithAttachment();
    },
    { capture: true },
  );

  // ─────────────────────────────────────────────────────────────────────
  // Phase B.1d — restoration banner.
  //
  // When the current thread has no instant_rag uploads but the catalog
  // has recent ones, show a strip above the composer offering one-click
  // "Attach to this chat" for each. No bytes re-uploaded — the click
  // goes through /chat/uploads/{doc_id}/link-to-thread which writes a
  // JSONB reference into the target thread's active.uploaded_files[]
  // so search_uploaded_document finds the same chunks already in
  // Chroma+PG.
  //
  // Fires on: page load, thread creation (currentThreadId becomes truthy).
  // Skips: sessionStorage "dismissed" flag, threads that already have
  // uploads, empty catalog.
  // ─────────────────────────────────────────────────────────────────────
  const uploadRestoreBanner = document.getElementById("uploadRestoreBanner") as HTMLElement | null;
  const uploadRestoreBannerList = document.getElementById("uploadRestoreBannerList") as HTMLElement | null;
  const uploadRestoreBannerDismiss = document.getElementById("uploadRestoreBannerDismiss") as HTMLButtonElement | null;

  // Tracks doc_ids currently being linked so double-clicks don't duplicate.
  const restoreInFlight = new Set<string>();

  function hideRestoreBanner(): void {
    if (uploadRestoreBanner) uploadRestoreBanner.hidden = true;
  }

  function userDismissedRestoreBanner(): boolean {
    try {
      return sessionStorage.getItem("_mobiusRestoreBannerDismissed") === "1";
    } catch {
      return false;
    }
  }

  uploadRestoreBannerDismiss?.addEventListener("click", () => {
    hideRestoreBanner();
    try {
      sessionStorage.setItem("_mobiusRestoreBannerDismissed", "1");
    } catch {
      // sessionStorage can fail in private-mode browsers; banner just
      // re-shows on next navigation — acceptable degradation.
    }
  });

  async function linkUploadToCurrentThread(
    documentId: string,
    filename: string,
    button: HTMLButtonElement,
  ): Promise<void> {
    // Phase B.1d 2026-04-18 fix: on fresh page load, currentThreadId
    // is null until the user sends their first message. Previous
    // version silently returned, making the Attach button feel dead.
    // Now we generate a thread_id client-side if needed; the server's
    // ensure_thread() creates the chat_threads row on first write,
    // matching the behavior of a fresh POST /chat turn.
    if (!currentThreadId) {
      currentThreadId = crypto.randomUUID(); window.__mobiusChatThreadId = currentThreadId;
    }
    if (restoreInFlight.has(documentId)) return;
    restoreInFlight.add(documentId);
    const originalText = button.textContent || "Attach";
    button.disabled = true;
    button.textContent = "Attaching…";
    try {
      const resp = await fetch(
        API_BASE + "/chat/uploads/" + encodeURIComponent(documentId) + "/link-to-thread",
        {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ thread_id: currentThreadId }),
        },
      );
      if (!resp.ok) {
        const detail = await resp.json().catch(() => null as any);
        throw new Error(detail?.detail || `Attach failed (${resp.status})`);
      }
      await resp.json();
      // Success: flash a banner and remove the just-attached row from
      // the list (if the user has more recent uploads, those stay).
      button.textContent = "Attached ✓";
      showChatStatusBanner(`✓ "${filename}" attached to this chat — ask away.`, 5000);
      // Remove the row after a short delay so the "Attached ✓" state is
      // visible for a moment.
      setTimeout(() => {
        const row = button.closest(".upload-restore-banner__row");
        row?.remove();
        // If the list is now empty, hide the whole banner.
        if (uploadRestoreBannerList && uploadRestoreBannerList.children.length === 0) {
          hideRestoreBanner();
        }
      }, 600);
    } catch (err: any) {
      console.error("[restore-banner] link failed:", err);
      showChatStatusBanner(`✗ Couldn't attach "${filename}": ${err?.message || err}`, 10000);
      button.disabled = false;
      button.textContent = originalText;
    } finally {
      restoreInFlight.delete(documentId);
    }
  }

  async function maybeShowRestoreBanner(): Promise<void> {
    if (!uploadRestoreBanner || !uploadRestoreBannerList) return;
    if (userDismissedRestoreBanner()) return;
    // Defense-in-depth: if the caller has no identity, the backend now
    // returns empty, but bail early to skip the round-trip entirely.
    const _whoami = await _getWhoami();
    if (!_whoami) return;
    // If the current thread already has instant-rag uploads, the user
    // isn't looking for a restore — don't nag them.
    if (currentThreadId) {
      try {
        const r = await apiFetch(
          API_BASE + "/chat/thread/" + encodeURIComponent(currentThreadId) + "/uploads",
        );
        if (r.ok) {
          const body = await r.json().catch(() => ({} as any));
          // The existing /chat/thread/{id}/uploads returns markdown;
          // we just need to know "does it mention an upload?". Markdown
          // for an empty thread starts with "No documents" or similar.
          const md = String(body?.markdown || body?.result || body || "");
          if (/instant[-_ ]?rag|\.pdf\b|\.docx\b/i.test(md)) {
            // Thread has uploads already — no banner.
            hideRestoreBanner();
            return;
          }
        }
      } catch {
        // Can't tell either way; fall through and try to show anyway.
      }
    }

    // Fetch recent uploads not on this thread (auth header propagates identity).
    let uploads: any[] = [];
    try {
      const params = new URLSearchParams({ limit: "5" });
      if (currentThreadId) params.set("current_thread_id", currentThreadId);
      const r = await apiFetch(API_BASE + "/chat/uploads/recent/for-restoration?" + params.toString());
      if (!r.ok) return;
      const body = await r.json();
      uploads = body?.uploads || [];
    } catch {
      return;
    }
    if (!uploads.length) {
      hideRestoreBanner();
      return;
    }

    // Render rows.
    uploadRestoreBannerList.replaceChildren();
    for (const u of uploads) {
      const row = document.createElement("div");
      row.className = "upload-restore-banner__row";
      const name = document.createElement("span");
      name.className = "upload-restore-banner__filename";
      name.textContent = String(u.filename || "upload");
      name.title = String(u.filename || "");
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "upload-restore-banner__attach";
      btn.textContent = "Attach to this chat";
      btn.addEventListener("click", () => {
        void linkUploadToCurrentThread(
          String(u.document_id || ""),
          String(u.filename || "upload"),
          btn,
        );
      });
      row.appendChild(name);
      row.appendChild(btn);
      uploadRestoreBannerList.appendChild(row);
    }
    uploadRestoreBanner.hidden = false;
  }

  // Fire once on load, and whenever the thread id changes (new chat).
  void maybeShowRestoreBanner();

  /** Reset upload UI and show sheet (⋯ → Upload file). */
  function openUploadModal(): void {
    hideRosterUploadReceipt();
    const modal = document.getElementById("uploadModal");
    const overlay = document.getElementById("uploadOverlay");
    const form = document.getElementById("uploadForm");
    const st = document.getElementById("uploadStatus");
    const progressWrap = document.getElementById("uploadProgressWrap");
    const uploadSig = document.getElementById("uploadRosterThreadSignal");
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
    const utid = (currentThreadId || "").trim();
    if (!utid) {
      setRosterThreadSignalBanner(
        uploadSig,
        "muted",
        "Send a message first so this upload attaches to a chat thread."
      );
    } else {
      setRosterThreadSignalBanner(uploadSig, "muted", "Checking roster on this chat…");
      fetch(API_BASE + "/chat/thread/" + encodeURIComponent(utid) + "/uploads")
        .then(
          (r) =>
            r.json() as Promise<{
              roster_reconciliation_files?: ThreadUploadsRosterRow[];
              latest_roster_reconciliation?: ThreadUploadsRosterRow | null;
              roster_freshness?: string;
              roster_fresh_days_threshold?: number;
            }>
        )
        .then((data) => {
          const th =
            typeof data.roster_fresh_days_threshold === "number" && data.roster_fresh_days_threshold > 0
              ? data.roster_fresh_days_threshold
              : 14;
          let latest: ThreadUploadsRosterRow | null =
            data.latest_roster_reconciliation && rosterLatestRowPresent(data.latest_roster_reconciliation)
              ? data.latest_roster_reconciliation
              : null;
          const rows = Array.isArray(data.roster_reconciliation_files) ? data.roster_reconciliation_files : [];
          if (!latest && rows.length > 0 && rosterLatestRowPresent(rows[0])) {
            latest = rows[0];
          }
          const apiF = normalizeRosterFreshness(data.roster_freshness);
          const effective: RosterThreadFreshnessApi = rosterLatestRowPresent(latest) ? apiF : "none";
          setRosterThreadSignalBanner(
            uploadSig,
            effective,
            messageForRosterThreadSignal(effective, latest, th)
          );
        })
        .catch(() => {
          setRosterThreadSignalBanner(
            uploadSig,
            "muted",
            "Could not check for an existing roster — you can still upload a file."
          );
        });
    }
    (document.getElementById("uploadOrgName") as HTMLInputElement | null)?.focus();
  }

  function setupComposerOptionsMenu(): void {
    const optionsBtn = document.getElementById("composerOptions");
    const optionsMenu = document.getElementById("composerOptionsMenu");
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
    document.addEventListener("click", () => hideOptionsMenu());
  }
  setupComposerOptionsMenu();

  function setupCredentialingEnvelope(): void {
    const form = document.getElementById("credentialingForm") as HTMLFormElement | null;
    const credOverlay = document.getElementById("credentialingOverlay");
    const cancel = document.getElementById("credentialingCancel");
    const defaultsBtn = document.getElementById("credentialingDefaults");
    form?.addEventListener("submit", (e) => {
      e.preventDefault();
      const pending = credentialingPendingMessage;
      if (!pending) return;
      const org = (document.getElementById("credentialingOrgName") as HTMLInputElement | null)?.value?.trim();
      if (!org) return;
      const modeEl = document.querySelector('input[name="credentialingMode"]:checked') as HTMLInputElement | null;
      const mode: "autopilot" | "copilot" = modeEl?.value === "copilot" ? "copilot" : "autopilot";
      const forceRefresh = !!(document.getElementById("credentialingForceRefresh") as HTMLInputElement | null)?.checked;
      const preferOutside = !!(document.getElementById("credentialingPreferOutsideIn") as HTMLInputElement | null)?.checked;
      const preferFresh = !!(document.getElementById("credentialingPreferFresh") as HTMLInputElement | null)?.checked;
      const freshHidden = document.getElementById("credentialingPreferFreshWrap")?.hasAttribute("hidden");
      hideCredentialingEnvelope();
      const credOpts: CredentialingOptionsPayload = {
        org_name: org,
        mode,
        force_refresh: forceRefresh,
      };
      if (preferOutside) credOpts.prefer_outside_in = true;
      if (preferFresh && !freshHidden) credOpts.prefer_fresh_report = true;
      sendMessage(pending, {
        credentialing_options: credOpts,
        use_react: true,
      });
    });
    cancel?.addEventListener("click", () => hideCredentialingEnvelope());
    credOverlay?.addEventListener("click", () => hideCredentialingEnvelope());
    defaultsBtn?.addEventListener("click", () => {
      const ap = document.querySelector('input[name="credentialingMode"][value="autopilot"]') as HTMLInputElement | null;
      if (ap) ap.checked = true;
      const fr = document.getElementById("credentialingForceRefresh") as HTMLInputElement | null;
      if (fr) fr.checked = false;
      const po = document.getElementById("credentialingPreferOutsideIn") as HTMLInputElement | null;
      if (po) po.checked = false;
      const pf = document.getElementById("credentialingPreferFresh") as HTMLInputElement | null;
      if (pf) pf.checked = false;
      refreshCredentialingRosterUi();
    });
    const orgNameField = document.getElementById("credentialingOrgName") as HTMLInputElement | null;
    orgNameField?.addEventListener("input", () => refreshCredentialingRosterUi());
    document.getElementById("credentialingPreferOutsideIn")?.addEventListener("change", () => refreshCredentialingRosterUi());
    document.getElementById("credentialingUploadRoster")?.addEventListener("click", () => {
      const pending = credentialingPendingMessage;
      credentialingReopenMessage = pending;
      const orgEl = document.getElementById("credentialingOrgName") as HTMLInputElement | null;
      const uploadOrg = document.getElementById("uploadOrgName") as HTMLInputElement | null;
      if (uploadOrg && orgEl) uploadOrg.value = orgEl.value.trim();
      const auto = document.getElementById("uploadAutoSendReconciliation") as HTMLInputElement | null;
      if (auto) auto.checked = false;
      hideCredentialingEnvelope();
      openUploadModal();
    });
  }
  setupCredentialingEnvelope();

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

    const rosterFields = document.getElementById("uploadFieldRoster");
    // Toggle roster-specific fields based on purpose
    uploadFilePurpose?.addEventListener("change", () => {
      const isRoster = uploadFilePurpose.value === "roster_reconciliation";
      if (rosterFields) rosterFields.hidden = !isRoster;
      if (uploadOrgName) uploadOrgName.required = isRoster;
      updateSubmitState();
    });

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
        : [
            // 2026-04-18 copy revision (user flagged "publishing to RAG"
            // as jargon). Same user-friendly arc as the composer-attach
            // flow — one narrative, not four technical stages.
            { ms: 0,     text: "Uploading…" },
            { ms: 4000,  text: "Reading your document…" },
            { ms: 15000, text: "Getting it ready to search…" },
            { ms: 40000, text: "Still working — larger docs take a bit longer…" },
            { ms: 75000, text: "Almost done…" },
          ];

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
      const isRoster = (uploadFilePurpose?.value || "roster_reconciliation") === "roster_reconciliation";
      const hasOrg = !!(uploadOrgName?.value?.trim());
      if (uploadSubmit) uploadSubmit.disabled = !(hasFile && (hasOrg || !isRoster));
    }
    uploadOrgName?.addEventListener("input", updateSubmitState);
    uploadFile?.addEventListener("change", () => {
      updateSubmitState();
      const f = uploadFile?.files?.[0];
      const rosterHint = document.getElementById("uploadRosterHint");
      if (!rosterHint) return;
      if (!f) { rosterHint.hidden = true; rosterHint.textContent = ""; return; }
      const isCsv = f.name.toLowerCase().endsWith(".csv") || f.type === "text/csv";
      if (!isCsv) { rosterHint.hidden = true; rosterHint.textContent = ""; return; }
      const reader = new FileReader();
      reader.onload = (ev) => {
        const firstLine = ((ev.target?.result as string) || "").split(/\r?\n/)[0] || "";
        if (_looksLikeRosterCsv(firstLine)) {
          rosterHint.textContent = "This looks like a roster file. To reconcile providers, use the Credentialing module instead.";
          rosterHint.hidden = false;
        } else {
          rosterHint.hidden = true;
          rosterHint.textContent = "";
        }
      };
      reader.readAsText(f.slice(0, 512));
    });

    uploadForm?.addEventListener("submit", (e) => {
      e.preventDefault();
      const orgName = uploadOrgName?.value?.trim() || "";
      const file = uploadFile?.files?.[0];
      const purpose = (uploadFilePurpose?.value || "roster_reconciliation").trim();
      const isRoster = purpose === "roster_reconciliation";
      if (!file || (isRoster && !orgName)) return;
      uploadSubmit?.setAttribute("disabled", "");
      uploadModal?.classList.add("upload-modal--busy");
      uploadForm?.setAttribute("aria-busy", "true");
      uploadProgressWrap?.removeAttribute("hidden");
      startUploadPhaseEmits(purpose);
      const formData = new FormData();
      formData.append("file", file);
      if (currentThreadId) formData.append("thread_id", currentThreadId);
      uploadAbort = new AbortController();
      const signal = uploadAbort.signal;
      apiFetch(API_BASE + "/chat/upload", { method: "POST", body: formData, signal })
        .then((r) => {
          if (!r.ok) return r.json().then((d) => Promise.reject(d?.detail ?? r.statusText));
          return r.json();
        })
        .then((data: RosterUploadResponse) => {
            const org = data.org_name ?? orgName;
            if (data.thread_id) currentThreadId = data.thread_id; window.__mobiusChatThreadId = currentThreadId;
            stopUploadPhaseEmits();
            uploadModal?.classList.remove("upload-modal--busy");
            uploadForm?.removeAttribute("aria-busy");
            uploadProgressWrap?.setAttribute("hidden", "");
            uploadAbort = null;
            showRosterUploadReceipt(data);
            // Capture purpose BEFORE form reset (reset reverts select to first option)
            const uploadPurpose = purpose;
            uploadForm?.reset();
            updateSubmitState();
            if (uploadPurpose === "instant_rag") {
              const fname = data.filename ?? file?.name ?? "document";
              inputEl.value = `I just uploaded "${fname}" — what does it say about eligibility and coverage?`;
            } else {
              inputEl.value = `Run reconciliation report for ${org}`;
            }
            updateSendState();
            hideUploadModal();
            // Reset roster fields visibility
            if (rosterFields) rosterFields.hidden = false;
            if (uploadOrgName) uploadOrgName.required = true;
            // For instant_rag: skip credentialing envelope and auto-send
            if (uploadPurpose === "instant_rag") {
              return;
            }
            const reopen = credentialingReopenMessage;
            if (reopen) {
              credentialingReopenMessage = null;
              window.setTimeout(() => {
                openCredentialingEnvelope(reopen);
              }, 0);
              return;
            }
            const auto = document.getElementById("uploadAutoSendReconciliation") as HTMLInputElement | null;
            if (uploadPurpose === "roster_reconciliation" && auto?.checked) {
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
      currentThreadId = null; window.__mobiusChatThreadId = currentThreadId;
      hideChatStatusBanner();
      hideRosterUploadReceipt();
      messagesEl.querySelectorAll(".chat-turn").forEach((n) => n.remove());
      if (chatEmpty) chatEmpty.classList.remove("hidden");
      document.body.classList.add("landing-state");
      loadSidebarHistory();
    });
  }

  /**
   * Phase 13.7 — Load a thread's existing turns into the chat pane and
   * set it as the active thread for follow-ups.
   *
   * Replaces the previous "click pre-fills input" behavior with full
   * rehydration. The user sees the conversation as it was; their next
   * message continues that thread (state_load picks up active context,
   * previous_thread_summary, last_turns from the same thread_id).
   *
   * Failure modes are non-destructive: a network error or empty payload
   * leaves the chat pane untouched and logs a console warning. We do
   * NOT clear messagesEl until we have data in hand.
   */
  async function loadAndRenderThread(threadId: string): Promise<void> {
    const tid = (threadId || "").trim();
    if (!tid) return;
    document.body.classList.remove("landing-state");
    type RehydratedTurn = {
      correlation_id: string;
      question: string;
      final_message: string;
      sources: Array<{
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
      }>;
      thinking_log: string[];
      source_confidence_strip: string | null;
      created_at: string;
    };
    let turns: RehydratedTurn[];
    try {
      const r = await fetch(
        API_BASE + "/chat/history/threads/" + encodeURIComponent(tid) + "/turns?limit=50",
        { headers: (await auth.getAuthHeader?.()) ?? {} }
      );
      if (!r.ok) {
        console.warn("[loadAndRenderThread] HTTP", r.status, "for", tid);
        // BETA-sprint Move 2 — loud failure on user-visible path. The
        // sidebar click is an explicit user action; if it silently
        // fails the user is left with an unchanged pane and no clue
        // why. Toast surfaces the problem without blocking the app.
        _showToast(`Couldn't load thread (HTTP ${r.status}). Please retry.`);
        return;
      }
      turns = await r.json();
    } catch (err) {
      console.warn("[loadAndRenderThread] fetch failed:", err);
      _showToast("Couldn't load thread. Check your connection and retry.");
      return;
    }
    if (!Array.isArray(turns)) {
      console.warn("[loadAndRenderThread] non-array response", typeof turns);
      _showToast("Thread response was unexpected. Please retry.");
      return;
    }

    // Now that we have the data, swap the chat pane.
    currentThreadId = tid;
    window.__mobiusChatThreadId = currentThreadId;
    if (chatEmpty) chatEmpty.classList.add("hidden");
    messagesEl.querySelectorAll(".chat-turn").forEach((n) => n.remove());
    hideChatStatusBanner();
    hideRosterUploadReceipt();

    for (const turn of turns) {
      const turnWrap = document.createElement("div");
      turnWrap.className = "chat-turn";
      // 1. User message — same renderer the live path uses.
      turnWrap.appendChild(renderUserMessage(turn.question || "", undefined));

      // 2. Thinking-log preview (collapsed by default; matches live shape).
      // We seed all lines and immediately call done() so it renders in
      // its terminal state — no streaming, no "Queued" pulse.
      //
      // chat_turns.thinking_log holds mixed types: some entries are
      // plain progress strings ("◌ Thinking…"), others are signal
      // dicts ({event, message, correlation_id}). renderThinkingBlock
      // expects string[] and calls .toLowerCase() per entry — pass a
      // dict in and it crashes. Coerce defensively: keep strings as
      // strings, render dict entries via their .message field if
      // present (the human-readable line), JSON-stringify everything
      // else, and drop empties.
      if (Array.isArray(turn.thinking_log) && turn.thinking_log.length > 0) {
        const lines: string[] = [];
        for (const entry of turn.thinking_log) {
          if (typeof entry === "string") {
            const s = entry.trim();
            if (s) lines.push(s);
          } else if (entry && typeof entry === "object") {
            const e = entry as { message?: unknown; line?: unknown };
            const msg = typeof e.message === "string" ? e.message : (typeof e.line === "string" ? e.line : "");
            if (msg && msg.trim()) {
              lines.push(msg.trim());
            } else {
              // Last-resort serialization so debug info isn't lost.
              try { lines.push(JSON.stringify(entry).slice(0, 200)); } catch { /* noop */ }
            }
          }
        }
        if (lines.length > 0) {
          const tb = renderThinkingBlock(lines);
          try { tb.done(lines.length); } catch { /* noop */ }
          turnWrap.appendChild(tb.el);
        }
      }

      // 3. Assistant answer — final_message is the AnswerCard JSON
      // exactly as live turns render. renderAssistantContent handles
      // both AnswerCard and prose-fallback shapes.
      const finalBody = turn.final_message || "";
      if (finalBody.trim()) {
        turnWrap.appendChild(
          renderAssistantContent(finalBody, false, {
            onFollowupClick: (q) => sendMessage(q),
            sourceConfidenceStrip: turn.source_confidence_strip || undefined,
          })
        );
      }

      // 4. Sources panel — same shape conversion the live path uses
      // (data.sources -> ParsedSource list -> renderSourceCiter).
      // Pass [] for cited indices since we don't persist them per turn;
      // the citer falls back to showing all sources in that case.
      if (Array.isArray(turn.sources) && turn.sources.length > 0) {
        const sourceList: ParsedSource[] = turn.sources.map((s) => ({
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
        }));
        turnWrap.appendChild(
          renderSourceCiter(sourceList, [], turn.correlation_id)
        );
      }

      // 5. Feedback bar — thumbs + Copy + Email. Same correlation_id
      // wiring as the live path; thumbs vote against the historical
      // turn, Copy grabs the assistant bubble text, Email opens the
      // thread-email dialog.
      if (turn.correlation_id) {
        turnWrap.appendChild(renderFeedback(turn.correlation_id));
      }

      messagesEl.appendChild(turnWrap);
    }
    scrollToBottom(messagesEl);
    // Refocus input so the user can immediately type a follow-up.
    try { (inputEl as HTMLInputElement).focus(); } catch { /* noop */ }
  }

  // ── My Vault sidebar block ───────────────────────────────────────────────────
  // Panel API: vault-panel.js (vendored at /static/vault-panel.js) exposes
  // window.MobiusVault = { open(opts?), close(), toggle() }.
  // opts: { tab: "recent"|"liked"|"tasks"|"uploads" }
  // Fallback: if the component isn't loaded yet, opens /vault in a new tab.
  function openVaultPanel(tab?: string): void {
    const w = window as Window & typeof globalThis & { MobiusVault?: { open: (opts?: { tab?: string; currentThreadId?: string }) => void } };
    if (typeof w.MobiusVault?.open === "function") {
      const opts: { tab?: string; currentThreadId?: string } = {};
      if (tab) opts.tab = tab;
      if (currentThreadId) opts.currentThreadId = currentThreadId;
      w.MobiusVault.open(Object.keys(opts).length ? opts : undefined);
    } else {
      window.open("/vault", "_blank", "noopener");
    }
  }

  let _vaultActiveTab = "recent";

  function initVaultBlock(): void {
    const vaultBlock = document.getElementById("sidebarVaultBlock");
    if (!vaultBlock) return;

    // Wire open buttons
    document.getElementById("vaultOpenBtn")?.addEventListener("click", () => openVaultPanel(_vaultActiveTab));
    document.getElementById("vaultManageBtn")?.addEventListener("click", () => openVaultPanel("recent"));
    document.getElementById("vaultRailBtn")?.addEventListener("click", () => {
      // Expand sidebar if collapsed, then scroll vault block into view
      const sidebar = document.getElementById("sidebar");
      if (sidebar?.classList.contains("sidebar--collapsed")) {
        document.getElementById("sidebarChevron")?.click();
      }
      vaultBlock.scrollIntoView({ behavior: "smooth", block: "nearest" });
    });

    // Wire tabs
    vaultBlock.querySelectorAll<HTMLButtonElement>(".vault-tab").forEach((btn) => {
      btn.addEventListener("click", () => {
        const tab = btn.dataset.vaultTab || "recent";
        vaultBlock.querySelectorAll(".vault-tab").forEach((t) => {
          t.classList.toggle("vault-tab--active", t === btn);
          t.setAttribute("aria-selected", t === btn ? "true" : "false");
        });
        _vaultActiveTab = tab;
        void loadVaultTab(tab);
      });
    });

    void loadVaultCounts();
    void loadVaultTab("recent");
  }

  async function loadVaultCounts(): Promise<void> {
    const _authHeaders = (await auth.getAuthHeader?.()) ?? {};
    // Fetch counts for all tabs in parallel; update badges silently on failure
    const [threads, liked, tasksResp, uploadsResp] = await Promise.allSettled([
      fetch(API_BASE + "/chat/history/threads?limit=1", { headers: _authHeaders }).then((r) => r.json() as Promise<unknown[]>),
      fetch(API_BASE + "/chat/history/most-helpful-searches?limit=1", { headers: _authHeaders }).then((r) => r.json() as Promise<unknown[]>),
      fetch(API_BASE + "/chat/tasks?limit=1&assigned_to=user:me", { headers: _authHeaders }).then((r) => r.json() as Promise<{ tasks?: unknown[] }>),
      fetch(API_BASE + "/chat/uploads?limit=1", { headers: _authHeaders }).then((r) => r.json() as Promise<{ uploads?: unknown[] }>),
    ]);
    // For count badges, hit the real list endpoints and use X-Total or array length
    // (endpoints don't return totals, so we show item count for the first page)
    void threads; void liked; void tasksResp; void uploadsResp;
    // Re-fetch with higher limit to get counts; counts update in loadVaultTab
  }

  async function loadVaultTab(tab: string): Promise<void> {
    const list = document.getElementById("vaultItemList");
    if (!list) return;
    const _authHeaders = (await auth.getAuthHeader?.()) ?? {};

    const snippet = (s: string, max = 72) => (s ?? "").trim().slice(0, max) + ((s ?? "").length > max ? "…" : "");

    const setCount = (id: string, n: number | null) => {
      const el = document.getElementById(id);
      if (el) el.textContent = n != null ? ` ${n}` : "";
    };

    list.innerHTML = `<li class="vault-item vault-item--muted">Loading…</li>`;

    try {
      if (tab === "recent") {
        const threads = await fetch(API_BASE + "/chat/history/threads?limit=20", { headers: _authHeaders })
          .then((r) => r.json() as Promise<Array<{ thread_id: string; title: string; summary?: string | null; turn_count: number }>>);
        setCount("vaultCountRecent", threads.length);
        list.innerHTML = "";
        if (!threads.length) {
          list.innerHTML = `<li class="vault-item vault-item--muted">No recent chats yet</li>`;
          return;
        }
        for (const th of threads) {
          const li = document.createElement("li");
          li.className = "vault-item";
          li.textContent = snippet((th.summary && th.summary.trim()) || th.title || "Untitled chat");
          li.title = th.summary || th.title || "";
          li.setAttribute("role", "button");
          li.setAttribute("tabindex", "0");
          li.addEventListener("click", () => void loadAndRenderThread(th.thread_id));
          li.addEventListener("keydown", (e) => {
            if (e.key === "Enter" || e.key === " ") { e.preventDefault(); void loadAndRenderThread(th.thread_id); }
          });
          list.appendChild(li);
        }

      } else if (tab === "liked") {
        const liked = await fetch(API_BASE + "/chat/history/most-helpful-searches?limit=20", { headers: _authHeaders })
          .then((r) => r.json() as Promise<HistoryTurnItem[]>);
        setCount("vaultCountLiked", liked.length);
        list.innerHTML = "";
        if (!liked.length) {
          list.innerHTML = `<li class="vault-item vault-item--muted">No liked answers yet — thumb up a helpful response</li>`;
          return;
        }
        for (const t of liked) {
          const li = document.createElement("li");
          li.className = "vault-item";
          li.textContent = snippet(t.question || "(empty)");
          li.title = t.question || "";
          li.setAttribute("role", "button");
          li.setAttribute("tabindex", "0");
          const tid = (t.thread_id || "").trim();
          li.addEventListener("click", () => {
            if (tid) void loadAndRenderThread(tid);
            else { (inputEl as HTMLInputElement).value = t.question ?? ""; updateSendState(); sendMessage(); }
          });
          list.appendChild(li);
        }

      } else if (tab === "tasks") {
        const data = await fetch(API_BASE + "/chat/tasks?limit=20", { headers: _authHeaders })
          .then((r) => r.json() as Promise<{ tasks?: Array<{ task_id: string; title?: string; kind?: string; status?: string }> }>);
        const tasks = data.tasks || [];
        const open = tasks.filter((t) => t.status !== "completed" && t.status !== "closed");
        setCount("vaultCountTasks", open.length || null);
        list.innerHTML = "";
        if (!open.length) {
          list.innerHTML = `<li class="vault-item vault-item--muted">No open tasks</li>`;
          return;
        }
        for (const t of open) {
          const li = document.createElement("li");
          li.className = "vault-item";
          li.textContent = snippet(t.title || t.kind || "Task");
          li.title = t.title || "";
          list.appendChild(li);
        }

      } else if (tab === "uploads") {
        const data = await fetch(API_BASE + "/chat/uploads?limit=20", { headers: _authHeaders })
          .then((r) => r.json() as Promise<{ uploads?: Array<{ document_id: string; filename?: string; status?: string }> }>);
        const uploads = data.uploads || [];
        setCount("vaultCountUploads", uploads.length || null);
        list.innerHTML = "";
        if (!uploads.length) {
          list.innerHTML = `<li class="vault-item vault-item--muted">No uploads yet</li>`;
          return;
        }
        for (const u of uploads) {
          const li = document.createElement("li");
          li.className = "vault-item";
          li.textContent = snippet(u.filename || u.document_id);
          li.title = u.filename || u.document_id;
          list.appendChild(li);
        }
      }
    } catch {
      list.innerHTML = `<li class="vault-item vault-item--muted">Failed to load — try again</li>`;
    }
  }

  function loadSidebarHistory(): void {
    // Sidebar history now lives in the My Vault block.
    void loadVaultTab(_vaultActiveTab);
  }

  // Legacy function kept for compat — the real Recent list is in loadVaultTab("recent")
  function _loadSidebarHistoryFull(): void {
    const recentList = document.getElementById("recentList");
    const helpfulList = document.getElementById("helpfulList");
    const documentsList = document.getElementById("documentsList");
    if (!recentList) return;

    const snippet = (q: string, max = 80) =>
      (q ?? "").trim().slice(0, max) + ((q ?? "").length > max ? "…" : "");

    // getAuthHeader() is async — must await it before passing to fetch().
    // Wrapping in an async IIFE keeps the outer function signature void.
    void (async () => {
    const _authHeaders = (await auth.getAuthHeader?.()) ?? {};
    Promise.all([
      // Phase 2.3: sidebar now shows deduplicated *threads* with real titles
      // instead of per-turn rows that exposed raw URLs / tool inputs. Endpoint
      // returns {thread_id, title, updated_at, turn_count}. Gracefully returns
      // [] if migration 030 hasn't run, so the list is empty rather than broken.
      // Auth header required — history is user-scoped (fix 2026-05-06).
      fetch(API_BASE + "/chat/history/threads?limit=20", { headers: _authHeaders }).then(
        (r) => r.json() as Promise<Array<{ thread_id: string; title: string; summary?: string | null; updated_at: string; turn_count: number }>>
      ),
      helpfulList
        ? fetch(API_BASE + "/chat/history/most-helpful-searches?limit=10", { headers: _authHeaders }).then(
            (r) => r.json() as Promise<HistoryTurnItem[]>
          )
        : Promise.resolve([] as HistoryTurnItem[]),
      documentsList
        ? fetch(API_BASE + "/chat/history/most-helpful-documents?limit=10", { headers: _authHeaders }).then(
            (r) => r.json() as Promise<HistoryDocumentItem[]>
          )
        : Promise.resolve([] as HistoryDocumentItem[]),
    ])
      .then(([recentThreads, helpful, documents]) => {
        recentList.innerHTML = "";
        for (const th of recentThreads) {
          const li = document.createElement("li");
          li.className = "recent-item";
          // Phase 13.7 — prefer the rolling thread summary as the
          // sidebar label (morphs across turns, captures current
          // state). Fall back to title (=first turn's question), then
          // 'Untitled'. Tooltip shows the full string.
          const label = (th.summary && th.summary.trim()) || th.title || "Untitled chat";
          const countSuffix = th.turn_count > 1 ? `  (${th.turn_count})` : "";
          li.textContent = snippet(label) + countSuffix;
          li.title = label;
          li.setAttribute("role", "button");
          li.setAttribute("tabindex", "0");
          li.setAttribute("data-thread-id", th.thread_id);
          // Phase 13.7 — click loads the existing thread instead of
          // re-submitting the question as a fresh turn (which lost
          // continuity AND burned LLM cost on already-answered work).
          li.addEventListener("click", () => {
            void loadAndRenderThread(th.thread_id);
          });
          li.addEventListener("keydown", (e) => {
            if (e.key === "Enter" || e.key === " ") {
              e.preventDefault();
              void loadAndRenderThread(th.thread_id);
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
            // 2026-05-05: re-open the existing thread instead of
            // re-running the question. Same behavior as recent threads.
            // Falls back to re-submit if thread_id is missing (older
            // rows pre-backend-fix) so the click is never a dead end.
            const tid = (t.thread_id || "").trim();
            const openOrReSubmit = (): void => {
              if (tid) {
                void loadAndRenderThread(tid);
              } else {
                (inputEl as HTMLInputElement).value = t.question ?? "";
                updateSendState();
                sendMessage();
              }
            };
            li.addEventListener("click", openOrReSubmit);
            li.addEventListener("keydown", (e) => {
              if (e.key === "Enter" || e.key === " ") {
                e.preventDefault();
                openOrReSubmit();
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
    })();
  }
  // end _loadSidebarHistoryFull (legacy, elements no longer in DOM)

  // Set landing-state on load; cleared on first message send.
  // Only applies when there are no existing thread messages (fresh page load without a thread).
  if (messagesEl && messagesEl.querySelectorAll(".chat-turn").length === 0) {
    document.body.classList.add("landing-state");
    // Collapse sidebar by default on landing — decluttered-landing spec.
    const landingSidebar = document.getElementById("sidebar");
    const landingMain = document.querySelector<HTMLElement>(".main");
    if (landingSidebar) landingSidebar.classList.add("sidebar--collapsed");
    if (landingMain) landingMain.classList.add("sidebar-collapsed");
  }

  // Composer landing chips: set composer text + send on click.
  document.getElementById("composerLandingChips")?.addEventListener("click", (e) => {
    const chip = (e.target as HTMLElement).closest(".composer-chip") as HTMLElement | null;
    if (!chip) return;
    const q = chip.getAttribute("data-query")?.trim();
    if (!q) return;
    inputEl.value = q;
    updateSendState();
    sendMessage();
  });

  // Legacy: .landing-try-link clicks (kept for any old links still in DOM).
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
    const pThread = u.searchParams.get("thread")?.trim();
    if (pq) {
      u.searchParams.delete("q");
      const next = u.pathname + (u.search ? u.search : "") + u.hash;
      window.history.replaceState({}, "", next);
      inputEl.value = pq;
      updateSendState();
      sendMessage();
    } else if (pThread) {
      u.searchParams.delete("thread");
      const next = u.pathname + (u.search ? u.search : "") + u.hash;
      window.history.replaceState({}, "", next);
      document.body.classList.remove("landing-state");
      void loadAndRenderThread(pThread);
    }
  } catch {
    /* ignore */
  }

  initVaultBlock();

  // Thumbs-up on any turn fires mobiusFeedbackUp: refresh Liked tab count.
  window.addEventListener("mobiusFeedbackUp", () => {
    if (_vaultActiveTab === "liked") void loadVaultTab("liked");
    // Always refresh recent since a new answer was given
    void (async () => {
      const el = document.getElementById("vaultCountRecent");
      if (el) { /* count refreshes on next Recent tab open */ }
    })();
  });

  updateSendState();

  // ── Operations Suite + Skills modal ─────────────────────────────────────────
  //
  // Two-layer discoverability:
  //   1. Sidebar "Operations Suite" → 3 always-visible direct-link tiles
  //      (Strategy, Credentialing, Roster) — each opens the standalone
  //      product in a new tab.
  //   2. "Learn more about chat skills →" link below the tiles → opens
  //      the full themed modal with all categories.
  //
  // No tool names ("search_corpus", "healthcare_query") leak into user-
  // facing copy — themes are described by what they do for the operator.
  // Power users wanting the raw planner manifest still have
  // _openSeeAllSkillsModal() (the chip-list).
  //
  // The data structure carries `selected: true` per theme, decorative
  // today; it becomes a per-role toggle when tool-gating ships (queued).
  //
  // Brand colors are semantic (mobius-tokens.css):
  //   indigo  → runs / pipeline / process state   (Strategy)
  //   violet  → credentialing (policy-of-record)  (Credentialing)
  //   emerald → roster (operational data)         (Roster)
  //
  (function setupSkillsModal(): void {
    const overlay = document.getElementById("skillsOverlay");
    const modal = document.getElementById("skillsModal");
    const modalBody = document.getElementById("skillsModalBody");
    const sidebarTilesContainer = document.getElementById("suiteTilesContainer");
    const learnMoreBtn = document.getElementById("suiteLearnMore");

    type SuiteTile = {
      key: string;
      label: string;
      tagline: string;
      accent: "indigo" | "violet" | "emerald" | "accent";
      urlEnvKey: string;       // window.<key> read first
      fallbackUrl: string;     // dev / unconfigured fallback
      comingSoon?: boolean;    // 2026-04-28 — disabled in UI until ready
      description?: string;    // 2026-04-29 — long blurb shown in skills modal
    };

    // 2026-04-28: Strategy / Credentialing / Roster surface in the
    // sidebar + skills modal, but their backends are not yet hardened
    // for production use. Marking them ``comingSoon`` keeps the visual
    // hint (so users know they're planned) while disabling the click
    // handler — no tab opens, no broken landing page. Library stays
    // active because the corpus UI is the one that is in good shape.
    // 2026-04-29: layout cleanup
    //   * Credentialing folded into Roster (same backing service today;
    //     surfacing both as separate tiles confused users).
    //   * Library renamed → "Public Library" to leave room for the Vault
    //     concept: future per-org / per-user / per-patient namespaces
    //     served via a separate agent + isolation boundary.
    //   * Vault tile added as ``comingSoon`` so the surface area is
    //     visible to users now even though the backing implementation
    //     is the next sprint.
    const SUITE_TILES: SuiteTile[] = [
      {
        // 2026-05-05: strategy agent (mobius-story-ui) is now deployed
        // and reachable. Removed comingSoon so the sidebar tile + skills
        // modal can open it in a new tab. Backend URL configurable via
        // MOBIUS_STRATEGY_URL env (window-injected) — fallback points at
        // the dev Cloud Run service.
        key: "strategy",
        label: "Strategy",
        tagline: "Benchmarking + KPIs",
        accent: "indigo",
        urlEnvKey: "MOBIUS_STRATEGY_URL",
        fallbackUrl: "https://mobius-story-ui-ortabkknqa-uc.a.run.app",
      },
      {
        key: "roster",
        label: "Roster",
        tagline: "Provider directory + credentialing",
        accent: "emerald",
        urlEnvKey: "MOBIUS_CREDENTIALING_URL",
        fallbackUrl: "https://mobius-provider-roster-credentialing-ortabkknqa-uc.a.run.app/index.html",
      },
      {
        key: "library",
        label: "Public Library",
        tagline: "Shared corpus — payer manuals, regs, public sources",
        accent: "accent",
        urlEnvKey: "MOBIUS_LIBRARY_URL",
        fallbackUrl: "https://mobius-rag-ortabkknqa-uc.a.run.app",
      },
      {
        key: "platform",
        label: "Platform",
        tagline: "Architecture schematic",
        accent: "violet",
        urlEnvKey: "MOBIUS_PLATFORM_URL",
        fallbackUrl: "/platform",
      },
      // Vault is now the sidebar block above this section; not a tile.
    ];

    function tileUrl(t: SuiteTile): string {
      const winAny = window as Window & typeof globalThis & Record<string, unknown>;
      const fromEnv = (winAny[t.urlEnvKey] as string | undefined) || "";
      let url = (fromEnv && fromEnv.trim()) ? fromEnv.trim() : t.fallbackUrl;
      // Forward the platform access token to Mobius-internal tools so they can
      // authenticate the user without a second login (e.g. RAG/Library/Vault,
      // and anything they hand off to such as Lexicon Maintenance). The token
      // rides in the URL fragment (#t=…) which browsers never send to servers
      // nor write to access logs; the receiving SPA reads it then strips it.
      // Only Mobius-owned hosts get the token, never arbitrary external URLs.
      try {
        const tok = localStorage.getItem("mobius.auth.accessToken");
        if (tok && /(^|\/\/)([^/]*\.)?(run\.app|localhost|127\.0\.0\.1)/.test(url)) {
          url += (url.includes("#") ? "&" : "#") + "t=" + encodeURIComponent(tok);
        }
      } catch { /* localStorage unavailable — open without token */ }
      return url;
    }

    type ChatTheme = {
      title: string;
      tagline: string;
      description: string;
      examplePrompt: string;
      selected: boolean;       // hook for future per-role gating
    };

    // 2026-04-29: framed as "universal capabilities" — these are baked
    // into every chat turn (planner picks them automatically based on
    // the question). Distinct from Suite modules (Strategy / Roster /
    // Public Library / Vault) which are open-in-tab products.
    const CHAT_THEMES: ChatTheme[] = [
      {
        title: "Healthcare lookup",
        tagline: "Codes, NPIs, payer policies",
        description: "Look up procedure and diagnosis codes, verify NPI registry entries, and pull authoritative payer documents from your corpus — all with source citations you can defend.",
        examplePrompt: "What's Sunshine Health's prior authorization timeline for H0036?",
        selected: true,
      },
      {
        title: "External search",
        tagline: "Search beyond your library",
        description: "When the answer isn't in your corpus yet, Mobius searches the web, reads specific pages, and can permanently add authoritative sources to your library — so the next person asking gets an indexed answer.",
        examplePrompt: "Find Sunshine's dental plan transition dates and add the page to our library",
        selected: true,
      },
      {
        title: "Document chat",
        tagline: "Ask about a file you uploaded",
        description: "Upload a denial letter, provider manual, or policy PDF and ask questions about it directly. Mobius keeps it on the thread and searches inside it alongside the broader corpus.",
        examplePrompt: "What does the attached denial letter say about timely filing?",
        selected: true,
      },
      {
        title: "Task management",
        tagline: "Make conversations actionable",
        description: "Convert answers into letters, emails, or memos. Track follow-up tasks. Reshape a prior answer without re-running the whole research process.",
        examplePrompt: "Convert this to an appeal letter for Sunshine Health",
        selected: true,
      },
      {
        title: "PHI guardrail",
        tagline: "Refuses questions about specific patients",
        description: "Mobius will not answer questions tied to specific named patients, MRNs, or identifying combinations. The refusal happens up-front — before any retrieval or model call — and is consistent across every model the bandit might pick.",
        examplePrompt: "(Mobius will refuse questions like 'Has patient John Doe had his colonoscopy approved?')",
        selected: true,
      },
    ];

    type ComingSoon = { title: string; tagline: string; description: string };
    const COMING_SOON: ComingSoon[] = [
      {
        title: "Denial management",
        tagline: "Build defendable appeals end-to-end",
        description: "Intake the denial, retrieve the contract and regulatory rules that apply, construct the argument, run a counterpoint check (\"what's the payer's likely rebuttal?\"), and assemble the submission packet — letter, form, supporting documents, timeline.",
      },
    ];

    // ── Renderers ────────────────────────────────────────────────────

    function escapeHtml(s: string): string {
      return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&#39;");
    }

    function renderSidebarSuiteTiles(): void {
      if (!sidebarTilesContainer) return;
      sidebarTilesContainer.innerHTML = "";
      for (const t of SUITE_TILES) {
        const btn = document.createElement("button");
        btn.type = "button";
        const baseCls = `suite-tile suite-tile--${t.accent}`;
        btn.className = t.comingSoon ? `${baseCls} suite-tile--coming-soon` : baseCls;
        btn.setAttribute("aria-label", t.comingSoon ? `${t.label} (coming soon)` : `Open ${t.label}`);
        btn.dataset.tourId = `sidebar-suite-${t.key}`;
        if (t.comingSoon) {
          btn.disabled = true;
          btn.setAttribute("aria-disabled", "true");
          btn.title = "Coming soon";
        }
        const arrowOrBadge = t.comingSoon
          ? `<span class="suite-tile-coming-soon" aria-hidden="true">Coming soon</span>`
          : `<span class="suite-tile-arrow" aria-hidden="true">↗</span>`;
        btn.innerHTML =
          `<span class="suite-tile-label">${escapeHtml(t.label)}</span>` +
          `<span class="suite-tile-tagline">${escapeHtml(t.tagline)}</span>` +
          arrowOrBadge;
        if (!t.comingSoon) {
          btn.addEventListener("click", () => {
            const url = tileUrl(t);
            window.open(url, "_blank", "noopener");
          });
        }
        sidebarTilesContainer.appendChild(btn);
      }
    }

    // 2026-04-29: long-form descriptions for each suite module, shown
    // in the skills modal so users learn what each module is for. Kept
    // local to the modal renderer rather than added to the SuiteTile
    // type because they're modal-display copy, not data-model.
    const SUITE_LONG_DESC: Record<string, string> = {
      strategy: (
        "Benchmarks your organization against peer CMHCs on revenue, " +
        "denials, panel mix, and credentialing throughput. Pulls from " +
        "our public payer + DOGE rate datasets and overlays your roster " +
        "to show where you sit on each KPI. Useful when board / leadership " +
        "asks 'how do we compare?'."
      ),
      roster: (
        "Single source of truth for your provider directory + the " +
        "credentialing pipeline. Tracks who's enrolled with which payer, " +
        "what's pending, what's expired, and surfaces re-credentialing " +
        "windows before they lapse. Roster reconciliation, NPI verification, " +
        "and run-by-run credentialing reports all live here."
      ),
      library: (
        "The shared corpus \u2014 payer manuals, state Medicaid handbooks, " +
        "federal regs, public CMS guidance. Anything anyone uploads as a " +
        "public source becomes searchable across every chat (with source " +
        "citation). Mobius retrieves from this library automatically when " +
        "you ask a payer / policy / regulatory question."
      ),
      vault: (
        "Your private workspace \u2014 recent chats, liked answers, open tasks, " +
        "and uploaded documents. Use the My Vault block in the sidebar to browse, " +
        "or click '\u2922 Open' to launch the full Vault panel."
      ),
    };

    function renderSkillsModal(): void {
      if (!modalBody) return;
      const html = [
        // Universal capabilities \u2014 baked into every chat
        '<div class="skills-section">',
          '<div class="skills-section-head">',
            '<span class="skills-section-eyebrow">Always on \u2014 baked into every chat</span>',
            '<span class="skills-section-hint">These five capabilities run in every turn. Mobius picks the right ones automatically based on your question.</span>',
          '</div>',
          '<div class="skills-themes-grid">',
            ...CHAT_THEMES.map((t) =>
              '<article class="skills-theme">' +
                '<header class="skills-theme-head">' +
                  `<h3 class="skills-theme-title">${escapeHtml(t.title)}</h3>` +
                  `<p class="skills-theme-tagline">${escapeHtml(t.tagline)}</p>` +
                '</header>' +
                `<p class="skills-theme-desc">${escapeHtml(t.description)}</p>` +
                '<p class="skills-theme-example">' +
                  '<span class="skills-theme-example-label">Try:</span> ' +
                  `\u201c${escapeHtml(t.examplePrompt)}\u201d` +
                '</p>' +
              '</article>'
            ),
          '</div>',
        '</div>',
        // Mobius modules \u2014 open-in-tab today, with descriptions
        '<div class="skills-section">',
          '<div class="skills-section-head">',
            '<span class="skills-section-eyebrow">Mobius modules</span>',
            '<span class="skills-section-hint">Standalone workspaces that complement chat. Open in a new tab today; deeper chat integration on the roadmap.</span>',
          '</div>',
          '<div class="skills-standalone-grid">',
            ...SUITE_TILES.map((t) =>
              `<article class="skills-standalone skills-standalone--${t.accent}${t.comingSoon ? ' skills-standalone--coming-soon' : ''}">` +
                `<h3 class="skills-standalone-title">${escapeHtml(t.label)}</h3>` +
                `<p class="skills-standalone-tagline">${escapeHtml(t.tagline)}</p>` +
                (SUITE_LONG_DESC[t.key]
                  ? `<p class="skills-standalone-desc">${escapeHtml(SUITE_LONG_DESC[t.key])}</p>`
                  : "") +
                (t.comingSoon
                  ? '<span class="skills-standalone-badge">Coming soon</span>'
                  : `<button type="button" class="skills-standalone-open" data-suite-key="${escapeHtml(t.key)}">` +
                      `Open ${escapeHtml(t.label)} \u2197` +
                    '</button>') +
              '</article>'
            ),
          '</div>',
        '</div>',
        // Coming soon
        '<div class="skills-section">',
          '<div class="skills-section-head">',
            '<span class="skills-section-eyebrow">Coming soon</span>',
          '</div>',
          '<div class="skills-coming-grid">',
            ...COMING_SOON.map((c) =>
              '<article class="skills-coming">' +
                `<h3 class="skills-coming-title">${escapeHtml(c.title)}</h3>` +
                `<p class="skills-coming-tagline">${escapeHtml(c.tagline)}</p>` +
                `<p class="skills-coming-desc">${escapeHtml(c.description)}</p>` +
              '</article>'
            ),
          '</div>',
        '</div>',
        // Trust footer
        '<div class="skills-trust">',
          '<span class="skills-trust-eyebrow">How Mobius protects you</span>',
          '<ul class="skills-trust-list">',
            '<li>Cached answers for repeated lookups — fast when it matters</li>',
            '<li>Hard refuse on questions about specific patients</li>',
            '<li>Every claim cited to its source</li>',
          '</ul>',
        '</div>',
      ].join("");
      modalBody.innerHTML = html;

      // Wire the standalone-product Open buttons inside the modal.
      modalBody.querySelectorAll<HTMLButtonElement>("[data-suite-key]").forEach((btn) => {
        btn.addEventListener("click", () => {
          const key = btn.getAttribute("data-suite-key") || "";
          const tile = SUITE_TILES.find((t) => t.key === key);
          if (!tile) return;
          closeSkillsModal();
          const url = tileUrl(tile);
          window.open(url, "_blank", "noopener");
        });
      });
    }

    // ── Open / close ─────────────────────────────────────────────────

    function openSkillsModal(): void {
      overlay?.removeAttribute("hidden");
      modal?.removeAttribute("hidden");
    }

    function closeSkillsModal(): void {
      overlay?.setAttribute("hidden", "");
      modal?.setAttribute("hidden", "");
    }

    // Initial render — sidebar tiles + modal body (modal stays hidden
    // until learn-more click).
    renderSidebarSuiteTiles();
    renderSkillsModal();

    // Sidebar "Learn more about chat skills →" → open modal.
    learnMoreBtn?.addEventListener("click", openSkillsModal);

    // Modal close button + overlay click + Esc.
    document.getElementById("skillsModalClose")?.addEventListener("click", closeSkillsModal);
    overlay?.addEventListener("click", closeSkillsModal);
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && !modal?.hasAttribute("hidden")) closeSkillsModal();
    });

    // Defensive: keep handlers for legacy element ids in case any
    // ancillary HTML (static/index.html) still references them. They
    // delegate to the same SUITE_TILES URL resolution and respect the
    // ``comingSoon`` flag so a temporarily-disabled tile doesn't open
    // a broken page when the legacy button is clicked.
    function _wireLegacySuiteButton(btnId: string, tileKey: string): void {
      const el = document.getElementById(btnId) as HTMLButtonElement | null;
      if (!el) return;
      const t = SUITE_TILES.find((x) => x.key === tileKey);
      if (t?.comingSoon) {
        el.disabled = true;
        el.classList.add("skill-sidebar-item--coming-soon");
        el.title = "Coming soon";
        el.setAttribute("aria-disabled", "true");
        // Append a small badge so the disabled state is legible.
        if (!el.querySelector(".skill-sidebar-coming-soon")) {
          const badge = document.createElement("span");
          badge.className = "skill-sidebar-coming-soon";
          badge.textContent = "Coming soon";
          el.appendChild(badge);
        }
        return;
      }
      el.addEventListener("click", () => {
        if (!t) return;
        closeSkillsModal();
        const url = tileUrl(t);
        window.open(url, "_blank", "noopener");
      });
    }
    // 2026-04-29: btnOpenSkillPipeline removed from sidebar HTML
    // (Credentialing folded into Roster). Wire-up kept for old open
    // tabs that still reference the button — null-safe via the
    // helper's element lookup.
    _wireLegacySuiteButton("btnOpenSkillPipeline", "roster");
    _wireLegacySuiteButton("btnOpenFinancialStrategy", "strategy");
    _wireLegacySuiteButton("btnOpenRoster", "roster");
  })();

  // ── Boot landing dashboard ──────────────────────────────────
  _initLandingDashboard();
}

run();

// ════════════════════════════════════════════════════════════════
// LANDING DASHBOARD  (ld-* namespace)
// ════════════════════════════════════════════════════════════════

let _ldAllRuns: any[] = [];

function _initLandingDashboard(): void {
  function _openPipeline(): void {
    window.open("http://localhost:3999/credentialing-home.html", "_blank", "noopener");
  }
  function _openRoster(): void {
    const base = (window as any).API_BASE || window.location.origin;
    const lastOrg = localStorage.getItem("lastOrg") || "";
    const rosterUrl = base + "/roster" + (lastOrg ? "?org=" + encodeURIComponent(lastOrg) : "");
    openRosterPanel(rosterUrl);
  }
  document.getElementById("ldNewRunBtn")?.addEventListener("click", _openPipeline);
  document.getElementById("ldStartRunBtn")?.addEventListener("click", _openPipeline);
  document.getElementById("ldSetupBtn")?.addEventListener("click", _openPipeline);

  document.getElementById("ldOrgSelect")?.addEventListener("change", function(this: HTMLSelectElement) {
    const org = this.value;
    if (!org) return;
    localStorage.setItem("lastOrg", org);
    _ldOnOrgSelected(org, (window as any).API_BASE || window.location.origin);
  });

  // roster link in dashboard
  document.getElementById("ldRosterOpenBtn")?.addEventListener("click", _openRoster);

  _ldBootstrap((window as any).API_BASE || window.location.origin);
}

async function _ldBootstrap(base: string): Promise<void> {
  const sel = document.getElementById("ldOrgSelect") as HTMLSelectElement | null;
  try {
    const r = await fetch(`${base}/chat/credentialing-runs?limit=50`);
    if (r.ok) _ldAllRuns = await r.json();
  } catch { _ldAllRuns = []; }

  const seen = new Set<string>(), orgs: string[] = [];
  for (const run of _ldAllRuns) {
    const o = (run.org_name || "").trim();
    if (o && !seen.has(o)) { seen.add(o); orgs.push(o); }
  }

  if (sel) {
    sel.innerHTML = orgs.length
      ? orgs.map(o => `<option value="${_ldEsc(o)}">${_ldEsc(o)}</option>`).join("")
      : '<option value="">No orgs yet — start a run</option>';
    const last = localStorage.getItem("lastOrg") || "";
    if (last && orgs.includes(last)) sel.value = last;
  }

  const activeOrg = sel?.value || orgs[0] || "";
  if (activeOrg) {
    if (activeOrg !== localStorage.getItem("lastOrg")) localStorage.setItem("lastOrg", activeOrg);
    _ldOnOrgSelected(activeOrg, base);
  } else {
    _ldRenderRunList([], base);
    _ldRosterNoData("Start your first credentialing run to populate.");
  }
}

function _ldOnOrgSelected(org: string, base: string): void {
  const link = document.getElementById("ldRosterLink") as HTMLAnchorElement | null;
  if (link) link.href = `${base}/roster?org=${encodeURIComponent(org)}`;
  const orgRuns = _ldAllRuns.filter((r: any) => (r.org_name || "").trim() === org);
  _ldRenderRunList(orgRuns, base);
  _ldRenderOrgSteps(orgRuns);
  _ldFetchRosterStats(org, base);
}

function _ldRenderOrgSteps(orgRuns: any[]): void {
  const vo = orgRuns[0]?.validated_outputs || {};
  const steps = [
    { chipId: "ldStep1Chip", valId: "ldStep1Val", key: "identify_org" },
    { chipId: "ldStep2Chip", valId: "ldStep2Val", key: "find_locations" },
  ];
  for (const s of steps) {
    const done = !!vo[s.key];
    const chip = document.getElementById(s.chipId);
    const val  = document.getElementById(s.valId);
    if (chip) chip.className = "ld-step-chip " + (done ? "ld-step-chip--done" : "ld-step-chip--idle");
    if (val) {
      if (s.key === "identify_org") {
        const npi = (typeof vo.identify_org === "object" && vo.identify_org?.npi) ? vo.identify_org.npi : "";
        val.textContent = done ? (npi || "✓") : "—";
      } else {
        const d = typeof vo.find_locations === "object" ? vo.find_locations : {} as any;
        const n = d.row_count ?? d.location_count ?? null;
        val.textContent = done ? (n != null ? n + " loc" : "✓") : "—";
      }
    }
  }
}

function _ldRenderRunList(runs: any[], base: string): void {
  const listEl = document.getElementById("ldRunList");
  if (!listEl) return;
  if (!runs.length) {
    listEl.innerHTML = '<div class="ld-empty-note">No runs for this org yet.</div>';
    return;
  }
  const STEP_META = [
    { id: "nppes_alignment",            short: "NPPES",      num: 3 },
    { id: "pml_alignment",              short: "PML",        num: 4 },
    { id: "find_associated_providers",  short: "Compliance", num: 5 },
    { id: "taxonomy_optimization",      short: "Taxonomy",   num: 6 },
  ];
  listEl.innerHTML = runs.slice(0, 8).map((run: any) => {
    const phase = run.phase || "pending";
    const vo    = run.validated_outputs || {};
    const badgeCls = phase === "complete" ? "ld-cap-badge--complete"
                   : (phase === "error" || phase === "failed") ? "ld-cap-badge--error"
                   : (phase === "running" || phase === "in_progress") ? "ld-cap-badge--running"
                   : "ld-cap-badge--pending";
    const badgeLbl = phase === "complete" ? "✓ Complete"
                   : (phase === "error" || phase === "failed") ? "✗ Error"
                   : phase === "running" ? "● Running"
                   : phase === "in_progress" ? "→ In progress" : "Pending";
    const capCls = phase === "complete" ? "ld-run-capsule--complete"
                 : (phase === "error" || phase === "failed") ? "ld-run-capsule--error"
                 : "ld-run-capsule--active";
    const mode = run.mode === "autopilot" ? "autopilot" : run.mode === "copilot" ? "co-pilot" : (run.mode || "");
    const dt   = run.updated_at ? new Date(run.updated_at).toLocaleDateString("en-US", { month: "short", day: "numeric" }) : "";
    const pills = STEP_META.map(s =>
      `<span class="ld-step-pill${vo[s.id] ? " ld-step-pill--done" : ""}" title="Step ${s.num}: ${s.short}">${s.short}</span>`
    ).join("");
    const runUrl = `${base}/pipeline?run_id=${encodeURIComponent(run.run_id)}`;
    return `<a class="ld-run-capsule ${capCls}" href="${runUrl}" target="_blank" rel="noopener">
      <div class="ld-cap-head">
        <div class="ld-cap-date">${dt}${mode ? " · " + _ldEsc(mode) : ""}</div>
        <span class="ld-cap-badge ${badgeCls}">${badgeLbl}</span>
      </div>
      <div class="ld-cap-steps-row">${pills}</div>
    </a>`;
  }).join("");
}

async function _ldFetchRosterStats(org: string, base: string): Promise<void> {
  ["ldStatTotal", "ldStatBillable", "ldStatAtRisk", "ldStatBlocked", "ldStatTasks"]
    .forEach(id => { const el = document.getElementById(id); if (el) el.textContent = "…"; });
  try {
    const r = await fetch(`${base}/chat/roster-truth/${encodeURIComponent(org)}?limit=500`);
    if (!r.ok) throw new Error(String(r.status));
    const data = await r.json();
    _ldRenderRosterStats(Array.isArray(data) ? data : (data.providers || data.items || []));
  } catch { _ldRosterNoData("Could not load roster."); }
}

function _ldRenderRosterStats(providers: any[]): void {
  const total = providers.length;
  const tasks = providers.filter((p: any) => { const t = p.open_tasks; return Array.isArray(t) ? t.length > 0 : false; }).length;
  let billable = 0, atRisk = 0, blocked = 0;
  for (const p of providers) {
    const snap    = (typeof p.nppes_snapshot === "object" && p.nppes_snapshot) ? p.nppes_snapshot : {} as any;
    const nppesOk = (snap.nppes_status || "").toUpperCase() === "A";
    const openCnt = Array.isArray(p.open_tasks) ? p.open_tasks.length : 0;
    const valid   = p.decision === "validated";
    if (valid && nppesOk && openCnt === 0) billable++;
    else if (valid) atRisk++;
    else blocked++;
  }
  if (billable + atRisk + blocked === 0 && total > 0) {
    billable = providers.filter((p: any) => p.decision === "validated").length;
    atRisk   = providers.filter((p: any) => p.decision === "flagged" || p.decision === "review").length;
    blocked  = total - billable - atRisk;
  }
  const ids: Record<string, number> = { ldStatTotal: total, ldStatBillable: billable, ldStatAtRisk: atRisk, ldStatBlocked: blocked, ldStatTasks: tasks };
  Object.entries(ids).forEach(([id, v]) => { const el = document.getElementById(id); if (el) _ldCountUp(el, v); });
  if (total > 0) {
    const bw = document.getElementById("ldBarWrap");
    if (bw) {
      bw.style.display = "";
      setTimeout(() => {
        const g = document.getElementById("ldBarGreen"), a = document.getElementById("ldBarAmber"), rd = document.getElementById("ldBarRed");
        if (g)  g.style.width = ((billable / total) * 100).toFixed(1) + "%";
        if (a)  a.style.width = ((atRisk / total) * 100).toFixed(1) + "%";
        if (rd) rd.style.width = ((blocked / total) * 100).toFixed(1) + "%";
      }, 30);
      const leg = document.getElementById("ldBarLegend");
      if (leg) leg.textContent = `${Math.round((billable / total) * 100)}% billable · ${atRisk} at risk · ${blocked} blocked`;
    }
  }
  const issueEl = document.getElementById("ldIssueList");
  if (issueEl) {
    const chips: { cls: string; icon: string; text: string }[] = [];
    if (blocked > 0) chips.push({ cls: "ld-issue-chip--crit", icon: "✗", text: `${blocked} provider${blocked > 1 ? "s" : ""} blocked from billing` });
    if (atRisk  > 0) chips.push({ cls: "ld-issue-chip--warn", icon: "⚠", text: `${atRisk} provider${atRisk > 1 ? "s" : ""} at risk — gaps exist` });
    if (tasks   > 0) chips.push({ cls: "ld-issue-chip--warn", icon: "◎", text: `${tasks} open credentialing task${tasks > 1 ? "s" : ""}` });
    if (!chips.length && total > 0) chips.push({ cls: "ld-issue-chip--ok", icon: "✓", text: "All providers clean — no gaps detected" });
    if (!total) chips.push({ cls: "ld-issue-chip", icon: "·", text: "No providers in roster yet" });
    issueEl.innerHTML = chips.map(c => `<div class="ld-issue-chip ${c.cls}"><span>${c.icon}</span><span>${c.text}</span></div>`).join("");
  }
  const lr = document.getElementById("ldLastRun");
  if (lr) lr.textContent = `${total} provider${total !== 1 ? "s" : ""} on record`;
}

function _ldRosterNoData(msg: string): void {
  ["ldStatTotal", "ldStatBillable", "ldStatAtRisk", "ldStatBlocked", "ldStatTasks"]
    .forEach(id => { const el = document.getElementById(id); if (el) el.textContent = "—"; });
  const issueEl = document.getElementById("ldIssueList");
  if (issueEl) issueEl.innerHTML = `<div class="ld-issue-chip">${_ldEsc(msg)}</div>`;
}

function _ldCountUp(el: HTMLElement, target: number): void {
  el.textContent = "0";
  if (!target) { el.textContent = "0"; return; }
  const steps = 18, dur = 500;
  let cur = 0;
  const iv = setInterval(() => {
    cur = Math.min(cur + Math.ceil(target / steps), target);
    el.textContent = String(cur);
    if (cur >= target) clearInterval(iv);
  }, dur / steps);
}

function _ldEsc(str: string): string {
  return String(str || "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

export {};
