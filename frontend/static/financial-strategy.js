/* ── Financial Strategy — Mobius ─────────────────────────────────────────── */

const API = window.location.origin;
let _baseline = null;  // cached baseline data
let _industry = null;  // cached industry data
let _currentOrg = '';
let _tasks = [];       // accumulated strategy tasks
let _pendingTasks = {}; // task suggestions keyed by button ID
let _bookmarks = [];   // user annotations/bookmarks from selected text
let _documentId = '';   // permanent per-org document handle (persisted)
let _versionId = '';    // current version within document (persisted)
let _threadId = '';     // current chat thread ID (persisted)

// ── Helpers ──────────────────────────────────────────────────────────────────
function esc(s) { const d = document.createElement('div'); d.textContent = s || ''; return d.innerHTML; }
function fmt$(n) { return n == null ? '—' : typeof n === 'string' ? n : '$' + Number(n).toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2}); }
function fmtN(n) { return n == null ? '—' : Number(n).toLocaleString(); }
function fmtPct(n) { return n == null ? '—' : (n >= 0 ? '+' : '') + n.toFixed(1) + '%'; }
function signalClass(s) { return s === 'green' ? 'green' : s === 'yellow' ? 'yellow' : s === 'red' ? 'red' : 'gray'; }
function verdictColor(label) {
  const g = ['Favorable', 'Opportunity', 'Manageable', 'Near Best-in-Class', 'Volume Engine'];
  const y = ['Mixed', 'Concentrated', 'At Risk'];
  if (g.includes(label)) return 'green';
  if (y.includes(label)) return 'yellow';
  return 'red';
}

// ── Markdown rendering (pipeline reports) ───────────────────────────────────
function renderMarkdown(md) {
  if (typeof marked !== 'undefined' && marked.parse) {
    return marked.parse(md);
  }
  // Minimal fallback if marked.js not loaded
  return md
    .replace(/^### (.+)$/gm, '<h3>$1</h3>')
    .replace(/^## (.+)$/gm, '<h2>$1</h2>')
    .replace(/^# (.+)$/gm, '<h1>$1</h1>')
    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
    .replace(/\n\n/g, '<br><br>')
    .replace(/\|(.+)\|/g, (m) => m) // tables left as-is in fallback
    ;
}

// ── Landing: load available orgs into dropdown ──────────────────────────────
async function loadOrgs() {
  const sel = document.getElementById('orgSelect');
  try {
    const cbh = document.getElementById('cbhToggle')?.checked ? '1' : '0';
    const r = await fetch(`${API}/chat/financial-strategy/orgs?include_community_bh=${cbh}`);
    const data = r.ok ? await r.json() : { orgs: [] };
    const orgs = data.orgs || [];
    sel.innerHTML = '<option value="">— Select an organization —</option>';
    for (const o of orgs) {
      const opt = document.createElement('option');
      opt.value = o.org_name;
      const city = o.org_city ? ` — ${o.org_city}` : '';
      opt.textContent = `${o.org_name}${city}`;
      opt.dataset.slug = o.org_slug || '';
      opt.dataset.type = o.org_type || '';
      opt.dataset.revenue = o.total_revenue || '';
      opt.dataset.claims = o.total_claims || '';
      opt.dataset.city = o.org_city || '';
      opt.dataset.panel = o.panel_size || '';
      opt.dataset.clinicians = o.servicing_npi_count || o.npi_count || '';
      opt.dataset.market = o.market_tier || '';
      opt.dataset.size = o.size_band || '';
      sel.appendChild(opt);
    }
  } catch (e) {
    sel.innerHTML = `<option value="">Failed to load organizations</option>`;
  }
}

function onOrgSelect() {
  const sel = document.getElementById('orgSelect');
  const btn = document.getElementById('startBtn');
  const preview = document.getElementById('orgPreview');
  const opt = sel.selectedOptions[0];

  if (!sel.value) {
    btn.disabled = true;
    preview.style.display = 'none';
    return;
  }

  btn.disabled = false;
  const orgType = opt.dataset.type || 'Unknown';
  const rev = opt.dataset.revenue ? fmt$(Number(opt.dataset.revenue)) : '—';
  const claims = opt.dataset.claims ? fmtN(Number(opt.dataset.claims)) : '—';
  const city = opt.dataset.city || '';
  const panel = opt.dataset.panel ? fmtN(Number(opt.dataset.panel)) : '';
  const clinicians = opt.dataset.clinicians ? Number(opt.dataset.clinicians) : '';
  const market = opt.dataset.market || '';
  const size = opt.dataset.size || '';

  let info = `<strong>${esc(sel.value)}</strong>`;
  if (city) info += ` · ${esc(city)}, FL`;
  info += `<br>${esc(orgType)}`;
  if (size) info += ` · ${esc(size)}`;
  if (market) info += ` · ${esc(market)} market`;
  info += `<br>Revenue: ${rev} · Claims: ${claims}`;
  if (panel) info += ` · Panel: ${panel}`;
  if (clinicians) info += ` · Clinicians: ${clinicians}`;
  preview.innerHTML = info;
  preview.style.display = 'block';
}

// ── Report Progress Card Renderer ────────────────────────────────────────────

const _PHASES = {
  1: { label: 'Data Assembly', steps: ['load_org', 'fetch_benchmarks', 'compute_verdicts', 'rank_priorities'] },
  2: { label: 'Draft Report', steps: ['build_context', 'draft_report'] },
  3: { label: 'Quality Assurance', steps: ['validate', 'critique', 'compose'] },
};

const _STEP_DEFAULTS = {
  load_org:          { title: 'Load org profile',                      edu: 'Pulling billing NPIs, revenue, and service lines from BigQuery' },
  fetch_benchmarks:  { title: 'Fetch code-level claims & benchmarks',  edu: 'Comparing your rates to P25/P50/P75 across 5 peer dimensions' },
  compute_verdicts:  { title: 'Compute rate, engagement & burnout verdicts', edu: 'Flagging codes where your rates fall below CMHC P25' },
  rank_priorities:   { title: 'Rank service line priorities',          edu: 'Sorting by revenue impact — biggest opportunities first' },
  build_context:     { title: 'Build analysis context',                edu: 'Structuring your data into the report framework' },
  draft_report:      { title: 'Draft executive summary & rate analysis', edu: 'Writing your personalized report covering rates, engagement, capacity, and risks' },
  validate:          { title: 'Validate numbers & signals',            edu: 'Cross-checking every rate against published AHCA fee schedule' },
  critique:          { title: 'Score narrative quality',               edu: 'Evaluating clarity, specificity, and actionability across 8 dimensions' },
  compose:           { title: 'Compose final report',                  edu: 'Merging draft + validation + critique into your polished deliverable' },
};

let _rpSteps = {};   // step_id -> { phase, status, title, edu, duration }
let _rpStartTime = 0;
let _rpTimerInterval = null;

function _rpRender(orgName, completed = false) {
  const panel = document.getElementById('rpPanel');
  if (!panel) return;

  let html = `<div class="rp-header">
    <span class="rp-header-icon">📊</span>
    <span class="rp-header-title">${completed ? 'Financial Strategy Report' : 'Generating Financial Strategy Report'}</span>
    <span class="rp-header-sub">${esc(orgName)}</span>
  </div>`;

  for (const [phaseNum, phaseDef] of Object.entries(_PHASES)) {
    const pn = parseInt(phaseNum);
    const phaseSteps = phaseDef.steps.map(sid => _rpSteps[sid] || { step_id: sid, phase: pn, status: 'pending', ...(_STEP_DEFAULTS[sid] || {}) });
    const allDone = phaseSteps.every(s => s.status === 'done');
    const anyRunning = phaseSteps.some(s => s.status === 'running');
    const allPending = phaseSteps.every(s => s.status === 'pending');

    // Collapse completed phases when a later phase is active
    const laterPhaseActive = Object.entries(_rpSteps).some(([, s]) => s.phase > pn && (s.status === 'running' || s.status === 'done'));
    const collapsed = allDone && laterPhaseActive;

    html += `<div class="rp-phase${collapsed ? ' collapsed' : ''}">`;
    html += `<div class="rp-phase-label">Phase ${phaseNum} — ${phaseDef.label}${allDone ? ' ✓' : ''}</div>`;

    // Phase progress bar
    html += `<div class="rp-phase-bar">`;
    for (const s of phaseSteps) {
      const cls = s.status === 'done' ? 'done' : s.status === 'running' ? 'running' : '';
      html += `<div class="rp-seg ${cls}"></div>`;
    }
    html += `</div>`;

    // Step cards (hidden when collapsed)
    if (!collapsed) {
      let pendingIdx = 0;
      for (const s of phaseSteps) {
        let icon = '';
        if (s.status === 'done') icon = '✓';
        else if (s.status === 'running') icon = '⚡';
        else { pendingIdx++; icon = String(pendingIdx); }

        const dur = s.duration ? (s.status === 'done' ? `${s.duration}s` : `${s.duration}s…`) : '';
        html += `<div class="rp-step ${s.status}">
          <div class="rp-icon">${icon}</div>
          <div class="rp-text">
            <div class="rp-title">${esc(s.title || '')}</div>
            <div class="rp-edu">${esc(s.edu || '')}</div>
          </div>
          ${dur ? `<div class="rp-dur">${dur}</div>` : ''}
        </div>`;
      }
    }
    html += `</div>`;
  }

  if (completed) {
    const totalSteps = Object.values(_rpSteps).filter(s => s.status === 'done').length;
    const elapsed = _rpStartTime ? _fmtElapsed(Date.now() / 1000 - _rpStartTime) : '—';
    html += `<div class="rp-complete-banner">
      <span>✓</span> Report ready — ${totalSteps} steps completed in ${elapsed}
      <button class="rp-cta" onclick="_rpShowReport()">View Report</button>
    </div>`;
  } else {
    // Footer with elapsed + progress bar
    const elapsed = _rpStartTime ? _fmtElapsed(Date.now() / 1000 - _rpStartTime) : '0:00';
    const totalSteps = Object.keys(_STEP_DEFAULTS).length;
    const doneSteps = Object.values(_rpSteps).filter(s => s.status === 'done').length;
    const pct = Math.round((doneSteps / totalSteps) * 100);
    html += `<div class="rp-footer">
      <span>Elapsed:</span> <span class="rp-footer-elapsed">${elapsed}</span>
      <div class="rp-footer-bar"><div class="rp-footer-fill indigo" style="width:${pct}%"></div></div>
    </div>`;
  }

  panel.innerHTML = html;
}

function _fmtElapsed(secs) {
  const m = Math.floor(secs / 60);
  const s = Math.floor(secs % 60);
  return `${m}:${String(s).padStart(2, '0')}`;
}

function _rpShowReport() {
  // Transition from progress view to analysis view
  document.getElementById('reportProgress').hidden = true;
  document.getElementById('analysisView').hidden = false;
  document.getElementById('hdOrg').textContent = _currentOrg;
  document.getElementById('fsTaskSection').style.display = 'block';
  renderAll(_industry, _baseline);
  renderTaskList();
}

// Show the analysis view early with the static industry report loaded immediately,
// org-specific Chapter 2 shows a loading placeholder until the pipeline finishes.
function _showIndustryReportEarly(orgName) {
  document.getElementById('reportProgress').hidden = true;
  document.getElementById('analysisView').hidden = false;
  document.getElementById('hdOrg').textContent = orgName;
  const el = document.getElementById('fsContent');
  el.hidden = false;
  document.getElementById('fsLoading').hidden = true;
  el.innerHTML = `
    <div id="industryReportContainer" class="fs-industry-report-embed"></div>
    <div class="fs-chapter-divider"><span>Chapter 2 — ${esc(orgName)}</span></div>
    <div id="ch2Loading" style="text-align:center;padding:3rem 1rem;color:var(--text-3)">
      <span class="spinner-sm"></span>
      <div style="margin-top:.75rem;font-size:.875rem">Generating org-specific analysis…</div>
    </div>
  `;
  _loadIndustryReport();

  // Launch onboarding tour after a short delay (once report is visible)
  setTimeout(() => startTour(), 1200);
}

// ── Start analysis ───────────────────────────────────────────────────────────
async function startAnalysis() {
  const sel = document.getElementById('orgSelect');
  const orgName = sel.value;
  const errEl = document.getElementById('startError');
  if (!orgName) { errEl.textContent = 'Select an organization'; errEl.style.display = 'block'; return; }
  errEl.style.display = 'none';
  const orgSlug = sel.selectedOptions[0]?.dataset?.slug || '';

  const btn = document.getElementById('startBtn');
  btn.disabled = true; btn.textContent = 'Generating...';

  // Reset progress state
  _rpSteps = {};
  _rpStartTime = Date.now() / 1000;

  // Show industry report immediately (static, no generation needed)
  // Then run org-specific pipeline in background for Chapter 2
  document.getElementById('startScreen').hidden = true;
  _showIndustryReportEarly(orgName);

  try {
    // Start industry fetch in parallel (for data used by legacy renderers)
    const indPromise = fetch(`${API}/chat/financial-strategy/industry`).then(r => r.ok ? r.json() : null);

    // Start streaming baseline generation for org-specific Chapter 2
    const baseR = await fetch(`${API}/chat/financial-strategy/generate-baseline`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ org_name: orgName, org_slug: orgSlug, stream: true }),
    });
    if (!baseR.ok) {
      const err = await baseR.json().catch(() => ({ detail: baseR.statusText }));
      throw new Error(err.detail || 'Failed to start report generation');
    }

    const baseJson = await baseR.json();
    const { correlation_id: cid, stream_url: streamUrl } = baseJson;
    // Capture document_id, version_id, thread_id for persistence
    if (baseJson.document_id) _documentId = baseJson.document_id;
    if (baseJson.version_id) _versionId = baseJson.version_id;
    if (baseJson.thread_id) _threadId = baseJson.thread_id;

    let result = null;

    // SSE stream reader for progress updates (shown in ch2Loading area)
    const sseUrl = `${API}${streamUrl}`;
    let sseAbort = new AbortController();
    (async () => {
      while (!result) {
        try {
          const evtR = await fetch(sseUrl, { signal: sseAbort.signal });
          if (!evtR.ok) { await _sleep(500); continue; }
          const reader = evtR.body.getReader();
          const decoder = new TextDecoder();
          let buffer = '';
          while (true) {
            const { done, value } = await reader.read();
            if (done) break;
            buffer += decoder.decode(value, { stream: true });
            const lines = buffer.split('\n');
            buffer = lines.pop() || '';
            for (const line of lines) {
              if (line.startsWith('data: ')) {
                try {
                  const evt = JSON.parse(line.slice(6));
                  if (evt.event === 'step') {
                    // Update the ch2Loading area with step progress
                    const loading = document.getElementById('ch2Loading');
                    if (loading) {
                      const stepName = evt.data.label || evt.data.step_id;
                      const status = evt.data.status === 'done' ? ' ✓' : '…';
                      loading.innerHTML = `<span class="spinner-sm"></span><div style="margin-top:.75rem;font-size:.875rem">${esc(stepName)}${status}</div>`;
                    }
                  }
                } catch (_) {}
              }
            }
          }
        } catch (_) {}
        if (!result) await _sleep(500);
      }
    })();

    // Poll for completion
    while (!result) {
      await _sleep(2000);
      try {
        const pollR = await fetch(`${API}/chat/financial-strategy/response/${cid}`);
        if (pollR.ok) {
          const pollData = await pollR.json();
          if (pollData.status === 'completed') {
            result = pollData;
          }
        }
      } catch (_) { /* retry */ }
    }

    sseAbort.abort();

    _industry = await indPromise;
    _baseline = result;
    _currentOrg = _baseline.org_name || orgName;
    if (result.document_id) _documentId = result.document_id;
    if (result.version_id) _versionId = result.version_id;
    if (result.thread_id) _threadId = result.thread_id;
    _tasks = [];

    // Update URL with document_id for session resume / sharing
    if (_documentId) {
      const url = new URL(window.location);
      url.searchParams.set('doc', _documentId);
      window.history.replaceState({}, '', url);
    }

    // Re-render industry chapter now that _industry is loaded (replaces HTML fragment fallback)
    _loadIndustryReport();
    // Replace the ch2Loading placeholder with the actual Chapter 2 content
    _renderChapter2(_baseline);
    document.getElementById('fsTaskSection').style.display = 'block';
    renderTaskList();

  } catch (e) {
    console.error('startAnalysis error:', e);
    // Show error in the Chapter 2 area, but keep the industry report visible
    const loading = document.getElementById('ch2Loading');
    if (loading) {
      loading.innerHTML = `<div style="color:var(--text-2);font-size:.875rem">⚠ Failed to generate org analysis: ${esc(e.message || String(e))}</div>`;
    }
  } finally {
    btn.disabled = false; btn.textContent = 'Generate Strategy Report →';
  }
}

function _sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

// ── Refresh baseline (force rebuild, same version) ──────────────────────────
async function refreshBaseline() {
  if (!_currentOrg || !_versionId || !_documentId) return;

  const btn = document.getElementById('refreshBtn');
  btn.disabled = true;
  btn.innerHTML = `<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" class="spin-icon"><polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 11-2.12-9.36L23 10"/></svg> Refreshing...`;

  // Replace Chapter 2 content with progress steps
  const ch2 = document.getElementById('ch2Loading');
  const ch2Content = document.getElementById('ch2Content');
  if (ch2Content) {
    ch2Content.id = 'ch2Loading';
    ch2Content.innerHTML = `<div class="rp-panel" id="rpRefreshPanel"></div>`;
  } else if (ch2) {
    ch2.innerHTML = `<div class="rp-panel" id="rpRefreshPanel"></div>`;
  }

  // Reset progress step state
  _rpSteps = {};
  _rpStartTime = Date.now() / 1000;

  // Scroll to the progress panel
  const rpPanel = document.getElementById('rpRefreshPanel');
  if (rpPanel) rpPanel.scrollIntoView({ behavior: 'smooth', block: 'start' });

  try {
    const orgSlug = _baseline?.org_slug || '';
    const r = await fetch(`${API}/chat/financial-strategy/refresh-baseline`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        org_name: _currentOrg,
        org_slug: orgSlug,
        version_id: _versionId,
        document_id: _documentId,
        thread_id: _threadId,
      }),
    });
    if (!r.ok) throw new Error('Failed to start refresh');
    const { correlation_id: cid, stream_url: streamUrl } = await r.json();

    let result = null;

    // SSE stream reader for live progress steps
    const sseUrl = `${API}${streamUrl}`;
    const sseAbort = new AbortController();
    (async () => {
      while (!result) {
        try {
          const evtR = await fetch(sseUrl, { signal: sseAbort.signal });
          if (!evtR.ok) { await _sleep(500); continue; }
          const reader = evtR.body.getReader();
          const decoder = new TextDecoder();
          let buffer = '';
          while (true) {
            const { done, value } = await reader.read();
            if (done) break;
            buffer += decoder.decode(value, { stream: true });
            const lines = buffer.split('\n');
            buffer = lines.pop() || '';
            for (const line of lines) {
              if (line.startsWith('data: ')) {
                try {
                  const evt = JSON.parse(line.slice(6));
                  if (evt.event === 'step') {
                    const s = evt.data;
                    _rpSteps[s.step_id] = {
                      phase: s.phase, status: s.status,
                      title: s.label || (_STEP_DEFAULTS[s.step_id]?.title || s.step_id),
                      edu: s.edu || (_STEP_DEFAULTS[s.step_id]?.edu || ''),
                      duration: s.duration ? s.duration.toFixed(1) : '',
                    };
                    _rpRenderInline(_currentOrg);
                  }
                } catch (_) {}
              }
            }
          }
        } catch (_) {}
        if (!result) await _sleep(500);
      }
    })();

    // Poll for completion
    while (!result) {
      await _sleep(2000);
      try {
        const pollR = await fetch(`${API}/chat/financial-strategy/response/${cid}`);
        if (pollR.ok) {
          const pollData = await pollR.json();
          if (pollData.status === 'completed') result = pollData;
        }
      } catch (_) {}
    }
    sseAbort.abort();

    // Update state with refreshed baseline
    _baseline = result;
    _currentOrg = _baseline.org_name || _currentOrg;

    // Re-render Chapter 2 with fresh data
    // Restore the ch2Loading element for _renderChapter2
    const panel = document.getElementById('rpRefreshPanel')?.parentElement;
    if (panel) { panel.id = 'ch2Loading'; }
    _renderChapter2(_baseline);

  } catch (e) {
    console.error('refreshBaseline error:', e);
    const panel = document.getElementById('rpRefreshPanel');
    if (panel) {
      panel.innerHTML = `<div style="color:var(--red);padding:1rem">Refresh failed: ${esc(e.message || String(e))}</div>`;
    }
  } finally {
    btn.disabled = false;
    btn.innerHTML = `<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 11-2.12-9.36L23 10"/></svg> Refresh`;
  }
}

