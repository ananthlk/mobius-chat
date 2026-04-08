/**
 * roster-page.js — init and interaction logic for the standalone /roster page.
 *
 * Depends on pipeline-nppes.js (for _renderRosterTruthRows, openRosterProviderDrawer,
 * _buildRosterProvDrawerHtml, closeRosterProviderDrawer) and the shims in roster.html.
 *
 * The page is org-aware: org is read from ?org= in the URL, or from the last
 * run stored in localStorage key 'lastOrg'. Users can also pick from a dropdown
 * if multiple orgs exist.
 */

'use strict';

// ── State ─────────────────────────────────────────────────────────────────────
let _rosterPageOrg    = '';     // current org name
let _rosterPageFilter = 'all';  // active filter
let _rosterPageSearch = '';     // search string
let _rosterPageAll    = [];     // full provider array from DB (window._rosterTruth)

// ── Init ──────────────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', async () => {
  // Initialise the reusable Mobius chat widget (self-injects #chatDrawer if absent)
  if (typeof initMobiusChatWidget === 'function') {
    initMobiusChatWidget({
      pageName: 'Provider Roster',
      placeholder: 'Ask about providers, credentials, billing…',
      contextFn: () => {
        const org  = _rosterPageOrg || window.lastRun?.org_name || '';
        const sel  = document.querySelector('.rt-card.selected');
        const prov = sel ? (sel.dataset.name || sel.dataset.npi || '') : '';
        return org
          ? `[Roster context: org="${org}"${prov ? `, viewing provider="${prov}"` : ''}] `
          : '';
      },
    });
  }

  // 1. Detect org from URL or localStorage
  const urlOrg = new URLSearchParams(window.location.search).get('org') || '';
  const lsOrg  = localStorage.getItem('lastOrg') || '';
  const org    = urlOrg || lsOrg;

  if (org) {
    await _rosterPageLoad(org);
  } else {
    // No org — try to discover available orgs from recent runs
    await _rosterPageLoadOrgList();
  }
});

// ── Load org ──────────────────────────────────────────────────────────────────
async function _rosterPageLoad(org) {
  _rosterPageOrg = org;
  localStorage.setItem('lastOrg', org);

  // Update URL without reload
  const u = new URL(window.location.href);
  u.searchParams.set('org', org);
  window.history.replaceState({}, '', u.toString());

  // Set lastRun shim so _loadRosterTruth / openRosterProviderDrawer work
  window.lastRun = { org_name: org };

  // Keep the floating chat widget header in sync with the active org
  if (typeof setChatOrgLabel === 'function') setChatOrgLabel(org);

  // Show org pill
  const pill = document.getElementById('rosterOrgPill');
  const name = document.getElementById('rosterOrgName');
  if (pill) { pill.style.display = ''; }
  if (name) name.textContent = org;

  // Update last-run stat from runs API
  _fetchLastRunDate(org);

  // Load providers
  _showState('loading');
  try {
    const resp = await fetch(`/chat/roster-truth/${encodeURIComponent(org)}?limit=500`);
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const data = await resp.json();
    window._rosterTruth = data.providers || [];
    _rosterPageAll = window._rosterTruth;

    if (!_rosterPageAll.length) {
      _showState('empty');
      _updateStats([]);
      return;
    }

    _showState('list');
    _rosterPageApply();
    feEmit(`Loaded ${_rosterPageAll.length} providers for ${org}`, 'ok');

    // One-liner summaries are now pre-computed by the post-run AI flush and returned
    // directly in the list API response (ai_summary_short).  No prefetch needed.
  } catch (e) {
    _showState('empty');
    feEmit(`Failed to load roster — ${e.message}`, 'error');
  }
}

// _prefetchRosterSummaries removed — AI one-liners are now pre-computed by the
// post-run AI summary flush and returned in the list API as `ai_summary_short`.
// The pipeline-nppes.js list render reads p.ai_summary_short directly.

async function _fetchLastRunDate(org) {
  try {
    const r = await fetch(`/chat/roster-reconcile/uploads?org_name=${encodeURIComponent(org)}&limit=1`);
    if (!r.ok) return;
    const d = await r.json();
    const latest = (d.uploads || [])[0];
    if (latest?.created_at) {
      const el = document.getElementById('statLastRun');
      if (el) el.textContent = new Date(latest.created_at).toLocaleDateString('en-US',{month:'short',day:'numeric',year:'numeric'});
    }
  } catch { /* non-fatal */ }
}

