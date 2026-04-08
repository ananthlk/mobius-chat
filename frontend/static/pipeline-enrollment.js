// ── Payor Enrollment (Step 4) helpers ────────────────────────────────────────

// ── PML task state — in-memory cache (source of truth while page is open) ──
// Seeded from orchestrator_state.pml_task_state on each render; writes go to
// localStorage (instant) AND the DB via PATCH (durable, run-scoped).
let _pmlTaskState = { done: [], notes: {}, manual: [], dismissed: [], providerLocations: {} };

function _pmlTaskStateKey() { return `mobius-pml-ts-${lastRun?.run_id || 'default'}`; }

/** Load task state: DB value (embedded in lastRun) wins over localStorage. */
function _loadPmlTaskState() {
  const dbState = window.lastRun?.orchestrator_state?.pml_task_state;
  if (dbState && (dbState.done?.length || Object.keys(dbState.notes||{}).length || dbState.manual?.length)) {
    _pmlTaskState = { done: dbState.done||[], notes: dbState.notes||{}, manual: dbState.manual||[] };
    // sync localStorage to match DB
    try { localStorage.setItem(_pmlTaskStateKey(), JSON.stringify(_pmlTaskState)); } catch {}
    return;
  }
  // fall back to localStorage while DB hasn't been written yet
  try {
    const ls = JSON.parse(localStorage.getItem(_pmlTaskStateKey()) || 'null');
    if (ls) { _pmlTaskState = ls; return; }
  } catch {}
  // migrate legacy done-only key
  try {
    const legacyKey = `mobius-pml-task-done-${lastRun?.run_id || 'default'}`;
    const legacyDone = JSON.parse(localStorage.getItem(legacyKey) || '[]');
    if (legacyDone.length) _pmlTaskState.done = legacyDone;
  } catch {}
}

/** Persist task state to localStorage immediately and schedule a DB PATCH. */
let _pmlPatchTimer = null;
function _savePmlTaskState() {
  try { localStorage.setItem(_pmlTaskStateKey(), JSON.stringify(_pmlTaskState)); } catch {}
  // debounce DB write by 600 ms
  if (_pmlPatchTimer) clearTimeout(_pmlPatchTimer);
  _pmlPatchTimer = setTimeout(_flushPmlTaskState, 600);
}

async function _flushPmlTaskState() {
  const rid = lastRun?.run_id;
  if (!rid) return;
  try {
    await fetch(`${API}/chat/credentialing-runs/${encodeURIComponent(rid)}/pml-tasks`, {
      method: 'PATCH', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(_pmlTaskState),
    });
  } catch (e) {
    console.warn('PML task state PATCH failed (will retry on next change):', e.message);
  }
}

// ── Convenience accessors ────────────────────────────────────────────────────
function _loadPmlTaskDone()      { return new Set(_pmlTaskState.done || []); }
function _loadPmlTaskDismissed() { return new Set(_pmlTaskState.dismissed || []); }
function _persistPmlTaskDone(id) {
  if (!_pmlTaskState.done.includes(id)) _pmlTaskState.done.push(id);
  _savePmlTaskState();
}
function _clearPmlTaskDone(id) {
  _pmlTaskState.done = (_pmlTaskState.done || []).filter(x => x !== id);
  _savePmlTaskState();
}
function _dismissPmlRow(key) {
  if (!(_pmlTaskState.dismissed || []).includes(key)) {
    _pmlTaskState.dismissed = [...(_pmlTaskState.dismissed || []), key];
  }
  // also mark the task done so it disappears from the drawer
  const tid = `pml-${key}`;
  if (!_pmlTaskState.done.includes(tid)) _pmlTaskState.done.push(tid);
  _savePmlTaskState();
  feEmit(`PML row dismissed — ${key}`, 'ok');
}

// ── Stable provider key for task IDs ──────────────────────────
// Must include taxonomy_code so a provider with two taxonomy rows gets distinct keys.
function _pmlTaskKey(r) {
  const tax = (r.taxonomy_code || '').replace(/\W/g, '');
  const suffix = tax ? `-${tax}` : '';
  if (r.npi)           return `${r.npi}${suffix}`;
  if (r.provider_name) return `name-${r.provider_name.toLowerCase().replace(/\W+/g,'_').slice(0,40)}${suffix}`;
  return `unk-${Math.random().toString(36).slice(2,8)}`;
}

// ── Pull PML arrays from lastRun orchestrator state ───────────
function _pmlData() {
  const s = window.lastRun?.orchestrator_state || {};
  return {
    validated: s.pml_validated      || [],
    flagged:   s.pml_flagged        || [],
    missing:   s.missing_enrollment || [],
  };
}

// ── Readiness score ────────────────────────────────────────────
function _computePmlScore(validated, flagged, missing) {
  const enrolled = validated.length, withIssues = flagged.length, notIn = missing.length;
  const withPml  = enrolled + withIssues;
  const total    = withPml + notIn;
  if (!total) return null;

  const enrollCov = withPml / total;
  const validRate = withPml > 0 ? enrolled / withPml : 0;
  const taxOk  = [...validated,...flagged].filter(r => !(r.issues||[]).some(i => /taxon/i.test(i))).length;
  const taxRate  = withPml > 0 ? taxOk  / withPml : 0;
  const zipOk  = [...validated,...flagged].filter(r => !(r.issues||[]).some(i => /zip/i.test(i))).length;
  const zipRate  = withPml > 0 ? zipOk  / withPml : 0;

  const score = Math.max(0, Math.min(100, Math.round(
    enrollCov * 40 + validRate * 35 + taxRate * 15 + zipRate * 10
  )));
  const band = score >= 85 ? 'green' : score >= 65 ? 'amber' : 'red';
  const bandLabel = score >= 85 ? 'Payor ready' : score >= 65 ? 'Gaps to address' : 'Credentialing risk';
  return { score, band, bandLabel, enrolled, withIssues, notIn, total, withPml,
           enrollCov, validRate, taxRate, zipRate };
}

// ── Payor selector strip ───────────────────────────────────────
const _PAYORS = [
  { id: 'pml',      name: 'PML — FL Medicaid', sub: 'Provider Master List (AHCA)', live: true },
  { id: 'sunshine', name: 'Sunshine Health',    sub: 'Medicare Advantage, FL',       live: false },
  { id: 'united',   name: 'United Healthcare',  sub: 'Commercial + MA',              live: false },
  { id: 'humana',   name: 'Humana',             sub: 'Commercial + MA',              live: false },
  { id: 'aetna',    name: 'Aetna / CVS',        sub: 'Commercial',                   live: false },
  { id: 'cigna',    name: 'Cigna',              sub: 'Commercial',                   live: false },
];

function _buildPayorStripHtml(score) {
  return `<div class="payor-strip">${_PAYORS.map(p => {
    if (p.live) {
      const scHtml = score
        ? `<div class="payor-card-score ${score.band}">${score.score} <span style="font-size:.62rem;font-weight:400">${score.bandLabel}</span></div>` : '';
      return `<div class="payor-card pml-active">
        <span class="payor-card-badge live">Live</span>
        <div class="payor-card-name">${esc(p.name)}</div>
        <div class="payor-card-sub">${esc(p.sub)}</div>
        ${scHtml}
      </div>`;
    }
    return `<div class="payor-card payor-soon">
      <span class="payor-cs-hint">Data source → Rules → Validation → Score</span>
      <span class="payor-card-badge soon">Soon</span>
      <div class="payor-card-name">${esc(p.name)}</div>
      <div class="payor-card-sub">${esc(p.sub)}</div>
    </div>`;
  }).join('')}</div>`;
}

// ── Data source section ────────────────────────────────────────
function _buildPmlDataSrcHtml() {
  // Per-source freshness from the PML validation result stored in orchestrator state
  const freshness = lastRun?.orchestrator_state?.pml_source_freshness || {};

  function _srcDateLabel(isoDate) {
    if (!isoDate) return null;
    try {
      const d = new Date(isoDate + 'T00:00:00');
      const label = d.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });
      const ageDays = Math.floor((Date.now() - d.getTime()) / 86400000);
      const staleWarn = ageDays > 3 ? ` <span style="color:var(--amber,#d97706);font-size:.65rem">(${ageDays}d ago)</span>` : '';
      return `Updated ${label}${staleWarn}`;
    } catch {
      return null;
    }
  }

  const sources = [
    { code: 'PML', key: 'pml', name: 'Provider Master List',      desc: 'Active Medicaid enrollments and Medicaid Provider IDs' },
    { code: 'TML', key: 'tml', name: 'Taxonomy Master List',      desc: 'AHCA-approved taxonomy codes for Medicaid billing' },
    { code: 'PPL', key: 'ppl', name: 'Provider Participation List', desc: 'Participating providers by contract and program' },
  ];

  const rows = sources.map(s => {
    const dateLabel = _srcDateLabel(freshness[s.key]) || 'Date unknown';
    const hasDate   = !!freshness[s.key];
    const dot       = hasDate ? (freshness[s.key] < new Date(Date.now() - 4 * 86400000).toISOString().slice(0,10) ? 'stale' : 'live') : 'pending';
    return `<div class="pml-datasrc-item">
      <div class="pml-src-dot ${dot}"></div>
      <div>
        <div class="pml-src-name">${s.code} — <span style="font-weight:400">${s.name}</span></div>
        <div class="pml-src-desc">${s.desc}</div>
      </div>
      <div class="pml-src-ts" style="color:${hasDate ? 'var(--text-2)' : 'var(--text-3)'}">${dateLabel}</div>
    </div>`;
  }).join('');

  return rows + `<div style="margin-top:.5rem;font-size:.72rem;color:var(--text-3);line-height:1.5">
    PML data is sourced daily from AHCA. Use <strong>Refresh</strong> to re-validate against the latest snapshot.
  </div>`;
}