// Render progress steps inline (within Chapter 2 area during refresh)
function _rpRenderInline(orgName) {
  const panel = document.getElementById('rpRefreshPanel');
  if (!panel) return;

  let html = `<div class="rp-header">
    <span class="rp-header-icon">🔄</span>
    <span class="rp-header-title">Rebuilding Report</span>
    <span class="rp-header-sub">${esc(orgName)}</span>
  </div>`;

  for (const [phaseNum, phaseDef] of Object.entries(_PHASES)) {
    const pn = parseInt(phaseNum);
    const phaseSteps = phaseDef.steps.map(sid => _rpSteps[sid] || { step_id: sid, phase: pn, status: 'pending', ...(_STEP_DEFAULTS[sid] || {}) });
    const allDone = phaseSteps.every(s => s.status === 'done');
    const laterPhaseActive = Object.entries(_rpSteps).some(([, s]) => s.phase > pn && (s.status === 'running' || s.status === 'done'));
    const collapsed = allDone && laterPhaseActive;

    html += `<div class="rp-phase${collapsed ? ' collapsed' : ''}">`;
    html += `<div class="rp-phase-label">Phase ${phaseNum} — ${phaseDef.label}${allDone ? ' ✓' : ''}</div>`;
    html += `<div class="rp-phase-bar">`;
    for (const s of phaseSteps) {
      const cls = s.status === 'done' ? 'done' : s.status === 'running' ? 'running' : '';
      html += `<div class="rp-seg ${cls}"></div>`;
    }
    html += `</div>`;

    if (!collapsed) {
      for (const s of phaseSteps) {
        let icon = s.status === 'done' ? '✓' : s.status === 'running' ? '⚡' : '·';
        const dur = s.duration ? `${s.duration}s` : '';
        html += `<div class="rp-step ${s.status}">
          <div class="rp-icon">${icon}</div>
          <div class="rp-text">
            <div class="rp-title">${esc(s.title || '')}</div>
            <div class="rp-edu">${esc(s.edu || '')}</div>
          </div>
          ${dur ? `<div class="rp-dur">${dur}</div>` : ''}
        </div>`;
      }
    }
    html += `</div>`;
  }
  panel.innerHTML = html;
}

// Render org-specific Chapter 2 content into the existing page (replacing the loading placeholder)
function _renderChapter2(b) {
  // Accept either ch2Loading (initial) or ch2Content (refresh/version switch)
  let loading = document.getElementById('ch2Loading') || document.getElementById('ch2Content');
  if (!loading) return;

  let ch2Html = '';
  if (b.pipeline && b.report_md) {
    ch2Html = `
      <div class="fs-pipeline-report">${renderMarkdown(b.report_md)}</div>
      <div style="text-align:center;color:#94a3b8;font-size:.75rem;margin:1.5rem 0">
        Report authored by Claude · Draft → Validate + Critique → Compose pipeline · Data from FL Medicaid BQ
      </div>
    `;
  } else {
    const codes = b.code_analysis || [];
    const v = b.verdicts || {};
    const bInd = b.industry || {};
    const pri = b.priorities || [];
    const prose = b.prose || {};
    const llm = b.llm_authored;

    ch2Html = `
      ${prose.executive_summary ? renderExecutiveSummary(prose, b) : ''}
      ${renderOverview(b)}
      ${renderRates(codes, bInd, prose)}
      ${renderEngagement(codes, b, prose)}
      ${renderBurnout(codes, b, prose)}
      ${prose.key_risks || prose.key_strengths ? renderRisksStrengths(prose) : ''}
      ${renderVerdicts(v)}
      ${renderPriorities(pri)}
      ${prose.strategic_outlook ? renderStrategicOutlook(prose) : ''}
      ${llm ? '<div style="text-align:center;color:#94a3b8;font-size:.75rem;margin:1rem 0">Report narrative authored by Claude · Data from FL Medicaid BQ</div>' : ''}
    `;
  }

  loading.outerHTML = `<div id="ch2Content">${ch2Html}</div>`;
}

function backToStart() {
  document.getElementById('analysisView').hidden = true;
  document.getElementById('startScreen').hidden = false;
  _baseline = null;
  _tasks = [];
}

// ── PDF Download ─────────────────────────────────────────────────────────────
function toggleDownloadMenu() {
  const menu = document.getElementById('downloadMenu');
  menu.hidden = !menu.hidden;
  if (!menu.hidden) {
    // Close on outside click
    const close = (e) => {
      if (!e.target.closest('.fs-download-wrap')) { menu.hidden = true; document.removeEventListener('click', close); }
    };
    setTimeout(() => document.addEventListener('click', close), 0);
  }
}

function downloadPDF(scope) {
  document.getElementById('downloadMenu').hidden = true;

  const content = document.getElementById('fsContent');
  const sections = content.querySelectorAll('.fs-section');
  const dividers = content.querySelectorAll('.fs-chapter-divider');
  const pipelineReport = content.querySelector('.fs-pipeline-report');

  // Determine which sections are industry vs provider
  const industrySections = ['sec-data-calibration', 'sec-industry-landscape', 'sec-industry-published-rates', 'sec-industry-rates', 'sec-industry-fqhc', 'sec-industry-leakage', 'sec-industry-burnout', 'sec-industry-market-archetypes', 'sec-industry-trends'];

  // Mark sections to hide based on scope
  sections.forEach(sec => {
    const isIndustry = industrySections.includes(sec.id);
    if (scope === 'industry' && !isIndustry) sec.classList.add('pdf-hidden');
    if (scope === 'provider' && isIndustry) sec.classList.add('pdf-hidden');
  });

  dividers.forEach(d => {
    if (scope === 'industry') d.classList.add('pdf-hidden');
  });

  if (pipelineReport && scope === 'industry') {
    pipelineReport.classList.add('pdf-hidden');
  }

  // Inject print header
  const orgName = _currentOrg || '—';
  const scopeLabel = scope === 'industry' ? 'Industry Report' : scope === 'provider' ? 'Provider Report' : 'Full Strategy Report';
  let printHeader = document.querySelector('.fs-print-header');
  if (!printHeader) {
    printHeader = document.createElement('div');
    printHeader.className = 'fs-print-header';
    content.insertBefore(printHeader, content.firstChild);
  }
  printHeader.innerHTML = `<h1>${esc(scopeLabel)}: ${esc(orgName)}</h1><p>Mobius Financial Strategy · Generated ${new Date().toLocaleDateString('en-US', { year: 'numeric', month: 'long', day: 'numeric' })}</p>`;

  // Print
  window.print();

  // Cleanup: remove pdf-hidden classes and print header
  content.querySelectorAll('.pdf-hidden').forEach(el => el.classList.remove('pdf-hidden'));
}

// ── Render all ───────────────────────────────────────────────────────────────
function renderAll(ind, b) {
  const el = document.getElementById('fsContent');
  el.hidden = false;
  document.getElementById('fsLoading').hidden = true;

  // If we have a full pipeline-generated markdown report, render it
  if (b.pipeline && b.report_md) {
    el.innerHTML = `
      <div id="industryReportContainer" class="fs-industry-report-embed"></div>
      <div class="fs-chapter-divider"><span>Chapter 2 — ${esc(b.org_name)}</span></div>
      <div class="fs-pipeline-report">${renderMarkdown(b.report_md)}</div>
      <div style="text-align:center;color:#94a3b8;font-size:.75rem;margin:1.5rem 0">
        Report authored by Claude · Draft → Validate + Critique → Compose pipeline · Data from FL Medicaid BQ
      </div>
    `;
    _loadIndustryReport();
    return;
  }

  // Fallback: deterministic rendering with optional prose overlays
  const codes = b.code_analysis || [];
  const v = b.verdicts || {};
  const bInd = b.industry || {};
  const pri = b.priorities || [];

  const prose = b.prose || {};
  const llm = b.llm_authored;

  el.innerHTML = `
    <div id="industryReportContainer" class="fs-industry-report-embed"></div>
    <div class="fs-chapter-divider"><span>Chapter 2 — ${esc(b.org_name)}</span></div>
    ${prose.executive_summary ? renderExecutiveSummary(prose, b) : ''}
    ${renderOverview(b)}
    ${renderRates(codes, bInd, prose)}
    ${renderEngagement(codes, b, prose)}
    ${renderBurnout(codes, b, prose)}
    ${prose.key_risks || prose.key_strengths ? renderRisksStrengths(prose) : ''}
    ${renderVerdicts(v)}
    ${renderPriorities(pri)}
    ${prose.strategic_outlook ? renderStrategicOutlook(prose) : ''}
    ${llm ? '<div style="text-align:center;color:#94a3b8;font-size:.75rem;margin:1rem 0">Report narrative authored by Claude · Data from FL Medicaid BQ</div>' : ''}
  `;
  _loadIndustryReport();
}

// ── Chapter 1: Load standalone industry report ──────────────────────────────
async function _loadIndustryReport() {
  const container = document.getElementById('industryReportContainer');
  if (!container) return;
  // Render industry chapter from structured data (primary path)
  if (_industry) {
    container.innerHTML = renderDataCalibration(_industry) + renderIndustryLandscape(_industry) + renderIndustryPublishedRates(_industry) + renderIndustryRates(_industry) + renderIndustryFqhc(_industry) + renderIndustryLeakage(_industry) + renderIndustryBurnout(_industry) + renderIndustryMarketArchetypes(_industry) + renderIndustryTrends(_industry);
  } else {
    // Fallback: try loading from pre-rendered HTML fragment
    container.innerHTML = '<div style="text-align:center;padding:2rem;color:var(--text-3)"><span class="spinner-sm"></span> Loading industry report…</div>';
    try {
      const r = await fetch(`${API}/chat/financial-strategy/industry-report-html`);
      if (!r.ok) throw new Error('Failed to load industry report');
      const html = await r.text();
      container.innerHTML = html;
      container.querySelectorAll('.report-header, .dl-btn-wrap, .review-note, [id="sel-toolbar"]').forEach(el => el.remove());
      container.querySelectorAll('.report-wrap > div').forEach(el => {
        if (el.children.length === 0 && el.textContent.includes('Generated April 2026')) el.remove();
      });
      container.querySelectorAll('script').forEach(el => el.remove());
    } catch (e) {
      container.innerHTML = '<div style="padding:1rem;color:var(--text-3);font-size:.8125rem">Industry report unavailable.</div>';
    }
  }
}

// ── 5 Service Categories (constant) ──────────────────────────────────────────
const CATEGORY_ORDER = ['intake', 'high_acuity', 'ongoing_bh', 'eval_mgmt', 'med_mgmt'];
const CATEGORY_META = {
  intake:      { label: 'Intake / Assessment', short: 'Intake', color: 'blue' },
  high_acuity: { label: 'High-Acuity / Stabilization', short: 'High Acuity', color: 'yellow' },
  ongoing_bh:  { label: 'Ongoing BH Outpatient', short: 'Ongoing BH', color: 'green' },
  eval_mgmt:   { label: 'Evaluation & Management', short: 'E&M', color: 'purple' },
  med_mgmt:    { label: 'Medication Management', short: 'Med Mgmt', color: 'red' },
};

function _getCategoryMap(ind) {
  // Build code→category lookup from industry data (supports both 'categories' and 'segments' keys)
  const cats = ind.categories || ind.segments || {};
  const map = {};
  for (const [catKey, catDef] of Object.entries(cats)) {
    for (const code of (catDef.codes || [])) map[code] = catKey;
  }
  // If data didn't have full 5 categories, fill from constant
  if (Object.keys(map).length === 0) {
    for (const [catKey, meta] of Object.entries(CATEGORY_META)) {
      // Use hardcoded fallback codes
      const fallbackCodes = {
        intake: ['H0031','H0032','H2000','90791','90792'],
        high_acuity: ['H0036','H2017','H0040','H2010'],
        ongoing_bh: ['H2019','T1017','90832','90834','90837','H0004','H0048','T1007'],
        eval_mgmt: ['99202','99203','99204','99205','99212','99213','99214','99215'],
        med_mgmt: ['T1015','90863','90833','90836','90838','M0064'],
      };
      for (const code of (fallbackCodes[catKey] || [])) map[code] = catKey;
    }
  }
  return map;
}

function _catOf(code, catMap) {
  return catMap[code] || 'other';
}

function _groupByCategory(codes, catMap) {
  const groups = {};
  for (const cat of CATEGORY_ORDER) groups[cat] = [];
  groups['other'] = [];
  for (const item of codes) {
    const cat = _catOf(item.code || item, catMap);
    (groups[cat] || groups['other']).push(item);
  }
  return groups;
}

// ── Code description lookup ─────────────────────────────────────────────────
const CODE_LABELS = {
  'H0031': 'Intake Assessment', 'H0032': 'Plan Development', 'H2000': 'Comprehensive Eval',
  '90791': 'Psych Diagnostic Eval', '90792': 'Psychiatric Eval w/ Med',
  'H0036': 'Community Psych Support', 'H2017': 'Psychosocial Rehab (PSR)',
  'H0040': 'FACT/ACT Per Diem', 'H2010': 'Comp Med Services',
  'H2019': 'Individual/Family Therapy', 'T1017': 'Care Management',
  '90832': 'Psychotherapy 30 min', '90834': 'Psychotherapy 45 min', '90837': 'Psychotherapy 60 min',
  'H0004': 'BH Counseling', 'H0048': 'Alcohol/Drug Screening', 'T1007': 'Treatment Plan Review',
  '99202': 'E&M New Pt Lv2', '99203': 'E&M New Pt Lv3', '99204': 'E&M New Pt Lv4', '99205': 'E&M New Pt Lv5',
  '99212': 'E&M Estab Pt Lv2', '99213': 'E&M Estab Pt Lv3', '99214': 'E&M Estab Pt Lv4', '99215': 'E&M Estab Pt Lv5',
  'T1015': 'Medication Mgmt', '90863': 'Pharmacologic Mgmt', '90833': 'Psychotherapy Add-on 30 min',
  '90836': 'Psychotherapy Add-on 45 min', '90838': 'Psychotherapy Add-on 60 min', 'M0064': 'Brief Office Visit',
};
function _codeLabel(code) { return CODE_LABELS[code] || code; }

// ── Service Line Coverage helpers ────────────────────────────────────────────
function _renderCoverageTable(ind) {
  const cov = ind.service_line_coverage || {};
  const codes = cov.codes || {};
  const total = cov.total_cmhcs || 86;
  const catMap = _getCategoryMap(ind);
  const items = Object.entries(codes).map(([code, d]) => ({code, ...d}));
  const grouped = _groupByCategory(items, catMap);

  let tableRows = '';
  for (const cat of CATEGORY_ORDER) {
    const rows = grouped[cat] || [];
    if (!rows.length) continue;
    const meta = CATEGORY_META[cat];
    tableRows += `<tr class="fs-cat-header"><td colspan="5"><span class="fs-badge ${meta.color}">${esc(meta.short)}</span></td></tr>`;
    tableRows += rows.sort((a,b) => (b.org_count||0) - (a.org_count||0)).map(d => {
      const pct = d.pct || (d.org_count / total * 100);
      const cls = pct >= 50 ? 'green' : pct >= 20 ? 'yellow' : 'red';
      const barW = Math.min(100, pct);
      return `<tr>
        <td><strong>${esc(d.code)}</strong></td>
        <td style="font-size:.75rem;color:var(--text-3)">${esc(d.label || '')}</td>
        <td>${d.org_count} / ${total}</td>
        <td style="width:140px"><div style="background:var(--bg-2);border-radius:4px;height:14px;position:relative"><div style="width:${barW}%;height:100%;border-radius:4px;background:var(--${cls === 'green' ? 'green' : cls === 'yellow' ? 'amber' : 'red'})"></div></div></td>
        <td><span class="fs-badge ${cls}">${pct.toFixed(0)}%</span></td>
      </tr>`;
    }).join('');
  }

  return `<div class="fs-table-wrap">
    <table class="fs-table" style="font-size:.8rem">
      <thead><tr><th>Code</th><th>Service</th><th>CMHCs Billing</th><th style="width:140px">Coverage</th><th>%</th></tr></thead>
      <tbody>${tableRows}</tbody>
    </table>
  </div>`;
}

function _renderSupplyRetentionCards(ind) {
  const cov = ind.service_line_coverage || {};
  const byCat = cov.by_category || {};
  return `<div class="fs-card-grid" style="grid-template-columns:repeat(5,1fr)">
    ${CATEGORY_ORDER.map(cat => {
      const meta = CATEGORY_META[cat];
      const c = byCat[cat] || {};
      const avgCov = c.avg_coverage_pct || 0;
      const supplyGaps = (c.supply_gap_codes || []);
      const cls = avgCov >= 50 ? 'green' : avgCov >= 25 ? 'yellow' : 'red';
      const diagnosis = avgCov < 25 ? 'Supply Gap' : avgCov < 50 ? 'Mixed' : 'Retention Gap';
      return `<div class="fs-card ${meta.color}">
        <div class="fs-card-title">${esc(meta.short)}</div>
        <div class="fs-card-big">${avgCov.toFixed(0)}%</div>
        <div class="fs-card-sub">avg coverage</div>
        <div style="margin-top:.5rem"><span class="fs-badge ${cls}">${diagnosis}</span></div>
        ${supplyGaps.length ? `<div style="font-size:.7rem;color:var(--text-3);margin-top:.35rem">Gaps: ${supplyGaps.join(', ')}</div>` : ''}
        ${c.note ? `<div style="font-size:.7rem;color:var(--text-3);margin-top:.25rem">${esc(c.note)}</div>` : ''}
      </div>`;
    }).join('')}
  </div>`;
}

const COMP_COLORS = {intake:'#3b82f6',high_acuity:'#ef4444',ongoing_bh:'#22c55e',eval_mgmt:'#a855f7',med_mgmt:'#f59e0b',psr:'#16a34a',therapy:'#4ade80',care_mgmt:'#86efac'};

