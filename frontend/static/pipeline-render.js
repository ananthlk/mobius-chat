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
        provider_summaries:        'Step 7 — Provider AI summaries',
        org_summary:               'Step 8 — Organization health report',
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
  // Keep the sidebar Roster link org-scoped
  const _rosterAnchor = document.getElementById('plRosterAnchor');
  if (_rosterAnchor && data.org_name) {
    _rosterAnchor.href = `/roster?org=${encodeURIComponent(data.org_name)}`;
    if (data.org_name) localStorage.setItem('lastOrg', data.org_name);
  }
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
  // Mount task feed once per run_id (re-mount on run change)
  _mountTaskFeed(data);
}

function _mountTaskFeed(data) {
  const el = document.getElementById('plTaskFeed');
  if (!el) return;
  const rid = data.run_id;
  if (!rid) { el.style.display = 'none'; return; }
  // Only re-mount when run changes; rely on widget's own Refresh button otherwise
  if (el.dataset.mountedRunId === rid) return;
  el.dataset.mountedRunId = rid;
  el.style.display = '';
  if (typeof TaskManager !== 'undefined') {
    TaskManager.mount(el, {
      run_id: rid,
      org: data.org_name || '',
      allowCreate: false,
      allowResolve: true,
    });
  }
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
    pml_alignment: 'PML', find_associated_providers: 'Compliance', taxonomy_optimization: 'Taxonomy',
    provider_summaries: 'Summaries', org_summary: 'Org Report' };

  // Group by step so user sees where tasks came from
  const byStep = {};
  for (const t of open) {
    const key = t.stepId || t.step || 'nppes_alignment';
    (byStep[key] = byStep[key] || []).push(t);
  }

  const stepOrder = ['identify_org','find_locations','nppes_alignment','pml_alignment','find_associated_providers','taxonomy_optimization','provider_summaries','org_summary'];
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
    provider_summaries:       'What is each provider\'s credential health and billability status?',
    org_summary:              'What is the overall credential health of this organization?',
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
  provider_summaries: [
    { label: '✎ Update provider record', text: 'Update provider record based on AI summary findings' },
    { label: '⚠ Escalate for review',    text: 'Escalate flagged provider to compliance team' },
    { label: '✓ Mark as reviewed',        text: 'Mark provider summary as reviewed — no action needed' },
  ],
  org_summary: [
    { label: '📋 Share org report',     text: 'Share organization health report with leadership' },
    { label: '⚠ Schedule credentialing review', text: 'Schedule full credentialing review — org health below threshold' },
    { label: '✓ Approve for submission', text: 'Approve organization credential file for payor submission' },
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
    provider_summaries:       'What is each provider\'s credential health and billability status?',
    org_summary:              'What is the overall credential health of this organization?',
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
      const nR = draft.restriction_count || 0;
      const nG = draft.gap_count || 0;
      const nA = draft.analyzed_count || 0;
      if (nR > 0) return `<span class="step-status-chip red">✗ ${nR} billing restriction${nR!==1?'s':''}</span>`;
      if (nG > 0) return `<span class="step-status-chip amber">⚠ ${nG} enrollment gap${nG!==1?'s':''}</span>`;
      if (nA > 0) return `<span class="step-status-chip green">✓ ${nA} providers analyzed — codes aligned</span>`;
      return `<span class="step-status-chip grey">Awaiting taxonomy analysis</span>`;
    }
    case 'provider_summaries': {
      const ex = draft.extra_data || {};
      const tot   = ex.total       || 0;
      const clean = ex.clean_count || 0;
      const risk  = ex.risk_count  || 0;
      if (risk  > 0) return `<span class="step-status-chip amber">⚠ ${risk} flagged</span><span class="step-status-chip green" style="margin-left:.25rem">✦ ${tot} summaries</span>`;
      if (clean > 0) return `<span class="step-status-chip green">✦ ${tot} summaries — ${clean} fully credentialed</span>`;
      if (tot   > 0) return `<span class="step-status-chip green">✦ ${tot} summaries generated</span>`;
      return `<span class="step-status-chip grey">Awaiting provider summaries</span>`;
    }
    case 'org_summary': {
      const ex  = draft.extra_data || {};
      const met = (ex.org_summary || {}).metrics || ex.metrics || {};
      const pct = met.billable_pct || 0;
      const ot  = met.open_tasks  || 0;
      if (pct > 0) return `<span class="step-status-chip ${pct>=90?'green':pct>=70?'amber':'red'}">${pct}% billable</span>${ot>0?`<span class="step-status-chip amber" style="margin-left:.25rem">⚠ ${ot} open tasks</span>`:''}`;
      return `<span class="step-status-chip grey">Awaiting org health report</span>`;
    }
    default:
      return `<span class="step-status-chip grey">Complete</span>`;
  }
}

// ── Taxonomy provider detail drawer ──────────────────────────────────────────
// Stores the full analysis array so the drawer can access any provider by index.
window._taxAnalysisData = [];