// ── Validation rules section ───────────────────────────────────
function _buildPmlRulesHtml() {
  // Rules derived from FL FLMMIS NPI Mapping Logic (effective Feb 15, 2024)
  // Source: FL-SMPLYCHA-CD-SMMC-002104-25-GRP581 NPI Initiative FAQ, Dec 3 2025
  // Mapping logic waterfall: Step 1 NPI → Step 2 Taxonomy → Step 3 ZIP+4 → Step 4 ZIP5 → Step 5 Address → Step 6 Default
  const rules = [
    {
      n: 1, step: 'Step 1',
      title: 'NPI active — enrolled in FLMMIS during date of service',
      desc: 'FLMMIS only considers Medicaid Provider IDs with Contract Date spans active during the submitted date of service. NPI must be active in NPPES and associated with at least one active Medicaid enrollment. If NPI has no active enrollment → DENIAL.',
      editCodes: '1901 (DENIAL)',
      tags: ['blk'],
    },
    {
      n: 2, step: 'Step 2',
      title: 'Taxonomy must be in TML and valid for provider type',
      desc: 'Submitted taxonomy is matched against the Taxonomy Master List (TML). Any taxonomy appropriately associated with the provider\'s specialty is accepted — not just the one on file. End-dated taxonomies are still considered. Health plans CANNOT alter provider-submitted taxonomy on encounters. Invalid or missing taxonomy → DENIAL.',
      editCodes: '1900–1926 (DENIAL), 1912 taxonomy missing',
      tags: ['blk'],
    },
    {
      n: 3, step: 'Step 3 → 4',
      title: 'ZIP+4 (9-digit) must match service location; ZIP5 is fallback',
      desc: 'FLMMIS first compares submitted ZIP+4 (9-digit) to service location ZIP+4 on file. If no 9-digit match, falls back to ZIP5. If ZIP5 also fails → DENIAL (edit 1110). ZIP+4 is critical: if a provider has multiple enrollments at the same ZIP5 and ZIP+4 is all zeros, FLMMIS defaults to the oldest Medicaid Provider ID (PAY edit 1980 — Agency may convert to DENIAL).',
      editCodes: '1110 BILLING ZIP INVALID (DENIAL); 1980 MULTIPLE ID DEFAULT (PAY → risk of DENIAL)',
      tags: ['blk', 'warn'],
    },
    {
      n: 4, step: 'Step 5',
      title: 'Address Line 1 must match when multiple ZIP5 hits exist',
      desc: 'When multiple Medicaid Provider IDs share the same ZIP5, FLMMIS compares submitted Address Line 1 to the service location address on file. Mismatch → DENIAL. Address on claim (Box 33 / Loop 2010AA) must exactly match the service location address in the provider file — not the pay-to address.',
      editCodes: '1120 BILLING SERVICE ADDRESS INVALID (DENIAL)',
      tags: ['blk'],
    },
    {
      n: 5, step: 'Step 6',
      title: 'Multiple enrollment default risk — duplicate enrollments',
      desc: 'When multiple Medicaid Provider IDs cannot be disambiguated by ZIP+4 or Address, FLMMIS defaults to the oldest contract date and posts PAY edit 1980. Agency reserves the right to change this to DENIAL. Resolve by: (a) updating ZIP+4 to the specific service location, (b) differentiating address line 1, or (c) removing duplicate enrollments.',
      editCodes: '1980–1989 (PAY edit, risk of future DENIAL)',
      tags: ['warn'],
    },
    {
      n: 6, step: 'Check',
      title: 'Valid Medicaid Provider ID must exist on file',
      desc: 'Each enrolled provider must have a Medicaid Provider ID (issued by AHCA) present and non-empty. If NPI, taxonomy, and ZIP all pass but no Medicaid ID exists, enrollment is incomplete.',
      editCodes: 'Enrollment incomplete',
      tags: ['blk'],
    },
    {
      n: 7, step: 'Check',
      title: 'No entity-type mismatch (org taxonomy on individual NPI)',
      desc: 'An individual provider NPI (Type 1) must not carry an organizational taxonomy code in the PML. FLMMIS uses the NUCC taxonomy classification to determine entity type. Billing an org taxonomy on an individual NPI will fail Step 2 taxonomy validation.',
      editCodes: '1906 TAXONOMY NOT VALID FOR BILLING (DENIAL)',
      tags: ['blk'],
    },
  ];
  const tagLabel = { blk: 'Blocking (DENIAL)', warn: 'Warning (PAY/Default risk)' };
  const tagCls   = { blk: 'blk', warn: 'warn' };

  return `
    <div style="font-size:.68rem;color:var(--text-3);margin-bottom:.45rem;display:flex;align-items:center;gap:.4rem">
      <span>Source: FLMMIS NPI Mapping Logic (eff. Feb 15 2024)</span>
      <span style="opacity:.5">·</span>
      <span style="font-style:italic">FL-SMPLYCHA-CD-SMMC-002104-25-GRP581, Dec 3 2025</span>
    </div>
    <div class="pml-rule-list">${rules.map(r => `
    <div class="pml-rule-item">
      <div class="pml-rule-num">${r.n}</div>
      <div style="flex:1">
        <div style="display:flex;align-items:center;gap:.4rem;margin-bottom:.12rem">
          <div class="pml-rule-title">${r.title}</div>
          <span style="font-size:.6rem;font-weight:600;color:var(--text-3);white-space:nowrap;border:1px solid var(--border);border-radius:3px;padding:.02rem .28rem">${r.step}</span>
        </div>
        <div class="pml-rule-desc">${r.desc}</div>
        <div style="font-size:.65rem;font-family:var(--mobius-font-mono,monospace);color:var(--text-3);margin-top:.18rem">Edit: ${r.editCodes}</div>
        <div class="pml-rule-tags">${r.tags.map(t => `<span class="pml-rule-tag ${tagCls[t]}">${tagLabel[t]}</span>`).join('')}</div>
      </div>
    </div>`).join('')}</div>`;
}

// ── Provider table helpers ─────────────────────────────────────
let _pmlFilter = 'all';
let _pmlQuery  = '';

function _pmlRowStatus(r) {
  // Prefer backend-computed display_status; fall back for legacy rows
  if (r.display_status) return r.display_status;
  if (r._missing) return 'missing';
  if (r.valid)    return 'enrolled';
  return 'flagged';
}

function _pmlProvRows(allRows) {
  const q = _pmlQuery.toLowerCase();
  const dismissed = _loadPmlTaskDismissed();

  // Sort by provider name first, then by NPI for stable ordering
  const sorted = [...allRows].sort((a, b) => {
    const na = (a.provider_name || a.npi || '').toUpperCase();
    const nb = (b.provider_name || b.npi || '').toUpperCase();
    return na < nb ? -1 : na > nb ? 1 : 0;
  });

  const shown = sorted.filter(r => {
    const key = _pmlTaskKey(r);
    if (dismissed.has(key)) return false;                  // hide dismissed rows
    if (_pmlFilter === 'enrolled' && _pmlRowStatus(r) !== 'enrolled') return false;
    if (_pmlFilter === 'flagged'  && _pmlRowStatus(r) !== 'flagged')  return false;
    if (_pmlFilter === 'missing'  && _pmlRowStatus(r) !== 'missing')  return false;
    if (q) {
      const h = `${r.provider_name||''} ${r.npi||''} ${r.medicaid_provider_id||''}`.toLowerCase();
      if (!h.includes(q)) return false;
    }
    return true;
  });
  if (!shown.length) return `<tr><td colspan="7" style="text-align:center;padding:1.25rem;font-size:.78rem;color:var(--text-3)">No providers match this filter.</td></tr>`;

  const done = _loadPmlTaskDone();
  return shown.map((r, _ri) => {
    const st    = _pmlRowStatus(r);
    const key   = _pmlTaskKey(r);
    const tid   = `pml-${key}`;
    const issues = r.issues || [];
    // Build edit code map for this row
    const _rowEditMap = {};
    (r.edit_codes || []).forEach(ec => {
      if (!_rowEditMap[ec.internal_code]) _rowEditMap[ec.internal_code] = ec;
    });
    const chips = issues.slice(0,3).map(i => {
      const blk = /zip|npi|medicaid.id|not.*in.*pml/i.test(i);
      const ec  = _rowEditMap[i.split(':')[0]];
      const editTag = ec
        ? `<span title="${esc(ec.description)}" style="font-size:.58rem;font-weight:700;opacity:.85;margin-left:.22rem">⚡${esc(ec.code)}</span>`
        : '';
      return `<span class="pml-issue-chip ${blk?'blocking':''}" style="display:inline-flex;align-items:center;gap:0">${esc(i)}${editTag}</span>`;
    }).join('') + (issues.length > 3 ? `<span class="pml-issue-chip">+${issues.length-3}</span>` : '');

    const badge = `<span class="pml-status-badge ${st}">${
      st==='enrolled'?'✓ Enrolled':st==='flagged'?'⚠ Flagged':'✗ Not enrolled'
    }</span>`;

    const isDone = done.has(tid);
    return `
      <tr class="pml-row-data pml-${st} mob-row-enter" id="pmlr-${esc(key)}"
        onclick="pmlToggleDetail('${esc(key)}')" style="cursor:pointer;animation-delay:${_ri*18}ms">
        <td>
          <div style="font-size:.78rem;font-weight:600;color:var(--text)">${esc(r.provider_name||'—')}</div>
          ${r.npi?`<div style="font-family:var(--mobius-font-mono,monospace);font-size:.68rem;color:var(--text-3)">${esc(r.npi)}</div>`:''}
        </td>
        <td style="font-family:var(--mobius-font-mono,monospace);font-size:.7rem;color:var(--text-2)">${esc(r.medicaid_provider_id||'—')}</td>
        <td style="font-size:.72rem;color:var(--text-2)">${esc(r.taxonomy_code||'—')}</td>
        <td style="font-size:.72rem;color:var(--text-2)">${esc(r.zip9||'—')}</td>
        <td>${badge}</td>
        <td>${chips||''}</td>
        <td style="text-align:right;font-size:.67rem;color:var(--text-3)">${isDone?'✓':'▾'}</td>
      </tr>
      <tr class="pml-detail-row" id="pmld-${esc(key)}" style="display:none">
        <td colspan="7">${_pmlDetailHtml(r, tid, isDone)}</td>
      </tr>`;
  }).join('');
}