function _renderMarketComposition(ind) {
  const mc = ind.market_composition;
  if (!mc || !mc.categories) return '';
  const cats = mc.categories;
  const em = cats['eval_mgmt'] || {};
  // Exclude E&M from the BH market totals
  const bhTotal = (mc.total_fl_op_market || 0) - (em.fl_market_dollars || 0);
  const cmhcBhTotal = (mc.total_cmhc_revenue || 0) - (em.cmhc_dollars || 0);
  const cmhcBhPct = bhTotal > 0 ? (cmhcBhTotal / bhTotal * 100).toFixed(1) : '0';
  const catOrder = ['ongoing_bh','high_acuity','med_mgmt','intake'];

  // Recompute % of BH total for each category
  const bhPcts = {};
  for (const k of catOrder) {
    const c = cats[k];
    if (c) bhPcts[k] = (c.fl_market_dollars / bhTotal * 100).toFixed(1);
  }

  // Build flat row list — expand subcategories for ongoing_bh
  const SUB_COLORS = {psr:'#16a34a',therapy:'#4ade80',care_mgmt:'#86efac'};
  const flatRows = [];
  for (const k of catOrder) {
    const c = cats[k];
    if (!c) continue;
    const subs = c.subcategories;
    if (subs && Object.keys(subs).length) {
      flatRows.push({key: k, label: c.label, mkt: c.fl_market_dollars, cmhc: c.cmhc_dollars, cap: c.cmhc_capture_pct, benes: c.fl_beneficiaries || 0, insight: c.insight, color: COMP_COLORS[k], isParent: true});
      const subOrder = ['psr','therapy','care_mgmt'];
      for (const sk of subOrder) {
        const s = subs[sk];
        if (!s) continue;
        flatRows.push({key: sk, label: s.label, mkt: s.fl_market_dollars, cmhc: s.cmhc_dollars, cap: s.cmhc_capture_pct, benes: s.fl_beneficiaries || 0, insight: s.insight, color: SUB_COLORS[sk] || COMP_COLORS[k], isSub: true, codes: s.codes});
      }
    } else {
      flatRows.push({key: k, label: c.label, mkt: c.fl_market_dollars, cmhc: c.cmhc_dollars, cap: c.cmhc_capture_pct, benes: c.fl_beneficiaries || 0, insight: c.insight, color: COMP_COLORS[k]});
    }
  }

  // Stacked bar — use subcategory colors where available
  const barItems = flatRows.filter(r => !r.isParent);
  const barSegs = barItems.map(r => {
    const pct = (r.mkt / bhTotal * 100);
    if (pct < 0.5) return '';
    return `<div style="width:${pct.toFixed(1)}%;background:${r.color};height:100%;display:inline-block" title="${esc(r.label)}: ${pct.toFixed(1)}%"></div>`;
  }).join('');

  // Table rows — benes shown as /mo (annual ÷ 12)
  function fmtBenes(annual) {
    const mo = Math.round(annual / 12);
    return mo >= 1000 ? (mo / 1000).toFixed(1) + 'K' : mo.toLocaleString();
  }
  const rows = flatRows.map(r => {
    const mktM = (r.mkt / 1e6).toFixed(0);
    const cmhcM = (r.cmhc / 1e6).toFixed(1);
    const bhPct = (r.mkt / bhTotal * 100).toFixed(1);
    const capCls = r.cap >= 15 ? 'green' : r.cap >= 5 ? 'yellow' : 'red';
    const benesCell = r.benes ? `<td style="text-align:right">${fmtBenes(r.benes)}</td>` : '<td style="text-align:right">—</td>';
    if (r.isParent) {
      return `<tr style="background:var(--bg-2);font-weight:600">
        <td><span style="display:inline-block;width:10px;height:10px;border-radius:2px;background:${r.color};margin-right:6px"></span>${esc(r.label)}</td>
        ${benesCell}
        <td style="text-align:right">$${mktM}M</td>
        <td style="text-align:right">$${cmhcM}M</td>
        <td style="text-align:center"><span class="fs-badge ${capCls}">${r.cap}%</span></td>
        <td style="font-size:.75rem;color:var(--text-3)">${esc(r.insight || '')}</td>
      </tr>`;
    }
    const indent = r.isSub ? 'padding-left:1.5rem' : '';
    const codeHint = r.codes ? ` <span style="font-size:.65rem;color:var(--text-4)">${r.codes.join(', ')}</span>` : '';
    return `<tr>
      <td style="${indent}"><span style="display:inline-block;width:10px;height:10px;border-radius:2px;background:${r.color};margin-right:6px"></span>${esc(r.label)}${codeHint}</td>
      ${benesCell}
      <td style="text-align:right">$${mktM}M</td>
      <td style="text-align:right">$${cmhcM}M</td>
      <td style="text-align:center"><span class="fs-badge ${capCls}">${r.cap}%</span></td>
      <td style="font-size:.75rem;color:var(--text-3)">${esc(r.insight || '')}</td>
    </tr>`;
  }).join('');

  // E&M footnote
  const emNote = em.fl_market_dollars
    ? `<div style="font-size:.75rem;color:var(--text-3);margin-top:.75rem;border-top:1px solid var(--border-2);padding-top:.5rem">
        <strong>*</strong> E&M codes (99202–99215) excluded — $${(em.fl_market_dollars/1e6).toFixed(0)}M market includes non-BH services (primary care, specialist visits). CMHCs bill $${(em.cmhc_dollars/1e6).toFixed(1)}M in E&M (${em.cmhc_capture_pct}% capture), mostly 99214.
      </div>`
    : '';

  return `
    <div class="fs-card blue" style="margin-bottom:1rem">
      <div class="fs-card-title">Where the Money Is: FL Medicaid BH Market ($${(bhTotal/1e6).toFixed(0)}M)*</div>
      <div class="fs-card-body">
        <div style="display:flex;align-items:center;gap:.75rem;margin-bottom:.75rem">
          <div style="flex:1;height:24px;border-radius:4px;overflow:hidden;display:flex;background:#e5e7eb">${barSegs}</div>
          <div style="font-size:.8rem;white-space:nowrap;color:var(--text-3)">CMHC share: <strong>${cmhcBhPct}%</strong> ($${(cmhcBhTotal/1e6).toFixed(0)}M)</div>
        </div>
        <table class="fs-table" style="font-size:.8rem">
          <thead><tr><th>Category</th><th style="text-align:right">Benes/Mo</th><th style="text-align:right">FL Market</th><th style="text-align:right">CMHC Rev</th><th style="text-align:center">CMHC Capture</th><th>Insight</th></tr></thead>
          <tbody>${rows}</tbody>
        </table>
        ${emNote}
      </div>
    </div>`;
}

// ── Chapter 1, Section 0: Data Calibration ────────────────────────────────────
function renderDataCalibration(ind) {
  const cal = ind.data_calibration;
  if (!cal) return '';
  const du = cal.doge_universe || {};
  const pub = cal.fl_medicaid_published || {};
  const tbl = cal.calibration_table || [];
  const tots = cal.totals || {};

  function fmtM(n) { return n == null ? '—' : '$' + Number(n).toLocaleString() + 'M'; }
  function fmtB(n) { return n == null ? '—' : '$' + (n / 1e9).toFixed(1) + 'B'; }
  function fmtK(n) { return n == null ? '—' : Math.round(n / 1000).toLocaleString() + 'K'; }
  function confBadge(c) {
    const map = {high:'🟢',medium:'🟡',very_low:'🔴',not_observable:'⬛',high_total_low_bh:'🟡'};
    return map[c] || '⚪';
  }
  function rangePct(lo, hi) {
    if (lo == null && hi == null) return '—';
    if (lo === 0 && hi === 0) return '0%';
    return lo + '–' + hi + '%';
  }

  // ── Extrapolation guidance per confidence level ──
  function extrapLabel(conf, covLo, covHi) {
    if (conf === 'high') return '<span style="color:var(--green-text)">Use as reported</span>';
    if (conf === 'medium') return '<span style="color:var(--amber)">~1.1–1.3× adjustment</span>';
    if (conf === 'very_low') return '<span style="color:var(--red)">' + (covLo != null ? `~${Math.round(100/covHi)}–${Math.round(100/covLo)}× (${covLo}–${covHi}% visible)` : 'Not extrapolable') + '</span>';
    if (conf === 'not_observable') return '<span style="color:var(--text-3)">Not in dataset</span>';
    if (conf === 'high_total_low_bh') return '<span style="color:var(--amber)">~1.0–1.3× (mostly non-BH)</span>';
    return '—';
  }

  // ── Build the OP BH children rows ──
  const opBh = tbl.find(r => r.id === 'op_bh');
  let opRows = '';
  if (opBh && opBh.children) {
    opRows = opBh.children.map(c => `
      <tr>
        <td style="padding-left:1.5rem;font-size:.8rem">${esc(c.label)}</td>
        <td class="num">${fmtM(c.doge_fl_M)}</td>
        <td class="num">${fmtK(c.doge_benes)}</td>
        <td class="num">${fmtM(c.cmhc_M)}</td>
        <td class="num">${fmtK(c.cmhc_benes)}</td>
        <td class="num"><strong>${c.cmhc_share_pct}%</strong></td>
        <td class="num"><strong>${c.cmhc_bene_share_pct}%</strong></td>
        <td class="num">${confBadge(c.confidence)}</td>
        <td class="num" style="font-size:.75rem">${extrapLabel(c.confidence)}</td>
      </tr>`).join('');
  }

  // ── Build non-OP rows ──
  const otherRows = tbl.filter(r => r.id !== 'op_bh').map(r => {
    const hasEst = r.est_fl_medicaid_lo_M != null;
    const estCell = hasEst ? fmtM(r.est_fl_medicaid_lo_M) + '–' + fmtM(r.est_fl_medicaid_hi_M) : '—';
    const covCell = rangePct(r.doge_coverage_lo_pct, r.doge_coverage_hi_pct);
    return `
      <tr style="border-top:1px solid var(--border)">
        <td><strong>${esc(r.label)}</strong></td>
        <td class="num">${fmtM(r.doge_fl_M)}</td>
        <td class="num">${r.doge_benes ? fmtK(r.doge_benes) : '—'}</td>
        <td class="num" colspan="2">${estCell}</td>
        <td class="num">${covCell}</td>
        <td class="num">${confBadge(r.confidence)}</td>
        <td class="num" style="font-size:.75rem">${extrapLabel(r.confidence, r.doge_coverage_lo_pct, r.doge_coverage_hi_pct)}</td>
      </tr>
      <tr><td colspan="9" style="font-size:.75rem;color:var(--text-3);padding:.15rem .5rem .5rem 1rem">${esc(r.rationale)}</td></tr>`;
  }).join('');

  return `
  <div class="fs-section" id="sec-data-calibration">
    <div class="fs-section-header">
      <span class="fs-section-tag blue">CHAPTER 1</span>
      <h2 class="fs-section-h">Data Source & Calibration</h2>
    </div>

    <div class="fs-narrative" style="margin-bottom:1rem">
      <p>This report uses the <strong>DOGE Medicaid Provider Spending</strong> dataset — outpatient and professional claims (HCPCS-coded) for all FL Medicaid-enrolled providers. Before analyzing any numbers, we calibrate what this data covers vs. the full FL Medicaid market.</p>
    </div>

    <div class="fs-stat-grid" style="grid-template-columns:repeat(4,1fr);margin-bottom:1.25rem">
      <div class="fs-stat-card">
        <div class="fs-stat-value">${fmtB(pub.total_spending?.value)}</div>
        <div class="fs-stat-label">Total FL Medicaid (FY2024)</div>
      </div>
      <div class="fs-stat-card">
        <div class="fs-stat-value">${fmtB(du.fl_paid)}</div>
        <div class="fs-stat-label">DOGE FL Coverage (${cal.coverage_summary?.doge_vs_total_pct || '—'}%)</div>
      </div>
      <div class="fs-stat-card">
        <div class="fs-stat-value">${(du.fl_providers || 0).toLocaleString()}</div>
        <div class="fs-stat-label">FL Providers in DOGE</div>
      </div>
      <div class="fs-stat-card">
        <div class="fs-stat-value">${(du.fl_pml_npis || 0).toLocaleString()}</div>
        <div class="fs-stat-label">FL Medicaid-Enrolled NPIs</div>
      </div>
    </div>

    <div class="fs-card blue" style="margin-bottom:.75rem">
      <div class="fs-card-title">What DOGE Captures</div>
      <div class="fs-card-body" style="font-size:.8125rem">
        <strong>Includes:</strong> Fee-for-service claims + partial managed care encounter data, outpatient/professional HCPCS-coded services<br>
        <strong>Excludes:</strong> Inpatient (DRGs), pharmacy, LTSS capitation, nursing facility per-diem, claims with <12 encounters (cell suppression)<br>
        <strong>FL Medicaid split:</strong> ${pub.mco_pct || 65}% managed care (SMMC) / ${pub.ffs_pct || 35}% fee-for-service · ${(pub.enrollment?.value/1e6).toFixed(1)}M enrollees
      </div>
    </div>

    <div class="fs-card" style="margin-bottom:1.25rem">
      <div class="fs-card-title">Calibration: DOGE Visibility by Service Line</div>
      <div class="fs-card-body" style="overflow-x:auto">
        <table class="fs-table" style="width:100%;font-size:.8rem">
          <thead>
            <tr>
              <th style="text-align:left">Service Line</th>
              <th class="num">DOGE FL ($)</th>
              <th class="num">DOGE Benes</th>
              <th class="num">CMHC ($)</th>
              <th class="num">CMHC Benes</th>
              <th class="num">CMHC $ Share</th>
              <th class="num">CMHC Bene Share</th>
              <th class="num">Confidence</th>
              <th class="num">Extrapolation</th>
            </tr>
          </thead>
          <tbody>
            <tr style="background:var(--bg-2);font-weight:600">
              <td>Outpatient BH</td>
              <td class="num">${fmtM(opBh?.doge_fl_M)}</td>
              <td class="num">—</td>
              <td class="num">${fmtM(opBh?.cmhc_M)}</td>
              <td class="num">—</td>
              <td class="num">${opBh?.cmhc_share_pct}%</td>
              <td class="num">—</td>
              <td class="num">${confBadge(opBh?.confidence)}</td>
              <td class="num" style="font-size:.75rem"><span style="color:var(--green-text)">~1.2–1.4× (${opBh?.doge_coverage_lo_pct}–${opBh?.doge_coverage_hi_pct}% visible)</span></td>
            </tr>
            ${opRows}
            <tr style="background:var(--bg-2);font-weight:600;border-top:2px solid var(--border)">
              <td colspan="9" style="padding-top:.75rem;font-size:.8125rem;letter-spacing:.03em">OTHER FL MEDICAID BH (not in OP BH totals above)</td>
            </tr>
          </tbody>
          <tbody>
            ${otherRows}
          </tbody>
          <tfoot>
            <tr style="border-top:2px solid var(--text-1);font-weight:700">
              <td>Total FL Medicaid BH (est.)</td>
              <td class="num">${fmtM(tots.doge_fl_visible_M)}</td>
              <td class="num">—</td>
              <td class="num" colspan="3">${fmtM(tots.est_fl_medicaid_bh_lo_M)}–${fmtM(tots.est_fl_medicaid_bh_hi_M)}</td>
              <td class="num">${tots.doge_coverage_lo_pct}–${tots.doge_coverage_hi_pct}%</td>
              <td></td>
              <td></td>
            </tr>
          </tfoot>
        </table>
      </div>
    </div>

    <div class="fs-card orange" style="margin-bottom:1rem">
      <div class="fs-card-title">What This Report Covers vs. What It Can't See</div>
      <div class="fs-card-body" style="font-size:.8125rem">
        <strong>This report analyzes: Outpatient BH ($795M visible, est. 69–86% of true market)</strong> — where DOGE coverage is strong and CMHC market position is measurable.<br><br>
        <strong>Not in this report:</strong><br>
        · <strong>ABA ($111M visible / $1.5–2.6B est.)</strong> — 4–7% coverage. Legislature budgeted $1.52B; larger than all other OP BH combined. Moved from FFS to managed care Feb 2025.<br>
        · <strong>SUD ($41M visible / $400–700M est.)</strong> — 6–10% coverage. Mostly residential, pharmacy (MAT), and MCO-routed. FL ranks 51st of 52 in per-capita MH spending.<br>
        · <strong>Inpatient BH ($0 visible / $500M–1B est.)</strong> — DRG billing not in DOGE. 174K Baker Act exams/yr.<br>
        · <strong>Pharmacy ($0 visible / $675–850M est.)</strong> — Antipsychotics alone est. $420–480M.
      </div>
    </div>
  </div>`;
}

// ── Chapter 1: Industry Landscape ───────────────────────────────────────────
function renderIndustryLandscape(ind) {
  const sector = ind.sector || {};
  const hero = ind.hero_stats || {};
  return `
  <div class="fs-section" id="sec-industry-landscape">
    <div class="fs-section-header">
      <span class="fs-section-tag blue">CHAPTER 1</span>
      <h2 class="fs-section-h">Florida CMHC Sector Landscape</h2>
    </div>

    <div class="fs-sector-ref" style="font-size:.9rem">
      <strong>${esc(ind.headline)}</strong>
    </div>

    <div class="fs-stat-grid" style="grid-template-columns:repeat(4,1fr)">
      <div class="fs-stat-card"><div class="fs-stat-value">${esc(hero.rate_gap?.value || '$8.1M')}</div><div class="fs-stat-label">${esc(hero.rate_gap?.label || 'Lost to Rate Gaps')}</div></div>
      <div class="fs-stat-card"><div class="fs-stat-value">${esc(hero.leakage_gap?.value || '$14.3M')}</div><div class="fs-stat-label">${esc(hero.leakage_gap?.label || 'Lost to Post-Crisis Leakage')}</div></div>
      <div class="fs-stat-card"><div class="fs-stat-value">${esc(hero.ratio?.value || '1.8x')}</div><div class="fs-stat-label">${esc(hero.ratio?.label || 'Leakage vs Rate Impact')}</div></div>
      <div class="fs-stat-card"><div class="fs-stat-value">${esc(hero.caseload_imbalance?.value || '3.3x')}</div><div class="fs-stat-label">${esc(hero.caseload_imbalance?.label || 'Intake-to-Therapy Caseload')}</div></div>
    </div>

    <div class="fs-card blue" style="margin-bottom:1rem">
      <div class="fs-card-title">Sector Profile</div>
      <div class="fs-card-body">
        <strong>${sector.n_orgs || 86}</strong> CMHCs across Florida · <strong>${sector.n_codes || 20}</strong> service codes analyzed · Period: <strong>${esc(sector.period || '2024')}</strong><br>
        Data source: ${esc(sector.data_source || 'FL Medicaid FFS (DOGE public data)')}<br>
        Comparison group: <strong>${esc(sector.comparison_group || 'Rest of FL Medicaid (excluding CMHCs)')}</strong>
      </div>
    </div>

    ${_renderMarketComposition(ind)}

    <div class="fs-narrative">
      <p>Florida's 86 Community Mental Health Centers are the behavioral health safety net — the front door for Medicaid beneficiaries in crisis or seeking care for the first time. This report analyzes the sector through <strong>three core metrics</strong> that together tell the full financial story:</p>
      <ul style="margin:.5rem 0 .75rem 1.2rem">
        <li><strong>Payment per Claim (PPC)</strong> — what you collect per service. Are your rates competitive?</li>
        <li><strong>Beneficiaries per Clinician (BPC)</strong> — your panel size. Is your workforce capacity healthy?</li>
        <li><strong>Market Share</strong> — what % of FL Medicaid BH patients you serve. Where are you losing patients?</li>
      </ul>
      <p>We measure these across <strong>5 service categories</strong> and benchmark against <strong>Rest-of-FL P75</strong> — best-in-class performance — as the transformation target. What emerges is a sector with an $8.1M rate gap, a $14.3M post-crisis leakage gap, and a caseload imbalance that compounds both.</p>
    </div>

    <div class="fs-insight" style="margin-bottom:1rem">
      <strong>💡 The big picture:</strong> The rate problem is real ($8.1M). Post-crisis leakage adds another $14.3M. CMHCs handle 32% of crisis/ACT but only 12% of ongoing BH — <em>post-crisis retention</em> is the hidden lever.
    </div>

    <div class="fs-card-grid" style="grid-template-columns:repeat(5,1fr)">
      ${CATEGORY_ORDER.map(key => {
        const cat = (ind.categories || ind.segments || {})[key] || {};
        const meta = CATEGORY_META[key] || {};
        return `
        <div class="fs-card ${meta.color || 'blue'}">
          <div class="fs-card-title">${esc(cat.label || meta.label)}</div>
          <div class="fs-card-big">${cat.cmhc_share || '—'}%</div>
          <div class="fs-card-sub">CMHC market share</div>
          <div style="margin-top:.5rem;font-size:.75rem;color:var(--text-3)">
            CMHC RPB: ${fmt$(cat.sector_rpb)} · Rest-of-FL RPB: ${fmt$(cat.rest_rpb)}
          </div>
          <div style="font-size:.7rem;color:var(--text-3);margin-top:.25rem">${(cat.codes || []).join(', ')}</div>
        </div>`;
      }).join('')}
    </div>
  </div>`;
}