function openTaxProviderDrawer(idx) {
  const a = window._taxAnalysisData[idx];
  if (!a) return;

  // Ensure the drawer exists in the DOM. If the page was loaded from cache
  // before this drawer was added to pipeline.html, create it on first use.
  if (!document.getElementById('taxProviderDrawer')) {
    const backdrop = document.createElement('div');
    backdrop.className = 'task-drawer-backdrop';
    backdrop.id = 'taxProviderDrawerBackdrop';
    backdrop.addEventListener('click', e => { if (e.target === backdrop) closeTaxProviderDrawer(); });

    const drawer = document.createElement('div');
    drawer.className = 'tax-provider-drawer';
    drawer.id = 'taxProviderDrawer';
    drawer.innerHTML = `
      <div class="pml-task-drawer-head">
        <div>
          <div class="pml-task-drawer-title" id="taxProviderDrawerTitle">Provider Detail</div>
          <div style="font-size:.7rem;color:var(--text-3);font-family:monospace;margin-top:.1rem" id="taxProviderDrawerNpi"></div>
        </div>
        <button onclick="closeTaxProviderDrawer()" style="background:none;border:none;cursor:pointer;font-size:1rem;color:var(--indigo);padding:0">✕</button>
      </div>
      <div class="pml-task-drawer-body" id="taxProviderDrawerBody"></div>`;

    const container = document.getElementById('drawerContainer') || document.body;
    container.appendChild(backdrop);
    container.appendChild(drawer);
  }

  const drawerEl = document.getElementById('taxProviderDrawer');
  const titleEl  = document.getElementById('taxProviderDrawerTitle');
  const npiEl    = document.getElementById('taxProviderDrawerNpi');

  const rt    = a.result_type || 'clean';
  const name  = a.provider_name || a.npi || 'Unknown';
  const npi   = a.npi || '';
  const delta = parseFloat(a.delta_billing_pct || 0);
  const codes = a.codes || [];
  const deltaHcpcs = a.delta_hcpcs || [];

  // ── Header ──
  titleEl.textContent = name;
  npiEl.textContent   = npi;

  // ── Status badge ──
  const statusBadge = rt === 'restriction'
    ? `<span class="step-status-chip ${delta > 20 ? 'red' : 'amber'}" style="font-size:.68rem">${delta > 0 ? `✗ ${delta.toFixed(1)}% billing at risk` : '✗ Billing restriction'}</span>`
    : rt === 'gap_only'
    ? `<span class="step-status-chip amber" style="font-size:.68rem">⚠ Enrollment gap</span>`
    : rt === 'no_nppes_taxonomies'
    ? `<span class="step-status-chip grey" style="font-size:.68rem">No NPPES taxonomy data</span>`
    : `<span class="step-status-chip green" style="font-size:.68rem">✓ All codes aligned</span>`;

  // ── Taxonomy codes section ──
  let taxSection = '';
  if (codes.length) {
    const codeRows = codes.map(c => {
      const icon  = c.status === 'approved_enrolled'    ? '✅'
                  : c.status === 'approved_missing_pml' ? '⚠️' : '❌';
      const tmlLbl = (c.status === 'approved_enrolled' || c.status === 'approved_missing_pml')
                   ? `<span style="color:var(--green,#16a34a);font-size:.65rem">✓ In TML</span>`
                   : `<span style="color:var(--red,#dc2626);font-size:.65rem">✗ Not in TML</span>`;
      const pmlLbl = c.status === 'approved_enrolled'
                   ? `<span style="color:var(--green,#16a34a);font-size:.65rem">✓ PML enrolled</span>`
                   : c.status === 'approved_missing_pml'
                   ? `<span style="color:var(--amber,#d97706);font-size:.65rem">⚠ Not in PML</span>`
                   : `<span style="color:var(--text-3);font-size:.65rem">— PML N/A</span>`;
      return `<div style="display:flex;align-items:flex-start;gap:.5rem;padding:.45rem .6rem;border:1px solid var(--border);border-radius:7px;background:var(--bg)">
        <span style="font-size:1rem;line-height:1.3;flex-shrink:0">${icon}</span>
        <div style="flex:1;min-width:0">
          <div style="font-size:.8rem;font-family:monospace;font-weight:600;color:var(--text)">${esc(c.code)}</div>
          <div style="font-size:.72rem;color:var(--text-2);margin:.1rem 0">${esc(c.desc || '—')}</div>
          <div style="display:flex;gap:.6rem;flex-wrap:wrap;margin-top:.2rem">${tmlLbl}${pmlLbl}</div>
        </div>
      </div>`;
    }).join('');
    taxSection = `
      <div style="font-size:.72rem;font-weight:700;text-transform:uppercase;letter-spacing:.05em;color:var(--text-3);margin-bottom:.4rem">NPPES Taxonomy Codes</div>
      <div style="display:flex;flex-direction:column;gap:.35rem;margin-bottom:.75rem">${codeRows}</div>`;
  } else if (rt === 'no_nppes_taxonomies') {
    taxSection = `<div style="font-size:.78rem;color:var(--text-3);padding:.5rem 0">No NPPES taxonomy codes found for this provider in the NPPES registry.</div>`;
  }

  // ── Delta / restriction alert ──
  let alertSection = '';
  if (rt === 'restriction' && deltaHcpcs.length) {
    const atRiskRows = deltaHcpcs.map(h => {
      const pct = h.billing_pct ? `${parseFloat(h.billing_pct).toFixed(1)}%` : '—';
      return `<div style="display:flex;align-items:center;gap:.5rem;padding:.3rem .5rem;border-bottom:1px solid var(--border)">
        <code style="font-size:.72rem;font-weight:700;flex:1;color:var(--red,#dc2626)">${esc(h.hcpcs_code)}</code>
        <span style="font-size:.68rem;color:var(--text-3)">${pct} of billing</span>
      </div>`;
    }).join('');
    alertSection = `
      <div style="background:rgba(220,38,38,.05);border:1px solid rgba(220,38,38,.2);border-radius:8px;padding:.55rem .65rem;margin-bottom:.75rem">
        <div style="font-size:.75rem;font-weight:700;color:var(--red,#dc2626);margin-bottom:.35rem">
          ✗ Billing Restriction — ${delta.toFixed(1)}% of billing at risk
        </div>
        <div style="font-size:.7rem;color:var(--text-2);margin-bottom:.35rem">
          The following HCPC codes are billed under taxonomies that are either not TML-approved or not enrolled in PML.
          These services can no longer be billed until the taxonomy issue is resolved.
        </div>
        <div style="border:1px solid var(--border);border-radius:6px;overflow:hidden;background:var(--bg)">${atRiskRows}</div>
      </div>`;
  } else if (rt === 'gap_only') {
    alertSection = `
      <div style="background:rgba(217,119,6,.05);border:1px solid rgba(217,119,6,.2);border-radius:8px;padding:.55rem .65rem;margin-bottom:.75rem">
        <div style="font-size:.75rem;font-weight:600;color:var(--amber,#d97706)">⚠ Enrollment gap — no billing impact detected</div>
        <div style="font-size:.7rem;color:var(--text-2);margin-top:.25rem">A taxonomy code is TML-approved but missing from PML enrollment. Enroll the missing taxonomy in PML to resolve.</div>
      </div>`;
  }

  // ── Heatmap ──
  const heatmapHtml = _buildTaxHeatmap(a);
  const heatSection = heatmapHtml
    ? `<div style="font-size:.72rem;font-weight:700;text-transform:uppercase;letter-spacing:.05em;color:var(--text-3);margin-bottom:.35rem">HCPC × Taxonomy Coverage</div>
       <div style="font-size:.68rem;color:var(--text-3);margin-bottom:.4rem">HCPC codes this provider has billed, mapped to taxonomy coverage. ⚠ = at-risk codes only covered by gap taxonomies.</div>
       ${heatmapHtml}` : '';

  // ── CTA ──
  const _rosterUrl = `/roster${npi ? '?org=' + encodeURIComponent(window.lastRun?.org_name||'') : ''}`;
  const ctaSection = `
    <div style="margin-top:.75rem;padding-top:.6rem;border-top:1px solid var(--border);display:flex;align-items:center;gap:.6rem">
      <a href="${_rosterUrl}" target="_blank"
        style="font-size:.78rem;color:var(--indigo,#4f46e5);font-weight:700;text-decoration:none;padding:.28rem .75rem;border:1px solid var(--indigo-border,#c7d2fe);border-radius:6px;background:var(--indigo-bg,#eef2ff)">
        Open in Roster →
      </a>
      <span style="font-size:.68rem;color:var(--text-3)">Update taxonomy codes on the roster to resolve issues.</span>
    </div>`;

  const bodyEl = document.getElementById('taxProviderDrawerBody');
  if (bodyEl) bodyEl.innerHTML =
    `<div>${statusBadge}</div>
     <div style="margin-top:.75rem">${taxSection}</div>
     ${alertSection}
     ${heatSection}
     ${ctaSection}`;

  // Open the drawer + backdrop
  drawerEl.classList.add('open');
  const backdropEl = document.getElementById('taxProviderDrawerBackdrop');
  if (backdropEl) backdropEl.classList.add('open');
}

