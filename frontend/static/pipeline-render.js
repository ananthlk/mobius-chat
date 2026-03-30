// ── Dashboard functions ────────────────────────────────────────
async function loadDashboard() {
  const listEl  = document.getElementById('dashRunList');
  const countEl = document.getElementById('dashRunCount');
  try {
    const r = await fetch(`${API}/chat/credentialing-runs?limit=20`);
    const runs = r.ok ? await r.json() : [];
    if (countEl) countEl.textContent = runs.length ? `${runs.length} run${runs.length !== 1 ? 's' : ''}` : '';
    if (!runs.length) {
      listEl.innerHTML = `<div style="padding:2rem 1.5rem;text-align:center;color:var(--text-3);font-size:.875rem;background:var(--surface);border-radius:12px;border:1.5px solid var(--border)">
        <div style="font-size:1.5rem;margin-bottom:.5rem">📋</div>
        <div style="font-weight:600;color:var(--text);margin-bottom:.25rem">No runs yet</div>
        Start your first credentialing run using the form on the right.
      </div>`;
      return;
    }
    listEl.innerHTML = runs.map(run => renderRunCard(run)).join('');
  } catch (e) {
    listEl.innerHTML = `<div style="color:var(--red);font-size:.8rem;padding:.75rem">Failed to load runs: ${esc(e.message)}</div>`;
  }
}

function renderRunCard(run) {
  const total      = PLAN.length;
  const completed  = run.completed_steps || [];
  const done       = completed.length;
  const isComplete = run.phase === 'complete';
  const isError    = run.phase === 'error';
  const badgeCls   = isComplete ? 'complete' : isError ? 'error' : 'in-progress';
  const badgeLabel = isComplete ? '✓ Complete' : isError ? '✗ Error' : '● In progress';
  const modeIcon   = run.mode === 'autopilot' ? '⚡' : '🧭';
  const modeLabel  = run.mode === 'autopilot' ? 'Autopilot' : 'Copilot';

  const pendingLabel = run.pending_step_id
    ? (PLAN.find(p => p.id === run.pending_step_id)?.short || run.pending_step_id)
    : null;

  const timeAgo    = run.updated_at ? _timeAgo(run.updated_at) : '';
  const createdStr = run.created_at ? new Date(run.created_at).toLocaleDateString() : '';

  // ── 4 metric chips ────────────────────────────────────────────
  const stepsCls = isComplete ? 'chip-steps-done' : done > 0 ? 'chip-steps-prog' : '';
  const chipsHtml = `
    <div class="run-card-chips">
      <span class="run-chip ${stepsCls}">${done}/${total} steps</span>
      <span class="run-chip" title="NPI alignment score — available after Step 4">NPI Score: —</span>
      <span class="run-chip" title="Roster provider count — available after roster upload">Providers: —</span>
      <span class="run-chip" title="Open tasks — available after NPPES alignment">Tasks: —</span>
    </div>`;

  // ── Segmented step track ──────────────────────────────────────
  const pendingIdx = run.pending_step_id ? PLAN.findIndex(p => p.id === run.pending_step_id) : -1;
  const trackHtml = `<div class="run-step-track" title="${done} of ${total} steps complete">` +
    PLAN.map((p, i) => {
      const isDone   = completed.includes(p.id) || (isComplete);
      const isActive = i === pendingIdx && !isComplete && !isError;
      const cls      = isDone ? 'seg-done' : isActive ? 'seg-active' : '';
      return `<div class="run-step-seg ${cls}" title="${esc(p.short)}"></div>`;
    }).join('') +
  `</div>`;

  // ── Single CTA ────────────────────────────────────────────────
  let ctaBtn;
  if (isError) {
    ctaBtn = `<button class="btn-action" style="font-size:.78rem;padding:.3rem .8rem;color:var(--red);border-color:var(--red-border)" onclick="loadRun('${run.run_id}')">View error</button>`;
  } else if (isComplete) {
    ctaBtn = `<button class="btn-action ghost" style="font-size:.78rem;padding:.3rem .8rem" onclick="loadRun('${run.run_id}')">View report →</button>`;
  } else {
    ctaBtn = `<button class="btn-action" style="font-size:.78rem;padding:.3rem .8rem" onclick="loadRun('${run.run_id}')">Resume → ${pendingLabel ? esc(pendingLabel) : 'next step'}</button>`;
  }

  return `<div class="run-card" id="run-card-${run.run_id}">
    <div class="run-card-top">
      <span class="run-card-org" title="${esc(run.org_name || '')}">${esc(run.org_name || 'Unknown org')}</span>
      <span class="run-card-badge ${badgeCls}">${badgeLabel}</span>
      <button class="run-delete-btn" onclick="deleteRun('${run.run_id}', '${esc(run.org_name||'this run')}', this)" title="Delete run">
        <svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.75">
          <path d="M3 4h10M6 4V2.5h4V4M5 4l.5 9.5h5L11 4"/>
        </svg>
      </button>
    </div>
    ${chipsHtml}
    ${trackHtml}
    <div class="run-card-foot">
      <span class="run-card-mode">${modeIcon} ${modeLabel}</span>
      <span class="run-card-time">${timeAgo ? `Updated ${timeAgo}` : ''}${createdStr ? ` · ${createdStr}` : ''}</span>
    </div>
    <div class="run-card-actions">${ctaBtn}</div>
  </div>`;
}

async function deleteRun(runId, orgName, btnEl) {
  // Two-step confirm: first click shows inline confirm text; second click deletes
  if (!btnEl.dataset.confirming) {
    btnEl.dataset.confirming = '1';
    btnEl.title = 'Click again to confirm deletion';
    btnEl.style.color = 'var(--red)';
    btnEl.style.borderColor = 'var(--red-border)';
    btnEl.style.background  = 'var(--red-bg)';
    btnEl.innerHTML = `<svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.75"><path d="M3 4h10M6 4V2.5h4V4M5 4l.5 9.5h5L11 4"/></svg> Delete?`;
    // Auto-cancel after 4 s
    setTimeout(() => {
      if (btnEl.dataset.confirming) {
        delete btnEl.dataset.confirming;
        btnEl.title   = 'Delete run';
        btnEl.style.cssText = '';
        btnEl.innerHTML = `<svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.75"><path d="M3 4h10M6 4V2.5h4V4M5 4l.5 9.5h5L11 4"/></svg>`;
      }
    }, 4000);
    return;
  }
  // Confirmed — delete
  btnEl.disabled = true;
  btnEl.innerHTML = `<span style="font-size:.68rem">Deleting…</span>`;
  try {
    const r = await fetch(`${API}/chat/credentialing-runs/${runId}`, { method: 'DELETE' });
    if (r.ok) {
      feEmit('Run deleted — ' + orgName);
      const card = document.getElementById(`run-card-${runId}`);
      if (card) {
        card.style.transition = 'opacity .25s, transform .25s';
        card.style.opacity    = '0';
        card.style.transform  = 'translateX(-8px)';
        setTimeout(() => { card.remove(); loadDashboard(); }, 260);
      }
    } else {
      btnEl.disabled = false;
      btnEl.innerHTML = `<svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.75"><path d="M3 4h10M6 4V2.5h4V4M5 4l.5 9.5h5L11 4"/></svg>`;
      alert(`Could not delete run: ${r.status}`);
    }
  } catch (e) {
    btnEl.disabled = false;
    alert(`Delete failed: ${e.message}`);
  }
}

function _timeAgo(iso) {
  const ms = Date.now() - new Date(iso).getTime();
  const m = Math.floor(ms / 60000);
  if (m < 1)   return 'just now';
  if (m < 60)  return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24)  return `${h}h ago`;
  return `${Math.floor(h/24)}d ago`;
}

function loadRun(rid) {
  runId = rid;
  lastRun = null; window.lastRun = null;
  _npiDetailsCache = null; _npiPickerStepRunId = null; _npiSelections = new Set(); _manualNpis = new Set();
  window._activeProvTab = 'all';
  window._rosterUploadState = null;
  window._reconFilter = 'all';
  _reconTasks = null;
  _autoLoadRosterAttempted = false;
  _lastStepBodyKey = null;
  _viewStepId = null; _lastPendingStepId = null;
  _validationInFlight = false;
  window.history.replaceState({}, '', `?run_id=${runId}`);
  feEmit(`Loading run ${rid.slice(0,8)}…`);
  showPipeline();
  poll();
}

function prefillNewRun(orgName) {
  const inp = document.getElementById('orgInput');
  if (inp) inp.value = orgName;
  scrollToNewRun();
}

function scrollToNewRun() {
  const el = document.getElementById('newRunSection');
  if (el) el.scrollIntoView({ behavior: 'smooth', block: 'center' });
  setTimeout(() => { const inp = document.getElementById('orgInput'); if (inp) inp.focus(); }, 400);
}