// ── Org selector (when no org in URL) ────────────────────────────────────────
async function _rosterPageLoadOrgList() {
  _showState('no-org');
  try {
    const r = await fetch('/chat/credentialing-runs?limit=50');
    if (!r.ok) return;
    const d = await r.json();
    const runs = d.runs || [];
    const orgs = [...new Set(runs.map(r => r.org_name).filter(Boolean))];
    if (!orgs.length) return;

    if (orgs.length === 1) {
      await _rosterPageLoad(orgs[0]);
      return;
    }

    // Show selector
    const wrap = document.getElementById('rosterOrgSelectorWrap');
    const sel  = document.getElementById('rosterOrgSelector');
    if (wrap) wrap.style.display = '';
    if (sel) {
      orgs.forEach(o => {
        const opt = document.createElement('option');
        opt.value = o; opt.textContent = o;
        sel.appendChild(opt);
      });
    }
  } catch { /* non-fatal */ }
}

function rosterOrgSelected(org) {
  if (!org) return;
  const wrap = document.getElementById('rosterOrgSelectorWrap');
  if (wrap) wrap.style.display = 'none';
  _rosterPageLoad(org);
}

// ── Filter + search + render ──────────────────────────────────────────────────
function rosterSetFilter(filter, btn) {
  _rosterPageFilter = filter;
  document.querySelectorAll('.roster-filter-btn').forEach(b => b.classList.remove('active'));
  if (btn) btn.classList.add('active');
  _rosterPageApply();
}

function rosterSearch(q) {
  _rosterPageSearch = (q || '').toLowerCase().trim();
  _rosterPageApply();
}

function _rosterPageApply() {
  let rows = _rosterPageAll;

  // Filter
  if (_rosterPageFilter === 'active') {
    rows = rows.filter(p => (p.nppes_snapshot?.nppes_status || '').toUpperCase() === 'A');
  } else if (_rosterPageFilter === 'deactivated') {
    rows = rows.filter(p => (p.nppes_snapshot?.nppes_status || '').toUpperCase() === 'D');
  } else if (_rosterPageFilter === 'open-tasks') {
    rows = rows.filter(p => Array.isArray(p.open_tasks) && p.open_tasks.length > 0);
  } else if (_rosterPageFilter === 'clean') {
    rows = rows.filter(_providerIsClean);
  } else if (_rosterPageFilter === 'pml-gap') {
    rows = rows.filter(_pmlHasGap);
  }

  // Search
  if (_rosterPageSearch) {
    const q = _rosterPageSearch;
    rows = rows.filter(p =>
      (p.provider_name || '').toLowerCase().includes(q) ||
      (p.npi_validated || '').includes(q) ||
      (p.npi_roster || '').includes(q) ||
      (p.specialty || '').toLowerCase().includes(q)
    );
  }

  _updateStats(_rosterPageAll);
  _rosterPageRender(rows);
}

function _rosterPageRender(providers) {
  const list  = document.getElementById('rosterLiveList');
  const label = document.getElementById('rosterCountLabel');
  if (!list) return;

  if (label) {
    label.textContent = providers.length === _rosterPageAll.length
      ? `${providers.length} provider${providers.length !== 1 ? 's' : ''}`
      : `${providers.length} of ${_rosterPageAll.length}`;
  }

  if (!providers.length) {
    list.innerHTML = `<div style="padding:1.5rem;text-align:center;font-size:.82rem;color:var(--text-3)">No providers match this filter.</div>`;
    return;
  }

  // Re-use the existing card renderer from pipeline-nppes.js
  // We temporarily override window._rosterTruth so _renderRosterTruthRows picks up the filtered set
  const saved = window._rosterTruth;
  window._rosterTruth = providers;
  _renderRosterTruthRows(providers);
  window._rosterTruth = saved;
}

function _providerIsClean(p) {
  // "clean" = NPPES active, no open tasks, billability is literally "billable"
  const snap   = p.nppes_snapshot || {};
  const nSt    = (snap.nppes_status || '').toUpperCase();
  const billSt = (p.billability_status || '').toLowerCase();
  if (nSt === 'D') return false;
  if (billSt === 'inactive' || billSt === 'blocked' || billSt === 'risk' || billSt === 'at_risk' || billSt === 'warning') return false;
  return !Array.isArray(p.open_tasks) || p.open_tasks.length === 0;
}

function _pmlHasGap(p) {
  // pml_gap flag on snapshot is the authoritative field, but also check open_tasks for pml dim
  if (p.nppes_snapshot?.pml_gap === true) return true;
  // Secondary: any open task with dim=pml and severity!=info means a real gap
  if (Array.isArray(p.open_tasks)) {
    return p.open_tasks.some(t => t.dim === 'pml' && (t.severity || '') !== 'info');
  }
  return false;
}

