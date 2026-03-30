// ── NPI Selection (Step 2 / identify_org) ────────────────────
let _npiSelections = new Set();
let _npiDetailsCache = null;
let _npiPickerStepRunId = null; // tracks which run+step the cache belongs to

async function loadNpiDetails(npis, runData) {
  const wrap = document.getElementById('npiPickerWrap');
  if (!wrap) return;

  // Guard: only fetch once per run
  const cacheKey = `${runId}:identify_org`;
  if (_npiPickerStepRunId === cacheKey && _npiDetailsCache !== null) {
    renderNpiPicker(wrap, npis, _npiDetailsCache);
    return;
  }

  // Try to load full details from our endpoint
  let detail = null;
  try {
    const r = await fetch(`${API}/chat/credentialing-runs/${runId}/org-npis`);
    if (r.ok) detail = await r.json();
  } catch { /* ignore */ }

  _npiDetailsCache = detail;
  _npiPickerStepRunId = cacheKey;

  // Pre-select: previously persisted NPIs take priority, otherwise select all current
  _npiSelections = new Set();
  const prevNpis = (detail?.previously_persisted || []).map(p => p.npi);
  if (prevNpis.length) {
    prevNpis.forEach(n => _npiSelections.add(n));
  } else {
    npis.forEach(n => _npiSelections.add(n));
  }

  renderNpiPicker(wrap, npis, detail);
}

function _buildNpiCard(npi, d, isSelected, isManual) {
  const sel      = isSelected ? ' selected' : '';
  const isType1  = d?.entity_type === 'NPI-1';
  const isInact  = d?.status && d.status !== 'A';
  const statusBadge = isInact
    ? `<span style="font-size:.62rem;font-weight:700;padding:.1rem .4rem;border-radius:10px;background:var(--red-bg);color:var(--red);border:1px solid var(--red-border)">Inactive</span>`
    : d?.status === 'A' ? `<span class="npi-status-active">Active</span>` : '';
  const typeBadge = isType1
    ? `<span style="font-size:.6rem;font-weight:600;padding:.1rem .35rem;border-radius:10px;background:var(--amber-bg,#fffbeb);color:var(--amber,#d97706);border:1px solid var(--amber-border,#fde68a)" title="Individual provider NPI — unusual for org identity step">Type 1 · Individual</span>`
    : (d?.entity_type === 'NPI-2' ? `<span style="font-size:.6rem;font-weight:600;padding:.1rem .35rem;border-radius:10px;background:var(--grey-bg);color:var(--text-3);border:1px solid var(--border)">Type 2 · Org</span>` : '');
  const manualBadge = isManual
    ? `<span style="font-size:.6rem;font-weight:600;padding:.1rem .35rem;border-radius:10px;background:var(--green-bg);color:var(--green);border:1px solid var(--green-border)">+ Added manually</span>`
    : '';
  const name     = d?.name ? `<span class="npi-name">${esc(titleCase(d.name))}</span>` : `<span style="color:var(--text-3)">NPI ${esc(npi)}</span>`;
  const address  = d?.address  ? `<span>📍 ${esc(d.address)}</span>` : '';
  const phone    = d?.phone    ? `<span>📞 ${esc(d.phone)}</span>` : '';
  const taxonomy = d?.taxonomy ? `<span class="npi-taxonomy">${esc(d.taxonomy)}${d.taxonomy_code ? ` <code>${esc(d.taxonomy_code)}</code>` : ''}</span>` : '';
  const updated  = d?.last_updated ? `<span style="color:var(--text-3)">Updated ${esc(d.last_updated)}</span>` : '';
  const npiTaskCtx = JSON.stringify({ stepId:'identify_org', type:'npi', name: d?.name||npi, npi, issue: isInact ? 'NPI is inactive' : isType1 ? 'Type 1 (individual) — verify intent' : '', suggestedText:`Review org NPI ${npi}${d?.name?' — '+d.name:''}` });
  return `
    <div class="npi-card${sel}${isType1?' npi-card-warn':''}" onclick="toggleNpiSelection('${esc(npi)}', this)" id="npicard-${esc(npi)}">
      <div class="npi-card-check">
        <div class="npi-check-circle"><span class="npi-check-tick">✓</span></div>
      </div>
      <div class="npi-main-row" style="flex-wrap:wrap;gap:.25rem">
        ${name}
        <span class="npi-number">${esc(npi)}</span>
        ${statusBadge}${typeBadge}${manualBadge}
      </div>
      <div style="grid-column:3;grid-row:1/3;display:flex;align-items:flex-start;padding-top:2px">
        <button class="row-task-btn" onclick="openTaskPopover(JSON.parse(this.dataset.ctx),event);event.stopPropagation()" data-ctx="${esc(npiTaskCtx)}" title="Create task for this NPI">
          <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6"><rect x="3" y="2" width="10" height="12" rx="1.5"/><path d="M6 6h4M6 9h4M6 12h2"/></svg>
        </button>
      </div>
      <div class="npi-detail-row">
        ${address}${phone ? (address ? ' · ' : '') + phone : ''}
        ${taxonomy}${updated ? (taxonomy ? ' · ' : '') + updated : ''}
      </div>
    </div>`;
}