function selectMode(el) {
  document.getElementById('labelCopilot').classList.toggle('selected', el.value === 'copilot');
  document.getElementById('labelAutopilot').classList.toggle('selected', el.value === 'autopilot');
}

function newRun() {
  clearInterval(pollTimer);
  clearAutoAdvanceTimers();
  _validationInFlight = false;
  runId = null; lastRun = null;
  window._rosterUploadState = null;
  window._reconFilter = 'all';
  _reconTasks = null;
  _autoLoadRosterAttempted = false;
  _lastStepBodyKey = null;
  _viewStepId = null; _lastPendingStepId = null;
  document.getElementById('pipelineView').hidden = true;
  document.getElementById('startScreen').style.display = '';
  _feTickerShow(false);
  window.history.replaceState({}, '', window.location.pathname);
  loadDashboard();
}

function showPipeline() {
  document.getElementById('startScreen').style.display = 'none';
  document.getElementById('pipelineView').hidden = false;
  _feTickerShow(true);
}

// ── Sidebar collapse ───────────────────────────────────────────
function togglePipelineSidebar() {
  const sb = document.getElementById('plSidebar');
  if (!sb) return;
  const collapsed = sb.classList.toggle('collapsed');
  localStorage.setItem('plSidebarCollapsed', collapsed ? '1' : '0');
  _updateSbChevron(collapsed);
}

function _updateSbChevron(collapsed) {
  const ch = document.getElementById('plSbChevron');
  if (ch) ch.textContent = collapsed ? '›' : '‹';
}

async function startPipeline() {
  const org = (document.getElementById('orgInput').value || '').trim();
  if (!org) { showStartError('Please enter an organization name.'); return; }
  const mode = document.querySelector('input[name=mode]:checked')?.value || 'copilot';
  const btn = document.getElementById('startBtn');
  btn.disabled = true; btn.textContent = 'Starting…';
  hideStartError();
  try {
    const r = await fetch(`${API}/chat/credentialing-runs`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ org_name: org, mode }),
    });
    if (!r.ok) throw new Error(await r.text());
    const data = await r.json();
    runId = data.run_id;
    _npiDetailsCache = null; _npiPickerStepRunId = null; _npiSelections = new Set(); _manualNpis = new Set();
    window._activeProvTab = 'all';
    window._rosterUploadState = null;
    window._reconFilter = 'all';
    _reconTasks = null;
    _autoLoadRosterAttempted = false;
    _lastStepBodyKey = null;
    _viewStepId = null; _lastPendingStepId = null;
    window.history.replaceState({}, '', `?run_id=${runId}`);
    feEmit('Pipeline started — ' + org, 'ok');
    showPipeline();
    render(data);
    schedulePoll(data);
  } catch (e) {
    showStartError('Could not start pipeline: ' + e.message);
  } finally {
    btn.disabled = false; btn.textContent = 'Start Pipeline →';
  }
}

function showStartError(msg) {
  const el = document.getElementById('startError');
  el.textContent = msg; el.style.display = 'block';
}
function hideStartError() { document.getElementById('startError').style.display = 'none'; }

function schedulePoll(data) {
  clearInterval(pollTimer);
  if (data?.phase === 'complete' || data?.phase === 'error') return;
  // Autopilot: fast poll. Copilot awaiting_validation: slow poll (user drives it).
  // Decision steps (user is interacting with expanded rows etc): very slow poll — 20s.
  const isCopilotWaiting = data?.mode === 'copilot' && data?.phase === 'awaiting_validation';
  const isDecisionStep = DECISION_STEPS.has(data?.pending_step_id);
  const delay = (isCopilotWaiting && isDecisionStep) ? 20000 : isCopilotWaiting ? 8000 : 2500;
  pollTimer = setInterval(poll, delay);
}

async function poll() {
  if (!runId) return;
  try {
    const r = await fetch(`${API}/chat/credentialing-runs/${runId}?full=1`);
    if (!r.ok) return;
    const data = await r.json();
    // Emit step transitions to the activity ticker
    const prevPending = window._lastPendingStepId;
    const newPending  = data.pending_step_id;
    if (newPending && newPending !== prevPending) {
      const stepFriendly = {
        identify_org:              'Step 1 — Identify organization',
        find_locations:            'Step 2 — Find locations',
        nppes_alignment:           'Step 3 — NPPES alignment',
        pml_alignment:             'Step 4 — PML / Medicaid check',
        find_associated_providers: 'Step 5 — Associated providers',
        taxonomy_optimization:     'Step 6 — Taxonomy optimization',
      };
      feEmit(`Pipeline moved to: ${stepFriendly[newPending] || newPending}`);
    }
    if (data.status === 'complete' && lastRun?.status !== 'complete') {
      feEmit('✓ Pipeline complete!', 'ok');
    } else if (data.status === 'error' && lastRun?.status !== 'error') {
      feEmit('Pipeline error — check pipeline view', 'error');
    }
    render(data);
    schedulePoll(data);
  } catch { /* ignore transient */ }
}

// ── Render full run state ─────────────────────────────────────
function render(data) {
  clearAutoAdvanceTimers();
  lastRun = data;
  window.lastRun = data;          // expose for functions that access window.lastRun
  window._lastRunId = data.run_id;
  document.getElementById('hdOrg').textContent = data.org_name || '—';
  document.getElementById('hdMode').textContent = data.mode === 'autopilot' ? '⚡ Autopilot' : '🧭 Copilot';
  // Update chat context
  document.getElementById('chatOrgName').textContent = data.org_name || 'this organization';

  const steps = (data.orchestrator_state?.steps || []);
  const doneCount = steps.filter(s => s.status === 'done' || s.status === 'skipped').length;
  const pct = Math.round(doneCount / PLAN.length * 100);
  ['pipelineProgressFill','pipelineChatProgressFill'].forEach(id => {
    const fill = document.getElementById(id);
    if (fill) fill.style.width = pct + '%';
  });
  const prog = document.getElementById('scProgress');
  if (prog) prog.textContent = doneCount > 0 ? `${doneCount}/${PLAN.length} steps` : '';

  renderStepper(steps, data.pending_step_id);         // drives hidden #stepper (legacy)
  renderSidebarSteps(steps, data.pending_step_id);    // drives #plStepList
  renderCurrentStep(data, steps);
  renderHistory(steps, data);
  renderBanner(data);
  // Sidebar tasks must come after renderCurrentStep so _reconTasks is populated
  setTimeout(renderSidebarTasks, 0);
}

function stepStatusFromState(stepId, steps) {
  const s = steps.find(x => x.id === stepId);
  return s?.status || 'pending';
}

function renderStepper(steps, pendingId) {
  const el = document.getElementById('stepper');
  el.innerHTML = PLAN.map((p, i) => {
    const st = steps.find(x => x.id === p.id);
    let css = 'step-node';
    let icon = i + 1;
    const isViewing = _viewStepId === p.id;
    if (isViewing)                    css += ' viewing';
    if (st?.status === 'done')        { css += ' done';        icon = '✓'; }
    else if (st?.status === 'skipped'){ css += ' skipped';     icon = '—'; }
    else if (st?.status === 'failed') { css += ' failed';      icon = '✗'; }
    else if (st?.status === 'in_progress' || p.id === pendingId) { css += ' in_progress'; }

    // Completed, skipped, or failed steps are navigable (can be jumped to)
    const navigable = ['done', 'skipped', 'failed'].includes(st?.status);
    if (navigable) css += ' navigable';
    const click = navigable ? `onclick="jumpToStep('${p.id}')"` : '';
    const tip   = navigable ? `${p.label} — click to review` : p.label;

    return `<div class="${css}" ${click} title="${tip}">
      <div class="step-circle">${icon}</div>
      <div class="step-label">${p.short}</div>
    </div>`;
  }).join('');
}

function renderSidebarSteps(steps, pendingId) {
  const el = document.getElementById('plStepList');
  if (!el) return;
  el.innerHTML = PLAN.map((p, i) => {
    const st = steps.find(x => x.id === p.id);
    let cls = 'pl-step-item';
    let icon = String(i + 1);
    const isViewing = _viewStepId === p.id;
    if (isViewing) cls += ' viewing';
    if (st?.status === 'done')        { cls += ' done';    icon = '✓'; }
    else if (st?.status === 'skipped'){ cls += ' skipped'; icon = '—'; }
    else if (st?.status === 'failed') { cls += ' failed';  icon = '✗'; }
    else if (st?.status === 'in_progress' || p.id === pendingId) { cls += ' active'; }
    const navigable = ['done','skipped','failed'].includes(st?.status);
    if (navigable) cls += ' navigable';
    const click = navigable ? `onclick="jumpToStep('${p.id}')"` : '';
    return `<div class="${cls}" ${click} title="${esc(p.label)}">
      <div class="pl-step-circle">${icon}</div>
      <div class="pl-step-label">${esc(p.short)}</div>
    </div>`;
  }).join('');
}