function _pmlDetailHtml(r, tid, isDone) {
  const issues   = r.issues   || [];
  const warnings = r.warnings || [];
  const issueSet = new Set(issues.map(i => i.toLowerCase()));

  // Which fields have mismatches?
  const zipMismatch  = issueSet.has('zip_mismatch_location') || issueSet.has('zip_not_9_digits');
  const taxMismatch  = issueSet.has('entity_type_mismatch');
  const npiInvalid   = issues.some(i => /npi.*(not active|inactive|not.*nppes)/i.test(i));
  const midMissing   = issueSet.has('medicaid_id_missing') || issues.some(i => /medicaid.*id.*(not found|missing)/i.test(i));

  const fld = (lbl, val, cls='', extra='') => {
    const v = val || '—';
    const isEmpty = !val;
    const finalCls = isEmpty ? 'missing' : (cls || '');
    return `<div class="pml-field">
      <span class="pml-field-lbl">${lbl}</span>
      <span class="pml-field-val ${finalCls}">${esc(v)}</span>
      ${extra}
    </div>`;
  };

  // Human-readable labels for warning/issue codes
  const _PML_CODE_LABELS = {
    zip4_zeros_default_risk:          'ZIP+4 Zeros (PAY 1980)',
    address_missing_multiple_zip5:    'Address Missing — Multiple ZIP5 (DENIAL 1120)',
    address_mismatch_multiple_zip5:   'Address Mismatch — Multiple ZIP5 (DENIAL 1120)',
    multiple_enrollment_default_risk: 'Multiple Active Enrollments (PAY 1980)',
    npi_not_in_nppes:                 'NPI Not in NPPES',
    npi_deactivated_nppes:            'NPI Deactivated in NPPES',
    npi_inactive_pml:                 'PML Enrollment Inactive',
    taxonomy_missing:                 'Taxonomy Missing',
    taxonomy_not_medicaid_approved:   'Taxonomy Not Medicaid-Approved (TML)',
    entity_type_mismatch:             'Entity Type Mismatch',
    zip_not_9_digits:                 'ZIP Not 9 Digits',
    zip_mismatch_location:            'ZIP Mismatch — Service Location',
    medicaid_id_missing:              'Medicaid ID Missing',
  };
  const _fmtCode = raw => {
    const colonIdx = raw.indexOf(':');
    const code = colonIdx > 0 ? raw.slice(0, colonIdx).trim() : '';
    const desc = colonIdx > 0 ? raw.slice(colonIdx + 1).trim() : raw;
    const label = _PML_CODE_LABELS[code] || code.replace(/_/g,' ');
    return { code, label, desc };
  };

  // Build a lookup: internal_code → edit code ref (from backend bridge output)
  const _editCodeMap = {};
  (r.edit_codes || []).forEach(ec => {
    if (!_editCodeMap[ec.internal_code]) _editCodeMap[ec.internal_code] = ec;
  });

  // Render a payor edit code badge inline next to an issue/warning
  const _editBadge = (internalCode, isBlocking) => {
    const ec = _editCodeMap[internalCode];
    if (!ec) return '';
    const bg    = isBlocking ? '#fca5a5' : '#fde68a';
    const color = isBlocking ? '#7f1d1d' : '#78350f';
    const border = isBlocking ? '#f87171' : '#fbbf24';
    return `<span title="${esc(ec.description)}\n\nRef: ${esc(ec.reference)}\nStep: ${esc(ec.processing_step||'—')}"
      style="display:inline-flex;align-items:center;gap:.18rem;font-size:.62rem;font-weight:700;
             background:${bg};color:${color};border:1px solid ${border};
             border-radius:4px;padding:.05rem .35rem;margin-left:.3rem;cursor:help;vertical-align:middle;white-space:nowrap">
      ⚡ ${esc(ec.code)}
    </span>`;
  };

  // Helper: render a single warning row with optional edit code badge
  const _warnRow = w => {
    const { code, label, desc } = _fmtCode(w);
    return `<div style="display:flex;align-items:flex-start;gap:.36rem;font-size:.73rem;color:#92400e;margin-top:.14rem">
      <span style="flex-shrink:0;margin-top:.05rem">⚠</span>
      <div>
        ${code ? `<span style="font-weight:700;font-size:.68rem;color:#78350f;background:#fef3c7;border-radius:3px;padding:0 .28rem;margin-right:.28rem;vertical-align:middle">${esc(label)}</span>` : ''}
        <span style="color:#78350f">${esc(desc)}</span>
        ${_editBadge(code, false)}
      </div>
    </div>`;
  };

  // Deduplicated edit code summary strip — groups unique edit codes across all issues+warnings
  const _editCodeSummary = () => {
    const seen = new Set();
    const chips = (r.edit_codes || []).filter(ec => {
      if (seen.has(ec.code)) return false;
      seen.add(ec.code); return true;
    });
    if (!chips.length) return '';
    return `<div style="display:flex;align-items:center;gap:.3rem;flex-wrap:wrap;margin-top:.3rem;padding-top:.3rem;border-top:1px solid rgba(0,0,0,.07)">
      <span style="font-size:.6rem;font-weight:700;color:var(--text-3);text-transform:uppercase;letter-spacing:.05em;white-space:nowrap">
        ${esc(r.payor_id === 'fl_ahca_pml' ? 'FL AHCA edits' : (r.payor_id || 'Payor edits'))}
      </span>
      ${chips.map(ec => {
        const isBlk = ec.severity === 'blocking';
        const bg    = isBlk ? '#fca5a5' : '#fde68a';
        const color = isBlk ? '#7f1d1d' : '#78350f';
        const border = isBlk ? '#f87171' : '#fbbf24';
        return `<span title="${esc(ec.description)}\n\nRef: ${esc(ec.reference)}\nStep: ${esc(ec.processing_step||'—')}"
          style="display:inline-flex;align-items:center;gap:.18rem;font-size:.65rem;font-weight:700;
                 background:${bg};color:${color};border:1px solid ${border};
                 border-radius:4px;padding:.08rem .4rem;cursor:help;white-space:nowrap">
          ⚡ ${esc(ec.code)}
          <span style="font-weight:400;font-size:.6rem">${esc(ec.label)}</span>
        </span>`;
      }).join('')}
    </div>`;
  };

  // Issue/warning banner
  let issueBanner = '';
  if (r._missing) {
    issueBanner = `<div class="pml-issues-section" style="background:#fef2f2;border-color:#fca5a5">
      <div style="font-size:.72rem;font-weight:700;color:#991b1b;margin-bottom:.18rem">✗ Not enrolled in PML</div>
      <div style="font-size:.72rem;color:#7f1d1d">No Medicaid enrollment record found for NPI <strong>${esc(r.npi||'unknown')}</strong>.
        Submit enrollment request with FL AHCA.</div>
    </div>`;
  } else if (issues.length) {
    const rows = issues.map(i => {
      const { code, label, desc } = _fmtCode(i);
      const blk = /zip|npi|medicaid|not.*in.*pml|entity.type/i.test(i);
      return `<div style="display:flex;align-items:flex-start;gap:.36rem;font-size:.73rem;color:${blk?'#991b1b':'#92400e'}">
        <span style="flex-shrink:0;margin-top:.1rem">${blk?'✗':'⚠'}</span>
        <div>
          ${code ? `<span style="font-weight:700;font-size:.68rem;background:${blk?'#fee2e2':'#fef3c7'};border-radius:3px;padding:0 .28rem;margin-right:.28rem;vertical-align:middle">${esc(label)}</span>` : ''}
          <span>${esc(desc||i)}</span>
          ${_editBadge(code, blk)}
        </div>
      </div>`;
    });
    if (warnings.length) rows.push(...warnings.map(w => _warnRow(w)));
    issueBanner = `<div class="pml-issues-section">
      <div style="font-size:.65rem;font-weight:700;color:#92400e;margin-bottom:.22rem;text-transform:uppercase;letter-spacing:.04em">⚠ Issues detected</div>
      ${rows.join('')}
      ${_editCodeSummary()}
    </div>`;
  } else if (warnings.length) {
    issueBanner = `<div class="pml-issues-section" style="background:#fffbeb;border-color:#fcd34d">
      <div style="font-size:.65rem;font-weight:700;color:#78350f;margin-bottom:.18rem;text-transform:uppercase;letter-spacing:.04em">⚠ Enrolled — compliance warnings</div>
      <div style="font-size:.72rem;color:#166534;margin-bottom:.3rem">✓ Provider is enrolled and active in PML.</div>
      ${warnings.map(w => _warnRow(w)).join('')}
      ${_editCodeSummary()}
    </div>`;
  } else {
    issueBanner = `<div class="pml-issues-section all-ok"><div style="font-size:.73rem;color:#166534">✓ All validation checks passed — provider is enrolled and active in PML.</div></div>`;
  }

  // ZIP display helpers — must be declared before actionHtml and location panel
  const enrolledZip  = r.zip9 || '';
  const enrolledZip5 = enrolledZip.slice(0, 5);
  const formattedEnrolledZip = enrolledZip.length >= 9 && enrolledZip.slice(5,9) !== '0000'
    ? `${enrolledZip.slice(0,5)}-${enrolledZip.slice(5,9)}`
    : enrolledZip.slice(0,5) || enrolledZip || '—';
  const nppesZip = r.nppes_practice_zip || '';

  // ── Service location data — all pre-computed by backend ─────────────────
  // Backend provides zip5_match_locations[] and zip5_passes so the frontend
  // never runs its own matching logic. allLocs is used only for the user-selection picker.
  const allLocs = (window.lastRun?.orchestrator_state?.locations || []).filter(l => typeof l === 'object');

  // Has the user already confirmed a specific location for this provider? (UI interaction state)
  const savedLocIdx = (_pmlTaskState.providerLocations || {})[tid];
  const confirmedLoc = (savedLocIdx !== undefined && savedLocIdx !== null) ? allLocs[savedLocIdx] : null;

  // zip5_passes: backend ground truth — true when ZIP5 matches ≥1 confirmed location
  const zip5Passes = r.zip5_passes ?? false;

  // zip5_match_locations: pre-computed list from backend (each: {loc_idx, address, zip5, city, state})
  const backendMatches = r.zip5_match_locations || [];

  // Helpers used only for confirmed/selected location display (user-interaction state)
  const _locZip5 = l => (l.site_zip5 || l.site_zip || l.zip || '').replace(/\D/g,'').slice(0,5);
  const _locAddr = l => [l.site_address_line_1 || l.site_address, l.site_city, l.site_state].filter(Boolean).join(', ');

  // Zip for comparison uses confirmed loc if selected, else first backend match
  const confirmedLocObj = confirmedLoc;  // already resolved above from savedLocIdx
  const firstMatchLoc   = backendMatches.length > 0 ? allLocs[backendMatches[0].loc_idx] : null;
  const effectiveLoc    = confirmedLocObj || firstMatchLoc || null;
  const practiceZip5    = effectiveLoc ? _locZip5(effectiveLoc) : null;
  const zipMatchesBilling = zip5Passes || (practiceZip5 ? (enrolledZip5 === practiceZip5) : false);

  // ── Build the right-column location panel ────────────────────────────────
  // All matching logic is backend-computed. Frontend only handles rendering and
  // user selection state (which specific location was confirmed by the user).
  let locPanelHtml = '';

  if (!allLocs.length) {
    locPanelHtml = `<div style="font-size:.72rem;color:var(--text-3);font-style:italic">No service locations confirmed in Step 2.</div>`;

  } else if (confirmedLoc) {
    // User explicitly confirmed a specific location (UI state)
    const zip5 = _locZip5(confirmedLoc);
    const zipMatch = zip5 === enrolledZip5;
    locPanelHtml = `
      <div class="pml-loc-option confirmed">
        <span class="pml-loc-dot confirmed"></span>
        <div style="flex:1">
          <div class="pml-loc-name">✓ Practice Location Confirmed</div>
          <div class="pml-loc-addr">${esc(_locAddr(confirmedLoc))}</div>
          <div class="pml-loc-zip ${zipMatch?'match':'nomatch'}" style="margin-top:.1rem">
            ZIP ${esc(zip5)} ${zipMatch ? '— matches PML ✓' : '— differs from PML ZIP ('+esc(formattedEnrolledZip)+')'}
          </div>
        </div>
        <button class="pml-loc-action select-btn" onclick="event.stopPropagation();pmlClearProviderLocation('${esc(tid)}')">Change</button>
      </div>`;

  } else if (zip5Passes && backendMatches.length === 1) {
    // Backend confirmed exactly one ZIP5 match — display it, no user action required
    const bm = backendMatches[0];
    locPanelHtml = `
      <div style="font-size:.67rem;color:#166534;background:#f0fdf4;border:1px solid #86efac;border-radius:5px;padding:.25rem .45rem;margin-bottom:.2rem">
        ✓ ZIP5 matches confirmed service location — passes PML minimum requirement
        <span style="color:var(--text-3);font-weight:400;margin-left:.25rem">(ZIP+4 not in Step 2 records)</span>
      </div>
      <div class="pml-loc-option confirmed">
        <span class="pml-loc-dot confirmed"></span>
        <div style="flex:1">
          <div class="pml-loc-name">${esc(bm.address)}</div>
          <div class="pml-loc-zip match" style="margin-top:.06rem">ZIP ${esc(bm.zip5)} — matches PML ✓</div>
        </div>
        <button class="pml-loc-action select-btn" onclick="event.stopPropagation();pmlSelectProviderLocation('${esc(tid)}',${bm.loc_idx})" title="Override to a different location">Change</button>
      </div>`;

  } else if (zip5Passes && backendMatches.length > 1) {
    // Backend found multiple ZIP5 matches — ZIP still passes, user may optionally confirm which site
    locPanelHtml = `
      <div style="font-size:.67rem;color:#166534;background:#f0fdf4;border:1px solid #86efac;border-radius:5px;padding:.25rem .45rem;margin-bottom:.2rem">
        ✓ ZIP5 <strong>${esc(backendMatches[0].zip5)}</strong> matches ${backendMatches.length} confirmed locations — passes PML minimum requirement
        <span style="color:var(--text-3);font-weight:400;margin-left:.25rem">(ZIP+4 not in Step 2 records)</span>
      </div>
      <div style="font-size:.67rem;color:#1e40af;margin-bottom:.2rem">
        Optionally confirm the specific practice site (does not affect enrollment status):
      </div>
      <div class="pml-loc-picker">
        ${backendMatches.map(bm => `
          <div class="pml-loc-option">
            <span class="pml-loc-dot option"></span>
            <div style="flex:1">
              <div class="pml-loc-name">${esc(bm.address)}</div>
              <div class="pml-loc-zip match" style="margin-top:.06rem">ZIP ${esc(bm.zip5)} ✓</div>
            </div>
            <button class="pml-loc-action select-btn" onclick="event.stopPropagation();pmlSelectProviderLocation('${esc(tid)}',${bm.loc_idx})">Confirm</button>
          </div>`).join('')}
      </div>`;

  } else {
    // zip5_passes is false → zip_mismatch_location (blocking). Show full picker.
    locPanelHtml = `
      <div style="font-size:.67rem;color:#991b1b;background:#fef2f2;border:1px solid #fca5a5;border-radius:5px;padding:.25rem .45rem;margin-bottom:.2rem">
        ✗ PML ZIP <strong>${esc(formattedEnrolledZip)}</strong> does not match any confirmed service location (ZIP5 check fails — blocking).
        Select the correct practice site:
      </div>
      <div class="pml-loc-picker">
        ${allLocs.map((l, i) => {
          const zip5 = _locZip5(l);
          return `<div class="pml-loc-option">
            <span class="pml-loc-dot option"></span>
            <div style="flex:1">
              <div class="pml-loc-name">${esc(_locAddr(l))}</div>
              <div class="pml-loc-zip" style="margin-top:.06rem">${esc(zip5 || '—')}</div>
            </div>
            <button class="pml-loc-action select-btn" onclick="event.stopPropagation();pmlSelectProviderLocation('${esc(tid)}',${i})">Select</button>
          </div>`;
        }).join('')}
      </div>`;
  }

  // PML ZIP inline field note
  const zipCls = zip5Passes ? 'ok' : (!practiceZip5 ? '' : (zipMatchesBilling ? 'ok' : 'mismatch'));
  const zipNote = confirmedLoc && !zipMatchesBilling
    ? `<span class="pml-field-correction" style="background:#fef2f2;border-color:#fca5a5;color:#991b1b">↳ Update PML to ${esc(_locZip5(confirmedLoc))}</span>`
    : (zip5Passes
        ? `<span class="pml-field-correction">↳ ZIP5 matches location ✓</span>`
        : '');

  // Required action strip — derived dynamically from user's confirmed service location.
  // We never trust NPPES for the "correct" ZIP; only the user-confirmed Step 2 location
  // can drive the update recommendation. If no location is confirmed yet, prompt selection.
  // NOTE: must come after enrolledZip5, formattedEnrolledZip, confirmedLoc, _locZip5, _locAddr.
  let actionHtml = '';
  {
    const hasZipIssue = issues.some(i => i === 'zip_mismatch_location' || i === 'zip_not_9_digits');
    const currentZipDisplay = r.current_zip_display || formattedEnrolledZip;

    if (hasZipIssue) {
      if (confirmedLoc) {
        const selectedZip5 = _locZip5(confirmedLoc);
        const selectedAddr = _locAddr(confirmedLoc);
        if (selectedZip5 && selectedZip5 !== enrolledZip5) {
          actionHtml = `
            <div class="pml-card-full" style="background:#eff6ff;border:1px solid #bfdbfe;border-radius:6px;padding:.4rem .6rem">
              <div style="display:flex;align-items:center;gap:.3rem;flex-wrap:wrap;margin-bottom:.1rem">
                <span style="font-size:.62rem;font-weight:700;color:#1d4ed8;text-transform:uppercase;letter-spacing:.04em">Required action</span>
                <span style="font-size:.62rem;color:#166534;margin-left:.3rem">Source: Step 2 confirmed location</span>
              </div>
              <div style="font-size:.73rem;color:#1e3a8a">
                Update PML enrollment ZIP from <strong>${esc(currentZipDisplay)}</strong> to <strong>${esc(selectedZip5)}</strong>
                (confirmed service location: ${esc(selectedAddr)}) — contact FL AHCA
              </div>
            </div>`;
        } else if (selectedZip5 && selectedZip5 === enrolledZip5) {
          actionHtml = `
            <div class="pml-card-full" style="background:#f0fdf4;border:1px solid #86efac;border-radius:6px;padding:.4rem .6rem">
              <div style="font-size:.73rem;color:#166534">✓ PML ZIP matches selected practice location — no update required.</div>
            </div>`;
        }
      } else {
        actionHtml = `
          <div class="pml-card-full" style="background:#fffbeb;border:1px solid #fcd34d;border-radius:6px;padding:.4rem .6rem">
            <div style="display:flex;align-items:center;gap:.3rem;margin-bottom:.1rem">
              <span style="font-size:.62rem;font-weight:700;color:#92400e;text-transform:uppercase;letter-spacing:.04em">Action required</span>
            </div>
            <div style="font-size:.73rem;color:#78350f">
              Select the provider's confirmed practice site above to determine the correct ZIP for PML update.
              NPPES address is not used as reference — only Step 2 confirmed locations are authoritative.
            </div>
          </div>`;
      }
    } else if (r.recommendation) {
      actionHtml = `
        <div class="pml-card-full" style="background:#eff6ff;border:1px solid #bfdbfe;border-radius:6px;padding:.4rem .6rem">
          <div style="font-size:.62rem;font-weight:700;color:#1d4ed8;text-transform:uppercase;letter-spacing:.04em;margin-bottom:.1rem">Required action</div>
          <div style="font-size:.73rem;color:#1e3a8a">${esc(r.recommendation)}</div>
        </div>`;
    }
  }

  // PPL badge
  const pplHtml = r.ppl_found ? `
    <div class="pml-card-full">
      <span class="ppl-badge" title="Provider found in PPL${r.ppl_programs?.length?' — '+r.ppl_programs.join(', '):''}">
        🔵 In PPL (managed care)${r.ppl_programs?.length?' · '+r.ppl_programs[0]:''}
        ${r.ppl_contract_date?' · '+r.ppl_contract_date:''}
      </span>
    </div>` : '';

  // Contract / enrollment status row
  const _statusColor = !(r.enrollment_status) ? '' :
    /inactive|terminat|revok/i.test(r.enrollment_status) ? 'mismatch' : 'ok';
  const contractHtml = (r.enrollment_status || r.contract_effective_date || r.contract_end_date) ? `
    <div class="pml-field" style="grid-column:1/-1;border-top:1px solid var(--border);padding-top:.35rem;margin-top:.15rem">
      <span class="pml-field-lbl">Enrollment</span>
      <span style="display:flex;align-items:center;gap:.45rem;flex-wrap:wrap">
        ${r.enrollment_status ? `<span class="pml-field-val ${_statusColor}">${esc(r.enrollment_status)}</span>` : ''}
        ${r.contract_effective_date ? `<span style="font-size:.68rem;color:var(--text-3)">Eff: ${esc(r.contract_effective_date)}</span>` : ''}
        ${r.contract_end_date       ? `<span style="font-size:.68rem;color:var(--text-3)">End: ${esc(r.contract_end_date)}</span>`       : ''}
      </span>
    </div>` : '';

  // Notes — persisted per provider (loaded from localStorage keyed by tid)
  const _noteKey = `pml-card-note-${tid}`;
  const _savedNote = (() => { try { return localStorage.getItem(_noteKey) || ''; } catch(_) { return ''; } })();

  const card = `<div class="pml-card">
    ${issueBanner}

    <!-- Left: AHCA PML Record (what is currently on file with Florida Medicaid) -->
    <div class="pml-card-section">
      <div class="pml-card-section-hdr" title="Data from Florida AHCA Medicaid stg_pml">
        AHCA PML Record
        <span style="font-size:.6rem;font-weight:400;color:var(--text-3);margin-left:.3rem">current on file</span>
      </div>
      ${fld('Provider Name', r.provider_name)}
      ${fld('NPI', r.npi, npiInvalid ? 'mismatch' : (r.npi ? 'ok' : 'missing'))}
      ${fld('Medicaid Provider ID', r.medicaid_provider_id, midMissing ? 'mismatch' : (r.medicaid_provider_id ? '' : 'missing'))}
      ${fld('Taxonomy Code', r.taxonomy_code, taxMismatch ? 'mismatch' : '')}
      ${fld('ZIP-9 on file', formattedEnrolledZip, zipCls, zipNote)}
      ${fld('Address Line 1', r.address_line_1)}
      ${fld('City / State', [r.city, r.state].filter(Boolean).join(', '))}
      ${contractHtml}
    </div>

    <!-- Right: Confirmed service location from Step 2 (source of truth for billing) -->
    <div class="pml-card-section">
      <div class="pml-card-section-hdr" title="Locations confirmed in Step 2 — source of truth for billing address">
        Confirmed Service Location
        <span style="font-size:.6rem;font-weight:400;color:var(--text-3);margin-left:.3rem">Step 2 · source of truth</span>
      </div>
      ${locPanelHtml}
    </div>

    ${actionHtml}
    ${pplHtml}

    <!-- Notes -->
    <div class="pml-card-full" style="margin-top:.35rem">
      <div style="font-size:.67rem;font-weight:600;color:var(--text-2);margin-bottom:.2rem">Notes</div>
      <textarea id="pml-note-area-${esc(tid)}"
        placeholder="Add notes about this provider's enrollment…"
        oninput="try{localStorage.setItem('${_noteKey}',this.value)}catch(_){}"
        style="width:100%;min-height:3rem;font-size:.72rem;padding:.3rem .45rem;border:1px solid var(--border);border-radius:6px;resize:vertical;background:var(--surface);color:var(--text);font-family:var(--font);box-sizing:border-box"
      >${esc(_savedNote)}</textarea>
    </div>
  </div>`;

  const isDismissed = _loadPmlTaskDismissed().has(tid.replace(/^pml-/, ''));
  const _btnBase = 'font-size:.7rem;font-weight:600;padding:.22rem .55rem;border-radius:6px;cursor:pointer;font-family:var(--font);transition:all .12s;white-space:nowrap';
  return `${card}
    <div style="display:flex;align-items:center;justify-content:space-between;gap:.45rem;flex-wrap:wrap;margin-top:.45rem;padding-top:.4rem;border-top:1px solid var(--border)">
      <div style="display:flex;align-items:center;gap:.4rem;flex-wrap:wrap">
        <button class="pml-resolve-btn ${isDone?'resolved':''}"
          id="pmlresbtn-${esc(tid)}"
          onclick="event.stopPropagation();pmlToggleTask('${esc(tid)}')">
          ${isDone ? '✓ Resolved' : '+ Mark resolved'}
        </button>
        <button onclick="event.stopPropagation();pmlCreateManualTask('${esc(tid)}','${esc(r.provider_name||r.npi||'')}','${esc(r.npi||'')}',document.getElementById('pml-note-area-${esc(tid)}')?.value||'')"
          style="${_btnBase};border:1px solid var(--indigo-border);background:var(--indigo-bg);color:var(--indigo)"
          title="Create a tracked task for this provider">
          + Create task
        </button>
        <button onclick="event.stopPropagation();pmlExportProviderRecord(${JSON.stringify(r).replace(/"/g,'&quot;')})"
          style="${_btnBase};border:1px solid var(--border);background:var(--surface);color:var(--text-2)"
          title="Download a clean correction record for this provider (CSV row)">
          ↓ Export record
        </button>
      </div>
      ${!r._missing && !isDismissed ? `
      <button onclick="event.stopPropagation();pmlDismissRow('${esc(tid.replace(/^pml-/,''))}')"
        style="${_btnBase};border:1px solid var(--border);background:var(--surface);color:var(--text-3)"
        title="Hide this row — removes the associated task">
        Dismiss row
      </button>` : ''}
    </div>`;
}