function renderNpiPicker(wrap, npis, detail) {
  const nppes    = detail?.nppes_details || {};
  const prevNpis = detail?.previously_persisted || [];
  const prevDate = detail?.prev_validated_at;
  const orgName  = detail?.org_name || lastRun?.org_name || '';
  const parts = [];

  // ── Completion summary (when revisiting a done step) ──────────────
  // If all org_npis in the draft are in prevNpis, show confirmed summary first
  if (prevDate && prevNpis.length) {
    const d = new Date(prevDate).toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });
    // Quiet confirmation — no green background; just a muted row with a checkmark
    parts.push(`<div style="display:flex;align-items:center;gap:.5rem;padding:.45rem .65rem;border:1px solid var(--border);border-radius:7px;background:var(--grey-bg);margin-bottom:.5rem;flex-wrap:wrap">
      <span style="color:var(--green);font-size:.8rem;flex-shrink:0">✓</span>
      <span style="font-size:.8rem;font-weight:600;color:var(--text-2)">Confirmed on ${d}</span>
      <div style="display:flex;flex-wrap:wrap;gap:.25rem;margin-left:.25rem">
        ${prevNpis.map(p => `<span style="font-size:.72rem;font-family:monospace;background:var(--surface);border:1px solid var(--border);border-radius:5px;padding:.1rem .4rem;color:var(--text-2)">${esc(p.npi)}${p.detail?.name ? ` · ${esc(titleCase(p.detail.name).substring(0,28))}` : ''}</span>`).join('')}
      </div>
      <span style="font-size:.72rem;color:var(--text-3);margin-left:auto">Review cards below to update</span>
    </div>`);
  }

  // ── Re-search toolbar ─────────────────────────────────────────────
  parts.push(`<div style="display:flex;align-items:center;gap:.4rem;margin-bottom:.5rem">
    <input id="npiReSearchInput" type="text" value="${esc(orgName)}" placeholder="Search by org name…"
      style="flex:1;font-size:.8rem;padding:.35rem .6rem;border:1px solid var(--border);border-radius:6px;background:var(--surface);color:var(--text);outline:none"
      onfocus="this.style.borderColor='var(--indigo)'" onblur="this.style.borderColor=''"
      onkeydown="if(event.key==='Enter')npiReSearch()">
    <button onclick="npiReSearch()" style="font-size:.75rem;font-weight:600;padding:.35rem .8rem;border-radius:6px;border:1px solid var(--border);background:var(--surface);color:var(--text-2);cursor:pointer;white-space:nowrap;transition:all .12s"
      onmouseover="this.style.borderColor='var(--indigo-border)';this.style.color='var(--indigo)'"
      onmouseout="this.style.borderColor='';this.style.color=''">🔍 Re-search</button>
  </div>`);

  if (!npis.length) {
    parts.push(`<div style="background:var(--grey-bg);border:1px dashed var(--border);border-radius:9px;padding:1rem;text-align:center">
      <div style="font-size:.875rem;font-weight:600;color:var(--text);margin-bottom:.3rem">No NPIs found for this name</div>
      <div style="font-size:.8rem;color:var(--text-2);margin-bottom:.75rem">Try a different org name in the search box above, or add an NPI manually below.</div>
    </div>`);
  } else {
    parts.push(`<div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:.35rem">
      <span style="font-size:.75rem;font-weight:600;color:var(--text-2)">Select the correct NPI(s) for this organization</span>
      <div style="display:flex;gap:.4rem">
        <button onclick="npiSelectAll()" style="font-size:.68rem;padding:.15rem .5rem;border-radius:10px;border:1px solid var(--border);background:none;color:var(--text-3);cursor:pointer">All</button>
        <button onclick="npiSelectNone()" style="font-size:.68rem;padding:.15rem .5rem;border-radius:10px;border:1px solid var(--border);background:none;color:var(--text-3);cursor:pointer">None</button>
      </div>
    </div>`);
    parts.push(`<div class="npi-pick-grid" id="npiPickGrid">`);
    for (const npi of npis) {
      const d = nppes[npi];
      const isType1 = d?.entity_type === 'NPI-1';
      parts.push(_buildNpiCard(npi, d, _npiSelections.has(npi), _manualNpis.has(npi)));
      // Auto-deselect Type 1 (individual) on first load if no previous selection
      if (isType1 && !_npiSelections.has(npi)) { /* already not selected */ }
    }
    parts.push(`</div>`);
    parts.push(`<p style="font-size:.75rem;color:var(--text-3);margin-top:.4rem">
      ${npis.length} NPI${npis.length !== 1 ? 's' : ''} found · <span id="selCount">${_npiSelections.size}</span> selected
    </p>`);
  }

  // ── Manual NPI entry ──────────────────────────────────────────────
  parts.push(`<div style="border-top:1px solid var(--border);margin-top:.75rem;padding-top:.65rem">
    <div style="font-size:.75rem;font-weight:600;color:var(--text-2);margin-bottom:.35rem">Add NPI manually</div>
    <div style="display:flex;gap:.4rem;align-items:center">
      <input id="manualNpiInput" type="text" maxlength="10" placeholder="10-digit NPI…"
        style="width:140px;font-family:monospace;font-size:.85rem;padding:.35rem .6rem;border:1px solid var(--border);border-radius:6px;background:var(--surface);color:var(--text);outline:none"
        onfocus="this.style.borderColor='var(--indigo)'" onblur="this.style.borderColor=''"
        oninput="this.value=this.value.replace(/\\D/g,'')"
        onkeydown="if(event.key==='Enter')npiManualAdd()">
      <button onclick="npiManualAdd()" id="manualNpiBtn"
        style="font-size:.75rem;font-weight:700;padding:.35rem .8rem;border-radius:6px;border:none;background:var(--indigo);color:#fff;cursor:pointer;white-space:nowrap;transition:opacity .12s"
        onmouseover="this.style.opacity='.85'" onmouseout="this.style.opacity=''">
        Fetch &amp; add
      </button>
      <span id="manualNpiStatus" style="font-size:.75rem;color:var(--text-3)"></span>
    </div>
  </div>`);

  wrap.innerHTML = parts.join('');
}