function renderSidebarTasks() {
  const el = document.getElementById('plTaskSection');
  if (!el) return;
  const open = (_reconTasks || []).filter(t => !t.done && t.type !== 'confirmed');
  if (!open.length) { el.style.display = 'none'; return; }
  el.style.display = '';

  // Step short labels for sidebar tags
  const stepShort = { identify_org: 'Identity', find_locations: 'Locations', nppes_alignment: 'NPPES',
    pml_alignment: 'PML', find_associated_providers: 'Compliance', taxonomy_optimization: 'Taxonomy' };

  // Group by step so user sees where tasks came from
  const byStep = {};
  for (const t of open) {
    const key = t.stepId || t.step || 'nppes_alignment';
    (byStep[key] = byStep[key] || []).push(t);
  }

  const stepOrder = ['identify_org','find_locations','nppes_alignment','pml_alignment','find_associated_providers','taxonomy_optimization'];
  let body = '';
  let shown = 0;
  for (const sid of stepOrder) {
    const group = byStep[sid];
    if (!group) continue;
    body += `<div style="font-size:.62rem;font-weight:700;text-transform:uppercase;letter-spacing:.04em;color:var(--text-3);padding:.2rem .4rem .05rem;margin-top:${shown>0?'.35rem':'0'}">${stepShort[sid] || sid}</div>`;
    for (const t of group.slice(0, 4)) {
      shown++;
      const txt = t.text || t.action || t.description || '';
      body += `<div class="pl-task-item">
        <div class="pl-task-dot ${sid}"></div>
        <div class="pl-task-text" style="font-size:.73rem">${esc(t.providerName ? t.providerName + ' — ' + txt : txt)}</div>
      </div>`;
    }
    if (group.length > 4) body += `<div style="font-size:.65rem;color:var(--text-3);padding:.1rem .4rem">+${group.length-4} more in this step</div>`;
  }
  const extra = open.length - shown;

  el.innerHTML = `<div class="pl-task-head">
    <span>Open tasks (${open.length})</span>
    <a href="#taskQueuePanel" class="link-btn" style="font-size:.65rem">View all</a>
  </div>${body}${extra > 0 ? `<div style="font-size:.68rem;color:var(--text-3);padding:.25rem .4rem">+${extra} more</div>` : ''}`;
}

function renderBanner(data) {
  const el = document.getElementById('phaseBanner');
  if (data.phase === 'complete') {
    el.innerHTML = `<div class="banner complete">
      <strong>✓ Pipeline complete</strong> — all steps finished for <strong>${esc(data.org_name)}</strong>.
      ${data.final_report_text ? `<button class="link-btn" onclick="openReport()" style="margin-left:.75rem">View report →</button>` : ''}
    </div>`;
  } else if (data.phase === 'error') {
    el.innerHTML = `<div class="banner error"><strong>✗ Pipeline error:</strong> ${esc(data.error || 'unknown error')}</div>`;
  } else {
    el.innerHTML = '';
  }
}

function renderCurrentStep(data, steps) {
  const pid = data.pending_step_id;
  const draft = data.draft_output || {};

  // Auto-clear historical view when a new pipeline step becomes active
  if (pid && pid !== _lastPendingStepId) {
    _lastPendingStepId = pid;
    _viewStepId = null;
  }

  let displayPlan, displayStatus, displayDraft, isHistoricalView = false;

  if (_viewStepId && _viewStepId !== pid) {
    // ── Historical step view ──────────────────────────────────
    const histStep = steps.find(s => s.id === _viewStepId);
    displayPlan   = PLAN.find(p => p.id === _viewStepId) || PLAN[0];
    displayStatus = histStep?.status || 'done';
    // Prefer full validated draft (available when fetched with ?full=1) over sparse StepState
    displayDraft  = (data.step_drafts && data.step_drafts[_viewStepId])
      ? { status: histStep?.status, ...data.step_drafts[_viewStepId] }
      : { status: histStep?.status, result_summary: histStep?.result_summary };
    isHistoricalView = true;
  } else {
    // ── Active / default view ─────────────────────────────────
    displayPlan   = PLAN.find(p => p.id === pid) || PLAN[PLAN.length - 1];
    displayStatus = draft.status || 'pending';
    displayDraft  = draft;
    if (!pid && data.phase === 'complete') {
      const lastDone = [...steps].reverse().find(s => s.status === 'done');
      displayPlan   = PLAN.find(p => p.id === lastDone?.id) || PLAN[PLAN.length - 1];
      displayStatus = 'done';
    }
  }

  const stepIdx = PLAN.findIndex(p => p.id === displayPlan?.id);
  // Use the full question text as the title — it IS the mission
  const _stepQuestions = {
    identify_org:             'Do we have the correct legal identity and NPIs for this organization?',
    find_locations:           'Have we locked down every approved site where clinicians are authorized to practice?',
    nppes_alignment:          'Does every clinician on the roster have a valid, active NPPES entry?',
    pml_alignment:            'Is every clinician enrolled with the payors they need to bill?',
    find_associated_providers:'Are there providers billing under this org\'s NPI who aren\'t on the approved roster?',
    taxonomy_optimization:    'Are all billing taxonomy codes correctly aligned to each provider\'s credentials and services?',
  };
  const questionText = _stepQuestions[displayPlan?.id] || displayPlan?.label || '—';
  // Step label is a small eyebrow above the question — not inline with the question text
  const stepPill = `<span style="display:block;font-size:.67rem;font-weight:600;text-transform:uppercase;letter-spacing:.07em;color:var(--text-3);margin-bottom:.2rem">Step ${stepIdx + 1} of ${PLAN.length}</span>`;
  document.getElementById('scTitle').innerHTML = `${stepPill}<span style="display:block">${esc(questionText)}</span>`;

  // Row 2: status badge + key insight chip + open tasks count
  const _stepStatus = (() => {
    if (data.pending_step_id === displayPlan?.id) return 'running';
    const _ss = (steps || []).find(s => s.id === displayPlan?.id);
    if (_ss?.status === 'done')    return 'done';
    if (_ss?.status === 'skipped') return 'skipped';
    if (_ss?.status === 'failed')  return 'failed';
    if (_ss?.status === 'pending') return 'pending';
    if (displayDraft.result_summary) return 'done';
    return 'pending';
  })();
  const insightChip = buildMissionStatusChips(displayPlan?.id, displayDraft, _stepStatus);
  let taskPill = '';
  {
    if (displayPlan?.id === 'pml_alignment') {
      // PML has its own task system — compute counts directly from pml auto-tasks
      try {
        const _pmlAll   = typeof _buildPmlAutoTasks === 'function' ? _buildPmlAutoTasks() : [];
        const _pmlDone  = typeof _loadPmlTaskDone  === 'function' ? _loadPmlTaskDone()  : new Set();
        const _pmlOpen  = _pmlAll.filter(t => !_pmlDone.has(t.id)).length;
        const _pmlTotal = _pmlAll.length;
        if (_pmlTotal > 0) {
          const _pmlBlocking  = _pmlAll.filter(t => !t.warnOnly && !_pmlDone.has(t.id)).length;
          const _pmlRisk      = _pmlAll.filter(t =>  t.warnOnly && !_pmlDone.has(t.id)).length;
          const _label = _pmlOpen === 0
            ? '✓ all resolved'
            : [_pmlBlocking  > 0 ? `✗ ${_pmlBlocking} blocking`   : '',
               _pmlRisk      > 0 ? `⚠ ${_pmlRisk} risk`           : '']
               .filter(Boolean).join(' · ') || `${_pmlOpen} open`;
          taskPill = `<button class="step-head-task-count" onclick="togglePmlTaskDrawer(true)"
            title="Open PML task panel · ${_pmlOpen} open, ${_pmlTotal} total">
            <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" style="width:10px;height:10px;flex-shrink:0"><rect x="2" y="3" width="12" height="10" rx="1.5"/><path d="M5 6h6M5 9h4"/></svg>
            ${_label} · ${_pmlTotal} task${_pmlTotal !== 1 ? 's' : ''}</button>`;
        }
      } catch(_) {}
    } else {
      // General recon tasks for all other steps
      const openTasks  = (_reconTasks || []).filter(t => !t.done && (t.stepId === displayPlan?.id || !t.stepId)).length;
      const totalTasks = (_reconTasks || []).filter(t => t.stepId === displayPlan?.id || !t.stepId).length;
      if (totalTasks > 0) {
        taskPill = `<button class="step-head-task-count" onclick="toggleTaskDrawer()"
          title="Open task panel · ${openTasks} open, ${totalTasks} total">
          <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" style="width:10px;height:10px;flex-shrink:0"><rect x="2" y="3" width="12" height="10" rx="1.5"/><path d="M5 6h6M5 9h4"/></svg>
          ${openTasks > 0 ? `${openTasks} open` : '✓ all done'} · ${totalTasks} task${totalTasks !== 1 ? 's' : ''}</button>`;
      }
    }
  }

  const badgeHtml = isHistoricalView
    ? `<span class="status-badge badge-done">✓ Done</span>`
    : statusBadge(displayStatus, data);

  document.getElementById('scBadge').innerHTML = `${badgeHtml}${insightChip ? ' ' + insightChip : ''}${taskPill ? ' ' + taskPill : ''}`;

  // Fingerprint prevents destroying DOM state (expanded rows etc.) on every poll.
  const emitCount = Object.values(data.orchestrator_state?.step_emit_log || {}).reduce((s, v) => s + (v?.length || 0), 0);
  // Include a hash of the draft's keys so historical views re-render when step_drafts loads
  const draftRichness = Object.keys(displayDraft).length;
  const bodyKey = `${displayPlan?.id}|${displayStatus}|${data.phase}|${displayDraft.result_summary || ''}|${emitCount}|${_viewStepId || ''}|${draftRichness}`;
  if (bodyKey !== _lastStepBodyKey) {
    _lastStepBodyKey = bodyKey;
    document.getElementById('scBody').innerHTML = buildStepBody(displayPlan?.id, displayDraft, data, steps);
    document.getElementById('scFoot').innerHTML = isHistoricalView
      ? `<div style="padding:.4rem 0;display:flex;align-items:center;gap:.5rem;border-top:1px solid var(--border)">
           <button class="link-btn" style="font-size:.8125rem;font-weight:600;color:var(--indigo)" onclick="jumpToCurrentStep()">← Back to current step</button>
           <span style="font-size:.75rem;color:var(--text-3)">· Read-only view</span>
         </div>`
      : buildStepFoot(displayPlan?.id, data, displayDraft);
  }
}