function renderIndustryRates(ind) {
  const rates = ind.problems?.rates || {};
  const benchmarks = ind.benchmark_table || [];
  const catMap = _getCategoryMap(ind);
  const panels = ind.problems?.burnout?.panel_gaps || [];
  const engagement = ind.problems?.engagement || {};
  const mktShare = engagement.market_share || {};

  // Build a unified row per code: rate + panel + leakage
  const benchByCode = {};
  for (const b of benchmarks) benchByCode[b.code] = b;
  const panelByCode = {};
  for (const p of panels) panelByCode[p.code] = p;

  const allCodes = [...new Set([...benchmarks.map(b=>b.code), ...panels.map(p=>p.code)])];
  const items = allCodes.map(code => ({code, ...benchByCode[code], panel: panelByCode[code]}));
  const grouped = _groupByCategory(items, catMap);

  function renderRow(d) {
    const rateGap = d.cmhc_p50 && d.rest_p75 ? ((d.cmhc_p50 - d.rest_p75) / d.rest_p75 * 100) : null;
    const rateCls = rateGap == null ? '' : rateGap >= 0 ? 'green' : rateGap >= -15 ? 'yellow' : 'red';
    const p = d.panel;
    const panelGap = p ? p.gap_pct : null;
    const panelCls = panelGap == null ? '' : panelGap > 20 ? 'red' : panelGap > 0 ? 'yellow' : panelGap > -20 ? 'yellow' : 'green';
    return `<tr>
      <td><strong>${esc(d.code)}</strong><br><span style="font-size:.7rem;color:var(--text-3)">${esc(_codeLabel(d.code))}</span></td>
      <td>${fmt$(d.cmhc_p50)}</td>
      <td>${fmt$(d.rest_p50)}</td>
      <td><strong>${fmt$(d.rest_p75)}</strong></td>
      <td>${rateGap != null ? `<span class="fs-badge ${rateCls}">${rateGap > 0 ? '+' : ''}${rateGap.toFixed(1)}%</span>` : '—'}</td>
      <td>${p ? fmtN(p.cmhc_panel) : '—'}</td>
      <td>${p ? fmtN(p.rest_panel) : '—'}</td>
      <td>${panelGap != null ? `<span class="fs-badge ${panelCls}">${panelGap > 0 ? '+' : ''}${panelGap.toFixed(0)}%</span>` : '—'}</td>
      <td>${fmt$(d.rest_p50_rpb)}</td>
    </tr>`;
  }

  let tableRows = '';
  for (const cat of CATEGORY_ORDER) {
    const rows = (grouped[cat] || []).sort((a,b) => (CMHC_REVENUE[b.code]||0) - (CMHC_REVENUE[a.code]||0));
    if (!rows.length) continue;
    const meta = CATEGORY_META[cat];
    const share = mktShare[cat]?.cmhc_pct;
    const shareLabel = share != null ? ` · CMHC share: ${share}%` : '';
    tableRows += `<tr class="fs-cat-header"><td colspan="9"><span class="fs-badge ${meta.color}">${esc(meta.short)}</span>${shareLabel}</td></tr>`;
    tableRows += rows.map(renderRow).join('');
  }

  return `
  <div class="fs-section" id="sec-industry-rates">
    <div class="fs-section-header">
      <span class="fs-section-tag problem">RATES · PANEL · LEAKAGE</span>
      <h2 class="fs-section-h">CMHC vs Rest-of-FL by Category</h2>
    </div>

    <div class="fs-sector-ref">${esc(rates.description)}</div>

    <div class="fs-narrative">
      <p>Now we benchmark CMHCs against the broader market. This table consolidates <strong>all three core metrics</strong> — PPC (rate), BPC (panel), and market share — by service category. The key column is <strong>Gap to Rest-of-FL P75</strong>: this is the distance to best-in-class, and it's significant.</p>
      <p>The P50→P75 spread in the Rest-of-FL market is often <strong>20-40%</strong>. That spread represents the difference between average and excellent. CMHCs that close this gap capture materially more revenue per claim without seeing more patients.</p>
    </div>

    <div class="fs-insight" style="margin-bottom:1rem">
      <strong>💡 Aha:</strong> The spread between P50 and P75 is where the money is. On T1017 (care mgmt), P75 pays $78.88 vs P50 at $63.45 — a 24% premium. On T1015 (med mgmt), P75 pays $172.44 vs P50 at $70.86 — a <em>143%</em> premium. P75 is the benchmark worth chasing.
    </div>

    <div class="fs-table-wrap">
      <table class="fs-table" style="font-size:.8rem">
        <thead>
          <tr>
            <th rowspan="2">Code</th>
            <th colspan="4" style="text-align:center;border-bottom:1px solid var(--border)">Rate (PPC)</th>
            <th colspan="3" style="text-align:center;border-bottom:1px solid var(--border)">Panel (BPC)</th>
            <th rowspan="2">RPB</th>
          </tr>
          <tr>
            <th>CMHC</th><th>Rest P50</th><th>Rest P75</th><th>Gap</th>
            <th>CMHC</th><th>Rest-FL</th><th>Gap</th>
          </tr>
        </thead>
        <tbody>${tableRows}</tbody>
      </table>
    </div>

    <div class="fs-insight" style="margin-top:1rem">
      <strong>Reading this table:</strong> Green badges = CMHC outperforms the benchmark. Red = underperforms. The rate gap shows distance to Rest-of-FL P75 (the target). Panel gap shows clinician workload relative to the market. RPB shows revenue per beneficiary — a proxy for engagement depth.
    </div>
  </div>`;
}

function _renderMarketShareTrends(ind) {
  const trends = ind.market_share_trends || {};
  const years = Object.keys(trends).filter(y => !y.startsWith('_')).sort();
  if (years.length < 2) return '';

  const cats = [
    {key: 'intake', label: 'Intake', color: '#3b82f6'},
    {key: 'high_acuity', label: 'High Acuity', color: '#ef4444'},
    {key: 'ongoing_bh', label: 'Ongoing BH', color: '#22c55e'},
    {key: 'med_mgmt', label: 'Med Mgmt', color: '#f59e0b'},
    {key: 'total', label: 'Total CMHC', color: '#6b7280'},
  ];

  // Build sparkline SVGs for each category
  const W = 120, H = 32, PAD = 2;
  function sparkline(key, color) {
    const vals = years.map(y => trends[y]?.[key] || 0);
    const mn = Math.min(...vals) * 0.8;
    const mx = Math.max(...vals) * 1.1 || 1;
    const pts = vals.map((v, i) => {
      const x = PAD + (i / (vals.length - 1)) * (W - PAD * 2);
      const y = H - PAD - ((v - mn) / (mx - mn)) * (H - PAD * 2);
      return `${x},${y}`;
    }).join(' ');
    const first = vals[0], last = vals[vals.length - 1];
    const delta = last - first;
    const arrow = delta > 1 ? '↑' : delta < -1 ? '↓' : '→';
    const deltaColor = delta > 1 ? 'var(--green, #16a34a)' : delta < -1 ? 'var(--red, #dc2626)' : '#6b7280';
    return `<td style="text-align:center;padding:4px 8px">
      <svg width="${W}" height="${H}" style="display:block;margin:0 auto"><polyline points="${pts}" fill="none" stroke="${color}" stroke-width="2"/></svg>
      <span style="font-size:.7rem;color:${deltaColor};font-weight:600">${arrow} ${first}% → ${last}% (${delta > 0 ? '+' : ''}${delta.toFixed(1)}pp)</span>
    </td>`;
  }

  const headerRow = years.map(y => `<th style="font-size:.7rem;color:var(--text-3);padding:2px 6px;text-align:center">${y}</th>`).join('');
  const dataRows = cats.map(c => {
    const vals = years.map(y => {
      const v = trends[y]?.[c.key];
      return `<td style="text-align:center;padding:3px 6px;font-size:.8rem;font-weight:${c.key === 'total' ? '700' : '500'}">${v != null ? v + '%' : '—'}</td>`;
    }).join('');
    return `<tr${c.key === 'total' ? ' style="border-top:2px solid var(--border);background:var(--bg-2,#f8fafc)"' : ''}>
      <td style="padding:3px 8px;font-size:.8rem;font-weight:600"><span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:${c.color};margin-right:4px"></span>${c.label}</td>
      ${vals}
      ${sparkline(c.key, c.color)}
    </tr>`;
  }).join('');

  return `
    <h3 style="font-size:.9rem;font-weight:700;margin:1.5rem 0 .5rem">Market Share Trend: CMHC Capture by Category (2019–2024)</h3>
    <div style="overflow-x:auto">
      <table class="fs-table" style="width:100%">
        <thead><tr>
          <th style="text-align:left;padding:2px 8px;font-size:.7rem;color:var(--text-3)">Category</th>
          ${headerRow}
          <th style="font-size:.7rem;color:var(--text-3);text-align:center;padding:2px 8px">Trend</th>
        </tr></thead>
        <tbody>${dataRows}</tbody>
      </table>
    </div>
    <div class="fs-insight" style="margin-top:.75rem">
      <strong>💡 Key trend:</strong> High acuity share surged from 12% to 31% — CMHCs are becoming the crisis backbone.
      Ongoing BH share recovered to 12% after a dip in 2022.
      Med mgmt share nearly doubled (14.5% → 24.5%).
      The sector is growing its market position, but primarily in acute and medication services.
    </div>
  `;
}

function renderIndustryLeakage(ind) {
  const eng = ind.problems?.engagement || {};
  const share = eng.market_share || {};
  const leak = eng.leakage_math || {};
  const surprise = eng.surprising_finding || {};

  return `
  <div class="fs-section" id="sec-industry-leakage">
    <div class="fs-section-header">
      <span class="fs-section-tag problem">PROBLEM 2</span>
      <h2 class="fs-section-h">${esc(eng.label || 'Patient Leakage')}</h2>
    </div>

    <div class="fs-sector-ref">${esc(eng.description)}</div>

    <div class="fs-narrative">
      <p>CMHCs capture 15% of intake and 12% of ongoing care — the retention gap is smaller than initially estimated. But the real signal is in <strong>high acuity</strong>: CMHCs handle <strong>32% of crisis/ACT services</strong> but only 12% of ongoing BH. The question is post-crisis retention — do patients step down to ongoing CMHC care or leave the system?</p>
      <p>The critical question remains: <strong>why do patients leave after crisis stabilization?</strong> Is it a <em>supply gap</em> (CMHCs don't offer step-down services) or a <em>handoff gap</em> (they offer it, but the transition fails)?</p>
    </div>

    <div class="fs-insight" style="margin-bottom:1rem">
      <strong>💡 Aha:</strong> It's largely a <em>supply gap</em>. Only 1 of 25 codes (H2019) is offered by all 86 CMHCs. Care management (T1017) — an $18M revenue category — is offered by only 40% of CMHCs. High-acuity services average just 17% coverage. Patients can't stay for services that aren't offered.
    </div>

    <h3 style="font-size:.9rem;font-weight:700;margin:1.5rem 0 .5rem">Market Share by Category</h3>
    <div class="fs-stat-grid" style="grid-template-columns:repeat(5,1fr)">
      ${CATEGORY_ORDER.map(cat => {
        const meta = CATEGORY_META[cat];
        const pct = share[cat]?.cmhc_pct ?? '—';
        const isLow = typeof pct === 'number' && pct < 10;
        return `<div class="fs-stat-card"${isLow ? ' style="border-color:var(--red-border)"' : ''}>
          <div class="fs-stat-value"${isLow ? ' style="color:var(--red)"' : ''}>${pct}%</div>
          <div class="fs-stat-label">${esc(meta.short)}</div>
        </div>`;
      }).join('')}
    </div>

    ${_renderMarketShareTrends(ind)}

    <h3 style="font-size:.9rem;font-weight:700;margin:1.5rem 0 .5rem">Supply Gap: % of CMHCs Offering Each Service</h3>
    <div class="fs-narrative" style="margin-bottom:.75rem">
      <p>If patients leave because the CMHC <em>doesn't offer</em> the service, that's a supply gap. The table below shows how many of 86 CMHCs bill each code. Low coverage = supply gap = structural leakage that no amount of retention effort can fix.</p>
    </div>
    ${_renderCoverageTable(ind)}

    <h3 style="font-size:.9rem;font-weight:700;margin:1.5rem 0 .5rem">Supply Gap vs Retention Gap by Category</h3>
    ${_renderSupplyRetentionCards(ind)}

    <div class="fs-card-grid-2" style="margin-top:1.25rem">
      <div class="fs-card red">
        <div class="fs-card-title">Leakage Math</div>
        <div class="fs-card-body">
          Current ongoing revenue: <strong>${fmt$(leak.current_ongoing_revenue)}</strong><br>
          If CMHCs retained intake share: <strong>${fmt$(leak.if_retained_intake_share)}</strong><br>
          <strong style="font-size:1.1em">Gap: ${fmt$(leak.leakage_gap)}</strong><br>
          <span style="font-size:.75rem;color:var(--text-3)">${esc(leak.note || '')}</span>
        </div>
      </div>
      <div class="fs-card green">
        <div class="fs-card-title">When Patients Stay, CMHCs Win</div>
        <div class="fs-card-body">
          CMHC ongoing RPB: <strong>${fmt$(surprise.cmhc_ongoing_rpb)}</strong><br>
          Rest-of-FL ongoing RPB: <strong>${fmt$(surprise.rest_ongoing_rpb)}</strong><br>
          Advantage: <strong>+${surprise.advantage_pct || ''}%</strong><br>
          <span style="font-size:.75rem;margin-top:.25rem;display:block">${esc(surprise.implication || '')}</span>
        </div>
      </div>
    </div>

    <div class="fs-insight" style="margin-top:1rem">
      <strong>Rate gaps ($8.1M) + post-crisis leakage ($14.3M) = $22.4M total sector opportunity.</strong>
      CMHCs dominate crisis care (32% capture) but lose patients in the step-down. Expanding service line coverage — especially care management, ongoing therapy, and CPT psychotherapy — is the highest-leverage move.
    </div>
  </div>`;
}

function renderIndustryBurnout(ind) {
  const burn = ind.problems?.burnout || {};
  const kpi = burn.caseload_kpi || {};
  const panels = burn.panel_gaps || [];
  const cycle = burn.burnout_cycle || [];
  const intake = kpi.intake_overload || {};
  const ongoing = kpi.ongoing_underload || {};
  const careMgmt = kpi.care_mgmt_load || {};

  const catMap = _getCategoryMap(ind);
  const panelItems = panels.map(p => ({code: p.code, ...p}));
  const panelGrouped = _groupByCategory(panelItems, catMap);

  let panelRows = '';
  for (const cat of CATEGORY_ORDER) {
    const rows = panelGrouped[cat] || [];
    if (!rows.length) continue;
    const meta = CATEGORY_META[cat];
    panelRows += `<tr class="fs-cat-header"><td colspan="5"><span class="fs-badge ${meta.color}">${esc(meta.short)}</span></td></tr>`;
    panelRows += rows.map(p => `
      <tr>
        <td><strong>${esc(p.code)}</strong><br><span style="font-size:.7rem;color:var(--text-3)">${esc(_codeLabel(p.code))}</span></td>
        <td>${fmtN(p.cmhc_panel)}</td>
        <td>${fmtN(p.rest_panel)}</td>
        <td><span class="fs-badge ${p.gap_pct > 0 ? 'red' : p.gap_pct > -20 ? 'yellow' : 'green'}">${p.gap_pct > 0 ? '+' : ''}${fmtPct(p.gap_pct)}</span></td>
        <td style="font-size:.75rem;color:var(--text-3)">${esc(p.note || '')}</td>
      </tr>`).join('');
  }

  return `
  <div class="fs-section" id="sec-industry-burnout">
    <div class="fs-section-header">
      <span class="fs-section-tag problem">PROBLEM 3</span>
      <h2 class="fs-section-h">${esc(burn.label || 'Caseload Imbalance & Clinician Burnout')}</h2>
    </div>

    <div class="fs-sector-ref">
      <strong>${esc(kpi.headline || burn.description)}</strong>
    </div>

    <div class="fs-narrative">
      <p>Rate gaps and leakage don't just cost money — they break the people delivering care. Lower rates force higher volume. Leakage means intake clinicians are on a treadmill: constantly processing new patients who don't convert to sustained caseloads.</p>
      <p>CMHC intake clinicians carry panels of <strong>115 patients — 50% more than the industry norm</strong>. Therapy panels sit at 123, barely half the market's 227. Care managers at 275 patients absorb the coordination burden. The 3.3x intake-to-therapy imbalance ratio is the signature of this cycle.</p>
    </div>

    <div class="fs-insight" style="margin-bottom:1rem">
      <strong>💡 Aha:</strong> Burnout isn't a standalone problem — it's the <em>outcome</em> of Problems 1 and 2. Fix leakage (so patients stay) and rates (so you need fewer visits), and the caseload rebalances itself. The 3.3x intake-to-therapy ratio is the diagnostic metric.
    </div>

    <div class="fs-stat-grid" style="grid-template-columns:repeat(3,1fr);margin-top:1rem">
      <div class="fs-stat-card" style="border-left:3px solid var(--red)">
        <div class="fs-stat-value" style="color:var(--red)">${fmtN(intake.cmhc_panel)} pts</div>
        <div class="fs-stat-label">Intake Caseload (H0031)</div>
        <div style="margin-top:.35rem;font-size:.75rem;color:var(--text-3)">
          Industry: ${fmtN(intake.industry_panel)} pts
          <span class="fs-badge red" style="margin-left:.25rem">+${Math.round(intake.gap_pct || 0)}% over</span>
        </div>
      </div>
      <div class="fs-stat-card" style="border-left:3px solid var(--amber)">
        <div class="fs-stat-value" style="color:var(--amber)">${fmtN(ongoing.cmhc_panel)} pts</div>
        <div class="fs-stat-label">Therapy Caseload (H2019)</div>
        <div style="margin-top:.35rem;font-size:.75rem;color:var(--text-3)">
          Industry: ${fmtN(ongoing.industry_panel)} pts
          <span class="fs-badge yellow" style="margin-left:.25rem">${Math.round(ongoing.gap_pct || 0)}% under</span>
        </div>
      </div>
      <div class="fs-stat-card" style="border-left:3px solid var(--indigo)">
        <div class="fs-stat-value" style="color:var(--indigo)">${kpi.imbalance_ratio || '—'}x</div>
        <div class="fs-stat-label">Intake-to-Therapy Ratio</div>
        <div style="margin-top:.35rem;font-size:.75rem;color:var(--text-3)">
          ${esc(kpi.imbalance_note || 'Intake clinicians carry disproportionate load')}
        </div>
      </div>
    </div>

    <div class="fs-card-grid" style="grid-template-columns:1fr 1fr;margin-top:1rem">
      <div class="fs-card red">
        <div class="fs-card-title">Front Door Overload</div>
        <div class="fs-card-body">${esc(intake.interpretation || '')}</div>
      </div>
      <div class="fs-card yellow">
        <div class="fs-card-title">Back Door Leakage</div>
        <div class="fs-card-body">${esc(ongoing.interpretation || '')}</div>
      </div>
    </div>

    <h3 style="font-size:.875rem;font-weight:700;margin:1.5rem 0 .5rem">Caseload by Service Code</h3>
    <div class="fs-table-wrap">
      <table class="fs-table">
        <thead><tr><th>Code</th><th>CMHC Panel</th><th>Rest-of-FL</th><th>Gap</th><th>Note</th></tr></thead>
        <tbody>${panelRows}</tbody>
      </table>
    </div>

    <div class="fs-card yellow" style="margin-top:1rem">
      <div class="fs-card-title">The Burnout Cycle</div>
      <div class="fs-card-body">
        <ol style="padding-left:1.25rem;margin:0">${cycle.map(s => `<li style="margin-bottom:.35rem">${esc(s)}</li>`).join('')}</ol>
      </div>
    </div>

    <div class="fs-insight" style="margin-top:1rem">
      <strong>Each org feels this cycle differently</strong> based on their rate position (Problem 1) and engagement pattern (Problem 2).
      Orgs with above-market rates have a buffer. Orgs with strong retention have less churn pressure.
      The next chapter maps where <em>your</em> organization sits within each problem.
    </div>
  </div>`;
}