function closeTaxProviderDrawer() {
  document.getElementById('taxProviderDrawer').classList.remove('open');
  document.getElementById('taxProviderDrawerBackdrop').classList.remove('open');
}

// ── Taxonomy heatmap builder ──────────────────────────────────────────────────
function _buildTaxHeatmap(analysis) {
  const heatmapRows = analysis.heatmap_rows || [];
  const codes       = analysis.codes || [];
  if (!heatmapRows.length || !codes.length) return '';

  const colHtml = codes.map(c => {
    const icon = c.status === 'approved_enrolled' ? '✅' : c.status === 'approved_missing_pml' ? '⚠️' : '❌';
    return `<th style="font-size:.62rem;font-family:monospace;padding:.2rem .3rem;text-align:center;border-bottom:1px solid var(--border);color:var(--text-2);white-space:nowrap" title="${esc(c.desc||c.code)}">${icon} ${esc(c.code.slice(-5))}</th>`;
  }).join('') + `<th style="font-size:.62rem;padding:.2rem .3rem;text-align:right;border-bottom:1px solid var(--border);color:var(--text-3)">% billing</th>`;

  const rowHtml = heatmapRows.map(row => {
    const isDelta = row.is_delta;
    const cells = codes.map(c => {
      const covered = row.cells?.[c.code];
      return `<td style="text-align:center;padding:.18rem .3rem;font-size:.7rem">${covered ? '✓' : ''}</td>`;
    }).join('');
    const vol = row.total_volume ? (row.total_volume > 1000 ? `${(row.total_volume/1000).toFixed(1)}k` : row.total_volume) : '—';
    return `<tr style="background:${isDelta ? 'rgba(220,38,38,.06)' : 'transparent'}">
      <td style="font-size:.68rem;font-family:monospace;padding:.18rem .3rem;color:${isDelta?'var(--red)':'var(--text-2)'}${isDelta?';font-weight:600':''}">
        ${esc(row.hcpcs_code)} ${isDelta ? '<span title="At risk — only covered by gap taxonomy">⚠</span>' : ''}
      </td>
      ${cells}
      <td style="font-size:.68rem;text-align:right;padding:.18rem .3rem;color:var(--text-3)">${vol}</td>
    </tr>`;
  }).join('');

  return `<div style="overflow-x:auto;margin:.4rem 0">
    <table style="border-collapse:collapse;width:100%;font-size:.72rem">
      <thead><tr><th style="font-size:.62rem;padding:.2rem .3rem;text-align:left;border-bottom:1px solid var(--border);color:var(--text-3)">HCPC</th>${colHtml}</tr></thead>
      <tbody>${rowHtml}</tbody>
    </table>
  </div>`;
}

// ── Taxonomy provider card (summary row — click opens detail drawer) ───────────
function _buildTaxProviderCard(a, idx) {
  const rt    = a.result_type || 'clean';
  const name  = a.provider_name || a.npi || 'Unknown';
  const npi   = a.npi || '';
  const delta = parseFloat(a.delta_billing_pct || 0);
  const deltaHcpcs = a.delta_hcpcs || [];
  const codes = a.codes || [];

  // Compact code pills (just icons + last 5 chars of code)
  const codeChips = codes.slice(0, 4).map(c => {
    const icon = c.status === 'approved_enrolled' ? '✅' : c.status === 'approved_missing_pml' ? '⚠️' : '❌';
    return `<span style="font-size:.63rem;font-family:monospace;padding:.1rem .3rem;border-radius:5px;background:var(--grey-bg,#f3f4f6);border:1px solid var(--border)">${icon} ${esc(c.code.slice(-7))}</span>`;
  }).join('') + (codes.length > 4 ? `<span style="font-size:.63rem;color:var(--text-3)">+${codes.length-4}</span>` : '');

  // Border + badge per result type
  const borderColor =
    rt === 'restriction' ? (delta > 20 ? 'var(--red,#dc2626)' : 'var(--amber,#d97706)')
    : rt === 'gap_only'  ? 'var(--amber,#d97706)'
    : rt === 'clean'     ? 'var(--green,#16a34a)'
    : 'var(--border)';

  const badge =
    rt === 'restriction'
      ? `<span class="step-status-chip ${delta>20?'red':'amber'}" style="font-size:.63rem">${delta.toFixed(1)}% at risk · ${deltaHcpcs.length} HCPC${deltaHcpcs.length!==1?'s':''}</span>`
    : rt === 'gap_only'
      ? `<span class="step-status-chip amber" style="font-size:.63rem">⚠ Enrollment gap</span>`
    : rt === 'no_nppes_taxonomies'
      ? `<span class="step-status-chip grey" style="font-size:.63rem">No taxonomy data</span>`
    : `<span class="step-status-chip green" style="font-size:.63rem">✓ Aligned</span>`;

  return `<div onclick="openTaxProviderDrawer(${idx})"
    style="border:1px solid var(--border);border-left:3px solid ${borderColor};border-radius:8px;
           padding:.5rem .75rem;background:var(--bg);cursor:pointer;transition:box-shadow .12s;display:flex;align-items:center;gap:.5rem"
    onmouseenter="this.style.boxShadow='0 2px 12px rgba(79,70,229,.12)'"
    onmouseleave="this.style.boxShadow=''">
    <div style="flex:1;min-width:0">
      <div style="font-size:.8125rem;font-weight:600;color:var(--text)">${esc(name)}</div>
      <div style="display:flex;align-items:center;gap:.3rem;flex-wrap:wrap;margin-top:.2rem">
        <span style="font-size:.7rem;font-family:monospace;color:var(--text-3)">${esc(npi)}</span>
        ${codeChips}
      </div>
    </div>
    <div style="display:flex;align-items:center;gap:.4rem;flex-shrink:0">
      ${badge}
      <svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2" style="color:var(--text-3)"><path d="M6 4l4 4-4 4"/></svg>
    </div>
  </div>`;
}

