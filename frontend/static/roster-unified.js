/**
 * roster-unified.js
 * Clean state + API + render module for the unified Roster & Credentialing screen.
 *
 * API surface (existing endpoints — no backend changes needed today):
 *   GET /chat/credentialing-runs?limit=50           → org discovery + runId
 *   GET /chat/roster-truth/{org}?limit=500          → provider rows
 *   GET /chat/credentialing-runs/{runId}?full=1     → pipeline step state
 *   POST /chat/credentialing-runs                   → start new run
 *   POST /chat/roster-truth/{org}/provider          → add provider
 *   GET  /chat/roster-reconcile/uploads?org_name=…  → last run date
 *   GET  https://npiregistry.cms.hhs.gov/api/…      → live NPI lookup
 *
 * When the backend dashboard endpoint is ready, swap loadOrg() to:
 *   GET /orgs/{org}/dashboard  → single call returns stats + pipeline + providers
 */

'use strict';

/* ── State ───────────────────────────────────────────── */
const S = {
  org:       null,   // active org name string
  runId:     null,   // active credentialing run id
  providers: [],     // raw provider array from roster-truth
  run:       null,   // full run object (steps, status)
  filter:    'all',  // 'all' | 'ok' | 'pml' | 'tasks'
  search:    '',     // search query
  sort:      { col: 'name', dir: 'asc' },
  loading:   false,
};

const STEPS = [
  { id: 'identity',   label: 'Identity' },
  { id: 'locations',  label: 'Locations' },
  { id: 'nppes',      label: 'NPPES' },
  { id: 'pml',        label: 'Medicaid' },
  { id: 'compliance', label: 'Compliance' },
  { id: 'taxonomy',   label: 'Taxonomy' },
];

/* ── Bootstrap ───────────────────────────────────────── */
document.addEventListener('DOMContentLoaded', () => {
  const params = new URLSearchParams(location.search);
  const urlOrg = params.get('org');
  const stored = localStorage.getItem('lastOrg');
  const org = urlOrg || stored;

  bindCommandInput();
  bindChips();

  if (org) {
    loadOrg(org);
  } else {
    loadOrgList();
  }
});

