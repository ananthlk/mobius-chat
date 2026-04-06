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
      opt.dataset.type = o.org_type || '';
      opt.dataset.revenue = o.total_revenue || '';
      opt.dataset.claims = o.total_claims || '';
      opt.dataset.city = o.org_city || '';
      opt.dataset.panel = o.panel_size || '';
      opt.dataset.clinicians = o.servicing_npi_count || '';
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
  const orgName = document.getElementById('orgSelect').value;
  const errEl = document.getElementById('startError');
  if (!orgName) { errEl.textContent = 'Select an organization'; errEl.style.display = 'block'; return; }
  errEl.style.display = 'none';

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
      body: JSON.stringify({ org_name: orgName, stream: true }),
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

// Render org-specific Chapter 2 content into the existing page (replacing the loading placeholder)
function _renderChapter2(b) {
  const loading = document.getElementById('ch2Loading');
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

  loading.outerHTML = ch2Html;
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
  const industrySections = ['sec-industry-landscape', 'sec-industry-rates', 'sec-industry-leakage', 'sec-industry-burnout', 'sec-industry-trends'];

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
  container.innerHTML = '<div style="text-align:center;padding:2rem;color:var(--text-3)"><span class="spinner-sm"></span> Loading industry report…</div>';
  try {
    const r = await fetch(`${API}/chat/financial-strategy/industry-report-html`);
    if (!r.ok) throw new Error('Failed to load industry report');
    const html = await r.text();
    container.innerHTML = html;
    // Remove elements that don't belong in the embedded view
    container.querySelectorAll('.report-header, .dl-btn-wrap, .review-note, [id="sel-toolbar"]').forEach(el => el.remove());
    // Remove the closing footer (only direct children, not the report-wrap container itself)
    container.querySelectorAll('.report-wrap > div').forEach(el => {
      if (el.children.length === 0 && el.textContent.includes('Generated April 2026')) el.remove();
    });
    // Remove standalone scripts (selection toolbar etc)
    container.querySelectorAll('script').forEach(el => el.remove());
  } catch (e) {
    // Fallback: render the old deterministic industry sections
    container.innerHTML = '<div style="padding:1rem;color:var(--text-3);font-size:.8125rem">Industry report unavailable — showing summary view.</div>';
    if (_industry) {
      container.innerHTML = renderIndustryLandscape(_industry) + renderIndustryRates(_industry) + renderIndustryLeakage(_industry) + renderIndustryBurnout(_industry) + renderIndustryTrends(_industry);
    }
  }
}