// ── Chapter 1: Industry Trends ──────────────────────────────────────────────
function renderIndustryTrends(ind) {
  const trends = ind.trends || [];
  if (!trends.length) return '';

  // Build rows and compute direction arrows
  const first = trends[0];
  const last = trends[trends.length - 1];

  function arrow(first, last, inverted = false) {
    // For rates: gap is negative (CMHC earns less), getting less negative = improving
    // For leakage RPB: positive = CMHC advantage, growing = good
    // inverted: true when bigger number = worse
    const delta = last - first;
    const improved = inverted ? delta < 0 : delta > 0;
    const flat = Math.abs(delta) < 1;
    if (flat) return { icon: '→', cls: 'yellow', label: 'Flat' };
    return improved
      ? { icon: '↑', cls: 'green', label: 'Improving' }
      : { icon: '↓', cls: 'red', label: 'Worsening' };
  }

  const rateDir = arrow(first.rate_gap_pct, last.rate_gap_pct, false);
  // Rate gap is negative — less negative = improving, so invert display
  const rateImproving = last.rate_gap_pct > first.rate_gap_pct;
  const rateArrow = Math.abs(last.rate_gap_pct - first.rate_gap_pct) < 1
    ? { icon: '→', cls: 'yellow', label: 'Flat' }
    : rateImproving
      ? { icon: '↑', cls: 'green', label: 'Narrowing' }
      : { icon: '↓', cls: 'red', label: 'Widening' };

  const rpbDir = last.rpb_gap_pct != null && first.rpb_gap_pct != null
    ? arrow(first.rpb_gap_pct, last.rpb_gap_pct, false)
    : { icon: '—', cls: 'yellow', label: 'N/A' };

  const caseloadFirst = first.intake_therapy_ratio;
  const caseloadLast = last.intake_therapy_ratio;
  const caseloadDir = caseloadFirst != null && caseloadLast != null
    ? (Math.abs(caseloadLast - caseloadFirst) < 0.1
        ? { icon: '→', cls: 'yellow', label: 'Flat' }
        : caseloadLast < caseloadFirst
          ? { icon: '↓', cls: 'green', label: 'Rebalancing' }
          : { icon: '↑', cls: 'red', label: 'More Imbalanced' })
    : { icon: '—', cls: 'yellow', label: 'N/A' };

  const yearCols = trends.map(t => `<th>${t.year}</th>`).join('');

  const rateRow = trends.map(t =>
    `<td><span class="fs-badge ${t.rate_gap_pct > -10 ? 'yellow' : 'red'}">${t.rate_gap_pct > 0 ? '+' : ''}${t.rate_gap_pct}%</span></td>`
  ).join('');

  const rpbRow = trends.map(t =>
    `<td>${t.rpb_gap_pct != null ? `<span class="fs-badge ${t.rpb_gap_pct > 0 ? 'green' : 'red'}">${t.rpb_gap_pct > 0 ? '+' : ''}${t.rpb_gap_pct}%</span>` : '—'}</td>`
  ).join('');

  const caseloadRow = trends.map(t =>
    `<td>${t.intake_therapy_ratio != null ? `<strong>${t.intake_therapy_ratio}x</strong>` : '—'}</td>`
  ).join('');

  return `
  <div class="fs-section" id="sec-industry-trends">
    <div class="fs-section-header">
      <span class="fs-section-tag blue">TREND ANALYSIS</span>
      <h2 class="fs-section-h">How Have These Problems Changed? (${first.year}–${last.year})</h2>
    </div>

    <div class="fs-narrative">
      <p>Are these problems getting better or worse? The six-year trend data is sobering: the rate gap has widened (-3% → -14%), the RPB advantage is eroding, and the caseload imbalance persists. None of these problems are self-correcting.</p>
    </div>

    <div class="fs-insight" style="margin-bottom:1rem">
      <strong>💡 Aha:</strong> The rate gap has widened from -3% to -14% since 2019. The broader market captured rate increases that CMHCs missed. Without deliberate intervention — rate renegotiation, service expansion, retention infrastructure — the gap compounds.
    </div>

    <div class="fs-stat-grid" style="grid-template-columns:repeat(3,1fr);margin-bottom:1.25rem">
      <div class="fs-stat-card">
        <div class="fs-stat-value" style="color:var(--${rateArrow.cls === 'green' ? 'green' : rateArrow.cls === 'red' ? 'red' : 'amber'})">${rateArrow.icon}</div>
        <div class="fs-stat-label">Rate Gap</div>
        <div style="font-size:.75rem;color:var(--text-3);margin-top:.25rem">
          <span class="fs-badge ${rateArrow.cls}">${rateArrow.label}</span>
          ${first.rate_gap_pct}% → ${last.rate_gap_pct}%
        </div>
      </div>
      <div class="fs-stat-card">
        <div class="fs-stat-value" style="color:var(--${rpbDir.cls === 'green' ? 'green' : rpbDir.cls === 'red' ? 'red' : 'amber'})">${rpbDir.icon}</div>
        <div class="fs-stat-label">Ongoing Care RPB Advantage</div>
        <div style="font-size:.75rem;color:var(--text-3);margin-top:.25rem">
          <span class="fs-badge ${rpbDir.cls}">${rpbDir.label}</span>
          ${first.rpb_gap_pct ?? '—'}% → ${last.rpb_gap_pct ?? '—'}%
        </div>
      </div>
      <div class="fs-stat-card">
        <div class="fs-stat-value" style="color:var(--${caseloadDir.cls === 'green' ? 'green' : caseloadDir.cls === 'red' ? 'red' : 'amber'})">${caseloadDir.icon}</div>
        <div class="fs-stat-label">Caseload Imbalance</div>
        <div style="font-size:.75rem;color:var(--text-3);margin-top:.25rem">
          <span class="fs-badge ${caseloadDir.cls}">${caseloadDir.label}</span>
          ${caseloadFirst ?? '—'}x → ${caseloadLast ?? '—'}x
        </div>
      </div>
    </div>

    <div class="fs-table-wrap">
      <table class="fs-table">
        <thead>
          <tr><th>Metric</th>${yearCols}<th>Direction</th></tr>
        </thead>
        <tbody>
          <tr>
            <td><strong>Rate Gap</strong><br><span style="font-size:.7rem;color:var(--text-3)">CMHC vs Rest-of-FL PPC</span></td>
            ${rateRow}
            <td><span class="fs-badge ${rateArrow.cls}">${rateArrow.icon} ${rateArrow.label}</span></td>
          </tr>
          <tr>
            <td><strong>Ongoing Care RPB</strong><br><span style="font-size:.7rem;color:var(--text-3)">CMHC advantage in RPB</span></td>
            ${rpbRow}
            <td><span class="fs-badge ${rpbDir.cls}">${rpbDir.icon} ${rpbDir.label}</span></td>
          </tr>
          <tr>
            <td><strong>Caseload Ratio</strong><br><span style="font-size:.7rem;color:var(--text-3)">Intake ÷ Therapy panel</span></td>
            ${caseloadRow}
            <td><span class="fs-badge ${caseloadDir.cls}">${caseloadDir.icon} ${caseloadDir.label}</span></td>
          </tr>
        </tbody>
      </table>
    </div>

    <div class="fs-insight" style="margin-top:1rem">
      <strong>Bottom line:</strong> None of these problems are self-correcting. The rate gap is widening, the RPB advantage is eroding, and the caseload imbalance persists.
      Without deliberate intervention — rate renegotiation, retention infrastructure, caseload rebalancing — the sector's structural pressures will continue to compound.
      The next chapter positions <em>your</em> organization within these dynamics to identify where you have leverage and where you're most exposed.
    </div>
  </div>`;
}

// ── CMHC revenue by code (for sort-by-relevance) ───────────────────────────
const CMHC_REVENUE = {
  'H2017':17601670,'H2019':16891459,'T1017':12132488,'T1015':5665842,
  'H0032':2968865,'H0040':2528102,'H0031':1937619,'99214':1763947,
  'H2000':1732101,'99213':754070,'90837':615566,'90833':348442,
  '99215':181871,'90792':141435,'H2010':108540,'H0036':108270,
  'H0048':46530,'T1007':36489,'90791':36420,'90834':35590,
  '99203':29678,'90832':28947,'99204':26833,'99212':24898,'H0004':10889,
  '99202':0,'99205':0,'90836':0,'90838':0,'M0064':0,'90863':0,
};

// Category-level takeaways for published rates table
const CATEGORY_TAKEAWAYS = {
  intake: 'CMHCs are the gateway — 23% market share on intake, but tiered billing means most bill at the lowest rate.',
  high_acuity: 'High-acuity is CMHC-dominated where offered (H0036: 57%, H2010: 37% share) but only a handful of CMHCs bill these codes.',
  ongoing_bh: 'H2019 therapy is the anchor ($16.9M) but CPT psychotherapy codes (90832-90837) are underutilized — CMHCs collect 33% less than market on 60-min sessions.',
  eval_mgmt: 'E&M is not a CMHC business — <2% market share across all levels. 99214 is the only code with meaningful volume.',
  med_mgmt: 'T1015 drives $5.7M in revenue but CMHCs collect 9% less than published. 90833 (add-on psychotherapy) is a bright spot at 24% share.',
};

// ── Section: Published Rate Comparison ───────────────────────────────────────
function renderIndustryPublishedRates(ind) {
  const pub = ind.published_rate_comparison || {};
  const codes = pub.codes || {};
  const findings = pub.key_findings || [];
  if (!Object.keys(codes).length) return '';

  const catMap = _getCategoryMap(ind);
  const items = Object.entries(codes).map(([code, d]) => ({code, ...d}));
  const grouped = _groupByCategory(items, catMap);

  function renderRow(d) {
    // Show range if available, else single rate
    const pubDisplay = d.published_range ? esc(d.published_range) : fmt$(d.published_rate);
    // % of all FL claims done by CMHCs
    const claimShare = d.cmhc_claim_share_pct;
    const shareCls = claimShare == null ? '' : claimShare >= 20 ? 'green' : claimShare >= 10 ? 'yellow' : 'red';
    return `<tr>
      <td><strong>${esc(d.code)}</strong><br><span style="font-size:.75rem;color:var(--text-3)">${esc(_codeLabel(d.code))}</span></td>
      <td style="font-size:.8rem;color:var(--text-3)">${esc(d.unit)}</td>
      <td style="font-size:.8rem">${pubDisplay}</td>
      <td>${fmt$(d.cmhc_p50_ppc)}</td>
      <td>${fmt$(d.all_fl_p50_ppc)}</td>
      <td>${claimShare != null ? `<span class="fs-badge ${shareCls}">${claimShare.toFixed(1)}%</span>` : '—'}</td>
    </tr>`;
  }

  let tableRows = '';
  for (const cat of CATEGORY_ORDER) {
    const rows = (grouped[cat] || []).sort((a,b) => (CMHC_REVENUE[b.code]||0) - (CMHC_REVENUE[a.code]||0));
    if (!rows.length) continue;
    const meta = CATEGORY_META[cat];
    const takeaway = CATEGORY_TAKEAWAYS[cat] || '';
    tableRows += `<tr class="fs-cat-header"><td colspan="6"><span class="fs-badge ${meta.color}">${esc(meta.short)}</span>${takeaway ? `<span style="margin-left:.75rem;font-size:.75rem;color:var(--text-3)">${esc(takeaway)}</span>` : ''}</td></tr>`;
    tableRows += rows.map(renderRow).join('');
  }

  return `
  <div class="fs-section" id="sec-industry-published-rates">
    <div class="fs-section-header">
      <span class="fs-section-tag blue">FEE SCHEDULE</span>
      <h2 class="fs-section-h">Effective Rates vs Published Fee Schedule</h2>
    </div>

    <div class="fs-narrative">
      <p>Before benchmarking against peers, we establish the <strong>baseline</strong>: what does the state actually pay? Florida Medicaid publishes fee schedule rates (AHCA Rule 59G-4.002). The table below shows that <strong>the broad market runs close to published rates</strong> — this is the floor, not the ceiling.</p>
      <p>Time-based codes (therapy, PSR, care mgmt) show effective rates 3-14x their per-unit published rate due to bundled billing. Event-based codes (intake, med mgmt, E&M) show rates <em>at or below</em> published — these are the real pressure points.</p>
    </div>

    <div class="fs-insight" style="margin-bottom:1rem">
      <strong>💡 Aha:</strong> The market runs <em>around</em> published rates — not above them. But notice the last column: many codes are offered by fewer than half of CMHCs. The published rate is accessible to everyone, yet most CMHCs leave revenue on the table by not billing these codes at all.
    </div>

    <div class="fs-table-wrap">
      <table class="fs-table">
        <thead>
          <tr><th>Code</th><th>Unit</th><th>Published Rate</th><th>CMHC P50</th><th>All FL P50</th><th>CMHC Mkt Share</th></tr>
        </thead>
        <tbody>${tableRows}</tbody>
      </table>
    </div>

    ${findings.length ? `
    <div class="fs-card blue" style="margin-top:1rem">
      <div class="fs-card-title">Key Findings</div>
      <div class="fs-card-body">
        <ul style="margin:0;padding-left:1.2rem">${findings.map(f => `<li>${esc(f)}</li>`).join('')}</ul>
      </div>
    </div>` : ''}
  </div>`;
}

// ── Section: CMHC vs FQHC Comparison ────────────────────────────────────────
function renderIndustryFqhc(ind) {
  const fqhc = ind.fqhc_comparison || {};
  const ratesData = fqhc.rates || {};
  const prodData = fqhc.productivity || {};
  const findings = fqhc.key_findings || [];
  if (!Object.keys(ratesData).length) return '';

  const catMap = _getCategoryMap(ind);
  // Merge rates + productivity per code
  const allCodes = [...new Set([...Object.keys(ratesData), ...Object.keys(prodData)])];
  const items = allCodes.map(code => ({
    code,
    rate: ratesData[code] || null,
    prod: prodData[code] || null,
  }));
  const grouped = _groupByCategory(items, catMap);

  let tableRows = '';
  for (const cat of CATEGORY_ORDER) {
    const rows = grouped[cat] || [];
    if (!rows.length) continue;
    const meta = CATEGORY_META[cat];
    tableRows += `<tr class="fs-cat-header"><td colspan="7"><span class="fs-badge ${meta.color}">${esc(meta.short)}</span></td></tr>`;
    tableRows += rows.map(d => {
      const r = d.rate;
      const p = d.prod;
      const rateCls = r ? (r.signal === 'green' ? 'green' : r.signal === 'red' ? 'red' : 'yellow') : '';
      const rateAdv = r?.cmhc_advantage_pct != null ? `${r.cmhc_advantage_pct > 0 ? '+' : ''}${r.cmhc_advantage_pct.toFixed(1)}%` : '—';
      const prodAdv = p?.cmhc_advantage_pct != null ? `${p.cmhc_advantage_pct > 0 ? '+' : ''}${p.cmhc_advantage_pct.toFixed(1)}%` : '—';
      const prodCls = p ? (p.cmhc_advantage_pct > 0 ? 'green' : 'red') : '';
      return `<tr>
        <td><strong>${esc(d.code)}</strong><br><span style="font-size:.7rem;color:var(--text-3)">${esc(_codeLabel(d.code))}</span></td>
        <td>${r ? fmt$(r.cmhc_p50) : '—'}</td>
        <td>${r?.fqhc_p50 != null ? fmt$(r.fqhc_p50) : '<span style="color:var(--text-3)">N/A</span>'}</td>
        <td>${r ? `<span class="fs-badge ${rateCls}">${rateAdv}</span>` : '—'}</td>
        <td>${p ? fmtN(p.cmhc_bpc) : '—'}</td>
        <td>${p ? fmtN(p.fqhc_bpc) : '—'}</td>
        <td>${p ? `<span class="fs-badge ${prodCls}">${prodAdv}</span>` : '—'}</td>
      </tr>`;
    }).join('');
  }

  return `
  <div class="fs-section" id="sec-industry-fqhc">
    <div class="fs-section-header">
      <span class="fs-section-tag yellow">PEER COMPARISON</span>
      <h2 class="fs-section-h">CMHC vs FQHC: Where Each Model Wins</h2>
    </div>

    <div class="fs-narrative">
      <p>FQHCs operate under Prospective Payment System (PPS) — a fixed encounter rate. They compete for the same Medicaid BH patients but with a fundamentally different funding model. This comparison clarifies <strong>where CMHCs have a structural advantage</strong> and where they don't.</p>
    </div>

    <div class="fs-insight" style="margin-bottom:1rem">
      <strong>💡 Aha:</strong> CMHCs win on BH-specific codes (therapy +70%, PSR +24%, intake +21%). FQHCs win on med mgmt (T1015: 2.7x higher due to PPS encounter rate). Don't try to out-FQHC the FQHCs — lean into BH specialization.
    </div>

    <div class="fs-table-wrap">
      <table class="fs-table" style="font-size:.8rem">
        <thead>
          <tr>
            <th rowspan="2">Code</th>
            <th colspan="3" style="text-align:center;border-bottom:1px solid var(--border)">Rate (PPC)</th>
            <th colspan="3" style="text-align:center;border-bottom:1px solid var(--border)">Panel (BPC)</th>
          </tr>
          <tr>
            <th>CMHC</th><th>FQHC</th><th>Δ</th>
            <th>CMHC</th><th>FQHC</th><th>Δ</th>
          </tr>
        </thead>
        <tbody>${tableRows}</tbody>
      </table>
    </div>

    ${findings.length ? `
    <div class="fs-card yellow" style="margin-top:1rem">
      <div class="fs-card-title">Key Findings</div>
      <div class="fs-card-body">
        <ul style="margin:0;padding-left:1.2rem">${findings.map(f => `<li>${esc(f)}</li>`).join('')}</ul>
      </div>
    </div>` : ''}

    ${fqhc.strategic_implication ? `
    <div class="fs-insight" style="margin-top:1rem">
      <strong>Strategic implication:</strong> ${esc(fqhc.strategic_implication)}
    </div>` : ''}
  </div>`;
}