// ── Compliance card builder ────────────────────────────────────────────────────
function _buildComplianceCard(c) {
  const score      = Math.round(c.score || 0);
  const npi        = c.npi || '';
  const name       = c.provider_name || c.name || 'Unknown provider';
  const phone      = c.contact_phone || '';
  const locCount   = c.location_count || 1;
  const chips      = (c.rationale_chips || []).slice(0, 5);
  const isGhost    = c.association_type === 'ghost_billing';
  const isHighConf = score >= 65;
  const action     = c.action || 'pending';
  const cardId     = `cc-${npi}`;

  const scoreColor = score >= 65 ? 'var(--red,#dc2626)' : score >= 45 ? 'var(--amber,#d97706)' : 'var(--text-3)';
  const borderColor= isGhost ? 'var(--red,#dc2626)' : 'var(--amber,#d97706)';

  const chipHtml = chips.map(ch =>
    `<span style="display:inline-block;font-size:.63rem;padding:.1rem .4rem;border-radius:9px;background:var(--grey-bg);border:1px solid var(--border);color:var(--text-3);white-space:nowrap">${esc(ch)}</span>`
  ).join('');

  const actionBadge = action !== 'pending' ? `
    <span style="font-size:.63rem;padding:.1rem .4rem;border-radius:9px;border:1px solid var(--border);color:var(--text-3);white-space:nowrap;background:var(--bg)">
      ${action === 'moved_to_roster' ? '✓ Moved to roster' :
        action === 'contact_created' ? '📬 Outreach created' :
        action === 'ignored'         ? 'Ignored'            :
        action === 'dismissed'       ? 'Dismissed'          : action}
    </span>` : '';

  const locLine = locCount > 1
    ? `<span style="font-size:.72rem;color:var(--text-3)">Seen at ${locCount} location${locCount!==1?'s':''}</span>`
    : '';

  const phoneHint = phone
    ? `<span style="font-size:.72rem;color:var(--text-3)" title="NPPES phone (use as contact hint)">📞 ${esc(phone)}</span>`
    : '';

  const rationale = c.roster_rationale || '';

  return `<div id="${cardId}" style="border:1px solid var(--border);border-left:3px solid ${borderColor};border-radius:8px;background:var(--bg);overflow:hidden">
    <!-- Card header row -->
    <div style="display:flex;align-items:center;gap:.5rem;padding:.5rem .75rem;cursor:pointer" onclick="_toggleComplianceCard('${cardId}')">
      <div style="flex:1;min-width:0">
        <div style="display:flex;align-items:center;gap:.4rem;flex-wrap:wrap">
          <span style="font-size:.8125rem;font-weight:600;color:var(--text)">${esc(name)}</span>
          <span style="font-size:.7rem;font-family:monospace;color:var(--text-3)">${esc(npi)}</span>
          ${actionBadge}
        </div>
        <div style="display:flex;align-items:center;gap:.5rem;flex-wrap:wrap;margin-top:.2rem">
          ${chipHtml}
          ${locLine}
          ${phoneHint}
        </div>
      </div>
      <!-- Score badge -->
      <div style="flex-shrink:0;text-align:center;min-width:44px">
        <div style="font-size:1rem;font-weight:700;color:${scoreColor};line-height:1">${score}%</div>
        <div style="font-size:.6rem;color:var(--text-3);text-transform:uppercase;letter-spacing:.05em">confidence</div>
      </div>
      <!-- Expand toggle -->
      <div class="cc-chevron-${cardId}" style="flex-shrink:0;color:var(--text-3);transition:transform .15s">
        <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2"><path d="M4 6l4 4 4-4"/></svg>
      </div>
    </div>

    <!-- Expanded detail (hidden by default) -->
    <div id="${cardId}-detail" style="display:none;border-top:1px solid var(--border);padding:.5rem .75rem">
      ${rationale ? `<div style="font-size:.78rem;color:var(--text-2);margin-bottom:.5rem">${esc(rationale)}</div>` : ''}
      ${(c.locations||[]).length > 0 ? `<div style="font-size:.72rem;color:var(--text-3);margin-bottom:.35rem">
        <strong style="color:var(--text-2)">Seen at:</strong>
        ${(c.locations||[]).map(l => esc(l.location_address||l.location_id||'')).join(' · ')}
      </div>` : ''}
      ${phone ? `<div style="font-size:.72rem;color:var(--text-3);margin-bottom:.5rem">
        <strong style="color:var(--text-2)">Phone hint (NPPES):</strong> ${esc(phone)}
        <span style="opacity:.6"> — enter updated contact info when creating outreach task</span>
      </div>` : ''}

      <!-- Action buttons -->
      ${action === 'pending' ? `<div style="display:flex;gap:.4rem;flex-wrap:wrap;margin-top:.35rem">
        <button onclick="_complianceAction('${npi}','moved_to_roster')" style="font-size:.72rem;padding:.3rem .65rem;border-radius:6px;border:1px solid var(--border);background:var(--bg);cursor:pointer;color:var(--text-2);white-space:nowrap">✓ Move to roster</button>
        <button onclick="_complianceAction('${npi}','contact_created')" style="font-size:.72rem;padding:.3rem .65rem;border-radius:6px;border:1px solid var(--border);background:var(--bg);cursor:pointer;color:var(--text-2);white-space:nowrap">📬 Create outreach task</button>
        <button onclick="_complianceAction('${npi}','ignored')" style="font-size:.72rem;padding:.3rem .65rem;border-radius:6px;border:1px solid var(--border);background:var(--bg);cursor:pointer;color:var(--text-3);white-space:nowrap">Ignore</button>
        <button onclick="_complianceAction('${npi}','dismissed')" style="font-size:.72rem;padding:.3rem .65rem;border-radius:6px;border:1px solid var(--border);background:var(--bg);cursor:pointer;color:var(--text-3);white-space:nowrap">Dismiss</button>
      </div>` : `<div style="margin-top:.35rem;display:flex;gap:.4rem">
        <button onclick="_complianceAction('${npi}','pending')" style="font-size:.72rem;padding:.3rem .65rem;border-radius:6px;border:1px solid var(--border);background:var(--bg);cursor:pointer;color:var(--text-3)">↩ Reset to pending</button>
      </div>`}
    </div>
  </div>`;
}

function _toggleComplianceCard(cardId) {
  const detail  = document.getElementById(cardId + '-detail');
  const chevron = document.querySelector('.' + 'cc-chevron-' + cardId);
  if (!detail) return;
  const open = detail.style.display !== 'none';
  detail.style.display = open ? 'none' : 'block';
  if (chevron) chevron.style.transform = open ? '' : 'rotate(180deg)';
}

