const API = window.location.origin;
const PLAN = [
  { id: 'identify_org',             short: 'Identity',    label: 'Establish organization identity',                      desc: 'Confirm who you are — org NPIs, legal name, and registry presence.' },
  { id: 'find_locations',           short: 'Locations',   label: 'Confirm approved service locations',                   desc: 'Lock down every site where your clinicians are authorized to practice.' },
  { id: 'nppes_alignment',          short: 'Roster',      label: 'Roster Validation — NPPES',                            desc: 'Match each roster provider to their NPPES record — catch missing or mismatched entries.' },
  { id: 'pml_alignment',            short: 'Payor',       label: 'Payor Enrollment',                                     desc: 'Validate enrollment status across payors — Medicaid PML and others.' },
  { id: 'find_associated_providers',short: 'Compliance',  label: 'Identify ghost billing and compliance risks',          desc: 'Find providers billing under your NPI who are not on your approved roster.' },
  { id: 'taxonomy_optimization',    short: 'Taxonomy',    label: 'Ensure billing taxonomy codes are perfectly aligned',  desc: 'Audit taxonomy codes — wrong codes leave revenue on the table.' },
];

let runId = null;
let pollTimer = null;
let lastRun = null;
let _lastStepBodyKey = null;
let _viewStepId = null;       // null = follow active step; stepId = viewing a historical step
let _lastPendingStepId = null; // track when active step changes so we auto-clear view
let _reconTasks = null;       // null = not yet built; array of task objects once initialized
let _autoLoadRosterAttempted = false;

// ── Step sealing helpers ──────────────────────────────────────────────────────
// A step is "sealed" when it has been completed (status=done or skipped) in the
// current run. Sealed steps are served from persisted run state — no recompute.
// Only an explicit "Re-run step" or a new run breaks the seal.
function _isStepSealed(stepId) {
  const steps = lastRun?.orchestrator_state?.steps || {};
  const stepArr = Array.isArray(steps)
    ? steps
    : Object.entries(steps).map(([id, s]) => ({ id, ...(typeof s === 'object' ? s : {}) }));
  const st = stepArr.find(s => s.id === stepId);
  return st?.status === 'done' || st?.status === 'skipped';
}

function _stepCompletedAt(stepId) {
  const steps = lastRun?.orchestrator_state?.steps || {};
  const stepArr = Array.isArray(steps)
    ? steps
    : Object.entries(steps).map(([id, s]) => ({ id, ...(typeof s === 'object' ? s : {}) }));
  const st = stepArr.find(s => s.id === stepId);
  return st?.completed_at || st?.updated_at || null;
}

// Render a subtle "sealed" banner for completed steps — reassures the user
// that data is from the original run, not recomputed, and lets them re-run explicitly.
function _buildSealedBanner(stepId) {
  const at = _stepCompletedAt(stepId);
  const dateStr = at
    ? new Date(at).toLocaleString('en-US', { month: 'short', day: 'numeric', year: 'numeric', hour: 'numeric', minute: '2-digit' })
    : 'this run';
  return `<div class="sealed-banner" style="display:flex;align-items:center;gap:.5rem;padding:.28rem .7rem;background:#f0fdf4;border:1px solid #bbf7d0;border-radius:6px;margin-bottom:.6rem;font-size:.7rem;color:#166534">
    <span>✓ Results from ${esc(dateStr)} · sealed</span>
    <span style="flex:1"></span>
    <button onclick="_requestStepRerun('${stepId}')"
      style="font-size:.67rem;background:none;border:1px solid #16a34a;color:#166534;border-radius:4px;padding:.1rem .45rem;cursor:pointer;white-space:nowrap">
      Re-run step
    </button>
  </div>`;
}

