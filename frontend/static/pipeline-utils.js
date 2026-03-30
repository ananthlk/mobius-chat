// ── Shared rendering helpers ──────────────────────────────────
function statRow(stats) {
  const cells = stats.map(s => {
    const cls = s.cls ? ` ${s.cls}` : '';
    return `<div class="step-stat${cls}"><span class="step-stat-val">${esc(String(s.val))}</span><span class="step-stat-lbl">${esc(s.lbl)}</span></div>`;
  });
  return `<div class="step-stat-row">${cells.join('')}</div>`;
}

function fmt(n) {
  const num = Number(n);
  if (isNaN(num)) return String(n);
  return num >= 1000 ? num.toLocaleString('en-US', { maximumFractionDigits: 0 }) : num.toLocaleString('en-US', { maximumFractionDigits: 2 });
}

function csvPreview(csv, preferCols) {
  if (!csv || csv.startsWith('(')) return '';
  const lines = csv.trim().split('\n').filter(Boolean);
  if (lines.length < 2) return '';
  const allCols = lines[0].split(',');
  // Determine which columns to display — prefer the ones listed, fall back to first 5
  let cols = preferCols
    ? preferCols.filter(c => allCols.includes(c))
    : allCols.slice(0, 5);
  if (!cols.length) cols = allCols.slice(0, 5);
  const colIdxs = cols.map(c => allCols.indexOf(c));
  const rows = lines.slice(1, 8); // max 7 data rows
  return `<div style="overflow-x:auto;margin-top:.25rem">
    <table class="recon-table">
      <thead><tr>${cols.map(c => `<th>${esc(c.replace(/_/g,' '))}</th>`).join('')}</tr></thead>
      <tbody>${rows.map(row => {
        const cells = row.split(',');
        return `<tr>${colIdxs.map(i => `<td>${esc((cells[i] || '').trim())}</td>`).join('')}</tr>`;
      }).join('')}</tbody>
    </table>
    ${lines.length > 8 ? `<p style="font-size:.72rem;color:var(--text-3);padding:.2rem .1rem">…and ${lines.length - 8} more rows</p>` : ''}
  </div>`;
}

// ── Emit log helper — chat-stream style ───────────────────────
// Renders a very muted collapsible activity log. Does not fight
// for attention — meant to be glanced at, not read front-and-center.
// ── Frontend activity ticker ────────────────────────────────────
// feEmit(msg, level) — call from any interaction handler to surface
// an event in the bottom-of-screen activity strip.
// level: 'info' (default) | 'ok' | 'warn' | 'error'
window._feLog = [];