async function _complianceAction(npi, action) {
  const candidates = window._complianceCandidates || [];
  const candidate  = candidates.find(c => c.npi === npi || c.npi === npi.padStart(10, '0'));
  if (!candidate) return;

  // Optimistic UI update
  candidate.action = action;
  const cardId = 'cc-' + npi;
  const card   = document.getElementById(cardId);

  // Contact action: show a small inline task prompt
  if (action === 'contact_created') {
    const contactNote = prompt(
      `Create outreach task for ${candidate.provider_name || npi}.\n\n` +
      `Enter contact info (phone/email) or leave blank for a generic task:`,
      candidate.contact_phone || ''
    );
    if (contactNote === null) return; // cancelled
    candidate.action_notes = contactNote;
  }

  const promote = action === 'moved_to_roster';
  const skillBase = window.API || '/api/v1';
  // Derive skill URL from API base — use credentialing skill endpoint
  const skillUrl  = skillBase.replace('/api/v1', '').replace('/chat', '') || '';

  // Find finding_id if we have it, otherwise need to look it up
  // For now use the NPI as a fallback key via org summary (best-effort PATCH)
  const orgName = window._runData?.org_name || '';

  try {
    // We don't have finding IDs in the frontend cache, so patch via a
    // lookup endpoint instead — hit the org findings list to find the ID
    const findingsUrl = `${skillUrl}/compliance/${encodeURIComponent(orgName)}/findings`;
    const resp = await fetch(findingsUrl + `?run_id=${encodeURIComponent(window._complianceRunId||'')}`, { method: 'GET' });
    if (resp.ok) {
      const data = await resp.json();
      const finding = (data.findings || []).find(f => f.npi === npi || f.npi === npi.padStart(10,'0'));
      if (finding) {
        await fetch(`${skillUrl}/compliance/finding/${finding.id}/action`, {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            action,
            action_notes: candidate.action_notes || null,
            promote_to_roster: promote,
          }),
        });
      }
    }
  } catch (e) {
    console.warn('compliance action persist failed:', e);
  }

  // Re-render card in place
  if (card) {
    const newCard = document.createElement('div');
    newCard.innerHTML = _buildComplianceCard(candidate);
    const built = newCard.firstElementChild;
    if (built) {
      card.replaceWith(built);
      // Auto-expand detail so user sees the action confirmation
      _toggleComplianceCard('cc-' + npi);
    }
  }

  // Toast feedback
  const msgs = {
    moved_to_roster:  '✓ Provider queued for roster addition.',
    contact_created:  '📬 Outreach task created.',
    ignored:          'Provider marked as ignored.',
    dismissed:        'Provider dismissed.',
    pending:          'Reset to pending.',
  };
  if (typeof _showToast === 'function') _showToast(msgs[action] || 'Action saved.');
}