function _updateStats(all) {
  const total     = all.length;
  const active    = all.filter(p => (p.nppes_snapshot?.nppes_status || '').toUpperCase() === 'A').length;
  const deact     = all.filter(p => {
    const nSt    = (p.nppes_snapshot?.nppes_status || '').toUpperCase();
    const billSt = (p.billability_status || '').toLowerCase();
    return nSt === 'D' || billSt === 'inactive';
  }).length;
  const withTasks = all.filter(p => Array.isArray(p.open_tasks) && p.open_tasks.length > 0).length;
  const taskTotal = all.reduce((n, p) => n + (Array.isArray(p.open_tasks) ? p.open_tasks.length : 0), 0);
  // PML gaps: count providers with any real PML issue (not just the snapshot flag)
  const pmlGaps   = all.filter(_pmlHasGap).length;
  // Clean: active NPPES + no issues + no open tasks
  const cleanCount = all.filter(_providerIsClean).length;

  const set = (id, val) => { const el = document.getElementById(id); if (el) el.textContent = val; };
  set('statTotal',     total);
  set('statActive',    active);
  set('statPmlGaps',   pmlGaps);
  set('statOpenTasks', taskTotal > 0 ? `${taskTotal} (${withTasks} providers)` : 0);

  // Filter counts
  set('fc-all',         total);
  set('fc-active',      active);
  set('fc-deactivated', deact);
  set('fc-open-tasks',  withTasks);
  set('fc-clean',       cleanCount);
  set('fc-pml-gap',     pmlGaps);
}

// ── State helpers ─────────────────────────────────────────────────────────────
function _showState(state) {
  const loading = document.getElementById('rosterLoadingState');
  const noOrg   = document.getElementById('rosterNoOrg');
  const empty   = document.getElementById('rosterEmptyState');
  const list    = document.getElementById('rosterLiveList');

  if (loading) loading.style.display = state === 'loading' ? '' : 'none';
  if (noOrg)   noOrg.style.display   = state === 'no-org'  ? '' : 'none';
  if (empty)   empty.style.display   = state === 'empty'   ? '' : 'none';
  if (list)    list.style.display    = state === 'list'    ? '' : 'none';
}

