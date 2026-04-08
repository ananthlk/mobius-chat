// ── Stable task-ID helpers ─────────────────────────────────────
// Build a stable string key for a provider that survives page reload.
// Prefers source_provider_id (DB integer) → uploaded NPI → sanitised name.
function _taskKey(p) {
  if (p.id != null)    return String(p.id);
  if (p.npi_uploaded)  return `npi-${p.npi_uploaded}`;
  if (p.provider_name) return `name-${p.provider_name.toLowerCase().replace(/\W+/g,'_').slice(0,40)}`;
  return `unk-${Math.random().toString(36).slice(2,8)}`;
}

// localStorage key for dismissed task IDs, scoped to the current run
function _taskDoneKey() {
  return `mobius-task-done-${lastRun?.run_id || 'default'}`;
}

function _loadTaskDoneSet() {
  try {
    const raw = localStorage.getItem(_taskDoneKey());
    return raw ? new Set(JSON.parse(raw)) : new Set();
  } catch { return new Set(); }
}

function _persistTaskDone(taskId) {
  try {
    const s = _loadTaskDoneSet(); s.add(taskId);
    localStorage.setItem(_taskDoneKey(), JSON.stringify([...s]));
  } catch {}
}

function _clearPersistedTaskDone(taskId) {
  try {
    const s = _loadTaskDoneSet(); s.delete(taskId);
    localStorage.setItem(_taskDoneKey(), JSON.stringify([...s]));
  } catch {}
}

// ── Task list builder ──────────────────────────────────────────