function _buildPmlTableHtml(allRows) {
  const pillCls = { all:'fa', enrolled:'fe', flagged:'ff', missing:'fm' };
  // Task count chip for the filter bar (right side)
  const _pmlChip = (() => {
    try {
      const _all  = _buildPmlAutoTasks();
      const _done = _loadPmlTaskDone();
      const _open = _all.filter(t => !_done.has(t.id)).length;
      if (!_all.length) return '';
      const _allDone = _open === 0;
      return `<button onclick="togglePmlTaskDrawer(true)"
        style="margin-left:auto;display:flex;align-items:center;gap:.3rem;font-size:.72rem;font-weight:600;padding:.25rem .6rem;border-radius:7px;border:1px solid var(--border);background:var(--surface);color:${_allDone?'var(--green)':'var(--text-2)'};cursor:pointer;white-space:nowrap;flex-shrink:0;transition:all .15s"
        title="Open task panel">
        <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" style="width:11px;height:11px"><rect x="2" y="3" width="12" height="10" rx="1.5"/><path d="M5 6h6M5 9h4"/></svg>
        ${_allDone ? '✓ all done' : `Tasks <span style="background:${_open>0?'var(--red)':'var(--green)'};color:#fff;border-radius:10px;padding:.04rem .38rem;font-size:.6rem">${_open}</span>`}
      </button>`;
    } catch(_) { return ''; }
  })();
  return `
    <div class="pml-filter-bar">
      <input class="pml-filter-input" type="text" placeholder="Search provider, NPI, Medicaid ID…"
        oninput="_pmlQuery=this.value;_rerenderPmlTable()" value="${esc(_pmlQuery)}">
      ${['all','enrolled','flagged','missing'].map(f =>
        `<button class="pml-filter-pill ${_pmlFilter===f?pillCls[f]:''}"
          onclick="pmlSetFilter('${f}')">${f==='all'?'All':f==='enrolled'?'Enrolled':f==='flagged'?'Flagged':'Not enrolled'}</button>`
      ).join('')}
      ${_pmlChip}
    </div>
    <div class="pml-table-wrap">
      <table class="pml-prov-table">
        <thead><tr>
          <th>Provider</th><th>Medicaid ID</th><th>Taxonomy</th>
          <th>ZIP-9</th><th>Status</th><th>Issues</th><th></th>
        </tr></thead>
        <tbody id="pmlTableBody">${_pmlProvRows(allRows)}</tbody>
      </table>
    </div>`;
}

function _rerenderPmlTable() {
  const body = document.getElementById('pmlTableBody');
  if (!body) return;
  const { validated, flagged, missing } = _pmlData();
  const allRows = [
    ...validated.map(r=>({...r,_missing:false})),
    ...flagged.map(r=>({...r,_missing:false})),
    ...missing.map(r=>({...r,_missing:true})),
  ];
  body.innerHTML = _pmlProvRows(allRows);
}