function statusBadge(status, data) {
  if (data.phase === 'complete') return `<span class="status-badge badge-complete">✓ Complete</span>`;
  if (data.phase === 'error')    return `<span class="status-badge badge-failed">✗ Error</span>`;
  if (status === 'done')         return `<span class="status-badge badge-done">✓ Done</span>`;
  if (status === 'in_progress')  return `<span class="status-badge badge-running"><span class="spinner"></span> Running</span>`;
  if (status === 'failed')       return `<span class="status-badge badge-failed">✗ Failed</span>`;
  if (status === 'skipped')      return `<span class="status-badge badge-pending">— Skipped</span>`;
  if (data.mode === 'copilot')   return `<span class="status-badge badge-review">⏸ Review</span>`;
  return `<span class="status-badge badge-pending">Pending</span>`;
}

// ── Step body renderers ───────────────────────────────────────
/* ── Step task builder ───────────────────────────────────────────── */

const STEP_TASK_TEMPLATES = {
  identify_org: [
    { label: '+ Add org NPI',          text: 'Add org NPI manually to registry' },
    { label: '✗ Flag for deactivation', text: 'Flag org NPI for deactivation' },
    { label: '✎ Request NPI correction',text: 'Submit NPI correction request to NPPES' },
    { label: '✓ Mark org as reviewed',  text: 'Mark organization identity as reviewed and confirmed' },
  ],
  find_locations: [
    { label: '+ Add location',          text: 'Add missing service location' },
    { label: '✗ Deactivate location',   text: 'Deactivate location — no longer in use' },
    { label: '✎ Update address',        text: 'Update location address or contact info' },
    { label: '⚠ Flag for review',       text: 'Flag location for compliance review' },
  ],
  nppes_alignment: [
    { label: '✓ Validate NPI manually', text: 'Validate provider NPI manually — confirmed match' },
    { label: '+ Request new NPI',       text: 'Request new NPI creation for provider' },
    { label: '⚠ Flag NPI mismatch',     text: 'Flag NPI mismatch — name or taxonomy inconsistency' },
    { label: '✗ Remove junk row',       text: 'Remove junk / placeholder row from roster' },
  ],
  pml_alignment: [
    { label: '+ Enroll in Medicaid',    text: 'Enroll provider in Medicaid PML' },
    { label: '⚠ Flag PML mismatch',     text: 'Flag PML enrollment mismatch — review required' },
    { label: '✎ Correct PML record',    text: 'Submit PML record correction' },
    { label: '✗ Remove from PML',       text: 'Remove provider from PML — no longer active' },
  ],
  find_associated_providers: [
    { label: '⚠ Flag ghost biller',     text: 'Flag as potential ghost billing risk — not on roster' },
    { label: '✓ Approve for roster',    text: 'Approve provider for roster inclusion' },
    { label: '🔍 Request compliance review', text: 'Escalate to compliance team for review' },
    { label: '✗ Terminate billing access', text: 'Terminate billing access — provider not credentialed' },
  ],
  taxonomy_optimization: [
    { label: '✎ Update taxonomy code',  text: 'Update taxonomy code to correct specialty' },
    { label: '⚠ Flag taxonomy mismatch',text: 'Flag taxonomy mismatch — billing code misaligned' },
    { label: '🔍 Request re-credentialing', text: 'Request re-credentialing review for taxonomy update' },
    { label: '+ Add taxonomy code',     text: 'Add additional taxonomy code for dual-specialty provider' },
  ],
};

function buildStepTaskBuilder(stepId) {
  const templates = STEP_TASK_TEMPLATES[stepId] || [];
  const stepTasks = (_reconTasks || []).filter(t => t.stepId === stepId && !t.done);
  const allStepCount = (_reconTasks || []).filter(t => t.stepId === stepId).length;

  const chipHtml = templates.map(t =>
    `<button class="step-task-chip" onclick="stepSelectTemplate(this,'${stepId}',${JSON.stringify(t.text).replace(/'/g,"\\'")})">${esc(t.label)}</button>`
  ).join('');

  const addedHtml = stepTasks.length ? `
    <div class="step-task-added-list">
      ${stepTasks.map(t => `
        <div class="step-task-added-item">
          <div class="step-dot step-dot-${stepId}"></div>
          <span style="flex:1;color:var(--text)">${esc(t.text)}</span>
          <button onclick="reconDismissTask('${t.id}')" style="font-size:.65rem;color:var(--text-3);background:none;border:none;cursor:pointer;padding:0 .2rem">✕</button>
        </div>`).join('')}
    </div>` : '';

  return `
  <div class="step-task-builder" id="stepTaskBuilder-${stepId}">
    <div class="step-task-builder-head" onclick="toggleStepTaskBuilder('${stepId}')">
      <div class="step-task-builder-head-left">
        <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.75"><path d="M2 8h12M8 2v12"/></svg>
        Add tasks for this step
        ${allStepCount ? `<span class="step-task-builder-count">${allStepCount}</span>` : ''}
      </div>
      <span class="step-task-builder-toggle" id="stepTaskBuilderToggle-${stepId}">▾</span>
    </div>
    <div class="step-task-builder-body" id="stepTaskBuilderBody-${stepId}">
      ${chipHtml ? `<div class="step-task-chips" id="stepTaskChips-${stepId}">${chipHtml}</div>` : ''}
      <div class="step-task-input-row">
        <input class="step-task-input" id="stepTaskInput-${stepId}" type="text"
               placeholder="Describe the task…"
               onkeydown="stepTaskBuilderEnter(event,'${stepId}')">
        <button class="step-task-add-btn" onclick="stepAddTask('${stepId}')">Add task</button>
      </div>
      ${addedHtml}
    </div>
  </div>`;
}

function toggleStepTaskBuilder(stepId) {
  const body    = document.getElementById(`stepTaskBuilderBody-${stepId}`);
  const toggle  = document.getElementById(`stepTaskBuilderToggle-${stepId}`);
  if (!body) return;
  const open = body.style.display !== 'none';
  body.style.display   = open ? 'none' : '';
  toggle.textContent   = open ? '▸' : '▾';
}

function stepSelectTemplate(btn, stepId, text) {
  const input = document.getElementById(`stepTaskInput-${stepId}`);
  if (!input) return;
  // Deselect other chips
  const chips = btn.closest('.step-task-chips');
  if (chips) chips.querySelectorAll('.step-task-chip').forEach(c => c.classList.remove('selected'));
  btn.classList.add('selected');
  input.value = text;
  input.focus();
}

function stepTaskBuilderEnter(event, stepId) {
  if (event.key === 'Enter') stepAddTask(stepId);
}