function _buildReconTaskList() {
  const rs = window._rosterUploadState;
  if (!rs || rs.phase !== 'done' || !rs.report) return [];

  // Use backend-computed task list when available
  if (rs.report.recon_tasks?.length > 0) {
    // Append ghost tasks from _provData (frontend-only data source)
    const provData = window._provData || [];
    const ghosts = provData.filter(p => (p.sources||[]).includes('nppes') && !(p.sources||[]).includes('roster'));
    const ghostTasks = ghosts.map(g => ({
      id: `ghost-${g.npi||g.name}`, provider_idx: -1, provider_name: g.name || `NPI ${g.npi}`,
      type: 'ghost', severity: 'medium', phase: 2,
      text: 'Visible in NPPES but not on your roster',
      detail: 'Add to roster or request NPPES org address update.',
      done: false,
    }));
    // Normalise snake_case → camelCase for legacy rendering code
    const norm = t => ({
      id: t.id, providerIdx: t.provider_idx ?? t.providerIdx ?? -1,
      providerName: t.provider_name ?? t.providerName ?? '',
      type: t.type, severity: t.severity, phase: t.phase ?? 0,
      text: t.text, detail: t.detail, done: t.done ?? false,
    });
    return [...rs.report.recon_tasks.map(norm), ...ghostTasks.map(norm)];
  }

  // JS fallback for un-enriched / legacy reports
  const clean = rs.report.clean || [];
  const tasks = [];
  for (let i = 0; i < clean.length; i++) {
    const p = clean[i];
    if (p._decision === 'excluded') continue;
    const vr = p.latest_validation, decision = p._decision || null;
    const deactivated = vr && _isDeactivated(p);
    const npiValidated = vr?.npi_validated, npiUploaded = p.npi_uploaded;
    const conf = vr?.match_confidence || 0;
    const al = vr?.validation_details?.alignment || {};
    const alSum = al.summary || [];
    const pk = _taskKey(p), pname = p.provider_name;
    if (!vr || !npiValidated) {
      tasks.push({ id: `nomatch-${pk}`, providerIdx: i, providerName: pname, type: 'no_match', severity: 'medium', phase: 1, text: 'No NPI match found in NPPES', detail: 'Search NPPES or enter NPI manually.', done: false });
    } else if (decision === 'rejected') {
      tasks.push({ id: `rej-${pk}`, providerIdx: i, providerName: pname, type: 'rejected', severity: 'medium', phase: 1, text: 'All NPPES matches rejected — no NPI assigned', detail: 'Enter NPI manually or search NPPES.', done: false });
    } else if (npiUploaded && npiValidated && npiUploaded !== npiValidated) {
      tasks.push({ id: `mismatch-${pk}`, providerIdx: i, providerName: pname, type: 'npi_mismatch', severity: 'medium', phase: 1, text: `NPI mismatch: roster ${npiUploaded} vs NPPES ${npiValidated}`, detail: 'Confirm which NPI is correct and validate.', done: false });
    } else if (conf < 0.50 && decision !== 'validated') {
      tasks.push({ id: `lowconf-${pk}`, providerIdx: i, providerName: pname, type: 'low_conf', severity: 'low', phase: 1, text: `Low-confidence match (${Math.round(conf * 100)}%)`, detail: 'Review — may be the wrong person.', done: false });
    }
    if (vr && npiValidated) {
      if (deactivated || al.status?.flag === 'deactivated') tasks.push({ id: `deact-${pk}`, providerIdx: i, providerName: pname, type: 'deactivated', severity: 'high', phase: 2, text: 'NPI DEACTIVATED in NPPES', detail: 'Remove from active credentialing — NPI marked inactive by CMS.', done: false });
      if (alSum.includes('name')) { const na = al.name||{}; tasks.push({ id: `namedrift-${pk}`, providerIdx: i, providerName: pname, type: 'name_drift', severity: na.flag==='mismatch'?'medium':'low', phase: 2, text: na.flag==='mismatch'?'Name mismatch with NPPES':'Name differs slightly from NPPES', detail: `Roster: "${na.roster||''}" · NPPES: "${na.nppes||''}"`, done: decision==='validated' }); }
      if (alSum.includes('taxonomy')) { const ta = al.taxonomy||{}; tasks.push({ id: `taxmism-${pk}`, providerIdx: i, providerName: pname, type: 'taxonomy_mismatch', severity: 'low', phase: 2, text: 'Taxonomy/specialty does not align with NPPES', detail: `Roster: "${ta.roster||'—'}" · NPPES: "${ta.nppes||'—'}"`, done: decision==='validated' }); }
      if (alSum.includes('address')) { const aa = al.address||{}; tasks.push({ id: `addrmism-${pk}`, providerIdx: i, providerName: pname, type: 'address_mismatch', severity: 'low', phase: 2, text: 'State differs from NPPES practice location', detail: `Roster state: ${aa.roster||'—'} · NPPES: ${aa.nppes||'—'}`, done: decision==='validated' }); }
    }
    if (decision === 'validated' && !deactivated && alSum.filter(k => k !== 'name').length === 0) {
      tasks.push({ id: `ok-${pk}`, providerIdx: i, providerName: pname, type: 'confirmed', severity: 'none', phase: 0, text: 'NPI confirmed', detail: npiValidated||'', done: true });
    }
  }
  const provData = window._provData || [];
  provData.filter(p => (p.sources||[]).includes('nppes') && !(p.sources||[]).includes('roster')).forEach(g => {
    tasks.push({ id: `ghost-${g.npi||g.name}`, providerIdx: -1, providerName: g.name||`NPI ${g.npi}`, type: 'ghost', severity: 'medium', phase: 2, text: 'Visible in NPPES but not on your roster', detail: 'Add to roster or request NPPES org address update.', done: false });
  });
  return tasks;
}

function _getOrInitReconTasks() {
  // Load persisted dismissed IDs for this run (survives page reload)
  const dismissed = _loadTaskDoneSet();

  // Build fresh task list, pre-marking any previously dismissed IDs as done
  const fresh = _buildReconTaskList().map(t =>
    (dismissed.has(t.id) ? { ...t, done: true } : t)
  );

  if (!_reconTasks) { _reconTasks = fresh; return _reconTasks; }

  // Also merge in-memory done states from this session (user toggled mid-session)
  const doneMap = {};
  for (const t of _reconTasks) { if (t.done) doneMap[t.id] = true; }
  const merged = fresh.map(t => ({ ...t, done: doneMap[t.id] || t.done }));

  // Re-append user-created tasks (source:'user') — they are never auto-generated
  const userTasks = _reconTasks.filter(t => t.source === 'user');
  _reconTasks = [...merged, ...userTasks];
  return _reconTasks;
}