async function _requestStepRerun(stepId) {
  if (!runId) return;
  const label = { nppes_alignment: 'NPPES Alignment', find_locations: 'Location Confirmation',
    pml_alignment: 'PML/Payor Alignment', identify_org: 'Org Identification' }[stepId] || stepId;
  if (!confirm(`Re-run ${label}?\n\nThis will clear the current sealed result and fetch fresh data from all sources. This cannot be undone within this run.`)) return;
  feEmit(`Requesting re-run of ${label}…`);
  try {
    await fetch(`${API}/chat/credentialing-runs/${encodeURIComponent(runId)}/validate`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ step_id: stepId, validated_output: { rerun: true, force_refresh: true } }),
    });
    const r = await fetch(`${API}/chat/credentialing-runs/${encodeURIComponent(runId)}?full=1`);
    if (r.ok) { const d = await r.json(); _viewStepId = stepId; render(d); schedulePoll(d); }
    feEmit(`✓ ${label} re-run started`, 'ok');
  } catch (e) {
    feEmit(`Re-run failed — ${e.message}`, 'error');
  }
}

// ── Alignment task suggestions (per dimension × flag) ────────────────────────
// Keys: "<dimension>:<flag>"  Values: array of suggested action strings
const _ALIGN_SUGGESTIONS = {
  "name:drift": [
    "Note: minor name difference \u2014 acceptable",
    "Update roster name to match NPPES",
    "Verify name directly with provider",
  ],
  "name:mismatch": [
    "Update roster name to match NPPES record",
    "Flag for identity verification \u2014 may be wrong match",
    "Update NPPES to reflect legal name change",
    "Verify with provider credentialing file",
  ],
  "taxonomy:mismatch": [
    "Update roster specialty to match NPPES taxonomy",
    "Request provider update NPPES taxonomy",
    "Verify against provider credentialing file",
    "Defer to Step 9 taxonomy review",
  ],
  "address:mismatch": [
    "Confirm provider active practice state",
    "Update roster state field",
    "Request NPPES practice location update",
  ],
  "status:deactivated": [
    "Remove from active credentialing immediately",
    "Contact provider to reactivate NPI via NPPES",
    "Apply for a new NPI via CMS I&A",
    "Suspend billing under this NPI",
  ],
  "zip:mismatch": [
    "Confirm provider practice zip code — ZIP-5 does not match NPPES",
    "Update NPPES practice address with correct ZIP code",
    "Verify provider is licensed to practice at this location",
  ],
  "zip:drift": [
    "Confirm ZIP+4 extension with provider — ZIP-5 matches but +4 differs",
    "Update NPPES practice address ZIP+4 for FL Medicaid accuracy",
    "Note: ZIP-5 match is sufficient for most payers; +4 needed for FL Medicaid",
  ],
  "credential:mismatch": [
    "Verify credential with provider license board",
    "Update NPPES credential field to match",
    "Request provider update credentials in NPPES",
    "Review — may be credential abbreviation difference",
  ],
  "credential:drift": [
    "Note: partial credential match — review additional designations",
    "Confirm all credentials are current and active",
    "Request provider update NPPES credential field",
  ],
};

function jumpToStep(stepId) {
  // Close any in-flight roster SSE stream when navigating away
  if (window._rosterSse) {
    try { window._rosterSse.close(); } catch(_) {}
    window._rosterSse = null;
  }
  _viewStepId = stepId;
  _lastStepBodyKey = null; // force re-render
  if (lastRun) render(lastRun);
}

function jumpToCurrentStep() {
  _viewStepId = null;
  _lastStepBodyKey = null;
  if (lastRun) render(lastRun);
}
let skillBase = '';  // base URL of the provider-roster-credentialing skill server

// ── Restore from URL + fetch skill base URL ───────────────────
async function initPipeline() {
  // Get the skill server base URL from chat config
  try {
    const r = await fetch(`${API}/chat/skills/urls`);
    if (r.ok) {
      const d = await r.json();
      skillBase = (d.roster_base || '').replace(/\/+$/, '');
    }
  } catch { /* non-fatal */ }

  // Restore sidebar collapse state
  if (localStorage.getItem('plSidebarCollapsed') === '1') {
    const sb = document.getElementById('plSidebar');
    if (sb) { sb.classList.add('collapsed'); _updateSbChevron(true); }
  }

  const p = new URLSearchParams(window.location.search);
  const rid = p.get('run_id');
  if (rid) {
    runId = rid; showPipeline(); poll();
  } else {
    loadDashboard();
  }
}