// ── Section: Market Archetype Comparison ─────────────────────────────────────
function renderIndustryMarketArchetypes(ind) {
  const mkt = ind.market_archetype_comparison || {};
  const ratesPpc = mkt.rates_ppc || {};
  const prodBpc = mkt.productivity_bpc || {};
  const archetypes = mkt.archetypes || {};
  const findings = mkt.key_findings || [];
  if (!Object.keys(ratesPpc).length) return '';

  const tiers = ['dense', 'moderate', 'sparse'];
  const tierLabels = { dense: 'Dense', moderate: 'Mid-Size', sparse: 'Rural' };
  const catMap = _getCategoryMap(ind);

  // Merge rate and productivity data per code
  const allCodes = [...new Set([...Object.keys(ratesPpc), ...Object.keys(prodBpc)])];
  const items = allCodes.map(code => ({code, rate: ratesPpc[code], prod: prodBpc[code]}));
  const grouped = _groupByCategory(items, catMap);

  let tableRows = '';
  for (const cat of CATEGORY_ORDER) {
    const rows = grouped[cat] || [];
    if (!rows.length) continue;
    const meta = CATEGORY_META[cat];
    tableRows += `<tr class="fs-cat-header"><td colspan="8"><span class="fs-badge ${meta.color}">${esc(meta.short)}</span></td></tr>`;
    tableRows += rows.map(d => {
      const r = d.rate;
      const p = d.prod;
      return `<tr>
        <td><strong>${esc(d.code)}</strong><br><span style="font-size:.7rem;color:var(--text-3)">${esc(_codeLabel(d.code))}</span></td>
        ${tiers.map(t => {
          const rv = r ? r[t] : null;
          const pv = p ? p[t] : null;
          const rStr = rv != null ? fmt$(rv) : '';
          const pStr = pv != null ? `<span style="font-size:.7rem;color:var(--text-3)">${fmtN(pv)} bpc</span>` : '';
          return `<td>${rStr}${rStr && pStr ? '<br>' : ''}${pStr}</td>`;
        }).join('')}
        <td>${r ? `<span class="fs-badge ${r.spread_pct > 25 ? 'red' : r.spread_pct > 15 ? 'yellow' : 'green'}">${r.spread_pct}%</span>` : '—'}</td>
      </tr>`;
    }).join('');
  }

  const archetypeCards = Object.entries(archetypes).map(([key, a]) => {
    const cls = key === 'dense' ? 'blue' : key === 'moderate' ? 'green' : 'yellow';
    return `
      <div class="fs-card ${cls}">
        <div class="fs-card-title">${esc(a.label)}</div>
        <div style="font-size:.75rem;color:var(--text-3);margin-bottom:.5rem">${esc(a.examples)}</div>
        <div class="fs-card-body" style="font-size:.85rem">${esc(a.profile)}</div>
        <div style="margin-top:.5rem;font-size:.8rem"><strong>Key risk:</strong> ${esc(a.risk)}</div>
      </div>`;
  }).join('');

  return `
  <div class="fs-section" id="sec-industry-market-archetypes">
    <div class="fs-section-header">
      <span class="fs-section-tag green">MARKET GEOGRAPHY</span>
      <h2 class="fs-section-h">Performance by Market Archetype</h2>
    </div>

    <div class="fs-narrative">
      <p>Not all CMHCs face the same market dynamics. Dense metros, mid-size cities, and rural areas create fundamentally different competitive landscapes. Your market archetype contextualizes whether your rates and productivity are strong <em>for your market</em>.</p>
    </div>

    <div class="fs-insight" style="margin-bottom:1rem">
      <strong>💡 Aha:</strong> Therapy rates (H2019) are surprisingly stable across markets — only 7% spread. But care management (T1017) shows a 35% gap: dense-market providers collect $67 vs rural at $50. Rural CMHCs face a double squeeze: lower rates AND smaller panels.
    </div>

    <div class="fs-card-grid" style="grid-template-columns:repeat(3,1fr);margin-bottom:1.25rem">
      ${archetypeCards}
    </div>

    <h3 style="font-size:.9rem;margin:1rem 0 .5rem">Rate & Panel by Market Tier</h3>
    <div class="fs-table-wrap">
      <table class="fs-table" style="font-size:.8rem">
        <thead>
          <tr><th>Code</th>${tiers.map(t => `<th>${tierLabels[t]}</th>`).join('')}<th>Spread</th></tr>
        </thead>
        <tbody>${tableRows}</tbody>
      </table>
    </div>

    ${findings.length ? `
    <div class="fs-card green" style="margin-top:1rem">
      <div class="fs-card-title">Key Findings</div>
      <div class="fs-card-body">
        <ul style="margin:0;padding-left:1.2rem">${findings.map(f => `<li>${esc(f)}</li>`).join('')}</ul>
      </div>
    </div>` : ''}
  </div>`;
}

// ── Section: Overview ────────────────────────────────────────────────────────
function renderExecutiveSummary(prose, b) {
  return `
  <div class="fs-section" id="sec-exec-summary">
    <div class="fs-section-header">
      <span class="fs-section-tag blue">EXECUTIVE SUMMARY</span>
      <h2 class="fs-section-h">${esc(b.org_name)}</h2>
    </div>
    <div class="fs-card blue" style="font-size:1.05rem;line-height:1.7">
      <div class="fs-card-body">${esc(prose.executive_summary)}</div>
    </div>
    ${b.org_city ? `<div style="color:#64748b;font-size:.85rem;margin-top:.5rem">${esc(b.org_city)}, ${esc(b.org_state || 'FL')} · ${esc(b.org_type)} · ${esc(b.size_band || '')} · ${esc(b.market_tier || '')} market · Source: Live BQ data (2024)</div>` : ''}
  </div>`;
}

function renderRisksStrengths(prose) {
  const risks = prose.key_risks || [];
  const strengths = prose.key_strengths || [];
  let html = '<div class="fs-section" id="sec-risks-strengths"><div class="fs-card-grid" style="grid-template-columns:1fr 1fr">';
  if (risks.length) {
    html += `<div class="fs-card red"><div class="fs-card-title">Key Risks</div><div class="fs-card-body"><ul style="margin:0;padding-left:1.2rem">${risks.map(r => `<li>${esc(r)}</li>`).join('')}</ul></div></div>`;
  }
  if (strengths.length) {
    html += `<div class="fs-card green"><div class="fs-card-title">Key Strengths</div><div class="fs-card-body"><ul style="margin:0;padding-left:1.2rem">${strengths.map(s => `<li>${esc(s)}</li>`).join('')}</ul></div></div>`;
  }
  html += '</div></div>';
  return html;
}

function renderStrategicOutlook(prose) {
  return `
  <div class="fs-section" id="sec-strategic-outlook">
    <div class="fs-section-header">
      <span class="fs-section-tag green">STRATEGIC OUTLOOK</span>
      <h2 class="fs-section-h">Recommended Focus Areas</h2>
    </div>
    <div class="fs-card green" style="font-size:1rem;line-height:1.7">
      <div class="fs-card-body">${esc(prose.strategic_outlook)}</div>
    </div>
  </div>`;
}

function renderOverview(b) {
  const ind = b.industry || {};
  return `
  <div class="fs-section" id="sec-overview">
    <div class="fs-section-header">
      <span class="fs-section-tag blue">CHAPTER 2</span>
      <h2 class="fs-section-h">Your Position in the Market</h2>
    </div>

    <div class="fs-stat-grid">
      <div class="fs-stat-card"><div class="fs-stat-value">${fmt$(b.total_revenue)}</div><div class="fs-stat-label">Total Revenue</div></div>
      <div class="fs-stat-card"><div class="fs-stat-value">${fmtN(b.total_claims)}</div><div class="fs-stat-label">Total Claims</div></div>
      <div class="fs-stat-card"><div class="fs-stat-value">${b.service_line_count || '—'}</div><div class="fs-stat-label">Active Service Lines</div></div>
      <div class="fs-stat-card"><div class="fs-stat-value">${esc(b.org_type) || '—'}</div><div class="fs-stat-label">Classification</div></div>
    </div>

    <div class="fs-sector-ref">
      <strong>Sector Context:</strong> Florida's 86 CMHCs collectively face three structural challenges.
      The sector loses <strong>${esc(ind.sector_gap || '$8.1M')}</strong> annually to rate gaps,
      <strong>${esc(ind.leakage_gap || '$14.3M')}</strong> to post-crisis leakage (${esc(ind.ratio || '1.8x')} the rate impact),
      and clinician burnout driven by compressed capacity.
      <br><br>
      <strong>In this report, we don't benchmark you against the lagging sector — we benchmark you against the market.</strong>
      Rest-of-FL P75 is the standard: what best-in-class non-CMHC providers achieve. Where the CMHC sector falls below that, we show it for context — but it is not the target.
    </div>
  </div>`;
}

// ── Section: Rates ───────────────────────────────────────────────────────────
function renderRates(codes, ind, prose) {
  prose = prose || {};
  const rateCodes = codes.filter(c => c.org_rate != null && c.rest_p75 != null);

  const rows = rateCodes.map(c => `
    <tr>
      <td><strong>${esc(c.code)}</strong></td>
      <td>${esc(c.service)}</td>
      <td><span class="fs-badge ${signalClass(c.stage === 'intake' ? 'blue' : c.stage === 'high_acuity' ? 'yellow' : 'green')}">${esc(c.stage)}</span></td>
      <td>${fmt$(c.org_rate)}</td>
      <td><strong>${fmt$(c.rest_p75)}</strong></td>
      <td style="color:#94a3b8;font-size:.75rem">${fmt$(c.cmhc_p50)}</td>
      <td><span class="fs-badge ${signalClass(c.signal)}">${fmtPct(c.gap_pct)}</span></td>
      <td>${c.trend_arrow || ''} ${esc(c.trend_label || '')}</td>
    </tr>
  `).join('');

  const greens = rateCodes.filter(c => c.signal === 'green');
  const yellows = rateCodes.filter(c => c.signal === 'yellow');
  const reds = rateCodes.filter(c => c.signal === 'red');

  const cards = [];
  if (greens.length) cards.push(`<div class="fs-card green"><div class="fs-card-title">At or Above Best-in-Class</div><div class="fs-card-body">${greens.map(c => `<strong>${c.code}</strong> (${fmtPct(c.gap_pct)})`).join(', ')} — these codes meet or exceed Rest-of-FL P75. Protect these rates.</div></div>`);
  if (yellows.length) cards.push(`<div class="fs-card yellow"><div class="fs-card-title">Within Striking Distance</div><div class="fs-card-body">${yellows.map(c => `<strong>${c.code}</strong> (${fmtPct(c.gap_pct)})`).join(', ')} — within 20% of P75. What would close the remaining gap?</div></div>`);
  if (reds.length) cards.push(`<div class="fs-card red"><div class="fs-card-title">Significant Gap to Best-in-Class</div><div class="fs-card-body">${reds.map(c => `<strong>${c.code}</strong> (${fmtPct(c.gap_pct)})`).join(', ')} — 20%+ below P75. Investigate denial patterns, modifier usage, and contract terms.</div></div>`);

  return `
  <div class="fs-section" id="sec-rates">
    <div class="fs-section-header">
      <span class="fs-section-tag problem">RATES</span>
      <h2 class="fs-section-h">Your Rates vs Best-in-Class</h2>
    </div>

    <div class="fs-sector-ref">
      The CMHC sector collects less per claim than the market on most codes. Matching the CMHC median means matching underperformance.
      The benchmark below is <strong>Rest-of-FL P75</strong> — what best-in-class providers in the broader FL Medicaid market collect.
    </div>

    <div class="fs-table-wrap">
      <table class="fs-table">
        <thead><tr><th>Code</th><th>Service</th><th>Stage</th><th>Your Rate</th><th>Best-in-Class (P75)</th><th style="color:#94a3b8">CMHC P50</th><th>Gap</th><th>Trend</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </div>

    <div class="fs-card-grid">${cards.join('')}</div>

    <div class="fs-insight">
      ${prose.rate_analysis ? esc(prose.rate_analysis) : `<strong>Rate Position:</strong>
      ${greens.length} code${greens.length !== 1 ? 's' : ''} at or above P75,
      ${yellows.length} within striking distance,
      ${reds.length} with significant gaps.
      ${reds.length > greens.length ? 'The majority of service lines are below best-in-class — rate optimization is a priority.' : greens.length > reds.length ? 'More codes are at best-in-class than below — the rate position is relatively strong.' : 'A mixed rate picture — some strengths to protect, some gaps to investigate.'}`}
    </div>
  </div>`;
}

// ── Section: Engagement ──────────────────────────────────────────────────────
function renderEngagement(codes, b, prose) {
  prose = prose || {};
  const topByRevenue = [...codes].filter(c => c.revenue).sort((a, b) => (b.revenue || 0) - (a.revenue || 0)).slice(0, 3);
  const v = b.verdicts?.engagement || {};

  const rpbCards = topByRevenue.map(c => {
    const rpb = c.revenue_per_bene;
    return `
    <div class="fs-card ${signalClass(c.signal)}">
      <div class="fs-card-title">${esc(c.service)}</div>
      <div class="fs-card-big">${fmt$(rpb)}</div>
      <div class="fs-card-sub">Revenue per beneficiary</div>
      <div style="margin-top:.5rem;font-size:.75rem;color:var(--text-3)">${esc(c.code)} · ${fmtN(c.benes)} benes</div>
    </div>`;
  }).join('');

  const stages = {};
  for (const c of codes) {
    const s = c.stage || 'ongoing';
    if (!stages[s]) stages[s] = { revenue: 0, benes: 0 };
    stages[s].revenue += (c.revenue || 0);
    stages[s].benes += (c.benes || 0);
  }
  const totalRev = Object.values(stages).reduce((a, s) => a + s.revenue, 0);
  const stageRows = Object.entries(stages).map(([s, d]) => `
    <tr><td><span class="fs-badge ${s === 'intake' ? 'blue' : s === 'high_acuity' ? 'yellow' : 'green'}">${esc(s)}</span></td>
    <td>${fmt$(d.revenue)}</td><td>${totalRev ? (d.revenue / totalRev * 100).toFixed(1) + '%' : '—'}</td>
    <td>${fmtN(d.benes)}</td></tr>
  `).join('');

  return `
  <div class="fs-section" id="sec-engagement">
    <div class="fs-section-header">
      <span class="fs-section-tag problem">ENGAGEMENT</span>
      <h2 class="fs-section-h">Patient Engagement & Leakage</h2>
    </div>

    <div class="fs-sector-ref">
      CMHCs capture 15% of intake and 12% of ongoing care — but handle <strong>32% of crisis/ACT services</strong>.
      Post-crisis retention is the key lever: the $14.3M leakage gap adds to the $8.1M rate gap for a combined $22.4M sector opportunity.
    </div>

    <div class="fs-card-grid">${rpbCards}</div>

    <div class="fs-card-grid-2">
      <div class="fs-card blue">
        <div class="fs-card-title">Care Stage Mix</div>
        <table class="fs-table" style="border:none">
          <thead><tr><th>Stage</th><th>Revenue</th><th>Share</th><th>Benes</th></tr></thead>
          <tbody>${stageRows}</tbody>
        </table>
      </div>
      <div class="fs-card blue">
        <div class="fs-card-title">Engagement Signal</div>
        <div class="fs-card-body">
          Intake beneficiaries: <strong>${fmtN(v.intake_benes)}</strong><br>
          Ongoing beneficiaries: <strong>${fmtN(v.ongoing_benes)}</strong><br><br>
          ${v.ongoing_benes > v.intake_benes ? 'More patients in ongoing care than intake — suggests retention capacity.' : 'Fewer patients in ongoing care than intake — potential leakage at the care transition point.'}
        </div>
      </div>
    </div>

    <div class="fs-insight">
      ${prose.engagement_analysis ? esc(prose.engagement_analysis) : `<strong>Engagement Verdict:</strong> ${esc(v.label || '—')}.
      ${v.label === 'Opportunity' ? 'Strong ongoing volume relative to intake — the org retains patients well.' : v.label === 'Exposed' ? 'Significant drop-off from intake to ongoing care. Investigate care transition workflows.' : 'Moderate retention — some leakage likely occurring at the intake-to-ongoing transition.'}`}
    </div>
  </div>`;
}

// ── Section: Burnout ─────────────────────────────────────────────────────────
function renderBurnout(codes, b, prose) {
  prose = prose || {};
  const panelCodes = codes.filter(c => c.panel != null && c.panel > 0);
  const v = b.verdicts?.burnout || {};

  const rows = panelCodes.map(c => `
    <tr>
      <td><strong>${esc(c.code)}</strong></td>
      <td>${esc(c.service)}</td>
      <td>${fmtN(c.panel)}</td>
      <td>${fmtN(c.clinicians)}</td>
      <td>${fmtN(c.benes)}</td>
    </tr>
  `).join('');

  return `
  <div class="fs-section" id="sec-burnout">
    <div class="fs-section-header">
      <span class="fs-section-tag problem">CAPACITY</span>
      <h2 class="fs-section-h">Clinician Capacity & Burnout</h2>
    </div>

    <div class="fs-sector-ref">
      <strong>This is an outcome of Problems 1 and 2.</strong>
      Lower rates force higher volume. Lower engagement means constant new-patient acquisition.
      Both compress clinician capacity and contribute to burnout and turnover.
    </div>

    ${panelCodes.length ? `
    <div class="fs-table-wrap">
      <table class="fs-table">
        <thead><tr><th>Code</th><th>Service</th><th>Panel Size</th><th>Clinicians</th><th>Benes</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </div>
    ` : '<div style="padding:1rem;color:var(--text-3);font-size:.875rem">Panel data not available for this organization.</div>'}

    <div class="fs-insight">
      ${prose.capacity_analysis ? esc(prose.capacity_analysis) : `<strong>Burnout Verdict:</strong> ${esc(v.label || '—')} (max panel ratio: ${v.max_panel_ratio || '—'}x market).
      ${v.label === 'Critical' ? 'At least one service line has panels significantly above market with very few clinicians — concentration and burnout risk is acute.' : v.label === 'Concentrated' ? 'Some service lines have elevated panels — monitor clinician workload and retention.' : 'Panels are generally at or below market levels — capacity risk is manageable.'}`}
    </div>
  </div>`;
}

// ── Section: Verdicts ────────────────────────────────────────────────────────
function renderVerdicts(v) {
  const cards = [
    { label: 'Rates', verdict: v.rates?.label || '—', detail: `${v.rates?.codes_above || 0} above P75, ${v.rates?.codes_near || 0} near, ${v.rates?.codes_below || 0} below` },
    { label: 'Engagement', verdict: v.engagement?.label || '—', detail: `${fmtN(v.engagement?.intake_benes)} intake, ${fmtN(v.engagement?.ongoing_benes)} ongoing` },
    { label: 'Capacity', verdict: v.burnout?.label || '—', detail: `Max panel ratio: ${v.burnout?.max_panel_ratio || '—'}x` },
  ];

  return `
  <div class="fs-section" id="sec-verdicts">
    <div class="fs-section-header">
      <span class="fs-section-tag blue">VERDICT</span>
      <h2 class="fs-section-h">Summary Assessment</h2>
    </div>

    <div class="fs-verdict-grid">
      ${cards.map(c => `
        <div class="fs-verdict ${verdictColor(c.verdict)}">
          <div class="fs-verdict-label">${esc(c.label)}</div>
          <div class="fs-verdict-value">${esc(c.verdict)}</div>
          <div class="fs-verdict-detail">${esc(c.detail)}</div>
        </div>
      `).join('')}
    </div>
  </div>`;
}

// ── Section: Priorities ──────────────────────────────────────────────────────
function renderPriorities(priorities) {
  if (!priorities.length) return '';

  const cards = priorities.map(p => {
    const cls = p.signal === 'investigate' ? 'red' : p.signal === 'monitor' ? 'yellow' : 'green';
    const label = p.signal === 'investigate' ? 'Investigate' : p.signal === 'monitor' ? 'Monitor' : 'Strength';
    return `
    <div class="fs-card ${cls}">
      <div class="fs-card-title">${esc(p.code)} — ${esc(p.service)}</div>
      <div style="margin:.25rem 0"><span class="fs-badge ${cls}">${label}</span></div>
      <div class="fs-card-body">${esc(p.reason)}</div>
    </div>`;
  }).join('');

  return `
  <div class="fs-section" id="sec-priorities">
    <div class="fs-section-header">
      <span class="fs-section-tag blue">NEXT STEPS</span>
      <h2 class="fs-section-h">Investigation Priorities</h2>
    </div>

    <div class="fs-card-grid-2">${cards}</div>

    <div style="margin-top:1rem;text-align:center">
      <button class="btn-primary" onclick="generatePlan()" style="font-size:.875rem;padding:.6rem 1.5rem">
        Generate Strategy Plan &rarr;
      </button>
    </div>
  </div>`;
}