// ── Collect tracking tasks from promoted roster_truth providers ──────────────
// These are the open_tasks stored alongside each promoted provider record.
// We infer they're "tracking" simply because the provider has been promoted —
// no extra metadata needed on the task object itself.
function _getTrackingTasks() {
  const truth = window._rosterTruth || [];
  const result = [];
  for (const p of truth) {
    const openTasks = Array.isArray(p.open_tasks) ? p.open_tasks : [];
    for (const t of openTasks) {
      result.push({
        id:           `track-${p.id}-${t.dim || t.type || result.length}`,
        providerName: p.provider_name,
        npi:          p.npi_validated || p.npi_roster || '',
        text:         t.note || t.text || `${t.dim || t.type} — pending resolution`,
        detail:       t.detail || null,
        dim:          t.dim || t.type || '',
        severity:     t.severity || 'low',
        source:       t.source || 'system',
        promoted:     true,   // derived state — provider is in roster
        promotedAt:   p.promoted_at,
      });
    }
  }
  return result;
}

// ── Render task queue HTML ─────────────────────────────────────

function _renderTaskQueueHtml(tasks, focusSection) {
  // focusSection: 'open' | 'tracking' | null (show all)
  // We derive groupings from provider state, not task metadata

  const workspaceTasks = tasks.filter(t => !t.done && t.type !== 'confirmed');
  const doneTasks      = tasks.filter(t =>  t.done || t.type === 'confirmed');
  const trackingTasks  = _getTrackingTasks();

  const openCount     = workspaceTasks.length;
  const trackingCount = trackingTasks.length;
  const doneCount     = doneTasks.length;

  const severityOrder = { high: 0, medium: 1, low: 2, none: 3 };
  workspaceTasks.sort((a, b) => (severityOrder[a.severity] || 3) - (severityOrder[b.severity] || 3));

  const dimLabel = { name: 'Name', taxonomy: 'Taxonomy', address: 'Address', zip: 'Zip', status: 'Status' };

  // ── Task card renderer ──────────────────────────────────────
  const taskHtml = (t, isTracking) => {
    const jumpBtn    = !isTracking && t.providerIdx >= 0
      ? `<button class="task-jump" onclick="reconJumpToRow(${t.providerIdx})">View provider ↗</button>`
      : '';
    const dismissBtn = !isTracking && !t.done
      ? `<button class="task-jump" style="color:var(--text-3)" onclick="reconDismissTask('${t.id}')">Dismiss</button>`
      : '';
    const sevCls = t.priority === 'high' || t.severity === 'high' ? 'high'
                 : t.severity === 'medium' || t.priority === 'medium' ? 'medium'
                 : t.priority === 'low' || t.severity === 'low' ? 'low' : '';
    const metaParts = [];
    if (t.assignee) metaParts.push(`<span>👤 ${esc(t.assignee)}</span>`);
    if (t.deadline) {
      const d = new Date(t.deadline + 'T00:00');
      const today = new Date(); today.setHours(0,0,0,0);
      const overdue = d < today;
      metaParts.push(`<span style="color:${overdue?'var(--red)':'inherit'}">${overdue?'⚠':'📅'} ${d.toLocaleDateString('en-US',{month:'short',day:'numeric'})}</span>`);
    }
    if (isTracking && t.promotedAt) {
      const d = new Date(t.promotedAt);
      metaParts.push(`<span style="color:var(--text-3)">In roster since ${d.toLocaleDateString('en-US',{month:'short',day:'numeric'})}</span>`);
    }
    const metaHtml = metaParts.length ? `<div style="display:flex;gap:.5rem;font-size:.67rem;color:var(--text-3);margin:.1rem 0">${metaParts.join('')}</div>` : '';
    const providerCtx = t.providerName
      ? `<div style="font-size:.67rem;color:var(--text-3);font-weight:500;margin-bottom:.08rem">${esc(titleCase(t.providerName))}${t.npi ? ` <span style="font-family:monospace;opacity:.7">${esc(t.npi)}</span>` : ''}</div>`
      : (t.rowContext?.name ? `<div style="font-size:.67rem;color:var(--text-3);">${esc(titleCase(t.rowContext.name))}${t.rowContext.npi?` · ${t.rowContext.npi}`:''}</div>` : '');
    // dim tag for tracking tasks
    const dimTag = isTracking && t.dim
      ? `<span style="font-size:.58rem;font-weight:700;text-transform:uppercase;letter-spacing:.04em;padding:.08rem .28rem;border-radius:3px;background:var(--indigo-bg);color:var(--indigo);margin-left:.3rem">${esc(dimLabel[t.dim]||t.dim)}</span>`
      : (t.source === 'user' ? `<span style="font-size:.58rem;font-weight:700;text-transform:uppercase;padding:.08rem .28rem;border-radius:3px;background:#fef9c3;color:#854d0e;margin-left:.3rem">custom</span>` : '');
    return `<div class="task-item ${sevCls}${t.done?'done':''}" id="task-${t.id}"
      style="${isTracking ? 'opacity:.82;' : ''}">
      ${!isTracking ? `<input type="checkbox" class="task-cb" ${t.done?'checked':''} onchange="reconToggleTask('${t.id}',this.checked)">` : '<span style="width:13px;flex-shrink:0;display:flex;align-items:center;justify-content:center;font-size:.6rem;color:var(--text-3)">◎</span>'}
      <div style="flex:1;min-width:0">
        ${providerCtx}
        <div class="task-name">${esc(t.text)}${dimTag}</div>
        ${metaHtml}
        ${t.detail ? `<div class="task-detail">${esc(t.detail)}</div>` : ''}
        ${(jumpBtn || dismissBtn) ? `<div style="display:flex;gap:.5rem;margin-top:.2rem">${jumpBtn}${dismissBtn}</div>` : ''}
      </div>
    </div>`;
  };

  // ── Build sections ─────────────────────────────────────────
  const highOpen = workspaceTasks.filter(t => t.severity === 'high' || t.priority === 'high');
  const normalOpen = workspaceTasks.filter(t => !highOpen.includes(t));
  const userOpen   = normalOpen.filter(t => t.source === 'user');
  const sysOpen    = normalOpen.filter(t => t.source !== 'user');

  let body = '';

  // ── Open section (workspace providers) ────────────────────
  const showOpen = !focusSection || focusSection === 'open';
  if (showOpen) {
    if (openCount > 0) {
      if (highOpen.length) {
        body += `<div class="task-section-label" style="color:var(--red);margin-top:0">⚠ Needs attention · ${highOpen.length}</div>`;
        body += highOpen.map(t => taskHtml(t, false)).join('');
      }
      if (sysOpen.length) {
        body += `<div class="task-section-label" style="${highOpen.length?'margin-top:.5rem':'margin-top:0'}">Open · ${sysOpen.length}</div>`;
        body += sysOpen.map(t => taskHtml(t, false)).join('');
      }
      if (userOpen.length) {
        body += `<div class="task-section-label" style="margin-top:.4rem;color:var(--text-2)">Added by you · ${userOpen.length}</div>`;
        body += userOpen.map(t => taskHtml(t, false)).join('');
      }
    } else if (!focusSection) {
      body += `<div style="font-size:.72rem;color:var(--text-3);padding:.4rem .5rem">No open items — all providers reviewed.</div>`;
    }
  }

  // ── Tracking section (promoted providers with unresolved issues) ─
  if (trackingCount > 0) {
    const showTracking = !focusSection || focusSection === 'tracking';
    if (showTracking) {
      const trackingInner = trackingTasks.map(t => taskHtml(t, true)).join('');
      if (focusSection === 'tracking') {
        body += `<div class="task-section-label" style="margin-top:0">Monitoring · ${trackingCount}</div>`;
        body += trackingInner;
      } else {
        body += `<details style="margin-top:.55rem" ${focusSection==='tracking'?'open':''}>
          <summary style="cursor:pointer;list-style:none;-webkit-appearance:none">
            <div class="task-section-label" style="display:inline-flex;align-items:center;gap:.35rem">
              Monitoring · ${trackingCount}
              <span style="font-size:.6rem;color:var(--text-3);font-weight:400">Promoted providers with open issues — resolved when NPPES confirms</span>
            </div>
          </summary>
          <div style="margin-top:.3rem">${trackingInner}</div>
        </details>`;
      }
    }
  }

  // ── Done section ───────────────────────────────────────────
  if (doneTasks.length && (!focusSection || focusSection === 'open')) {
    body += `<details style="margin-top:.4rem"><summary style="cursor:pointer;list-style:none;-webkit-appearance:none"><div class="task-section-label" style="display:inline">Resolved · ${doneCount}</div></summary><div style="margin-top:.3rem">${doneTasks.map(t => taskHtml(t, false)).join('')}</div></details>`;
  }

  if (!openCount && !trackingCount && !doneCount) {
    body = `<div class="task-queue-empty">No tasks yet.<br>Upload a roster to see action items here.</div>`;
  }

  const totalOpen = openCount + trackingCount;
  return `
    <div class="task-queue" id="taskQueuePanel">
      <div class="task-queue-head">
        <span class="task-queue-title">📋 Tasks</span>
        <div style="display:flex;align-items:center;gap:.5rem">
          <span class="task-queue-counts">${totalOpen} open · ${doneCount} resolved</span>
          <button onclick="toggleTaskDrawer(false)" style="background:none;border:none;cursor:pointer;color:var(--indigo);font-size:.85rem;line-height:1;padding:0 .1rem" title="Close">×</button>
        </div>
      </div>
      <div class="task-queue-body" id="taskQueueBody">${body}</div>
      <div class="task-queue-foot">
        <button class="task-export-btn" onclick="exportReconTasks('csv')">Export CSV</button>
        <button class="task-export-btn" onclick="exportReconTasks('txt')">Copy text</button>
      </div>
    </div>`;
}