// ── Auto task list ─────────────────────────────────────────────
// Warning codes that should surface as tasks even on enrolled (validated) providers.
// Each entry: code prefix → { sev, short description, action hint }
const _PML_WARN_TASK_MAP = {
  zip4_zeros_default_risk: {
    sev: 'medium',
    text: 'ZIP+4 all zeros — PAY 1980 default risk',
    det: 'Update PML ZIP+4 to the exact service-location ZIP+4. If this provider has multiple '
       + 'Medicaid enrollments at the same ZIP5, FLMMIS will default to oldest contract '
       + '(PAY edit 1980 → risk of DENIAL). Contact FL AHCA to correct.',
  },
  address_mismatch_multiple_zip5: {
    sev: 'medium',
    text: 'Address mismatch — multiple ZIP5 locations (DENIAL 1120 risk)',
    det: 'Multiple confirmed service locations share this ZIP5. PML address does not match any. '
       + 'FLMMIS Step 5 uses Address Line 1 as tiebreaker — mismatch causes DENIAL (edit 1120). '
       + 'Confirm the correct practice site and update PML address to match exactly.',
  },
  address_missing_multiple_zip5: {
    sev: 'medium',
    text: 'Address missing — multiple ZIP5 locations (DENIAL 1120 risk)',
    det: 'Multiple confirmed service locations share this ZIP5 but PML has no Address Line 1. '
       + 'FLMMIS Step 5 cannot resolve ambiguity — may default or DENY (edit 1120). '
       + 'Obtain and update the PML address on file with FL AHCA.',
  },
  multiple_enrollment_default_risk: {
    sev: 'medium',
    text: 'Multiple active Medicaid enrollments — default risk (PAY 1980)',
    det: 'Provider has more than one active PML enrollment. FLMMIS uses ZIP+4 → ZIP5 → Address '
       + 'to pick the correct Medicaid Provider ID. If unresolvable, defaults to oldest contract '
       + '(PAY 1980 → risk of DENIAL). Ensure each enrollment has a distinct ZIP+4 and address, '
       + 'or terminate stale enrollments.',
  },
};

function _buildPmlAutoTasks() {
  const { validated, flagged, missing } = _pmlData();
  const dismissed = _loadPmlTaskDismissed();
  const tasks = [];

  // Blocking issues on flagged rows
  flagged.forEach(r => {
    const key = _pmlTaskKey(r);
    if (dismissed.has(key)) return;
    const issues = r.issues||[];
    const sev = issues.some(i=>/zip|npi|medicaid|not.*in.*pml/i.test(i)) ? 'high':'medium';
    tasks.push({
      id: `pml-${key}`,
      prov: r.provider_name||r.npi||'—',
      npi: r.npi||'',
      sev,
      text: issues[0]||'Flagged for review',
      det:  r.recommendation||(issues.length>1?issues.slice(1).join(' · '):''),
      allIssues: issues,
      ppl_found: r.ppl_found||false,
      ppl_programs: r.ppl_programs||[],
      ppl_contract_date: r.ppl_contract_date||null,
      manual: false,
    });
  });

  // Compliance warnings on validated (enrolled) rows — surface as medium-severity tasks
  // so nothing slips through undetected just because the provider is technically enrolled.
  validated.forEach(r => {
    const key = _pmlTaskKey(r);
    if (dismissed.has(key)) return;
    (r.warnings||[]).forEach(w => {
      const code = w.indexOf(':') > 0 ? w.slice(0, w.indexOf(':')).trim() : '';
      const def = _PML_WARN_TASK_MAP[code];
      if (!def) return;
      const taskId = `pml-warn-${key}-${code}`;
      // Deduplicate: skip if already added (NPI with multiple taxonomy rows)
      if (tasks.find(t => t.id === taskId)) return;
      tasks.push({
        id: taskId,
        prov: r.provider_name||r.npi||'—',
        npi: r.npi||'',
        sev: def.sev,
        text: def.text,
        det:  def.det,
        allIssues: [code],
        ppl_found: r.ppl_found||false,
        ppl_programs: r.ppl_programs||[],
        ppl_contract_date: r.ppl_contract_date||null,
        manual: false,
        warnOnly: true,
      });
    });
  });

  // Also catch warnings on flagged rows that aren't already captured above
  flagged.forEach(r => {
    const key = _pmlTaskKey(r);
    if (dismissed.has(key)) return;
    (r.warnings||[]).forEach(w => {
      const code = w.indexOf(':') > 0 ? w.slice(0, w.indexOf(':')).trim() : '';
      const def = _PML_WARN_TASK_MAP[code];
      if (!def) return;
      const taskId = `pml-warn-${key}-${code}`;
      if (tasks.find(t => t.id === taskId)) return;
      tasks.push({
        id: taskId,
        prov: r.provider_name||r.npi||'—',
        npi: r.npi||'',
        sev: def.sev,
        text: def.text,
        det:  def.det,
        allIssues: [code],
        ppl_found: false, manual: false, warnOnly: true,
      });
    });
  });

  missing.forEach(r => {
    const key = _pmlTaskKey(r);
    if (dismissed.has(key)) return;
    tasks.push({
      id: `pml-missing-${key}`,
      prov: r.provider_name||r.npi||'—',
      npi: r.npi||'',
      sev: 'high',
      text: 'Not enrolled in PML',
      det: r.recommendation||'No Medicaid enrollment record — submit enrollment request.',
      allIssues: ['Not enrolled in PML'],
      ppl_found: r.ppl_found||false,
      ppl_programs: r.ppl_programs||[],
      ppl_contract_date: r.ppl_contract_date||null,
      manual: false,
    });
  });
  const manual = _loadPmlManualTasks();
  return tasks.concat(manual);
}

// ── PML manual task + notes — delegate to _pmlTaskState ─────────
function _loadPmlManualTasks() { return _pmlTaskState.manual || []; }
function _savePmlManualTasks(arr) { _pmlTaskState.manual = arr; _savePmlTaskState(); }
function _loadPmlTaskNotes()   { return _pmlTaskState.notes || {}; }
function _savePmlTaskNotes(map) { _pmlTaskState.notes = map; _savePmlTaskState(); }

// ── Inline task-chip (replaces old inline list) ──────────────────
function _buildPmlTaskChipHtml() {
  const tasks = _buildPmlAutoTasks();
  const done  = _loadPmlTaskDone();
  const open  = tasks.filter(t => !done.has(t.id));
  if (!tasks.length) return `<div style="font-size:.74rem;color:var(--green);margin-top:.35rem">✓ No issues — all providers enrolled and valid.</div>`;
  const allDone = open.length === 0;
  const blocking  = open.filter(t => !t.warnOnly).length;
  const riskFlags = open.filter(t =>  t.warnOnly).length;
  const summaryParts = [];
  if (blocking)  summaryParts.push(`<span style="color:var(--red)">✗ ${blocking} blocking</span>`);
  if (riskFlags) summaryParts.push(`<span style="color:#78350f">⚠ ${riskFlags} risk flag${riskFlags!==1?'s':''}</span>`);
  const chipLabel = allDone ? '✓ All resolved'
    : summaryParts.length ? summaryParts.join('<span style="opacity:.4;margin:0 .2rem">·</span>')
    : `⚠ ${open.length} open`;
  return `<div style="margin-top:.35rem;display:flex;align-items:center;gap:.5rem;flex-wrap:wrap">
    <span class="pml-tasks-chip ${allDone?'all-done':''}" onclick="togglePmlTaskDrawer(true)"
      title="Open PML task panel — ${open.length} open, ${tasks.length-open.length} resolved">
      ${chipLabel}
      ${tasks.length > open.length ? `<span style="font-size:.6rem;opacity:.6;margin-left:.2rem">(${tasks.length-open.length} done)</span>` : ''}
    </span>
    ${tasks.filter(t=>t.ppl_found&&!done.has(t.id)).length ?
      `<span class="ppl-badge" title="Some flagged providers appear in PPL (managed care contracts) — may resolve issues">
        🔵 ${tasks.filter(t=>t.ppl_found&&!done.has(t.id)).length} in PPL
      </span>` : ''}
  </div>`;
}

// Kept for backward compat (pmlToggleTask re-renders this)
function _buildPmlTaskListHtml() {
  return _buildPmlTaskChipHtml();
}

// ── PML task drawer rendering ────────────────────────────────────
function _renderPmlTaskDrawer() {
  const tasks = _buildPmlAutoTasks();
  const done  = _loadPmlTaskDone();
  const notes = _loadPmlTaskNotes();
  const open  = tasks.filter(t => !done.has(t.id));
  const doneT = tasks.filter(t =>  done.has(t.id));
  const pplCount = open.filter(t=>t.ppl_found).length;

  const renderItem = (t, isDone, _idx=0) => {
    const note = notes[t.id]||'';
    const pplHtml = t.ppl_found ? `
      <span class="ppl-badge" title="Found in PPL${t.ppl_programs.length?' — '+t.ppl_programs.join(', '):''}${t.ppl_contract_date?' ('+t.ppl_contract_date+')':''}">
        🔵 In PPL${t.ppl_programs.length?' · '+t.ppl_programs[0]:''}
      </span>` : '';
    const extraIssues = (t.allIssues||[]).slice(1).filter(Boolean);
    // warnOnly = enrolled but has a compliance/default-risk warning (not a hard block)
    const warnBadge = t.warnOnly
      ? `<span style="font-size:.6rem;font-weight:600;background:#fef3c7;color:#78350f;border-radius:3px;padding:.02rem .28rem;margin-left:.3rem;vertical-align:middle">enrolled · risk flag</span>`
      : '';
    return `
    <div class="pml-task-item mob-card-enter ${isDone?'done':''}" id="ptask-${esc(t.id)}" style="border-left:3px solid ${t.warnOnly?'#f59e0b':'var(--'+(t.sev==='high'?'red':t.sev==='medium'?'amber':'green')+')'};animation-delay:${_idx*30}ms">
      <input type="checkbox" class="pml-task-cb" ${isDone?'checked':''}
        onchange="pmlToggleTask('${esc(t.id)}')">
      <div class="pml-task-body">
        <div class="pml-task-prov" title="${esc(t.npi)}">${esc(t.prov)}${t.npi?`<span style="font-weight:400;margin-left:.3rem;opacity:.7">${esc(t.npi)}</span>`:''} ${warnBadge}</div>
        <div class="pml-task-text">${esc(t.text)}</div>
        ${t.det?`<div class="pml-task-det">${esc(t.det)}</div>`:''}
        ${extraIssues.length?`<div class="pml-task-det" style="margin-top:.1rem">${extraIssues.map(i=>`· ${esc(i)}`).join('<br>')}</div>`:''}
        ${pplHtml}
        ${note?`<div class="pml-task-note">📝 ${esc(note)}</div>`:''}
        <div style="display:flex;gap:.4rem;margin-top:.3rem;flex-wrap:wrap">
          <button onclick="pmlEditNote('${esc(t.id)}')" style="font-size:.65rem;background:none;border:none;cursor:pointer;color:var(--indigo);padding:0">${note?'Edit note':'+ Add note'}</button>
          ${t.manual?`<button onclick="pmlDeleteTask('${esc(t.id)}')" style="font-size:.65rem;background:none;border:none;cursor:pointer;color:var(--red);padding:0">Delete</button>`:''}
        </div>
        <div id="pml-note-form-${esc(t.id)}" style="display:none"></div>
      </div>
      <span class="pml-task-sev ${t.sev}">${t.sev}</span>
    </div>`;
  };

  const body = document.getElementById('pmlTaskDrawerBody');
  if (!body) return;

  let html = '';
  if (pplCount) {
    html += `<div style="font-size:.7rem;color:#0369a1;background:#e0f2fe;border:1px solid #bae6fd;border-radius:7px;padding:.35rem .55rem;margin-bottom:.3rem">
      🔵 <strong>${pplCount}</strong> flagged provider${pplCount!==1?'s are':' is'} found in PPL (managed care contracts) — verify enrollment path before acting.
    </div>`;
  }
  if (!tasks.length) {
    html += `<div style="font-size:.75rem;color:var(--green);text-align:center;padding:2rem .5rem">✓ No issues — all providers enrolled and valid.</div>`;
  } else {
    html += `<div style="font-size:.7rem;font-weight:700;color:var(--text-3);margin-bottom:.15rem">Open (${open.length})</div>`;
    html += open.map((t,i)=>renderItem(t,false,i)).join('');
    if (doneT.length) {
      html += `<div style="font-size:.7rem;color:var(--text-3);margin:.55rem 0 .15rem;font-weight:600">Done (${doneT.length})</div>`;
      html += doneT.map((t,i)=>renderItem(t,true,open.length+i)).join('');
    }
  }
  body.innerHTML = html;

  const footer = document.getElementById('pmlTaskDrawerFooter');
  if (footer) {
    footer.innerHTML = `<button onclick="pmlAddTaskForm()" style="width:100%;padding:.38rem;font-size:.75rem;font-weight:600;background:var(--indigo-bg);color:var(--indigo);border:1.5px dashed var(--indigo-border);border-radius:7px;cursor:pointer">+ Add task</button>
    <div id="pmlAddTaskFormWrap" style="display:none;margin-top:.4rem"></div>`;
  }
}