// ── Navigation ───────────────────────────────────────────────────────────────
function navigateTo(section) {
  const target = document.getElementById('sec-' + section);
  if (target) target.scrollIntoView({ behavior: 'smooth', block: 'start' });

  document.querySelectorAll('.fs-nav-item').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.section === section);
  });
}

// ── Task accumulation ────────────────────────────────────────────────────────
function addTask(task) {
  // Deduplicate by title
  if (_tasks.some(t => t.title === task.title)) return;
  _tasks.push(task);
  renderTaskList();
}

function renderTaskList() {
  const el = document.getElementById('fsTaskList');
  let html = '';

  // Bookmarks section
  if (_bookmarks.length) {
    html += '<div class="fs-task-group-label">Bookmarks</div>';
    html += _bookmarks.map((b, i) => `
      <div class="fs-task-card bookmark">
        <div class="fs-task-card-title">
          <svg width="10" height="10" viewBox="0 0 24 24" fill="currentColor" stroke="none" style="flex-shrink:0;color:var(--amber)"><path d="M19 21l-7-5-7 5V5a2 2 0 012-2h10a2 2 0 012 2z"/></svg>
          ${esc(b.text.substring(0, 80))}${b.text.length > 80 ? '…' : ''}
        </div>
        <div class="fs-task-card-meta">${esc(b.section)} <button class="fs-task-remove" onclick="_removeBookmark(${i})">×</button></div>
      </div>
    `).join('');
  }

  // Tasks section
  if (_tasks.length) {
    html += '<div class="fs-task-group-label">Strategy Tasks</div>';
    html += _tasks.map((t, i) => `
      <div class="fs-task-card">
        <div class="fs-task-card-title"><span class="fs-task-severity ${t.severity || 'medium'}"></span>${esc(t.title)}</div>
        <div class="fs-task-card-meta">${esc(t.severity || 'medium')} · ${(t.tags || []).join(', ')} <button class="fs-task-remove" onclick="_removeTask(${i})">×</button></div>
      </div>
    `).join('');
  }

  if (!html) {
    html = '<div style="padding:.75rem;font-size:.75rem;color:var(--text-3);text-align:center">Ask questions or generate a plan to build tasks</div>';
  }
  el.innerHTML = html;
}

// ── Bookmark: annotate/save selected text ──
function _addBookmark(text, section) {
  if (_bookmarks.some(b => b.text === text)) return; // deduplicate
  const bookmark = { text, section, ts: new Date().toISOString() };
  _bookmarks.push(bookmark);
  document.getElementById('fsTaskSection').style.display = 'block';
  renderTaskList();
  _showToast('Bookmarked');
  // Persist to backend
  if (_versionId) {
    fetch(`${API}/chat/financial-strategy/bookmark`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ version_id: _versionId, bookmark }),
    }).catch(() => {});
  }
}

function _removeBookmark(idx) {
  const removed = _bookmarks.splice(idx, 1)[0];
  renderTaskList();
  // Persist removal
  if (_versionId && removed) {
    fetch(`${API}/chat/financial-strategy/bookmark/remove`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ version_id: _versionId, text: removed.text }),
    }).catch(() => {});
  }
}

// ── Add to Plan: create a strategy task from selected text ──
function _addToPlan(text, section) {
  const title = text.length > 100 ? text.substring(0, 97) + '…' : text;
  addTask({
    title: `Investigate: ${title}`,
    severity: 'medium',
    tags: [section],
    source: 'selection',
    text: text,
  });
  document.getElementById('fsTaskSection').style.display = 'block';
  _showToast('Added to plan');
}

function _removeTask(idx) {
  _tasks.splice(idx, 1);
  renderTaskList();
}

// ── Toast notification ──
function _showToast(msg) {
  let toast = document.getElementById('fsToast');
  if (!toast) {
    toast = document.createElement('div');
    toast.id = 'fsToast';
    toast.className = 'fs-toast';
    document.body.appendChild(toast);
  }
  toast.textContent = msg;
  toast.classList.add('show');
  setTimeout(() => toast.classList.remove('show'), 2000);
}

// ── Generate Plan (bulk tasks from baseline) ────────────────────────────────
async function generatePlan() {
  if (!_currentOrg) return;
  const taskList = document.getElementById('fsTaskList');
  taskList.innerHTML = '<div class="fs-loading"><span class="spinner"></span> Generating strategy plan...</div>';

  try {
    const r = await fetch(`${API}/chat/financial-strategy/generate-plan`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ org_name: _currentOrg, auto_import: false }),
    });
    if (!r.ok) throw new Error('Failed to generate plan');
    const data = await r.json();
    const tasks = data.tasks || [];

    // Merge into accumulated tasks
    for (const t of tasks) {
      addTask(t);
    }

    // Also show in chat
    const msgs = document.getElementById('chatMessages');
    let summary = `<strong>Strategy plan generated</strong> for ${esc(data.org_name)}.<br>`;
    summary += `${data.count} tasks created from baseline analysis.<br><br>`;
    const v = data.verdicts || {};
    if (v.rates) summary += `<strong>Rates:</strong> ${esc(v.rates.label)} — ${v.rates.codes_below} codes below P75<br>`;
    if (v.engagement) summary += `<strong>Engagement:</strong> ${esc(v.engagement.label)}<br>`;
    if (v.burnout) summary += `<strong>Capacity:</strong> ${esc(v.burnout.label)} (${v.burnout.max_panel_ratio}x)<br>`;
    summary += `<br>Tasks are in the sidebar. Ask follow-up questions to refine the plan.`;
    msgs.innerHTML += `<div class="fs-chat-msg system">${summary}</div>`;
    msgs.scrollTop = msgs.scrollHeight;

    // Open chat to show confirmation
    chatExpand();
  } catch (e) {
    taskList.innerHTML = `<div style="padding:.75rem;font-size:.75rem;color:var(--red)">${esc(e.message)}</div>`;
  }
}

// ── Chat ─────────────────────────────────────────────────────────────────────
function chatExpand() {
  document.getElementById('chatPane').classList.add('open');
  document.getElementById('chatFab').classList.add('hidden');
  document.getElementById('chatInput').focus();
}
function chatCollapse() {
  document.getElementById('chatPane').classList.remove('open');
  document.getElementById('chatFab').classList.remove('hidden');
}

// ── Chat resize ──
(function initChatResize() {
  const handle = document.getElementById('chatResize');
  const pane = document.getElementById('chatPane');
  if (!handle || !pane) return;

  let startX, startY, startW, startH;

  handle.addEventListener('mousedown', (e) => {
    e.preventDefault();
    startX = e.clientX;
    startY = e.clientY;
    startW = pane.offsetWidth;
    startH = pane.offsetHeight;
    pane.classList.add('resizing');

    function onMove(e) {
      // Dragging top-left: moving left = wider, moving up = taller
      const dw = startX - e.clientX;
      const dh = startY - e.clientY;
      pane.style.width = Math.max(300, startW + dw) + 'px';
      pane.style.height = Math.max(280, startH + dh) + 'px';
    }
    function onUp() {
      pane.classList.remove('resizing');
      document.removeEventListener('mousemove', onMove);
      document.removeEventListener('mouseup', onUp);
    }
    document.addEventListener('mousemove', onMove);
    document.addEventListener('mouseup', onUp);
  });
})();

let _chatIntent = 'general';  // current intent: general, explain, dispute, plan

async function sendQuestion() {
  const input = document.getElementById('chatInput');
  const q = input.value.trim();
  if (!q || !_currentOrg) return;
  input.value = '';
  // Detect intent from toolbar or auto-detect from question text
  let intent = _chatIntent;
  _chatIntent = 'general';  // reset after use
  if (intent === 'general') {
    const ql = q.toLowerCase();
    if (ql.startsWith('explain') || ql.includes('how is') || ql.includes('how do you') || ql.includes('where does') || ql.includes('show me the math') || ql.includes('break down')) {
      intent = 'explain';
    } else if (ql.startsWith('i disagree') || ql.startsWith('i challenge') || ql.startsWith('that') && (ql.includes("wrong") || ql.includes("incorrect")) || ql.includes("doesn't account") || ql.includes("what about") || ql.includes("but ")) {
      intent = 'dispute';
    } else if (ql.includes('what should') || ql.includes('action') || ql.includes('next step') || ql.includes('plan') || ql.includes('priorit') || ql.includes('recommend')) {
      intent = 'plan';
    }
  }

  const msgs = document.getElementById('chatMessages');
  msgs.innerHTML += `<div class="fs-chat-msg user">${esc(q)}</div>`;

  // Create a working indicator + cards container
  const workingId = 'working_' + Date.now();
  const cardsId = 'cards_' + Date.now();
  const answerId = 'answer_' + Date.now();
  msgs.innerHTML += `<div class="fs-chat-working" id="${workingId}"><span class="spinner-sm"></span> <span class="fs-chat-working-text">Thinking…</span></div>`;
  msgs.innerHTML += `<div class="fs-chat-cards" id="${cardsId}"></div>`;
  msgs.scrollTop = msgs.scrollHeight;

  try {
    // POST to get correlation_id, then stream via SSE
    const r = await fetch(`${API}/chat/financial-strategy/ask`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        org_name: _currentOrg,
        question: q,
        intent: intent,
        stream: true,
        version_id: _versionId,
        thread_id: _threadId,
        baseline_context: _baseline ? {
          code_analysis: _baseline.code_analysis,
          verdicts: _baseline.verdicts,
          priorities: _baseline.priorities,
          total_revenue: _baseline.total_revenue,
          total_claims: _baseline.total_claims,
          panel_size: _baseline.panel_size,
          clinicians: _baseline.clinicians,
          clinician_mix: _baseline.clinician_mix,
          industry: _baseline.industry,
          org_city: _baseline.org_city,
          market_tier: _baseline.market_tier,
          size_band: _baseline.size_band,
        } : null,
      }),
    });
    if (!r.ok) throw new Error('Failed');
    const { correlation_id, stream_url } = await r.json();

    // Create streaming answer element (hidden until tokens arrive)
    const streamId = 'stream_' + Date.now();
    msgs.innerHTML += `<div class="fs-chat-msg system fs-chat-streaming" id="${streamId}" style="display:none"></div>`;
    let streamedText = '';

    // Open SSE stream
    const data = await new Promise((resolve, reject) => {
      const es = new EventSource(`${API}${stream_url}`);
      const cards = {};

      es.onmessage = (e) => {
        let parsed;
        try { parsed = JSON.parse(e.data); } catch { return; }

        const evType = parsed.event;
        const evData = parsed.data;

        if (evType === 'thinking') {
          const workingEl = document.getElementById(workingId);
          if (workingEl) {
            const stage = evData.stage || '';
            const line = evData.line || 'Thinking…';

            if (stage === 'reasoning') {
              let logEl = workingEl.querySelector('.fs-chat-reasoning-log');
              if (!logEl) {
                logEl = document.createElement('div');
                logEl.className = 'fs-chat-reasoning-log';
                logEl.innerHTML = '<div class="fs-reasoning-header" onclick="this.parentElement.classList.toggle(\'collapsed\')"><span class="spinner-sm"></span> <span>Reasoning…</span> <span class="fs-reasoning-toggle">▾</span></div><div class="fs-reasoning-entries"></div>';
                workingEl.appendChild(logEl);
              }
              const entries = logEl.querySelector('.fs-reasoning-entries');
              const entry = document.createElement('div');
              entry.className = 'fs-reasoning-entry';
              entry.textContent = line;
              entries.appendChild(entry);
              const headerText = logEl.querySelector('.fs-reasoning-header span:nth-child(2)');
              if (headerText) headerText.textContent = `Reasoning (${entries.children.length} steps)…`;
            } else {
              const textEl = workingEl.querySelector('.fs-chat-working-text');
              if (textEl) textEl.textContent = line;
            }
          }
          msgs.scrollTop = msgs.scrollHeight;
        }
        else if (evType === 'card') {
          const card = evData.card;
          const stepId = card.step_id || ('card_' + Date.now());
          cards[stepId] = card;
          _renderStreamCards(cardsId, cards);
          msgs.scrollTop = msgs.scrollHeight;
        }
        else if (evType === 'message') {
          // Live token streaming — show answer as it generates
          const chunk = evData.chunk || '';
          streamedText += chunk;
          const streamEl = document.getElementById(streamId);
          if (streamEl) {
            streamEl.style.display = '';
            // Hide working indicator once streaming starts
            const workingEl = document.getElementById(workingId);
            if (workingEl) workingEl.style.display = 'none';
            // Render markdown live
            streamEl.innerHTML = (typeof renderMarkdown === 'function')
              ? renderMarkdown(streamedText) + '<span class="fs-cursor"></span>'
              : esc(streamedText) + '<span class="fs-cursor"></span>';
            msgs.scrollTop = msgs.scrollHeight;
          }
        }
        else if (evType === 'completed') {
          es.close();
          fetch(`${API}/chat/financial-strategy/response/${correlation_id}`)
            .then(r => r.json())
            .then(fullData => resolve(fullData))
            .catch(() => resolve(evData));
        }
        else if (evType === 'error') {
          es.close();
          reject(new Error(evData.message || 'Stream error'));
        }
      };

      es.onerror = () => {
        es.close();
        _pollForResult(correlation_id).then(resolve).catch(reject);
      };
    });

    // Remove working indicator
    const workingEl = document.getElementById(workingId);
    if (workingEl) workingEl.remove();

    // Remove the streaming element (will be replaced by final render)
    const streamEl = document.getElementById(streamId);
    if (streamEl) streamEl.remove();

    // Check for backend error
    if (data.error && !data.answer) {
      const errMsg = data.error.includes('overloaded') || data.error.includes('529')
        ? 'The AI service is temporarily busy. Please try again in a moment.'
        : `Something went wrong: ${data.error}`;
      msgs.innerHTML += `<div class="fs-chat-msg system" style="color:var(--red)">${esc(errMsg)}</div>`;
    } else {
      const reply = _buildFinalReply(data);
      msgs.innerHTML += `<div class="fs-chat-msg system">${reply}</div>`;
    }

  } catch (e) {
    const workingEl = document.getElementById(workingId);
    if (workingEl) workingEl.remove();
    msgs.innerHTML += `<div class="fs-chat-msg system" style="color:var(--red)">Error: ${esc(e.message)}</div>`;
  }
  msgs.scrollTop = msgs.scrollHeight;
}

function _renderStreamCards(containerId, cards) {
  const el = document.getElementById(containerId);
  if (!el) return;
  const entries = Object.entries(cards);
  let html = '';
  for (let i = 0; i < entries.length; i++) {
    const [stepId, card] = entries[i];
    const isLast = i === entries.length - 1;
    const isRunning = card.status === 'running';
    const icon = isRunning ? '<span class="spinner-sm"></span>' : '<span class="fs-card-check">&#10003;</span>';

    if (isLast) {
      // Latest card — expanded with body preview
      html += `<div class="fs-stream-card ${card.status} expanded">
        ${icon}
        <span class="fs-stream-card-title">${esc(card.title || '')}</span>
      </div>`;
    } else {
      // Previous cards — collapsed to single line
      html += `<div class="fs-stream-card ${card.status} collapsed">
        ${icon}
        <span class="fs-stream-card-title">${esc(card.title || '')}</span>
      </div>`;
    }
  }
  el.innerHTML = html;
}

async function _pollForResult(cid, maxWait = 120000) {
  const start = Date.now();
  while (Date.now() - start < maxWait) {
    const r = await fetch(`${API}/chat/financial-strategy/response/${cid}`);
    const data = await r.json();
    if (data.status === 'completed') return data;
    await new Promise(ok => setTimeout(ok, 1000));
  }
  throw new Error('Timeout waiting for response');
}

function _buildFinalReply(data) {
  let reply = '';

  // Main answer (render as markdown if available)
  if (data.answer) {
    reply += renderMarkdown ? renderMarkdown(data.answer) : `<p>${esc(data.answer)}</p>`;
  }

  // Supporting data codes
  const relevant = data.relevant_codes || [];
  if (relevant.length) {
    reply += '<div class="fs-chat-codes">';
    for (const c of relevant.slice(0, 4)) {
      const sig = signalClass(c.signal);
      reply += `<span class="fs-badge ${sig}" style="margin-right:.25rem">${esc(c.code)}</span> `;
      reply += `${esc(c.service)}: ${fmt$(c.org_rate)} vs P75 ${fmt$(c.rest_p75)}`;
      if (c.gap_pct != null) reply += ` (${fmtPct(c.gap_pct)})`;
      reply += `<br>`;
    }
    reply += '</div>';
  }

  // BQ indicator
  if (data.needs_bq) {
    const hasData = data.bq_data;
    reply += `<div class="fs-chat-bq-badge ${hasData ? 'has-data' : ''}">
      <strong>${hasData ? '&#10003; Live BQ data' : 'Live data available'}:</strong> ${esc(data.bq_description || 'BigQuery query')}
      ${data.bq_query_type ? ' &middot; <code>' + esc(data.bq_query_type) + '</code>' : ''}
    </div>`;
  }

  // React agent metadata
  if (data.react) {
    const tools = (data.react_tools || []).map(t => t.tool);
    const unique = [...new Set(tools)];
    reply += `<div class="fs-chat-react-meta">
      ReAct agent &middot; ${data.react_rounds || 0} rounds &middot; ${unique.join(', ')}
    </div>`;
  } else if (data.llm_reframed) {
    reply += `<div class="fs-chat-react-meta">Refined by Claude</div>`;
  }

  // Strategy nudge + task suggestion
  if (data.suggested_task) {
    const taskId = 'task_' + Date.now();
    _pendingTasks[taskId] = data.suggested_task;
    if (data.strategy_nudge) {
      reply += `<div class="fs-chat-strategy">${esc(data.strategy_nudge)}</div>`;
    }
    reply += `<button class="fs-chat-task-btn" id="${taskId}" onclick="addTaskFromChat('${taskId}')">+ Add to strategy plan</button>`;
  }

  return reply;
}

function addTaskFromChat(btnId) {
  const task = _pendingTasks[btnId];
  if (!task) return;
  addTask(task);
  delete _pendingTasks[btnId];
  const btn = document.getElementById(btnId);
  if (btn) {
    btn.classList.add('added');
    btn.textContent = '\u2713 Added to plan';
    btn.onclick = null;
  }
}