// ── Task queue actions ─────────────────────────────────────────

function reconToggleTask(taskId, checked) {
  if (!_reconTasks) return;
  const t = _reconTasks.find(t => t.id === taskId);
  if (!t) return;
  t.done = checked;
  feEmit((checked ? '✓ Task done — ' : '· Task reopened — ') + (t.providerName || taskId), checked ? 'ok' : 'info');
  // Persist in localStorage (offline fallback)
  if (checked) _persistTaskDone(taskId);
  else          _clearPersistedTaskDone(taskId);
  // Mirror to task-manager API (best-effort — taskId may be a stable UUID if tasks were bulk-imported)
  if (checked && t._apiId) {
    fetch(`/chat/tasks/${t._apiId}/resolve`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' }).catch(() => {});
  } else if (!checked && t._apiId) {
    fetch(`/chat/tasks/${t._apiId}`, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ status: 'open' }) }).catch(() => {});
  }
  // Update just the task item styling
  const el = document.getElementById(`task-${taskId}`);
  if (el) el.className = el.className.replace(/\bdone\b/, '').trim() + (checked ? ' done' : '');
  // Update row highlight (if row is visible in recon table)
  if (t.providerIdx >= 0) {
    const row = document.getElementById(`recon-row-${t.providerIdx}`);
    if (row) row.classList.toggle('recon-highlighted', checked);
  }
  _refreshTaskQueueHeader();
}