// ── Score card + stat row ──────────────────────────────────────
function _buildPmlScoreHtml(s) {
  const pct = f => Math.round(f * 100);
  const barCls = p => p >= 85 ? 'green' : p >= 65 ? 'amber' : 'red';
  const bar = (rate) => {
    const p = pct(rate);
    return `<div class="pml-dim-bar"><div class="pml-dim-fill ${barCls(p)}" style="width:${p}%"></div></div>`;
  };
  return `
    <div class="pml-score-card">
      <div class="pml-score-num ${s.band}">${s.score}</div>
      <div style="flex:1">
        <div style="display:flex;align-items:center;gap:.45rem;margin-bottom:.25rem">
          <span class="pml-score-band ${s.band}">${s.bandLabel}</span>
          <span style="font-size:.65rem;color:var(--text-3);cursor:help"
            title="Enrollment coverage ×40% + Validation pass rate ×35% + Taxonomy compliance ×15% + ZIP-9 coverage ×10%">ⓘ how scored</span>
        </div>
        <div class="pml-score-dims">
          <span class="pml-score-dim">${bar(s.enrollCov)} Enrollment ${pct(s.enrollCov)}%</span>
          <span class="pml-score-dim">${bar(s.validRate)} Valid ${pct(s.validRate)}%</span>
          <span class="pml-score-dim">${bar(s.taxRate)} Taxonomy ${pct(s.taxRate)}%</span>
          <span class="pml-score-dim">${bar(s.zipRate)} ZIP-9 ${pct(s.zipRate)}%</span>
        </div>
      </div>
    </div>`;
}

function _buildPmlStatRowHtml(s) {
  const cell = (val, lbl, cls='') =>
    `<div class="pml-stat-cell"><span class="pml-stat-val ${cls}">${val}</span><span class="pml-stat-lbl">${lbl}</span></div>`;
  const exportBtn = (s.withIssues > 0 || s.notIn > 0) ? `
    <button onclick="pmlExportAllCorrections()"
      style="margin-left:auto;display:flex;align-items:center;gap:.3rem;font-size:.7rem;font-weight:600;padding:.22rem .65rem;border-radius:7px;border:1px solid var(--border);background:var(--surface);color:var(--text-2);cursor:pointer;white-space:nowrap;transition:all .15s"
      title="Download correction CSV for all flagged providers">
      ↓ Export corrections
    </button>` : '';
  return `<div class="pml-stat-row" style="align-items:center">
    ${cell(s.enrolled,    'enrolled (PML)', 'green')}
    ${cell(s.withIssues,  'flagged',        s.withIssues > 0 ? 'amber' : '')}
    ${cell(s.notIn,       'not enrolled',   s.notIn > 0 ? 'red' : '')}
    ${cell(s.total,       'total roster')}
    ${exportBtn}
  </div>`;
}

// ── Validation results section ─────────────────────────────────
function _buildPmlResultsHtml() {
  const { validated, flagged, missing } = _pmlData();
  const s = _computePmlScore(validated, flagged, missing);

  if (!s) return `<div class="coming-soon-box" style="text-align:left">
    <div class="cs-title" style="text-align:left">PML data not yet available</div>
    <div class="cs-body" style="text-align:left;max-width:none;margin:0">
      Complete Step 4 in the pipeline to run Medicaid enrollment validation against the latest PML snapshot.
    </div>
  </div>`;

  const allRows = [
    ...validated.map(r=>({...r,_missing:false})),
    ...flagged.map(r=>({...r,_missing:false})),
    ...missing.map(r=>({...r,_missing:true})),
  ];

  return _buildPmlScoreHtml(s)
    + _buildPmlStatRowHtml(s)
    + _buildPmlTableHtml(allRows)
    + _buildPmlTaskChipHtml();
}

// ── Top-level builder ──────────────────────────────────────────
function _buildPayorTabHtml() {
  const { validated, flagged, missing } = _pmlData();
  const score = _computePmlScore(validated, flagged, missing);

  // Compact meta bar with overflow menu for Data Source + Rules
  const metaBar = `
    <div class="pml-meta-bar" id="pmlMetaBar">
      <span style="font-size:.72rem;color:var(--text-3)">Florida Medicaid · AHCA · PML / TML / PPL</span>
      <span style="flex:1"></span>
      <div class="pml-overflow-wrap" id="pmlOverflowWrap">
        <button class="pml-overflow-btn" id="pmlOverflowBtn"
          onclick="event.stopPropagation();pmlToggleOverflow()"
          title="Data source &amp; validation rules">⋯</button>
        <div class="pml-overflow-menu" id="pmlOverflowMenu" style="display:none">
          <div class="pml-overflow-section">
            <div class="pml-overflow-hdr">
              Data Source
              <button class="pml-overflow-refresh" onclick="pmlToggleOverflow(false);refreshPmlData(this)" id="pmlRefreshBtn">↺ Refresh</button>
            </div>
            <div class="pml-overflow-body">${_buildPmlDataSrcHtml()}</div>
          </div>
          <div class="pml-overflow-section" style="border-top:1px solid var(--border);margin-top:.45rem;padding-top:.45rem">
            <div class="pml-overflow-hdr">Validation Rules <span style="font-size:.65rem;font-weight:400;color:var(--text-3)">5 rules · deterministic</span></div>
            <div class="pml-overflow-body">${_buildPmlRulesHtml()}</div>
          </div>
        </div>
      </div>
    </div>`;

  return _buildPayorStripHtml(score) + metaBar + `
    <div class="sec-card" style="border-radius:10px;overflow:hidden">
      <div class="sec-body" id="pmlResultsBody">${_buildPmlResultsHtml()}</div>
    </div>`;
}

// ── Roster tab switching ──────────────────────────────────────────────────────
function _applyRosterTabDisplay(tab) {
  // Sections keyed by tab
  const show = {
    workspace: ['workspaceSection','sessionBanner','rosterActivityLink','macroAuditBar'],
    upload:    ['uploadSection'],
    roster:    ['rosterSection'],
  };
  // Hide all managed sections first
  ['workspaceSection','sessionBanner','rosterActivityLink','uploadSection','rosterSection','macroAuditBar'].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.style.display = 'none';
  });
  // Show the ones for the active tab
  (show[tab] || show.workspace).forEach(id => {
    const el = document.getElementById(id);
    if (el) el.style.display = '';
  });
  // The filter bar is shown by _loadRosterDiff when data is ready (workspace only)
  // If switching away from workspace, hide it immediately
  if (tab !== 'workspace') {
    const fb = document.getElementById('rosterFilterBar');
    if (fb) fb.style.display = 'none';
  }
  // Update tab button active states
  ['upload','workspace','roster'].forEach(t => {
    const btn = document.querySelector(`.roster-tab[onclick="rosterTabSwitch('${t}')"]`);
    if (btn) btn.classList.toggle('active', t === tab);
  });
}

function rosterTabSwitch(tab) {
  window._rosterTab = tab;
  _applyRosterTabDisplay(tab);
  feEmit(`Roster view — ${tab}`);
  // When switching to Upload, ensure emissions log is ready if previously loaded
  if (tab === 'upload') {
    const l = document.getElementById('rosterEmissionsLog');
    if (l && l.classList.contains('open')) _renderEmissionsInto(l);
  }
}

function pmlToggleOverflow(force) {
  const menu = document.getElementById('pmlOverflowMenu');
  const btn  = document.getElementById('pmlOverflowBtn');
  if (!menu) return;
  const open = (force === undefined) ? menu.style.display === 'none' : !!force;
  menu.style.display = open ? '' : 'none';
  if (btn) btn.classList.toggle('active', open);
}
// Close overflow when clicking outside
document.addEventListener('click', e => {
  const wrap = document.getElementById('pmlOverflowWrap');
  if (wrap && !wrap.contains(e.target)) pmlToggleOverflow(false);
});

// ── Interaction handlers ───────────────────────────────────────
function pmlSetFilter(f) {
  _pmlFilter = f;
  const labels = { all: 'All', enrolled: 'Enrolled', flagged: 'Flagged', missing: 'Not in PML' };
  feEmit('PML filter — ' + (labels[f] || f));
  _rerenderPmlTable();
}

function pmlToggleDetail(key) {
  const el = document.getElementById(`pmld-${key}`);
  if (el) el.style.display = el.style.display === 'none' ? '' : 'none';
}

function togglePmlTaskDrawer(open) {
  const drawer   = document.getElementById('pmlTaskDrawer');
  const backdrop = document.getElementById('pmlTaskDrawerBackdrop');
  if (!drawer) return;
  const shouldOpen = (open === undefined) ? !drawer.classList.contains('open') : !!open;
  drawer.classList.toggle('open', shouldOpen);
  if (backdrop) backdrop.classList.toggle('open', shouldOpen);
  if (shouldOpen) {
    _loadPmlTaskState();  // refresh from lastRun in case a poll updated it
    _renderPmlTaskDrawer();
  }
}

function pmlToggleTask(tid) {
  const done = _loadPmlTaskDone();
  const wasDone = done.has(tid);
  if (wasDone) _clearPmlTaskDone(tid);
  else         _persistPmlTaskDone(tid);
  feEmit((wasDone ? 'PML task reopened — ' : '✓ PML task resolved — ') + tid, wasDone ? 'info' : 'ok');
  // re-render drawer if open
  const drawer = document.getElementById('pmlTaskDrawer');
  if (drawer?.classList.contains('open')) _renderPmlTaskDrawer();
  // re-render inline chip
  const rb = document.getElementById('pmlResultsBody');
  if (rb) rb.innerHTML = _buildPmlResultsHtml();
  // update resolve button in detail row if exists
  const btn = document.getElementById(`pmlresbtn-${tid}`);
  const isDone = _loadPmlTaskDone().has(tid);
  if (btn) { btn.textContent = isDone ? '✓ Resolved' : '+ Mark resolved'; btn.classList.toggle('resolved', isDone); }
}

function pmlEditNote(tid) {
  const wrap = document.getElementById(`pml-note-form-${tid}`);
  if (!wrap) return;
  if (wrap.style.display !== 'none') { wrap.style.display = 'none'; return; }
  const notes = _loadPmlTaskNotes();
  const existing = notes[tid]||'';
  wrap.style.display = '';
  wrap.innerHTML = `<textarea class="pml-task-note-input" id="pml-note-ta-${esc(tid)}" rows="2" placeholder="Add a note…">${esc(existing)}</textarea>
    <div style="display:flex;gap:.35rem;margin-top:.18rem">
      <button onclick="pmlSaveNote('${esc(tid)}')" style="font-size:.68rem;padding:.18rem .55rem;background:var(--indigo);color:#fff;border:none;border-radius:5px;cursor:pointer">Save</button>
      <button onclick="document.getElementById('pml-note-form-${esc(tid)}').style.display='none'" style="font-size:.68rem;background:none;border:none;cursor:pointer;color:var(--text-3)">Cancel</button>
    </div>`;
}

function pmlSaveNote(tid) {
  const ta = document.getElementById(`pml-note-ta-${tid}`);
  if (!ta) return;
  const notes = _loadPmlTaskNotes();
  const val = ta.value.trim();
  if (val) notes[tid] = val;
  else delete notes[tid];
  _savePmlTaskNotes(notes);
  feEmit('PML task note saved', 'ok');
  _renderPmlTaskDrawer();
}

function pmlSelectProviderLocation(tid, locIdx) {
  if (!_pmlTaskState.providerLocations) _pmlTaskState.providerLocations = {};
  _pmlTaskState.providerLocations[tid] = locIdx;
  _savePmlTaskState();
  const allLocs = window.lastRun?.orchestrator_state?.locations || [];
  const loc = allLocs[locIdx];
  const zip5 = loc ? (loc.site_zip5 || loc.site_zip || '').replace(/\D/g,'').slice(0,5) : '?';
  feEmit(`Practice location confirmed for ${tid} — ZIP ${zip5}`, 'ok');
  // Re-render the detail row in place
  const detailKey = tid.replace(/^pml-/, '');
  const detRow = document.getElementById(`pmld-${detailKey}`);
  if (detRow && detRow.style.display !== 'none') {
    const allRows = (() => {
      const { validated, flagged, missing } = _pmlData();
      return [...validated.map(r=>({...r,_missing:false})), ...flagged.map(r=>({...r,_missing:false})), ...missing.map(r=>({...r,_missing:true}))];
    })();
    const row = allRows.find(r => _pmlTaskKey(r) === detailKey);
    if (row) {
      const cell = detRow.querySelector('td');
      if (cell) cell.innerHTML = _pmlDetailHtml(row, tid, _loadPmlTaskDone().has(tid));
    }
  }
  // Also refresh results chip
  const rb = document.getElementById('pmlResultsBody');
  if (rb) rb.innerHTML = _buildPmlResultsHtml();
}