function feEmit(msg, level) {
  level = level || 'info';
  const entry = { ts: new Date(), msg, level };
  window._feLog.push(entry);

  const bar = document.getElementById('feTickerBar');
  if (!bar) return;
  bar.classList.add('fe-active');

  // Update the scrolling message in the head
  const msgEl = document.getElementById('feTickerMsg');
  if (msgEl) {
    msgEl.textContent = msg;
    msgEl.classList.remove('fe-flash');
    void msgEl.offsetWidth; // force reflow to restart animation
    msgEl.classList.add('fe-flash');
  }

  const countEl = document.getElementById('feTickerCount');
  if (countEl) countEl.textContent = window._feLog.length > 1 ? `${window._feLog.length} events` : '';

  // Prepend a new line into the expanded log body (newest at top)
  const body = document.getElementById('feTickerBody');
  if (body) {
    const icon = level === 'ok' ? '✓' : level === 'error' ? '✗' : level === 'warn' ? '△' : '·';
    const ts = entry.ts.toLocaleTimeString('en-US', { hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' });
    const div = document.createElement('div');
    div.className = `fe-line fe-${level}`;
    div.innerHTML = `<span class="fe-ts">${ts}</span><span class="fe-icon">${icon}</span><span class="fe-text">${esc(msg)}</span>`;
    body.insertBefore(div, body.firstChild);
  }
}

function _feTickerClick(e) {
  const bar  = document.getElementById('feTickerBar');
  const body = document.getElementById('feTickerBody');
  if (!bar) return;
  // Clicks inside the expanded log body don't close the bar
  if (body && body.contains(e.target) && bar.classList.contains('fe-open')) return;
  const isOpen = bar.classList.toggle('fe-open');
  const chevron = document.getElementById('feTickerChevron');
  if (chevron) chevron.textContent = isOpen ? '▴' : '▾';
}

function _feTickerShow(visible) {
  const bar = document.getElementById('feTickerBar');
  if (!bar) return;
  if (visible && window._feLog.length > 0) bar.classList.add('fe-active');
  else if (!visible) bar.classList.remove('fe-active', 'fe-open');
}

const _emitLogOpenIds = new Set();  // track which logs are expanded

function buildEmitLog(lines, logId) {
  if (!lines?.length) return '';
  const id    = logId || 'emitLog_' + Math.random().toString(36).slice(2,7);
  const isOpen = _emitLogOpenIds.has(id);
  const hasErr = lines.some(l => l.startsWith('✗'));
  const hasWarn = lines.some(l => l.toLowerCase().includes('warn') || l.toLowerCase().includes('timeout'));

  // Last meaningful line for the collapsed summary
  const lastLine = [...lines].reverse().find(l => l.trim()) || '';
  const lastMsg  = lastLine.replace(/^[✓✗·△\s]+/,'').substring(0,70);

  const statusDot = hasErr  ? `<span style="width:5px;height:5px;border-radius:50%;background:var(--red);flex-shrink:0;display:inline-block"></span>`
                 : hasWarn  ? `<span style="width:5px;height:5px;border-radius:50%;background:var(--amber);flex-shrink:0;display:inline-block"></span>`
                 : '';

  const bodyLines = lines.map((t, i) => {
    const isSuccess = t.startsWith('✓');
    const isFail    = t.startsWith('✗');
    const isPhase   = t.startsWith('──') || t.startsWith('---') || (t.endsWith('---') && t.length < 40);
    const cls       = isSuccess ? 'success' : isFail ? 'error' : '';
    const icon      = isSuccess ? '✓' : isFail ? '✗' : '·';
    const msg       = t.replace(/^[✓✗·△\-\s]+/,'');
    if (isPhase) return `<div class="sl-phase">${esc(msg || t)}</div>`;
    return `<div class="sl-line">
      <span class="sl-icon" style="${isSuccess?'color:var(--green)':isFail?'color:var(--red)':'color:var(--text-3);opacity:.5'}">${icon}</span>
      <span class="sl-msg ${cls}">${esc(msg)}</span>
    </div>`;
  }).join('');

  return `<div class="stream-log-wrap" id="wrap_${id}">
    <button class="stream-log-toggle" onclick="_toggleEmitLog('${id}')">
      ${statusDot}
      <span style="opacity:.7">Process log</span>
      <span style="font-size:.6rem;opacity:.5;font-variant-numeric:tabular-nums">${lines.length}</span>
      ${lastMsg && !isOpen ? `<span style="opacity:.45;max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">· ${esc(lastMsg)}</span>` : ''}
      <span style="font-size:.55rem;opacity:.4">${isOpen ? '▴' : '▾'}</span>
    </button>
    <div class="stream-log-body${isOpen?' open':''}" id="${id}">
      ${bodyLines}
    </div>
  </div>`;
}

function _toggleEmitLog(id) {
  const body = document.getElementById(id);
  if (!body) return;
  const isNowOpen = body.classList.toggle('open');
  if (isNowOpen) { _emitLogOpenIds.add(id); body.scrollTop = body.scrollHeight; }
  else _emitLogOpenIds.delete(id);
  // Refresh toggle button text (last-line preview)
  const wrap = document.getElementById('wrap_' + id);
  if (wrap) {
    const toggle = wrap.querySelector('.stream-log-toggle');
    if (toggle) {
      const lastSpan = toggle.querySelector('span:last-of-type');
      if (lastSpan) lastSpan.textContent = isNowOpen ? '▴' : '▾';
    }
  }
}

function _showToast(msg, duration = 2500) {
  let t = document.getElementById('mobiusToast');
  if (!t) {
    t = document.createElement('div');
    t.id = 'mobiusToast';
    t.style.cssText = 'position:fixed;bottom:2.5rem;left:50%;transform:translateX(-50%) translateY(80px);background:var(--mobius-text-primary);color:#fff;padding:.45rem 1.25rem;border-radius:8px;font-size:.8125rem;font-weight:600;z-index:9999;transition:transform .22s ease,opacity .22s ease;opacity:0;pointer-events:none;white-space:nowrap;box-shadow:0 4px 16px rgba(0,0,0,.25)';
    document.body.appendChild(t);
  }
  t.textContent = msg;
  t.style.transform = 'translateX(-50%) translateY(0)';
  t.style.opacity = '1';
  clearTimeout(t._timer);
  t._timer = setTimeout(() => {
    t.style.transform = 'translateX(-50%) translateY(80px)';
    t.style.opacity = '0';
  }, duration);
}

function simScore(a, b) {
  const norm = s => s.toLowerCase().replace(/[^a-z\s]/g,'').trim().split(/\s+/).sort().join(' ');
  const na = norm(a), nb = norm(b);
  if (!na || !nb) return 0;
  const sa = new Set(na.split(' ')), sb = new Set(nb.split(' '));
  const inter = [...sa].filter(x => sb.has(x)).length;
  return inter / Math.max(sa.size, sb.size);
}

function openReport() {
  if (!lastRun?.final_report_text) return;
  const w = window.open('', '_blank');
  w.document.write(`<pre style="font-family:sans-serif;white-space:pre-wrap;padding:2rem;max-width:800px;margin:auto">${esc(lastRun.final_report_text)}</pre>`);
}

function esc(s) {
  return String(s || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

// Normalize NPPES ALL-CAPS names for display.
// Preserves credential acronyms (LLC, MD, INC) in uppercase;
// lowercases English prepositions/articles except at word start.
const _titleAcronyms  = new Set(['LLC','INC','PA','PC','LLP','MD','DO','DDS','DMD','DPM','NP','RN','PLLC','DBA','II','III','IV','VI','VII','NPI']);
const _titleLowerWords = new Set(['OF','AND','AT','THE','A','AN','OR','FOR','IN','ON','BY','TO','WITH','FROM','AS','BUT','NOR','YET','SO']);
function titleCase(str) {
  if (!str) return '';
  // Already mixed-case (not NPPES all-caps): leave as-is
  if (str !== str.toUpperCase()) return str;
  const words = str.split(/\b/);
  return words.map((w, i) => {
    if (!/[A-Z]/.test(w)) return w;                 // punctuation / whitespace tokens
    if (_titleAcronyms.has(w)) return w;             // LLC, MD etc — stay upper
    if (_titleLowerWords.has(w) && i > 0) return w.toLowerCase(); // "of", "and" etc
    return w.charAt(0).toUpperCase() + w.slice(1).toLowerCase();
  }).join('');
}

/* ── Row-level task popover ──────────────────────────────────────── */

let _taskPopoverCtx = null;   // { stepId, type, name, npi, issue, extra }
let _taskPopoverPriority = 'medium';

function taskIconBtn(ctx) {
  // Returns an inline HTML button; ctx is a JS object serialised into the onclick
  const ctxJson = JSON.stringify(ctx).replace(/"/g, '&quot;');
  const hasTask = (_reconTasks || []).some(t =>
    t.rowKey === (ctx.type + ':' + (ctx.npi || ctx.name || '')) && !t.done
  );
  return `<button class="row-task-btn${hasTask ? ' has-task' : ''}"
    onclick="openTaskPopover(JSON.parse(this.dataset.ctx),event)"
    data-ctx="${ctxJson}"
    title="${hasTask ? 'Task already open — click to add another' : 'Create a task for this item'}">
    <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6">
      <rect x="3" y="2" width="10" height="12" rx="1.5"/>
      <path d="M6 6h4M6 9h4M6 12h2"/>
      <path d="M10.5 1v2.5" stroke-linecap="round"/>
      <path d="M5.5 1v2.5" stroke-linecap="round"/>
    </svg>
  </button>`;
}

function openTaskPopover(ctx, event) {
  event.stopPropagation();
  _taskPopoverCtx = ctx;
  _taskPopoverPriority = 'medium';

  // Resolve suggestion chips: explicit ctx.suggestions > _ALIGN_SUGGESTIONS by dim:flag > step templates
  const alignKey = ctx.dim && ctx.flag ? `${ctx.dim}:${ctx.flag}` : null;
  const chips = ctx.suggestions
    || (alignKey && _ALIGN_SUGGESTIONS[alignKey])
    || [];
  const templates = STEP_TASK_TEMPLATES[ctx.stepId] || [];
  // Pre-fill textarea: use first chip text if available, else ctx.suggestedText, else first template
  const firstSuggestion = chips[0] || ctx.suggestedText || (templates[0]?.text || '');

  const pop = document.getElementById('taskPopover');
  pop.querySelector('.task-popover-ctx').textContent =
    [ctx.name, ctx.npi ? `NPI ${ctx.npi}` : '', ctx.issue].filter(Boolean).join(' · ');
  pop.querySelector('#tpText').value = firstSuggestion;
  pop.querySelector('#tpWho').value  = '';
  pop.querySelector('#tpWhen').value = '';
  pop.querySelectorAll('.task-prio-btn').forEach(b => b.classList.toggle('sel', b.dataset.p === 'medium'));

  // Render suggestion chips
  const sugBox = document.getElementById('tpSuggestions');
  if (sugBox) {
    if (chips.length) {
      sugBox.innerHTML = chips.map((s, i) =>
        `<button class="tp-chip${i === 0 ? ' active' : ''}" onclick="tpPickChip(this)"
          style="font-size:.68rem;padding:.18rem .55rem;border-radius:20px;border:1px solid var(--border);
          background:${i===0?'var(--indigo)':'var(--grey-bg)'};color:${i===0?'#fff':'var(--text-2)'};
          cursor:pointer;white-space:nowrap;transition:all .1s">${s.replace(/</g,'&lt;')}</button>`
      ).join('');
      sugBox.style.display = 'flex';
    } else {
      sugBox.innerHTML = '';
      sugBox.style.display = 'none';
    }
  }

  // Position near the click
  const rect = event.target.getBoundingClientRect();
  const popW = 320;
  const popH = 300 + (chips.length ? 44 : 0);
  let top  = rect.bottom + 6;
  let left = rect.left - popW + 26;
  if (top + popH > window.innerHeight - 12) top = rect.top - popH - 6;
  if (left < 8) left = 8;
  if (left + popW > window.innerWidth - 8) left = window.innerWidth - popW - 8;
  pop.style.top  = top  + 'px';
  pop.style.left = left + 'px';
  pop.classList.add('open');
  setTimeout(() => pop.querySelector('#tpText').focus(), 60);
}

function tpPickChip(btn) {
  // Fill textarea with chip text and highlight the selected chip
  const sugBox = document.getElementById('tpSuggestions');
  if (sugBox) sugBox.querySelectorAll('.tp-chip').forEach(b => {
    const sel = b === btn;
    b.style.background = sel ? 'var(--indigo)' : 'var(--grey-bg)';
    b.style.color = sel ? '#fff' : 'var(--text-2)';
  });
  document.getElementById('tpText').value = btn.textContent;
  document.getElementById('tpText').focus();
}

function closeTaskPopover() {
  document.getElementById('taskPopover').classList.remove('open');
  _taskPopoverCtx = null;
}

function setTaskPriority(btn, p) {
  _taskPopoverPriority = p;
  btn.closest('.task-priority-btns').querySelectorAll('.task-prio-btn').forEach(b => b.classList.remove('sel'));
  btn.classList.add('sel');
}

function submitTaskPopover() {
  const text = (document.getElementById('tpText').value || '').trim();
  if (!text) { document.getElementById('tpText').focus(); return; }
  const ctx  = _taskPopoverCtx || {};
  const who  = (document.getElementById('tpWho').value  || '').trim();
  const when = (document.getElementById('tpWhen').value || '').trim();

  if (!_reconTasks) _reconTasks = [];
  const rowKey = ctx.type + ':' + (ctx.npi || ctx.name || '');
  _reconTasks.push({
    id:          `task-row-${Date.now()}`,
    stepId:      ctx.stepId || '',
    rowKey,
    source:      'user',
    type:        'user_created',
    phase:       0,
    providerIdx: ctx.providerIdx >= 0 ? ctx.providerIdx : -1,
    providerName:ctx.name || '',
    // Rich fields
    text,
    assignee:    who,
    deadline:    when,
    priority:    _taskPopoverPriority,
    // Row context for display
    rowContext: {
      type:  ctx.type  || 'item',
      name:  ctx.name  || '',
      npi:   ctx.npi   || '',
      issue: ctx.issue || '',
    },
    done: false,
    createdAt: Date.now(),
  });

  closeTaskPopover();
  renderSidebarTasks();
  _refreshTaskQueueFull();

  // Refresh the task icon button if providerIdx is known
  if (ctx.providerIdx >= 0) {
    const row = document.getElementById(`rr-${ctx.providerIdx}`);
    if (row) {
      const btn = row.querySelector('.row-task-btn');
      if (btn) btn.classList.add('has-task');
    }
  }

  // Brief sidebar flash
  const sl = document.getElementById('plTaskSection');
  if (sl) { sl.style.outline = '2px solid var(--indigo)'; setTimeout(() => sl.style.outline = '', 800); }
}

// Close popover on outside click
document.addEventListener('click', (e) => {
  const pop = document.getElementById('taskPopover');
  if (pop && pop.classList.contains('open') && !pop.contains(e.target)) closeTaskPopover();
});