// ── Selection → Chat bridge ──────────────────────────────────────────────────
(function initSelectionToolbar() {
  const toolbar = document.getElementById('selToolbar');
  if (!toolbar) return;

  let _selText = '';
  let _selSection = '';

  // Detect which section the selection is in
  function getSection(node) {
    let el = node.nodeType === 3 ? node.parentElement : node;
    while (el && el !== document.body) {
      // Check for section header nearby
      const header = el.querySelector && el.querySelector('.fs-section-h, .fs-ch-title');
      if (header) return header.textContent.trim();
      // Check for nav-linked sections
      if (el.id && el.id.startsWith('section-')) return el.id.replace('section-', '').replace(/-/g, ' ');
      el = el.parentElement;
    }
    return 'Report';
  }

  // Build prompt based on action
  function buildPrompt(action, text, section) {
    const ctx = `[Section: ${section}]`;
    switch (action) {
      case 'explain':
        return `${ctx} Explain this — show me the arithmetic, the data source, and what assumptions it rests on: "${text}"`;
      case 'dispute':
        return `${ctx} I want to challenge this: "${text}"`;
      case 'plan':
        return `${ctx} Based on this, what should we investigate or act on? "${text}"`;
      case 'ask':
        return `${ctx} Regarding: "${text}" — `;
      default:
        return text;
    }
  }

  // Show toolbar on text selection within the content area
  document.addEventListener('mouseup', function(e) {
    if (toolbar.contains(e.target)) return;

    setTimeout(() => {
      const sel = window.getSelection();
      const text = sel.toString().trim();

      if (text.length < 10) {
        toolbar.style.display = 'none';
        return;
      }

      // Only trigger in the main content area, not in chat drawer
      const anchor = sel.anchorNode;
      if (anchor) {
        let p = anchor.nodeType === 3 ? anchor.parentElement : anchor;
        while (p) {
          if (p.id === 'chatDrawer' || p.id === 'chatPane') {
            toolbar.style.display = 'none';
            return;
          }
          p = p.parentElement;
        }
      }

      const range = sel.getRangeAt(0);
      const rect = range.getBoundingClientRect();

      toolbar.style.display = 'block';
      toolbar.style.left = Math.max(8, rect.left + window.scrollX + (rect.width / 2) - (toolbar.offsetWidth / 2)) + 'px';
      toolbar.style.top = (rect.top + window.scrollY - toolbar.offsetHeight - 8) + 'px';

      _selText = text;
      _selSection = getSection(range.startContainer);
    }, 10);
  });

  // Hide on click elsewhere
  document.addEventListener('mousedown', function(e) {
    if (!toolbar.contains(e.target)) toolbar.style.display = 'none';
  });

  // Hide on scroll
  window.addEventListener('scroll', function() { toolbar.style.display = 'none'; }, { passive: true });

  // Button clicks → handle action
  toolbar.addEventListener('click', function(e) {
    const btn = e.target.closest('.sel-btn');
    if (!btn) return;

    const action = btn.dataset.action;

    // ── Bookmark: save selected text as an annotation ──
    if (action === 'bookmark') {
      _addBookmark(_selText, _selSection);
      toolbar.style.display = 'none';
      window.getSelection().removeAllRanges();
      return;
    }

    // ── Add to Plan: create a strategy task from selected text ──
    if (action === 'plan') {
      _addToPlan(_selText, _selSection);
      toolbar.style.display = 'none';
      window.getSelection().removeAllRanges();
      return;
    }

    // ── Chat actions: explain, dispute, ask ──
    const prompt = buildPrompt(action, _selText, _selSection);
    _chatIntent = action === 'ask' ? 'general' : action;

    chatExpand();
    const input = document.getElementById('chatInput');
    input.value = prompt;

    if (action === 'ask') {
      input.focus();
    } else {
      sendQuestion();
    }

    toolbar.style.display = 'none';
    window.getSelection().removeAllRanges();
  });
})();

// ── Onboarding Tour ─────────────────────────────────────────────────────────
const TOUR_STEPS = [
  {
    target: '#fsNavList',
    title: 'Navigate the Report',
    body: 'Jump between <strong>Industry Analysis</strong> (Chapter 1) and <strong>Your Org</strong> (Chapter 2). Each section is a self-contained finding you can investigate.',
    position: 'right',
    padX: 4, padY: 4,
  },
  {
    target: '#industryReportContainer',
    title: 'Select Any Text',
    body: 'Highlight any claim, number, or paragraph in the report. A toolbar appears with three actions: <strong>Explain</strong> the math, <strong>Dispute</strong> the finding, or <strong>Ask</strong> a follow-up.',
    position: 'top',
    padX: 20, padY: 10,
    maxHeight: 200,
  },
  {
    target: '#chatFab',
    title: 'Ask Anything',
    body: 'Open the analyst chat to ask freeform questions. The agent has <strong>full access to your data</strong>, runs BigQuery in real time, and will show its arithmetic.',
    position: 'left',
    padX: 4, padY: 4,
  },
  {
    target: '#fsTaskSection',
    title: 'Build Your Playbook',
    body: 'Click <strong>Generate Plan</strong> to create strategy tasks — rate negotiations, engagement campaigns, capacity actions. Track progress as you execute.',
    position: 'right',
    padX: 4, padY: 4,
  },
  {
    target: '.fs-download-wrap',
    title: 'Export & Share',
    body: 'Download as <strong>PDF</strong> — industry report, org report, or both. Ready to share with your board, MCOs, or AHCA.',
    position: 'bottom',
    padX: 4, padY: 4,
  },
];

function _tourGetRect(step) {
  const sel = step.target;
  let el = document.querySelector(sel);
  if (!el || el.offsetHeight === 0) {
    if (step.fallbackTarget) el = document.querySelector(step.fallbackTarget);
  }
  if (!el || el.offsetHeight === 0) return null;
  const r = el.getBoundingClientRect();
  const px = step.padX || 8;
  const py = step.padY || 8;
  let h = r.height + py * 2;
  if (step.maxHeight && h > step.maxHeight) h = step.maxHeight;
  return {
    left: r.left - px,
    top: r.top - py,
    width: r.width + px * 2,
    height: h,
  };
}

let _tourRunning = false;

function startTour() {
  if (localStorage.getItem('fs_tour_done')) return;
  if (_tourRunning) return;
  _tourRunning = true;

  let current = 0;

  // Create DOM
  const overlay = document.createElement('div');
  overlay.className = 'tour-overlay';
  const spotlight = document.createElement('div');
  spotlight.className = 'tour-spotlight';
  const tooltip = document.createElement('div');
  tooltip.className = 'tour-tooltip';

  document.body.appendChild(overlay);
  document.body.appendChild(spotlight);
  document.body.appendChild(tooltip);

  function renderStep(idx) {
    const step = TOUR_STEPS[idx];
    const rect = _tourGetRect(step);
    if (!rect) {
      // Skip this step if target not visible
      if (idx < TOUR_STEPS.length - 1) { renderStep(idx + 1); current = idx + 1; }
      else endTour();
      return;
    }

    // Position spotlight
    spotlight.style.left = rect.left + 'px';
    spotlight.style.top = rect.top + 'px';
    spotlight.style.width = rect.width + 'px';
    spotlight.style.height = rect.height + 'px';

    // Dots
    const dots = TOUR_STEPS.map((_, i) => {
      const cls = i < idx ? 'done' : i === idx ? 'active' : '';
      return `<div class="tour-dot ${cls}"></div>`;
    }).join('');

    // Buttons
    const isLast = idx === TOUR_STEPS.length - 1;
    const nextBtn = isLast
      ? `<button class="tour-btn tour-btn-done" onclick="endTour()">Got it</button>`
      : `<button class="tour-btn tour-btn-next" onclick="tourNext()">Next</button>`;

    tooltip.innerHTML = `
      <div class="tour-tooltip-title">${step.title}</div>
      <div class="tour-tooltip-body">${step.body}</div>
      <div class="tour-tooltip-footer">
        <div style="display:flex;align-items:center;gap:.75rem">
          <button class="tour-btn tour-btn-skip" onclick="endTour()">Skip</button>
          <div class="tour-dots">${dots}</div>
        </div>
        <div style="display:flex;align-items:center;gap:.5rem">
          <span class="tour-step-counter">${idx + 1}/${TOUR_STEPS.length}</span>
          ${nextBtn}
        </div>
      </div>
    `;

    // Position tooltip relative to spotlight
    requestAnimationFrame(() => {
      const tw = tooltip.offsetWidth;
      const th = tooltip.offsetHeight;
      const gap = 16;
      let tx, ty;

      switch (step.position) {
        case 'right':
          tx = rect.left + rect.width + gap;
          ty = rect.top + rect.height / 2 - th / 2;
          break;
        case 'left':
          tx = rect.left - tw - gap;
          ty = rect.top + rect.height / 2 - th / 2;
          break;
        case 'bottom':
          tx = rect.left + rect.width / 2 - tw / 2;
          ty = rect.top + rect.height + gap;
          break;
        case 'top':
          tx = rect.left + rect.width / 2 - tw / 2;
          ty = rect.top - th - gap;
          break;
      }

      // Clamp to viewport
      tx = Math.max(12, Math.min(tx, window.innerWidth - tw - 12));
      ty = Math.max(12, Math.min(ty, window.innerHeight - th - 12));

      tooltip.style.left = tx + 'px';
      tooltip.style.top = ty + 'px';
    });
  }

  window.tourNext = function() {
    current++;
    if (current >= TOUR_STEPS.length) { endTour(); return; }
    renderStep(current);
  };

  window.endTour = function() {
    _tourRunning = false;
    localStorage.setItem('fs_tour_done', '1');
    overlay.classList.add('fade-out');
    spotlight.style.opacity = '0';
    tooltip.style.opacity = '0';
    setTimeout(() => {
      overlay.remove();
      spotlight.remove();
      tooltip.remove();
    }, 350);
  };

  // Click overlay to skip
  overlay.addEventListener('click', endTour);

  renderStep(0);
}

// ── Version History ─────────────────────────────────────────────────────────

async function toggleVersionHistory() {
  const drawer = document.getElementById('versionHistoryDrawer');
  if (!drawer) return;
  if (drawer.classList.contains('vh-open')) {
    drawer.classList.remove('vh-open');
    return;
  }
  drawer.classList.add('vh-open');
  // Close on backdrop click (only the overlay itself, not children)
  drawer.onmousedown = (e) => { if (e.target === drawer) toggleVersionHistory(); };
  await _loadVersionHistory();
}

async function _loadVersionHistory() {
  const body = document.getElementById('versionHistoryBody');
  if (!body) return;
  body.innerHTML = '<div class="vh-loading"><span class="spinner"></span> Loading versions...</div>';

  const q = _documentId ? `document_id=${_documentId}` : `org=${encodeURIComponent(_currentOrg)}`;
  try {
    const r = await fetch(`${API}/chat/financial-strategy/version-history?${q}`);
    if (!r.ok) throw new Error('fetch failed');
    const data = await r.json();
    const versions = data.versions || [];
    if (!versions.length) {
      body.innerHTML = '<div class="vh-empty">No versions yet.<br>Generate a baseline to create the first version.</div>';
      return;
    }
    // Action bar: Finalize current or create new version
    const current = versions.find(v => v.version_id === _versionId);
    let actionsHtml = '';
    if (current && (current.status === 'draft' || current.status === 'active')) {
      actionsHtml = `<div class="vh-actions">
        <button class="vh-action-btn vh-finalize" onclick="event.stopPropagation(); finalizeCurrentVersion()">Finalize V${current.version_num}</button>
        <span class="vh-action-hint">Lock this version and add notes</span>
      </div>`;
    } else if (current && current.status === 'finalized') {
      actionsHtml = `<div class="vh-actions">
        <button class="vh-action-btn vh-new-version" onclick="event.stopPropagation(); createNewVersion()">New Version</button>
        <span class="vh-action-hint">Rebuild report incorporating your feedback</span>
      </div>`;
    } else if (!current && _documentId) {
      // Viewing an old version — offer to create new
      actionsHtml = `<div class="vh-actions">
        <button class="vh-action-btn vh-new-version" onclick="event.stopPropagation(); createNewVersion()">New Version</button>
        <span class="vh-action-hint">Rebuild report incorporating your feedback</span>
      </div>`;
    }
    body.innerHTML = actionsHtml + versions.map(v => _renderVersionCard(v)).join('');
  } catch (e) {
    body.innerHTML = '<div class="vh-empty">Failed to load version history.</div>';
  }
}

function _renderVersionCard(v) {
  const isCurrent = v.version_id === _versionId;
  const badgeCls = `vh-badge vh-badge-${v.status}`;
  const date = v.updated_at ? new Date(v.updated_at).toLocaleDateString('en-US', { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' }) : '';

  let statsHtml = '';
  const parts = [];
  if (v.chat_turns) parts.push(`<span>💬 ${v.chat_turns}</span>`);
  if (v.bookmarks) parts.push(`<span>🔖 ${v.bookmarks}</span>`);
  if (v.tasks) parts.push(`<span>☑ ${v.tasks}</span>`);
  if (parts.length) statsHtml = `<div class="vh-card-stats">${parts.join('')}</div>`;

  let notesHtml = '';
  if (v.user_notes) notesHtml = `<div class="vh-card-notes" title="${esc(v.user_notes)}">${esc(v.user_notes)}</div>`;

  return `
    <div class="vh-card${isCurrent ? ' vh-current' : ''}" onclick="loadVersion('${v.version_id}')" title="Click to load this version">
      <div class="vh-card-head">
        <span class="vh-card-vnum">V${v.version_num}</span>
        <span class="${badgeCls}">${v.status}</span>
        ${isCurrent ? '<span class="vh-badge vh-badge-active">current</span>' : ''}
        <span class="vh-card-date">${date}</span>
      </div>
      <div class="vh-card-summary">${esc(v.change_summary)}</div>
      ${statsHtml}
      ${notesHtml}
    </div>
  `;
}

async function loadVersion(versionId) {
  if (versionId === _versionId) {
    toggleVersionHistory();
    return;
  }
  // Load version in-place via fetch (no page reload)
  try {
    const r = await fetch(`${API}/chat/financial-strategy/version/${versionId}`);
    if (!r.ok) throw new Error('load failed');
    const session = await r.json();
    if (!session || !session.version_id) throw new Error('bad response');

    // Update state
    _documentId = session.document_id || _documentId;
    _versionId = session.version_id;
    _threadId = session.thread_id || '';
    _currentOrg = session.org_name || _currentOrg;
    _bookmarks = (session.body && session.body.bookmarks) || [];
    _baseline = (session.body && session.body.baseline) || _baseline;

    // Update URL without reload
    const url = new URL(window.location);
    url.searchParams.set('doc', _documentId);
    url.searchParams.delete('v');
    url.searchParams.delete('org');
    window.history.replaceState({}, '', url);

    // Re-render report if baseline changed
    if (session.body && session.body.baseline) {
      _renderChapter2(session.body.baseline);
    }

    // Restore tasks
    const taskSnap = (session.body && session.body.tasks_snapshot) || [];
    _tasks = taskSnap.map(t => ({ ...t, source: 'restored' }));
    renderTaskList();

    // Restore chat history
    const history = session.chat_history || [];
    const msgs = document.getElementById('chatMessages');
    if (msgs) {
      msgs.innerHTML = '';
      for (const turn of history) {
        if (turn.user) msgs.innerHTML += `<div class="fs-chat-msg user">${esc(turn.user)}</div>`;
        if (turn.assistant) {
          const rendered = (typeof renderMarkdown === 'function') ? renderMarkdown(turn.assistant) : esc(turn.assistant);
          msgs.innerHTML += `<div class="fs-chat-msg system">${rendered}</div>`;
        }
      }
      msgs.scrollTop = msgs.scrollHeight;
    }

    // Refresh drawer to show new current
    await _loadVersionHistory();
  } catch (e) {
    alert('Failed to load version. Please try again.');
  }
}

async function finalizeCurrentVersion() {
  if (!_versionId) return;
  const notes = prompt('Add finalization notes (what was decided, next steps):');
  if (notes === null) return; // cancelled

  const btn = document.querySelector('.vh-finalize');
  if (btn) { btn.disabled = true; btn.textContent = 'Finalizing...'; }

  try {
    const r = await fetch(`${API}/chat/financial-strategy/finalize`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ version_id: _versionId, user_notes: notes }),
    });
    if (!r.ok) throw new Error('finalize failed');
    await _loadVersionHistory(); // refresh the drawer
  } catch (e) {
    if (btn) { btn.disabled = false; btn.textContent = 'Finalize'; }
    alert('Failed to finalize version.');
  }
}

async function createNewVersion() {
  if (!_documentId) return;
  const btn = document.querySelector('.vh-new-version');
  if (btn) { btn.disabled = true; btn.textContent = 'Rebuilding report...'; }

  try {
    const r = await fetch(`${API}/chat/financial-strategy/new-version`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ document_id: _documentId, stream: true }),
    });
    if (!r.ok) throw new Error('new-version failed');
    const data = await r.json();
    const cid = data.correlation_id;
    const newVersionId = data.version_id;

    if (cid) {
      // Poll for completion (report is being regenerated with feedback)
      if (btn) btn.textContent = `Regenerating report (V${data.version_num})...`;
      let done = false;
      while (!done) {
        await new Promise(ok => setTimeout(ok, 2000));
        const pr = await fetch(`${API}/chat/financial-strategy/response/${cid}`);
        if (pr.ok) {
          const pd = await pr.json();
          if (pd.status === 'completed') done = true;
        }
      }
    }
    // Load the new version in-place
    await loadVersion(newVersionId);
  } catch (e) {
    if (btn) { btn.disabled = false; btn.textContent = 'New Version'; }
    alert('Failed to create new version.');
  }
}

// ── Session resume ──────────────────────────────────────────────────────────
async function _tryResumeSession() {
  // Check URL for ?doc=xxx (document) or ?v=xxx (specific version) or ?org=xxx
  const params = new URLSearchParams(window.location.search);
  const docParam = params.get('doc');
  const verParam = params.get('v');
  const orgParam = params.get('org');

  let session = null;
  try {
    if (verParam) {
      const r = await fetch(`${API}/chat/financial-strategy/version/${verParam}`);
      if (r.ok) session = await r.json();
    } else if (docParam || orgParam) {
      const q = docParam ? `document_id=${docParam}` : `org=${encodeURIComponent(orgParam)}`;
      const r = await fetch(`${API}/chat/financial-strategy/session/resume?${q}`);
      if (r.ok) session = await r.json();
    }
  } catch (_) {}

  if (!session || session.found === false || !session.version_id) return false;

  // Restore state
  _documentId = session.document_id || '';
  _versionId = session.version_id || '';
  _threadId = session.thread_id || '';
  _currentOrg = session.org_name || '';
  _bookmarks = (session.body && session.body.bookmarks) || [];
  _baseline = (session.body && session.body.baseline) || null;

  if (!_baseline) return false;

  // Restore UI — skip start screen, render report
  document.getElementById('startScreen').hidden = true;
  _showIndustryReportEarly(_currentOrg);

  // Load industry data
  try {
    const indR = await fetch(`${API}/chat/financial-strategy/industry`);
    if (indR.ok) _industry = await indR.json();
  } catch (_) {}

  _renderChapter2(_baseline);
  document.getElementById('hdOrg').textContent = _currentOrg;
  document.getElementById('fsTaskSection').style.display = 'block';

  // Restore tasks snapshot
  const taskSnap = (session.body && session.body.tasks_snapshot) || [];
  _tasks = taskSnap.map(t => ({ ...t, source: 'restored' }));
  renderTaskList();

  // Restore chat history into the chat pane
  const history = session.chat_history || [];
  if (history.length > 0) {
    const msgs = document.getElementById('chatMessages');
    if (msgs) {
      for (const turn of history) {
        if (turn.user) {
          msgs.innerHTML += `<div class="fs-chat-msg user">${esc(turn.user)}</div>`;
        }
        if (turn.assistant) {
          const rendered = (typeof renderMarkdown === 'function')
            ? renderMarkdown(turn.assistant) : esc(turn.assistant);
          msgs.innerHTML += `<div class="fs-chat-msg system">${rendered}</div>`;
        }
      }
      msgs.scrollTop = msgs.scrollHeight;
    }
  }

  // Update URL with document_id for shareability
  if (!docParam && _documentId) {
    const url = new URL(window.location);
    url.searchParams.set('doc', _documentId);
    window.history.replaceState({}, '', url);
  }

  return true;
}

// ── Init ─────────────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', async () => {
  await loadOrgs();
  const resumed = await _tryResumeSession();
  if (resumed) {
    // Session restored — skip tour
  }
});