function pmlClearProviderLocation(tid) {
  if (_pmlTaskState.providerLocations) delete _pmlTaskState.providerLocations[tid];
  _savePmlTaskState();
  feEmit(`Practice location cleared — ${tid}`);
  // Re-render detail row
  const detailKey = tid.replace(/^pml-/, '');
  const detRow = document.getElementById(`pmld-${detailKey}`);
  if (detRow && detRow.style.display !== 'none') {
    const allRows = (() => {
      const { validated, flagged, missing } = _pmlData();
      return [...validated.map(r=>({...r,_missing:false})), ...flagged.map(r=>({...r,_missing:false})), ...missing.map(r=>({...r,_missing:true}))];
    })();
    const row = allRows.find(r => _pmlTaskKey(r) === detailKey);
    if (row) {
      const cell = detRow.querySelector('td');
      if (cell) cell.innerHTML = _pmlDetailHtml(row, tid, _loadPmlTaskDone().has(tid));
    }
  }
}

function pmlShowAllLocations(tid) {
  const wrap = document.getElementById(`pml-allLocs-${tid}`);
  if (!wrap) return;
  if (wrap.style.display !== 'none') { wrap.style.display = 'none'; return; }
  const allLocs = window.lastRun?.orchestrator_state?.locations || [];
  wrap.style.display = '';
  wrap.innerHTML = `<div class="pml-loc-picker" style="margin-top:.2rem">
    ${allLocs.map((l,i) => {
      const zip5 = (l.site_zip5 || l.site_zip || '').replace(/\D/g,'').slice(0,5);
      const addr = [l.site_address_line_1 || l.site_address, l.site_city, l.site_state].filter(Boolean).join(', ');
      return `<div class="pml-loc-option">
        <span class="pml-loc-dot option"></span>
        <div style="flex:1">
          <div class="pml-loc-name">${esc(addr)}</div>
          <div class="pml-loc-zip" style="margin-top:.04rem">${esc(zip5||'—')}</div>
        </div>
        <button class="pml-loc-action select-btn" onclick="event.stopPropagation();pmlSelectProviderLocation('${esc(tid)}',${i})">Select</button>
      </div>`;
    }).join('')}
  </div>`;
}

function pmlDismissRow(key) {
  _dismissPmlRow(key);
  // Re-render table (row disappears)
  const rb = document.getElementById('pmlResultsBody');
  if (rb) rb.innerHTML = _buildPmlResultsHtml();
  // Re-render drawer if open
  const drawer = document.getElementById('pmlTaskDrawer');
  if (drawer?.classList.contains('open')) _renderPmlTaskDrawer();
}

// Create a manual PML task from the detail card, pre-filled with note
function pmlCreateManualTask(tid, provName, npi, note) {
  const text = note?.trim()
    ? note.trim().slice(0, 120)
    : `Review PML enrollment for ${provName || npi || 'provider'}`;
  if (!_pmlTaskState.manual) _pmlTaskState.manual = [];
  const taskId = `pml-manual-${tid}-${Date.now()}`;
  _pmlTaskState.manual.push({
    id: taskId, prov: provName || npi || '—', npi: npi || '',
    sev: 'medium', text, det: note?.trim() || '',
    allIssues: [], ppl_found: false, ppl_programs: [], ppl_contract_date: null,
    manual: true, warnOnly: false,
  });
  _savePmlTaskState();
  feEmit(`Task created — ${provName || npi}`, 'ok');
  _renderPmlTaskDrawer();
  togglePmlTaskDrawer(true);
}

// Export a single provider's clean PML correction record as a CSV download
function pmlExportProviderRecord(r) {
  const _loc = (window.lastRun?.orchestrator_state?.locations || []);
  const _locZip5 = l => (l?.site_zip5 || l?.site_zip || '').replace(/\D/g,'').slice(0, 5);
  const _locAddr = l => l ? [l.site_address_line_1 || l.site_address, l.site_city, l.site_state].filter(Boolean).join(', ') : '';

  // Confirmed location (if selected)
  const savedLocIdx = (_pmlTaskState.providerLocations || {})[`pml-${r.npi}-${r.taxonomy_code}`];
  const confirmedLoc = savedLocIdx != null ? _loc[savedLocIdx] : null;

  // Corrected ZIP comes from the user-confirmed service location, never from NPPES
  const _cLoc = confirmedLoc;
  const _cLocZip5 = _cLoc ? (_cLoc.site_zip5 || _cLoc.site_zip || '').replace(/\D/g,'').slice(0,5) : '';
  const correctedZip  = _cLocZip5 || r.zip || '';
  const correctedAddr = _cLoc ? (_cLoc.site_address_line_1 || _cLoc.site_address || r.address_line_1) : r.address_line_1;
  const correctedCity = _cLoc ? (_cLoc.site_city || r.city) : r.city;
  const correctedState = r.state || 'FL';
  const _actionDesc = _cLocZip5 && _cLocZip5 !== (r.zip9||r.zip||'').slice(0,5)
    ? `Update PML enrollment ZIP from ${r.current_zip_display || r.zip9?.slice(0,5) || r.zip} to ${_cLocZip5} (confirmed service location) — contact FL AHCA`
    : (r.recommendation || '');

  // Retrieve note from textarea
  const _noteKey = `pml-card-note-pml-${r.npi}-${r.taxonomy_code}`;
  const note = (() => { try { return localStorage.getItem(_noteKey) || ''; } catch(_) { return ''; } })();

  const headers = [
    'NPI','Provider Name','Medicaid Provider ID','Taxonomy Code',
    'Address Line 1 (Corrected)','City (Corrected)','State','ZIP-9 (Corrected)',
    'Enrollment Status','Contract Effective Date','Contract End Date',
    'Current ZIP on File','Issues','Warnings','Notes','Required Action'
  ];
  const row = [
    r.npi, r.provider_name, r.medicaid_provider_id, r.taxonomy_code,
    correctedAddr, correctedCity, correctedState, correctedZip,
    r.enrollment_status || '', r.contract_effective_date || '', r.contract_end_date || '',
    r.zip9 ? `${r.zip9.slice(0,5)}-${r.zip9.slice(5)}` : (r.zip || ''),
    (r.issues || []).join('; '), (r.warnings || []).map(w => w.split(':')[0]).join('; '),
    note, _actionDesc
  ];
  const csv = [headers, row].map(cols =>
    cols.map(c => `"${String(c||'').replace(/"/g,'""')}"`).join(',')
  ).join('\n');
  const blob = new Blob([csv], { type: 'text/csv' });
  const a = document.createElement('a'); a.href = URL.createObjectURL(blob);
  a.download = `pml-correction-${r.npi}.csv`; a.click();
  feEmit(`Exported PML correction record for ${r.provider_name || r.npi}`);
}

// Export ALL flagged providers' correction records as one CSV
function pmlExportAllCorrections() {
  const { flagged, missing } = _pmlData();
  if (!flagged.length && !missing.length) {
    feEmit('No flagged providers to export'); return;
  }
  const _loc = (window.lastRun?.orchestrator_state?.locations || []);
  const headers = [
    'NPI','Provider Name','Medicaid Provider ID','Taxonomy Code',
    'Current Address Line 1','Current City','State','Current ZIP-9',
    'Corrected ZIP-9','Correct Source',
    'Enrollment Status','Contract Effective Date','Contract End Date',
    'Issues','Warnings','Required Action'
  ];
  const _locZip5b = l => (l?.site_zip5 || l?.site_zip || '').replace(/\D/g,'').slice(0,5);
  const rows = [...flagged, ...missing].map(r => {
    const _tid = `pml-${r.npi}-${r.taxonomy_code}`;
    const _savedIdx = (_pmlTaskState.providerLocations || {})[_tid];
    const _cL = _savedIdx != null ? _loc[_savedIdx] : null;
    const _cZip5 = _cL ? _locZip5b(_cL) : '';
    // Corrected ZIP from confirmed service location only — never from NPPES
    const correctedZip = _cZip5 || '';
    const correctSrc = _cZip5 ? 'user_confirmed_location' : (r.correct_source || 'pending_selection');
    const hasZipIssue = (r.issues||[]).some(i => i==='zip_mismatch_location'||i==='zip_not_9_digits');
    const actionDesc = hasZipIssue && _cZip5 && _cZip5 !== (r.zip9||r.zip||'').slice(0,5)
      ? `Update PML enrollment ZIP from ${r.current_zip_display || r.zip9?.slice(0,5) || r.zip} to ${_cZip5} (confirmed service location) — contact FL AHCA`
      : (hasZipIssue && !_cZip5 ? 'Pending: select confirmed service location in PML tab'
        : (r.recommendation || ''));
    return [
      r.npi, r.provider_name, r.medicaid_provider_id, r.taxonomy_code,
      r.address_line_1, r.city, r.state || 'FL',
      r.zip9 ? `${r.zip9.slice(0,5)}-${r.zip9.slice(5)}` : (r.zip || ''),
      correctedZip, correctSrc,
      r.enrollment_status || '', r.contract_effective_date || '', r.contract_end_date || '',
      (r.issues || []).join('; '), (r.warnings || []).map(w => w.split(':')[0]).join('; '),
      actionDesc
    ];
  });
  const csv = [headers, ...rows].map(cols =>
    cols.map(c => `"${String(c||'').replace(/"/g,'""')}"`).join(',')
  ).join('\n');
  const blob = new Blob([csv], { type: 'text/csv' });
  const a = document.createElement('a'); a.href = URL.createObjectURL(blob);
  a.download = `pml-corrections-all-${new Date().toISOString().slice(0,10)}.csv`; a.click();
  feEmit(`Exported ${rows.length} PML correction records`);
}

function pmlDeleteTask(tid) {
  _pmlTaskState.manual = (_pmlTaskState.manual||[]).filter(t=>t.id!==tid);
  _pmlTaskState.done   = (_pmlTaskState.done||[]).filter(x=>x!==tid);
  delete (_pmlTaskState.notes||{})[tid];
  _savePmlTaskState();
  feEmit('PML task deleted — ' + tid);
  _renderPmlTaskDrawer();
}

function pmlAddTaskForm() {
  const wrap = document.getElementById('pmlAddTaskFormWrap');
  if (!wrap) return;
  if (wrap.style.display !== 'none') { wrap.style.display = 'none'; return; }
  wrap.style.display = '';
  wrap.innerHTML = `<div style="display:flex;flex-direction:column;gap:.3rem">
    <input id="pmlNewTaskProv" type="text" placeholder="Provider name or NPI" style="border:1.5px solid var(--border);border-radius:6px;padding:.28rem .5rem;font-size:.75rem;font-family:var(--font);outline:none">
    <input id="pmlNewTaskText" type="text" placeholder="Issue / task description" style="border:1.5px solid var(--border);border-radius:6px;padding:.28rem .5rem;font-size:.75rem;font-family:var(--font);outline:none">
    <select id="pmlNewTaskSev" style="border:1.5px solid var(--border);border-radius:6px;padding:.28rem .5rem;font-size:.75rem;font-family:var(--font);outline:none">
      <option value="high">High</option><option value="medium" selected>Medium</option><option value="low">Low</option>
    </select>
    <div style="display:flex;gap:.35rem">
      <button onclick="pmlSaveNewTask()" style="font-size:.72rem;padding:.22rem .65rem;background:var(--indigo);color:#fff;border:none;border-radius:6px;cursor:pointer;font-weight:600">Add</button>
      <button onclick="document.getElementById('pmlAddTaskFormWrap').style.display='none'" style="font-size:.72rem;background:none;border:none;cursor:pointer;color:var(--text-3)">Cancel</button>
    </div>
  </div>`;
}

function pmlSaveNewTask() {
  const prov = (document.getElementById('pmlNewTaskProv')?.value||'').trim();
  const text = (document.getElementById('pmlNewTaskText')?.value||'').trim();
  const sev  = document.getElementById('pmlNewTaskSev')?.value||'medium';
  if (!text) { alert('Please enter a task description.'); return; }
  const manual = _loadPmlManualTasks();
  const id = `pml-manual-${Date.now()}`;
  manual.push({ id, prov: prov||'—', npi:'', sev, text, det:'', allIssues:[text], ppl_found:false, ppl_programs:[], ppl_contract_date:null, manual:true });
  _savePmlManualTasks(manual);
  feEmit('PML task added — ' + text);
  // close form and re-render
  const w = document.getElementById('pmlAddTaskFormWrap');
  if (w) w.style.display = 'none';
  _renderPmlTaskDrawer();
  // refresh chip
  const rb = document.getElementById('pmlResultsBody');
  if (rb) rb.innerHTML = _buildPmlResultsHtml();
}