/* ── API helpers ─────────────────────────────────────── */
async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json', ...opts.headers },
    ...opts,
  });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText} — ${path}`);
  return res.json();
}

/* ── Load org list (no org known yet) ───────────────── */
async function loadOrgList() {
  showState('loading');
  try {
    const data = await api('/chat/credentialing-runs?limit=50');
    const runs = data.runs || [];
    const orgs = [...new Set(runs.map(r => r.org_name).filter(Boolean))];

    if (orgs.length === 0) { showState('empty'); return; }
    if (orgs.length === 1)  { loadOrg(orgs[0]); return; }

    renderOrgSelector(orgs);
    showState('selector');
  } catch (e) {
    showError(e.message);
  }
}

/* ── Load a specific org ─────────────────────────────── */
async function loadOrg(org) {
  S.org = org;
  S.filter = 'all';
  S.search = '';
  localStorage.setItem('lastOrg', org);

  // Update header org pill
  const pill = document.getElementById('ru-org-name');
  if (pill) pill.textContent = org;

  showState('loading');

  try {
    // Parallel: providers + runs list
    const [truthData, runsData] = await Promise.all([
      api(`/chat/roster-truth/${encodeURIComponent(org)}?limit=500`),
      api('/chat/credentialing-runs?limit=50'),
    ]);

    S.providers = truthData.providers || [];

    // Find the latest run for this org
    const runs = (runsData.runs || []).filter(r => r.org_name === org);
    const latest = runs[0] || null;
    S.runId = latest?.run_id || null;

    // Load full run details if we have a runId
    if (S.runId) {
      const runFull = await api(`/chat/credentialing-runs/${S.runId}?full=1`);
      S.run = runFull;
    } else {
      S.run = null;
    }

    renderAll();
    showState('content');
  } catch (e) {
    showError(e.message);
  }
}

/* ── Render everything ───────────────────────────────── */
function renderAll() {
  renderStats();
  renderSteps();
  renderTable();
}

/* ── Stats strip ─────────────────────────────────────── */
function renderStats() {
  const ps = S.providers;
  const total     = ps.length;
  const nppes_ok  = ps.filter(p => p.nppes_snapshot?.nppes_status === 'A').length;
  const pml_gaps  = ps.filter(p => pmlHasGap(p)).length;
  const open_tasks = ps.reduce((n, p) => n + (p.open_tasks?.length || 0), 0);

  setText('ru-stat-total',      total);
  setText('ru-stat-nppes',      nppes_ok);
  setText('ru-stat-pml',        pml_gaps);
  setText('ru-stat-tasks',      open_tasks);

  // Update filter chip counts
  setText('ru-fc-all',   total);
  setText('ru-fc-ok',    ps.filter(p => providerIsClean(p)).length);
  setText('ru-fc-pml',   pml_gaps);
  setText('ru-fc-tasks', open_tasks);
}

/* ── Pipeline step strip ─────────────────────────────── */
function renderSteps() {
  const el = document.getElementById('ru-steps');
  if (!el) return;

  const stepOutputs = S.run?.step_outputs || {};

  const html = STEPS.map(s => {
    const out    = stepOutputs[s.id];
    const status = out?.status || 'pending';
    let cls = 'mbx-step mbx-step-pending';
    let prefix = '';

    if (status === 'completed') { cls = 'mbx-step mbx-step-done'; prefix = '✓ '; }
    else if (status === 'running' || status === 'active') {
      cls = 'mbx-step mbx-step-active';
      prefix = '<span class="mbx-dot mbx-dot-pending mbx-pulse mbx-step-dot" style="display:inline-block;vertical-align:middle;margin-right:3px;"></span>';
    }
    return `<div class="${cls}" title="${s.label}">${prefix}${esc(s.label)}</div>`;
  }).join('');

  el.innerHTML = html;
}

/* ── Provider table ──────────────────────────────────── */
function renderTable() {
  const el = document.getElementById('ru-table-body');
  if (!el) return;

  let rows = applyFilter(S.providers);
  rows = applySearch(rows);
  rows = applySort(rows);

  if (rows.length === 0) {
    el.innerHTML = `<tr><td colspan="6" class="mbx-table-empty">No providers match this filter.</td></tr>`;
    return;
  }

  el.innerHTML = rows.map(p => {
    const name      = esc(p.provider_name || '—');
    const npi       = esc(p.npi_validated || p.npi_roster || '—');
    const spec      = esc(p.specialty || '—');
    const nppesStatus = nppesLabel(p);
    const pmlStatus   = pmlLabel(p);
    const taskCount   = p.open_tasks?.length || 0;
    const taskBadge   = taskCount > 0
      ? `<span class="mbx-badge mbx-badge-warning">${taskCount} task${taskCount > 1 ? 's' : ''}</span>`
      : '';

    return `
      <tr onclick="openProviderDrawer(${JSON.stringify(npi.replace(/"/g, '&quot;'))})" data-npi="${npi}">
        <td><span class="mbx-mono" style="font-size:0.75em;color:var(--mobius-text-secondary);">${npi}</span></td>
        <td style="font-weight:500;">${name}</td>
        <td style="color:var(--mobius-text-secondary);font-size:0.8rem;">${spec}</td>
        <td>${nppesStatus}</td>
        <td>${pmlStatus} ${taskBadge}</td>
        <td style="text-align:right;">
          <button class="mbx-btn mbx-btn-ghost mbx-btn-sm" onclick="event.stopPropagation(); openProviderDrawer('${npi}')">↗</button>
        </td>
      </tr>`;
  }).join('');
}

/* ── Filter helpers ──────────────────────────────────── */
function providerIsClean(p) {
  return p.nppes_snapshot?.nppes_status === 'A'
    && !pmlHasGap(p)
    && !(p.open_tasks?.length > 0);
}

function pmlHasGap(p) {
  if (p.nppes_snapshot?.pml_gap) return true;
  return p.open_tasks?.some(t => t.dim === 'pml') || false;
}

function applyFilter(ps) {
  switch (S.filter) {
    case 'ok':    return ps.filter(p => providerIsClean(p));
    case 'pml':   return ps.filter(p => pmlHasGap(p));
    case 'tasks': return ps.filter(p => (p.open_tasks?.length || 0) > 0);
    default:      return ps;
  }
}

function applySearch(ps) {
  const q = S.search.trim().toLowerCase();
  if (!q) return ps;
  return ps.filter(p => {
    const name = (p.provider_name || '').toLowerCase();
    const npi  = (p.npi_validated || p.npi_roster || '').toLowerCase();
    const spec = (p.specialty || '').toLowerCase();
    return name.includes(q) || npi.includes(q) || spec.includes(q);
  });
}

function applySort(ps) {
  return [...ps].sort((a, b) => {
    let av, bv;
    switch (S.sort.col) {
      case 'npi':  av = a.npi_validated || a.npi_roster || ''; bv = b.npi_validated || b.npi_roster || ''; break;
      case 'spec': av = a.specialty || ''; bv = b.specialty || ''; break;
      default:     av = a.provider_name || ''; bv = b.provider_name || '';
    }
    const cmp = av.localeCompare(bv);
    return S.sort.dir === 'asc' ? cmp : -cmp;
  });
}

function sortBy(col) {
  if (S.sort.col === col) {
    S.sort.dir = S.sort.dir === 'asc' ? 'desc' : 'asc';
  } else {
    S.sort.col = col; S.sort.dir = 'asc';
  }
  // Update header indicators
  document.querySelectorAll('.ru-th-sort').forEach(th => {
    const c = th.dataset.col;
    th.textContent = th.dataset.label + (S.sort.col === c ? (S.sort.dir === 'asc' ? ' ↑' : ' ↓') : '');
  });
  renderTable();
}

/* ── Badge helpers ───────────────────────────────────── */
function nppesLabel(p) {
  const s = p.nppes_snapshot?.nppes_status;
  if (s === 'A') return `<span class="mbx-badge mbx-badge-active">Active</span>`;
  if (s === 'D') return `<span class="mbx-badge mbx-badge-error">Inactive</span>`;
  return `<span class="mbx-badge mbx-badge-muted">Unknown</span>`;
}

function pmlLabel(p) {
  if (pmlHasGap(p)) return `<span class="mbx-badge mbx-badge-warning">Gap</span>`;
  if (p.nppes_snapshot?.nppes_status === 'A') return `<span class="mbx-badge mbx-badge-active">Active</span>`;
  return `<span class="mbx-badge mbx-badge-muted">—</span>`;
}

/* ── Filter chip interaction ─────────────────────────── */
function setFilter(filter) {
  S.filter = filter;
  document.querySelectorAll('.ru-filter-chip').forEach(c => {
    c.classList.toggle('ru-chip-active', c.dataset.filter === filter);
  });
  renderTable();
}

/* ── Search ──────────────────────────────────────────── */
function onSearch(val) {
  S.search = val;
  renderTable();
}

/* ── Command input ───────────────────────────────────── */
function bindCommandInput() {
  const input = document.getElementById('ru-cmd-input');
  if (!input) return;
  input.addEventListener('keydown', e => {
    if (e.key === 'Enter') {
      handleCommand(input.value.trim());
      input.value = '';
    }
  });
}

function handleCommand(text) {
  if (!text) return;
  const t = text.toLowerCase();

  if (t.startsWith('run') || t.includes('credentialing')) {
    runCredentialing();
  } else if (t.includes('export') || t.includes('csv')) {
    exportCSV();
  } else if (t.includes('add provider') || t.includes('add prov')) {
    openAddProviderModal();
  } else if (t.includes('upload') || t.includes('roster')) {
    location.href = '/roster-ui/upload.html';
  } else {
    // treat as search
    S.search = text;
    const el = document.getElementById('ru-search');
    if (el) el.value = text;
    renderTable();
  }
}

function bindChips() {
  document.querySelectorAll('.ru-cmd-chip').forEach(chip => {
    chip.addEventListener('click', () => {
      handleCommand(chip.dataset.cmd);
    });
  });
}

/* ── Run credentialing ───────────────────────────────── */
async function runCredentialing() {
  if (!S.org) return;
  // Open pipeline in new tab (existing implementation handles the full flow)
  window.open(`/pipeline?org=${encodeURIComponent(S.org)}&run=${S.runId || ''}`, '_blank');
}

/* ── Export CSV ──────────────────────────────────────── */
function exportCSV() {
  if (!S.providers.length) return;

  const headers = ['NPI', 'Name', 'Specialty', 'City', 'State', 'NPPES Status', 'PML Gap', 'Open Tasks', 'Decision'];
  const rows = S.providers.map(p => [
    p.npi_validated || p.npi_roster || '',
    p.provider_name || '',
    p.specialty || '',
    p.city || '',
    p.state_cd || '',
    p.nppes_snapshot?.nppes_status === 'A' ? 'Active' : (p.nppes_snapshot?.nppes_status || 'Unknown'),
    pmlHasGap(p) ? 'Yes' : 'No',
    (p.open_tasks?.length || 0).toString(),
    p.decision || '',
  ]);

  const csv = [headers, ...rows].map(r => r.map(cell => csvCell(String(cell))).join(',')).join('\n');
  const blob = new Blob([csv], { type: 'text/csv' });
  const url  = URL.createObjectURL(blob);
  const a    = Object.assign(document.createElement('a'), {
    href: url, download: `${S.org || 'roster'}-${new Date().toISOString().slice(0,10)}.csv`
  });
  a.click(); URL.revokeObjectURL(url);
}

function csvCell(v) {
  return /[,"\n]/.test(v) ? `"${v.replace(/"/g, '""')}"` : v;
}