function stepAddTask(stepId) {
  const input = document.getElementById(`stepTaskInput-${stepId}`);
  if (!input) return;
  const text = (input.value || '').trim();
  if (!text) return;
  feEmit('Task added — ' + text);

  if (!_reconTasks) _reconTasks = [];
  const id = `step-${stepId}-${Date.now()}`;
  _reconTasks.push({
    id,
    stepId,
    providerIdx:  -1,
    providerName: '',
    text,
    type:         'user_created',
    source:       'user',
    severity:     'medium',
    phase:        0,
    done:         false,
  });

  // Clear input and deselect chips
  input.value = '';
  const chips = document.getElementById(`stepTaskChips-${stepId}`);
  if (chips) chips.querySelectorAll('.step-task-chip').forEach(c => c.classList.remove('selected'));

  // Refresh sidebar + full task queue (if visible)
  renderSidebarTasks();
  _refreshTaskQueueFull();

  // Refresh just the "added" list inside this builder so newly added task shows
  const builder = document.getElementById(`stepTaskBuilder-${stepId}`);
  if (builder) builder.outerHTML = buildStepTaskBuilder(stepId);

  // Brief flash on sidebar to signal the task landed
  const taskEl = document.getElementById('plTaskSection');
  if (taskEl) { taskEl.style.outline = '2px solid var(--indigo)'; setTimeout(() => { taskEl.style.outline = ''; }, 800); }
}

/* ── Step mission banner ─────────────────────────────────────────── */
function buildStepMission(stepId, draft, planEntry, stepStatus) {
  // stepStatus: 'pending' | 'running' | 'done' | 'failed' | 'skipped'
  const questions = {
    identify_org:             'Are we confident about who this organization is in the NPPES registry?',
    find_locations:           'Have we locked down every approved site where clinicians are authorized to practice?',
    nppes_alignment:          'Does every clinician on the roster have a valid, active NPPES record?',
    pml_alignment:            'Is every clinician enrolled with the payors they need to bill?',
    find_associated_providers:'Are there providers billing under this organization\'s NPI who aren\'t on the approved roster?',
    taxonomy_optimization:    'Are all billing taxonomy codes correctly aligned to each provider\'s credentials and services?',
  };

  const chips = buildMissionStatusChips(stepId, draft, stepStatus);
  const q = questions[stepId] || planEntry?.desc || '';

  return `<div class="step-mission">
    <div class="step-mission-q">The question</div>
    <div class="step-mission-label">${esc(q)}</div>
    ${chips ? `<div class="step-mission-status">${chips}</div>` : ''}
  </div>`;
}

function buildMissionStatusChips(stepId, draft, stepStatus) {
  if (stepStatus === 'pending') {
    return `<span class="step-status-chip grey">Not started</span>`;
  }
  if (stepStatus === 'running') {
    return `<span class="step-status-chip indigo">⟳ In progress…</span>`;
  }
  if (stepStatus === 'failed') {
    return `<span class="step-status-chip red">✗ Step failed — review errors below</span>`;
  }
  if (stepStatus === 'skipped') {
    return `<span class="step-status-chip grey">Skipped</span>`;
  }

  // Done — step-specific answer chips
  switch (stepId) {
    case 'identify_org': {
      const npis = draft.org_npis || [];
      if (npis.length) {
        return `<span class="step-status-chip green">✓ ${npis.length} org NPI${npis.length !== 1 ? 's' : ''} confirmed</span>`;
      }
      return `<span class="step-status-chip amber">⚠ No org NPI confirmed yet</span>`;
    }
    case 'find_locations': {
      const locs = draft.locations || [];
      if (locs.length) {
        return `<span class="step-status-chip green">✓ ${locs.length} approved location${locs.length !== 1 ? 's' : ''} confirmed</span>`;
      }
      return `<span class="step-status-chip amber">⚠ No locations found yet</span>`;
    }
    case 'nppes_alignment': {
      const rs = window._rosterUploadState;
      if (rs?.phase === 'done') {
        const clean     = rs.report?.clean || [];
        const validated = clean.filter(p => p._decision === 'validated').length;
        const pending   = clean.filter(p => !p._decision).length;
        const rejected  = clean.filter(p => p._decision === 'rejected').length;
        const chips = [];
        if (validated) chips.push(`<span class="step-status-chip green">✓ ${validated} validated</span>`);
        if (pending)   chips.push(`<span class="step-status-chip amber">⚠ ${pending} need review</span>`);
        if (rejected)  chips.push(`<span class="step-status-chip red">✗ ${rejected} rejected</span>`);
        if (!chips.length) chips.push(`<span class="step-status-chip grey">Roster loaded — begin review</span>`);
        return chips.join('');
      }
      const so = draft.step_output || {};
      if (so.validated_count || so.needs_review_count) {
        const chips = [];
        if (so.validated_count)    chips.push(`<span class="step-status-chip green">✓ ${so.validated_count} validated</span>`);
        if (so.needs_review_count) chips.push(`<span class="step-status-chip amber">⚠ ${so.needs_review_count} need review</span>`);
        return chips.join('');
      }
      return `<span class="step-status-chip grey">Waiting for roster</span>`;
    }
    case 'pml_alignment': {
      const v = draft.pml_validated_count || 0;
      const f = draft.pml_flagged_count   || 0;
      if (v || f) {
        const chips = [];
        if (v) chips.push(`<span class="step-status-chip green">✓ ${v} enrolled</span>`);
        if (f) chips.push(`<span class="step-status-chip amber">⚠ ${f} flagged</span>`);
        return chips.join('');
      }
      return `<span class="step-status-chip grey">PML source not yet connected</span>`;
    }
    case 'find_associated_providers': {
      const bc = draft.bucket_counts || {};
      const total = draft.provider_count || (draft.providers || []).length;
      if (total) {
        const chips = [];
        if (bc.aligned)       chips.push(`<span class="step-status-chip green">✓ ${bc.aligned} on roster + confirmed</span>`);
        if (bc.external_only) chips.push(`<span class="step-status-chip red">⚠ ${bc.external_only} billing but not on roster</span>`);
        if (bc.anomaly)       chips.push(`<span class="step-status-chip red">✗ ${bc.anomaly} anomalies</span>`);
        return chips.join('') || `<span class="step-status-chip grey">${total} providers analyzed</span>`;
      }
      return `<span class="step-status-chip grey">Complete earlier steps to enable audit</span>`;
    }
    case 'taxonomy_optimization': {
      const so = draft.step_output || {};
      if (so.opportunity_count > 0) {
        return `<span class="step-status-chip amber">⚠ ${so.opportunity_count} optimization opportunit${so.opportunity_count !== 1 ? 'ies' : 'y'} found</span>`;
      }
      if (so.analyzed_count > 0) {
        return `<span class="step-status-chip green">✓ ${so.analyzed_count} providers analyzed — codes aligned</span>`;
      }
      return `<span class="step-status-chip grey">Awaiting taxonomy analysis</span>`;
    }
    default:
      return `<span class="step-status-chip grey">Complete</span>`;
  }
}