// ── Chapter 1: Industry Landscape (legacy fallback) ─────────────────────────
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
      <div class="fs-stat-card"><div class="fs-stat-value">${esc(hero.leakage_gap?.value || '$157M')}</div><div class="fs-stat-label">${esc(hero.leakage_gap?.label || 'Lost to Leakage')}</div></div>
      <div class="fs-stat-card"><div class="fs-stat-value">${esc(hero.ratio?.value || '19x')}</div><div class="fs-stat-label">${esc(hero.ratio?.label || 'Leakage vs Rate Impact')}</div></div>
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

    <div class="fs-narrative">
      <p>Florida's 86 Community Mental Health Centers are the behavioral health safety net — the front door for Medicaid beneficiaries in crisis or seeking care for the first time. They handle nearly a quarter of all BH intake assessments statewide. But the data tells a story of a sector under structural pressure.</p>
      <p>Three interconnected problems define the CMHC financial landscape. First, <strong>effective reimbursement rates lag the broader market</strong> on intake and high-acuity services, costing the sector an estimated $8.1M annually. Second — and far more consequential — <strong>patients who enter through CMHCs don't stay for ongoing care</strong>. The sector captures 23.6% of intake but only 5.0% of ongoing services, an engagement gap worth up to $157M. Third, these two forces combine to create <strong>unsustainable caseload imbalance</strong>: intake clinicians are stretched to 50% above industry norms while therapy panels sit at half capacity.</p>
      <p>The comparison group throughout this report is <strong>Rest-of-FL Medicaid providers (excluding CMHCs)</strong>. The P75 of that group — best-in-class performance — is the transformation target. Performing at the CMHC median is not a sign of health; it may still represent underperformance relative to the broader market.</p>
    </div>

    <div class="fs-card-grid" style="grid-template-columns:repeat(3,1fr)">
      ${Object.entries(ind.segments || {}).map(([key, seg]) => `
        <div class="fs-card ${key === 'intake' ? 'blue' : key === 'high_acuity' ? 'yellow' : 'green'}">
          <div class="fs-card-title">${esc(seg.label)}</div>
          <div class="fs-card-big">${seg.cmhc_share || '—'}%</div>
          <div class="fs-card-sub">CMHC market share</div>
          <div style="margin-top:.5rem;font-size:.75rem;color:var(--text-3)">
            CMHC RPB: ${fmt$(seg.sector_rpb)} · Rest-of-FL RPB: ${fmt$(seg.rest_rpb)}
          </div>
          <div style="font-size:.7rem;color:var(--text-3);margin-top:.25rem">${(seg.codes || []).join(', ')}</div>
        </div>
      `).join('')}
    </div>
  </div>`;
}

function renderIndustryRates(ind) {
  const rates = ind.problems?.rates || {};
  const byStage = rates.by_stage || {};
  const gaps = rates.key_rate_gaps || [];
  const strengths = rates.key_rate_strengths || [];

  const stageRows = Object.entries(byStage).map(([stage, d]) => `
    <tr>
      <td><span class="fs-badge ${stage === 'intake' ? 'blue' : stage === 'high_acuity' ? 'yellow' : 'green'}">${esc(stage)}</span></td>
      <td>${fmt$(d.cmhc_rpb)}</td>
      <td>${fmt$(d.rest_rpb)}</td>
      <td><span class="fs-badge ${d.gap_pct < 0 ? 'red' : 'green'}">${fmtPct(d.gap_pct)}</span></td>
      <td style="font-size:.75rem;color:var(--text-3)">${esc(d.direction)}</td>
    </tr>
  `).join('');

  const benchRows = (ind.benchmark_table || []).map(b => `
    <tr>
      <td><strong>${esc(b.code)}</strong></td>
      <td>${fmt$(b.cmhc_p50)}</td>
      <td>${fmt$(b.rest_p50)}</td>
      <td><strong>${fmt$(b.rest_p75)}</strong></td>
      <td>${fmtN(b.rest_n)}</td>
    </tr>
  `).join('');

  return `
  <div class="fs-section" id="sec-industry-rates">
    <div class="fs-section-header">
      <span class="fs-section-tag problem">PROBLEM 1</span>
      <h2 class="fs-section-h">${esc(rates.label || 'Lower Effective Rates')}</h2>
    </div>

    <div class="fs-sector-ref">${esc(rates.description)}</div>

    <div class="fs-narrative">
      <p>When a CMHC clinician performs the same service as a non-CMHC provider, the CMHC typically collects less. This isn't a billing error — it reflects a structural gap in effective reimbursement that compounds across thousands of encounters.</p>
      <p>The damage is concentrated at the <strong>front door and the acute end of the spectrum</strong>. Intake assessments (H0031) pay CMHCs 15% less per beneficiary than the rest of the market. High-acuity services — crisis stabilization, psychosocial rehabilitation — show a 25% gap. These are exactly the services CMHCs are built to deliver, and they're being underpaid for them.</p>
      <p>There is one bright spot: <strong>ongoing care rates actually favor CMHCs</strong>, with a +48.7% RPB advantage in sustained treatment. This likely reflects deeper engagement intensity when patients do stay — more visits, more wraparound services. The problem isn't what CMHCs earn per patient in ongoing care. It's that so few patients make it there.</p>
    </div>

    <h3 style="font-size:.875rem;font-weight:700;margin:1rem 0 .5rem">Rate Gap by Care Stage</h3>
    <div class="fs-table-wrap">
      <table class="fs-table">
        <thead><tr><th>Stage</th><th>CMHC RPB</th><th>Rest-of-FL RPB</th><th>Gap</th><th>Direction</th></tr></thead>
        <tbody>${stageRows}</tbody>
      </table>
    </div>

    <h3 style="font-size:.875rem;font-weight:700;margin:1rem 0 .5rem">Market Benchmark Table (Rest-of-FL excl. CMHCs)</h3>
    <div class="fs-table-wrap">
      <table class="fs-table">
        <thead><tr><th>Code</th><th>CMHC P50</th><th>Rest P50</th><th>Rest P75 (Target)</th><th>Providers</th></tr></thead>
        <tbody>${benchRows}</tbody>
      </table>
    </div>

    <div class="fs-card-grid-2" style="margin-top:1rem">
      <div class="fs-card red">
        <div class="fs-card-title">Key Rate Gaps</div>
        <div class="fs-card-body">${gaps.map(g => `<strong>${g.code}</strong>: CMHC P50 ${fmt$(g.cmhc_p50)} vs Rest P50 ${fmt$(g.rest_p50)} (${fmtPct(g.gap_pct)})`).join('<br>')}</div>
      </div>
      <div class="fs-card green">
        <div class="fs-card-title">Sector Strengths</div>
        <div class="fs-card-body">${strengths.map(g => `<strong>${g.code}</strong>: CMHC P50 ${fmt$(g.cmhc_p50)} vs Rest P50 ${fmt$(g.rest_p50)} (${fmtPct(g.gap_pct)})`).join('<br>')}</div>
      </div>
    </div>
  </div>`;
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
      <p>This is the sector's defining problem — and it's not about rates. For every dollar CMHCs lose to lower reimbursement, they lose <strong>nineteen dollars to patients who walk in the front door and never come back</strong> for ongoing care.</p>
      <p>CMHCs are the entry point for nearly 1 in 4 Medicaid BH beneficiaries in Florida. But by the time those patients need sustained therapy, care management, or medication follow-up, the vast majority are being seen elsewhere — or not at all. The sector's share drops from 23.6% at intake to just 5.0% in ongoing care. That cliff is the leakage problem.</p>
      <p>The math is stark: CMHCs currently earn $38.6M in ongoing care revenue. If they retained patients at the same rate they acquire them, that figure would approach $183M. The gap — up to $157M — represents the single largest financial opportunity in the sector. And it comes with a counterintuitive insight: <strong>when patients do stay, CMHCs deliver more intensive, higher-value care</strong> than the rest of the market, earning 48.7% more per beneficiary. The sector doesn't have an ongoing care rate problem. It has a retention problem.</p>
    </div>

    <div class="fs-stat-grid" style="grid-template-columns:repeat(3,1fr);margin-top:1rem">
      <div class="fs-stat-card">
        <div class="fs-stat-value">${share.intake?.cmhc_pct || 23.6}%</div>
        <div class="fs-stat-label">Intake Market Share</div>
      </div>
      <div class="fs-stat-card">
        <div class="fs-stat-value">${share.high_acuity?.cmhc_pct || 14.4}%</div>
        <div class="fs-stat-label">High-Acuity Share</div>
      </div>
      <div class="fs-stat-card" style="border-color:var(--red-border)">
        <div class="fs-stat-value" style="color:var(--red)">${share.ongoing?.cmhc_pct || 5.0}%</div>
        <div class="fs-stat-label">Ongoing Care Share</div>
      </div>
    </div>

    <div class="fs-card-grid-2" style="margin-top:1rem">
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
        <div class="fs-card-title">Surprising Finding</div>
        <div class="fs-card-body">
          CMHC ongoing RPB: <strong>${fmt$(surprise.cmhc_ongoing_rpb)}</strong><br>
          Rest-of-FL ongoing RPB: <strong>${fmt$(surprise.rest_ongoing_rpb)}</strong><br>
          Advantage: <strong>+${surprise.advantage_pct || ''}%</strong><br>
          <span style="font-size:.75rem;margin-top:.25rem;display:block">${esc(surprise.implication || '')}</span>
        </div>
      </div>
    </div>

    <div class="fs-insight" style="margin-top:1rem">
      <strong>The rate problem is real — but leakage dwarfs it ${esc(eng.ratio_display || '19x')}.</strong>
      CMHCs handle nearly a quarter of FL Medicaid BH intake but only 5% of ongoing care.
      The strategic question isn't just "how do we get paid more per visit?" but "how do we keep patients in care after the front door?"
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

  const panelRows = panels.map(p => `
    <tr>
      <td><strong>${esc(p.code)}</strong></td>
      <td>${fmtN(p.cmhc_panel)}</td>
      <td>${fmtN(p.rest_panel)}</td>
      <td><span class="fs-badge ${p.gap_pct > 0 ? 'red' : p.gap_pct > -20 ? 'yellow' : 'green'}">${p.gap_pct > 0 ? '+' : ''}${fmtPct(p.gap_pct)}</span></td>
      <td><span class="fs-badge ${p.stage === 'intake' ? 'blue' : p.stage === 'high_acuity' ? 'yellow' : 'green'}">${esc(p.stage || '')}</span></td>
      <td style="font-size:.75rem;color:var(--text-3)">${esc(p.note || '')}</td>
    </tr>
  `).join('');

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
      <p>Problems 1 and 2 don't just cost money — they break the people delivering care. Lower rates force higher volume to meet revenue targets. Patient leakage means intake clinicians are on a treadmill: constantly processing new patients who won't convert to sustained caseloads downstream.</p>
      <p>The data shows this clearly. CMHC intake clinicians carry panels of <strong>115 patients — 50% more than the industry norm</strong> of 76.5. Meanwhile, therapy clinicians serve panels of just 123, barely half the market standard of 227. The system is lopsided: the front door is overwhelmed while the back end runs below capacity.</p>
      <p>This 3.3x imbalance ratio — intake volume per clinician divided by therapy volume — is the signature metric of the burnout cycle. Intake staff burn out from volume. Therapy staff can't build sustainable panels because patients leave. Care managers at 275 patients per clinician absorb the coordination burden for the entire flow. The result is turnover, which creates capacity gaps, which drives further revenue loss — a self-reinforcing spiral.</p>
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
        <thead><tr><th>Code</th><th>CMHC Panel</th><th>Rest-of-FL</th><th>Gap</th><th>Stage</th><th>Note</th></tr></thead>
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
      <p>The three problems above aren't static — they've been moving over the past six years. The question isn't just "how big is the gap?" but "is it getting better or worse?" The answer, for most metrics, is sobering.</p>
      <p>The <strong>rate gap has widened</strong> since 2019. CMHCs were already earning less per claim than the rest of the market, and that disadvantage has grown — from roughly -3% to -14%. The broader market has seen rate increases that CMHCs haven't fully captured, whether due to payer mix, contract structures, or coding patterns.</p>
      <p>The <strong>ongoing care RPB advantage has eroded</strong>. CMHCs still earn more per beneficiary than the market in sustained care, but that edge has shrunk. This is a warning signal: the one area where the sector outperforms is losing ground.</p>
      <p>The <strong>caseload imbalance has remained stubbornly flat</strong>. Despite awareness of burnout pressures, the ratio of intake-to-therapy volume per clinician hasn't materially improved. The structural forces — leakage and rate pressure — continue to drive the same lopsided workload distribution.</p>
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
      <strong>${esc(ind.leakage_gap || '$157M')}</strong> to patient leakage (${esc(ind.ratio || '19x')} the rate impact),
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
      CMHCs handle 23.6% of FL Medicaid BH intake but only 5.0% of ongoing care.
      For every $1 lost to rates, <strong>$19 is lost to patients who enter the front door and don't stay.</strong>
      The $157M leakage gap dwarfs the $8.1M rate gap.
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