/* ── Provider detail drawer ──────────────────────────── */
function openProviderDrawer(npi) {
  const p = S.providers.find(x => (x.npi_validated || x.npi_roster) === npi);
  if (!p) return;

  const drawerBody = document.getElementById('ru-drawer-body');
  const drawerTitle = document.getElementById('ru-drawer-title');
  if (!drawerTitle || !drawerBody) return;

  drawerTitle.textContent = p.provider_name || 'Provider Detail';

  const taskRows = (p.open_tasks || []).map(t => `
    <tr>
      <td>${esc(t.dim || '—')}</td>
      <td><span class="mbx-badge ${t.severity === 'high' ? 'mbx-badge-error' : 'mbx-badge-warning'}">${esc(t.severity || 'open')}</span></td>
      <td style="color:var(--mobius-text-secondary);font-size:0.78rem;">${esc(t.description || '—')}</td>
    </tr>`).join('');

  drawerBody.innerHTML = `
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:0.5rem;margin-bottom:1rem;">
      ${drawerField('NPI',       p.npi_validated || p.npi_roster || '—')}
      ${drawerField('Specialty', p.specialty || '—')}
      ${drawerField('Location',  [p.city, p.state_cd].filter(Boolean).join(', ') || '—')}
      ${drawerField('Decision',  p.decision || '—')}
    </div>
    <hr class="mbx-divider">
    <div style="display:flex;gap:0.5rem;margin-bottom:0.9rem;flex-wrap:wrap;">
      <div>
        <div style="font-size:0.7rem;color:var(--mobius-text-secondary);margin-bottom:0.3rem;">NPPES</div>
        ${nppesLabel(p)}
      </div>
      <div>
        <div style="font-size:0.7rem;color:var(--mobius-text-secondary);margin-bottom:0.3rem;">Medicaid PML</div>
        ${pmlLabel(p)}
      </div>
    </div>
    ${p.ai_summary_short ? `<p style="font-size:0.82rem;color:var(--mobius-text-secondary);margin-bottom:1rem;line-height:1.55;">${esc(p.ai_summary_short)}</p><hr class="mbx-divider">` : ''}
    ${taskRows ? `
      <div style="font-size:0.75rem;font-weight:600;margin-bottom:0.4rem;">Open tasks (${p.open_tasks.length})</div>
      <table class="mbx-table" style="font-size:0.78rem;">
        <tr><th>Dimension</th><th>Severity</th><th>Note</th></tr>
        ${taskRows}
      </table>` : ''}
  `;

  document.getElementById('ru-drawer-backdrop').classList.remove('mbx-hidden');
  document.getElementById('ru-drawer').classList.remove('mbx-hidden');
}