// ── Export CSV ────────────────────────────────────────────────────────────────
function rosterExportCsv() {
  const rows = _rosterPageAll;
  if (!rows.length) { feEmit('No roster data to export', 'warn'); return; }

  const headers = ['Provider Name','NPI','Specialty','City','State','Decision','Promoted At','Open Tasks'];
  const lines = [headers.join(',')];
  rows.forEach(p => {
    const tasks = Array.isArray(p.open_tasks) ? p.open_tasks.length : 0;
    const promoted = p.promoted_at ? new Date(p.promoted_at).toLocaleDateString() : '';
    lines.push([
      _csvCell(p.provider_name), _csvCell(p.npi_validated || p.npi_roster),
      _csvCell(p.specialty), _csvCell(p.city), _csvCell(p.state_cd),
      _csvCell(p.decision), _csvCell(promoted), tasks,
    ].join(','));
  });

  const blob = new Blob([lines.join('\n')], { type: 'text/csv' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = `roster-${_rosterPageOrg.replace(/\s+/g,'-')}-${new Date().toISOString().slice(0,10)}.csv`;
  a.click();
  feEmit(`Exported ${rows.length} providers to CSV`, 'ok');
}

function _csvCell(v) {
  const s = String(v || '');
  return s.includes(',') || s.includes('"') || s.includes('\n')
    ? `"${s.replace(/"/g,'""')}"` : s;
}

// ── Run Credentialing ─────────────────────────────────────────────────────────
function rosterRunCredentialing() {
  const base = window.location.origin;
  const org  = _rosterPageOrg;
  // Open pipeline; if we have a current org, pass it as a hint via lastOrg
  if (org) localStorage.setItem('lastOrg', org);
  window.open(`${base}/pipeline`, '_blank', 'noopener');
}

// ── Add Provider modal ────────────────────────────────────────────────────────
function rosterAddProvider() {
  const overlay = document.getElementById('addProviderOverlay');
  if (!overlay) return;
  // Clear form
  ['apNpi','apName','apCity','apState','apSpecialty'].forEach(id => {
    const el = document.getElementById(id); if (el) el.value = '';
  });
  const hint = document.getElementById('apNpiHint'); if (hint) hint.textContent = '';
  const err  = document.getElementById('apError');   if (err)  { err.style.display = 'none'; err.textContent = ''; }
  overlay.style.display = 'flex';
  setTimeout(() => document.getElementById('apNpi')?.focus(), 60);
}

function closeAddProvider() {
  const overlay = document.getElementById('addProviderOverlay');
  if (overlay) overlay.style.display = 'none';
}

let _apLookupTimer = null;
function apLookupNpi(val) {
  clearTimeout(_apLookupTimer);
  const hint = document.getElementById('apNpiHint');
  if (!hint) return;
  const npi = (val || '').replace(/\D/g, '');
  if (npi.length !== 10) { hint.textContent = npi.length > 0 ? `${npi.length}/10 digits` : ''; hint.style.color = 'var(--text-3)'; return; }
  hint.textContent = 'Looking up NPI…'; hint.style.color = 'var(--text-3)';
  _apLookupTimer = setTimeout(async () => {
    try {
      const r = await fetch(`https://npiregistry.cms.hhs.gov/api/?version=2.1&number=${npi}&limit=1`);
      const d = await r.json();
      const res = (d.results || [])[0];
      if (!res) { hint.textContent = 'NPI not found in NPPES'; hint.style.color = 'var(--red)'; return; }
      const basic = res.basic || {};
      const name = basic.authorized_official_last_name
        ? `${basic.authorized_official_first_name || ''} ${basic.authorized_official_last_name || ''}`.trim()
        : `${basic.first_name || ''} ${basic.last_name || ''}`.trim();
      const loc = (res.addresses || []).find(a => a.address_purpose === 'LOCATION') || (res.addresses||[])[0] || {};
      const nameEl = document.getElementById('apName');
      const cityEl = document.getElementById('apCity');
      const stateEl = document.getElementById('apState');
      const specEl  = document.getElementById('apSpecialty');
      if (nameEl && !nameEl.value) nameEl.value = name;
      if (cityEl && !cityEl.value) cityEl.value = loc.city || '';
      if (stateEl && !stateEl.value) stateEl.value = loc.state || '';
      if (specEl && !specEl.value) {
        const tax = (res.taxonomies || []).find(t => t.primary) || (res.taxonomies||[])[0];
        if (tax) specEl.value = tax.desc || tax.code || '';
      }
      hint.textContent = `✓ Found: ${name}`; hint.style.color = 'var(--green)';
    } catch { hint.textContent = 'Could not verify NPI (no internet?)'; hint.style.color = 'var(--text-3)'; }
  }, 600);
}

async function submitAddProvider() {
  const npi   = (document.getElementById('apNpi')?.value || '').replace(/\D/g,'');
  const name  = (document.getElementById('apName')?.value || '').trim();
  const city  = (document.getElementById('apCity')?.value || '').trim();
  const state = (document.getElementById('apState')?.value || '').trim().toUpperCase();
  const spec  = (document.getElementById('apSpecialty')?.value || '').trim();
  const err   = document.getElementById('apError');
  const btn   = document.getElementById('apSubmitBtn');

  if (!npi || npi.length !== 10) { _apShowErr('NPI must be 10 digits'); return; }
  if (!name) { _apShowErr('Provider name is required'); return; }

  if (btn) { btn.disabled = true; btn.textContent = 'Adding…'; }
  try {
    const resp = await fetch(`/chat/roster-truth/${encodeURIComponent(_rosterPageOrg)}/provider`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ npi, provider_name: name, city, state_cd: state, specialty: spec }),
    });
    if (!resp.ok) {
      const d = await resp.json().catch(() => ({}));
      throw new Error(d.detail || `HTTP ${resp.status}`);
    }
    closeAddProvider();
    feEmit(`Added ${name} (${npi}) to roster`, 'ok');
    // Reload list
    await _rosterPageLoad(_rosterPageOrg);
  } catch (e) {
    _apShowErr(e.message || 'Failed to add provider');
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = 'Add to Roster'; }
  }
}

function _apShowErr(msg) {
  const err = document.getElementById('apError');
  if (err) { err.textContent = msg; err.style.display = ''; }
}

// ── Ensure the drawer functions work on this page ─────────────────────────────
// openRosterProviderDrawer and closeRosterProviderDrawer are defined in pipeline-nppes.js.
// _ensureRosterProviderDrawer is also there — but on this page the drawer is already in the HTML,
// so we override _ensureRosterProviderDrawer to be a no-op.
window._ensureRosterProviderDrawer = function() {};