function buildStepBody(stepId, draft, data, steps) {
  const summary = draft.result_summary || '';
  const parts = [];

  // Find plan entry and step status for mission banner
  const planEntry  = (typeof PLAN !== 'undefined' ? PLAN : []).find(p => p.id === stepId);
  const stepState  = (steps || []).find(s => s.id === stepId);
  const stepStatus = (() => {
    if (data.pending_step_id === stepId) return 'running';
    if (stepState?.status === 'done')    return 'done';
    if (stepState?.status === 'skipped') return 'skipped';
    if (stepState?.status === 'failed')  return 'failed';
    if (stepState?.status === 'pending') return 'pending';
    if (draft.result_summary)            return 'done';
    return 'pending';
  })();

  // For nppes_alignment the upload section header describes the state — skip the
  // result_summary paragraph so the page doesn't open with an instructional placeholder.
  if (summary && stepId !== 'nppes_alignment') parts.push(`<p class="result-text">${esc(summary)}</p>`);

  // Emit log — shown on every step (collapsible)
  const emitLog = draft.step_emit_log || [];
  if (emitLog.length && stepId !== 'find_locations') { // find_locations renders it inline
    parts.push(buildEmitLog(emitLog));
  }

  switch (stepId) {

    case 'ensure_benchmarks': {
      const so = draft.step_output;
      if (so?.row_count > 0) {
        parts.push(statRow([
          { val: so.row_count, lbl: 'benchmark rows', cls: 'green' },
        ]));
        parts.push(csvPreview(so.csv_preview, ['taxonomy_code','description','benchmark_rvu','benchmark_paid']));
      }
      break;
    }

    case 'identify_org': {
      const npis = draft.org_npis || [];
      if (npis.length) {
        if (_npiDetailsCache !== null) {
          // Already fetched — render directly from cache, no re-fetch
          parts.push(`<div id="npiPickerWrap"></div>`);
          setTimeout(() => {
            const wrap = document.getElementById('npiPickerWrap');
            if (wrap) renderNpiPicker(wrap, npis, _npiDetailsCache);
          }, 0);
        } else {
          parts.push(`<div id="npiPickerWrap">
            <div class="npi-loading"><span class="spinner"></span> Loading NPI details…</div>
          </div>`);
          setTimeout(() => loadNpiDetails(npis, data), 80);
        }
      } else if (!summary) {
        parts.push(`<p class="result-text" style="color:var(--text-3)">Searching NPPES for organization…</p>`);
      }
      break;
    }

    case 'find_locations': {
      if (_isStepSealed('find_locations')) parts.push(_buildSealedBanner('find_locations'));
      const locs = draft.locations || [];
      const emitLog = draft.step_emit_log || [];
      if (emitLog.length) parts.push(buildEmitLog(emitLog));
      if (locs.length) {
        parts.push(statRow([{ val: locs.length, lbl: `location${locs.length !== 1 ? 's' : ''} found` }]));
        parts.push(`<div id="locGrid" class="loc-grid">${locs.map((l, i) => buildLocCard(l, i)).join('')}</div>`);
        parts.push(`<div class="loc-add-wrap">
          <button class="loc-add-btn" onclick="addLocCard()">+ Add location</button>
          <span style="font-size:.72rem;color:var(--text-3)" id="locCount">${locs.length} location${locs.length !== 1 ? 's' : ''}</span>
        </div>`);
        setTimeout(() => initLocCards(locs), 0);
      } else if (!summary) {
        parts.push(`<p class="result-text" style="color:var(--text-3)">Finding locations…</p>`);
      }
      break;
    }

    case 'find_associated_providers': {
      // Step 5: Ghost billing & compliance audit
      // Find external providers billing under the org's NPI who are NOT on the approved roster.
      const providers = draft.providers || [];
      const srcCounts = draft.source_counts || {};
      const bc        = draft.bucket_counts || {};
      const total     = draft.provider_count || providers.length || 0;

      if (total) {
        const chips = [];
        if (bc.aligned)          chips.push({ val: bc.aligned,         lbl: 'on roster + confirmed', cls: 'green' });
        if (bc.external_only)    chips.push({ val: bc.external_only,   lbl: 'billing but not on roster', cls: 'amber' });
        if (bc.anomaly)          chips.push({ val: bc.anomaly,         lbl: 'anomalies — review required', cls: 'red' });
        if (bc.needs_attention)  chips.push({ val: bc.needs_attention, lbl: 'need attention', cls: 'amber' });
        if (chips.length) parts.push(statRow(chips));

        if (bc.external_only > 0) {
          // Warning: left-border only, no heavy amber background
          parts.push(`<div style="border-left:3px solid var(--amber,#d97706);padding:.5rem .75rem;font-size:.8125rem;color:var(--text-2);background:var(--grey-bg);border-radius:0 6px 6px 0;margin-top:.25rem">
            <strong style="color:var(--text)">⚠ ${bc.external_only} provider${bc.external_only !== 1 ? 's' : ''} in external billing but not on your approved roster.</strong>
            Review each one — these represent potential ghost billing exposure.
          </div>`);
        }

        // Provider table
        const rows = providers.slice(0, 50).map(p => {
          const isGhost = p.bucket === 'external_only';
          const isAnom  = p.bucket === 'anomaly';
          // No row background coloring — use left-border stripe for signal instead
          const rowStyle = isGhost ? 'style="border-left:2px solid var(--amber,#d97706)"'
                         : isAnom  ? 'style="border-left:2px solid var(--red)"' : '';
          const cIssue  = isGhost ? 'Billing under org NPI — not on approved roster' : isAnom ? 'Anomaly — review required' : '';
          const cCtx    = JSON.stringify({ stepId:'find_associated_providers', type:'billing_provider', name: p.name||'', npi: p.npi||'', issue: cIssue, suggestedText: cIssue ? `${p.name||'Provider'} — ${cIssue}` : `Review compliance status for ${p.name||'provider'}` });
          return `<tr ${rowStyle}>
            <td style="font-size:.8rem;padding:.35rem .5rem;font-weight:500;color:var(--text)">${esc(p.name || '—')}</td>
            <td style="font-size:.75rem;padding:.35rem .5rem;color:var(--text-3);font-family:monospace">${esc(p.npi || '—')}</td>
            <td style="font-size:.78rem;padding:.35rem .5rem;color:var(--text-2)">${esc(p.specialty || '—')}</td>
            <td style="font-size:.75rem;padding:.35rem .5rem;color:var(--text-3)">${p.sources?.join(', ') || '—'}</td>
            <td style="padding:.35rem .5rem">
              ${isGhost ? `<span style="font-size:.67rem;color:var(--amber,#d97706);font-weight:600">Not on roster</span>` : ''}
              ${p.bucket === 'aligned' ? `<span style="font-size:.67rem;color:var(--green);font-weight:600">Aligned</span>` : ''}
              ${isAnom ? `<span style="font-size:.67rem;color:var(--red);font-weight:600">Anomaly</span>` : ''}
            </td>
            <td style="padding:.3rem .5rem">
              <button class="row-task-btn" onclick="openTaskPopover(JSON.parse(this.dataset.ctx),event)" data-ctx="${esc(cCtx)}" title="Create task">
                <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6"><rect x="3" y="2" width="10" height="12" rx="1.5"/><path d="M6 6h4M6 9h4M6 12h2"/></svg>
              </button>
            </td>
          </tr>`;
        }).join('');
        const thS5 = 'padding:.35rem .5rem;text-align:left;font-size:.65rem;font-weight:600;text-transform:uppercase;letter-spacing:.05em;color:var(--text-3)';
        parts.push(`<div style="overflow-x:auto;margin-top:.5rem;border:1px solid var(--border);border-radius:8px">
          <table style="width:100%;border-collapse:collapse;font-size:.8rem">
            <thead><tr style="border-bottom:1px solid var(--border);background:var(--grey-bg)">
              <th style="${thS5}">Provider</th>
              <th style="${thS5}">NPI</th>
              <th style="${thS5}">Specialty</th>
              <th style="${thS5}">Sources</th>
              <th style="${thS5}">Status</th>
              <th style="padding:.35rem .5rem;width:36px"></th>
            </tr></thead>
            <tbody>${rows}</tbody>
          </table>
          ${providers.length > 50 ? `<div style="font-size:.72rem;color:var(--text-3);padding:.35rem .5rem">+${providers.length - 50} more providers</div>` : ''}
        </div>`);
      } else {
        parts.push(`<div class="coming-soon-box">
          <div style="font-size:1.1rem;margin-bottom:.5rem;opacity:.4">🔍</div>
          <div class="cs-title">Ghost Billing & Compliance Audit</div>
          <div class="cs-body">
            Complete Steps 1–4 to enable this audit. We'll cross-reference your
            approved roster against external billing sources to surface unauthorized billers.
          </div>
        </div>`);
      }

      setTimeout(() => { window._provData = providers; }, 0);
      break;
    }

    case 'nppes_alignment': {
      const providers = draft.providers || [];
      const _rsu = window._rosterUploadState;
      const _isActive = ['uploading','parsing','cleaning'].includes(_rsu?.phase);

      // Auto-switch to upload tab when an upload is in progress
      if (_isActive && window._rosterTab !== 'upload') window._rosterTab = 'upload';
      if (!window._rosterTab) window._rosterTab = 'workspace';
      const _tab = window._rosterTab;

      // ── Sealed banner (when step is signed off) ──────────────────
      if (_isStepSealed('nppes_alignment')) {
        parts.push(_buildSealedBanner('nppes_alignment'));
      }

      // ── Tab bar ───────────────────────────────────────────────────
      const _pendingUpload = _rsu && _rsu.phase !== 'done' && _rsu.phase !== 'none';
      parts.push(`<div class="roster-tab-bar" id="rosterTabBar">
        <button class="roster-tab ${_tab==='workspace'?'active':''}" onclick="rosterTabSwitch('workspace')">
          Workspace
        </button>
        <button class="roster-tab ${_tab==='upload'?'active':''}" onclick="rosterTabSwitch('upload')">
          ↑ Upload${_pendingUpload?`<span class="rt-badge">!</span>`:''}
        </button>
        <button class="roster-tab ${_tab==='roster'?'active':''}" onclick="rosterTabSwitch('roster')">
          Roster
        </button>
      </div>`);

      // ── Query / filter bar — only in workspace tab ────────────────
      parts.push(`<div class="roster-filter-bar" id="rosterFilterBar"
        style="display:${_tab==='workspace'?'none':'none'};margin-bottom:.6rem">
        <div class="rqbar-input-row">
          <input class="roster-search-input" id="rosterSearchInput" type="text"
            placeholder="Search providers or NPIs…"
            oninput="_onRosterQueryInput()" />
          <button class="rqbar-ask-btn" tabindex="-1">
            ✦ Ask Mobius
            <span class="rqbar-tooltip">Smart filtering — coming soon</span>
          </button>
        </div>
        <div class="rqbar-chips" id="rqbarChips">
          <button class="rqchip" id="rqchip-needs-attention" data-filter="needs-attention"
            onclick="setRosterFilter('needs-attention')">
            Needs attention <span class="rqchip-count" id="rqc-needs-attention"></span>
          </button>
          <button class="rqchip" id="rqchip-no-npi" data-filter="no-npi"
            onclick="setRosterFilter('no-npi')">
            No NPI <span class="rqchip-count" id="rqc-no-npi"></span>
          </button>
          <button class="rqchip" id="rqchip-open-tasks" data-filter="open-tasks"
            onclick="setRosterFilter('open-tasks')">
            Open tasks <span class="rqchip-count" id="rqc-open-tasks"></span>
          </button>
          <button class="rqchip" id="rqchip-all" data-filter="all"
            onclick="setRosterFilter('all')" style="margin-left:auto;border-style:dashed;opacity:.55">
            Clear filter
          </button>
        </div>
      </div>`);

      // ── Upload section — plain div, shown only in upload tab ──────
      parts.push(`<div id="uploadSection" style="display:${_tab==='upload'?'':'none'};margin-bottom:.5rem">
        <div class="sec-card" style="overflow:hidden">
          <div class="sec-summary" id="uploadSectionSummary" style="cursor:default">
            ${_buildUploadSummaryHtml()}
          </div>
          <div class="sec-body">
            <div id="rosterFileZone">${buildRosterFileZoneHtml()}</div>
            <div id="rosterParseProgress">${buildRosterProgressHtml()}</div>
            <div id="rosterEmissionsSection">${buildRosterEmissionsHtml()}</div>
          </div>
        </div>
      </div>`);

      // ── Reconciliation + Roster content ───────────────────────────
      parts.push(`<div id="reconContent">${buildReconTabHtml(_tab)}</div>`);

      setTimeout(() => {
        window._provData = providers;
        const _sealed = _isStepSealed('nppes_alignment');
        if (!window._rosterUploadState) {
          if (_sealed) {
            // Step is sealed — load the report from DB (fast, no NPPES re-query).
            // _autoLoadRosterIfNeeded already hits ?quick=true + cached llm-clean,
            // so this is just a DB read, not a re-validation.
            _autoLoadRosterIfNeeded();
          } else {
            _autoLoadRosterIfNeeded();
          }
        } else {
          _syncRosterToAllSources();
        }
        _loadRosterDiff();
        if (window._rosterUploadState?.phase === 'done') {
          setTimeout(_loadSessionBanner, 600);
          setTimeout(_loadRosterTruth, 800);
        }
        // Apply tab visibility after DOM is ready
        _applyRosterTabDisplay(window._rosterTab || 'workspace');
      }, 0);
      break;
    }

    case 'pml_alignment': {
      _loadPmlTaskState();  // seed in-memory state from DB (via lastRun) or localStorage
      if (_isStepSealed('pml_alignment')) parts.push(_buildSealedBanner('pml_alignment'));
      parts.push(`<div id="payorContent">${_buildPayorTabHtml()}</div>`);
      break;
    }

    case 'taxonomy_optimization': {
      parts.push(`<div class="coming-soon-box">
        <div style="font-size:1.1rem;margin-bottom:.5rem;opacity:.4">🏷️</div>
        <div class="cs-title">Taxonomy Optimization</div>
        <div class="cs-body">
          Analyze provider taxonomy codes to surface opportunities —
          credentialing at higher-value specialties or correcting billing taxonomy mismatches.
        </div>
        ${summary ? `<div style="margin-top:.75rem;font-size:.8rem;color:var(--text-3)">${esc(summary)}</div>` : ''}
      </div>`);
      break;
    }

    // Legacy step cases kept for backwards compat with old stored runs
    case 'ensure_benchmarks':
    case 'org_benchmark':
    case 'find_services_by_location':
    case 'historic_billing_patterns':
    case 'step_6':
    case 'step_7':
    case 'opportunity_sizing':
    case 'build_report': {
      const so = draft.step_output;
      if (so?.row_count > 0) parts.push(csvPreview(so.csv_preview || '', []));
      break;
    }

    default: {
      const so = draft.step_output;
      if (so?.row_count > 0) parts.push(csvPreview(so.csv_preview));
      else if (!summary) parts.push(`<p class="result-text" style="color:var(--text-3)">Running…</p>`);
    }
  }

  if (data.phase === 'error' && data.error) {
    parts.push(`<div style="background:var(--red-bg);border:1px solid var(--red-border);border-radius:8px;padding:.75rem;font-size:.8125rem;color:var(--red)">${esc(data.error)}</div>`);
  }

  // Task creation lives in the right drawer — no inline "Add a general task" bar needed.

  return parts.join('') || `<p class="result-text" style="color:var(--text-3)">No output yet.</p>`;
}