function reconDismissTask(taskId) {
  if (!_reconTasks) return;
  const t = _reconTasks.find(t => t.id === taskId);
  if (!t) return;
  t.done = true;
  feEmit('Task dismissed — ' + (t.providerName || t.text || taskId));
  _persistTaskDone(taskId);  // survive page reload
  // Mirror to task-manager API (best-effort)
  if (t._apiId) {
    fetch(`/chat/tasks/${t._apiId}/dismiss`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' }).catch(() => {});
  }
  // Full re-render of queue
  _refreshTaskQueueFull();
}

function reconJumpToRow(idx) {
  // Open the inline detail card in the recon table and scroll to it
  const detRow = document.getElementById(`recon-detail-${idx}`);
  if (detRow && detRow.style.display === 'none') reconToggleDetail(idx);
  const row = document.getElementById(`recon-row-${idx}`);
  if (row) row.scrollIntoView({ behavior: 'smooth', block: 'center' });
}

// ── Task drawer toggle ─────────────────────────────────────────
function toggleTaskDrawer(forceOpen, focusSection) {
  const drawer   = document.getElementById('taskDrawer');
  const backdrop = document.getElementById('taskDrawerBackdrop');
  if (!drawer) return;
  const willOpen = forceOpen !== undefined ? forceOpen : !drawer.classList.contains('open');
  drawer.classList.toggle('open', willOpen);
  if (backdrop) backdrop.classList.toggle('open', willOpen);
  if (willOpen) {
    if (focusSection) window._taskDrawerFocus = focusSection;
    _refreshTaskQueueFull();
  }
}

// Unified entry-point: opens the correct task drawer for the current step.
// PML alignment has its own drawer; all other steps share the general drawer.
function _openStepTaskDrawer() {
  const stepId = lastRun?.pending_step_id || window._viewStepId;
  if (stepId === 'pml_alignment') {
    togglePmlTaskDrawer(true);
  } else if (stepId === 'taxonomy_optimization') {
    toggleTaxTaskDrawer(true);
  } else {
    toggleTaskDrawer(true);
  }
}

function _refreshTaskQueueHeader() {
  if (!_reconTasks) return;
  const open = _reconTasks.filter(t => !t.done && t.type !== 'confirmed').length;
  const done = _reconTasks.filter(t =>  t.done || t.type === 'confirmed').length;
  const hdr = document.querySelector('#taskQueuePanel .task-queue-counts');
  if (hdr) hdr.textContent = `${open} open · ${done} done`;
}

function _refreshTaskQueueFull() {
  const drawer = document.getElementById('taskDrawer');
  if (!drawer) return;
  const tasks = _getOrInitReconTasks();
  const focus = window._taskDrawerFocus || null;
  drawer.innerHTML = _renderTaskQueueHtml(tasks, focus);
  window._taskDrawerFocus = null; // reset after first render
}

// Called from _rerenderRosterRow / rosterExcludeRow when a provider decision changes
function _syncTaskFromRosterAction(idx, decision) {
  if (!_reconTasks) return;
  // Mark relevant tasks for this provider as done/undone, and persist
  _reconTasks.forEach(t => {
    if (t.providerIdx !== idx) return;
    if (decision === 'validated' || decision === 'rejected') {
      if (t.type !== 'deactivated') { t.done = true; _persistTaskDone(t.id); }
    } else if (!decision) {
      if (t.type !== 'confirmed') { t.done = false; _clearPersistedTaskDone(t.id); }
    }
  });
  // Re-build from scratch to capture deactivated status
  _reconTasks = null;
  _getOrInitReconTasks();
  _refreshTaskQueueFull();
  // Also update roster score section
  const rosterSecTk = document.getElementById('rosterSection');
  if (rosterSecTk) rosterSecTk.innerHTML = _buildRosterSectionHtml();
}

