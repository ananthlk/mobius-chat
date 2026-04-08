/**
 * TaskManager — Unified task management widget.
 *
 * Two entry points:
 *   TaskManager.mount(selector, opts)         — page mount; fetches /chat/tasks
 *   TaskManager.mountInline(el, opts)         — inline chat mount; data pre-loaded
 *
 * opts for mount:
 *   { org, module, status, assignee, npi, run_id, allowCreate, allowResolve }
 *
 * opts for mountInline:
 *   { tasks, filters, allowCreate, allowResolve, onTaskChange }
 */

(function (global) {
  'use strict';

  // ── Helpers ──────────────────────────────────────────────────────────────

  function esc(s) {
    if (!s) return '';
    return String(s)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  function titleCase(s) {
    if (!s) return '';
    return String(s).replace(/\w\S*/g, t => t.charAt(0).toUpperCase() + t.slice(1).toLowerCase());
  }

  function fmtDate(iso) {
    if (!iso) return '';
    try {
      return new Date(iso).toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: '2-digit' });
    } catch { return iso; }
  }

  function isOverdue(deadline) {
    if (!deadline) return false;
    try {
      const d = new Date(deadline + 'T00:00');
      return d < new Date(new Date().toDateString());
    } catch { return false; }
  }

  const SEV_ORDER = { critical: 0, warning: 1, info: 2, low: 3, none: 4 };

  function sevClass(sev) {
    return `tm-sev tm-sev-${sev || 'low'}`;
  }

  function statusLabel(s) {
    return s === 'in_progress' ? 'In Progress' : titleCase(s || 'open');
  }

  // ── API helpers ───────────────────────────────────────────────────────────

  async function apiFetch(method, path, body) {
    const opts = { method, headers: { 'Content-Type': 'application/json' } };
    if (body !== undefined) opts.body = JSON.stringify(body);
    const r = await fetch(path, opts);
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    return r.json();
  }

  async function loadTasks(opts) {
    const params = new URLSearchParams();
    if (opts.org)      params.set('org_name', opts.org);
    if (opts.module)   params.set('module', opts.module);
    if (opts.status)   params.set('status', opts.status);
    if (opts.assignee) params.set('assignee', opts.assignee);
    if (opts.npi)      params.set('npi', opts.npi);
    if (opts.run_id)   params.set('run_id', opts.run_id);
    params.set('limit', '200');
    const data = await apiFetch('GET', `/chat/tasks?${params}`);
    return data.tasks || [];
  }

  // ── Render helpers ────────────────────────────────────────────────────────

  function renderTaskCard(task, { allowResolve, onAction, inline }) {
    const tid = esc(task.task_id || '');
    const shortId = tid.slice(0, 8);
    const sev = task.severity || 'low';
    const st = task.status || 'open';
    const isDone = st === 'resolved' || st === 'dismissed';

    const provMeta = task.provider_name
      ? `<span class="tm-meta-tag">${esc(titleCase(task.provider_name))}${task.npi ? ` · ${esc(task.npi)}` : ''}</span>`
      : '';
    const modMeta = task.source_module
      ? `<span class="tm-meta-tag">${esc(task.source_module)}</span>`
      : '';
    const assigneeMeta = task.assignee
      ? `<span>👤 ${esc(task.assignee)}</span>`
      : '';
    const deadlineMeta = task.deadline
      ? `<span class="${isOverdue(task.deadline) ? 'tm-meta-overdue' : ''}">${isOverdue(task.deadline) ? '⚠ overdue' : '📅'} ${fmtDate(task.deadline)}</span>`
      : '';

    const resolveBtn = allowResolve && !isDone
      ? `<button class="tm-btn tm-btn-primary" onclick="TaskManager._action('resolve','${shortId}',this)">Resolve</button>`
      : '';
    const dismissBtn = allowResolve && !isDone
      ? `<button class="tm-btn tm-btn-danger" onclick="TaskManager._action('dismiss','${shortId}',this)">Dismiss</button>`
      : '';
    const assignBtn = !isDone
      ? `<button class="tm-btn" onclick="TaskManager._toggleEdit('assign','${shortId}',this)">Assign</button>`
      : '';
    const noteBtn = !isDone
      ? `<button class="tm-btn" onclick="TaskManager._toggleEdit('note','${shortId}',this)">Note</button>`
      : '';

    return `
<div class="tm-card ${st}" data-tid="${shortId}" data-task-id="${tid}">
  <div class="tm-card-top">
    <span class="${sevClass(sev)}">${esc(sev)}</span>
    <span class="tm-card-text">${esc(task.text || '')}</span>
  </div>
  ${task.detail ? `<div class="tm-card-detail">${esc(task.detail)}</div>` : ''}
  <div class="tm-card-meta">
    ${provMeta}${modMeta}${assigneeMeta}${deadlineMeta}
    <span class="tm-meta-tag">${statusLabel(st)}</span>
    <span style="opacity:.4;font-family:monospace">${shortId}</span>
  </div>
  ${!isDone ? `
  <div class="tm-card-actions">
    ${resolveBtn}${dismissBtn}${assignBtn}${noteBtn}
  </div>
  <div class="tm-inline-edit" id="tm-edit-${shortId}" style="display:none"></div>
  ` : ''}
</div>`.trim();
  }

  function renderAddForm(opts) {
    if (!opts.allowCreate) return '';
    return `
<div class="tm-add-form">
  <input class="tm-add-input" id="tm-add-text" placeholder="New task description…" />
  <select class="tm-add-input" id="tm-add-sev" style="flex:0;min-width:90px">
    <option value="low">Low</option>
    <option value="info">Info</option>
    <option value="warning">Warning</option>
    <option value="critical">Critical</option>
  </select>
  <button class="tm-btn tm-btn-primary" onclick="TaskManager._addTask(this)">Add task</button>
</div>`.trim();
  }

  function renderWidget(el, tasks, opts) {
    const isInline = opts._inline || false;
    const filterText = (opts._filterText || '').toLowerCase();

    const filtered = tasks.filter(t => {
      if (!filterText) return true;
      const hay = [t.text, t.provider_name, t.npi, t.source_module, t.detail, t.assignee]
        .filter(Boolean).join(' ').toLowerCase();
      return hay.includes(filterText);
    });

    const open = filtered.filter(t => t.status === 'open');
    const inProg = filtered.filter(t => t.status === 'in_progress');
    const done = filtered.filter(t => t.status === 'resolved' || t.status === 'dismissed');

    const tabs = ['open', 'in_progress', 'resolved'];
    const tabLabels = { open: 'Open', in_progress: 'In Progress', resolved: 'Resolved' };
    const tabData = { open, in_progress: inProg, resolved: done };
    const activeTab = opts._activeTab || 'open';

    const tabsHtml = tabs.map(t => `
<button class="tm-tab${t === activeTab ? ' active' : ''}" onclick="TaskManager._switchTab('${opts._rootId}','${t}')">
  ${tabLabels[t]}<span class="tm-tab-count">${tabData[t].length}</span>
</button>`).join('');

    const listTasks = tabData[activeTab] || [];
    listTasks.sort((a, b) => (SEV_ORDER[a.severity] ?? 4) - (SEV_ORDER[b.severity] ?? 4));

    const cardsHtml = listTasks.length
      ? listTasks.map(t => renderTaskCard(t, { allowResolve: opts.allowResolve !== false, inline: isInline, onAction: null })).join('')
      : `<div class="tm-empty">No ${tabLabels[activeTab].toLowerCase()} tasks</div>`;

    const filterBar = !isInline ? `
<div class="tm-filters">
  <input class="tm-filter-input" placeholder="Filter tasks…" value="${esc(opts._filterText || '')}"
    oninput="TaskManager._filter('${opts._rootId}',this.value)" />
  ${opts.org ? `<span class="tm-meta-tag" style="font-size:.65rem">${esc(opts.org)}</span>` : ''}
  ${opts.module ? `<span class="tm-meta-tag" style="font-size:.65rem">${esc(opts.module)}</span>` : ''}
  <button class="tm-btn" style="margin-left:auto" onclick="TaskManager._export('${opts._rootId}')">⬇ CSV</button>
</div>`.trim() : '';

    const totalOpen = open.length + inProg.length;

    el.innerHTML = `
<div class="tm-root${isInline ? ' tm-inline' : ''}" id="${opts._rootId}">
  <div class="tm-header">
    <span class="tm-title"><span class="tm-title-icon">☑</span> Tasks${opts.org ? ` — ${esc(opts.org)}` : ''}</span>
    <div class="tm-header-actions">
      <span style="font-size:.65rem;color:var(--mobius-text-muted)">${totalOpen} open</span>
      ${!isInline ? `<button class="tm-btn" onclick="TaskManager._refresh('${opts._rootId}')">↺ Refresh</button>` : ''}
    </div>
  </div>
  <div class="tm-tabs">${tabsHtml}</div>
  ${filterBar}
  <div class="tm-list">${cardsHtml}</div>
  ${renderAddForm(opts)}
  <div class="tm-footer">
    <span>${filtered.length} task${filtered.length !== 1 ? 's' : ''}</span>
    <span style="opacity:.5">task-manager v1</span>
  </div>
</div>`.trim();
  }

  // ── State store (keyed by rootId) ──────────────────────────────────────

  const _state = {};

  function _getState(rootId) { return _state[rootId]; }

  function _setState(rootId, patch) {
    _state[rootId] = Object.assign(_state[rootId] || {}, patch);
  }

  function _rerender(rootId) {
    const s = _state[rootId];
    if (!s) return;
    renderWidget(s.el, s.tasks, s.opts);
  }

  // ── Public API ─────────────────────────────────────────────────────────

  const TaskManager = {

    mount(selector, opts) {
      const el = typeof selector === 'string' ? document.querySelector(selector) : selector;
      if (!el) { console.warn('TaskManager.mount: element not found', selector); return; }

      const rootId = 'tm-' + Math.random().toString(36).slice(2, 8);
      const mergedOpts = Object.assign({ allowCreate: true, allowResolve: true }, opts, { _rootId: rootId, _inline: false });

      _setState(rootId, { el, opts: mergedOpts, tasks: [] });

      el.innerHTML = '<div class="tm-root"><div class="tm-loading">Loading tasks…</div></div>';

      loadTasks(mergedOpts).then(tasks => {
        _setState(rootId, { tasks });
        _rerender(rootId);
      }).catch(err => {
        el.innerHTML = `<div class="tm-root"><div class="tm-error">Failed to load tasks: ${esc(String(err))}</div></div>`;
      });
    },

    mountInline(el, opts) {
      if (!el) return;
      const rootId = 'tm-' + Math.random().toString(36).slice(2, 8);
      const tasks = Array.isArray(opts.tasks) ? opts.tasks : [];
      const mergedOpts = Object.assign(
        { allowCreate: false, allowResolve: true },
        opts,
        { _rootId: rootId, _inline: true, _activeTab: 'open' }
      );
      _setState(rootId, { el, opts: mergedOpts, tasks, onTaskChange: opts.onTaskChange || null });
      renderWidget(el, tasks, mergedOpts);
    },

    // ── Internal event handlers (called from onclick in rendered HTML) ──

    _switchTab(rootId, tab) {
      const s = _state[rootId];
      if (!s) return;
      s.opts._activeTab = tab;
      _rerender(rootId);
    },

    _filter(rootId, text) {
      const s = _state[rootId];
      if (!s) return;
      s.opts._filterText = text;
      _rerender(rootId);
    },

    _refresh(rootId) {
      const s = _state[rootId];
      if (!s || s.opts._inline) return;
      loadTasks(s.opts).then(tasks => {
        _setState(rootId, { tasks });
        _rerender(rootId);
      });
    },

    _action(action, shortId, btn) {
      const rootId = btn.closest('.tm-root')?.id;
      if (!rootId) return;
      const s = _state[rootId];
      if (!s) return;
      const task = s.tasks.find(t => (t.task_id || '').startsWith(shortId));
      if (!task) return;

      btn.disabled = true;
      const path = action === 'resolve'
        ? `/chat/tasks/${task.task_id}/resolve`
        : `/chat/tasks/${task.task_id}/dismiss`;

      apiFetch('POST', path, {}).then(updated => {
        const idx = s.tasks.findIndex(t => t.task_id === task.task_id);
        if (idx !== -1) s.tasks[idx] = updated;
        _rerender(rootId);
        if (s.onTaskChange) s.onTaskChange(updated);
      }).catch(err => {
        btn.disabled = false;
        alert('Failed: ' + err.message);
      });
    },

    _toggleEdit(type, shortId, btn) {
      const rootId = btn.closest('.tm-root')?.id;
      if (!rootId) return;
      const editEl = document.getElementById(`tm-edit-${shortId}`);
      if (!editEl) return;

      if (editEl.style.display !== 'none') {
        editEl.style.display = 'none';
        editEl.innerHTML = '';
        return;
      }

      editEl.style.display = 'flex';
      if (type === 'assign') {
        editEl.innerHTML = `
<input class="tm-edit-input" placeholder="Assignee name or email…" id="tm-ei-${shortId}" />
<input class="tm-edit-input" type="date" placeholder="Deadline" id="tm-dl-${shortId}" style="flex:0;min-width:120px" />
<button class="tm-btn tm-btn-primary" onclick="TaskManager._saveEdit('assign','${shortId}',this)">Save</button>`;
      } else {
        editEl.innerHTML = `
<input class="tm-edit-input" placeholder="Add a note…" id="tm-ni-${shortId}" style="flex:1" />
<button class="tm-btn tm-btn-primary" onclick="TaskManager._saveEdit('note','${shortId}',this)">Save</button>`;
      }
    },

    _saveEdit(type, shortId, btn) {
      const rootId = btn.closest('.tm-root')?.id;
      if (!rootId) return;
      const s = _state[rootId];
      if (!s) return;
      const task = s.tasks.find(t => (t.task_id || '').startsWith(shortId));
      if (!task) return;

      let body = {};
      if (type === 'assign') {
        const assignee = document.getElementById(`tm-ei-${shortId}`)?.value?.trim();
        const deadline = document.getElementById(`tm-dl-${shortId}`)?.value;
        if (assignee) body.assignee = assignee;
        if (deadline) body.deadline = deadline;
      } else {
        const noteText = document.getElementById(`tm-ni-${shortId}`)?.value?.trim();
        if (!noteText) return;
        const now = new Date().toISOString();
        body.notes = [...(task.notes || []), { text: noteText, at: now }];
      }

      btn.disabled = true;
      apiFetch('PATCH', `/chat/tasks/${task.task_id}`, body).then(updated => {
        const idx = s.tasks.findIndex(t => t.task_id === task.task_id);
        if (idx !== -1) s.tasks[idx] = updated;
        _rerender(rootId);
        if (s.onTaskChange) s.onTaskChange(updated);
      }).catch(err => {
        btn.disabled = false;
        alert('Failed: ' + err.message);
      });
    },

    _addTask(btn) {
      const rootId = btn.closest('.tm-root')?.id;
      if (!rootId) return;
      const s = _state[rootId];
      if (!s) return;

      const textEl = btn.closest('.tm-add-form')?.querySelector('#tm-add-text');
      const sevEl  = btn.closest('.tm-add-form')?.querySelector('#tm-add-sev');
      const text   = textEl?.value?.trim();
      if (!text) { textEl && (textEl.focus()); return; }

      btn.disabled = true;
      apiFetch('POST', '/chat/tasks', {
        org_name: s.opts.org || '',
        source_module: s.opts.module || 'manual',
        text,
        severity: sevEl?.value || 'low',
      }).then(created => {
        s.tasks.unshift(created);
        _rerender(rootId);
        if (s.onTaskChange) s.onTaskChange(created);
      }).catch(err => {
        btn.disabled = false;
        alert('Failed to create task: ' + err.message);
      });
    },

    _export(rootId) {
      const s = _state[rootId];
      if (!s) return;
      const params = new URLSearchParams();
      if (s.opts.org)    params.set('org_name', s.opts.org);
      if (s.opts.module) params.set('module', s.opts.module);
      window.open(`/chat/tasks/export?${params}`, '_blank');
    },
  };

  global.TaskManager = TaskManager;
})(window);