// ── Location cards (Step 3) ───────────────────────────────────
let _locState = [];   // mutable copy of locations for editing

function initLocCards(locs) {
  _locState = locs.map((l, i) => ({ ...l, _idx: i, _removed: false }));
}

function buildLocCard(loc, idx) {
  const addr1 = loc.site_address || loc.site_address_line_1 || loc.address || '';
  const city  = loc.site_city  || loc.city  || '';
  const state = loc.site_state || loc.state || '';
  // Prefer ZIP+4 if available; fall back to zip5
  const zip9  = loc.site_zip9  || loc.zip9  || '';
  const zip5  = loc.site_zip5  || loc.site_zip || loc.zip || (zip9 ? zip9.split('-')[0] : '');
  const zipDisplay = zip9 || zip5;   // show full zip+4 when we have it
  const fullAddr = [addr1, city, state, zipDisplay].filter(Boolean).join(', ');
  const name  = loc.name || loc.org_name || addr1 || loc.location_id || 'Location';
  const why   = loc.why_listed || '';
  const npi   = loc.npi || loc.org_npi || '';
  const locTaskCtx = JSON.stringify({ stepId:'find_locations', type:'location', name, npi, issue: fullAddr||'Review location', suggestedText:`Review location: ${name}${fullAddr?' — '+fullAddr:''}` });
  // Show a subtle ZIP+4 badge if we only have zip5 (prompt user to confirm +4)
  const zip4missing = zip5 && !zip9;
  const whyBadge = why
    ? `<span class="loc-why">${esc(why)}</span>`
    : '';
  const cityMeta = city
    ? `<span class="loc-addr">${esc(titleCase(city))}${state ? `, ${esc(state)}` : ''}</span>`
    : '';
  return `<div class="loc-card mob-card-enter" id="locCard${idx}" data-idx="${idx}" style="animation-delay:${idx*40}ms">
    <div class="loc-card-head" onclick="toggleLocCard(${idx})">
      <span class="loc-expand-icon">▾</span>
      <span class="loc-name">${esc(titleCase(name))}</span>
      ${cityMeta}
      ${whyBadge}
      <button class="row-task-btn" onclick="openTaskPopover(JSON.parse(this.dataset.ctx),event);event.stopPropagation()" data-ctx="${esc(locTaskCtx)}" title="Create task for this location" style="margin-left:auto">
        <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6"><rect x="3" y="2" width="10" height="12" rx="1.5"/><path d="M6 6h4M6 9h4M6 12h2"/></svg>
      </button>
      <button class="loc-remove-btn" onclick="event.stopPropagation();removeLocCard(${idx})">Remove</button>
    </div>
    <div class="loc-card-body">
      <div class="loc-field"><label>Address</label><input id="locAddr${idx}" value="${esc(addr1)}" placeholder="Street address" onchange="updateLocField(${idx},'site_address',this.value)"/></div>
      <div class="loc-field"><label>City</label><input id="locCity${idx}" value="${esc(city)}" placeholder="City" onchange="updateLocField(${idx},'site_city',this.value)"/></div>
      <div class="loc-field"><label>State</label><input id="locState${idx}" value="${esc(state)}" placeholder="FL" maxlength="2" style="max-width:60px" onchange="updateLocField(${idx},'site_state',this.value)"/></div>
      <div class="loc-field" style="flex-direction:column;align-items:flex-start;gap:.2rem">
        <label>ZIP</label>
        <div style="display:flex;align-items:center;gap:.4rem">
          <input id="locZip5-${idx}" value="${esc(zip5)}" placeholder="12345" maxlength="5" style="max-width:64px" onchange="updateLocField(${idx},'site_zip5',this.value)" title="5-digit ZIP"/>
          <span style="color:var(--text-3);font-size:.75rem">+4</span>
          <input id="locZip9-${idx}" value="${esc(zip9 ? zip9.split('-')[1] || '' : '')}" placeholder="0000" maxlength="4" style="max-width:52px;font-family:monospace" onchange="_updateZip9(${idx})" title="ZIP+4 extension"/>
          ${zip4missing ? `<span style="font-size:.63rem;color:var(--text-3);background:var(--grey-bg);border:1px solid var(--border);border-radius:3px;padding:1px 5px" title="ZIP+4 not yet confirmed">+4 ?</span>` : ''}
        </div>
      </div>
      ${npi ? `<div class="loc-field"><label>NPI</label><input value="${esc(npi)}" style="font-family:monospace;font-size:.75rem" readonly/></div>` : ''}
    </div>
  </div>`;
}