function drawerField(label, value) {
  return `<div>
    <div style="font-size:0.7rem;color:var(--mobius-text-secondary);margin-bottom:0.15rem;">${label}</div>
    <div style="font-size:0.83rem;">${esc(String(value))}</div>
  </div>`;
}

function closeProviderDrawer() {
  document.getElementById('ru-drawer-backdrop')?.classList.add('mbx-hidden');
  document.getElementById('ru-drawer')?.classList.add('mbx-hidden');
}

/* ── Add provider modal ──────────────────────────────── */
function openAddProviderModal() {
  document.getElementById('ru-add-modal')?.classList.remove('mbx-hidden');
}

function closeAddProviderModal() {
  document.getElementById('ru-add-modal')?.classList.add('mbx-hidden');
  const npiInput = document.getElementById('ru-ap-npi');
  const nameInput = document.getElementById('ru-ap-name');
  if (npiInput)  npiInput.value = '';
  if (nameInput) nameInput.value = '';
  setText('ru-ap-npi-hint', '');
}

let _npiLookupTimer;
function onNpiInput(val) {
  clearTimeout(_npiLookupTimer);
  const hint = document.getElementById('ru-ap-npi-hint');
  if (!hint) return;
  if (val.length < 10) { hint.textContent = ''; return; }
  hint.textContent = 'Looking up…';
  hint.style.color = 'var(--mobius-text-secondary)';
  _npiLookupTimer = setTimeout(() => lookupNpi(val), 400);
}