async function refreshPmlData(btn) {
  const rid = lastRun?.run_id;
  if (!rid) return;
  feEmit('Refreshing PML enrollment data…');
  if (btn) { btn.disabled = true; btn.innerHTML = '<span class="spinner" style="width:11px;height:11px;border-width:1.5px;display:inline-block;vertical-align:middle"></span> Refreshing…'; }
  try {
    await fetch(`${API}/chat/credentialing-runs/${encodeURIComponent(rid)}/validate`, {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({ step_id: 'pml_alignment', validated_output: { rerun: true } }),
    });
    // Re-fetch latest run state and re-render in-place.
    // Do NOT call loadRun() — that resets _viewStepId and navigates away.
    const r = await fetch(`${API}/chat/credentialing-runs/${encodeURIComponent(rid)}?full=1`);
    if (r.ok) {
      const data = await r.json();
      // Ensure we stay on the pml_alignment step view
      _viewStepId = 'pml_alignment';
      render(data);
      schedulePoll(data);
    }
    feEmit('✓ PML · TML · PPL data refreshed', 'ok');
    // Re-render task drawer if open
    const drawer = document.getElementById('pmlTaskDrawer');
    if (drawer?.classList.contains('open')) _renderPmlTaskDrawer();
  } catch (e) {
    feEmit('PML refresh failed — ' + e.message, 'error');
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = 'Refresh'; }
  }
}

// ── Taxonomy Task State (Step 6) ─────────────────────────────────────────────
// Mirrors the PML task state pattern: in-memory cache, localStorage fallback, DB PATCH.

let _taxTaskState = { done: [], notes: {}, dismissed: [] };

function _taxTaskStateKey() { return `mobius-tax-ts-${lastRun?.run_id || 'default'}`; }

function _loadTaxTaskState() {
  const dbState = window.lastRun?.orchestrator_state?.taxonomy_task_state;
  if (dbState && (dbState.done?.length || Object.keys(dbState.notes||{}).length)) {
    _taxTaskState = { done: dbState.done||[], notes: dbState.notes||{}, dismissed: dbState.dismissed||[] };
    try { localStorage.setItem(_taxTaskStateKey(), JSON.stringify(_taxTaskState)); } catch {}
    return;
  }
  try {
    const ls = JSON.parse(localStorage.getItem(_taxTaskStateKey()) || 'null');
    if (ls) { _taxTaskState = ls; return; }
  } catch {}
}

let _taxPatchTimer = null;
function _saveTaxTaskState() {
  try { localStorage.setItem(_taxTaskStateKey(), JSON.stringify(_taxTaskState)); } catch {}
  if (_taxPatchTimer) clearTimeout(_taxPatchTimer);
  _taxPatchTimer = setTimeout(_flushTaxTaskState, 600);
}

async function _flushTaxTaskState() {
  const rid = lastRun?.run_id;
  if (!rid) return;
  try {
    await fetch(`${API}/chat/credentialing-runs/${encodeURIComponent(rid)}/taxonomy-tasks`, {
      method: 'PATCH', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(_taxTaskState),
    });
  } catch (e) {
    console.warn('Taxonomy task state PATCH failed:', e.message);
  }
}

function _loadTaxTaskDone()      { return new Set(_taxTaskState.done || []); }
function _loadTaxTaskDismissed() { return new Set(_taxTaskState.dismissed || []); }
function _persistTaxTaskDone(id) {
  if (!_taxTaskState.done.includes(id)) _taxTaskState.done.push(id);
  _saveTaxTaskState();
}
function _clearTaxTaskDone(id) {
  _taxTaskState.done = (_taxTaskState.done||[]).filter(d => d !== id);
  _saveTaxTaskState();
}
function _dismissTaxTask(id) {
  if (!(_taxTaskState.dismissed||[]).includes(id)) {
    if (!_taxTaskState.dismissed) _taxTaskState.dismissed = [];
    _taxTaskState.dismissed.push(id);
    _saveTaxTaskState();
  }
}

/** Derive taxonomy tasks from orchestrator_state.taxonomy_analysis.
 *
 * Severity calibration based on delta_billing_pct:
 *   > 20% at risk → high  (critical task, shown in drawer)
 *   5–20%         → medium (standard task, shown in drawer)
 *   < 5% or no delta → low (inline warning only, not queued)
 *
 * result_type:
 *   'restriction' = gap codes cover HCPC codes that approved codes don't → task
 *   'gap_only'    = enrollment/TML gap but no procedure delta → warning only
 *   'clean'       = no issues
 */
function _buildTaxonomyAutoTasks() {
  const analysis = window.lastRun?.orchestrator_state?.taxonomy_analysis || [];
  const dismissed = _loadTaxTaskDismissed();
  const tasks = [];

  analysis.forEach(a => {
    const npi  = a.npi  || '';
    const name = a.provider_name || npi || '—';
    const rt   = a.result_type || 'clean';
    const delta = parseFloat(a.delta_billing_pct || 0);
    const deltaHcpcs = a.delta_hcpcs || [];

    if (rt === 'clean' || rt === 'no_nppes_taxonomies') return;

    // Determine severity and whether to queue a task or only show inline warning
    let sev, text, det, inlineOnly = false;
    if (rt === 'restriction') {
      if (delta > 20) {
        sev = 'high';
        text = `Billing restriction: ${deltaHcpcs.length} HCPC code(s) at risk (${delta.toFixed(1)}% of billing)`;
      } else if (delta >= 5) {
        sev = 'medium';
        text = `Taxonomy gap: ${deltaHcpcs.length} HCPC code(s) may be unbillable (${delta.toFixed(1)}% of billing)`;
      } else {
        sev = 'low';
        inlineOnly = true;
        text = `Minor taxonomy gap (${delta.toFixed(1)}% of billing — below threshold)`;
      }
      const codes = (a.codes||[]).filter(c => c.status !== 'approved_enrolled').map(c => c.code).join(', ');
      det = `Gap codes: ${codes || 'unknown'}. ${deltaHcpcs.slice(0,3).map(h=>h.hcpcs_code).join(', ')}${deltaHcpcs.length>3?'…':''}`;
    } else if (rt === 'gap_only') {
      sev = 'low';
      inlineOnly = true;
      const codes = (a.codes||[]).filter(c => c.status !== 'approved_enrolled').map(c => c.code).join(', ');
      text = `Enrollment gap: ${codes || 'taxonomy code'} — TML approved but missing PML enrollment`;
      det = 'No billing impact detected. Recommend enrolling the missing taxonomy code in PML.';
    } else {
      return;
    }

    const taskId = `tax-${npi}-${rt}`;
    if (dismissed.has(taskId)) return;
    if (inlineOnly) return;  // <5% or gap_only — warn inline, don't queue a task

    tasks.push({ id: taskId, prov: name, npi, sev, text, det, result_type: rt, delta, deltaHcpcs, manual: false });
  });

  return tasks;
}

function toggleTaxTaskDrawer(open) {
  const drawer   = document.getElementById('taxTaskDrawer');
  const backdrop = document.getElementById('taxTaskDrawerBackdrop');
  if (!drawer) return;
  const shouldOpen = (open === undefined) ? !drawer.classList.contains('open') : !!open;
  drawer.classList.toggle('open', shouldOpen);
  if (backdrop) backdrop.classList.toggle('open', shouldOpen);
  if (shouldOpen) {
    _loadTaxTaskState();
    _renderTaxTaskDrawer();
  }
}

function _renderTaxTaskDrawer() {
  const tasks = _buildTaxonomyAutoTasks() || [];
  const done  = _loadTaxTaskDone();
  const openT = tasks.filter(t => !done.has(t.id));
  const doneT = tasks.filter(t =>  done.has(t.id));

  const sevColor = sev => sev === 'high' ? 'var(--red)' : sev === 'medium' ? 'var(--amber,#d97706)' : 'var(--text-3)';
  const renderItem = (t, isDone) => `
    <div style="border:1px solid var(--border);border-left:3px solid ${isDone?'var(--border)':sevColor(t.sev)};border-radius:7px;padding:.45rem .6rem;margin-bottom:.3rem;background:${isDone?'var(--surface)':'var(--bg)'}">
      <div style="display:flex;align-items:flex-start;gap:.4rem">
        <div style="flex:1;min-width:0">
          <div style="font-size:.78rem;font-weight:600;color:${isDone?'var(--text-3)':'var(--text)'}${isDone?';text-decoration:line-through':''}">${esc(t.prov)}</div>
          <div style="font-size:.72rem;color:${isDone?'var(--text-3)':'var(--text-2)'};margin-top:.1rem">${esc(t.text)}</div>
          ${t.det?`<div style="font-size:.68rem;color:var(--text-3);margin-top:.15rem">${esc(t.det)}</div>`:''}
        </div>
        <div style="display:flex;flex-direction:column;align-items:flex-end;gap:.2rem;flex-shrink:0">
          <span style="font-size:.62rem;font-weight:700;text-transform:uppercase;letter-spacing:.04em;color:${sevColor(t.sev)}">${t.sev}</span>
          <button onclick="taxToggleTask('${esc(t.id)}')"
            style="font-size:.68rem;padding:.15rem .45rem;border-radius:5px;border:1px solid var(--border);background:${isDone?'var(--green-bg)':'var(--surface)'};color:${isDone?'var(--green)':'var(--text-2)'};cursor:pointer">
            ${isDone ? '✓ Done' : 'Resolve'}
          </button>
          ${t.npi?`<a href="../roster/index.html?org=${encodeURIComponent(lastRun?.org_name||'')}&npi=${encodeURIComponent(t.npi)}" target="_blank" style="font-size:.65rem;color:var(--indigo);text-decoration:none">Roster →</a>`:''}
        </div>
      </div>
    </div>`;

  const body = document.getElementById('taxTaskDrawerBody');
  if (!body) return;
  let html = '';
  if (!tasks.length) {
    html = `<div style="font-size:.75rem;color:var(--green);text-align:center;padding:2rem .5rem">✓ No billing restrictions detected.</div>`;
  } else {
    if (openT.length) {
      html += `<div style="font-size:.7rem;font-weight:700;color:var(--text-3);margin-bottom:.15rem">Open (${openT.length})</div>`;
      html += openT.map(t => renderItem(t, false)).join('');
    }
    if (doneT.length) {
      html += `<div style="font-size:.7rem;color:var(--text-3);margin:.55rem 0 .15rem;font-weight:600">Done (${doneT.length})</div>`;
      html += doneT.map(t => renderItem(t, true)).join('');
    }
  }
  body.innerHTML = html;
}

function taxToggleTask(tid) {
  const done = _loadTaxTaskDone();
  if (done.has(tid)) _clearTaxTaskDone(tid);
  else               _persistTaxTaskDone(tid);
  const drawer = document.getElementById('taxTaskDrawer');
  if (drawer?.classList.contains('open')) _renderTaxTaskDrawer();
}

/** Build inline chip for taxonomy step header (mirrors _buildPmlTaskChipHtml). */
function _buildTaxTaskChipHtml() {
  const tasks = _buildTaxonomyAutoTasks() || [];
  const done  = _loadTaxTaskDone();
  const open  = tasks.filter(t => !done.has(t.id));
  if (!tasks.length) return '';
  const allDone = open.length === 0;
  const critCount = open.filter(t => t.sev === 'high').length;
  const medCount  = open.filter(t => t.sev === 'medium').length;
  const parts = [];
  if (critCount) parts.push(`<span style="color:var(--red)">✗ ${critCount} critical</span>`);
  if (medCount)  parts.push(`<span style="color:var(--amber,#d97706)">⚠ ${medCount} standard</span>`);
  const label = allDone ? '✓ All resolved'
    : parts.length ? parts.join('<span style="opacity:.4;margin:0 .2rem">·</span>')
    : `⚠ ${open.length} open`;
  return `<span style="font-size:.72rem;cursor:pointer;border:1px solid var(--border);border-radius:6px;padding:.12rem .45rem;background:var(--surface)"
    onclick="toggleTaxTaskDrawer(true)" title="Open taxonomy task drawer">${label}</span>`;
}

// ── END Payor Enrollment helpers ──────────────────────────────────────────────