function toggleLocCard(idx) {
  const card = document.getElementById(`locCard${idx}`);
  if (card) card.classList.toggle('loc-open');
}

function removeLocCard(idx) {
  const card = document.getElementById(`locCard${idx}`);
  if (!card) return;
  const ls = _locState.find(l => l._idx === idx);
  if (ls) ls._removed = true;
  feEmit('Location removed' + (ls?.site_name ? ' — ' + ls.site_name : ''));
  card.classList.add('loc-removed');
  card.querySelector('.loc-remove-btn').style.display = 'none';
  const restore = document.createElement('button');
  restore.className = 'loc-restore-btn';
  restore.textContent = 'Restore';
  restore.onclick = (e) => { e.stopPropagation(); restoreLocCard(idx); };
  card.querySelector('.loc-card-head').appendChild(restore);
  updateLocCount();
}

function restoreLocCard(idx) {
  const card = document.getElementById(`locCard${idx}`);
  if (!card) return;
  const ls = _locState.find(l => l._idx === idx);
  if (ls) ls._removed = false;
  feEmit('Location restored' + (ls?.site_name ? ' — ' + ls.site_name : ''));
  card.classList.remove('loc-removed');
  card.querySelector('.loc-restore-btn')?.remove();
  const removeBtn = card.querySelector('.loc-remove-btn');
  if (removeBtn) removeBtn.style.display = '';
  updateLocCount();
}

function updateLocField(idx, field, value) {
  const ls = _locState.find(l => l._idx === idx);
  if (ls) ls[field] = value;
  const fieldLabel = { site_name: 'name', site_address: 'address', site_city: 'city', site_state: 'state', site_zip5: 'zip', npi: 'NPI' }[field] || field;
  if (value) feEmit('Location field updated — ' + fieldLabel + (ls?.site_name ? ' for ' + ls.site_name : ''));
}

function _updateZip9(idx) {
  // Combine zip5 + the 4-digit extension into site_zip9
  const z5el = document.getElementById(`locZip5-${idx}`);
  const z9el = document.getElementById(`locZip9-${idx}`);
  const z5 = (z5el ? z5el.value : '').trim().replace(/\D/g, '').slice(0, 5);
  const ext = (z9el ? z9el.value : '').trim().replace(/\D/g, '').slice(0, 4);
  const ls = _locState.find(l => l._idx === idx);
  if (!ls) return;
  ls.site_zip5 = z5;
  ls.site_zip9 = z5 && ext ? `${z5}-${ext}` : '';
}

function updateLocCount() {
  const active = _locState.filter(l => !l._removed).length;
  const el = document.getElementById('locCount');
  if (el) el.textContent = `${active} location${active !== 1 ? 's' : ''}`;
}

let _newLocIdx = 1000;
function addLocCard() {
  const grid = document.getElementById('locGrid');
  if (!grid) return;
  const idx = _newLocIdx++;
  feEmit('Location added manually');
  const newLoc = { _idx: idx, _removed: false, _new: true, why_listed: 'Added manually' };
  _locState.push(newLoc);
  const div = document.createElement('div');
  div.innerHTML = buildLocCard(newLoc, idx);
  const card = div.firstElementChild;
  grid.appendChild(card);
  card.classList.add('loc-open'); // auto-expand for editing
  updateLocCount();
}

async function validateLocations() {
  if (!runId || !lastRun) return;
  if (_validationInFlight) return;
  if (lastRun.pending_step_id !== 'find_locations') return;
  const active = _locState.filter(l => !l._removed).map(l => {
    const { _idx, _removed, _new, ...rest } = l;
    return rest;
  });
  if (!active.length) { alert('Please keep at least one location.'); return; }
  feEmit('Confirming ' + active.length + ' service location' + (active.length !== 1 ? 's' : '') + '…');
  const btn = document.getElementById('validateBtn');
  if (btn) { btn.disabled = true; btn.textContent = 'Saving…'; }
  _validationInFlight = true;
  clearAutoAdvanceTimers();
  try {
    const r = await fetch(`${API}/chat/credentialing-runs/${runId}/validate`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ step_id: 'find_locations', validated_output: { locations: active } }),
    });
    if (!r.ok) throw new Error(await r.text());
    const data = await r.json();
    feEmit('✓ Locations confirmed', 'ok');
    _locState = [];
    render(data);
    schedulePoll(data);
  } catch (e) {
    feEmit('Location confirmation failed — ' + e.message, 'error');
    if (btn) { btn.disabled = false; btn.textContent = 'Confirm locations →'; }
    alert('Validation failed: ' + e.message);
  } finally {
    _validationInFlight = false;
  }
}

// ── Provider flow graph helpers (Step 4) ─────────────────────
function buildProvTable(providers, filter) {
  const rows = filter === 'all'       ? providers
    : filter === 'confirmed'   ? providers.filter(p => p._rosterDecision === 'validated')
    : filter === 'pending'     ? providers.filter(p => !p._rosterDecision || p._rosterDecision === 'pending')
    : filter === 'rejected'    ? providers.filter(p => p._rosterDecision === 'rejected')
    : providers;

  if (!rows.length) return `<p style="padding:.75rem;font-size:.8125rem;color:var(--text-3)">No providers in this category.</p>`;

  const npiStatusBadge = (p) => {
    if (p._rosterDecision === 'validated')
      return `<span style="font-size:.65rem;font-weight:700;color:var(--green);background:var(--green-bg);border:1px solid var(--green-border);border-radius:3px;padding:1px 5px">✓ confirmed</span>`;
    if (p._rosterDecision === 'rejected')
      return `<span style="font-size:.65rem;font-weight:700;color:var(--red);background:var(--red-bg);border:1px solid var(--red-border);border-radius:3px;padding:1px 5px">✗ rejected</span>`;
    if (p._rosterNew)
      return `<span style="font-size:.65rem;font-weight:700;color:#6366f1;background:var(--indigo-bg);border:1px solid var(--indigo-border);border-radius:3px;padding:1px 5px">+ roster only</span>`;
    return `<span style="font-size:.65rem;color:var(--text-3);background:var(--grey-bg);border:1px solid var(--border);border-radius:3px;padding:1px 5px">pending</span>`;
  };

  return `<table class="prov-table">
    <thead><tr>
      <th>Provider</th>
      <th>Confirmed NPI</th>
      <th>Specialty</th>
      <th>NPI Status</th>
      <th></th>
    </tr></thead>
    <tbody>${rows.slice(0, 100).map(p => {
      const name    = esc(p.name || '—');
      const npi     = p.npi || '';
      const npiEl   = npi
        ? `<span style="font-family:monospace;font-size:.8rem;font-weight:600;color:${p._rosterDecision === 'validated' ? 'var(--green)' : 'var(--text-2)'}">${esc(npi)}</span>`
        : `<span style="color:var(--text-3);font-size:.8rem">—</span>`;
      const spec    = esc((p.specialty || '').substring(0, 40) || '—');
      const rowBg   = p._rosterDecision === 'validated' ? ' style="background:rgba(16,185,129,.04)"'
                    : p._rosterDecision === 'rejected'  ? ' style="background:var(--red-bg)"' : '';
      const action  = p._rosterDecision === 'validated'
        ? `<button class="link-btn" style="font-size:.72rem" onclick="switchProvTab('roster')">Edit →</button>`
        : `<button class="recon-resolve-btn" onclick="switchProvTab('roster')">Validate →</button>`;
      return `<tr${rowBg}>
        <td style="font-weight:600;font-size:.8125rem">${name}</td>
        <td>${npiEl}</td>
        <td style="font-size:.78rem;color:var(--text-2)">${spec}</td>
        <td>${npiStatusBadge(p)}</td>
        <td>${action}</td>
      </tr>`;
    }).join('')}
    ${rows.length > 100 ? `<tr><td colspan="5" style="text-align:center;padding:.5rem;font-size:.75rem;color:var(--text-3)">…and ${rows.length - 100} more</td></tr>` : ''}
    </tbody>
  </table>`;
}

function provFilter(filter) {
  feEmit('Provider filter — ' + filter);
  document.querySelectorAll('.prov-node').forEach(n => n.classList.remove('active-filter'));
  const nodeMap = {
    all:       '.src-merge',
    confirmed: '.bkt-aligned',
    pending:   '.bkt-attention',
    rejected:  '.bkt-anomaly',
  };
  const target = document.querySelector(nodeMap[filter]);
  if (target) target.classList.add('active-filter');
  document.querySelectorAll('.prov-filter-pill').forEach(p => {
    p.classList.toggle('active', p.dataset.filter === filter);
  });
  const wrap = document.getElementById('provTableWrap');
  if (wrap) {
    wrap.innerHTML = buildProvTable(_getMergedProviders(), filter);
  }
}