let _manualNpis = new Set();   // tracks which NPIs were added manually

function toggleNpiSelection(npi, card) {
  if (_npiSelections.has(npi)) {
    _npiSelections.delete(npi);
    card.classList.remove('selected');
  } else {
    _npiSelections.add(npi);
    card.classList.add('selected');
  }
  const cEl = document.getElementById('selCount');
  if (cEl) cEl.textContent = _npiSelections.size;
  const btn = document.getElementById('validateBtn');
  if (btn) btn.textContent = `Confirm ${_npiSelections.size} NPI${_npiSelections.size !== 1 ? 's' : ''} →`;
}

function npiSelectAll() {
  const grid = document.getElementById('npiPickGrid');
  if (!grid) return;
  grid.querySelectorAll('.npi-card').forEach(card => {
    const npi = card.id.replace('npicard-', '');
    if (npi) { _npiSelections.add(npi); card.classList.add('selected'); }
  });
  const cEl = document.getElementById('selCount');
  if (cEl) cEl.textContent = _npiSelections.size;
  const btn = document.getElementById('validateBtn');
  if (btn) btn.textContent = `Confirm ${_npiSelections.size} NPIs →`;
}

function npiSelectNone() {
  const grid = document.getElementById('npiPickGrid');
  if (!grid) return;
  grid.querySelectorAll('.npi-card').forEach(card => {
    const npi = card.id.replace('npicard-', '');
    if (npi) { _npiSelections.delete(npi); card.classList.remove('selected'); }
  });
  const cEl = document.getElementById('selCount');
  if (cEl) cEl.textContent = '0';
  const btn = document.getElementById('validateBtn');
  if (btn) btn.textContent = `Confirm 0 NPIs →`;
}

async function npiReSearch() {
  const input = document.getElementById('npiReSearchInput');
  const name  = (input?.value || '').trim();
  if (!name || !runId) return;

  const wrap = document.getElementById('npiPickerWrap');
  if (!wrap) return;
  wrap.innerHTML = `<div class="npi-loading"><span class="spinner"></span> Searching NPPES for "${esc(name)}"…</div>`;

  try {
    const r = await fetch(`${API}/chat/credentialing-runs/${runId}/org-npis?search_name=${encodeURIComponent(name)}`);
    if (!r.ok) throw new Error(await r.text());
    const detail = await r.json();
    _npiDetailsCache = detail;
    _npiSelections   = new Set();
    _manualNpis      = new Set();
    const npis = detail.current_npis || detail.npis || [];
    // Pre-select all by default for a fresh search
    npis.forEach(n => _npiSelections.add(n));
    renderNpiPicker(wrap, npis, detail);
    const btn = document.getElementById('validateBtn');
    if (btn) btn.textContent = `Confirm ${_npiSelections.size} NPI${_npiSelections.size !== 1 ? 's' : ''} →`;
  } catch (e) {
    wrap.innerHTML = `<div style="color:var(--red);font-size:.8rem;padding:.5rem">Search failed: ${esc(e.message)}</div>`;
  }
}