// ── Activity-bar piping ───────────────────────────────────────────────────────
// Emits server-side step_emit_log lines to the global feEmit activity bar,
// deduplicated per step so repeated render cycles don't re-emit old lines.
window._emittedStepLogs = window._emittedStepLogs || {};
function _pipeStepLogToTicker(stepId, lines) {
  if (!lines || !lines.length) return;
  const key = stepId;
  const already = window._emittedStepLogs[key] || 0;
  if (lines.length <= already) return;
  for (let i = already; i < lines.length; i++) {
    const raw = (lines[i] || '').trim();
    if (!raw) continue;
    const lvl = (raw.startsWith('✗') || /fail|error/i.test(raw)) ? 'error'
              : (raw.startsWith('△') || /warn/i.test(raw))        ? 'warn'
              : (raw.startsWith('✓') || /done|complete|ok/i.test(raw)) ? 'ok'
              : 'info';
    feEmit(raw, lvl);
  }
  window._emittedStepLogs[key] = lines.length;
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

  // Pipe step emit log lines to the bottom activity bar (deduplicated per step)
  const emitLog = draft.step_emit_log || [];
  _pipeStepLogToTicker(stepId, emitLog);

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
      // Step 5: Compliance — unrostered individuals audit
      // Shows providers with strong association to this org who are NOT on roster_truth.
      // Deduplicated by NPI, sorted by score, two sections: ghost billing / unrostered associate.
      const candidates = draft.compliance_candidates || [];
      const candidateCount = draft.compliance_candidate_count || candidates.length || 0;
      const ghostCount     = draft.compliance_ghost_billing_count || 0;
      const unrosteredCount= draft.compliance_unrostered_count || 0;
      const highConf       = draft.compliance_high_confidence_count || 0;
      const excluded       = draft.compliance_rostered_excluded || 0;
      const methodology    = draft.compliance_methodology || {};

      // Store globally for action callbacks
      window._complianceCandidates = candidates;
      window._complianceRunId      = draft.step_id ? (window._runId || '') : '';

      if (candidateCount > 0) {
        // ── Stats bar ─────────────────────────────────────────────────────────
        const statChips = [];
        if (ghostCount)      statChips.push({ val: ghostCount,      lbl: 'ghost billing suspects',    cls: 'red'   });
        if (unrosteredCount) statChips.push({ val: unrosteredCount, lbl: 'unrostered associates',     cls: 'amber' });
        if (highConf)        statChips.push({ val: highConf,        lbl: 'high confidence (≥65%)',    cls: 'amber' });
        if (excluded)        statChips.push({ val: excluded,        lbl: 'already rostered (filtered)', cls: 'green' });
        if (statChips.length) parts.push(statRow(statChips));

        // ── Threshold note ────────────────────────────────────────────────────
        if (highConf > 0) {
          parts.push(`<div style="border-left:3px solid var(--amber,#d97706);padding:.4rem .75rem;font-size:.78rem;color:var(--text-2);background:var(--grey-bg);border-radius:0 6px 6px 0;margin:.25rem 0">
            <strong style="color:var(--text)">⚡ ${highConf} high-confidence finding${highConf!==1?'s':''} auto-flagged</strong> — billing alerts created.
            Review and choose an action for each.
          </div>`);
        }

        // ── Section builder ───────────────────────────────────────────────────
        const _buildComplianceSection = (title, icon, items, borderColor) => {
          if (!items.length) return '';
          const cards = items.map(c => _buildComplianceCard(c)).join('');
          return `<div style="margin-top:.75rem">
            <div style="font-size:.75rem;font-weight:700;text-transform:uppercase;letter-spacing:.07em;color:var(--text-3);display:flex;align-items:center;gap:.4rem;margin-bottom:.4rem">
              <span style="display:inline-block;width:10px;height:10px;border-radius:50%;background:${borderColor}"></span>
              ${icon} ${esc(title)} <span style="font-weight:400;color:var(--text-3)">(${items.length})</span>
            </div>
            <div style="display:flex;flex-direction:column;gap:.375rem">${cards}</div>
          </div>`;
        };

        const ghostItems      = candidates.filter(c => c.association_type === 'ghost_billing');
        const unrosteredItems = candidates.filter(c => c.association_type !== 'ghost_billing');

        parts.push(_buildComplianceSection('Ghost Billing Suspects',      '💳', ghostItems,      'var(--red,#dc2626)'));
        parts.push(_buildComplianceSection('Unrostered Associates',        '🔗', unrosteredItems, 'var(--amber,#d97706)'));

        if (candidates.length > 50) {
          parts.push(`<div style="font-size:.72rem;color:var(--text-3);margin-top:.4rem;text-align:center">
            Showing top 50 of ${candidates.length} findings. Resolve high-confidence items first.
          </div>`);
        }
      } else if (draft.status === 'complete' || draft.status === 'done') {
        // The compliance scan is considered to have run if:
        //   - draft.compliance_rostered_excluded > 0 (filter executed against roster_truth), OR
        //   - draft.compliance_methodology is present (new code path executed)
        // If neither is set, this is an old run that predates the compliance feature.
        const complianceWasPopulated = (draft.compliance_rostered_excluded > 0) || !!draft.compliance_methodology;
        if (complianceWasPopulated) {
          parts.push(`<div style="padding:.75rem 1rem;background:var(--grey-bg);border-radius:8px;border:1px solid var(--border);font-size:.8125rem;color:var(--text-2)">
            <strong style="color:var(--text)">✓ No compliance concerns found.</strong>
            All associated providers are either on the approved roster or have insufficient evidence of current affiliation.
            ${excluded ? `<br><span style="color:var(--text-3)">${excluded} already-rostered provider${excluded!==1?'s':''} filtered out.</span>` : ''}
          </div>`);
        } else {
          // Old run — compliance data was never collected
          parts.push(`<div style="padding:.75rem 1rem;background:var(--grey-bg);border-radius:8px;border:1px solid var(--border);font-size:.8125rem;color:var(--text-2)">
            <strong style="color:var(--text)">⚠ Compliance data not available for this run.</strong><br>
            This run was completed before the compliance audit feature was added.
            To see unrostered-individual findings, start a new pipeline run — Step 5 will
            automatically filter against the approved roster, deduplicate by NPI, and persist results.
          </div>`);
        }
      } else {
        parts.push(`<div class="coming-soon-box">
          <div style="font-size:1.1rem;margin-bottom:.5rem;opacity:.4">🔍</div>
          <div class="cs-title">Compliance Audit — Unrostered Individuals</div>
          <div class="cs-body">
            Complete Steps 1–4 to enable this audit. We'll cross-reference
            DOGE billing records, NPPES, and PML against your approved roster to
            surface providers associated with this org who should be credentialed.
          </div>
        </div>`);
      }

      setTimeout(() => { window._provData = draft.providers || []; }, 0);
      break;
    }

    case 'nppes_alignment': {
      const providers = draft.providers || [];
      const _rsu = window._rosterUploadState;
      const _isActive = ['uploading','parsing','cleaning'].includes(_rsu?.phase);

      // Default: Upload tab if no roster loaded yet; Workspace once data is ready
      if (_isActive && window._rosterTab !== 'upload') window._rosterTab = 'upload';
      if (!window._rosterTab) {
        window._rosterTab = (_rsu?.phase === 'done') ? 'workspace' : 'upload';
      }
      const _tab = window._rosterTab;

      // ── Sealed banner (when step is signed off) ──────────────────
      if (_isStepSealed('nppes_alignment')) {
        parts.push(_buildSealedBanner('nppes_alignment'));
      }

      // ── Tab bar — order: Upload → Workspace → Roster ─────────────
      const _pendingUpload = _rsu && _rsu.phase !== 'done' && _rsu.phase !== 'none';
      parts.push(`<div class="roster-tab-bar" id="rosterTabBar">
        <button class="roster-tab ${_tab==='upload'?'active':''}" onclick="rosterTabSwitch('upload')">
          ↑ Upload${_pendingUpload?`<span class="rt-badge">!</span>`:''}
        </button>
        <button class="roster-tab ${_tab==='workspace'?'active':''}" onclick="rosterTabSwitch('workspace')">
          Workspace
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
        // Load org-level dismissals from postgres on every workspace render so
        // dismissed dims are honoured across runs and page reloads.
        _loadOrgDismissals();
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
      _loadTaxTaskState();
      const taxAnalysis = draft.taxonomy_analysis || [];
      // Expose to the provider detail drawer (openTaxProviderDrawer uses this)
      window._taxAnalysisData = taxAnalysis;
      const nRestriction = draft.restriction_count || 0;
      const nGap         = draft.gap_count || 0;
      const nClean       = draft.clean_count || 0;
      const nAnalyzed    = draft.analyzed_count || 0;

      if (!nAnalyzed && status !== 'in_progress') {
        parts.push(`<div class="coming-soon-box">
          <div style="font-size:1.1rem;margin-bottom:.5rem;opacity:.4">🏷</div>
          <div class="cs-title">Taxonomy Optimization</div>
          <div class="cs-body">
            Awaiting analysis — complete NPPES alignment and PML validation first.
          </div>
          ${summary ? `<div style="margin-top:.75rem;font-size:.8rem;color:var(--text-3)">${esc(summary)}</div>` : ''}
        </div>`);
        break;
      }

      // step_emit_log lines already piped to activity bar via _pipeStepLogToTicker above

      // ── Stats bar ──────────────────────────────────────────────────────────
      const taskChip = _buildTaxTaskChipHtml ? _buildTaxTaskChipHtml() : '';
      parts.push(`
        <div style="display:flex;align-items:center;gap:.5rem;flex-wrap:wrap;margin-bottom:.75rem">
          ${nRestriction > 0 ? `<span class="step-status-chip red">✗ ${nRestriction} billing restriction${nRestriction!==1?'s':''}</span>` : ''}
          ${nGap > 0        ? `<span class="step-status-chip amber">⚠ ${nGap} enrollment gap${nGap!==1?'s':''}</span>` : ''}
          ${nClean > 0      ? `<span class="step-status-chip green">✓ ${nClean} clean</span>` : ''}
          ${nAnalyzed > 0 && nRestriction === 0 && nGap === 0 ? `<span class="step-status-chip green">All ${nAnalyzed} providers aligned</span>` : ''}
          ${taskChip}
          ${nRestriction > 0 || nGap > 0 ? `<button onclick="toggleTaxTaskDrawer(true)" style="margin-left:auto;display:flex;align-items:center;gap:.3rem;font-size:.72rem;font-weight:600;padding:.22rem .65rem;border-radius:6px;border:1px solid var(--indigo-border,#6366f1);background:var(--indigo-bg,#eef2ff);color:var(--indigo,#4f46e5);cursor:pointer">
            View Tasks →
          </button>` : ''}
        </div>`);

      // ── Provider cards (sorted by severity, keeping original index for drawer) ─
      if (taxAnalysis.length) {
        const indexed = taxAnalysis.map((a, i) => ({ a, i }));
        indexed.sort((x, y) => {
          const sev = r => r.result_type === 'restriction' ? (r.delta_billing_pct > 20 ? 0 : r.delta_billing_pct >= 5 ? 1 : 2) : r.result_type === 'gap_only' ? 3 : 4;
          return sev(x.a) - sev(y.a);
        });
        const cards = indexed.map(({ a, i }) => _buildTaxProviderCard(a, i)).join('');
        parts.push(`<div style="display:flex;flex-direction:column;gap:.5rem">${cards}</div>`);
      }

      // ── Rate comparison: coming soon ───────────────────────────────────────
      parts.push(`<div class="coming-soon-box" style="margin-top:.75rem">
        <div style="font-size:.85rem;font-weight:600;color:var(--text-3);margin-bottom:.2rem">Rate Optimization</div>
        <div style="font-size:.78rem;color:var(--text-3)">
          Taxonomy-level reimbursement rate comparison — coming soon.
        </div>
      </div>`);
      break;
    }

    case 'provider_summaries': {
      const extra = draft.extra_data || {};
      const summaries = extra.summaries || [];
      const total     = extra.total     || summaries.length;
      const clean     = extra.clean_count || 0;
      const risk      = extra.risk_count  || 0;
      const orgName   = (window.lastRun?.org_name || '').trim();

      // ── Live progress view when step is running ───────────────────────────
      if (status === 'running' || status === 'in_progress') {
        // Parse emit log for per-provider lines like "✓ 3/55 — Jane Doe (billable)"
        const emitLines = (data?.orchestrator_state?.step_emit_log?.provider_summaries || []);
        const providerLines = emitLines.filter(l => /\d+\/\d+\s*—/.test(l));
        const lastLine = emitLines[emitLines.length - 1] || '';
        // Extract done/total from lines like "✓ 12/55 — …"
        const countMatch = lastLine.match(/(\d+)\/(\d+)/);
        const doneN  = countMatch ? parseInt(countMatch[1]) : providerLines.length;
        const totalN = countMatch ? parseInt(countMatch[2]) : (total || '?');
        const pct    = totalN > 0 ? Math.round((doneN / totalN) * 100) : 0;

        parts.push(`
          <div style="margin-bottom:1rem">
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:.4rem">
              <span style="font-size:.78rem;font-weight:600;color:var(--text)">
                <span class="spinner" style="width:10px;height:10px;border-width:1.5px;display:inline-block;vertical-align:middle;margin-right:4px"></span>
                Generating AI summaries… ${doneN}/${totalN}
              </span>
              <span style="font-size:.72rem;color:var(--text-3)">${pct}%</span>
            </div>
            <div style="height:6px;border-radius:3px;background:var(--border);overflow:hidden">
              <div style="height:100%;border-radius:3px;background:var(--indigo,#6366f1);width:${pct}%;transition:width .4s ease"></div>
            </div>
          </div>
          <div style="display:flex;flex-direction:column;gap:.25rem;max-height:14rem;overflow-y:auto">
            ${providerLines.slice(-20).reverse().map(l => {
              const isOk  = l.startsWith('✓');
              const isWarn= l.startsWith('⚠') || l.startsWith('△');
              const isRisk= l.startsWith('🚨');
              const col   = isRisk ? 'var(--red,#dc2626)' : isWarn ? 'var(--amber,#d97706)' : isOk ? 'var(--green,#16a34a)' : 'var(--text-2)';
              return `<div style="font-size:.72rem;color:${col};padding:.15rem 0;border-bottom:1px solid var(--border)">${esc(l)}</div>`;
            }).join('')}
          </div>
          ${doneN === 0 ? `<div style="font-size:.72rem;color:var(--text-3);font-style:italic;margin-top:.5rem">Fetching provider profiles from roster…</div>` : ''}
        `);
        break;
      }

      if (!total && status !== 'in_progress') {
        parts.push(`<div class="coming-soon-box">
          <div style="font-size:1.1rem;margin-bottom:.5rem;opacity:.4">✦</div>
          <div class="cs-title">Provider Summaries</div>
          <div class="cs-body">
            Run the credentialing pipeline through Step 6 to generate AI summaries.
          </div>
        </div>`);
        break;
      }

      // Stats bar
      parts.push(`
        <div style="display:flex;align-items:center;gap:.5rem;flex-wrap:wrap;margin-bottom:.85rem">
          ${total > 0     ? `<span class="step-status-chip green">✦ ${total} summaries generated</span>` : ''}
          ${clean > 0     ? `<span class="step-status-chip green">✓ ${clean} fully credentialed</span>` : ''}
          ${risk  > 0     ? `<span class="step-status-chip red">⚠ ${risk} flagged for review</span>` : ''}
          <a href="/roster?org=${encodeURIComponent(orgName)}" target="_blank"
            style="margin-left:auto;font-size:.72rem;font-weight:600;padding:.22rem .65rem;border-radius:6px;border:1px solid var(--indigo-border,#6366f1);background:var(--indigo-bg,#eef2ff);color:var(--indigo,#4f46e5);cursor:pointer;text-decoration:none">
            Open Roster Page →
          </a>
        </div>`);

      // Provider summary cards — clicking opens the roster drawer
      const BILL_COLOR = { billable:'green', warning:'amber', risk:'red', at_risk:'red', blocked:'red', inactive:'red' };
      const BILL_LABEL = { billable:'✓ Billable', warning:'⚠ Warning', risk:'⚠ At Risk', at_risk:'⚠ At Risk', blocked:'✗ Blocked', inactive:'✗ Inactive' };
      const cards = summaries.slice(0, 60).map(s => {
        const bill   = (s.billability || 'unknown').toLowerCase();
        const color  = BILL_COLOR[bill] || 'grey';
        const label  = BILL_LABEL[bill] || bill;
        const tasks  = s.open_tasks || 0;
        return `<div class="rt-card" style="padding:.55rem .75rem;cursor:pointer;animation:none"
            onclick="openRosterProviderByNpi('${esc(s.npi||'')}','${esc(orgName)}')"
            onmouseenter="this.style.background='var(--grey-bg)'"
            onmouseleave="this.style.background=''">
          <div style="display:flex;align-items:center;gap:.5rem;flex-wrap:wrap">
            <span style="font-size:.8rem;font-weight:600;color:var(--text)">${esc(s.name || s.npi || '—')}</span>
            <span style="font-size:.67rem;font-family:monospace;color:var(--text-3)">${esc(s.npi||'')}</span>
            <span class="step-status-chip ${color}" style="font-size:.65rem;margin-left:auto">${esc(label)}</span>
            ${tasks > 0 ? `<span style="font-size:.65rem;color:var(--text-3)">${tasks} task${tasks!==1?'s':''}</span>` : ''}
          </div>
          <div class="ai-oneliner-summary" id="sum-card-${esc(s.npi||'')}"></div>
        </div>`;
      }).join('');

      if (cards) parts.push(`<div style="display:flex;flex-direction:column;gap:.4rem">${cards}</div>`);

      // Lazy-load one-liners from already-stored summaries via the roster API
      if (summaries.length && orgName) {
        const _org = orgName;
        setTimeout(() => _injectSummaryOneLiners(summaries, _org), 300);
      }

      parts.push(`<div style="margin-top:.75rem;font-size:.75rem;color:var(--text-3);text-align:center">
        <a href="/roster?org=${encodeURIComponent(orgName)}" target="_blank"
          style="color:var(--indigo,#4f46e5);text-decoration:none;font-weight:500">
          Open full Roster page to view complete provider profiles and billing exposure →
        </a>
      </div>`);
      break;
    }

    case 'org_summary': {
      const extra   = draft.extra_data || {};
      const orgSum  = extra.org_summary || {};
      const metrics = orgSum.metrics || extra.metrics || {};
      const narrative = orgSum.narrative || extra.narrative || '';
      const orgName   = (window.lastRun?.org_name || '').trim();

      if (!narrative && !metrics.total && status !== 'in_progress') {
        parts.push(`<div class="coming-soon-box">
          <div style="font-size:1.1rem;margin-bottom:.5rem;opacity:.4">🏛</div>
          <div class="cs-title">Organization Summary</div>
          <div class="cs-body">
            Complete Steps 1–7 to generate the organization health report.
          </div>
        </div>`);
        break;
      }

      const total   = metrics.total    || 0;
      const bill    = metrics.billable  || 0;
      const billPct = metrics.billable_pct || (total > 0 ? Math.round(100*bill/total) : 0);
      const atRisk  = metrics.at_risk   || 0;
      const blocked = metrics.blocked   || 0;
      const pmlGaps = metrics.pml_gaps  || 0;
      const openT   = metrics.open_tasks|| 0;

      // Health score color
      const healthColor = billPct >= 90 ? 'green' : billPct >= 70 ? 'amber' : 'red';

      parts.push(`
        <div style="display:flex;align-items:stretch;gap:.75rem;flex-wrap:wrap;margin-bottom:1rem">
          <div style="flex:1;min-width:160px;background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:.75rem 1rem;text-align:center">
            <div style="font-size:1.8rem;font-weight:700;color:var(--${healthColor === 'green' ? 'green' : healthColor === 'amber' ? 'amber-text,#d97706' : 'red,#dc2626'})">${billPct}%</div>
            <div style="font-size:.72rem;color:var(--text-3);margin-top:.1rem">Fully Billable</div>
          </div>
          <div style="flex:1;min-width:100px;background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:.75rem 1rem;text-align:center">
            <div style="font-size:1.4rem;font-weight:700;color:var(--text)">${bill}<span style="font-size:.8rem;color:var(--text-3)">/${total}</span></div>
            <div style="font-size:.72rem;color:var(--text-3);margin-top:.1rem">Providers</div>
          </div>
          <div style="flex:1;min-width:100px;background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:.75rem 1rem;text-align:center">
            <div style="font-size:1.4rem;font-weight:700;color:${atRisk>0||blocked>0?'var(--red,#dc2626)':'var(--text)'}">${atRisk + blocked}</div>
            <div style="font-size:.72rem;color:var(--text-3);margin-top:.1rem">At-Risk / Blocked</div>
          </div>
          <div style="flex:1;min-width:100px;background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:.75rem 1rem;text-align:center">
            <div style="font-size:1.4rem;font-weight:700;color:${pmlGaps>0?'var(--amber-text,#d97706)':'var(--text)'}">${pmlGaps}</div>
            <div style="font-size:.72rem;color:var(--text-3);margin-top:.1rem">PML Gaps</div>
          </div>
          <div style="flex:1;min-width:100px;background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:.75rem 1rem;text-align:center">
            <div style="font-size:1.4rem;font-weight:700;color:${openT>0?'var(--amber-text,#d97706)':'var(--text)'}">${openT}</div>
            <div style="font-size:.72rem;color:var(--text-3);margin-top:.1rem">Open Tasks</div>
          </div>
        </div>`);

      // Narrative (markdown → HTML via _mdToHtml if available)
      if (narrative) {
        const htmlNarrative = (typeof _mdToHtml === 'function') ? _mdToHtml(narrative) : narrative.replace(/\n/g, '<br>');
        parts.push(`
          <div style="background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:1rem;margin-bottom:.75rem;font-size:.82rem;line-height:1.6">
            <div style="font-size:.68rem;font-weight:600;text-transform:uppercase;letter-spacing:.06em;color:var(--indigo,#4f46e5);margin-bottom:.5rem">✦ Mobius AI — Organization Health Assessment</div>
            ${htmlNarrative}
          </div>`);
      }

      parts.push(`<div style="margin-top:.5rem;font-size:.75rem;color:var(--text-3);text-align:center">
        <a href="/roster?org=${encodeURIComponent(orgName)}" target="_blank"
          style="color:var(--indigo,#4f46e5);text-decoration:none;font-weight:500">
          Open full Roster page →
        </a>
        ${orgSum.generated_at ? `<span style="margin-left:.75rem;opacity:.6">Generated ${new Date(orgSum.generated_at).toLocaleString()}</span>` : ''}
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


// ── Step 7: provider summary one-liner injection ──────────────────────────────

/**
 * After Step 7 cards render, populate the .ai-oneliner-summary divs with
 * the stored one-liners from the roster API (already in the list payload).
 * Falls back to sessionStorage cache if the list is already loaded.
 */
async function _injectSummaryOneLiners(summaries, orgName) {
  if (!summaries || !summaries.length || !orgName) return;
  try {
    const resp = await fetch(`/chat/roster-truth/${encodeURIComponent(orgName)}`);
    if (!resp.ok) return;
    const data = await resp.json();
    const byNpi = {};
    for (const p of (data.providers || [])) {
      const npi = p.npi_validated || p.npi_roster || '';
      if (npi && p.ai_summary_short) byNpi[npi] = p.ai_summary_short;
    }
    for (const s of summaries) {
      const npi = s.npi || '';
      const ol  = byNpi[npi] || ((() => {
        try { const c = sessionStorage.getItem(`mobius_summary_${s.roster_id || ''}`); return c ? JSON.parse(c).summary_short : ''; } catch(e) { return ''; }
      })());
      if (!ol) continue;
      const el = document.getElementById(`sum-card-${npi}`);
      if (el) {
        el.innerHTML = `<div style="font-size:.69rem;color:var(--indigo,#4f46e5);margin-top:.2rem;font-style:italic;opacity:.85;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">✦ ${esc(ol)}</div>`;
      }
    }
  } catch(e) { /* non-fatal */ }
}

/**
 * Navigate to /roster page and open the drawer for the given NPI.
 * If we are already on the roster page, opens the drawer directly.
 */
function openRosterProviderByNpi(npi, orgName) {
  if (!npi) return;
  const rosterBase = `/roster?org=${encodeURIComponent(orgName || '')}`;
  // If the openRosterProviderDrawer function is available we are on the pipeline page
  // which shares the drawer via pipeline-nppes.js — try to find the provider ID from
  // the already-loaded roster truth.
  if (typeof window._rosterTruth !== 'undefined' && window._rosterTruth) {
    const match = window._rosterTruth.find(p =>
      (p.npi_validated || p.npi_roster || '') === npi
    );
    if (match && typeof openRosterProviderDrawer === 'function') {
      openRosterProviderDrawer(match.id);
      return;
    }
  }
  // Fall back: open roster page with deep-link anchor
  window.open(`${rosterBase}&npi=${encodeURIComponent(npi)}`, '_blank');
}