async function lookupNpi(npi) {
  const hint = document.getElementById('ru-ap-npi-hint');
  try {
    const url = `https://npiregistry.cms.hhs.gov/api/?version=2.1&number=${encodeURIComponent(npi)}&limit=1`;
    const data = await fetch(url).then(r => r.json());
    const result = data?.results?.[0];
    if (!result) {
      hint.textContent = 'NPI not found in NPPES';
      hint.style.color = 'var(--mobius-error)';
      return;
    }
    const basic = result.basic || {};
    const name = [basic.first_name, basic.last_name].filter(Boolean).join(' ')
      || result.organization_name || '—';
    hint.textContent = `✓ ${name}`;
    hint.style.color = 'var(--mobius-success)';
    const nameEl = document.getElementById('ru-ap-name');
    if (nameEl && !nameEl.value) nameEl.value = name;
  } catch {
    hint.textContent = 'NPPES lookup failed';
    hint.style.color = 'var(--mobius-text-secondary)';
  }
}

async function submitAddProvider() {
  if (!S.org) return;
  const npi  = document.getElementById('ru-ap-npi')?.value?.trim();
  const name = document.getElementById('ru-ap-name')?.value?.trim();
  if (!npi)  { alert('NPI is required'); return; }

  const btn = document.getElementById('ru-ap-submit');
  if (btn) { btn.disabled = true; btn.textContent = 'Adding…'; }

  try {
    await api(`/chat/roster-truth/${encodeURIComponent(S.org)}/provider`, {
      method: 'POST',
      body: JSON.stringify({ npi, provider_name: name }),
    });
    closeAddProviderModal();
    await loadOrg(S.org);
  } catch (e) {
    alert(`Failed: ${e.message}`);
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = 'Add Provider'; }
  }
}

/* ── Org selector ────────────────────────────────────── */
function renderOrgSelector(orgs) {
  const el = document.getElementById('ru-org-selector');
  if (!el) return;
  el.innerHTML = orgs.map(o =>
    `<option value="${esc(o)}">${esc(o)}</option>`
  ).join('');
}

function onOrgSelected(val) {
  if (val) loadOrg(val);
}

/* ── UI state helpers ────────────────────────────────── */
function showState(state) {
  ['loading', 'content', 'empty', 'selector', 'error'].forEach(s => {
    document.getElementById(`ru-state-${s}`)?.classList.toggle('mbx-hidden', s !== state);
  });
}

function showError(msg) {
  const el = document.getElementById('ru-error-msg');
  if (el) el.textContent = msg;
  showState('error');
}

function setText(id, val) {
  const el = document.getElementById(id);
  if (el) el.textContent = val;
}

function esc(s) {
  return String(s ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

/* ── Expose globals needed by inline handlers ────────── */
window.setFilter         = setFilter;
window.onSearch          = onSearch;
window.sortBy            = sortBy;
window.openProviderDrawer = openProviderDrawer;
window.closeProviderDrawer = closeProviderDrawer;
window.openAddProviderModal = openAddProviderModal;
window.closeAddProviderModal = closeAddProviderModal;
window.onNpiInput        = onNpiInput;
window.submitAddProvider = submitAddProvider;
window.onOrgSelected     = onOrgSelected;
window.runCredentialing  = runCredentialing;
window.exportCSV         = exportCSV;
window.handleCommand     = handleCommand;