async function npiManualAdd() {
  const input  = document.getElementById('manualNpiInput');
  const status = document.getElementById('manualNpiStatus');
  const btn    = document.getElementById('manualNpiBtn');
  const npi    = (input?.value || '').trim();

  if (!npi || npi.length !== 10) {
    if (status) { status.textContent = 'Enter a 10-digit NPI'; status.style.color = 'var(--red)'; }
    return;
  }
  if (status) { status.textContent = 'Looking up…'; status.style.color = 'var(--text-3)'; }
  if (btn) btn.disabled = true;

  try {
    const r = await fetch(`${API}/chat/npi-lookup/${npi}`);
    if (!r.ok) {
      const err = await r.json().catch(() => ({}));
      throw new Error(err.detail || `HTTP ${r.status}`);
    }
    const d = await r.json();

    // Add to picker grid
    const grid = document.getElementById('npiPickGrid');
    if (!grid) {
      // No grid yet (empty state) — re-render full picker with this NPI
      const wrap = document.getElementById('npiPickerWrap');
      _npiDetailsCache = _npiDetailsCache || {};
      _npiDetailsCache.nppes_details = _npiDetailsCache.nppes_details || {};
      _npiDetailsCache.nppes_details[npi] = d;
      const allNpis = [...(_npiDetailsCache.current_npis || [])];
      if (!allNpis.includes(npi)) allNpis.push(npi);
      _npiDetailsCache.current_npis = allNpis;
      _npiSelections.add(npi);
      _manualNpis.add(npi);
      renderNpiPicker(wrap, allNpis, _npiDetailsCache);
    } else {
      // Append to existing grid
      if (document.getElementById(`npicard-${npi}`)) {
        if (status) { status.textContent = 'Already in list'; status.style.color = 'var(--text-3)'; }
        if (btn) btn.disabled = false;
        return;
      }
      if (_npiDetailsCache) {
        _npiDetailsCache.nppes_details = _npiDetailsCache.nppes_details || {};
        _npiDetailsCache.nppes_details[npi] = d;
      }
      _manualNpis.add(npi);
      _npiSelections.add(npi);
      const cardHtml = _buildNpiCard(npi, d, true, true);
      const tmp = document.createElement('div');
      tmp.innerHTML = cardHtml;
      grid.appendChild(tmp.firstElementChild);
    }

    // Update counts
    const cEl = document.getElementById('selCount');
    if (cEl) cEl.textContent = _npiSelections.size;
    const validateBtn = document.getElementById('validateBtn');
    if (validateBtn) validateBtn.textContent = `Confirm ${_npiSelections.size} NPI${_npiSelections.size !== 1 ? 's' : ''} →`;
    if (input) input.value = '';
    if (status) { status.textContent = `✓ Added: ${d.name || npi}`; status.style.color = 'var(--green)'; }
    setTimeout(() => { if (status) status.textContent = ''; }, 3000);
  } catch (e) {
    if (status) { status.textContent = `Not found: ${e.message}`; status.style.color = 'var(--red)'; }
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function validateNpiSelection() {
  if (!runId || !lastRun) return;
  if (_validationInFlight) return;
  if (lastRun.pending_step_id !== 'identify_org') return;
  const selected = Array.from(_npiSelections);
  if (!selected.length) { alert('Please select at least one NPI before continuing.'); return; }
  const btn = document.getElementById('validateBtn');
  if (btn) { btn.disabled = true; btn.textContent = 'Saving…'; }
  _validationInFlight = true;
  clearAutoAdvanceTimers();
  try {
    const r = await fetch(`${API}/chat/credentialing-runs/${runId}/validate`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        step_id: 'identify_org',
        validated_output: { org_npis: selected },
      }),
    });
    if (!r.ok) throw new Error(await r.text());
    const data = await r.json();
    _npiDetailsCache = null; _npiPickerStepRunId = null;
    render(data);
    schedulePoll(data);
  } catch (e) {
    if (btn) { btn.disabled = false; btn.textContent = 'Confirm selected NPIs →'; }
    alert('Validation failed: ' + e.message);
  } finally {
    _validationInFlight = false;
  }
}

