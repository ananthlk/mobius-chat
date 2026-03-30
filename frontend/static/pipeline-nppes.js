// ── Roster ↔ All Sources merge ────────────────────────────────
// Returns a copy of window._provData enriched with live roster decisions.
// Providers validated in the Roster tab get _rosterDecision='validated' so
// the All Sources table can reflect the current state without a pipeline re-run.
function _getMergedProviders() {
  const base = window._provData || [];
  const rs   = window._rosterUploadState;
  if (!rs || rs.phase !== 'done' || !rs.report) return base;

  const clean = rs.report.clean || [];
  if (!clean.length) return base;

  // Build lookup by NPI and normalized name
  const byNpi  = {};
  const byName = {};
  for (const rp of clean) {
    const npi = rp.npi_uploaded || rp.latest_validation?.npi_validated;
    if (npi) byNpi[npi] = rp;
    const nameKey = (rp.provider_name || '').toLowerCase().replace(/[^a-z0-9]/g, '');
    if (nameKey) byName[nameKey] = rp;
  }

  // Merge roster state into pipeline providers
  const merged = base.map(p => {
    const pNpi    = p.npi;
    const nameKey = (p.name || '').toLowerCase().replace(/[^a-z0-9]/g, '');
    const rp = (pNpi && byNpi[pNpi]) || byName[nameKey];
    if (!rp) return p;
    const validatedNpi = rp.npi_uploaded || rp.latest_validation?.npi_validated;
    return {
      ...p,
      npi: (rp._decision === 'validated' && validatedNpi) ? validatedNpi : p.npi,
      _rosterDecision: rp._decision || null,
      sources: rp._decision === 'validated'
        ? [...new Set([...(p.sources || []), 'roster'])]
        : (p.sources || []),
    };
  });

  // Also surface roster-only providers not in the pipeline list
  const pipelineNpis  = new Set(base.map(p => p.npi).filter(Boolean));
  const pipelineNames = new Set(base.map(p => (p.name||'').toLowerCase().replace(/[^a-z0-9]/g,'')));
  for (const rp of clean) {
    if (rp._decision !== 'validated') continue;
    const npi  = rp.npi_uploaded || rp.latest_validation?.npi_validated;
    const nkey = (rp.provider_name||'').toLowerCase().replace(/[^a-z0-9]/g,'');
    if ((npi && pipelineNpis.has(npi)) || pipelineNames.has(nkey)) continue;
    // New validated provider not yet in pipeline output
    merged.push({
      name:            rp.provider_name,
      npi:             npi || '',
      specialty:       rp.specialty_uploaded || rp.latest_validation?.specialty_validated || '',
      sources:         ['roster'],
      bucket:          'needs_attention',
      anomalies:       [],
      _rosterDecision: 'validated',
      _rosterNew:      true,
    });
  }
  return merged;
}

// Update flow graph counts + tab badges + reconciliation table whenever roster state changes.
function _syncRosterToAllSources() {
  const rs = window._rosterUploadState;
  if (!rs || rs.phase !== 'done' || !rs.report) return;

  const clean          = rs.report.clean || [];
  const validatedCount = clean.filter(p => p._decision === 'validated').length;
  const rejectedCount  = clean.filter(p => p._decision === 'rejected').length;
  const pendingCount   = clean.filter(p => !p._decision).length;
  const totalClean     = clean.length;

  // ── Roster flow-graph node ─────────────────────────────────
  const countEl = document.getElementById('rosterNodeCount');
  if (countEl) countEl.textContent = totalClean;
  const subEl = document.getElementById('rosterNodeSub');
  if (subEl) {
    if (validatedCount > 0) {
      subEl.innerHTML = `<span style="color:var(--green)">${validatedCount} validated</span>`
        + (pendingCount > 0 ? ` <span style="color:var(--text-3)">· ${pendingCount} pending</span>` : '');
    } else {
      subEl.innerHTML = `<span style="color:var(--text-3)">${totalClean} providers</span>`;
    }
  }

  // ── Output bucket counts ───────────────────────────────────
  const el = (id, val) => { const e = document.getElementById(id); if (e) e.textContent = val; };
  el('confirmedCount', validatedCount);
  el('pendingCount',   pendingCount);
  el('rejectedCount',  rejectedCount);

  // ── Roster tab badge + dot ─────────────────────────────────
  const rosterTab = document.querySelector('[data-tab="roster"]');
  if (rosterTab) {
    const badge = document.getElementById('rosterTabCount') || rosterTab.querySelector('.src-tab-count');
    if (badge) badge.textContent = validatedCount > 0 ? `${validatedCount}/${totalClean}` : totalClean;
    const dot = rosterTab.querySelector('.src-tab-dot');
    if (dot) dot.className = 'src-tab-dot ' + (validatedCount === totalClean && totalClean > 0 ? 'green' : validatedCount > 0 ? 'amber' : 'grey');
  }

  // ── Reconciliation tab badge + dot ────────────────────────
  const reconTab = document.querySelector('[data-tab="all"]');
  if (reconTab) {
    const badge = document.getElementById('reconTabCount') || reconTab.querySelector('.src-tab-count');
    if (badge) badge.textContent = totalClean;
    const dot = reconTab.querySelector('.src-tab-dot');
    if (dot) dot.className = 'src-tab-dot ' + (pendingCount === 0 && totalClean > 0 ? 'green' : validatedCount > 0 ? 'amber' : 'grey');
  }

  // ── Filter pills ───────────────────────────────────────────
  document.querySelectorAll('.prov-filter-pill').forEach(pill => {
    const f = pill.dataset.filter;
    const count = f === 'confirmed' ? validatedCount
      : f === 'pending' ? pendingCount
      : f === 'rejected' ? rejectedCount
      : totalClean;
    pill.textContent = pill.textContent.replace(/\d+$/, count);
  });

  // ── NPI Reconciliation tab — refresh live sub-sections ──────
  const rosterSec = document.getElementById('rosterSection');
  if (rosterSec) rosterSec.innerHTML = _buildRosterSectionHtml();
  const s3 = document.getElementById('reconSection3');
  if (s3) s3.innerHTML = _buildReconSection3Html(window._reconFilter || 'needs-help');
  _refreshTaskQueueFull();
}

// ═══════════════════════════════════════════════════════════════
// NPI RECONCILIATION SCORE & TASK QUEUE (Option C)
// ═══════════════════════════════════════════════════════════════

function _getNppiStatus(p) {
  // alignment.status.nppes is where the reconciliation service stores 'A' / 'D'
  const vd = p.latest_validation?.validation_details || {};
  return (vd.alignment?.status?.nppes || vd.nppes_status || vd.status || vd.basic?.status || 'A').toUpperCase();
}

function _isDeactivated(p) {
  return _getNppiStatus(p) === 'D';
}

function _computeReconScore() {
  const rs = window._rosterUploadState;
  if (!rs || rs.phase !== 'done' || !rs.report) return null;

  // Use backend-computed score when available (preferred — single source of truth)
  if (rs.report.roster_score?.score != null) {
    const s = rs.report.roster_score;
    // Ensure camelCase aliases expected by rendering code
    return {
      score:            s.score,
      band:             s.band,
      bandLabel:        s.band_label,
      matched:          s.matched,
      totalClean:       s.total_clean,
      deactivatedCount: s.deactivated_count,
      alignOk:          s.align_ok,
      alignIssues:      s.align_issues,
      ghosts:           s.ghost_count,
      matchPct:         s.match_pct,
      alignPct:         s.align_pct,
      baseScore:        s.base_score,
      deactivatedPenalty: s.deact_penalty,
      ghostPenalty:     s.ghost_penalty,
    };
  }

  // JS fallback for un-enriched / legacy reports
  const clean = rs.report.clean || [];
  const active = clean.filter(p => p._decision !== 'excluded');
  const totalClean = active.length;
  if (!totalClean) return null;
  let matched = 0, deactivatedCount = 0, alignOk = 0, alignIssues = 0;
  for (const p of active) {
    const vr = p.latest_validation;
    if (!vr?.npi_validated) continue;
    matched++;
    if (_isDeactivated(p)) { deactivatedCount++; continue; }
    const alignSum = vr?.validation_details?.alignment?.summary || [];
    const hard = alignSum.filter(k => k !== 'name' || vr?.validation_details?.alignment?.name?.flag === 'mismatch');
    hard.length === 0 ? alignOk++ : alignIssues++;
  }
  const provData = window._provData || [];
  const ghosts = provData.filter(p => (p.sources||[]).includes('nppes') && !(p.sources||[]).includes('roster')).length;
  const matchCov = matched / totalClean;
  const alignHealth = matched > 0 ? alignOk / matched : 0;
  const baseScore = Math.round((matchCov * 0.80 + alignHealth * 0.20) * 100);
  const deactivatedPenalty = deactivatedCount * 2;
  const ghostPenalty = Math.min(Math.round((ghosts / totalClean) * 50), 30);
  const score = Math.max(0, Math.min(100, baseScore - deactivatedPenalty - ghostPenalty));
  const band  = score >= 85 ? 'green' : score >= 65 ? 'amber' : 'red';
  return { score, band, bandLabel: score >= 85 ? 'Roster in good shape' : score >= 65 ? 'Some gaps to address' : 'Credentialing risk',
    matched, totalClean, deactivatedCount, alignOk, alignIssues, ghosts,
    matchPct: Math.round(matchCov * 100), alignPct: Math.round(alignHealth * 100),
    baseScore, deactivatedPenalty, ghostPenalty };
}

function _buildReconScoreHtml() {
  const s = _computeReconScore();
  if (!s) return `<div class="recon-score-bar"><span style="font-size:.8rem;color:var(--text-3)">Upload and validate providers to see your roster health score.</span></div>`;
  const tip = `Score = NPI coverage (${s.matchPct}% × 80%) + alignment health (${s.alignPct}% × 20%) − ${s.deactivatedPenalty}pts deactivated − ${s.ghostPenalty}pts ghost`;

  // Three-tier category counts
  const clean = window._rosterUploadState?.report?.clean || [];
  const noIssues  = clean.filter(p => _aiCategory(p) === 'no-issues').length;
  const confident = clean.filter(p => _aiCategory(p) === 'mobius-confident').length;
  const needsHelp = clean.filter(p => _aiCategory(p) === 'mobius-needs-help').length;
  const deact     = clean.filter(p => _reconCat(p) === 'deactivated').length;
  const allTasks  = (_reconTasks || []).filter(t => !t.done && t.type !== 'confirmed');
  const openTasks = allTasks.length;
  // Tracked = providers with open tasks that are in needs-attention bucket
  const trackedIdxSet = new Set(allTasks.filter(t => t.providerIdx != null).map(t => t.providerIdx));
  const tracked = clean.filter((p, i) => trackedIdxSet.has(i) && _aiCategory(p) !== 'no-issues').length;

  const catDot = (color) => `<span style="width:7px;height:7px;border-radius:50%;background:${color};display:inline-block;flex-shrink:0"></span>`;

  return `
    <div class="recon-score-bar" id="reconScoreBar">
      <div class="recon-score-num ${s.band}" id="reconScoreNum" title="${esc(tip)}">${s.score}</div>
      <div style="flex:1;display:flex;flex-direction:column;gap:.2rem;min-width:0">
        <div style="display:flex;align-items:center;gap:.5rem;flex-wrap:wrap">
          <div class="recon-score-label ${s.band}">${s.bandLabel}</div>
          <span style="font-size:.67rem;color:var(--text-3);cursor:help" title="${esc(tip)}">ⓘ how scored</span>
        </div>
        <!-- Category counts inline — no colored backgrounds, just dots for signal -->
        <div style="display:flex;gap:.6rem;flex-wrap:wrap;align-items:center">
          <span style="display:flex;align-items:center;gap:.2rem;font-size:.75rem;color:var(--text-3)">${catDot('var(--green)')} <span style="color:var(--text-2)">${noIssues}</span> no issues</span>
          ${confident ? `<span style="display:flex;align-items:center;gap:.2rem;font-size:.75rem;color:var(--text-3)">${catDot('var(--indigo)')} <span style="color:var(--text-2)">${confident}</span> confident</span>` : ''}
          ${needsHelp ? `<span style="display:flex;align-items:center;gap:.2rem;font-size:.75rem;color:var(--text-3)">${catDot('var(--amber,#f59e0b)')} <span style="color:var(--amber,#d97706);font-weight:600">${needsHelp}</span> need input</span>` : ''}
          ${deact     ? `<span style="display:flex;align-items:center;gap:.2rem;font-size:.75rem;color:var(--text-3)">${catDot('#dc2626')} <span style="color:var(--red);font-weight:600">${deact}</span> deactivated</span>` : ''}
        </div>
      </div>
      <button onclick="_openStepTaskDrawer()" id="taskDrawerToggleBtn"
        style="flex-shrink:0;display:flex;align-items:center;gap:.35rem;font-size:.72rem;font-weight:600;padding:.28rem .65rem;border-radius:7px;border:1px solid var(--border);background:var(--surface);color:var(--text-2);cursor:pointer;white-space:nowrap;transition:all .15s"
        title="Toggle task panel">
        <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" style="width:12px;height:12px"><rect x="2" y="3" width="12" height="10" rx="1.5"/><path d="M5 6h6M5 9h4"/></svg>
        Tasks${openTasks > 0 ? ` <span style="background:var(--indigo);color:#fff;border-radius:10px;padding:.05rem .4rem;font-size:.6rem">${openTasks}</span>` : ''}
      </button>
    </div>`;
}
// ── Section HTML builders ──────────────────────────────────────

function _buildReconStatBar() {
  const rs = window._rosterUploadState;

  // No roster yet — show the upload prompt
  if (!rs || rs.phase === 'uploading') {
    return `<div style="display:flex;align-items:center;gap:.75rem;padding:.6rem .25rem;font-size:.8125rem;color:var(--text-3)">
      No roster loaded yet.
      <button class="recon-refresh-btn" onclick="switchProvTab('roster')">Upload roster ↗</button>
    </div>`;
  }

  // Still parsing / cleaning
  if (rs.phase === 'parsing' || rs.phase === 'cleaning') {
    return `<div style="display:flex;align-items:center;gap:.5rem;padding:.5rem .25rem;font-size:.8125rem;color:var(--text-3)">
      <span class="spinner"></span> ${rs.phase === 'cleaning' ? 'AI reviewing roster…' : `Validating providers${rs.total ? ` (${rs.done || 0} / ${rs.total})` : '…'}`}
    </div>`;
  }

  if (rs.phase === 'error') {
    return `<div style="padding:.5rem .25rem;font-size:.8125rem;color:var(--red)">⚠ ${esc(rs.error || 'Error loading roster')}
      <button class="recon-refresh-btn" style="margin-left:.5rem" onclick="switchProvTab('roster')">Try again ↗</button>
    </div>`;
  }

  // Done — compute stats
  const clean    = (rs.report?.clean)    || [];
  const ex       = (rs.report?.excluded) || [];
  const total    = clean.length;
  const noIssues  = clean.filter(p => _aiCategory(p) === 'no-issues').length;
  const confident = clean.filter(p => _aiCategory(p) === 'mobius-confident').length;
  const needsHelp = clean.filter(p => _aiCategory(p) === 'mobius-needs-help').length;
  const deact     = clean.filter(p => _reconCat(p) === 'deactivated').length;
  // keep legacy vars used by tracked computation below
  const aligned   = noIssues;
  const needsAttn = needsHelp + confident;
  // "Tracked" = distinct provider indices that have an open task and are still needs-attention
  const _trackedIdxSet = new Set(
    (_reconTasks || []).filter(t => !t.done && t.type !== 'confirmed' && t.providerIdx != null).map(t => t.providerIdx)
  );
  const tracked = needsAttn > 0
    ? clean.filter((p, i) => _reconCat(p) === 'needs-attention' && _trackedIdxSet.has(i)).length
    : 0;
  const fileName = rs.fileName || 'roster';

  const uploadedAt = rs.uploadedAt ? new Date(rs.uploadedAt).toLocaleDateString('en-US',{month:'short',day:'numeric'}) : null;
  const chip = (label, color, bg, title='') =>
    `<span class="recon-stat-chip" style="color:${color};background:${bg};border-radius:6px;padding:.15rem .5rem;font-size:.78rem;font-weight:600" title="${esc(title)}">${label}</span>`;

  return `<div style="display:flex;align-items:center;gap:.5rem;flex-wrap:wrap;padding:.45rem .25rem .55rem;border-bottom:1px solid var(--border);margin-bottom:.6rem">
    <span style="font-size:.78rem;color:var(--text-3)">${total} providers${uploadedAt ? ` · uploaded ${uploadedAt}` : ''}</span>
    <span style="color:var(--border)">·</span>
    ${chip(`✓ ${noIssues} no issues`,      'var(--green)',  'var(--green-bg)', 'Fully aligned with NPPES')}
    ${confident ? chip(`∞ ${confident} Mobius confident`, 'var(--indigo)', 'var(--indigo-bg,#eef2ff)', 'AI made a confident call — review optional') : ''}
    ${needsHelp ? chip(`⚠ ${needsHelp} need your input`, '#d97706', '#fffbeb', 'Mobius needs human guidance') : ''}
    ${deact     ? chip(`🚨 ${deact} deactivated`,          '#dc2626', '#fef2f2', 'NPI deactivated — revenue risk') : ''}
    ${tracked   ? `<span style="flex:0 0 auto;font-size:.78rem;font-weight:600;color:var(--mobius-logo-grey);background:var(--grey-bg);border-radius:6px;padding:.15rem .5rem;border:1px solid var(--border)" title="Providers with open tracked tasks — revenue risk">📌 ${tracked} tracked</span>` : ''}
    <span style="flex:1"></span>
  </div>`;
}

// Legacy — kept because _refreshNppesSection still references section2 element (now gone but harmless)
function _buildReconSection1Html() { return ''; }
function _buildReconSection2Html() { return ''; }

// ── Row-level AI reason text (shown inline, no expand needed) ──
function _rowReasonText(p) {
  const aiCat = _aiCategory(p);
  if (aiCat === 'no-issues') return '';
  const vr = p.latest_validation;
  const al = vr?.validation_details?.alignment || {};
  const activeDims = _activeDriftDims(p);
  if (!activeDims.length) return '';
  const dim = activeDims[0];
  if (dim === 'name') {
    const na = al.name || {};
    // If backend flagged credential-only diff, use that specific reason
    if (na.cred_only && na.extra_creds?.length) {
      return `NPPES filed with credential suffix (${na.extra_creds.join(', ')}) — same provider`;
    }
    // Use raw NPPES name for analysis (has credentials to describe)
    return _analyzeNameDiff(na.roster, na.nppes_raw || na.nppes) || 'Name differs from NPPES record';
  }
  if (dim === 'address') return al.address?.addr_detail || 'Address differs from org locations';
  if (dim === 'taxonomy') return 'Specialty differs from NPPES record';
  if (dim === 'zip') return 'ZIP code differs from org locations';
  return `${dim} differs from NPPES`;
}

// ── Smart single-click task creation ──────────────────────────
function reconAutoCreateTask(idx) {
  const rs = window._rosterUploadState;
  if (!rs?.report?.clean) return;
  const p = rs.report.clean[idx];
  if (!p) return;
  const aiCat = _aiCategory(p);
  const activeDims = _activeDriftDims(p);
  const reason = _rowReasonText(p);
  const tasks = _getOrInitReconTasks();

  // If already tracked, highlight existing task in drawer
  const existing = tasks.find(t => t.providerIdx === idx && !t.done);
  if (existing) {
    toggleTaskDrawer(true);
    setTimeout(() => {
      const el = document.getElementById(`task-${existing.id}`);
      if (el) {
        el.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
        el.style.outline = '2px solid var(--indigo)';
        setTimeout(() => { el.style.outline = ''; }, 1600);
      }
    }, 300);
    _showToast(`📌 Already tracked — ${p.provider_name}`);
    return;
  }

  const taskType = aiCat === 'mobius-confident' ? 'auto_dismiss' : 'track';
  const severity = aiCat === 'mobius-needs-help' ? 'high' : 'low';
  const taskText = aiCat === 'mobius-confident'
    ? `Dismiss: ${reason || ((activeDims[0] || 'drift') + ' — Mobius confident')}`
    : `Review: ${reason || (activeDims[0] || 'needs attention')}`;

  tasks.push({
    id: `smart-${idx}-${Date.now()}`,
    providerIdx: idx,
    providerName: p.provider_name,
    type: taskType,
    severity,
    phase: 2,
    text: taskText,
    detail: reason,
    done: false,
    autoCreated: true,
    dims: activeDims,
  });

  // Persist audit event to DB
  if (p.id) {
    const runId   = lastRun?.run_id || '';
    const orgName = lastRun?.org_name || '';
    fetch(`/chat/roster-reconcile/provider/${p.id}/audit-log`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        event_type: 'task_created',
        actor: 'user',
        run_id: runId,
        org_name: orgName,
        event_data: { dim: activeDims[0] || 'review', reason, task_type: taskType, dims: activeDims },
      }),
    }).catch(() => {});
  }

  feEmit('Task created — ' + p.provider_name + (taskText ? ' · ' + taskText : ''));
  _showToast(`✓ Task created — ${p.provider_name}`);
  _refreshTaskQueueFull();
  _refreshReconView();
  toggleTaskDrawer(true);
}

// ── Brief bottom toast notification ───────────────────────────
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

function _buildReconSection3Html(filter) {
  const rs = window._rosterUploadState;
  if (!rs) {
    return '';  // upload section header already tells the user what to do
  }
  if (rs.phase === 'parsing' || rs.phase === 'cleaning') {
    const verb = rs.phase === 'cleaning' ? 'AI reviewing roster…' : (rs.total ? `Validating ${rs.done || 0} / ${rs.total} providers…` : 'Validating providers…');
    return `<div style="display:flex;align-items:center;gap:.6rem;padding:.75rem;font-size:.8rem;color:var(--text-3)"><span class="spinner"></span>${verb}</div>`;
  }
  if (rs.phase !== 'done' || !rs.report) {
    return '';  // upload header shows current state
  }

  // Streaming banner — shown while NPPES validation is still in progress
  const streamBanner = rs._streaming ? `
    <div id="streamingBanner" style="position:relative;overflow:hidden;display:flex;align-items:center;gap:.5rem;padding:.45rem .75rem;background:var(--indigo-bg);border:1px solid var(--indigo-border);border-radius:7px;font-size:.8rem;color:var(--indigo);margin-bottom:.6rem">
      <span class="spinner" style="width:12px;height:12px;border-width:1.5px"></span>
      <span style="flex:1">Validating providers against NPPES — table updates live as each result comes in</span>
      <div style="position:absolute;bottom:0;left:0;height:2px;background:var(--indigo);border-radius:1px;width:0%;transition:width .4s ease"></div>
    </div>` : '';

  const clean = rs.report.clean || [];

  const dimLabels = { name:'Name', taxonomy:'Specialty', address:'Address', zip:'ZIP', credential:'Credential' };

  // Drift chip: color reflects severity (red=mismatch, amber=drift)
  const driftChip = (d, flag) => {
    const isMismatch = flag === 'mismatch' || flag === 'deactivated';
    const bg = isMismatch ? '#fef2f2' : '#fef3c7';
    const col = isMismatch ? '#dc2626' : '#92400e';
    const border = isMismatch ? '#fca5a5' : '#fde68a';
    return `<span style="display:inline-block;font-size:.67rem;font-weight:600;padding:.1rem .4rem;border-radius:4px;background:${bg};color:${col};border:1px solid ${border};white-space:nowrap">${dimLabels[d] || d}</span>`;
  };

  const sigBadge = (p) => {
    const vr = p.latest_validation;
    if (p._decision === 'rejected') return `<span class="recon-sig-badge no-match">✗ rejected</span>`;
    if (!vr?.npi_validated && rs._streaming &&
        (p.status === 'unvalidated' || !p.status || p.status === 'pending')) {
      return `<span class="recon-sig-badge" style="color:var(--text-3);border-color:var(--border)"><span class="spinner" style="width:10px;height:10px;border-width:1.5px;display:inline-block;vertical-align:middle;margin-right:3px"></span>validating</span>`;
    }
    const cat  = _reconCat(p);
    const aiCat = _aiCategory(p);
    if (cat === 'no-match')    return `<span class="recon-sig-badge no-match">No match</span>`;
    if (cat === 'deactivated') return `<span class="recon-sig-badge deactivated">🚨 Deactivated</span>`;

    // Drift chips with severity color
    const activeDims = _activeDriftDims(p);
    const al = vr?.validation_details?.alignment || {};
    const flagFor = d => (al[d]||{}).flag || 'drift';

    if (aiCat === 'no-issues') {
      return `<span class="recon-sig-badge confirmed">✓ No Issues</span>`;
    }
    if (aiCat === 'mobius-confident') {
      const chips = activeDims.map(d => driftChip(d, flagFor(d))).join(' ');
      // ∞ indicates Mobius handled it
      return `<span style="display:flex;flex-wrap:wrap;gap:3px;align-items:center">
        <span style="font-size:.72rem;color:var(--mobius-logo-grey);font-weight:700;letter-spacing:-.5px" title="Mobius is confident — click to review">∞</span>
        ${chips}
      </span>`;
    }
    // mobius-needs-help — stronger visual
    const chips = activeDims.map(d => driftChip(d, flagFor(d))).join(' ');
    return chips
      ? `<span style="display:flex;flex-wrap:wrap;gap:3px;align-items:center">${chips}</span>`
      : `<span class="recon-sig-badge low-conf">Review</span>`;
  };

  // Task-based checkbox: is this provider row checked in the task queue?
  const isChecked = (idx) => {
    if (!_reconTasks) return false;
    return _reconTasks.some(t => t.providerIdx === idx && !t.done && t.type !== 'confirmed');
  };

  // WORKSPACE only shows providers that have NOT been promoted to roster yet
  const workspace = clean.filter(p => !p._approvedToTruth);

  let rows = workspace;
  if (filter === 'no-issues')
    rows = workspace.filter(p => _aiCategory(p) === 'no-issues');
  else if (filter === 'mobius-confident')
    rows = workspace.filter(p => _aiCategory(p) === 'mobius-confident');
  else if (filter === 'mobius-needs-help' || filter === 'needs-help')
    rows = workspace.filter(p => _aiCategory(p) === 'mobius-needs-help'
                              || _reconCat(p) === 'deactivated'
                              || _reconCat(p) === 'no-match');
  else if (filter === 'no-match')      rows = workspace.filter(p => _reconCat(p) === 'no-match');
  else if (filter === 'deactivated')   rows = clean.filter(p => _reconCat(p) === 'deactivated');

  if (!rows.length) return `<div style="padding:.75rem;font-size:.8125rem;color:var(--text-3)">No providers in this category.</div>`;

  const tableRows = rows.map((p, localIdx) => {
    const idx        = clean.indexOf(p);
    const vr         = p.latest_validation;
    const npiDisplay = vr?.npi_validated || p.npi_uploaded || '—';
    const cat        = _reconCat(p);
    const aiCatRow   = _aiCategory(p);
    const nameEsc    = esc(titleCase(p.provider_name || '—'));
    const reason     = _rowReasonText(p);
    const alreadyTracked = (_reconTasks || []).some(t => t.providerIdx === idx && !t.done);
    const promoted   = p._approvedToTruth;

    // ── Mobius Note chip ─────────────────────────────────────────
    let mobiusNote = '';
    if (promoted) {
      mobiusNote = `<span style="font-size:.67rem;font-weight:600;color:var(--green);background:var(--green-bg);border:1px solid var(--green-border);border-radius:5px;padding:.1rem .4rem">✓ In roster</span>`;
    } else if (cat === 'deactivated') {
      mobiusNote = `<span style="font-size:.67rem;font-weight:700;color:var(--red);background:var(--red-bg);border:1px solid var(--red-border);border-radius:5px;padding:.1rem .4rem">🚨 Deactivated</span>`;
    } else if (cat === 'no-match') {
      mobiusNote = `<span style="font-size:.67rem;font-weight:600;color:var(--text-3);background:var(--grey-bg);border:1px solid var(--border);border-radius:5px;padding:.1rem .4rem">🔍 No NPI found</span>`;
    } else if (aiCatRow === 'mobius-needs-help') {
      const al = vr?.validation_details?.alignment || {};
      const dims = _activeDriftDims(p);
      const label = dims[0] === 'name' && al.name?.score < 0.70 ? 'Verify identity'
        : dims[0] === 'address' ? 'Address'
        : dims.length > 0 ? dims.map(d => d.charAt(0).toUpperCase()+d.slice(1)).join(', ')
        : 'Review';
      mobiusNote = `<span style="font-size:.67rem;font-weight:600;color:var(--amber,#d97706);background:var(--amber-bg,#fffbeb);border:1px solid var(--amber-border,#fde68a);border-radius:5px;padding:.1rem .4rem">⚠ ${esc(label)}</span>`;
    } else if (aiCatRow === 'mobius-confident') {
      const dims = _activeDriftDims(p);
      mobiusNote = `<span style="font-size:.67rem;font-weight:600;color:var(--mobius-logo-grey);background:var(--grey-bg);border:1px solid var(--border);border-radius:5px;padding:.1rem .4rem">∞ Confident${dims.length ? ` · ${dims[0]}` : ''}</span>`;
    } else {
      mobiusNote = `<span style="font-size:.67rem;color:var(--green)">✓ No issues</span>`;
    }

    // ── Your Action ──────────────────────────────────────────────
    let actionBtn = '';
    if (promoted) {
      actionBtn = ''; // already done
    } else if (cat === 'no-match') {
      actionBtn = `<button class="link-btn" onclick="event.stopPropagation();rosterEnterNpi(${idx})"
        style="font-size:.72rem;color:var(--red);font-weight:600;border:1px solid var(--red-border);padding:.15rem .45rem;border-radius:5px;background:var(--red-bg);white-space:nowrap">🔍 Find NPI</button>`;
    } else if (cat === 'deactivated') {
      actionBtn = `<button class="link-btn" onclick="event.stopPropagation();reconAutoCreateTask(${idx})"
        style="font-size:.72rem;color:var(--red);font-weight:600;border:1px solid var(--red-border);padding:.15rem .45rem;border-radius:5px;background:var(--red-bg);white-space:nowrap">📌 Track</button>`;
    } else if (aiCatRow === 'mobius-needs-help') {
      actionBtn = alreadyTracked
        ? `<span style="font-size:.72rem;color:var(--mobius-logo-grey);font-weight:700" title="Already tracked">📌</span>`
        : `<button class="link-btn" onclick="event.stopPropagation();reconAutoCreateTask(${idx})"
            style="font-size:.72rem;color:var(--text-2);font-weight:500;border:1px solid var(--border);padding:.15rem .45rem;border-radius:5px;background:var(--surface);white-space:nowrap;transition:all .12s">📌 Track</button>`;
    } else if (aiCatRow === 'mobius-confident') {
      actionBtn = alreadyTracked
        ? `<span style="font-size:.72rem;color:var(--mobius-logo-grey);font-weight:700" title="Already tracked">📌</span>`
        : `<button class="link-btn" onclick="event.stopPropagation();reconAutoCreateTask(${idx})"
            style="font-size:.72rem;color:var(--mobius-logo-grey);font-weight:600;border:1px solid var(--border);padding:.15rem .45rem;border-radius:5px;background:var(--grey-bg);white-space:nowrap">∞ Dismiss</button>`;
    }

    const catCls = { 'no-issues':'cat-no-issues', 'mobius-confident':'cat-confident',
                     'mobius-needs-help':'cat-needs-help' }[aiCatRow]
                 || ({ 'deactivated':'cat-deactivated', 'no-match':'cat-no-match' }[cat] || '');
    const promotedStyle = promoted ? 'opacity:.6' : '';

    // Exclude button — always available so users can remove junk/parse-artifact rows
    const excludeBtn = promoted ? '' :
      `<button class="link-btn" onclick="event.stopPropagation();rosterExcludeRow(${idx})"
        title="Remove this row from the workspace (junk / parse error / wrong person)"
        style="font-size:.67rem;color:var(--text-3);padding:.1rem .3rem;border-radius:4px;border:1px solid transparent;line-height:1;transition:all .12s;white-space:nowrap"
        onmouseenter="this.style.borderColor='var(--red-border)';this.style.color='var(--red)';this.style.background='var(--red-bg)'"
        onmouseleave="this.style.borderColor='transparent';this.style.color='var(--text-3)';this.style.background=''">✕</button>`;

    return `<tr class="${catCls} mob-row-enter" id="recon-row-${idx}" style="${promotedStyle}animation-delay:${localIdx*22}ms">
      <td onclick="reconToggleDetail(${idx})" style="cursor:pointer"
          onmouseenter="this.style.background='var(--grey-bg)'" onmouseleave="this.style.background=''">
        <div style="display:flex;align-items:baseline;gap:.3rem">
          <span class="recon-name-cell" style="font-weight:600;font-size:.8125rem" title="${nameEsc}">${nameEsc}</span>
          <span style="font-size:.6rem;color:var(--text-3);flex-shrink:0;line-height:1">▾</span>
        </div>
        ${reason ? `<span class="recon-row-reason" title="${esc(reason)}">${esc(reason)}</span>` : ''}
      </td>
      <td style="font-family:monospace;font-size:.78rem;color:var(--text-2)">${esc(npiDisplay)}</td>
      <td>${mobiusNote}</td>
      <td><div style="display:flex;gap:5px;align-items:center;flex-wrap:nowrap">${actionBtn}${excludeBtn}</div></td>
    </tr>
    <tr id="recon-detail-${idx}" style="display:none" data-open="false">
      <td colspan="4" style="padding:0">
        <div id="recon-panel-${idx}" class="nppes-detail-panel" style="margin:.25rem .5rem .5rem;border-radius:7px"></div>
      </td>
    </tr>`;
  }).join('');

  return `${streamBanner}
    <div class="recon-table-wrap">
      <table class="recon-table">
        <colgroup>
          <col class="col-name"><col class="col-npi">
          <col class="col-stat"><col class="col-act">
        </colgroup>
        <thead><tr>
          <th>Provider</th><th>NPI</th><th>Mobius note</th><th>Your action</th>
        </tr></thead>
        <tbody>${tableRows}</tbody>
      </table>
    </div>`;
}

function reconRowCheckChanged(idx, checked) {
  const p = window._rosterUploadState?.report?.clean?.[idx];
  if (checked && p) feEmit('Provider flagged for review — ' + (p.provider_name || 'provider #' + idx));
  if (!_reconTasks) return;
  const taskForRow = _reconTasks.find(t => t.providerIdx === idx && !t.done && t.type !== 'confirmed');
  if (taskForRow) {
    taskForRow.done = !checked; // uncheck = keep task open; check = work on it (highlight, scroll)
  }
  if (checked) {
    // scroll to row in task queue
    const taskEl = taskForRow ? document.getElementById(`task-${taskForRow.id}`) : null;
    if (taskEl) {
      taskEl.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
      taskEl.style.outline = '2px solid var(--indigo)';
      setTimeout(() => { taskEl.style.outline = ''; }, 2000);
    }
  }
  _refreshTaskQueueFull();
}

// Confirm a provider directly from the reconciliation table (accepts NPPES match)
function reconConfirmProvider(idx) {
  const p = window._rosterUploadState?.report?.clean?.[idx];
  feEmit('✓ Provider approved — ' + (p?.provider_name || 'provider #' + idx), 'ok');
  rosterUseNpi(idx);   // marks _decision = 'validated', saves to DB
  _refreshReconView(); // rebuilds table + pills + stat bar
}

// ── Export ─────────────────────────────────────────────────────

function exportReconTasks(fmt) {
  const tasks = _reconTasks || [];
  const openTasks = tasks.filter(t => !t.done && t.type !== 'confirmed');

  if (fmt === 'csv') {
    const header = 'Provider,Phase,Dimension,Issue,Detail,Severity,Source,Status';
    const phaseNames = { 1: 'Match', 2: 'Alignment' };
    const rows = openTasks.map(t => {
      const phaseStr  = phaseNames[t.phase] || '';
      const dimStr    = t.dimension ? (t.dimension.charAt(0).toUpperCase() + t.dimension.slice(1)) : '';
      const sourceStr = t.source === 'user' ? 'Manual' : 'Auto';
      return [t.providerName, phaseStr, dimStr, t.text, t.detail || '', t.severity, sourceStr, t.done ? 'done' : 'open']
        .map(v => `"${(v||'').replace(/"/g,'""')}"`)
        .join(',');
    });
    const blob = new Blob([header + '\n' + rows.join('\n')], { type: 'text/csv' });
    const a = document.createElement('a'); a.href = URL.createObjectURL(blob);
    a.download = 'npi-recon-tasks.csv'; a.click();
  } else {
    const lines = openTasks.map(t => {
      const ph = t.source === 'user' ? '(manual)' : t.phase === 1 ? '(match)' : t.phase === 2 ? '(alignment)' : '';
      return `[ ] ${t.providerName} — ${t.text} ${ph}`;
    });
    navigator.clipboard?.writeText(lines.join('\n'))
      .then(() => alert(`${lines.length} tasks copied to clipboard`))
      .catch(() => prompt('Copy this text:', lines.join('\n')));
  }
}

// ── AI-first three-tier classification ─────────────────────────
// 'no-issues'         — clean or AI-auto-dismissed minor drifts
// 'mobius-confident'  — AI detected something but is confident about its call
// 'mobius-needs-help' — AI needs human input (ambiguous, critical, or unknown)
function _aiCategory(p) {
  // Prefer backend-computed field; JS fallback for legacy/un-enriched rows.
  if (p.ai_category) return p.ai_category;
  const vr = p.latest_validation;
  if (!vr || !vr.npi_validated) return 'mobius-needs-help';
  if (_isDeactivated(p)) return 'mobius-needs-help';
  const al = vr.validation_details?.alignment || {};
  const aName = al.name || {}, aAddr = al.address || {};
  const aTax  = al.taxonomy || {}, aCred = al.credential || {};
  if (aName.flag === 'mismatch' || aTax.flag === 'mismatch') return 'mobius-needs-help';
  if (aAddr.flag === 'mismatch' || aAddr.flag === 'drift')   return 'mobius-needs-help';
  if (aName.flag === 'drift') return (aName.score || 0) >= 0.75 ? 'mobius-confident' : 'mobius-needs-help';
  if (aCred.flag === 'drift') return 'mobius-confident';
  if (_activeDriftDims(p).length > 0) return 'mobius-needs-help';
  return 'no-issues';
}

// ── Name diff synthesiser ───────────────────────────────────────
// Returns a specific plain-English reason for a name drift.
function _analyzeNameDiff(rosterName, nppesName) {
  const r = (rosterName  || '').trim();
  const n = (nppesName   || '').trim().replace(/^--\s*/, '');
  if (!r || !n) return null;

  const norm = s => s.toLowerCase().replace(/[.,]/g, '').replace(/\s+/g, ' ').trim();
  const rN = norm(r), nN = norm(n);

  const credTokens = new Set(['md','do','np','pa','rn','lcsw','lpc','lmhc','lmft','phd',
    'psyd','aprn','fnp','pmhnp','bc','cnm','cnp','dnp','dpm','dds','dmd','ot','pt',
    'slp','bcba','aud','agacnp','pmhnpbc','agacnpbc','crnp','cns','crna','licsw','lisw',
    'lpcc','lmft','mft','lpc','lmhc','nbcch','cctp','caadc','cadac']);
  const titleTokens = new Set(['dr','mr','mrs','ms','miss','prof']);

  const rToks = rN.split(' ');
  const nToks = nN.split(' ');

  // Credentials in NPPES not in roster
  const nCreds = nToks.filter(t => credTokens.has(t.replace(/[^a-z]/g,'')));
  const rCreds = rToks.filter(t => credTokens.has(t.replace(/[^a-z]/g,'')));
  const extraNppesC = nCreds.filter(c => !rCreds.includes(c));
  if (extraNppesC.length) {
    return `NPPES includes credential suffix (${extraNppesC.map(c=>c.toUpperCase()).join(', ')})`;
  }
  // Credentials in roster not in NPPES
  const extraRosterC = rCreds.filter(c => !nCreds.includes(c));
  if (extraRosterC.length) {
    return `Roster includes credential suffix (${extraRosterC.map(c=>c.toUpperCase()).join(', ')})`;
  }
  // Title prefix differences
  const nTitles = nToks.filter(t => titleTokens.has(t));
  const rTitles = rToks.filter(t => titleTokens.has(t));
  if (nTitles.length > rTitles.length) {
    const t = nTitles[0]; return `NPPES includes title (${t.charAt(0).toUpperCase()+t.slice(1)}.)`;
  }
  // Extra tokens = middle name / initial
  const rCore = rToks.filter(t => !credTokens.has(t) && !titleTokens.has(t));
  const nCore = nToks.filter(t => !credTokens.has(t) && !titleTokens.has(t));
  const extra = nCore.filter(t => !rCore.includes(t));
  if (extra.length) {
    return extra.some(t => t.length === 1) ? 'NPPES includes middle initial' : 'NPPES includes middle name';
  }
  // Hyphen variation
  if (rN.replace(/-/g,' ') === nN.replace(/-/g,' ')) return 'Hyphenated name variant';
  return null;
}

// ── Internal reconciliation category (used for table filtering) ─
function _reconCat(p) {
  if (p.recon_cat) return p.recon_cat;  // backend-computed
  const vr = p.latest_validation;
  if (!vr || !vr.npi_validated) return 'no-match';
  if (_isDeactivated(p)) return 'deactivated';
  return _activeDriftDims(p).length > 0 ? 'needs-attention' : 'aligned';
}

// All flagged drift dimensions for a provider
function _driftDims(p) {
  const al = p.latest_validation?.validation_details?.alignment || {};
  return ['name','taxonomy','address','zip','credential'].filter(k => {
    const f = al[k]?.flag;
    return f && f !== 'ok' && f !== 'no_roster_data' && f !== 'no_nppes_data';
  });
}

// Drift dimensions that haven't been dismissed yet
function _activeDriftDims(p) {
  // active_drifts is pre-computed by backend; re-filter if user dismissed locally this session
  const dismissed = p._dismissedDims || [];
  if (p.active_drifts && dismissed.length === 0) return p.active_drifts;
  return _driftDims(p).filter(d => !dismissed.includes(d));
}

// Dismiss a single discrepancy dimension — "expected / not a real problem"
function reconDismissDim(providerIdx, dim) {
  const p = window._rosterUploadState?.report?.clean?.[providerIdx];
  if (!p) return;
  if (!p._dismissedDims) p._dismissedDims = [];
  if (!p._dismissedDims.includes(dim)) p._dismissedDims.push(dim);
  feEmit('Flag dismissed — ' + dim + ' for ' + (p.provider_name || 'provider #' + providerIdx));
  // Persist dismiss event to audit log
  if (p.id) {
    fetch(`/chat/roster-reconcile/provider/${p.id}/audit-log`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        event_type: 'dismissed',
        actor: 'user',
        run_id: lastRun?.run_id || '',
        org_name: lastRun?.org_name || '',
        event_data: { dim, reason: `User dismissed ${dim} drift` },
      }),
    }).catch(() => {});
  }
  const panelEl = document.getElementById(`recon-panel-${providerIdx}`);
  if (panelEl) _renderNppesDetail(providerIdx, panelEl);
  _refreshReconView();
}

// Undo a dimension dismiss
function reconUndoDismissDim(providerIdx, dim) {
  const p = window._rosterUploadState?.report?.clean?.[providerIdx];
  if (!p || !p._dismissedDims) return;
  p._dismissedDims = p._dismissedDims.filter(d => d !== dim);
  feEmit('Flag restored — ' + dim + ' for ' + (p.provider_name || 'provider #' + providerIdx));
  const panelEl = document.getElementById(`recon-panel-${providerIdx}`);
  if (panelEl) _renderNppesDetail(providerIdx, panelEl);
  _refreshReconView();
}

// ── Full NPI Reconciliation tab HTML builder ───────────────────

// ── Session work banner ────────────────────────────────────────
// Builds from cached _sessionSummary or shows a loading state.
// Async-loads from audit log after render via _loadSessionBanner().
function _buildSessionBannerHtml(summary) {
  if (!summary) {
    // Placeholder — replaced once audit log loads
    return `<div id="sessionBannerInner" style="display:none"></div>`;
  }

  const byType   = summary.by_type || {};
  const approved = (byType.approved || 0) + (byType.mass_approved || 0);
  const tasks    = byType.task_created || 0;
  const dismissed = byType.dismissed || 0;
  const mobiusActor = summary.mobius_actor;
  const userActor   = summary.user_actor;

  // Nothing happened yet
  if (!approved && !tasks && !dismissed) return `<div id="sessionBannerInner" style="display:none"></div>`;

  const actorLabel = (mobiusActor && userActor) ? '∞ Mobius + 👤 You'
    : mobiusActor ? '∞ Mobius'
    : '👤 You';
  const actorColor = mobiusActor ? 'var(--indigo)' : 'var(--text-2)';

  const chips = [
    approved  && `<span style="font-size:.68rem;font-weight:600;color:var(--green);background:var(--green-bg);border:1px solid var(--green-border);border-radius:5px;padding:.1rem .4rem">✓ ${approved} promoted</span>`,
    tasks     && `<span style="font-size:.68rem;font-weight:700;color:var(--indigo);background:var(--indigo-bg);border:1px solid var(--indigo-border);border-radius:5px;padding:.1rem .4rem">📌 ${tasks} tasks created</span>`,
    dismissed && `<span style="font-size:.68rem;font-weight:600;color:var(--text-3);background:var(--grey-bg);border:1px solid var(--border);border-radius:5px;padding:.1rem .4rem">~ ${dismissed} dismissed</span>`,
    summary.deactivated_count && `<span style="font-size:.68rem;font-weight:700;color:var(--red);background:var(--red-bg);border:1px solid var(--red-border);border-radius:5px;padding:.1rem .4rem">🚨 ${summary.deactivated_count} deactivated — revenue risk</span>`,
    summary.pending_count     && `<span style="font-size:.68rem;font-weight:600;color:var(--amber,#d97706);background:var(--amber-bg,#fffbeb);border:1px solid var(--amber-border,#fde68a);border-radius:5px;padding:.1rem .4rem">⚠ ${summary.pending_count} need your review</span>`,
    summary.no_npi_count      && `<span style="font-size:.68rem;font-weight:600;color:var(--text-3);background:var(--grey-bg);border:1px solid var(--border);border-radius:5px;padding:.1rem .4rem">🔍 ${summary.no_npi_count} no NPI found</span>`,
  ].filter(Boolean).join('');

  const ts = summary.last_event_at
    ? new Date(summary.last_event_at).toLocaleString('en-US',{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'})
    : 'this session';

  return `
    <div id="sessionBannerInner" style="display:flex;align-items:flex-start;gap:.5rem;
      padding:.5rem .75rem;background:var(--indigo-bg);border:1px solid var(--indigo-border);
      border-radius:8px">
      <span style="font-size:.8rem;font-weight:800;color:${actorColor};flex-shrink:0;margin-top:.05rem">∞</span>
      <div style="flex:1;min-width:0">
        <div style="display:flex;align-items:center;gap:.5rem;flex-wrap:wrap;margin-bottom:.25rem">
          <span style="font-size:.72rem;font-weight:700;color:${actorColor}">${actorLabel}</span>
          <span style="font-size:.67rem;color:var(--text-3)">${ts}</span>
        </div>
        <div style="display:flex;gap:.3rem;flex-wrap:wrap">${chips}</div>
      </div>
      <button onclick="document.getElementById('sessionBannerInner').closest('#sessionBanner').style.display='none'"
        style="font-size:.72rem;color:var(--text-3);background:none;border:none;cursor:pointer;flex-shrink:0;padding:0;line-height:1">✕</button>
    </div>`;
}

async function _loadSessionBanner() {
  const runId = lastRun?.run_id;
  if (!runId) return;
  try {
    const orgName = encodeURIComponent(lastRun?.org_name || '');
    const resp = await fetch(`/chat/roster-reconcile/run/${encodeURIComponent(runId)}/audit-log?org_name=${orgName}&limit=200`);
    if (!resp.ok) return;
    const data = await resp.json();
    const byType = data.summary?.by_type || {};
    const events = data.events || [];

    // Determine actors
    const mobiusActor = events.some(e => e.actor === 'mobius');
    const userActor   = events.some(e => e.actor === 'user');

    // Compute derived counts from current frontend state
    const clean = window._rosterUploadState?.report?.clean || [];
    const deactivatedCount = clean.filter(p => _reconCat(p) === 'deactivated').length;
    const pendingCount     = clean.filter(p => _aiCategory(p) === 'mobius-needs-help').length;
    const noNpiCount       = clean.filter(p => _reconCat(p) === 'no-match').length;

    window._sessionSummary = {
      by_type:          byType,
      mobius_actor:     mobiusActor,
      user_actor:       userActor,
      deactivated_count: deactivatedCount,
      pending_count:    pendingCount,
      no_npi_count:     noNpiCount,
      last_event_at:    events[0]?.created_at || null,
    };

    const bannerEl = document.getElementById('sessionBanner');
    if (bannerEl) bannerEl.innerHTML = _buildSessionBannerHtml(window._sessionSummary);
  } catch (e) { /* silent — banner is optional */ }
}

// ── Roster section ─────────────────────────────────────────────
// Builds the static skeleton; _loadRosterTruth() fills in the live table
function _buildRosterSectionHtml() {
  const rs = window._rosterUploadState;
  if (!rs || rs.phase !== 'done' || !rs.report) return '';
  const s      = _computeReconScore();
  const clean  = rs.report.clean || [];

  const currentScore   = s ? s.score : null;
  const pendingCount   = clean.filter(p => _aiCategory(p) === 'mobius-needs-help').length
                       + clean.filter(p => _reconCat(p) === 'no-match').length;
  const projectedScore = s ? Math.min(100, s.score + Math.round(pendingCount * 1.2)) : null;
  const band      = s?.band || 'green';
  const bandLabel = s?.bandLabel || 'Roster in good shape';
  const tip       = s ? `Score = NPI coverage (${s.matchPct}% × 80%) + alignment health (${s.alignPct}% × 20%) − ${s.deactivatedPenalty}pts deactivated` : '';
  const scoreCol  = band === 'green' ? 'var(--green)' : band === 'amber' ? '#d97706' : '#dc2626';

  const storyHtml = currentScore !== null ? `
    <div style="display:flex;align-items:center;gap:.4rem;flex-wrap:wrap;font-size:.75rem;margin-top:.1rem;margin-bottom:.35rem;color:var(--text-3)">
      ${window._prevRosterScore != null
        ? `<span>Starting <strong style="color:var(--text-2)">${window._prevRosterScore}</strong></span><span>→</span>`
        : ''}
      <span>In workspace <strong style="color:${scoreCol}">${currentScore}${currentScore > (window._prevRosterScore||0) ? ' ↑' : ''}</strong></span>
      ${projectedScore && pendingCount > 0
        ? `<span>→</span><span>Expected after sync <strong style="color:var(--indigo)">${projectedScore}</strong></span>`
        : ''}
    </div>` : '';

  // (tableHtml removed — roster now renders as expandable card list via _renderRosterTruthRows)

  // Count tracking tasks from roster truth
  const trackingCount = (window._rosterTruth || [])
    .reduce((n, p) => n + (Array.isArray(p.open_tasks) ? p.open_tasks.length : 0), 0);

  const approvedCount = (window._rosterTruth || []).length;
  const approvedLabel = approvedCount > 0 ? `· ${approvedCount} approved` : '· no providers yet';

  // Roster section is open by default if there are promoted providers,
  // closed if empty. User override is remembered in _rosterSectionOpen.
  if (window._rosterSectionOpen === undefined) {
    window._rosterSectionOpen = approvedCount > 0;
  }
  const isOpen = window._rosterSectionOpen;

  // Score badge — shows the number with its label; "ⓘ" opens the tooltip explaining what it means
  const scoreHtml = currentScore !== null
    ? `<span style="font-size:.8125rem;font-weight:700;color:${scoreCol};cursor:help" title="${esc(tip)}">${currentScore}</span>
       <span style="font-size:.72rem;font-weight:400;color:var(--text-3);cursor:help" title="${esc(tip)}"> billable score · </span>
       <span style="font-size:.72rem;font-weight:500;color:${scoreCol};cursor:help" title="${esc(tip)}">${bandLabel}</span>`
    : '';

  return `
    <details id="rosterSectionDetails" class="sec-card" ${isOpen ? 'open' : ''}
      style="margin-top:.5rem;animation-delay:80ms"
      ontoggle="window._rosterSectionOpen=this.open">
      <summary class="sec-summary">
        <span class="sec-label">Roster</span>
        <span class="sec-meta" id="rosterCountBadge">${approvedLabel}</span>
        <span class="sec-spacer"></span>
        ${trackingCount > 0 ? `<button onclick="event.stopPropagation();toggleTaskDrawer(true,'tracking')" class="sec-action" data-monitoring-btn>
          ◎ Monitoring <span data-monitoring-count style="background:var(--indigo);color:#fff;border-radius:10px;padding:.02rem .32rem;font-size:.6rem">${trackingCount}</span>
        </button>` : `<span data-monitoring-btn style="display:none"></span>`}
        ${scoreHtml}
        <span class="sec-chevron">▾</span>
      </summary>
      <div class="sec-body">
        ${storyHtml}
        <div id="rosterLiveList" class="rt-card-list" style="margin-top:.3rem">
          <div style="padding:.65rem .5rem;font-size:.8rem;color:var(--text-3)">
            <span class="spinner" style="width:11px;height:11px;border-width:1.5px;display:inline-block;vertical-align:middle;margin-right:5px"></span>Loading roster…
          </div>
        </div>
      </div>
    </details>`;
}

// ── Roster truth loader (reads from DB) ───────────────────────
// Called on: page load with roster data, after individual approve, after mass approve
async function _loadRosterTruth() {
  const orgName = lastRun?.org_name;
  if (!orgName) return;

  // If data already cached in memory, just re-render (avoids extra network round-trip
  // after _setRosterState rebuilds reconContent and wipes the roster list DOM).
  if (window._rosterTruth?.length > 0) {
    try {
      _renderRosterTruthRows(window._rosterTruth);
    } catch(e) {
      console.error('[_loadRosterTruth] render error (cached):', e);
      const list = document.getElementById('rosterLiveList');
      if (list) list.innerHTML = `<div style="padding:.65rem .75rem;font-size:.72rem;color:var(--red)">Render error — ${esc(String(e))}</div>`;
    }
    return;
  }

  try {
    const resp = await fetch(`/chat/roster-truth/${encodeURIComponent(orgName)}?limit=500`);
    if (!resp.ok) throw new Error(`roster-truth ${resp.status}`);
    const data = await resp.json();
    window._rosterTruth = data.providers || [];
    try {
      _renderRosterTruthRows(window._rosterTruth);
    } catch(renderErr) {
      console.error('[_loadRosterTruth] render error:', renderErr);
      throw renderErr;
    }

    // ── Sync _approvedToTruth flags from DB ───────────────────────
    // Match by source_provider_id (integer FK), npi_validated (NPI anchor), or name (fallback)
    const clean = window._rosterUploadState?.report?.clean || [];
    const promotedSourceIds = new Set(
      (data.providers || []).map(p => p.source_provider_id).filter(Boolean)
    );
    const promotedNpis = new Set(
      (data.providers || []).map(p => p.npi_validated).filter(Boolean)
    );
    const _normN = n => (n || '').toLowerCase().replace(/\s+/g, ' ').trim();
    const promotedNames = new Set(
      (data.providers || []).map(p => _normN(p.provider_name)).filter(Boolean)
    );
    let newlyMarked = 0;
    clean.forEach(p => {
      const alreadyMarked = p._approvedToTruth;
      const idMatch   = promotedSourceIds.has(String(p.id)) || promotedSourceIds.has(p.id);
      const npiMatch  = (p.latest_validation?.npi_validated && promotedNpis.has(p.latest_validation.npi_validated))
                     || (p.npi_uploaded && promotedNpis.has(p.npi_uploaded));
      const nameMatch = promotedNames.has(_normN(p.provider_name));
      if (idMatch || npiMatch || nameMatch) {
        p._approvedToTruth = true;
        p._decision = 'validated';
        if (!alreadyMarked) newlyMarked++;
      }
    });

    // If we newly discovered promoted providers, rebuild workspace (they should disappear)
    if (newlyMarked > 0) {
      _refreshReconView();
      _refreshWorkspaceHeader();
    }
  } catch (e) {
    console.error('[_loadRosterTruth] error:', e);
    const list = document.getElementById('rosterLiveList');
    if (list) list.innerHTML = `<div style="padding:.65rem .75rem;font-size:.72rem;color:var(--red)">Could not load roster data — ${esc(String(e))}</div>`;
  }
}

function _renderRosterTruthRows(providers) {
  const list = document.getElementById('rosterLiveList');
  if (!list) return;

  // Update count badge
  const badge = document.getElementById('rosterCountBadge');
  if (badge) badge.textContent = providers.length > 0 ? `· ${providers.length} approved` : '· no providers promoted yet';

  // Refresh monitoring button
  const totalTracking = providers.reduce((n, p) => n + (Array.isArray(p.open_tasks) ? p.open_tasks.length : 0), 0);
  const monBtn = document.querySelector('[data-monitoring-btn]');
  if (monBtn) {
    monBtn.style.display = totalTracking > 0 ? '' : 'none';
    const cnt = monBtn.querySelector('[data-monitoring-count]');
    if (cnt) cnt.textContent = totalTracking;
  }

  if (!providers.length) {
    list.innerHTML = `<div style="padding:.75rem;font-size:.78rem;color:var(--text-3);font-style:italic">No providers promoted yet — approve from workspace above</div>`;
    return;
  }

  const dimLabel = { name: 'Name', taxonomy: 'Specialty', address: 'Address', zip: 'ZIP', phone: 'Phone', status: 'Status', npi: 'NPI', credential: 'Credentials' };

  list.innerHTML = providers.map((p, i) => {
    const openTasks  = Array.isArray(p.open_tasks) ? p.open_tasks : [];
    const snap       = p.nppes_snapshot || {};
    const al         = snap; // snapshot IS the alignment + enriched fields
    const loc        = [p.city, p.state_cd].filter(Boolean).join(', ') || '—';
    const promotedTs = p.promoted_at
      ? new Date(p.promoted_at).toLocaleDateString('en-US', {month:'short', day:'numeric', year:'numeric'})
      : '';

    // ── Header status indicators ──────────────────────────────
    const taskStatus = openTasks.length > 0
      ? `<span class="rt-task-chip">◎ ${openTasks.length} tracked</span>`
      : `<span style="font-size:.67rem;color:var(--green);flex-shrink:0">✓ clean</span>`;

    const nppesSt  = (snap.nppes_status || al.status?.nppes || '').toUpperCase();
    const stBadge  = nppesSt === 'D'
      ? `<span style="font-size:.65rem;font-weight:700;color:var(--red);background:var(--red-bg,#fef2f2);border:1px solid var(--red-border,#fca5a5);border-radius:4px;padding:.05rem .3rem;flex-shrink:0">Deactivated</span>`
      : nppesSt === 'A'
        ? `<span style="font-size:.65rem;font-weight:700;color:var(--green);background:var(--green-bg,#f0fdf4);border:1px solid var(--green-border,#86efac);border-radius:4px;padding:.05rem .3rem;flex-shrink:0">Active</span>`
        : '';

    const confPct = Math.round((snap.match_confidence || p.match_confidence || 0) * 100);
    const confBadge = confPct > 0
      ? `<span style="font-size:.65rem;color:var(--text-3);flex-shrink:0">${confPct}% match</span>`
      : '';

    const entityType = snap.entity_type || '';
    const entityBadge = entityType
      ? `<span style="font-size:.65rem;color:var(--text-3);flex-shrink:0">${esc(entityType)}</span>`
      : '';

    // ── Credentials ───────────────────────────────────────────
    const _credRaw = snap.credentials_list
      || (Array.isArray(al.credential?.nppes) ? al.credential.nppes
          : (al.credential?.nppes ? String(al.credential.nppes).split(',').map(c => c.trim()) : []));
    const credList = Array.isArray(_credRaw) ? _credRaw : [];
    const credHtml = credList.length
      ? `<div class="rt-profile-section">
          <div class="rt-profile-label">Credentials</div>
          <div style="display:flex;flex-wrap:wrap;gap:.25rem">
            ${credList.map(c => `<span style="font-size:.72rem;font-weight:600;color:var(--indigo);background:var(--indigo-bg,#eef2ff);border:1px solid var(--indigo-border,#c7d2fe);border-radius:4px;padding:.1rem .35rem">${esc(c)}</span>`).join('')}
          </div>
        </div>`
      : '';

    // ── Taxonomies (all) ──────────────────────────────────────
    const allTax = snap.all_taxonomies || [];
    // Fallback: build a single entry from the primary specialty/code stored at promote time
    const primaryCode = al.taxonomy?.nppes_code || '';
    const primaryDesc = p.specialty || al.taxonomy?.nppes || snap.taxonomy?.nppes || '';
    const taxEntries = allTax.length
      ? allTax
      : (primaryCode || primaryDesc)
        ? [{ code: primaryCode, desc: primaryDesc, primary: true, state: '', license: '' }]
        : [];

    const taxHtml = taxEntries.length
      ? `<div class="rt-profile-section">
          <div class="rt-profile-label">Taxonomy / Specialties</div>
          <div style="display:flex;flex-direction:column;gap:.3rem">
            ${taxEntries.map(t => {
              const isPrimary = t.primary;
              const licenseNote = (t.license || t.state)
                ? ` <span style="font-size:.67rem;color:var(--text-3)">Lic: ${esc((t.license||'')+(t.state?' ('+t.state+')':''))}</span>`
                : '';
              return `<div style="display:flex;align-items:flex-start;gap:.4rem;flex-wrap:wrap">
                ${t.code ? `<span style="font-family:monospace;font-size:.72rem;font-weight:600;color:var(--text-2);flex-shrink:0">${esc(t.code)}</span>` : ''}
                <span style="font-size:.75rem;color:var(--text);flex:1">${esc(t.desc || '—')}</span>
                ${isPrimary ? `<span style="font-size:.62rem;font-weight:700;color:var(--green);flex-shrink:0">PRIMARY</span>` : ''}
                ${licenseNote}
              </div>`;
            }).join('')}
          </div>
        </div>`
      : '';

    // ── Contact & address ─────────────────────────────────────
    const phone   = snap.phone || p.phone || '';
    const addrLine = p.address_line1 || (al.address?.nppes_raw || al.address?.nppes || '').split(',')[0]?.trim() || '';
    const city    = p.city    || '';
    const state   = p.state_cd || al.address?.nppes_state || '';
    const zip     = p.zip_code || snap.zip5 || '';
    const fullAddr = [addrLine, city, [state, zip].filter(Boolean).join(' ')].filter(Boolean).join(', ');
    const contactHtml = (phone || fullAddr)
      ? `<div class="rt-profile-section">
          <div class="rt-profile-label">Contact &amp; Location</div>
          <div style="display:flex;flex-direction:column;gap:.2rem">
            ${fullAddr ? `<div style="font-size:.78rem;color:var(--text-2)">📍 ${esc(fullAddr)}</div>` : ''}
            ${phone    ? `<div style="font-size:.78rem;color:var(--text-2)">📞 ${esc(phone)}</div>`    : ''}
          </div>
        </div>`
      : '';

    // ── Identity row ──────────────────────────────────────────
    const gender = snap.gender || '';
    const genderLabel = gender === 'M' ? 'Male' : gender === 'F' ? 'Female' : '';
    const identityHtml = `<div class="rt-profile-section" style="display:flex;flex-wrap:wrap;align-items:center;gap:.4rem;padding-bottom:.4rem;border-bottom:1px solid var(--border)">
      <span style="font-family:monospace;font-size:.8rem;font-weight:600;color:var(--text-2)">${esc(p.npi_validated || '—')}</span>
      ${stBadge}
      ${entityBadge}
      ${genderLabel ? `<span style="font-size:.65rem;color:var(--text-3)">${genderLabel}</span>` : ''}
      ${confBadge}
      ${promotedTs ? `<span style="font-size:.65rem;color:var(--text-3);margin-left:auto">Promoted ${esc(promotedTs)}</span>` : ''}
    </div>`;

    // ── Task list ─────────────────────────────────────────────
    const taskList = openTasks.length > 0
      ? `<div class="rt-profile-section" style="padding-top:.4rem;border-top:1px solid var(--border)">
          <div class="rt-profile-label">Open items</div>
          <div style="display:flex;flex-wrap:wrap;gap:.3rem">
            ${openTasks.map(t => `<span class="rt-task-chip">◎ ${esc(dimLabel[t.dim||t.type]||t.type||'issue')}${t.note ? ` — ${esc(t.note.substring(0,55))}` : ''}</span>`).join('')}
          </div>
        </div>`
      : `<div style="margin-top:.4rem;padding-top:.4rem;border-top:1px solid var(--border);font-size:.72rem;color:var(--green)">✓ No open items — all clear</div>`;

    return `<div class="rt-card roster-live-row" id="roster-row-prov-${p.id}" style="animation-delay:${i*18}ms">
      <div class="rt-card-head" onclick="_toggleRosterCard(this)">
        <span class="rt-card-name">${esc(titleCase(p.provider_name || '—'))}</span>
        <span class="rt-card-npi">${esc(p.npi_validated || '')}</span>
        <span class="rt-card-loc">${esc(loc)}</span>
        ${taskStatus}
        <span class="rt-card-chevron">▾</span>
      </div>
      <div class="rt-card-body" style="padding:.6rem .8rem .5rem">
        ${identityHtml}
        ${credHtml}
        ${taxHtml}
        ${contactHtml}
        ${taskList}
      </div>
    </div>`;
  }).join('');
}

function _toggleRosterCard(headEl) {
  const card = headEl.closest('.rt-card');
  if (card) card.classList.toggle('rt-open');
}

// ── Recon table search filter ──────────────────────────────────
function filterReconTable() {
  const q = (document.getElementById('reconSearchInput')?.value || '').toLowerCase().trim();
  if (q) feEmit('Roster search — ' + q);
  document.querySelectorAll('.recon-table tbody tr[id^="recon-row-"]').forEach(row => {
    const detail = document.getElementById(row.id.replace('recon-row-', 'recon-detail-'));
    const text = row.textContent.toLowerCase();
    const show = !q || text.includes(q);
    row.style.display = show ? '' : 'none';
    if (detail) detail.style.display = (!show || detail.dataset.open !== 'true') ? 'none' : '';
  });
}

// ── Mass-approve action bar ────────────────────────────────────
function _buildMassApproveBarHtml() {
  const rs = window._rosterUploadState;
  if (!rs || rs.phase !== 'done' || !rs.report) return '';
  const clean = rs.report.clean || [];
  if (!clean.length) return '';

  const runMode = lastRun?.mode || 'copilot'; // 'copilot' | 'autopilot'
  const isAgentic = runMode === 'autopilot';

  // Only count workspace providers (not already promoted to roster)
  const ws            = clean.filter(p => !p._approvedToTruth);
  const safeToSync    = ws.filter(p => p.latest_validation?.npi_validated
                                    || (p._decision === 'validated' && p.npi_uploaded));
  const needInput     = ws.filter(p => !(p.latest_validation?.npi_validated
                                      || (p._decision === 'validated' && p.npi_uploaded)));
  const alreadyInRoster = clean.filter(p => p._approvedToTruth).length;

  // If workspace is empty, show a clean cleared state
  if (ws.length === 0 && alreadyInRoster > 0) {
    return `
      <div id="massApproveBar" style="display:flex;align-items:center;gap:.5rem;padding:.45rem .65rem;
        background:var(--grey-bg);border:1px solid var(--border);border-radius:8px;margin:.5rem 0 .35rem;font-size:.78rem">
        <span style="color:var(--green);font-weight:600">✓</span>
        <span style="color:var(--text-2);font-weight:500">All ${alreadyInRoster} providers in roster</span>
        <span style="color:var(--text-3)">— workspace is clear</span>
        <button onclick="loadRunAuditLog()" style="margin-left:auto;font-size:.67rem;color:var(--text-3);background:none;border:none;cursor:pointer;text-decoration:underline">📋 Audit log</button>
      </div>`;
  }

  // Build the contextual summary line based on mode
  const syncCount = safeToSync.length;
  const inputCount = needInput.length;

  let summaryLine = '';
  let btnLabel = 'Sync to roster →';
  let btnMode  = isAgentic ? 'agentic' : 'high_confident';

  if (isAgentic) {
    // Autopilot: Mobius already evaluated everything — we surface what's ready and what needs review
    summaryLine = syncCount > 0
      ? `<span style="color:var(--green)">✓ <strong>${syncCount}</strong> provider${syncCount!==1?'s':''} ready to sync</span>`
      : '';
    if (inputCount > 0) {
      summaryLine += (summaryLine ? ' <span style="color:var(--text-3)">·</span> ' : '')
        + `<span style="color:#92400e">⚑ <strong>${inputCount}</strong> need${inputCount===1?'s':''} your input</span>`;
    }
    btnLabel = `Sync ${syncCount} to roster →`;
  } else {
    // Copilot: user reviews each card; bulk sync still allowed for validated (NPI-matched) ones
    summaryLine = syncCount > 0
      ? `<span style="color:var(--green)">✓ <strong>${syncCount}</strong> validated provider${syncCount!==1?'s':''} ready to sync</span>`
      : `<span style="color:var(--text-3)">No validated providers to sync yet</span>`;
    if (inputCount > 0) {
      summaryLine += ` <span style="color:var(--text-3)">·</span> <span style="color:#92400e">${inputCount} need${inputCount===1?'s':''} review — use ✓ Approve on each card</span>`;
    }
    btnLabel = `Sync ${syncCount} validated →`;
  }

  const prevNote = alreadyInRoster > 0
    ? `<div style="font-size:.68rem;color:var(--green);padding:.15rem 0 .3rem">
         ✓ <strong>${alreadyInRoster}</strong> already in roster — only the remaining ${ws.length} workspace providers will be synced
       </div>`
    : '';

  const canSync = syncCount > 0;

  return `
    <div id="massApproveBar" style="background:var(--surface);border:1px solid var(--border);
      border-radius:8px;margin:.5rem 0 .35rem;padding:.55rem .75rem">
      <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:.2rem">
        <span style="font-size:.72rem;font-weight:700;color:var(--text-2)">Sync to roster</span>
        <button onclick="loadRunAuditLog()" title="View full audit trail for this run"
          style="font-size:.68rem;color:var(--text-3);background:none;border:none;cursor:pointer;padding:0;text-decoration:underline">
          📋 Audit log
        </button>
      </div>
      ${prevNote}
      <div style="display:flex;align-items:center;justify-content:space-between;gap:.75rem;flex-wrap:wrap">
        <div style="font-size:.72rem;display:flex;align-items:center;gap:.5rem;flex-wrap:wrap">${summaryLine}</div>
        <button id="massApproveSubmitBtn" onclick="massApproveFromSelection()"
          ${canSync ? '' : 'disabled'}
          style="font-size:.78rem;font-weight:700;padding:.33rem .9rem;border-radius:6px;white-space:nowrap;
                 border:1.5px solid ${canSync ? 'var(--indigo)' : 'var(--border)'};
                 background:${canSync ? 'var(--indigo)' : 'var(--grey-bg)'};
                 color:${canSync ? '#fff' : 'var(--text-3)'};
                 cursor:${canSync ? 'pointer' : 'not-allowed'};transition:all .2s">
          ${btnLabel}
        </button>
      </div>
    </div>`;
  // Store mode for massApproveFromSelection
  window._selectedApproveMode = btnMode;
}

// Submit — mode was already determined by _buildMassApproveBarHtml based on run mode
function massApproveFromSelection() {
  const mode = window._selectedApproveMode
    || (lastRun?.mode === 'autopilot' ? 'agentic' : 'high_confident');
  massApprove(mode);
}

// Execute mass-approve
async function massApprove(mode) {
  const rs = window._rosterUploadState;
  if (!rs?.report?.clean) return;

  const modeLabels = {
    agentic:          '∞ Let Mobius decide',
    high_confident:   '✓ Safe only',
    approve_all_defer:'⚡ Approve all, fix later',
    manual:           'Manual',
  };

  const confirmMsg = {
    agentic:          `Mobius will approve all validated providers to your roster and create tasks for every open drift. Continue?`,
    high_confident:   `Approve the safe providers only. Tasks will be created for those that need review. Continue?`,
    approve_all_defer:`Approve all validated providers now, including those with open issues. Mobius creates tasks for every issue so nothing is lost. Continue?`,
    manual:           null,
  }[mode];

  if (confirmMsg && !confirm(confirmMsg)) return;
  if (mode === 'manual') { _showToast('Manual mode — use ✓ Approve to roster on each provider card'); return; }
  feEmit('Mass approve initiated — ' + (modeLabels[mode] || mode));

  // Build the providers payload — ONLY providers not yet in roster
  const clean = rs.report.clean || [];
  const alreadyInRoster = clean.filter(p => p._approvedToTruth);
  const toApprove = clean.filter(p => !p._approvedToTruth);
  const providers = toApprove.map(p => {
    // Merge NPPES result with roster data: prefer NPPES-confirmed NPI, fall back to
    // whatever the roster has so we never silently drop a provider due to an unmatched NPI.
    const npiValidated = p.latest_validation?.npi_validated
                      || (p._decision === 'validated' ? p.npi_uploaded : null)
                      || p.npi_uploaded   // use roster NPI as anchor if NPPES didn't match
                      || '';
    const specialty = p.latest_validation?.specialty_validated
                   || p.specialty_uploaded
                   || '';
    // Signal to the backend that NPPES was attempted (even if unmatched) so it
    // doesn't reject providers that genuinely have no NPPES record.
    const validationDetails = p.latest_validation?.validation_details
                           || (p.latest_validation ? { nppes_attempted: true } : {});
    return {
      id:               p.id,
      provider_name:    p.provider_name,
      npi_uploaded:     p.npi_uploaded,
      npi_validated:    npiValidated,
      state:            p.state || '',
      specialty,
      match_confidence: p.latest_validation?.match_confidence || 0,
      ai_category:      _aiCategory(p),
      drift_dims:       _driftDims(p),
      dismissed_dims:   p._dismissedDims || [],
      validation_details: validationDetails,
      nppes_status:     p.latest_validation?.nppes_status || p.status || '',
    };
  });

  const uploadId = rs.uploadId || rs.report?.upload_id || '';
  if (!uploadId) { _showToast('⚠ No upload ID — cannot sync'); return; }

  // Disable buttons while running
  document.querySelectorAll('#massApproveBar button').forEach(b => {
    b.disabled = true; b.style.opacity = '.5';
  });
  const resultEl = document.getElementById('massApproveResult');
  if (resultEl) resultEl.innerHTML = `<div style="font-size:.78rem;color:var(--indigo);padding:.3rem .25rem;display:flex;align-items:center;gap:.4rem"><span class="spinner"></span> Syncing to roster…</div>`;

  try {
    const runId = lastRun?.run_id || '';
    const orgName = lastRun?.org_name || '';
    const resp = await fetch(`/chat/roster-reconcile/${encodeURIComponent(uploadId)}/mass-approve`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ mode, run_id: runId, org_name: orgName, providers }),
    });
    if (!resp.ok) throw new Error(`Server error ${resp.status}`);
    const data = await resp.json();

    // Show result inline
    const modeLabel = modeLabels[mode] || mode;
    if (resultEl) {
      resultEl.innerHTML = `
        <div style="display:flex;align-items:center;gap:.6rem;flex-wrap:wrap;
          padding:.45rem .65rem;background:var(--grey-bg);border:1px solid var(--border);border-radius:7px;
          font-size:.78rem;margin-bottom:.35rem">
          <span style="color:var(--green);font-weight:600">✓</span>
          <span style="font-weight:600;color:var(--text-2)">${modeLabel} complete</span>
          ${data.approved > 0
            ? `<span style="color:var(--text-2)">${data.approved} promoted</span>`
            : ''}
          ${alreadyInRoster.length > 0
            ? `<span style="color:var(--text-3)">· ${alreadyInRoster.length} already in roster</span>`
            : ''}
          ${data.tasks_created ? `<span style="color:var(--text-3)">· ${data.tasks_created} tasks created</span>` : ''}
          ${data.skipped > 0 ? `<span style="color:var(--text-3)">· ${data.skipped} no NPI (skipped)</span>` : ''}
          <button onclick="this.closest('div').remove()" style="margin-left:auto;font-size:.72rem;color:var(--text-3);background:none;border:none;cursor:pointer">✕</button>
        </div>`;
    }

    // Refresh local task list from audit-created tasks
    await _syncAuditTasksToLocal(data, providers);
    // Mark approved providers in local state using the response list
    if (data.approved_ids && Array.isArray(data.approved_ids)) {
      const clean = window._rosterUploadState?.report?.clean || [];
      data.approved_ids.forEach(id => {
        const p = clean.find(p => p.id === id);
        if (p) { p._approvedToTruth = true; p._decision = 'validated'; }
      });
    }
    // Animated stagger: exit workspace rows, then rebuild both sections
    await _animateMassApproveTransition();
    _refreshTaskQueueFull();
    loadRunAuditLog();
    _loadSessionBanner();
    const toastMsg = data.approved > 0
      ? `✓ ${data.approved} promoted · ${alreadyInRoster.length} already in roster`
      : alreadyInRoster.length > 0
        ? `✓ ${alreadyInRoster.length} already in roster — no changes needed`
        : `⚠ No providers promoted`;
    feEmit('✓ Mass approve complete — ' + (data.approved || 0) + ' promoted' + (data.tasks_created ? ', ' + data.tasks_created + ' tasks created' : ''), 'ok');
    _showToast(toastMsg);
  } catch (err) {
    feEmit('Mass approve failed — ' + String(err), 'error');
    if (resultEl) resultEl.innerHTML = `<div style="font-size:.78rem;color:var(--red);padding:.3rem .25rem">⚠ Sync failed: ${esc(String(err))}</div>`;
  } finally {
    document.querySelectorAll('#massApproveBar button').forEach(b => {
      b.disabled = false; b.style.opacity = '';
    });
  }
}

// After mass-approve, push audit-created tasks into local _reconTasks so they appear in the drawer
function _syncAuditTasksToLocal(data, providers) {
  if (!data.approved && !data.tasks_created) return;
  const tasks = _getOrInitReconTasks();
  const clean = window._rosterUploadState?.report?.clean || [];

  providers.forEach((pd, i) => {
    const dims = (pd.drift_dims || []).filter(d => !(pd.dismissed_dims || []).includes(d));
    dims.forEach(dim => {
      const alreadyHas = tasks.some(t => t.providerIdx === i && t.dims?.includes(dim) && !t.done);
      if (!alreadyHas) {
        tasks.push({
          id: `mass-${i}-${dim}-${Date.now()}`,
          providerIdx: i,
          providerName: pd.provider_name,
          type: 'track',
          severity: _aiCategory(clean[i]) === 'mobius-needs-help' ? 'high' : 'low',
          phase: 2,
          text: `${dim} drift — follow up required`,
          detail: `Created by mass-approve (${data.mode || 'agentic'})`,
          done: false,
          autoCreated: true,
          dims: [dim],
        });
      }
    });
  });
}

// ── Run-level audit log loader ──────────────────────────────────
async function loadRunAuditLog() {
  const runId = lastRun?.run_id;
  const el = document.getElementById('macroAuditBar');
  if (!el) return;
  if (!runId) {
    el.innerHTML = `<div style="font-size:.72rem;color:var(--text-3);padding:.25rem 0">No run ID — audit log unavailable</div>`;
    return;
  }
  el.innerHTML = `<span style="font-size:.72rem;color:var(--text-3)"><span class="spinner" style="width:10px;height:10px;border-width:1.5px;display:inline-block;vertical-align:middle"></span> Loading audit log…</span>`;
  try {
    const orgName = encodeURIComponent(lastRun?.org_name || '');
    const resp = await fetch(`/chat/roster-reconcile/run/${encodeURIComponent(runId)}/audit-log?org_name=${orgName}&limit=100`);
    if (!resp.ok) throw new Error(`${resp.status}`);
    const data = await resp.json();
    // Persist so re-renders (poll-driven bodyKey changes) can restore the loaded state
    window._macroAuditData = data;
    window._macroAuditOpen = true;  // auto-open when user explicitly clicks the link
    el.innerHTML = _buildMacroAuditHtml(data, true);
  } catch (err) {
    el.innerHTML = `<div style="font-size:.72rem;color:var(--text-3);padding:.25rem 0">Audit log unavailable: ${esc(String(err))}</div>`;
  }
}

function _buildMacroAuditHtml(data, isOpen) {
  const summary = data.summary || {};
  const events  = data.events  || [];
  if (!events.length) return '';

  const byType = summary.by_type || {};
  const chips = Object.entries(byType).map(([et, cnt]) => {
    const col = { approved:'var(--green)', task_created:'var(--indigo)',
                  mass_approved:'var(--indigo)', dismissed:'#d97706',
                  npi_overridden:'#d97706', rejected:'var(--red)',
                  validated:'var(--green)' }[et] || 'var(--text-3)';
    return `<span style="font-size:.67rem;font-weight:600;color:${col};background:${col}18;border-radius:5px;padding:.1rem .4rem;border:1px solid ${col}44">${cnt} ${et.replace(/_/g,' ')}</span>`;
  }).join(' ');

  const eventRows = events.slice(0, 30).map(e => {
    const ts   = e.created_at ? new Date(e.created_at).toLocaleString('en-US', {month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'}) : '';
    const icon = { approved:'✓', task_created:'📌', mass_approved:'⚡', dismissed:'~',
                   validated:'🔍', npi_overridden:'✏', rejected:'✗', uploaded:'📤' }[e.event_type] || '·';
    const actorBadge = e.actor === 'mobius'
      ? `<span style="font-size:.6rem;font-weight:700;color:var(--mobius-logo-grey);background:var(--grey-bg);border:1px solid var(--border);border-radius:3px;padding:.05rem .3rem">∞</span>`
      : `<span style="font-size:.6rem;font-weight:700;color:var(--text-3);background:var(--grey-bg);border-radius:3px;padding:.05rem .3rem">user</span>`;
    const detail = e.event_data
      ? Object.entries(e.event_data).filter(([k]) => !['auto_created','total_providers'].includes(k))
               .map(([k,v]) => `${k}: ${typeof v === 'object' ? JSON.stringify(v) : v}`).join(' · ')
      : '';
    return `<div style="display:flex;align-items:baseline;gap:.4rem;padding:.22rem 0;border-bottom:1px solid var(--grey-bg);font-size:.7rem">
      <span style="flex-shrink:0;width:1rem;text-align:center">${icon}</span>
      ${actorBadge}
      <span style="font-weight:600;color:var(--text-2);flex-shrink:0">${esc(e.provider_name || 'run-level')}</span>
      <span style="color:var(--text-3);font-size:.67rem">${esc(e.event_type.replace(/_/g,' '))}${detail ? ' — ' + esc(detail) : ''}</span>
      <span style="flex:1"></span>
      <span style="color:var(--text-3);font-size:.65rem;white-space:nowrap">${ts}</span>
    </div>`;
  }).join('');

  return `
    <details id="macroAuditDetails" ${isOpen ? 'open' : ''}
      ontoggle="window._macroAuditOpen=this.open"
      style="border:1px solid var(--border);border-radius:7px;margin:.35rem 0 .5rem;overflow:hidden">
      <summary style="display:flex;align-items:center;gap:.5rem;padding:.4rem .65rem;cursor:pointer;list-style:none;font-size:.72rem;font-weight:700;color:var(--text-2);background:var(--grey-bg);user-select:none;-webkit-appearance:none">
        <span>📋 Run audit log</span>
        <span style="font-weight:400;color:var(--text-3);font-size:.67rem">${events.length} events</span>
        <span style="flex:1"></span>
        <div style="display:flex;gap:.3rem;flex-wrap:wrap">${chips}</div>
      </summary>
      <div style="padding:.35rem .65rem;max-height:280px;overflow-y:auto">
        ${eventRows}
        ${events.length > 30 ? `<div style="font-size:.67rem;color:var(--text-3);padding:.3rem 0">${events.length - 30} more events — export for full log</div>` : ''}
      </div>
    </details>`;
}

// ── Per-provider audit log loader ─────────────────────────────
async function loadProviderAuditLog(providerId, containerId) {
  const el = document.getElementById(containerId);
  if (!el) return;
  el.innerHTML = `<span class="spinner" style="width:10px;height:10px;border-width:1.5px;display:inline-block;vertical-align:middle"></span> Loading…`;
  try {
    const resp = await fetch(`/chat/roster-reconcile/provider/${providerId}/audit-log?limit=30`);
    if (!resp.ok) throw new Error(`${resp.status}`);
    const data = await resp.json();
    el.innerHTML = _buildProviderAuditHtml(data.events || []);
  } catch (err) {
    el.innerHTML = `<span style="color:var(--text-3);font-size:.72rem">Audit log unavailable</span>`;
  }
}

function _buildProviderAuditHtml(events) {
  if (!events.length) {
    return `<span style="font-size:.72rem;color:var(--text-3)">No audit events yet for this provider.</span>`;
  }
  const rows = events.map(e => {
    const ts   = e.created_at ? new Date(e.created_at).toLocaleString('en-US', {month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'}) : '';
    const icon = { approved:'✓', task_created:'📌', dismissed:'~', validated:'🔍',
                   npi_overridden:'✏', rejected:'✗', uploaded:'📤', drift_detected:'⚠',
                   mass_approved:'⚡' }[e.event_type] || '·';
    const col  = { approved:'var(--green)', task_created:'var(--indigo)', dismissed:'#d97706',
                   rejected:'var(--red)', npi_overridden:'#d97706', validated:'var(--green)' }[e.event_type] || 'var(--text-3)';
    const actorBadge = e.actor === 'mobius'
      ? `<span style="font-size:.58rem;font-weight:600;color:var(--mobius-logo-grey);background:var(--grey-bg);border:1px solid var(--border);border-radius:3px;padding:.03rem .25rem">∞ Mobius</span>`
      : `<span style="font-size:.58rem;color:var(--text-3);background:var(--grey-bg);border-radius:3px;padding:.03rem .25rem">👤 ${esc(e.actor_label || 'User')}</span>`;
    const detail = e.event_data
      ? Object.entries(e.event_data).filter(([k,v]) => v && !['auto_created','mode'].includes(k))
               .map(([k,v]) => `${k.replace(/_/g,' ')}: ${typeof v === 'object' ? JSON.stringify(v) : v}`).join(', ')
      : '';
    return `<div style="display:flex;align-items:baseline;gap:.35rem;padding:.2rem 0;border-bottom:1px solid var(--grey-bg);font-size:.7rem;line-height:1.4">
      <span style="flex-shrink:0;color:${col}">${icon}</span>
      ${actorBadge}
      <span style="color:var(--text-2)">${esc(e.event_type.replace(/_/g,' '))}${detail ? ` — ${esc(detail)}` : ''}</span>
      <span style="flex:1"></span>
      <span style="color:var(--text-3);font-size:.65rem;white-space:nowrap">${ts}</span>
    </div>`;
  }).join('');
  return `<div style="max-height:160px;overflow-y:auto">${rows}</div>`;
}

function buildReconTabHtml() {
  const tasks  = _getOrInitReconTasks();
  const filter = window._reconFilter || 'needs-help';  // default to what needs action
  const rs     = window._rosterUploadState;
  const clean  = rs?.report?.clean || [];

  // ── counts — workspace only (exclude already-promoted) ────────
  const workspace  = clean.filter(p => !p._approvedToTruth);
  const noIssues   = workspace.filter(p => _aiCategory(p) === 'no-issues').length;
  const confident  = workspace.filter(p => _aiCategory(p) === 'mobius-confident').length;
  const needsHelp  = workspace.filter(p => _aiCategory(p) === 'mobius-needs-help').length;
  const deact      = workspace.filter(p => _reconCat(p) === 'deactivated').length;
  const noMatch    = workspace.filter(p => _reconCat(p) === 'no-match').length;
  const promoted   = clean.filter(p => p._approvedToTruth).length;
  const pending    = needsHelp + deact + noMatch;
  const openTasks  = (tasks || []).filter(t => !t.done && t.type !== 'confirmed').length;

  // ── filter pills (3 only — no "All") ─────────────────────────
  const pillDefs = [
    ['mobius-needs-help', `⚠ Needs your input`, '#d97706', pending],
    ['mobius-confident',  `∞ Mobius confident`,  'var(--mobius-logo-grey)', confident],
    ['no-issues',         `✓ No issues`,          'var(--green)',  noIssues],
  ];
  const pills = pillDefs.map(([f, label, col, cnt]) => {
    const active = filter === f;
    // Active pill uses full color; inactive pills are neutral grey — color should guide, not shout
    const activeStyle = active
      ? `background:var(--text);color:#fff;border-color:var(--text);`
      : ``;
    const countColor = active ? 'rgba(255,255,255,.7)' : (cnt > 0 ? col : 'var(--text-3)');
    return `<button class="prov-filter-pill${active ? ' active' : ''}" data-filter="${f}"
      onclick="setReconFilter('${f}')"
      style="${activeStyle}">
      <span class="rpill-label">${label}</span>
      <span class="rpill-count" style="font-size:.65rem;font-weight:600;color:${countColor};margin-left:.1rem">${cnt}</span>
    </button>`;
  }).join('');

  return `
    <!-- ── Session work banner (async-loaded) ─── -->
    <div id="sessionBanner" style="margin-bottom:.6rem">${_buildSessionBannerHtml(window._sessionSummary || null)}</div>

    <!-- ── WORKSPACE section ─────────────────── -->
    <div id="workspaceSection" class="sec-card" style="margin-bottom:.5rem;overflow:hidden;animation-delay:40ms">
      <!-- Header bar mirrors old details summary -->
      <div class="sec-summary" style="cursor:default">
        <span class="sec-label">Workspace</span>
        ${pending > 0
          ? `<span class="sec-meta" style="color:#b45309;font-weight:500">· ${pending} need your input</span>`
          : `<span class="sec-meta">· all clear</span>`}
        ${promoted > 0 ? `<span class="sec-meta">· ${promoted} moved to roster</span>` : ''}
        <span class="sec-spacer"></span>
        <button onclick="_openStepTaskDrawer()" class="sec-action">
          Tasks${openTasks > 0 ? ` <span style="background:var(--indigo);color:#fff;border-radius:10px;padding:.02rem .35rem;font-size:.6rem">${openTasks}</span>` : ''}
        </button>
      </div>
      <div class="sec-body">
        <!-- Sync to roster bar -->
        ${_buildMassApproveBarHtml()}
        <div id="massApproveResult"></div>

        <!-- Filter pills + search -->
        <div style="display:flex;align-items:center;gap:.4rem;flex-wrap:wrap;margin:.5rem 0 .4rem">
          <div class="prov-filter-bar" id="reconPillBar" style="margin:0;flex:0 0 auto">${pills}</div>
          <input type="text" id="reconSearchInput" placeholder="Search by name or NPI…"
            oninput="filterReconTable()"
            style="flex:1;min-width:160px;font-size:.78rem;padding:.3rem .6rem;border:1px solid var(--border);border-radius:6px;color:var(--text);background:var(--surface)">
        </div>

        <!-- Provider table -->
        <div id="reconSection3">${_buildReconSection3Html(filter)}</div>
      </div>
    </div>

    <!-- ── ROSTER section ────────────────────── -->
    <div id="rosterSection" style="display:none">${_buildRosterSectionHtml()}</div>

    <!-- Activity log link (workspace only) -->
    <div id="rosterActivityLink" style="text-align:right;padding:.25rem 0;border-top:1px solid var(--grey-bg);margin-top:.5rem">
      <button onclick="loadRunAuditLog()" style="font-size:.67rem;color:var(--text-3);background:none;border:none;cursor:pointer;padding:0">
        Activity log · provider-level audit available in each card ▸
      </button>
    </div>
    <!-- macroAuditBar: populated by loadRunAuditLog(); if already loaded (window._macroAuditData),
         inline-render on every re-render so poll-driven bodyKey changes don't wipe the open state -->
    <div id="macroAuditBar">${window._macroAuditData ? _buildMacroAuditHtml(window._macroAuditData, window._macroAuditOpen) : ''}</div>

    <!-- Task drawers are rendered in the page shell (always in DOM) —
         see the static <div id="pipelineView"> section below the step card. -->`;
}

function _refreshReconPillCounts() {
  const rs = window._rosterUploadState;
  if (!rs?.report?.clean) return;
  // Only count providers not yet promoted to roster
  const c = rs.report.clean.filter(p => !p._approvedToTruth);
  const counts = {
    'no-issues':         c.filter(p => _aiCategory(p) === 'no-issues').length,
    'mobius-confident':  c.filter(p => _aiCategory(p) === 'mobius-confident').length,
    'mobius-needs-help': c.filter(p => _aiCategory(p) === 'mobius-needs-help'
                                    || ['deactivated','no-match'].includes(_reconCat(p))).length,
  };
  const cols = {
    'mobius-needs-help': '#d97706',
    'mobius-confident':  'var(--indigo)',
    'no-issues':         'var(--green)',
  };
  document.querySelectorAll('#reconPillBar .prov-filter-pill[data-filter]').forEach(btn => {
    const f       = btn.dataset.filter;
    const cnt     = counts[f] ?? 0;
    const col     = cols[f] || '';
    const isActive = btn.classList.contains('active');
    const countEl = btn.querySelector('.rpill-count');
    if (countEl) {
      countEl.textContent = cnt;
      // When active, white-on-dark; when inactive, only urgent gets color
      countEl.style.color = isActive ? 'rgba(255,255,255,.7)'
        : (cnt > 0 && f === 'mobius-needs-help' ? col : 'var(--text-3)');
      countEl.style.background = 'none';
    }
  });
}

// Full recon view refresh — rebuilds table + pills + stat bar
function _refreshReconView() {
  const f = window._reconFilter || 'needs-help';
  const s3 = document.getElementById('reconSection3');
  if (s3) s3.innerHTML = _buildReconSection3Html(f);
  _refreshReconPillCounts();
  _refreshNppesSection();
}

function setReconFilter(f) {
  window._reconFilter = f;
  document.querySelectorAll('.prov-filter-pill').forEach(p => {
    const active = p.dataset.filter === f;
    p.classList.toggle('active', active);
    // Update inline active colors
    const col = p.dataset.filter === 'mobius-needs-help' ? '#d97706'
      : p.dataset.filter === 'mobius-confident' ? 'var(--indigo)'
      : p.dataset.filter === 'no-issues' ? 'var(--green)' : '';
    if (col) {
      p.style.borderColor = active ? col : '';
      p.style.color       = active ? col : '';
      p.style.background  = active ? col + '14' : '';
    }
  });
  _refreshReconView();
}

function _refreshNppesSection() {
  // Refresh roster section (score story) after approvals or changes
  const rosterSec = document.getElementById('rosterSection');
  if (rosterSec) rosterSec.innerHTML = _buildRosterSectionHtml();
  // Update workspace header counts
  _refreshWorkspaceHeader();
  _refreshReconPillCounts();
}

function _refreshWorkspaceHeader() {
  const ws = document.getElementById('workspaceSection');
  if (!ws) return;
  const summary = ws.querySelector('summary');
  if (!summary) return;
  const clean = window._rosterUploadState?.report?.clean || [];
  const tasks  = _getOrInitReconTasks() || [];
  // Only count workspace providers (not already promoted)
  const wsProviders = clean.filter(p => !p._approvedToTruth);
  const pending   = wsProviders.filter(p => ['mobius-needs-help'].includes(_aiCategory(p)) || ['deactivated','no-match'].includes(_reconCat(p))).length;
  const promoted  = clean.filter(p => p._approvedToTruth).length;
  const openTasks = tasks.filter(t => !t.done && t.type !== 'confirmed').length;
  const wsEmpty   = wsProviders.length === 0 && promoted > 0;
  // Re-render summary contents
  summary.innerHTML = `
    <div style="display:flex;align-items:baseline;gap:.5rem;flex:1;flex-wrap:wrap">
      <span style="font-size:.875rem;font-weight:600;color:var(--text-2)">Workspace</span>
      ${wsEmpty
        ? `<span style="font-size:.8rem;font-weight:400;color:var(--text-3)">all ${promoted} moved to roster</span>`
        : pending > 0
          ? `<span style="font-size:.8rem;font-weight:500;color:#b45309">${pending} need your input</span>`
          : `<span style="font-size:.8rem;font-weight:400;color:var(--text-3)">all clear</span>`}
      ${promoted > 0 && !wsEmpty ? `<span style="font-size:.75rem;color:var(--text-3)">· ${promoted} moved to roster</span>` : ''}
    </div>
    <button onclick="_openStepTaskDrawer()" style="display:flex;align-items:center;gap:.3rem;font-size:.72rem;font-weight:600;padding:.22rem .65rem;border-radius:6px;border:1px solid var(--border);background:var(--surface);color:var(--text-2);cursor:pointer;white-space:nowrap;transition:all .12s">
      Tasks${openTasks > 0 ? ` <span style="background:var(--indigo);color:#fff;border-radius:10px;padding:.02rem .35rem;font-size:.6rem">${openTasks}</span>` : ''}
    </button>
    <span style="font-size:.6rem;color:var(--text-3)">▾</span>`;
}

// ── Tab switching (kept for any legacy references) ────────────
window._activeProvTab = 'all';
function switchProvTab(tab) {
  window._activeProvTab = tab;
  document.querySelectorAll('.src-tab').forEach(t => t.classList.toggle('active', t.dataset.tab === tab));
  document.querySelectorAll('.src-tab-panel').forEach(p => p.classList.toggle('active', p.id === `provTab-${tab}`));
}

// ── Roster diff + change-type system ──────────────────────────────────────────
let _rosterDiff = null;        // { providers, counts, delta, auto_pass }
let _rosterSnoozes = null;     // array of snooze records for this org
let _rosterFilter = 'all';
let _rosterSearch = '';

async function _loadRosterDiff() {
  if (!runId) return;
  try {
    const r = await fetch(`${API}/chat/credentialing-runs/${runId}/roster-diff`);
    if (!r.ok) return;
    _rosterDiff = await r.json();
    _applyRosterDiff();
  } catch { /* non-fatal */ }
}

function _applyRosterDiff() {
  if (!_rosterDiff) return;
  const { counts, delta, auto_pass, providers } = _rosterDiff;

  // ── Store diff summary for upload section header ──────────────
  const lastDate = (providers.find(p => p.truth_match?.validated_at) || {}).truth_match?.validated_at;
  window._lastRosterDiffSummary = {
    auto_pass,
    total:         counts.total || 0,
    new:           counts.new   || 0,
    changed:       counts.changed || 0,
    removed:       counts.removed || 0,
    lastValidated: lastDate || null,
  };

  // ── Emit diff result to process log ─────────────────────────
  if (auto_pass && counts.total > 0) {
    const dateLabel = lastDate
      ? `last validated ${new Date(lastDate).toLocaleDateString('en-US',{month:'short',day:'numeric',year:'numeric'})}`
      : '';
    _emitRosterLog('success',
      `${counts.total} providers — no changes from last validated roster`,
      dateLabel || null);
  } else if (counts.total > 0) {
    const parts = [];
    if (counts.new)     parts.push(`${counts.new} new`);
    if (counts.changed) parts.push(`${counts.changed} changed`);
    if (counts.removed) parts.push(`${counts.removed} removed`);
    _emitRosterLog('info', `Roster diff: ${parts.join(', ')} (${counts.total} total)`);
  }

  // ── Update upload section summary to show the diff result ────
  _updateUploadSummary();

  // ── Show/populate query bar chip counts ──────────────────────
  const bar = document.getElementById('rosterFilterBar');
  if (bar && counts.total > 0) {
    bar.style.display = '';

    // Needs attention = providers with an open non-confirmed task
    const needsAttn = (_reconTasks || []).filter(t => !t.done && t.type !== 'confirmed');
    const needsAttnProv = new Set(needsAttn.map(t => t.providerIdx));

    // No NPI = no validated or roster NPI
    const noNpi = (providers || []).filter(p => !p.npi_validated && !p.npi_roster).length;

    // Open tasks = providers with any open task
    const openTaskProv = new Set((_reconTasks||[]).filter(t=>!t.done).map(t=>t.providerIdx));

    const c_na  = document.getElementById('rqc-needs-attention');
    const c_npi = document.getElementById('rqc-no-npi');
    const c_ot  = document.getElementById('rqc-open-tasks');
    if (c_na)  c_na.textContent  = needsAttnProv.size  > 0 ? needsAttnProv.size  : '';
    if (c_npi) c_npi.textContent = noNpi               > 0 ? noNpi               : '';
    if (c_ot)  c_ot.textContent  = openTaskProv.size   > 0 ? openTaskProv.size   : '';

    // Hide chips with 0 count (not applicable for this roster)
    if (c_na?.parentElement)  c_na.parentElement.style.display  = needsAttnProv.size ? '' : 'none';
    if (c_npi?.parentElement) c_npi.parentElement.style.display = noNpi           ? '' : 'none';
    if (c_ot?.parentElement)  c_ot.parentElement.style.display  = openTaskProv.size  ? '' : 'none';

    _updateRqbarChips();
  }

  // ── Tag visible roster rows with change-type badges ───────────
  _applyChangeTypeBadges(providers);
}

function _applyChangeTypeBadges(providers) {
  if (!providers) return;
  const rs = window._rosterUploadState;
  if (!rs?.report?.clean) return;

  // Build a lookup by normalised name + NPI
  const diffByNpi  = {};
  const diffByName = {};
  for (const p of providers) {
    const npi = p.npi_validated || p.npi_roster || '';
    if (npi) diffByNpi[npi] = p;
    diffByName[(p.provider_name || '').toLowerCase().trim()] = p;
  }

  rs.report.clean.forEach((prov, idx) => {
    const row = document.getElementById(`rr-${idx}`);
    if (!row) return;
    const npi   = prov.npi_validated || prov.npi_roster || '';
    const match = diffByNpi[npi] || diffByName[(prov.provider_name || '').toLowerCase().trim()];
    if (!match) return;

    const ct = match.change_type || 'new';
    // Inject change badge into the name cell (first td) if not already there
    const nameTd = row.querySelector('td:first-child');
    if (nameTd && !nameTd.querySelector('.ct-badge')) {
      const badge = document.createElement('span');
      badge.className = `ct-badge ct-${ct}`;
      badge.textContent = ct === 'unchanged' ? '✓' : ct.charAt(0).toUpperCase() + ct.slice(1);
      badge.style.marginLeft = '.3rem';
      const nameEl = nameTd.querySelector('span');
      if (nameEl) nameEl.after(badge);
    }

    // Attach snooze buttons to alignment chips that have changes
    if (ct === 'changed') {
      (match.field_changes || []).forEach(fc => {
        _attachSnoozeButton(idx, prov, fc);
      });
    }
  });
}

function _attachSnoozeButton(idx, prov, fieldChange) {
  const row     = document.getElementById(`rr-${idx}`);
  if (!row) return;
  const dim     = fieldChange.field;
  const isSnoozed = fieldChange.snoozed && fieldChange.fingerprint_match;

  // Find the alignment chip for this dimension
  const chipSel = `button[onclick*="${dim}"]`;
  const chip    = row.querySelector(chipSel);
  if (!chip || chip.nextSibling?.classList?.contains('snooze-btn')) return;

  const npi = prov.npi_validated || prov.npi_roster || '';
  const btn = document.createElement('button');
  btn.className  = `snooze-btn${isSnoozed ? ' snoozed' : ''}`;
  btn.title      = isSnoozed ? 'Snoozed — will wake up if values change' : 'Snooze this mismatch';
  btn.textContent = isSnoozed ? '💤 Snoozed' : 'Snooze';
  btn.onclick = (e) => {
    e.stopPropagation();
    snoozeMismatch(npi || prov.provider_name, dim,
      fieldChange.roster_val, fieldChange.nppes_val || '', btn);
  };
  chip.after(btn);
}

// Text shortcuts: type natural phrases to activate a quick filter
const _rosterQueryShortcuts = {
  'no npi':          'no-npi',
  'missing npi':     'no-npi',
  'no match':        'no-npi',
  'open tasks':      'open-tasks',
  'tasks':           'open-tasks',
  'needs attention': 'needs-attention',
  'attention':       'needs-attention',
  'review':          'needs-attention',
  'deactivated':     'needs-attention',
};

function _onRosterQueryInput() {
  const raw = (document.getElementById('rosterSearchInput')?.value || '').toLowerCase().trim();
  const shortcut = Object.keys(_rosterQueryShortcuts).find(k => raw === k || raw.startsWith(k + ' '));
  if (shortcut) {
    const f = _rosterQueryShortcuts[shortcut];
    _rosterFilter = f;
    _rosterSearch = '';
    _updateRqbarChips();
  } else {
    _rosterSearch = raw;
    if (_rosterFilter !== 'all' && raw) {
      // typing free text clears the active chip to show all matching rows
      _rosterFilter = 'all';
      _updateRqbarChips();
    }
  }
  filterRosterTable();
}

function setRosterFilter(f) {
  // Toggle off if already active
  if (_rosterFilter === f) f = 'all';
  _rosterFilter = f;
  _rosterSearch = '';
  const inp = document.getElementById('rosterSearchInput');
  if (inp) inp.value = '';
  _updateRqbarChips();
  filterRosterTable();
}

function _updateRqbarChips() {
  document.querySelectorAll('#rqbarChips .rqchip[data-filter]').forEach(btn => {
    const isActive = btn.dataset.filter === _rosterFilter;
    btn.classList.toggle('active', isActive);
    // Hide "clear" chip unless a filter is active
    if (btn.dataset.filter === 'all') btn.style.display = _rosterFilter !== 'all' ? '' : 'none';
  });
}

function filterRosterTable() {
  const rs = window._rosterUploadState;
  if (!rs?.report?.clean) return;

  rs.report.clean.forEach((prov, idx) => {
    const row = document.getElementById(`rr-${idx}`);
    const det = document.getElementById(`nppes-detail-${idx}`);
    if (!row) return;

    const name = (prov.provider_name || '').toLowerCase();
    const npi  = (prov.npi_validated || prov.npi_roster || '').toLowerCase();

    // ── Text search ─────────────────────────────────────────────
    const searchPass = !_rosterSearch
      || name.includes(_rosterSearch)
      || npi.includes(_rosterSearch);

    // ── Quick-filter chips ───────────────────────────────────────
    let filterPass = true;
    if (_rosterFilter === 'no-npi') {
      filterPass = !prov.npi_validated && !prov.npi_roster;
    } else if (_rosterFilter === 'open-tasks') {
      filterPass = (_reconTasks || []).some(t => t.providerIdx === idx && !t.done);
    } else if (_rosterFilter === 'needs-attention') {
      const task = (_reconTasks || []).find(t => t.providerIdx === idx);
      filterPass = task && !task.done && task.type !== 'confirmed';
    }
    // 'all' = no filter

    const show = searchPass && filterPass;
    row.style.display = show ? '' : 'none';
    if (det) det.style.display = show ? det.style.display : 'none';
  });
}

async function snoozeMismatch(providerKey, dimension, rosterVal, nppesVal, btn) {
  if (!runId) return;
  btn.disabled = true;
  btn.textContent = 'Snoozing…';
  try {
    const r = await fetch(`${API}/chat/credentialing-runs/${runId}/roster-snooze`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ provider_key: providerKey, dimension, roster_val: rosterVal, nppes_val: nppesVal }),
    });
    if (r.ok) {
      btn.className = 'snooze-btn snoozed';
      btn.textContent = '💤 Snoozed';
      btn.title = 'Snoozed — will wake up if values change';
      btn.disabled = false;
      // Refresh diff counts
      await _loadRosterDiff();
    }
  } catch { btn.disabled = false; }
}

async function saveRosterTruth() {
  // Called when user confirms the roster (Accept & Continue)
  if (!runId) return;
  const rs = window._rosterUploadState;
  if (!rs?.report?.clean) return;
  const validated = rs.report.clean
    .filter(p => p._decision === 'validated' || p._decision === 'unchanged')
    .map(p => ({
      provider_name:    p.provider_name,
      npi_roster:       p.npi_roster || p.npi_uploaded || '',
      npi_validated:    p.npi_validated || (p.latest_validation?.npi_validated) || '',
      specialty:        p.specialty || '',
      match_confidence: p.latest_validation?.match_confidence || null,
      decision:         'validated',
    }));
  if (!validated.length) return;
  try {
    await fetch(`${API}/chat/credentialing-runs/${runId}/roster-truth`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ providers: validated }),
    });
  } catch { /* non-fatal */ }
}

// buildRosterFileZone replaced by buildRosterFileZoneHtml (state-driven)

async function toggleRosterHistory() {
  const panel = document.getElementById('rosterHistoryPanel');
  if (!panel) return;
  if (panel.style.display !== 'none') { panel.style.display = 'none'; return; }
  panel.style.display = '';
  const orgName = window.lastRun?.org_name || '';
  if (!orgName) { panel.innerHTML = '<span style="color:var(--text-3)">No org context — start a run first.</span>'; return; }
  try {
    const r = await fetch(`/chat/roster-reconcile/uploads?org_name=${encodeURIComponent(orgName)}&limit=10`);
    const d = await r.json();
    const uploads = d.uploads || [];
    if (!uploads.length) { panel.innerHTML = '<span>No previous uploads found.</span>'; return; }
    panel.innerHTML = uploads.map(u => {
      const dt = u.created_at ? new Date(u.created_at).toLocaleDateString('en-US',{month:'short',day:'numeric',year:'numeric'}) : '';
      const status = u.reconciliation_status || u.status || '';
      return `<div style="display:flex;justify-content:space-between;align-items:center;padding:.25rem 0;border-bottom:1px solid var(--border)">
        <span>${esc(u.filename || 'roster')}</span>
        <span style="color:var(--text-3)">${dt} · ${status}</span>
      </div>`;
    }).join('') + `<div style="margin-top:.35rem;color:var(--text-3);font-size:.65rem">Uploads are listed for audit. To use a previous roster, re-upload the file.</div>`;
  } catch { panel.innerHTML = '<span style="color:var(--red)">Failed to load history.</span>'; }
}

function triggerRosterUpload() {
  const inp = document.getElementById('rosterFileInput');
  if (inp) inp.click();
}

function triggerPmlUpload() {
  const inp = document.getElementById('pmlFileInput');
  if (inp) inp.click();
}

// ── Roster upload state (survives poll re-renders) ────────────
// phases: null | 'uploading' | 'parsing' | 'cleaning' | 'done' | 'error'
window._rosterUploadState = null;
window._rosterEmissions = [];   // timestamped event log for the process log panel
let _rosterSaveTimer = null;

// ── Roster emissions log ─────────────────────────────────────────────────────
function _emitRosterLog(type, message, detail) {
  // type: 'info' | 'success' | 'warn' | 'error' | 'ai' | 'progress'
  const phase = window._rosterUploadState?.phase || null;
  const entry = { ts: Date.now(), type, message, detail: detail || null, phase };
  window._rosterEmissions = window._rosterEmissions || [];
  window._rosterEmissions.push(entry);
  // Mirror milestone events to the global feEmit activity ticker
  if (type === 'success' || type === 'error' || type === 'warn' ||
      message.startsWith('✓') || message.startsWith('✗') || message.startsWith('⚠') ||
      message.includes('complete') || message.includes('ready') || message.includes('failed') ||
      message.includes('providers') || message.includes('workspace') || message.includes('roster')) {
    const feLevel = type === 'error' ? 'error' : type === 'warn' ? 'warn' : 'ok';
    feEmit(message, feLevel);
  }
  // Live-update the body if open
  const logEl = document.getElementById('rosterEmissionsLog');
  if (logEl && logEl.classList.contains('open')) _renderEmissionsInto(logEl);
  // Refresh count badge and preview text
  const cnt = document.getElementById('rosterEmissionCount');
  if (cnt) cnt.textContent = window._rosterEmissions.length;
  const prev = document.getElementById('rosterEmPreview');
  if (prev && !window._rosterEmOpen) prev.textContent = `· ${message.substring(0,65)}`;
  // Show pulse dot while active
  const wrap = document.getElementById('rosterEmWrap');
  if (wrap) {
    const pulse = wrap.querySelector('.sl-pulse');
    const phase_ = window._rosterUploadState?.phase;
    if (pulse && !['uploading','parsing','cleaning'].includes(phase_)) pulse.style.animation = 'none';
  }
}

function _renderEmissionsInto(container) {
  const ev = window._rosterEmissions || [];
  if (!ev.length) { container.innerHTML = ''; return; }

  // Group events by phase — insert phase headers when type changes
  let lastPhase = null;
  const phaseLabel = { uploading:'Uploading', parsing:'Parsing', cleaning:'AI review', done:'Complete' };
  container.innerHTML = ev.map(e => {
    const phase = e.phase || null;
    let phaseHdr = '';
    if (phase && phase !== lastPhase) {
      lastPhase = phase;
      phaseHdr = `<div class="sl-phase">${phaseLabel[phase] || phase}</div>`;
    }
    const ts   = new Date(e.ts).toLocaleTimeString('en-US',{hour:'numeric',minute:'2-digit',second:'2-digit'});
    const icon = e.type==='success'?'✓': e.type==='error'?'✗': e.type==='warn'?'△': e.type==='ai'?'∞': '·';
    const cls  = e.type==='success'?'success': e.type==='error'?'error': e.type==='warn'?'warn': e.type==='ai'?'ai': '';
    const detail = e.detail ? `<span style="opacity:.55"> — ${esc(e.detail)}</span>` : '';
    return `${phaseHdr}<div class="sl-line">
      <span class="sl-ts">${ts}</span>
      <span class="sl-icon" style="${cls==='success'?'color:var(--green)':cls==='error'?'color:var(--red)':cls==='warn'?'color:var(--amber)':cls==='ai'?'color:var(--mobius-logo-grey)':'color:var(--text-3);opacity:.5'}">${icon}</span>
      <span class="sl-msg ${cls}">${esc(e.message)}${detail}</span>
    </div>`;
  }).join('');
  container.scrollTop = container.scrollHeight;
}

function buildRosterEmissionsHtml() {
  const ev       = window._rosterEmissions || [];
  const count    = ev.length;
  const isActive = ['uploading','parsing','cleaning'].includes(window._rosterUploadState?.phase);
  const hasError = ev.some(e => e.type === 'error');
  const hasWarn  = ev.some(e => e.type === 'warn');

  if (!count && !isActive) return '';   // nothing to show yet

  // Last meaningful message for the toggle preview
  const last = [...ev].reverse().find(e => e.message);
  const preview = last ? last.message.substring(0, 65) : '';

  const dot = isActive
    ? `<span class="sl-pulse"></span>`
    : hasError ? `<span style="width:5px;height:5px;border-radius:50%;background:var(--red);display:inline-block"></span>`
    : hasWarn  ? `<span style="width:5px;height:5px;border-radius:50%;background:var(--amber);display:inline-block"></span>`
    : '';

  return `<div class="stream-log-wrap" id="rosterEmWrap" style="margin-top:.45rem">
    <button class="stream-log-toggle" onclick="_toggleRosterEmissions()">
      ${dot}
      <span style="opacity:.7">Process log</span>
      <span id="rosterEmissionCount" style="font-size:.6rem;opacity:.5;font-variant-numeric:tabular-nums">${count}</span>
      ${preview && !window._rosterEmOpen ? `<span id="rosterEmPreview" style="opacity:.4;max-width:220px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">· ${esc(preview)}</span>` : ''}
      <span id="rosterEmChevron" style="font-size:.55rem;opacity:.4">${window._rosterEmOpen?'▴':'▾'}</span>
    </button>
    <div class="stream-log-body${window._rosterEmOpen?' open':''}" id="rosterEmissionsLog" style="margin-top:.3rem">
    </div>
  </div>`;
}

window._rosterEmOpen = false;
function _toggleRosterEmissions() {
  window._rosterEmOpen = !window._rosterEmOpen;
  const body = document.getElementById('rosterEmissionsLog');
  const chev = document.getElementById('rosterEmChevron');
  const prev = document.getElementById('rosterEmPreview');
  if (body) {
    body.classList.toggle('open', window._rosterEmOpen);
    if (window._rosterEmOpen) { _renderEmissionsInto(body); body.scrollTop = body.scrollHeight; }
  }
  if (chev) chev.textContent = window._rosterEmOpen ? '▴' : '▾';
  if (prev) prev.style.display = window._rosterEmOpen ? 'none' : '';
}

function _showRosterSaveStatus(state) {
  const el = document.getElementById('rosterSaveStatus');
  if (!el) return;
  clearTimeout(_rosterSaveTimer);
  if (state === 'saving') {
    el.innerHTML = `<span class="spinner"></span> Saving…`;
    el.style.color = 'var(--text-3)';
  } else if (state === 'saved') {
    el.innerHTML = '✓ Saved';
    el.style.color = 'var(--green)';
    _rosterSaveTimer = setTimeout(() => { el.innerHTML = ''; }, 3000);
  } else {
    el.innerHTML = '⚠ Save failed';
    el.style.color = 'var(--red)';
    _rosterSaveTimer = setTimeout(() => { el.innerHTML = ''; }, 5000);
  }
}

async function _rosterSaveDecision(idx, body) {
  const p = _rosterProvider(idx);
  if (!p || !p.id) return;
  _showRosterSaveStatus('saving');
  try {
    const r = await fetch(`/chat/roster-reconcile/provider/${p.id}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (r.ok) _showRosterSaveStatus('saved');
    else _showRosterSaveStatus('error');
  } catch(e) {
    _showRosterSaveStatus('error');
  }
}

async function _rosterSaveExclude(providerId) {
  if (!providerId) return;
  _showRosterSaveStatus('saving');
  try {
    const r = await fetch(`/chat/roster-reconcile/provider/${providerId}`, { method: 'DELETE' });
    if (r.ok) _showRosterSaveStatus('saved');
    else _showRosterSaveStatus('error');
  } catch(e) {
    _showRosterSaveStatus('error');
  }
}

function _setRosterState(patch) {
  const prev = window._rosterUploadState;
  const prevPhase = prev?.phase;
  window._rosterUploadState = Object.assign(prev || {}, patch);
  // Re-render whenever the roster DOM elements are present
  const zone = document.getElementById('rosterFileZone');
  if (zone) zone.innerHTML = buildRosterFileZoneHtml();
  const prog = document.getElementById('rosterParseProgress');
  if (prog) prog.innerHTML = buildRosterProgressHtml();
  // Update the upload section summary on every phase change
  _updateUploadSummary();

  // Auto-open while actively processing so user can see progress
  if (['uploading','parsing','cleaning'].includes(patch.phase) && !['uploading','parsing','cleaning'].includes(prevPhase)) {
    window._uploadSectionOpenState = true;
    const uploadDetails = document.getElementById('uploadSection');
    if (uploadDetails) uploadDetails.setAttribute('open', '');
  }

  // When phase transitions to 'done', collapse upload section and remember that state
  if (patch.phase === 'done' && prevPhase !== 'done') {
    window._uploadSectionOpenState = false;
    const uploadDetails = document.getElementById('uploadSection');
    if (uploadDetails) uploadDetails.removeAttribute('open');
    const reconContent = document.getElementById('reconContent');
    if (reconContent) {
      console.log('[setRosterState] phase→done: refreshing reconContent');
      reconContent.innerHTML = buildReconTabHtml();
      setTimeout(_loadSessionBanner, 400);
      // Always schedule _loadRosterTruth — it will use window._rosterTruth if already
      // populated (no extra network fetch), or fetch fresh if not.
      setTimeout(_loadRosterTruth, 600);
    }
    // Roster is ready — enable the top action button if we're on the nppes_alignment step
    if (lastRun?.pending_step_id === 'nppes_alignment') {
      const foot = document.getElementById('scFoot');
      if (foot) foot.innerHTML = buildStepFoot('nppes_alignment', lastRun, lastRun.draft_output || {});
    }
  }
}

// ── Upload section summary headline ─────────────────────────────────────────
// Returns the inner HTML for the <summary> element.
// Called at render time and imperatively via _updateUploadSummary().
function _buildUploadSummaryHtml() {
  const s   = window._rosterUploadState;
  const diff = window._lastRosterDiffSummary;   // set by _loadRosterDiff

  // Shared inner row — uses sec-* classes from shared card CSS
  const row = (icon, label, meta, iconColor='var(--text-3)', action='Upload new ↑') => `
    <span style="font-size:.78rem;color:${iconColor};flex-shrink:0">${icon}</span>
    <span class="sec-label">${label}</span>
    ${meta ? `<span class="sec-meta">· ${meta}</span>` : ''}
    <span class="sec-spacer"></span>
    <span style="font-size:.72rem;color:var(--indigo);font-weight:500;flex-shrink:0">${action}</span>
    <span class="sec-chevron">▾</span>`;

  // ── Active states ──────────────────────────────────────────────
  if (s?.phase === 'uploading') return `
    <span class="spinner" style="width:.72rem;height:.72rem;border-width:2px;flex-shrink:0"></span>
    <span class="sec-label">Upload</span>
    <span class="sec-meta">· uploading ${esc(s.fileName || 'file')}…</span>
    <span class="sec-spacer"></span><span class="sec-chevron">▾</span>`;

  if (s?.phase === 'parsing') {
    const prog = s.total ? `${s.done || 0} / ${s.total} providers` : 'validating…';
    return `
    <span class="spinner" style="width:.72rem;height:.72rem;border-width:2px;flex-shrink:0"></span>
    <span class="sec-label">Upload</span>
    <span class="sec-meta">· ${prog}</span>
    <span class="sec-spacer"></span><span class="sec-chevron">▾</span>`;
  }
  if (s?.phase === 'cleaning') return `
    <span class="spinner" style="width:.72rem;height:.72rem;border-width:2px;flex-shrink:0"></span>
    <span class="sec-label">Upload</span>
    <span class="sec-meta">· AI reviewing rows…</span>
    <span class="sec-spacer"></span><span class="sec-chevron">▾</span>`;

  if (s?.phase === 'error') return `
    <span style="color:var(--red);font-size:.78rem;flex-shrink:0">⚠</span>
    <span class="sec-label">Upload</span>
    <span class="sec-meta">· ${esc(s.error || 'unknown error')}</span>
    <span class="sec-spacer"></span>
    <span style="font-size:.72rem;color:var(--red);font-weight:500;flex-shrink:0">Try again ↑</span>
    <span class="sec-chevron">▾</span>`;

  // ── Done ──────────────────────────────────────────────────────
  if (s?.phase === 'done') {
    const total = s.report?.clean?.length || s.total || 0;
    if (diff?.auto_pass) {
      const dateStr = diff.lastValidated
        ? `validated ${new Date(diff.lastValidated).toLocaleDateString('en-US',{month:'short',day:'numeric',year:'numeric'})}`
        : '';
      return `
        <span style="font-size:.78rem;color:var(--green);flex-shrink:0">✓</span>
        <span class="sec-label">Upload</span>
        <span class="sec-meta">· ${total} providers · no changes${dateStr ? ' · ' + dateStr : ''}</span>
        <span class="sec-spacer"></span>
        <span style="font-size:.72rem;color:var(--text-3);font-weight:500;flex-shrink:0;cursor:pointer">Upload new ↑</span>
        <span class="sec-chevron">▾</span>`;
    }
    if (diff) {
      const changes = [diff.new > 0 && `${diff.new} new`, diff.changed > 0 && `${diff.changed} changed`, diff.removed > 0 && `${diff.removed} removed`].filter(Boolean);
      return `
        <span style="font-size:.78rem;color:var(--mobius-logo-grey);flex-shrink:0">∞</span>
        <span class="sec-label">Upload</span>
        <span class="sec-meta">· ${total} providers · ${changes.join(', ')}</span>
        <span class="sec-spacer"></span>
        <span style="font-size:.72rem;color:var(--text-3);font-weight:500;flex-shrink:0;cursor:pointer">Upload new ↑</span>
        <span class="sec-chevron">▾</span>`;
    }
    return `
      <span style="font-size:.78rem;color:var(--green);flex-shrink:0">✓</span>
      <span class="sec-label">Upload</span>
      <span class="sec-meta">· ${total} providers · comparing with last run…</span>
      <span class="sec-spacer"></span>
      <span style="font-size:.72rem;color:var(--text-3);font-weight:500;flex-shrink:0;cursor:pointer">Upload new ↑</span>
      <span class="sec-chevron">▾</span>`;
  }

  // ── No file yet ────────────────────────────────────────────────
  return `
    <span class="sec-label">Upload</span>
    <span class="sec-meta">· no roster loaded</span>
    <span class="sec-spacer"></span>
    <span style="font-size:.72rem;color:var(--indigo);font-weight:500;flex-shrink:0">Upload ↑</span>
    <span class="sec-chevron">▾</span>`;
}

function _updateUploadSummary() {
  const el = document.getElementById('uploadSectionSummary');
  if (el) el.innerHTML = _buildUploadSummaryHtml();
  // Also refresh emissions count/log
  const emEl = document.getElementById('rosterEmissionsSection');
  if (emEl) emEl.innerHTML = buildRosterEmissionsHtml();
  const logEl = document.getElementById('rosterEmissionsLog');
  if (logEl) _renderEmissionsInto(logEl);
}

function buildRosterFileZoneHtml() {
  const s = window._rosterUploadState;
  if (s && s.fileName) {
    const done_ = s.done || 0, total_ = s.total || 0;
    const sub = s.phase === 'uploading' ? 'uploading…'
              : s.phase === 'parsing'   ? (total_ ? `validating ${done_} / ${total_}…` : 'validating…')
              : s.phase === 'cleaning'  ? 'AI reviewing…'
              : s.phase === 'error'     ? '⚠ error'
              : 'uploaded';
    return `<div class="upload-file-info">
      <span>📄</span>
      <span class="uf-name">${esc(s.fileName)}</span>
      <span style="font-size:.72rem;color:var(--text-3)">${sub}</span>
      ${s.phase === 'done' || s.phase === 'error' ? `
        <span class="uf-change" onclick="triggerRosterUpload()" title="Upload a new roster — replaces source of truth for this org">Upload new</span>
        <span style="color:var(--border)">·</span>
        <span class="uf-change" onclick="toggleRosterHistory()" title="View previous uploads">History ▾</span>
      ` : ''}
      <input type="file" id="rosterFileInput" accept=".csv,.xlsx,.xls" style="display:none" onchange="handleRosterFile(this.files[0])">
    </div>
    <div id="rosterHistoryPanel" style="display:none;margin-top:.35rem;padding:.4rem .6rem;background:var(--grey-bg);border:1px solid var(--border);border-radius:6px;font-size:.72rem;color:var(--text-3)">
      Loading upload history…
    </div>`;
  }
  return `<div class="upload-zone" onclick="triggerRosterUpload()"
      ondragover="event.preventDefault();this.classList.add('drag-over')"
      ondragleave="this.classList.remove('drag-over')"
      ondrop="handleRosterDrop(event)">
    <div class="upload-zone-icon">🗂</div>
    <div class="upload-zone-label">Upload a roster file</div>
    <div class="upload-zone-sub">CSV or Excel · provider name, NPI, specialty, location</div>
    <input type="file" id="rosterFileInput" accept=".csv,.xlsx,.xls" style="display:none" onchange="handleRosterFile(this.files[0])">
  </div>`;
}

function buildRosterProgressHtml() {
  const s = window._rosterUploadState;
  if (!s) return '';
  if (s.phase === 'uploading') {
    return `<div style="font-size:.8125rem;color:var(--text-3);padding:.5rem 0"><span class="spinner"></span> Uploading <strong>${esc(s.fileName)}</strong>…</div>`;
  }
  if (s.phase === 'parsing') {
    const total = s.total || 0, done = s.done || 0;
    const pct = total ? Math.round((done / total) * 100) : 0;
    const current = s.current_provider || '';
    const log = s.providerLog || [];
    const logHtml = log.length ? `
      <div style="margin-top:.625rem;border:1px solid var(--border);border-radius:7px;overflow:hidden">
        <div style="padding:.3rem .625rem;background:var(--grey-bg);border-bottom:1px solid var(--border);font-size:.65rem;font-weight:700;text-transform:uppercase;letter-spacing:.05em;color:var(--text-3)">Validation log</div>
        <div style="max-height:180px;overflow-y:auto;padding:.3rem .2rem" id="rosterStreamLog">
          ${log.slice(-30).map(e => {
            const icon = e.status === 'validated' ? '<span style="color:var(--green);font-weight:700">✓</span>'
                       : e.status === 'invalid'   ? '<span style="color:var(--red);font-weight:700">✗</span>'
                       : '<span style="color:var(--amber,#d97706);font-weight:700">~</span>';
            return `<div style="display:flex;align-items:center;gap:.4rem;padding:.2rem .5rem;font-size:.72rem;border-bottom:1px solid var(--border)">
              ${icon}
              <span style="flex:1;color:var(--text)">${esc(e.name || '')}</span>
              ${e.npi ? `<span style="font-family:monospace;font-size:.67rem;color:var(--text-3)">${esc(e.npi)}</span>` : ''}
            </div>`;
          }).join('')}
        </div>
      </div>` : '';
    return `<div style="padding:.5rem 0">
      <div style="display:flex;align-items:center;gap:.5rem;margin-bottom:.35rem">
        <span class="spinner"></span>
        <span style="font-size:.8125rem;color:var(--text-2);font-weight:600">${current ? `Validating ${esc(current)}…` : 'Starting validation…'}</span>
        ${total ? `<span style="font-size:.72rem;color:var(--text-3);margin-left:auto">${done} / ${total}</span>` : ''}
      </div>
      ${total ? `<div style="height:5px;border-radius:3px;background:var(--border);overflow:hidden"><div style="height:5px;border-radius:3px;background:var(--indigo);width:${pct}%;transition:width .4s ease"></div></div>` : ''}
      ${logHtml}
    </div>`;
  }
  if (s.phase === 'cleaning') {
    return `<div style="display:flex;align-items:center;gap:.5rem;padding:.5rem 0">
      <span class="spinner"></span>
      <span style="font-size:.8125rem;color:var(--text-2)">AI reviewing ${s.total || ''} rows — removing junk entries…</span>
    </div>`;
  }
  if (s.phase === 'error') {
    return `<p style="color:var(--red);font-size:.8125rem;padding:.5rem 0">⚠ ${esc(s.error || 'Unknown error')}</p>`;
  }
  if (s.phase === 'done') {
    // Upload complete — show compact "upload done" row + "upload new" option
    const clean   = s.report?.clean?.length    || s.total || 0;
    const excl    = s.report?.excluded?.length || 0;
    return `<div style="display:flex;align-items:center;gap:.6rem;padding:.25rem 0;font-size:.72rem;color:var(--text-3)">
      <span style="color:var(--green);font-weight:700">✓</span>
      <span>${clean} providers loaded${excl > 0 ? ` · ${excl} excluded` : ''}</span>
    </div>`;
  }
  return '';
}

function buildRosterReportHtml(report, fileName) {
  const clean    = report.clean    || [];
  const excluded = report.excluded || [];
  const summary  = report.summary  || {};
  const total    = summary.total_providers || (clean.length + excluded.length);
  const valid    = summary.validated_count || 0;
  const review   = summary.needs_review_count || 0;

  const npiMissing = clean.filter(p => !p.npi_roster && !p.npi_validated).length;
  const npiHas     = clean.length - npiMissing;

  const statItems = [
    { lbl: 'Clean rows', val: clean.length, cls: 'green' },
    ...(npiHas     ? [{ lbl: 'Has NPI',      val: npiHas,     cls: 'indigo' }] : []),
    ...(npiMissing ? [{ lbl: 'Needs NPI',    val: npiMissing, cls: 'amber'  }] : []),
    ...(review     ? [{ lbl: 'Needs review', val: review,     cls: 'amber'  }] : []),
  ];
  let html = `<div class="src-section-head" style="margin-top:.75rem;display:flex;align-items:center;justify-content:space-between">
    <span>${esc(fileName || 'Roster')} — ${clean.length} providers</span>
    <span id="rosterSaveStatus" style="font-size:.72rem;transition:color .2s"></span>
  </div>
  ${statRow(statItems)}`;

  if (excluded.length) {
    html += `<details style="margin-bottom:.625rem">
      <summary style="font-size:.78rem;font-weight:600;color:var(--text-3);cursor:pointer">
        🚫 Auto-excluded ${excluded.length} rows (junk / parse errors)
      </summary>
      <div style="margin-top:.375rem">
      ${excluded.slice(0, 30).map(p => `<div style="font-size:.75rem;padding:.2rem .5rem;color:var(--text-3)">
        ${esc(p.provider_name || p.raw_name || '(blank)')}
        ${p.exclude_reason ? `<span style="color:var(--amber);margin-left:.5rem">— ${esc(p.exclude_reason)}</span>` : ''}
        <button style="margin-left:.5rem;font-size:.68rem;color:var(--indigo);background:none;border:none;cursor:pointer" onclick="restoreRosterRow('${esc(p.id || p.provider_name || '')}')">Restore</button>
      </div>`).join('')}
      ${excluded.length > 30 ? `<div style="font-size:.72rem;color:var(--text-3);padding:.25rem .5rem">…and ${excluded.length - 30} more</div>` : ''}
      </div>
    </details>`;
  }

  html += buildRosterProvTable(clean);
  return html;
}

async function handleRosterFile(file) {
  if (!file) return;
  const orgName = (lastRun && lastRun.org_name) || '';
  if (!orgName) {
    alert('No organization loaded — start a pipeline run first, then upload the roster from the Roster tab.');
    return;
  }

  window._rosterEmissions = [];
  window._lastRosterDiffSummary = null;
  window._rosterUploadState = { phase: 'uploading', fileName: file.name };
  feEmit(`Roster file selected: ${file.name} (${(file.size/1024).toFixed(0)} KB)`);
  _emitRosterLog('info', `File selected: ${file.name}`, `${(file.size/1024).toFixed(0)} KB`);
  // Ensure we're on the Roster tab
  if (window._activeProvTab !== 'roster') switchProvTab('roster');

  // Trigger a re-render by touching the zone directly (poll may not fire immediately)
  const zone = document.getElementById('rosterFileZone');
  if (zone) zone.innerHTML = buildRosterFileZoneHtml();
  // Ensure progress container exists
  const panel = document.getElementById('provTab-roster');
  if (panel && !document.getElementById('rosterParseProgress')) {
    const prog = document.createElement('div');
    prog.id = 'rosterParseProgress';
    panel.appendChild(prog);
  }
  const prog = document.getElementById('rosterParseProgress');
  if (prog) prog.innerHTML = buildRosterProgressHtml();

  const fd = new FormData();
  fd.append('file', file);
  fd.append('org_name', orgName);
  if (_chatThreadId) fd.append('thread_id', _chatThreadId);
  if (runId) fd.append('run_id', runId);   // lets server look up Step-2 locations

  let uploadId = null;
  feEmit('Uploading roster to server…');
  _emitRosterLog('info', 'Uploading roster to server…');
  try {
    const resp = await fetch('/chat/roster-upload', { method: 'POST', body: fd });
    if (!resp.ok) {
      const errText = await resp.text().catch(() => resp.statusText);
      throw new Error(`${resp.status}: ${errText}`);
    }
    const json = await resp.json();
    uploadId = json.reconciliation_upload_id || json.upload_id || null;
    const rowHint = json.row_count ? ` · ${json.row_count} rows` : '';
    feEmit(`✓ Roster received by server${rowHint}`, 'ok');
    _emitRosterLog('success', 'File received by server', uploadId ? `upload_id: ${uploadId.slice(0,8)}…` : '');
  } catch(e) {
    feEmit(`Roster upload failed — ${String(e)}`, 'error');
    _emitRosterLog('error', `Upload failed: ${String(e)}`);
    _setRosterState({ phase: 'error', error: String(e) });
    return;
  }

  if (!uploadId) {
    feEmit('Server did not return a reconciliation ID', 'error');
    _emitRosterLog('error', 'Server did not return a reconciliation ID');
    _setRosterState({ phase: 'error', error: 'No reconciliation ID returned from server.' });
    return;
  }

  feEmit('Starting NPPES NPI validation…');
  _emitRosterLog('info', 'Starting NPI validation…');
  _setRosterState({ phase: 'parsing', uploadId });

  // Stream validation progress via SSE (TurboTax-style).
  // Falls back to a one-time status poll if the browser closes the stream early.
  _streamRosterValidation(uploadId, file.name);
}

function _streamRosterValidation(uploadId, fileName) {
  // Guard: close any prior stream for this upload
  if (window._rosterSse) { try { window._rosterSse.close(); } catch(_) {} window._rosterSse = null; }
  if (window._rosterPreloadTimer) { clearInterval(window._rosterPreloadTimer); window._rosterPreloadTimer = null; }

  const sse = new EventSource(`/chat/roster-reconcile/${uploadId}/progress`);
  window._rosterSse = sse;

  // 5-minute heartbeat — silently close if truly dead, but don't error the user
  let _hbTimer = setTimeout(() => {
    sse.close(); window._rosterSse = null;
    // Fallback: try status poll before giving up
    _fallbackPollStatus(uploadId, fileName, { silent: true });
  }, 300_000);
  const _resetHb = () => { clearTimeout(_hbTimer); _hbTimer = setTimeout(() => {
    sse.close(); window._rosterSse = null;
    _fallbackPollStatus(uploadId, fileName, { silent: true });
  }, 300_000); };

  // ── Background report pre-loader ────────────────────────────────────────────
  // Poll the report endpoint starting immediately, every 3 s.
  // Providers become visible after parsing commits (~5-15 s for large files).
  // As soon as we have ≥1 provider row, show the table immediately.
  let _preloadShown = false;
  const _tryPreload = async () => {
    if (_preloadShown) return;
    const s = window._rosterUploadState || {};
    if (s.phase === 'done' && !s._streaming) return; // already finalized by loadAndCleanRoster
    try {
      // ?quick=true skips validation_history → 2 DB round trips instead of 3 (~2x faster)
      const rr = await fetch(`/chat/roster-reconcile/${uploadId}/report?quick=true`);
      if (!rr.ok) return; // 404 = reconcile not yet started; keep retrying
      const raw = await rr.json();
      const providers = raw.providers || [];
      if (providers.length === 0) return; // parsed but empty; keep retrying
      _preloadShown = true;
      clearInterval(window._rosterPreloadTimer);
      window._rosterPreloadTimer = null;
      // Show table immediately in streaming mode (LLM clean deferred to 'complete')
      _showStreamingRoster(uploadId, fileName, providers, raw.report_summary || {});
    } catch(e) { console.warn('[preload] error:', e); }
  };
  // Fire immediately, then every 3 s (catches early commits fast)
  _tryPreload();
  window._rosterPreloadTimer = setInterval(_tryPreload, 3000);

  // ── SSE event handlers ───────────────────────────────────────────────────────

  let _lastProgressEmit = 0;

  sse.addEventListener('progress', (e) => {
    _resetHb();
    try {
      const d = JSON.parse(e.data);
      const s = window._rosterUploadState || {};
      // Emit a progress event every 10 providers (avoid flooding)
      const processed = d.processed || 0;
      if (processed > 0 && processed - _lastProgressEmit >= 10) {
        _lastProgressEmit = processed;
        const total = d.total || '?';
        feEmit(`Validating providers… ${processed} / ${total}`);
        _emitRosterLog('progress', `Validating providers… ${processed} / ${total}`,
          d.current_provider ? `current: ${d.current_provider}` : null);
      }
      if (s.phase === 'done') {
        _updateStreamingBanner(d.processed || 0, d.total || 0, d.current_provider || '');
      } else {
        const log = s.providerLog || [];
        _setRosterState({
          total: d.total || s.total || 0,
          done:  d.processed || s.done || 0,
          current_provider: d.current_provider || '',
          providerLog: log,
        });
        _updateUploadSummary();
        const logEl = document.getElementById('rosterStreamLog');
        if (logEl) logEl.scrollTop = logEl.scrollHeight;
      }
    } catch(_) {}
  });

  sse.addEventListener('provider_done', (e) => {
    _resetHb();
    try {
      const d = JSON.parse(e.data);
      const s = window._rosterUploadState || {};

      // Emit warn for flagged providers into process log
      if (d.status === 'invalid' || d.status === 'unmatched') {
        _emitRosterLog('warn', `${d.name || 'Unknown'} — no NPI match found`,
          d.npi ? `NPI: ${d.npi}` : null);
      } else if (d.status === 'error') {
        _emitRosterLog('error', `Error validating ${d.name || 'provider'}`, d.error || null);
      }

      if (s.phase === 'done') {
        _liveUpdateProviderRow(d);
      } else {
        const log = s.providerLog || [];
        const ei = log.findIndex(r => r.name === d.name);
        if (ei >= 0) log.splice(ei, 1);
        log.push({ name: d.name, status: d.status, npi: d.npi || '' });
        window._rosterUploadState = Object.assign(s, { providerLog: log });
        const logEl = document.getElementById('rosterStreamLog');
        if (logEl) {
          const icon = d.status === 'validated' ? '<span style="color:var(--green);font-weight:700">✓</span>'
                     : d.status === 'invalid'   ? '<span style="color:var(--red);font-weight:700">✗</span>'
                     : '<span style="color:var(--amber,#d97706);font-weight:700">~</span>';
          const row = document.createElement('div');
          row.style.cssText = 'display:flex;align-items:center;gap:.4rem;padding:.2rem .5rem;font-size:.72rem;border-bottom:1px solid var(--border)';
          row.innerHTML = `${icon}<span style="flex:1;color:var(--text)">${esc(d.name||'')}</span>${d.npi?`<span style="font-family:monospace;font-size:.67rem;color:var(--text-3)">${esc(d.npi)}</span>`:''}`;
          logEl.appendChild(row);
          while (logEl.children.length > 60) logEl.removeChild(logEl.firstChild);
          logEl.scrollTop = logEl.scrollHeight;
        } else {
          const prog = document.getElementById('rosterParseProgress');
          if (prog) prog.innerHTML = buildRosterProgressHtml();
        }
      }
    } catch(_) {}
  });

  sse.addEventListener('complete', async (e) => {
    clearTimeout(_hbTimer);
    clearInterval(window._rosterPreloadTimer); window._rosterPreloadTimer = null;
    sse.close(); window._rosterSse = null;
    feEmit('✓ NPI validation complete — running AI review…');
    _emitRosterLog('success', 'NPI validation stream complete — running AI review…');
    _setStreamingBannerComplete();
    await loadAndCleanRoster(uploadId, fileName, true); // fresh upload — always re-run LLM
  });

  sse.addEventListener('error', (e) => {
    if (sse.readyState === EventSource.CLOSED) {
      clearTimeout(_hbTimer);
      clearInterval(window._rosterPreloadTimer); window._rosterPreloadTimer = null;
      window._rosterSse = null;
      const s = window._rosterUploadState || {};
      if (s.phase === 'done' && !s._streaming) return;
      feEmit('Validation stream closed — switching to poll…', 'warn');
      _emitRosterLog('warn', 'Validation stream closed early — falling back to poll…');
      _fallbackPollStatus(uploadId, fileName, { silent: false });
    }
  });
}

// Show the provider table immediately with current (possibly unvalidated) data.
// Rows in 'processing' status render a spinner. Validation updates arrive via _liveUpdateProviderRow.
function _showStreamingRoster(uploadId, fileName, providers, summary) {
  const parseErrors = providers.filter(p => p.status === 'parse_error');
  const clean = providers.filter(p => p.status !== 'parse_error');

  // Build a minimal report shape that buildRosterReportHtml / _buildReconSection3Html can consume
  const report = {
    clean:    clean,
    excluded: parseErrors.map(p => ({ ...p, exclude_reason: p.parse_notes || 'parse error' })),
    summary,
  };

  window._rosterProviders = clean;
  // Mark as streaming so the banner knows to show progress
  _setRosterState({ phase: 'done', _streaming: true, report, uploadId, fileName });
  setTimeout(_syncRosterToAllSources, 100);
}

// Update the status badge of a single provider row already in the table (in-place, no full re-render).
function _liveUpdateProviderRow(d) {
  // Update _rosterProviders so any future re-render has fresh data
  const providers = window._rosterProviders || [];
  const p = providers.find(pr => pr.provider_name === d.name);
  if (p) {
    p.status = d.status;
    if (d.npi && !p.npi_uploaded) p.npi_uploaded = d.npi;
    if (d.status === 'validated' && d.npi) {
      p.latest_validation = p.latest_validation || {};
      p.latest_validation.npi_validated = d.npi;
      p.latest_validation.match_confidence = p.latest_validation.match_confidence || 70;
    }
  }

  // Patch the badge cell in-place across both table instances (reconSection3 + roster tab)
  const clean = window._rosterUploadState?.report?.clean || providers;
  const idx = clean.findIndex(pr => pr.provider_name === d.name);
  if (idx < 0) return;

  const statusIcon = d.status === 'validated'    ? '<span class="recon-sig-badge confirmed">✓ confirmed</span>'
                   : d.status === 'invalid'      ? '<span class="recon-sig-badge no-match">✗ invalid</span>'
                   : d.status === 'needs_review' ? '<span class="recon-sig-badge mismatch">review</span>'
                   :                               `<span class="recon-sig-badge" style="color:var(--text-3);border-color:var(--border)"><span class="spinner" style="width:10px;height:10px;border-width:1.5px"></span></span>`;

  // recon-row-{idx} (reconciliation grid)
  const reconRow = document.getElementById(`recon-row-${idx}`);
  if (reconRow) {
    const badgeCell = reconRow.querySelector('td:nth-child(4)');
    if (badgeCell) badgeCell.innerHTML = statusIcon;
    const npiCell = reconRow.querySelector('td:nth-child(3)');
    if (npiCell && d.npi) npiCell.textContent = d.npi;
  }

  // rr-{idx} (roster tab row — live update NPI chip if visible)
  const rosterRow = document.getElementById(`rr-${idx}`);
  if (rosterRow) {
    const chip = rosterRow.querySelector('.npi-match-chip, .npi-status-chip');
    if (chip) chip.outerHTML = statusIcon;
  }

  // Refresh score bar counts
  _refreshNppesSection();
}

// Update the streaming-in-progress banner at the top of the reconciliation grid.
function _updateStreamingBanner(done, total, currentProvider) {
  const el = document.getElementById('streamingBanner');
  if (!el) return;
  const pct = total ? Math.round(done * 100 / total) : 0;
  el.innerHTML = `<span class="spinner" style="width:12px;height:12px;border-width:1.5px"></span>
    <span style="flex:1">Validating${currentProvider ? ` — ${esc(currentProvider)}` : '…'}</span>
    <span style="font-size:.7rem;color:var(--text-3)">${done}${total ? ' / ' + total : ''} &nbsp; ${pct}%</span>
    <div style="position:absolute;bottom:0;left:0;height:2px;background:var(--indigo);border-radius:1px;width:${pct}%;transition:width .4s ease"></div>`;
}

function _setStreamingBannerComplete() {
  const el = document.getElementById('streamingBanner');
  if (el) el.innerHTML = '<span style="color:var(--green);font-weight:700">✓</span> <span>Validation complete — running AI review…</span>';
  const s = window._rosterUploadState || {};
  if (s) s._streaming = false;
}

async function _fallbackPollStatus(uploadId, fileName, { silent = false } = {}) {
  try {
    const sr = await fetch(`/chat/roster-reconcile/${uploadId}/status`);
    const sd = await sr.json();
    if (sd.status === 'completed') {
      feEmit('✓ Validation complete — loading roster…');
      _setStreamingBannerComplete();
      await loadAndCleanRoster(uploadId, fileName, true); // fresh upload — always re-run LLM
    } else if (sd.status === 'error') {
      feEmit(`Roster validation error — ${sd.error || 'unknown'}`, 'error');
      if (!silent) _setRosterState({ phase: 'error', error: sd.error || 'Validation failed' });
    } else {
      feEmit('Validation still running — reconnecting…');
      setTimeout(() => _streamRosterValidation(uploadId, fileName), 3000);
    }
  } catch(e) {
    setTimeout(() => _streamRosterValidation(uploadId, fileName), 4000);
  }
}

// Auto-load the latest roster for the current org if none is in session.
// Called once when Step 4 is rendered with _rosterUploadState null.
async function _autoLoadRosterIfNeeded() {
  if (window._rosterUploadState) { console.log('[autoload] skip — state already set:', window._rosterUploadState.phase); return; }
  if (_autoLoadRosterAttempted) { console.log('[autoload] skip — already attempted'); return; }
  const orgName = window.lastRun?.org_name;
  if (!orgName) { console.log('[autoload] skip — no orgName in lastRun'); return; }
  _autoLoadRosterAttempted = true;

  const zone = document.getElementById('rosterFileZone');
  const savedUploadId = window.lastRun?.orchestrator_state?.step3_roster_upload_id || '';
  console.log('[autoload] start — org:', orgName, 'savedUploadId:', savedUploadId || '(none)');

  // Fast path: the run state already has step3_roster_upload_id
  if (savedUploadId) {
    if (zone) zone.innerHTML = `<div style="padding:.75rem;font-size:.8rem;color:var(--text-3);display:flex;align-items:center;gap:.5rem"><span class="spinner"></span> Loading previous roster…</div>`;
    try {
      console.log('[autoload] fast path — checking report…');
      window._rosterEmissions = [];
      feEmit(`Auto-loading previous roster (${savedUploadId.slice(0,8)}…)`);
      _emitRosterLog('info', 'Auto-loading previous roster…', `upload_id: ${savedUploadId.slice(0,8)}…`);
      const checkResp = await fetch(`/chat/roster-reconcile/${savedUploadId}/report?quick=true`);
      console.log('[autoload] fast path report status:', checkResp.status);
      if (checkResp.ok) {
        _setRosterState({ phase: 'parsing', uploadId: savedUploadId, fileName: 'previous roster' });
        await loadAndCleanRoster(savedUploadId, 'previous roster');
        console.log('[autoload] fast path done');
        return;
      }
      console.warn('[autoload] fast path report not ok — falling to slow path');
    } catch(err) {
      console.warn('[autoload] fast path error:', err);
    }
  }

  // Slow path: ask the skill server for the latest upload for this org
  console.log('[autoload] slow path — querying latest-for-org…');
  if (zone) zone.innerHTML = `<div style="padding:.75rem;font-size:.8rem;color:var(--text-3);display:flex;align-items:center;gap:.5rem"><span class="spinner"></span> Checking for existing roster…</div>`;
  try {
    const resp = await fetch(`/chat/roster-reconcile/latest-for-org?org_name=${encodeURIComponent(orgName)}`);
    console.log('[autoload] latest-for-org status:', resp.status);
    if (!resp.ok) {
      console.warn('[autoload] latest-for-org not ok');
      if (zone) zone.innerHTML = buildRosterFileZoneHtml();
      return;
    }
    const meta = await resp.json();
    const uploadId = meta.upload_id;
    console.log('[autoload] got uploadId from org lookup:', uploadId);
    if (!uploadId) { if (zone) zone.innerHTML = buildRosterFileZoneHtml(); return; }

    const checkResp = await fetch(`/chat/roster-reconcile/${uploadId}/report?quick=true`);
    console.log('[autoload] slow path report status:', checkResp.status);
    if (!checkResp.ok) {
      if (zone) zone.innerHTML = buildRosterFileZoneHtml();
      return;
    }

    _setRosterState({ phase: 'parsing', uploadId, fileName: meta.filename || 'previous roster' });

    const curRunId = window._lastRunId || window.lastRun?.run_id;
    if (curRunId) {
      fetch(`/chat/credentialing-runs/${curRunId}/seed-roster`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ roster_upload_id: uploadId }),
      }).catch(() => {});
    }

    await loadAndCleanRoster(uploadId, meta.filename || 'roster', true); // new run — always re-run LLM
    console.log('[autoload] slow path done');
  } catch (e) {
    console.error('[autoload] slow path error:', e);
    if (zone) zone.innerHTML = buildRosterFileZoneHtml();
  }
}

async function loadAndCleanRoster(uploadId, fileName, forceRefresh = false) {
  console.log('[loadAndClean] start', uploadId, fileName, forceRefresh ? '(force-fresh)' : '(cache-ok)');
  feEmit('Loading roster report…');
  try {
    const rr = await fetch(`/chat/roster-reconcile/${uploadId}/report?quick=true`);
    console.log('[loadAndClean] report status:', rr.status);
    if (!rr.ok) throw new Error(`Report fetch failed (${rr.status})`);
    const raw = await rr.json();

    const providers = raw.providers || [];
    const summary   = raw.report_summary || {};
    feEmit(`✓ ${providers.length} providers parsed from roster`);
    _emitRosterLog('info', `Roster parsed — ${providers.length} providers found`);
    _setRosterState({ phase: 'cleaning', total: providers.length });

    // LLM clean pass — use cache only when explicitly allowed (page reload on existing run).
    // forceRefresh=true: new upload or new run → always re-run LLM, never serve stale cache.
    // forceRefresh=false: page reload of a sealed step → serve from cache for instant load.
    let report;
    let _fromCache = false;
    if (!forceRefresh) {
      try {
        // Try the GET cache endpoint first — instant DB read, no LLM cost
        const cacheResp = await fetch(`/chat/roster-reconcile/${uploadId}/llm-clean-cache`);
        if (cacheResp.ok) {
          report = await cacheResp.json();
          _fromCache = true;
          const excluded = (report.excluded || []).length;
          const clean    = (report.clean    || []).length;
          _emitRosterLog('success', `✓ ${clean} providers loaded from cache`,
            excluded > 0 ? `${excluded} previously excluded` : null);
        }
      } catch(_) { /* cache miss — fall through to LLM */ }
    }

    if (!_fromCache) {
      // New upload, new run, or forced refresh — run LLM and (re-)cache the result
      feEmit(`AI reviewing ${providers.length} rows for junk entries…`);
      _emitRosterLog('ai', `AI reviewing ${providers.length} rows — removing junk entries…`);
      try {
        const _llmCtl = new AbortController();
        const _llmTimeout = setTimeout(() => _llmCtl.abort(), 15000);
        const cr = await fetch(`/chat/roster-reconcile/${uploadId}/llm-clean?force=true`, {
          method: 'POST', signal: _llmCtl.signal
        });
        clearTimeout(_llmTimeout);
        if (cr.ok) {
          report = await cr.json();
          const excluded = (report.excluded || []).length;
          const clean    = (report.clean    || []).length;
          _emitRosterLog('success', `AI review complete — ${clean} valid providers`,
            excluded > 0 ? `${excluded} rows excluded as junk` : null);
        } else {
          _emitRosterLog('warn', `AI review skipped (${cr.status}) — using raw parse`);
          report = buildFallbackCleanReport(providers, summary);
        }
      } catch(e) {
        _emitRosterLog('warn', 'AI review timed out — using raw parse result');
        report = buildFallbackCleanReport(providers, summary);
      }
    }

    report.summary = summary;

    // Restore persisted decisions from DB status so they survive page refresh
    const restoredClean = [];
    const restoredExcluded = report.excluded || [];
    for (const p of (report.clean || [])) {
      if (p.status === 'excluded') {
        restoredExcluded.push({ ...p, exclude_reason: p.parse_notes || 'previously excluded' });
      } else {
        if (p.status === 'validated' || p.status === 'corrected') {
          p._decision = 'validated';
          p._restoredFromDb = true;
        }
        restoredClean.push(p);
      }
    }
    report.clean    = restoredClean;
    report.excluded = restoredExcluded;

    // ── Pre-mark _approvedToTruth BEFORE the workspace renders ──────────────
    // This prevents the workspace from ever flashing all providers then collapsing.
    // We inline-fetch roster truth and mark synchronously against the clean array.
    const orgNameForTruth = window.lastRun?.org_name;
    if (orgNameForTruth) {
      try {
        const truthResp = await fetch(`/chat/roster-truth/${encodeURIComponent(orgNameForTruth)}?limit=500`);
        if (truthResp.ok) {
          const truthData = await truthResp.json();
          window._rosterTruth = truthData.providers || [];
          const promotedIds   = new Set((truthData.providers || []).map(p => p.source_provider_id).filter(Boolean));
          const promotedNpis  = new Set((truthData.providers || []).map(p => p.npi_validated).filter(Boolean));
          // Name-based fallback: normalize to lowercase, strip extra spaces
          const _normName = n => (n || '').toLowerCase().replace(/\s+/g, ' ').trim();
          const promotedNames = new Set((truthData.providers || []).map(p => _normName(p.provider_name)).filter(Boolean));
          let preMarked = 0;
          restoredClean.forEach(p => {
            const idMatch   = promotedIds.has(String(p.id)) || promotedIds.has(p.id);
            const npiMatch  = (p.latest_validation?.npi_validated && promotedNpis.has(p.latest_validation.npi_validated))
                           || (p.npi_uploaded && promotedNpis.has(p.npi_uploaded));
            const nameMatch = promotedNames.has(_normName(p.provider_name));
            if (idMatch || npiMatch || nameMatch) {
              p._approvedToTruth = true;
              p._decision = 'validated';
              preMarked++;
            }
          });
          if (preMarked > 0) {
            _emitRosterLog('info', `${preMarked} providers already in roster — hidden from workspace`);
            console.log('[loadAndClean] pre-marked', preMarked, 'as _approvedToTruth from roster truth');
          }
        }
      } catch(e) {
        console.warn('[loadAndClean] roster truth pre-fetch failed:', e);
      }
    }

    const noNpi = restoredClean.filter(p => !p.npi_uploaded && !p.latest_validation?.npi_validated).length;
    if (noNpi > 0) _emitRosterLog('warn', `${noNpi} providers have no NPI — cannot be validated without one`);
    const wsCount = restoredClean.filter(p => !p._approvedToTruth).length;
    const alreadyInRoster = restoredClean.filter(p => p._approvedToTruth).length;
    if (alreadyInRoster > 0) feEmit(`✓ ${alreadyInRoster} providers already in roster — hidden from workspace`, 'ok');
    feEmit(`✓ Workspace ready — ${wsCount} provider${wsCount !== 1 ? 's' : ''} to review`, 'ok');
    _emitRosterLog('success', `Ready — ${wsCount} provider${wsCount !== 1 ? 's' : ''} in workspace`,
      restoredExcluded.length > 0 ? `${restoredExcluded.length} excluded` : null);

    window._rosterProviders = report.clean || [];
    _setRosterState({ phase: 'done', _streaming: false, report, uploadId });
    console.log('[loadAndClean] done — clean:', report.clean?.length, 'excluded:', report.excluded?.length);
    _updateUploadSummary();
    setTimeout(_syncRosterToAllSources, 100);
  } catch(e) {
    console.error('[loadAndClean] error:', e);
    feEmit(`Failed to load roster: ${String(e)}`, 'error');
    _emitRosterLog('error', `Failed to load roster: ${String(e)}`);
    _setRosterState({ phase: 'error', error: String(e) });
  }
}

function buildFallbackCleanReport(providers, summary) {
  const clean    = providers.filter(p => p.status !== 'parse_error');
  const excluded = providers.filter(p => p.status === 'parse_error').map(p => ({
    ...p, exclude_reason: p.parse_notes || 'parse error'
  }));
  return { clean, excluded, summary };
}

function _rosterProvider(idx) {
  const s = window._rosterUploadState;
  return s && s.report && s.report.clean[idx];
}

function _rerenderRosterReport() {
  const prog = document.getElementById('rosterParseProgress');
  if (prog) prog.innerHTML = buildRosterProgressHtml();
}

function _rerenderRosterRow(idx) {
  const p = _rosterProvider(idx);
  if (!p) return;
  const tr = document.getElementById(`rr-${idx}`);
  if (!tr) { _rerenderRosterReport(); return; }
  const tmp = document.createElement('tbody');
  tmp.innerHTML = buildRosterRow(p, idx);
  // Replace main data row
  tr.outerHTML = tmp.children[0].outerHTML;
  // Replace detail row
  const dr = document.getElementById(`nppes-detail-${idx}`);
  if (dr && tmp.children[1]) dr.outerHTML = tmp.children[1].outerHTML;
  // Reflect decision in All Sources tab + task queue
  _syncRosterToAllSources();
  const p2 = _rosterProvider(idx);
  if (p2) _syncTaskFromRosterAction(idx, p2._decision);
}

function rosterExcludeRow(idx) {
  const s = window._rosterUploadState;
  if (!s || !s.report) return;
  const [row] = s.report.clean.splice(idx, 1);
  if (row) {
    feEmit('Row excluded — ' + (row.provider_name || row.id || 'row ' + idx));
    s.report.excluded.push({ ...row, exclude_reason: 'removed by user' });
    _rosterSaveExclude(row.id);
  }
  _rerenderRosterReport();
  _syncRosterToAllSources();
  _syncTaskFromRosterAction(idx, 'excluded');
}

function rosterValidate(idx) { rosterUseNpi(idx); } // legacy alias → Use this NPI
function rosterClone(idx)    { rosterUseNpi(idx); } // legacy alias → Use this NPI

function rosterReject(idx) {
  const p = _rosterProvider(idx);
  if (!p) return;
  feEmit('NPI match rejected — ' + (p.provider_name || 'provider #' + idx), 'warn');
  p._decision = 'rejected';
  _rerenderRosterRow(idx);
  _refreshReconView();
  _rosterSaveDecision(idx, {
    resolution_reason:  'rejected_by_user',
    correction_notes:   'User rejected auto-matched NPPES record',
    correction_source:  'pipeline_roster_tab',
  });
}

function rosterUndoDecision(idx) {
  const p = _rosterProvider(idx);
  if (!p) return;
  feEmit('Decision undone — ' + (p.provider_name || 'provider #' + idx));
  delete p._decision;
  _rerenderRosterRow(idx);
  _refreshReconView();
  // No backend undo — the latest ValidationResult created by the PATCH still exists as audit history
}

function rosterPickAlternative(idx, npi, name) {
  feEmit('Alternative NPI selected — ' + (name || npi) + (npi ? ' (' + npi + ')' : ''));
  // Close the detail panel, then confirm via the same path as manual entry
  const dr = document.getElementById(`nppes-detail-${idx}`);
  if (dr) dr.style.display = 'none';
  rosterManualConfirmNpi(idx, npi, name, '');
}

// ── Alignment task creator ────────────────────────────────────────────────────


function rosterQuickCreateTask(idx, dim, flag, defaultAction) {
  // Called from inline row chip — creates a task immediately with the default action
  // and shows a brief toast. No form required.
  const p = _rosterProvider(idx);
  const providerName = p ? p.provider_name : `Provider #${idx}`;
  const sev = flag === 'deactivated' ? 'high' : flag === 'mismatch' ? 'medium' : 'low';
  const dimLabel = { name: 'Name', taxonomy: 'Taxonomy', address: 'Location', zip: 'Zip', status: 'NPPES Status' };

  // Check if this task already exists (avoid duplicates from repeated clicks)
  const dupeId = `user-${idx}-${dim}-quick`;
  if (!_reconTasks) _reconTasks = [];
  const existing = _reconTasks.find(t => t.id === dupeId && !t.done);
  if (existing) {
    // Already open — just jump to it in the queue
    _refreshTaskQueueFull();
    const el = document.getElementById(`task-${dupeId}`);
    if (el) el.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    return;
  }

  _reconTasks.push({
    id:           dupeId,
    providerIdx:  idx,
    providerName: providerName,
    type:         'user_created',
    severity:     sev,
    phase:        2,
    source:       'user',
    dimension:    dim,
    text:         defaultAction,
    detail:       `${dimLabel[dim] || dim} alignment — quick task`,
    done:         false,
  });

  _refreshTaskQueueFull();

  // Flash the chip green briefly to confirm
  event?.target?.setAttribute('style',
    (event?.target?.getAttribute('style') || '') +
    ';background:#d1fae5;border-color:#6ee7b7;color:#065f46'
  );
  setTimeout(() => _rerenderRosterRow(idx), 1800);
}

function rosterSubmitAlignTask(idx, dim, flag) {
  // Legacy shim — routes through the unified popover submit
  submitTaskPopover();
}


function rosterEditName(idx) {
  const p = _rosterProvider(idx);
  if (!p) return;
  const updated = prompt('Edit provider name:', p.provider_name || '');
  if (updated !== null && updated.trim()) {
    p.provider_name = updated.trim();
    _rerenderRosterRow(idx);
    _rosterSaveDecision(idx, {
      name_corrected:    updated.trim(),
      resolution_reason: 'name_corrected',
      correction_source: 'pipeline_roster_tab',
    });
  }
}

// "Use this NPI" — accepts the NPPES-matched NPI (replaces both old Validate + Clone)
function rosterUseNpi(idx) {
  const p = _rosterProvider(idx);
  if (!p) return;
  const vr = p.latest_validation || {};
  feEmit('✓ NPI accepted — ' + (p.provider_name || 'provider #' + idx) + (vr.npi_validated ? ' (' + vr.npi_validated + ')' : ''), 'ok');
  // Copy NPPES data into roster fields so the row shows the confirmed NPI
  if (vr.npi_validated)           p.npi_uploaded        = vr.npi_validated;
  if (vr.provider_name_validated) p.provider_name       = vr.provider_name_validated;
  if (vr.specialty_validated)     p.specialty_uploaded  = vr.specialty_validated;
  p._decision = 'validated';
  _rerenderRosterRow(idx);
  _refreshReconView();
  _rosterSaveDecision(idx, {
    npi_corrected:       vr.npi_validated           || undefined,
    name_corrected:      vr.provider_name_validated || undefined,
    specialty_corrected: vr.specialty_validated     || undefined,
    resolution_reason:   'user_confirmed_nppes_npi',
    correction_source:   'pipeline_roster_tab',
  });
}

// Show an inline NPI entry form under the action cell — no prompt().
function rosterEnterNpi(idx) {
  const tr = document.getElementById(`rr-${idx}`);
  if (!tr) return;
  const p = _rosterProvider(idx);
  const existingNpi = (p && p.npi_uploaded) || '';

  // Insert (or replace) a sub-row for the entry form
  const formRowId = `npi-form-row-${idx}`;
  let formRow = document.getElementById(formRowId);
  if (formRow) { formRow.remove(); return; } // toggle off

  formRow = document.createElement('tr');
  formRow.id = formRowId;
  formRow.innerHTML = `<td colspan="4">
    <div id="npi-form-${idx}" style="padding:.4rem .5rem;background:var(--grey-bg);border-radius:6px;border:1px solid var(--indigo-border);margin:.2rem 0">
      <div style="display:flex;align-items:center;gap:.4rem;flex-wrap:wrap">
        <span style="font-size:.75rem;font-weight:600;color:var(--indigo)">Enter NPI</span>
        <input id="npi-input-${idx}" type="text" inputmode="numeric" maxlength="10"
          placeholder="1234567890"
          value="${esc(existingNpi)}"
          style="width:110px;font-family:monospace;font-size:.85rem;padding:.25rem .4rem;border:1px solid var(--border);border-radius:4px;outline:none"
          oninput="this.value=this.value.replace(/\\D/g,'').substring(0,10);document.getElementById('npi-lookup-btn-${idx}').disabled=this.value.length!==10"
          onkeydown="if(event.key==='Enter')rosterLookupNpi(${idx})">
        <button id="npi-lookup-btn-${idx}" class="ra-btn ra-validate" onclick="rosterLookupNpi(${idx})"
          ${existingNpi.length === 10 ? '' : 'disabled'} style="padding:.25rem .6rem">Lookup →</button>
        <button class="link-btn" style="font-size:.72rem;color:var(--text-3)" onclick="document.getElementById('${formRowId}').remove()">cancel</button>
      </div>
      <div id="npi-lookup-result-${idx}" style="margin-top:.35rem"></div>
    </div>
  </td>`;

  // Insert right after the provider row (before the existing detail row)
  const detailRow = document.getElementById(`nppes-detail-${idx}`);
  tr.parentNode.insertBefore(formRow, detailRow || tr.nextSibling);

  // Focus the input
  setTimeout(() => { const el = document.getElementById(`npi-input-${idx}`); if(el) el.focus(); }, 50);
}

async function rosterLookupNpi(idx) {
  const input = document.getElementById(`npi-input-${idx}`);
  const resultDiv = document.getElementById(`npi-lookup-result-${idx}`);
  if (!input || !resultDiv) return;
  const npi = input.value.trim();
  if (!/^\d{10}$/.test(npi)) { resultDiv.innerHTML = `<span style="color:var(--red);font-size:.75rem">NPI must be exactly 10 digits</span>`; return; }

  feEmit(`Looking up NPI ${npi} in NPPES…`);
  resultDiv.innerHTML = `<span class="spinner"></span><span style="font-size:.75rem;color:var(--text-3);margin-left:.3rem">Looking up in NPPES…</span>`;

  try {
    const r = await fetch(`/chat/roster-reconcile/lookup-npi?npi=${encodeURIComponent(npi)}`);
    if (!r.ok) {
      const err = await r.json().catch(() => ({ detail: r.statusText }));
      resultDiv.innerHTML = `<div style="color:var(--red);font-size:.78rem">✗ ${esc(err.detail || 'NPI not found in NPPES')}</div>
        <button class="link-btn" style="font-size:.72rem;color:var(--text-3);margin-top:.25rem" onclick="rosterManualConfirmNpi(${idx},'${esc(npi)}','','')">Use anyway (unverified)</button>`;
      return;
    }
    const info = await r.json();
    feEmit(`✓ NPI ${npi} — ${info.name || 'found'} (${info.specialty || 'no specialty'})`, 'ok');
    const loc  = info.address || '';
    const tax  = info.taxonomy_code ? `${info.taxonomy_code}${info.specialty ? ' — ' + info.specialty : ''}` : (info.specialty || '');

    resultDiv.innerHTML = `
      <div style="background:var(--surface);border:1px solid var(--green-border);border-radius:6px;padding:.4rem .5rem;margin-top:.25rem">
        <div style="display:flex;align-items:center;gap:.5rem;flex-wrap:wrap;margin-bottom:.25rem">
          <span style="font-weight:600;font-size:.8125rem">${esc(info.name || '—')}</span>
          <span style="font-family:monospace;font-size:.75rem;color:var(--text-3)">${esc(info.npi || npi)}</span>
          ${info.status === 'A' ? `<span style="font-size:.68rem;color:var(--green);font-weight:700">Active</span>` : `<span style="font-size:.68rem;color:var(--amber)">${esc(info.status || '')}</span>`}
        </div>
        ${info.specialty ? `<div style="font-size:.75rem;color:var(--text-2)">${esc(info.specialty)}</div>` : ''}
        ${tax && tax !== info.specialty ? `<div style="font-size:.72rem;color:var(--text-3)">${esc(tax)}</div>` : ''}
        ${loc ? `<div style="font-size:.75rem;color:var(--text-2);margin-top:.15rem">📍 ${esc(loc)}</div>` : ''}
        <div style="margin-top:.4rem;display:flex;gap:.4rem">
          <button class="ra-btn ra-validate" onclick="rosterManualConfirmNpi(${idx},'${esc(npi)}','${esc(info.name||'')}','${esc(info.specialty||'')}')">✓ Use this NPI</button>
          <button class="link-btn" style="font-size:.72rem;color:var(--text-3)" onclick="document.getElementById('npi-form-row-${idx}').remove()">cancel</button>
        </div>
      </div>`;
  } catch(e) {
    resultDiv.innerHTML = `<span style="color:var(--red);font-size:.75rem">Lookup failed: ${esc(String(e))}</span>`;
  }
}

function rosterManualConfirmNpi(idx, npi, name, specialty) {
  const p = _rosterProvider(idx);
  if (!p) return;
  feEmit(`NPI confirmed: ${npi}${name ? ' — ' + name : ''}`);
  p.npi_uploaded = npi;
  if (name)     p.provider_name      = name;
  if (specialty) p.specialty_uploaded = specialty;
  // Update latest_validation so the chip shows correctly
  if (!p.latest_validation) p.latest_validation = {};
  p.latest_validation.npi_validated           = npi;
  if (name)     p.latest_validation.provider_name_validated = name;
  if (specialty) p.latest_validation.specialty_validated    = specialty;
  p.latest_validation.match_confidence = 1.0;
  p._decision = 'validated';
  // Remove the form row
  const fr = document.getElementById(`npi-form-row-${idx}`);
  if (fr) fr.remove();
  _rerenderRosterRow(idx);
  _rosterSaveDecision(idx, {
    npi_corrected:       npi,
    name_corrected:      name      || undefined,
    specialty_corrected: specialty || undefined,
    resolution_reason:   'npi_entered_manually_and_confirmed',
    correction_source:   'pipeline_roster_tab',
  });
}

function rosterAddNpi(idx) { rosterEnterNpi(idx); } // legacy alias

function rosterSearchNppes(idx) {
  const p = _rosterProvider(idx);
  if (!p) return;

  feEmit(`Searching NPPES for "${p.provider_name || 'provider'}"…`);
  // Show searching state inline in the row's NPPES cell
  const tr = document.getElementById(`rr-${idx}`);
  const nppesCell = tr && tr.cells[3];
  if (nppesCell) nppesCell.innerHTML = `<span style="font-size:.75rem;color:var(--text-3)"><span class="spinner"></span> searching…</span>`;

  fetch(`/chat/roster-reconcile/search-nppes?name=${encodeURIComponent(p.provider_name || '')}`)
    .then(r => r.json())
    .then(data => {
      if (data.results && data.results.length) {
        const top = data.results[0];
        if (!p.latest_validation) p.latest_validation = {};
        p.latest_validation.npi_validated = top.npi;
        p.latest_validation.provider_name_validated = top.name;
        p.latest_validation.specialty_validated = top.specialty;
        p.latest_validation.match_confidence = top.confidence || 0.5;
        p.latest_validation.validation_details = { ...top, candidates: data.results };
        _rerenderRosterRow(idx);
      } else {
        if (nppesCell) nppesCell.innerHTML = `<span style="font-size:.72rem;color:var(--red)">✗ no match</span>
          <button class="link-btn" style="font-size:.7rem;color:var(--indigo);display:block;margin-top:.2rem" onclick="rosterAddNpi(${idx})">+ add NPI manually</button>`;
      }
    })
    .catch(() => {
      if (nppesCell) nppesCell.innerHTML = `<span style="font-size:.72rem;color:var(--red)">search failed</span>`;
    });
}

function restoreRosterRow(nameOrId) {
  const s = window._rosterUploadState;
  if (!s || !s.report) return;
  const idx = s.report.excluded.findIndex(p => (p.id || p.provider_name) === nameOrId);
  if (idx >= 0) {
    const [row] = s.report.excluded.splice(idx, 1);
    s.report.clean.push(row);
    _setRosterState({});
  }
}

function buildRosterProvTable(providers) {
  if (!providers.length) return `<p class="src-empty">No providers found in file.</p>`;

  const rows = providers.map((p, idx) => buildRosterRow(p, idx)).join('');
  // Count previously-validated (restored from DB) vs new matches vs unmatched
  const prevCount  = providers.filter(p => p._restoredFromDb && p._decision === 'validated').length;
  const matchCount = providers.filter(p => !p._restoredFromDb && (p.latest_validation?.npi_validated)).length;
  const noneCount  = providers.filter(p => !p.latest_validation?.npi_validated).length;
  const legend = [
    prevCount  ? `<span style="font-size:.72rem;color:var(--green)">✓ ${prevCount} previously validated</span>` : '',
    matchCount ? `<span style="font-size:.72rem;color:var(--text-2)">${matchCount} NPPES matched</span>` : '',
    noneCount  ? `<span style="font-size:.72rem;color:var(--text-3)">${noneCount} no match</span>` : '',
  ].filter(Boolean).join('<span style="color:var(--border);margin:0 .4rem">·</span>');

  return `<div id="rosterTableWrap">
    ${legend ? `<div style="margin-bottom:.5rem;display:flex;gap:.25rem;align-items:center">${legend}</div>` : ''}
    <div class="prov-table-wrap">
      <table class="prov-table" id="rosterProvTable">
        <colgroup>
          <col class="col-name"><col class="col-npi"><col class="col-act"><col class="col-icon">
        </colgroup>
        <thead>
          <tr>
            <th class="col-name">Name</th>
            <th class="col-npi">NPPES NPI <span style="font-size:.65rem;font-weight:400;color:var(--text-3)">click to compare →</span></th>
            <th class="col-act">Action</th>
            <th class="col-icon"></th>
          </tr>
        </thead>
        <tbody id="rosterTbody">${rows}</tbody>
      </table>
    </div>
  </div>`;
}

function buildRosterRow(p, idx) {
  const npiRoster  = p.npi_uploaded || '';
  const vr         = p.latest_validation || null;
  const npiReg     = vr ? (vr.npi_validated || '') : '';
  const conf       = vr ? (vr.match_confidence || 0) : 0;
  const confPct    = Math.round(conf * 100);
  const decision   = p._decision || null;
  const prevValid  = p._restoredFromDb && decision === 'validated';

  // ── Alignment risk chips (Phase 2) shown inline on the row ─────
  // Each chip is clickable — directly creates a pre-filled task for that dimension
  const alignInfo = vr?.validation_details?.alignment || {};
  const _riskChip = (dim, flag, label, defaultAction) => {
    // Check if an open task already exists for this chip
    const dupeId   = `user-${idx}-${dim}-quick`;
    const hasTask  = (_reconTasks || []).some(t => t.id === dupeId && !t.done);
    const col = hasTask ? '#065f46'
      : flag === 'deactivated' || flag === 'mismatch' ? '#dc2626' : '#d97706';
    const bg  = hasTask ? '#d1fae5'
      : flag === 'deactivated' || flag === 'mismatch' ? '#fef2f2' : '#fffbeb';
    const brd = hasTask ? '#6ee7b7'
      : flag === 'deactivated' || flag === 'mismatch' ? '#fca5a5' : '#fde68a';
    const chipLabel = hasTask ? `${label} ✓` : `${label} ＋`;
    const title     = hasTask ? 'Task created — click to jump to it in queue' : 'Click to create a task for this issue';
    return `<button onclick="rosterQuickCreateTask(${idx},'${dim}','${flag}',${JSON.stringify(defaultAction)})"
      style="font-size:.63rem;padding:.1rem .35rem;border-radius:20px;border:1px solid ${brd};
             background:${bg};color:${col};font-weight:600;cursor:pointer;white-space:nowrap"
      title="${title}">${chipLabel}</button>`;
  };

  const alignChips = [];
  const aStatus  = alignInfo.status  || {};
  const aName    = alignInfo.name    || {};
  const aTax     = alignInfo.taxonomy|| {};
  const aAddr    = alignInfo.address || {};
  if (aStatus.flag === 'deactivated')
    alignChips.push(_riskChip('status',   'deactivated', '✗ Deactivated',    'Remove from active credentialing immediately'));
  if (aName.flag === 'mismatch')
    alignChips.push(_riskChip('name',     'mismatch',    'Name mismatch',     'Update roster name to match NPPES record'));
  if (aName.flag === 'drift')
    alignChips.push(_riskChip('name',     'drift',       'Name drift',        'Note: minor name difference — acceptable'));
  if (aAddr.flag === 'mismatch')
    alignChips.push(_riskChip('address',  'mismatch',    '📍 Location mismatch',
      aAddr.detail || 'Ask provider to update practice location in NPPES'));
  if (aAddr.flag === 'drift')
    alignChips.push(_riskChip('address',  'drift',       '📍 Unrecognised address',
      aAddr.detail || 'NPPES address not in known org locations — verify'));
  if (aTax.flag === 'mismatch')
    alignChips.push(_riskChip('taxonomy', 'mismatch',    'Taxonomy mismatch', 'Update roster specialty to match NPPES taxonomy'));
  const alignChipsHtml = alignChips.length
    ? `<div style="display:flex;gap:.2rem;flex-wrap:wrap;margin-top:.25rem">${alignChips.join('')}</div>`
    : '';

  // ── NPI chip ──────────────────────────────────────────────────────────────
  let npiCell;
  const credBadge = (() => {
    const creds = Array.isArray(p.credentials) ? p.credentials : [];
    return creds.length ? `<span style="font-size:.65rem;color:var(--text-3);letter-spacing:.02em">${creds.map(c => esc(c)).join(' · ')}</span>` : '';
  })();
  if (prevValid) {
    npiCell = `<div style="display:flex;flex-direction:column;gap:.2rem">
      <span style="font-family:monospace;font-size:.8rem;font-weight:600;color:var(--green)">${esc(npiReg || npiRoster)}</span>
      ${credBadge}
      <div style="display:flex;align-items:center;gap:.3rem">
        <span class="conf-badge conf-prev" style="cursor:default">✓ previously validated</span>
        <button class="nppes-toggle-btn" id="ntb-${idx}" onclick="rosterToggleNppes(${idx})" title="Show / hide NPPES comparison">+</button>
      </div>
    </div>`;
  } else if (!npiReg) {
    npiCell = `<span class="conf-badge conf-none" onclick="rosterSearchNppes(${idx})" style="font-size:.7rem">🔍 search</span>`;
  } else {
    const cls  = confPct >= 80 ? 'conf-high' : confPct >= 50 ? 'conf-med' : 'conf-low';
    const icon = confPct >= 80 ? '✓' : confPct >= 50 ? '~' : '?';
    npiCell = `<div style="display:flex;flex-direction:column;gap:.2rem">
      <span style="font-family:monospace;font-size:.8rem;font-weight:600">${esc(npiReg)}</span>
      ${credBadge}
      <div style="display:flex;align-items:center;gap:.3rem">
        <span class="conf-badge ${cls}" style="cursor:default">${icon} ${confPct}%</span>
        <button class="nppes-toggle-btn" id="ntb-${idx}" onclick="rosterToggleNppes(${idx})" title="Show / hide NPPES comparison">+</button>
      </div>
    </div>`;
  }

  // ── Action buttons ────────────────────────────────────────────────────────
  // "Use this NPI" replaces both Validate + Clone — clearer intent.
  // "Enter NPI" opens an inline lookup form (not a prompt()).
  let actions;
  if (prevValid) {
    actions = `<span style="font-size:.75rem;color:var(--green)">✓ no changes</span>
      <button class="ra-btn ra-delete" style="margin-left:.3rem" onclick="rosterUndoDecision(${idx})" title="Undo">undo</button>
      <button class="link-btn" style="font-size:.72rem;color:var(--text-3);margin-left:.3rem" onclick="rosterEnterNpi(${idx})">enter different NPI</button>`;
  } else if (decision === 'validated') {
    actions = `<span class="ra-btn ra-done">✓ confirmed</span>
      <button class="ra-btn ra-delete" onclick="rosterUndoDecision(${idx})">undo</button>
      <button class="link-btn" style="font-size:.72rem;color:var(--text-3);margin-left:.3rem" onclick="rosterEnterNpi(${idx})">enter different NPI</button>`;
  } else if (decision === 'rejected') {
    actions = `<span class="ra-btn ra-done" style="border-color:var(--amber);background:var(--amber-bg,#fffbeb);color:var(--amber,#d97706)">✗ not a match</span>
      <button class="ra-btn ra-delete" onclick="rosterUndoDecision(${idx})">undo</button>
      <button class="link-btn" style="font-size:.72rem;color:var(--text-3);margin-left:.3rem" onclick="rosterEnterNpi(${idx})">enter NPI manually</button>`;
  } else if (npiReg) {
    // File has same NPI as NPPES → "Confirm"; file missing NPI or has different → "Use this NPI"
    const fileMatchesReg = npiRoster && npiRoster === npiReg;
    actions = `
      <button class="ra-btn ra-validate" onclick="rosterUseNpi(${idx})" title="${fileMatchesReg ? 'Confirm this NPI is correct' : 'Accept NPPES NPI for this provider'}">
        ${fileMatchesReg ? '✓ Confirm' : '✓ Use this NPI'}
      </button>
      <button class="ra-btn ra-reject" onclick="rosterReject(${idx})" title="Wrong provider — flag for manual entry">✗ Not a match</button>
      <button class="link-btn" style="font-size:.72rem;color:var(--text-3);display:block;margin-top:.25rem" onclick="rosterEnterNpi(${idx})">enter different NPI →</button>`;
  } else {
    actions = `<button class="ra-btn ra-validate" onclick="rosterEnterNpi(${idx})" title="Enter NPI manually with auto-lookup">+ Enter NPI</button>`;
  }

  const rosterIssue = (() => {
    if (aStatus.flag === 'deactivated') return 'Provider NPPES record is deactivated';
    if (aName.flag === 'mismatch')      return 'Name mismatch vs NPPES';
    if (aAddr.flag === 'mismatch')      return 'Location not matching approved org sites';
    if (aAddr.flag === 'drift')         return aAddr.detail || 'NPPES address not in known org locations';
    if (aTax.flag === 'mismatch')       return 'Taxonomy mismatch';
    if (!npiReg)                        return 'No NPPES match found';
    return confPct < 60                 ? `Low confidence match (${confPct}%)` : '';
  })();
  const rCtx = JSON.stringify({ stepId:'nppes_alignment', type:'provider', name: p.provider_name||'', npi: npiReg||npiRoster||'', providerIdx: idx, issue: rosterIssue, suggestedText: rosterIssue ? `${p.provider_name||'Provider'} — ${rosterIssue}` : `Review NPPES record for ${p.provider_name||'provider'}` });
  const provNameEsc = esc(p.provider_name || '—');
  return `<tr id="rr-${idx}" data-idx="${idx}" style="${decision === 'rejected' ? 'opacity:.55' : ''}">
    <td class="prov-cell-name-wrap" title="${provNameEsc}">
      <div style="display:flex;align-items:center;gap:.3rem;min-width:0">
        <span class="prov-cell-name" style="font-weight:600;font-size:.8125rem">${provNameEsc}</span>
        <button class="link-btn" style="font-size:.65rem;color:var(--text-3);flex-shrink:0" onclick="rosterEditName(${idx})" title="Edit name">✎</button>
      </div>
      ${p.parse_notes ? `<div style="font-size:.69rem;color:var(--amber);overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(p.parse_notes)}</div>` : ''}
      ${npiRoster ? `<div style="font-size:.69rem;color:var(--text-3);font-family:monospace">file: ${esc(npiRoster)}</div>` : ''}
      ${alignChipsHtml}
    </td>
    <td>${npiCell}</td>
    <td>${actions}</td>
    <td style="white-space:nowrap">
      <button class="row-task-btn" onclick="openTaskPopover(JSON.parse(this.dataset.ctx),event)" data-ctx="${esc(rCtx)}" title="Create task for this provider">
        <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6"><rect x="3" y="2" width="10" height="12" rx="1.5"/><path d="M6 6h4M6 9h4M6 12h2"/></svg>
      </button>
      <button class="link-btn" style="font-size:.72rem;color:var(--text-3);margin-left:.2rem" onclick="rosterExcludeRow(${idx})" title="Remove row">✕</button>
    </td>
  </tr>
  <tr class="nppes-detail-row" id="nppes-detail-${idx}" style="display:none">
    <td colspan="4"><div class="nppes-detail-panel" id="nppes-panel-${idx}"></div></td>
  </tr>`;
}

// Shared detail renderer — populates any panel element with NPPES comparison content.
// Called by both rosterToggleNppes (roster table) and reconToggleDetail (recon table).
function _renderNppesDetail(idx, panelEl) {
  const s = window._rosterUploadState;
  const p = s?.report?.clean?.[idx];
  if (!p) {
    panelEl.innerHTML = `<div style="padding:.5rem;color:var(--text-3);font-size:.78rem">Loading NPPES data…</div>`;
    return;
  }

  const vr       = p.latest_validation || {};
  const det      = vr.validation_details || {};
  const conf     = Math.round((vr.match_confidence || 0) * 100);
  const valType  = vr.validation_type  || '';
  const valStat  = vr.validation_status || '';
  const prevValid = p._restoredFromDb && p._decision === 'validated';

  // ── Derive NPPES source fields ───────────────────────────────
  const topMatch = (det.matches && det.matches[0]) || (det.candidates && det.candidates[0]) || null;
  const src      = (valType === 'npi_lookup') ? det : (topMatch || {});
  const loc      = src.address || [src.city, src.state].filter(Boolean).join(', ');
  const taxonomy = src.taxonomy_code
    ? `${src.taxonomy_code}${src.taxonomy || src.taxonomy_description ? ' — ' + (src.taxonomy || src.taxonomy_description) : ''}`
    : (vr.specialty_validated || '');
  const confColor = conf >= 78 ? 'var(--green)' : conf >= 65 ? '#d97706' : 'var(--red)';
  // Show breakdown from top candidate's score_breakdown when available
  const topBk = (det.score_breakdown) || (det.candidates && det.candidates[0] && det.candidates[0].score_breakdown) || {};
  const scoreDesc = (() => {
    if (conf === 0) return 'NPI lookup confirmed';
    const parts = [];
    if (topBk.name_score !== undefined) parts.push(`name ${Math.round(topBk.name_score*100)}%`);
    if (topBk.location_label === 'zip_match') parts.push('✓ zip match');
    else if (topBk.location_label === 'state_match') parts.push('✓ same state');
    else if (topBk.location_label === 'state_mismatch') parts.push('✗ different state');
    if (topBk.status_label === 'deactivated') parts.push('✗ DEACTIVATED');
    if (!parts.length) return conf >= 78 ? 'Strong match' : conf >= 65 ? 'Review recommended' : 'Weak — manual review required';
    return parts.join(' · ');
  })();
  const simNote = conf >= 78 ? 'Strong' : conf >= 65 ? 'Review recommended' : 'Weak — manual review required';

  // ── Candidate-row helper ─────────────────────────────────────
  function candCard(c, showBreakdown) {
    // Show ZIP+4 in location string if available
    const cZipFull = c.zip9 || c.zip5 || '';
    const cLocBase = c.address || [c.city, c.state].filter(Boolean).join(', ');
    // Replace trailing 5-digit zip with zip+4 if we have it
    const cLoc = cZipFull && c.zip9 && cLocBase
      ? cLocBase.replace(/\b\d{5}(-\d{4})?\b$/, c.zip9)
      : cLocBase;
    const cTax  = c.taxonomy_code ? `${c.taxonomy_code}${c.taxonomy||c.specialty ? ' · '+(c.taxonomy||c.specialty) : ''}` : (c.specialty || '');
    // Use composite score when available, fall back to name similarity
    const scoreRaw = (typeof c.composite_score === 'number') ? c.composite_score
                   : (typeof c.similarity_score === 'number') ? c.similarity_score : null;
    const cConf = scoreRaw !== null ? Math.round(scoreRaw * 100) : null;
    const bk    = c.score_breakdown || {};
    const isDeact = (c.status || '').toUpperCase() === 'D';

    const pill = cConf !== null
      ? `<span style="font-size:.68rem;padding:.15rem .4rem;border-radius:4px;font-weight:700;
            background:${cConf>=80?'var(--green-bg)':cConf>=65?'#fef3c7':'#fff1f2'};
            color:${cConf>=80?'var(--green)':cConf>=65?'#d97706':'#dc2626'}">${cConf}%</span>`
      : '';
    const deactTag = isDeact ? `<span style="font-size:.63rem;font-weight:700;color:var(--red);background:var(--red-bg);border:1px solid var(--red-border);border-radius:3px;padding:1px 4px;margin-left:.2rem">DEACTIVATED</span>` : '';

    // Score breakdown tooltip hint
    let breakdownHtml = '';
    if (showBreakdown && bk.name_score !== undefined) {
      const locLabel = bk.location_label === 'state_match' ? '✓ state' : bk.location_label === 'zip_match' ? '✓ zip' : bk.location_label === 'state_mismatch' ? '✗ state' : '~ location';
      const locCol   = bk.location_label === 'state_match' || bk.location_label === 'zip_match' ? 'var(--green)' : bk.location_label === 'state_mismatch' ? 'var(--red)' : 'var(--text-3)';
      const statLabel = bk.status_label === 'active' ? '✓ active' : bk.status_label === 'deactivated' ? '✗ deactivated' : '~ status unknown';
      const statCol   = bk.status_label === 'active' ? 'var(--green)' : bk.status_label === 'deactivated' ? 'var(--red)' : 'var(--text-3)';
      breakdownHtml = `<div style="font-size:.68rem;color:var(--text-3);margin-top:.2rem;display:flex;gap:.5rem;flex-wrap:wrap">
        <span>name <strong style="color:var(--text-2)">${Math.round(bk.name_score*100)}%</strong></span>
        <span style="color:${locCol}">${locLabel}</span>
        <span style="color:${statCol}">${statLabel}</span>
      </div>`;
    }

    const cCredArr = Array.isArray(c.credentials) ? c.credentials : [];
    const cCredStr = cCredArr.length ? cCredArr.join(' · ') : '';
    // Highlight credentials that match the roster
    const rosterCredNorm = (Array.isArray(p.credentials) ? p.credentials : [])
      .map(x => x.toUpperCase().replace(/[.\-]/g, ''));
    const cCredHtml = cCredArr.length
      ? cCredArr.map(cr => {
          const norm = cr.toUpperCase().replace(/[.\-]/g, '');
          const matched = rosterCredNorm.includes(norm);
          const style = matched
            ? 'background:var(--green-bg);color:var(--green);border:1px solid var(--green-border)'
            : 'background:var(--red-bg);color:var(--red);border:1px solid var(--red-border)';
          return `<span style="font-size:.63rem;font-weight:700;border-radius:3px;padding:1px 5px;${style}">${esc(cr)}</span>`;
        }).join(' ')
      : '';
    return `<div style="padding:.45rem .55rem;border-radius:6px;margin-bottom:.3rem;background:var(--grey-bg);border:1px solid ${isDeact?'#fca5a5':'var(--border)'}">
      <div style="display:flex;align-items:center;gap:.35rem;flex-wrap:wrap">
        <span style="font-weight:600;font-size:.8rem">${esc(c.name || '—')}</span>
        ${deactTag}
        <span style="font-family:monospace;font-size:.72rem;color:var(--text-3)">${esc(c.npi || '—')}</span>
        ${pill}
        <button class="link-btn" style="font-size:.7rem;color:var(--indigo);margin-left:auto" onclick="rosterPickAlternative(${idx},'${esc(c.npi||'')}','${esc(c.name||'')}')">Use this →</button>
      </div>
      ${cCredHtml ? `<div style="margin-top:.2rem;display:flex;gap:.2rem;flex-wrap:wrap">${cCredHtml}</div>` : ''}
      <div style="font-size:.73rem;color:var(--text-2);margin-top:.2rem">📍 ${cLoc ? esc(cLoc) : '<span style="font-style:italic;color:var(--text-3)">Location not in NPPES</span>'}</div>
      ${cTax ? `<div style="font-size:.7rem;color:var(--text-3);margin-top:.1rem">${esc(cTax)}</div>` : ''}
      ${breakdownHtml}
    </div>`;
  }

  // ── Strong / Weak candidate split ────────────────────────────
  // Strong  ≥ 65% composite  OR  top match shown in comparison table
  // Weak    <  65% composite  (collapsed by default)
  const STRONG_THRESHOLD = 0.65;
  const candidates      = det.candidates || [];
  const otherCandidates = candidates.filter(c => c.npi !== vr.npi_validated);

  const strongOthers = otherCandidates.filter(c => (c.composite_score ?? c.similarity_score ?? 0) >= STRONG_THRESHOLD);
  const weakOthers   = otherCandidates.filter(c => (c.composite_score ?? c.similarity_score ?? 0) <  STRONG_THRESHOLD);

  const strongHtml = strongOthers.length ? `
    <div style="margin-top:.5rem">
      <div style="font-size:.68rem;font-weight:700;text-transform:uppercase;letter-spacing:.05em;color:var(--green);margin-bottom:.3rem">Strong matches (${strongOthers.length})</div>
      ${strongOthers.map(c => candCard(c, true)).join('')}
    </div>` : '';

  const weakHtml = weakOthers.length ? `
    <details style="margin-top:.4rem">
      <summary style="cursor:pointer;font-size:.68rem;font-weight:700;color:var(--text-3);text-transform:uppercase;letter-spacing:.05em;user-select:none;list-style:none;display:flex;align-items:center;gap:.3rem">
        <span>▸</span> ${weakOthers.length} weaker match${weakOthers.length > 1 ? 'es' : ''} <span style="font-weight:400;text-transform:none;letter-spacing:0;color:var(--text-3);font-size:.65rem">(different location or low name match)</span>
      </summary>
      <div style="margin-top:.3rem">${weakOthers.map(c => candCard(c, true)).join('')}</div>
    </details>` : '';

  const candidateHtml = (strongHtml || weakHtml) ? `<div style="border-top:1px solid var(--border);margin-top:.625rem;padding-top:.5rem">${strongHtml}${weakHtml}</div>` : '';

  // ── Pull alignment data (two-phase: match + alignment) ──────
  const matchInfo  = det.match     || {};
  const alignInfo  = det.alignment || {};
  const alignSum   = alignInfo.summary || [];   // ["name","taxonomy", ...]

  // Alignment dimension objects — declared here so they're available
  // to alignChipMap and cmpRow helpers defined below
  const aName    = (alignInfo.name       || {});
  const aTax     = (alignInfo.taxonomy   || {});
  const aAddr    = (alignInfo.address    || {});
  const aStatus  = (alignInfo.status     || {});
  const aZip     = (alignInfo.zip        || {});
  const aCred    = (alignInfo.credential || {});

  // ZIP display helpers — use structured fields when available
  const zipRoster5 = aZip.roster_zip5 || (aZip.roster ? aZip.roster.replace(/\D/g,'').slice(0,5) : '');
  const zipRoster9 = aZip.roster_zip9 || '';
  const zipNppes5  = aZip.nppes_zip5  || (src.zip5) || '';
  const zipNppes9  = aZip.nppes_zip9  || (src.zip9) || '';
  const zip5Flag   = aZip.zip5_flag || aZip.flag || 'no_roster_data';
  const zip9Flag   = aZip.zip9_flag || 'no_roster_data';

  // ── No-match panel ───────────────────────────────────────────
  if (valStat === 'fail') {
    const noMatchCands = det.candidates || [];
    panelEl.innerHTML = `
      <div style="color:var(--red);font-size:.8125rem;font-weight:600;margin-bottom:.5rem">✗ No NPPES match found for "${esc(det.search_name || p.provider_name || '')}"</div>
      ${noMatchCands.length ? `
        <details open style="margin-bottom:.5rem">
          <summary style="cursor:pointer;font-size:.72rem;font-weight:700;color:var(--amber,#d97706);text-transform:uppercase;letter-spacing:.04em;list-style:none;display:flex;gap:.4rem;user-select:none">
            <span>▾</span> ${noMatchCands.length} partial candidate${noMatchCands.length > 1 ? 's' : ''} — select or enter manually
          </summary>
          <div style="margin-top:.4rem">${noMatchCands.map(candCard).join('')}</div>
        </details>` : ''}
      <button class="ra-btn ra-clone" onclick="rosterAddNpi(${idx})">+ Enter NPI manually</button>`;
    return;
  }

  // ── Helpers ──────────────────────────────────────────────────
  // "Previously validated" banner
  const prevBanner = prevValid ? `
    <div style="border:1px solid var(--border);border-radius:6px;padding:.35rem .6rem;margin-bottom:.55rem;display:flex;align-items:center;justify-content:space-between;gap:.5rem;flex-wrap:wrap;background:var(--grey-bg)">
      <span style="font-size:.78rem;color:var(--text-2)"><span style="color:var(--green)">✓</span> Previously validated — no re-validation needed unless data changed below</span>
      <button class="link-btn" style="font-size:.72rem" onclick="rosterUndoDecision(${idx});document.getElementById('nppes-detail-${idx}').style.display='none'">Undo</button>
    </div>` : '';

  // Alignment flag renderer
  function alignBadge(flag) {
    if (!flag || flag === 'no_roster_data') return `<span style="color:var(--text-3);font-size:.72rem" title="Not in roster upload">—</span>`;
    if (flag === 'no_nppes_data')   return `<span style="color:var(--text-3);font-size:.72rem" title="Not returned by NPPES">—</span>`;
    if (flag === 'ok')              return `<span style="color:var(--green);font-size:.78rem;font-weight:700" title="Aligned">✓</span>`;
    if (flag === 'drift')           return `<span style="color:var(--amber,#d97706);font-size:.78rem;font-weight:700" title="Minor differences (credentials, middle name, etc.)">~</span>`;
    if (flag === 'deactivated')     return `<span style="color:var(--red);font-size:.78rem;font-weight:700" title="NPI is deactivated in NPPES">✗ Deactivated</span>`;
    return `<span style="color:var(--red);font-size:.78rem;font-weight:700" title="Does not match NPPES record">✗</span>`;
  }

  // Whether a flag is worth acting on (shows task button)
  const actionableFlags = new Set(['drift','mismatch','deactivated']);

  // Comparison table row: label | roster | nppes | align badge | actions
  // Every row gets a manual-flag icon so the user can raise an issue on ANY field,
  // even if the system did not detect a drift there.
  // optional `note` surfaces as a tooltip on the NPPES value cell (e.g. addr_detail).
  function cmpRow(label, uploadVal, npVal, alignFlag, dim, note) {
    const uStyle = !uploadVal ? 'color:var(--text-3);font-style:italic' : '';
    const nStyle = (alignFlag === 'mismatch' || alignFlag === 'deactivated')
      ? 'color:var(--red);font-weight:600' : alignFlag === 'drift' ? 'color:var(--amber,#d97706);font-weight:600' : '';
    const isActionable = dim && actionableFlags.has(alignFlag);
    const isDismissed  = isActionable && (p._dismissedDims || []).includes(dim);

    const dimLabels = { name:'Name', taxonomy:'Specialty/Taxonomy', address:'Location', zip:'ZIP', status:'NPPES Status', credential:'Credential' };
    const dimLabel  = dimLabels[dim] || label;

    // Manual-flag button — always present, lets user raise an issue on any field
    // Pre-fill with the specific field + both values so the task has clear context
    const manualCtx = JSON.stringify({
      stepId: 'nppes_alignment', type: 'manual_flag',
      name: p.provider_name || '', npi: vr.npi_validated || p.npi_uploaded || '',
      issue: `${dimLabel} — manual flag`, dim: dim || label.toLowerCase(),
      flag: alignFlag || 'manual', providerIdx: idx,
      rosterValue: uploadVal || '',
      nppesValue: npVal || '',
      suggestions: [`Verify ${dimLabel}: roster shows "${uploadVal||'—'}", NPPES shows "${npVal||'—'}"`],
      suggestedText: `Verify ${dimLabel} for ${p.provider_name || 'provider'}`,
    });
    const flagBtn = `<button class="cmp-flag-btn"
      onclick="openTaskPopover(JSON.parse(this.dataset.ctx),event)"
      data-ctx="${manualCtx.replace(/"/g,'&quot;')}"
      title="Flag this field — create a task to investigate">
      <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" style="width:12px;height:12px">
        <path d="M3 2v12M3 2h8l-2 3.5 2 3.5H3"/>
      </svg>
    </button>`;

    let actions = '';
    if (isActionable) {
      if (isDismissed) {
        actions = `<span style="display:flex;align-items:center;gap:3px">
          <span style="font-size:.65rem;color:var(--text-3);white-space:nowrap">dismissed
            <button class="link-btn" style="font-size:.62rem;color:var(--indigo)"
              onclick="reconUndoDismissDim(${idx},'${dim}')" title="Undo dismiss">undo</button>
          </span>
          ${flagBtn}
        </span>`;
      } else {
        const issueLabel = alignFlag === 'drift' ? `${dimLabel} drift` : alignFlag === 'deactivated' ? 'Deactivated' : `${dimLabel} mismatch`;
        const suggestions = _ALIGN_SUGGESTIONS[`${dim}:${alignFlag}`] || [];
        const tCtx = JSON.stringify({
          stepId: 'nppes_alignment', type: 'alignment',
          name: p.provider_name || '', npi: vr.npi_validated || p.npi_uploaded || '',
          issue: issueLabel, dim, flag: alignFlag, suggestions, providerIdx: idx,
          suggestedText: suggestions[0] || issueLabel,
        });
        actions = `<span style="display:flex;gap:3px;align-items:center;justify-content:flex-end">
          <button class="row-task-btn" style="width:20px;height:20px"
            onclick="openTaskPopover(JSON.parse(this.dataset.ctx),event)"
            data-ctx="${tCtx.replace(/"/g,'&quot;')}"
            title="Track — create task for this system-detected issue">
            <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6"><rect x="3" y="2" width="10" height="12" rx="1.5"/><path d="M6 6h4M6 9h4M6 12h2"/></svg>
          </button>
          <button class="link-btn" style="font-size:.63rem;color:var(--text-3);border:1px solid var(--border);border-radius:3px;padding:1px 5px;white-space:nowrap"
            onclick="reconDismissDim(${idx},'${dim}')" title="Expected / not a real problem — dismiss">
            Dismiss
          </button>
        </span>`;
      }
    } else {
      // Non-drifted row: just the manual flag icon (visible on row hover via CSS)
      actions = flagBtn;
    }

    const noteAttr = note ? ` title="${esc(note)}"` : '';
    const noteIcon = (note && isActionable) ? ` <span style="font-size:.65rem;color:var(--text-3);cursor:help" title="${esc(note)}">ⓘ</span>` : '';
    // Actionable rows: left-border stripe only (no background fill — keeps table readable)
    const rowBg = isDismissed ? 'opacity:.55' : '';
    const rowBorder = isActionable && !isDismissed ? 'border-left:2px solid var(--amber,#f59e0b)' : '';
    return `<tr class="cmp-row" id="cmprow-${idx}-${dim}" style="${rowBg}${rowBorder ? ';'+rowBorder : ''}">
      <td style="font-size:.72rem;color:var(--text-3);padding:.3rem .5rem .3rem 0;vertical-align:top;white-space:nowrap">${label}</td>
      <td style="font-size:.8rem;padding:.3rem .5rem;vertical-align:top;color:var(--text-2);${uStyle}">${esc(uploadVal || '—')}</td>
      <td style="font-size:.8rem;padding:.3rem .5rem;vertical-align:top;${nStyle}"${noteAttr}>${esc(npVal || '—')}${noteIcon}</td>
      <td style="padding:.3rem .25rem;vertical-align:top;text-align:center">${alignBadge(alignFlag)}</td>
      <td style="padding:.3rem 0;vertical-align:top;text-align:right;min-width:56px">${actions}</td>
    </tr>`;
  }

  // ── Phase 1 — Match ──────────────────────────────────────────
  const methodLabel = valType === 'npi_lookup'
    ? (matchInfo.npi_provided ? 'NPI lookup — confirmed' : 'NPI lookup')
    : valStat === 'manual_review' ? `Name search — ${matchInfo.candidate_count || ''} candidates`
    : 'Name search — best match';
  const methodColor = valType === 'npi_lookup' ? 'var(--green)' : '#d97706';

  const matchBk = matchInfo.score_breakdown || topBk;
  const matchBkHtml = (() => {
    const parts = [];
    if (matchBk.name_score  !== undefined) parts.push(`<span>name <strong>${Math.round(matchBk.name_score*100)}%</strong></span>`);
    if (matchBk.location_label === 'zip_match')       parts.push(`<span style="color:var(--green)">✓ zip match</span>`);
    else if (matchBk.location_label === 'state_match')parts.push(`<span style="color:var(--green)">✓ same state</span>`);
    else if (matchBk.location_label === 'state_mismatch') parts.push(`<span style="color:var(--red)">✗ different state</span>`);
    if (matchBk.status_label === 'deactivated')       parts.push(`<span style="color:var(--red)">✗ DEACTIVATED</span>`);
    return parts.length ? `<span style="font-size:.68rem;color:var(--text-3);display:flex;gap:.5rem;flex-wrap:wrap;margin-top:.2rem">${parts.join('')}</span>` : '';
  })();

  // ── Phase 2 — Alignment summary chips ───────────────────────
  const alignChipMap = {
    name:       { label: 'Name drift',          color: '#d97706' },
    taxonomy:   { label: 'Taxonomy mismatch',   color: '#dc2626' },
    address:    { label: aAddr.flag === 'drift' ? 'Unrecognised address' : (aAddr.org_locations_used ? 'Location mismatch' : 'State mismatch'), color: aAddr.flag === 'drift' ? '#d97706' : '#dc2626' },
    status:     { label: 'Deactivated',         color: '#dc2626' },
    credential: { label: aCred.flag === 'drift' ? 'Credential partial match' : 'Credential mismatch', color: aCred.flag === 'drift' ? '#d97706' : '#dc2626' },
    zip:        { label: zip5Flag === 'mismatch' ? 'ZIP mismatch' : 'ZIP+4 drift', color: zip5Flag === 'mismatch' ? '#dc2626' : '#d97706' },
  };
  const alignChips = alignSum.map(k => {
    const m = alignChipMap[k] || { label: k, color: '#dc2626' };
    return `<span style="font-size:.67rem;padding:.15rem .45rem;border-radius:20px;background:var(--red-bg);border:1px solid var(--red-border);color:${m.color};font-weight:600">${m.label}</span>`;
  }).join('');

  // (alignment dimension variables declared earlier, before alignChipMap)

  const npTaxFull = aTax.nppes_code
    ? `${aTax.nppes_code}${aTax.nppes ? ' — ' + aTax.nppes : ''}`
    : (taxonomy || '—');

  // Credential display helpers
  const rosterCredStr = Array.isArray(aCred.roster) && aCred.roster.length
    ? aCred.roster.join(', ')
    : (Array.isArray(p.credentials) && p.credentials.length ? p.credentials.join(', ') : '');
  const npCredStr = Array.isArray(aCred.nppes) && aCred.nppes.length
    ? aCred.nppes.join(', ') : '';

  // ── AI Recommendation ────────────────────────────────────────
  // Derive a plain-English recommendation so the human only needs to
  // act on things the AI genuinely cannot decide.
  const aiRec = (() => {
    if (valStat === 'fail') return null; // handled by no-match panel above

    // Deactivated — always track, revenue critical
    if (aStatus.flag === 'deactivated') return {
      verdict: 'track',
      icon: '🚨',
      color: '#dc2626', bg: '#fef2f2', border: '#fca5a5',
      headline: 'NPI is deactivated in NPPES',
      detail: 'This provider cannot be credentialed with an inactive NPI. Remove from active roster or obtain a new NPI.',
      action: 'track',
    };

    // Address at an unrecognised location — needs human verification
    if (aAddr.flag === 'drift' || aAddr.flag === 'mismatch') return {
      verdict: 'track',
      icon: '📍',
      color: '#d97706', bg: '#fffbeb', border: '#fde68a',
      headline: aAddr.flag === 'mismatch' ? 'Provider not at a known org location' : 'NPPES address not in known org locations',
      detail: aAddr.detail || 'Verify the provider\'s current practice location.',
      action: 'track',
    };

    // Taxonomy / specialty hard mismatch
    if (aTax.flag === 'mismatch') return {
      verdict: 'track',
      icon: '⚕️',
      color: '#d97706', bg: '#fffbeb', border: '#fde68a',
      headline: 'Specialty differs from NPPES record',
      detail: `Roster shows "${aTax.roster || '—'}", NPPES shows "${aTax.nppes || '—'}". Update roster or confirm specialty.`,
      action: 'track',
    };

    // Name drift — synthesise a specific reason
    // Use nppes_raw for analysis (has the credentials we want to describe),
    // but display nppes (credential-stripped) in the comparison table.
    const nameScore = aName.score || 0;
    const credOnly  = aName.cred_only === true;
    const extraCreds = aName.extra_creds || [];
    if (aName.flag === 'drift') {
      const specific = credOnly && extraCreds.length
        ? `NPPES filed with credential suffix (${extraCreds.join(', ')}) — same provider`
        : _analyzeNameDiff(aName.roster || p.provider_name, aName.nppes_raw || aName.nppes || vr.provider_name_validated);
      // Credential-only differences are always auto-dismiss
      const isConfident = credOnly || nameScore >= 0.75;
      return {
        verdict: isConfident ? 'auto-dismiss' : 'track',
        icon: isConfident ? '∞' : '⚠',
        color: isConfident ? 'var(--indigo)' : '#d97706',
        bg: isConfident ? '#eef2ff' : '#fffbeb',
        border: isConfident ? '#c7d2fe' : '#fde68a',
        headline: specific
          ? `${specific} — ${isConfident ? 'no action needed' : 'please verify'}`
          : (isConfident ? 'Minor name variant — same provider, no action needed.' : 'Name differs — please verify this is the correct provider.'),
        detail: (credOnly && aName.nppes_raw && aName.nppes_raw !== aName.nppes)
          ? `NPPES filed as: "${aName.nppes_raw}"` : null,
        action: isConfident ? 'dismiss' : 'track',
      };
    }

    // Name hard mismatch
    if (aName.flag === 'mismatch') return {
      verdict: 'track',
      icon: '⚠',
      color: '#d97706', bg: '#fffbeb', border: '#fde68a',
      headline: `Name differs substantially — verify this is the correct NPPES record`,
      detail: `Roster: "${aName.roster || p.provider_name}" vs NPPES: "${aName.nppes_raw || aName.nppes || vr.provider_name_validated}"`,
      action: 'track',
    };

    // Credential partial match — always confident
    if (aCred.flag === 'drift') {
      const specific = _analyzeNameDiff(
        Array.isArray(aCred.roster) ? aCred.roster.join(', ') : '',
        Array.isArray(aCred.nppes)  ? aCred.nppes.join(', ')  : ''
      );
      return {
        verdict: 'auto-dismiss',
        icon: '∞',
        color: 'var(--indigo)', bg: '#eef2ff', border: '#c7d2fe',
        headline: 'NPPES includes additional credential suffixes — expected, no action needed.',
        detail: null,
        action: 'dismiss',
      };
    }

    // All clear
    return {
      verdict: 'ok',
      icon: '∞',
      color: 'var(--indigo)', bg: '#eef2ff', border: '#c7d2fe',
      headline: 'All fields align with NPPES — Mobius is confident.',
      detail: null,
      action: null,
    };
  })();

  const recHtml = aiRec ? (() => {
    /* Mobius brand mark always uses the canonical logo grey, not the status color */
    const mobiusLogo = `<span style="font-size:1rem;font-weight:800;color:var(--mobius-logo-grey);letter-spacing:-.5px;font-family:serif;flex-shrink:0" title="Mobius AI">∞</span>`;
    const actionHtml = aiRec.action === 'dismiss'
      ? `<button class="link-btn" style="display:inline-flex;align-items:center;gap:.3rem;font-size:.72rem;color:${aiRec.color};font-weight:700;margin-top:.4rem;padding:.25rem .7rem;border:1px solid ${aiRec.border};border-radius:5px;background:white"
           onclick="reconDismissDim(${idx},'${alignSum[0]||'name'}')" title="Accept Mobius recommendation — one click">
           ✓ Accept &amp; dismiss
         </button>
         <button class="link-btn" style="font-size:.68rem;color:var(--text-3);margin-left:.5rem" onclick="void(0)" title="Override — mark as needs attention">override</button>`
      : aiRec.action === 'track'
      ? `<button class="link-btn" style="display:inline-flex;align-items:center;gap:.3rem;font-size:.72rem;color:var(--text-2);font-weight:600;margin-top:.4rem;padding:.25rem .7rem;border:1px solid var(--border);border-radius:5px;background:var(--surface)"
           onclick="openTaskPopover({stepId:'nppes_alignment',type:'ai_recommendation',name:${JSON.stringify(p.provider_name||'')},npi:${JSON.stringify(vr.npi_validated||'')},issue:${JSON.stringify(aiRec.headline)},suggestedText:${JSON.stringify(aiRec.headline)}},event)"
           title="Create a tracked task — Mobius will monitor">
           📌 Track this
         </button>`
      : '';
    return `<div style="background:${aiRec.bg};border:1px solid ${aiRec.border};border-radius:7px;padding:.45rem .65rem;margin-bottom:.5rem">
      <div style="display:flex;align-items:flex-start;gap:.45rem">
        ${mobiusLogo}
        <div style="flex:1;min-width:0">
          <span style="font-size:.72rem;font-weight:600;color:var(--mobius-logo-grey);margin-right:.3rem">Mobius</span><span style="font-size:.8125rem;font-weight:500;color:var(--text)">${aiRec.headline}</span>
          ${aiRec.detail ? `<div style="font-size:.7rem;color:var(--text-3);margin-top:.15rem">${esc(aiRec.detail)}</div>` : ''}
          <div>${actionHtml}</div>
        </div>
      </div>
    </div>`;
  })() : '';

  // ── "How we scored this" — collapsible audit detail ─────────
  const scoringHtml = `
    <details style="margin-bottom:.5rem">
      <summary style="cursor:pointer;font-size:.68rem;color:var(--text-3);user-select:none;list-style:none;display:flex;align-items:center;gap:.25rem;padding:.15rem 0">
        <span style="font-size:.6rem">▸</span> How Mobius scored this
        <span style="font-size:.63rem;color:var(--text-3);font-weight:400">${conf}% confidence · ${methodLabel}</span>
      </summary>
      <div style="background:var(--grey-bg);border:1px solid var(--border);border-radius:6px;padding:.45rem .6rem;margin-top:.3rem">
        ${matchBkHtml}
        ${alignChips ? `<div style="display:flex;flex-wrap:wrap;gap:.2rem;margin-top:.35rem">${alignChips}</div>` : ''}
        ${candidateHtml}
      </div>
    </details>`;

  panelEl.innerHTML = `
    ${recHtml}

    <!-- Comparison table — facts, always visible -->
    <table style="width:100%;border-collapse:collapse;margin-bottom:.4rem">
      <thead>
        <tr>
          <th style="font-size:.65rem;font-weight:600;text-transform:uppercase;letter-spacing:.05em;color:var(--text-3);text-align:left;padding:.25rem .5rem .25rem 0;border-bottom:1px solid var(--border)">Field</th>
          <th style="font-size:.65rem;font-weight:600;text-transform:uppercase;letter-spacing:.05em;color:var(--text-3);text-align:left;padding:.25rem .5rem;border-bottom:1px solid var(--border)">Roster file</th>
          <th style="font-size:.65rem;font-weight:600;text-transform:uppercase;letter-spacing:.05em;color:var(--text-3);text-align:left;padding:.25rem .5rem;border-bottom:1px solid var(--border)">${prevValid ? 'Validated (NPPES)' : 'NPPES registry'}</th>
          <th style="font-size:.65rem;font-weight:600;text-transform:uppercase;letter-spacing:.05em;color:var(--text-3);text-align:center;padding:.25rem .25rem;border-bottom:1px solid var(--border)"></th>
          <th style="width:80px;border-bottom:1px solid var(--border)"></th>
        </tr>
      </thead>
      <tbody>
        ${cmpRow('Name',         p.provider_name,
                               /* prefer credential-stripped name; fall back to raw */
                               aName.nppes || aName.nppes_raw || vr.provider_name_validated,
                               aName.flag, 'name',
                               /* tooltip: show raw NPPES name if credentials were stripped */
                               (aName.extra_creds?.length && aName.nppes_raw && aName.nppes_raw !== aName.nppes)
                                 ? `NPPES filed as: ${aName.nppes_raw}` : null)}
        ${cmpRow('NPI',          p.npi_uploaded || '(not in file)',  vr.npi_validated,                                                       vr.npi_validated ? 'ok' : 'no_nppes_data', null)}
        ${cmpRow('Credentials',  rosterCredStr,                      npCredStr,                                                               aCred.flag,   'credential')}
        ${cmpRow('Specialty',    p.specialty_uploaded,               aTax.nppes   || vr.specialty_validated,                                 aTax.flag,    'taxonomy')}
        ${cmpRow('Location',     aAddr.roster || p.state,            aAddr.nppes_state ? `${aAddr.nppes_state} · ${aAddr.nppes||loc||''}` : (aAddr.nppes || loc || ''), aAddr.flag, 'address', aAddr.detail)}
        ${cmpRow('ZIP',          zipRoster5,                          zipNppes5,                                                               zip5Flag,     'zip')}
        ${aStatus.flag === 'deactivated' ? cmpRow('Status', '', 'Deactivated', 'deactivated', 'status') : ''}
      </tbody>
    </table>

    ${scoringHtml}

    <!-- ── Audit trail (collapsed) ────────────────────────── -->
    ${p.id ? `
    <details style="margin-top:.5rem;border:1px solid var(--border);border-radius:6px;overflow:hidden"
      ontoggle="if(this.open) loadProviderAuditLog(${p.id}, 'prov-audit-${idx}')">
      <summary style="display:flex;align-items:center;gap:.4rem;padding:.3rem .6rem;cursor:pointer;list-style:none;font-size:.7rem;font-weight:600;color:var(--text-3);background:var(--grey-bg);user-select:none;-webkit-appearance:none">
        <span>📋 Activity log</span>
        <span style="font-weight:400;font-size:.65rem">▸</span>
      </summary>
      <div id="prov-audit-${idx}" style="padding:.4rem .65rem;font-size:.7rem;color:var(--text-2)">
        Click to load…
      </div>
    </details>` : ''}

    <!-- ── Footer actions ──────────────────────────────────── -->
    <div style="display:flex;align-items:center;justify-content:space-between;gap:.5rem;padding-top:.45rem;border-top:1px solid var(--border);margin-top:.4rem;flex-wrap:wrap">
      <div style="display:flex;gap:.5rem;align-items:center;flex-wrap:wrap">
        ${vr.npi_validated ? `
          <button class="link-btn" style="font-size:.72rem;font-weight:700;color:var(--green);border:1px solid var(--green-border);border-radius:5px;padding:.25rem .75rem;background:var(--green-bg)"
            onclick="approveProviderToTruth(${idx})" title="Confirm this NPI as your org's source of truth — merges NPPES + roster fields">
            ✓ Approve to roster
          </button>` : ''}
        <button class="link-btn" style="font-size:.68rem;color:var(--text-3)"
          onclick="toggleRematchPanel(${idx})" title="Wrong provider matched? Search for a different NPI">
          Wrong match? Change NPI
        </button>
      </div>
      ${vr.npi_validated ? `<span style="font-size:.65rem;color:var(--text-3)">NPI ${vr.npi_validated}</span>` : ''}
    </div>

    <!-- ── Inline NPI re-match panel ──────────────────────── -->
    <div id="rematch-panel-${idx}" style="display:none;margin-top:.5rem;background:var(--grey-bg);border:1px solid var(--border);border-radius:7px;padding:.6rem .75rem">
      <div style="font-size:.72rem;font-weight:700;color:var(--text-2);margin-bottom:.4rem">Find a different NPI match</div>
      <div style="display:flex;gap:.4rem;align-items:flex-start;flex-wrap:wrap">
        <div style="flex:0 0 auto">
          <input id="rematch-npi-${idx}" type="text" placeholder="Paste NPI (10 digits)"
            style="width:140px;font-size:.75rem;padding:.3rem .5rem;border:1px solid var(--border);border-radius:5px;font-family:monospace"
            oninput="this.value=this.value.replace(/\D/g,'').slice(0,10)"
            onkeydown="if(event.key==='Enter')rematchLookupNpi(${idx})">
        </div>
        <div style="flex:1;min-width:140px;display:flex;gap:.3rem">
          <input id="rematch-name-${idx}" type="text" placeholder="Name" value="${esc(p.provider_name||'')}"
            style="flex:1;font-size:.75rem;padding:.3rem .5rem;border:1px solid var(--border);border-radius:5px"
            onkeydown="if(event.key==='Enter')rematchSearchName(${idx})">
          <input id="rematch-state-${idx}" type="text" placeholder="State" value="${esc(p.state||'')}"
            style="width:46px;font-size:.75rem;padding:.3rem .4rem;border:1px solid var(--border);border-radius:5px;text-transform:uppercase"
            maxlength="2" onkeydown="if(event.key==='Enter')rematchSearchName(${idx})">
        </div>
        <div style="display:flex;gap:.3rem">
          <button class="link-btn" style="font-size:.72rem;background:var(--indigo);color:#fff;border-radius:5px;padding:.28rem .65rem;font-weight:600"
            onclick="rematchLookupNpi(${idx})">Lookup NPI</button>
          <button class="link-btn" style="font-size:.72rem;border:1px solid var(--border);border-radius:5px;padding:.28rem .65rem"
            onclick="rematchSearchName(${idx})">Search name</button>
        </div>
      </div>
      <div id="rematch-results-${idx}" style="margin-top:.45rem"></div>
    </div>`;
}

// ── Approve provider to roster truth (NPI Anchor model) ────────
async function approveProviderToTruth(idx) {
  const s = window._rosterUploadState;
  const p = s?.report?.clean?.[idx];
  if (!p?.id) return;
  feEmit(`Approving ${p.provider_name || 'provider'} to roster…`);
  const btn = document.querySelector(`#recon-panel-${idx} button[onclick*="approveProviderToTruth"]`);
  if (btn) { btn.disabled = true; btn.innerHTML = '<span class="spinner" style="width:11px;height:11px;border-width:1.5px;display:inline-block;vertical-align:middle;margin-right:4px"></span>Promoting…'; }
  try {
    const runId  = window._lastRunId || window.lastRun?.run_id || '';
    const orgName = window.lastRun?.org_name || '';
    const r = await fetch(`/chat/roster-reconcile/provider/${p.id}/approve`, {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({ run_id: runId, org_name: orgName }),
    });
    const data = await r.json();
    if (!r.ok) throw new Error(data.detail || 'Approval failed');
    feEmit(`✓ ${p.provider_name || 'Provider'} moved to roster`, 'ok');

    // Mark as approved in local state
    p._approvedToTruth = true;
    p._decision = 'validated';

    // Log audit event
    fetch(`/chat/roster-reconcile/provider/${p.id}/audit-log`, {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({ event_type: 'approved', actor: 'user',
        run_id: runId, org_name: orgName,
        event_data: { promoted_to_roster: true } }),
    }).catch(() => {});

    // ── Step 1: animate the workspace row out ────────────────────
    const wsRow = document.getElementById(`recon-row-${idx}`);
    const detRow = document.getElementById(`recon-detail-${idx}`);
    if (wsRow) wsRow.classList.add('recon-row-exiting');
    if (detRow) detRow.style.display = 'none';

    await new Promise(r => setTimeout(r, 420)); // wait for exit animation

    // ── Step 2: rebuild workspace table (row gone) ───────────────
    _refreshReconView();

    // ── Step 3: animate new row into roster section ──────────────
    _appendRosterRow(p, idx);
    _refreshNppesSection();   // update score story
    _loadSessionBanner();     // update session banner counts
    _showToast(`✓ ${p.provider_name} moved to roster`);

  } catch(e) {
    feEmit(`Approval failed — ${e.message}`, 'error');
    if (btn) { btn.disabled = false; btn.textContent = '✓ Approve to roster'; }
    alert('Approval failed: ' + e.message);
  }
}

// After individual approve: optimistically insert one card, then refresh from DB
function _appendRosterRow(p, idx) {
  const list = document.getElementById('rosterLiveList');
  if (list) {
    const vr  = p.latest_validation;
    const npi = vr?.npi_validated || p.npi_uploaded || '—';
    const div = document.createElement('div');
    div.className = 'rt-card roster-live-row roster-row-new';
    div.id        = `roster-row-prov-${p.id || ''}`;
    div.innerHTML = `<div class="rt-card-head" onclick="_toggleRosterCard(this)">
      <span class="rt-card-name">${esc(titleCase(p.provider_name || '—'))}</span>
      <span class="rt-card-npi">${esc(npi)}</span>
      <span class="rt-card-loc">—</span>
      <span class="spinner" style="width:10px;height:10px;border-width:1.5px;flex-shrink:0"></span>
      <span class="rt-card-chevron">▾</span>
    </div><div class="rt-card-body"></div>`;
    list.insertBefore(div, list.firstChild);
    // Open the roster section so the new card is visible
    const details = document.getElementById('rosterSectionDetails');
    if (details) { details.open = true; window._rosterSectionOpen = true; }
  }

  // Ensure roster section exists
  const rosterSec = document.getElementById('rosterSection');
  if (!rosterSec?.innerHTML?.trim()) {
    rosterSec.innerHTML = _buildRosterSectionHtml();
  }

  // Scroll into view
  setTimeout(() => {
    const sec = document.getElementById('rosterSection');
    if (sec) sec.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  }, 100);

  // Then reload from DB (source of truth) to get accurate data including gaps filled
  setTimeout(_loadRosterTruth, 300);
}

// ── Mass approve visual transition ──────────────────────────────
async function _animateMassApproveTransition() {
  const clean = window._rosterUploadState?.report?.clean || [];
  // Stagger-exit all rows that are now approved
  const approvedIdxs = clean.map((p,i) => p._approvedToTruth ? i : -1).filter(i => i >= 0);
  const STAGGER = 35; // ms between each row exit start

  approvedIdxs.forEach((idx, i) => {
    setTimeout(() => {
      const wsRow  = document.getElementById(`recon-row-${idx}`);
      const detRow = document.getElementById(`recon-detail-${idx}`);
      if (wsRow)  wsRow.classList.add('recon-row-exiting');
      if (detRow) detRow.style.display = 'none';
    }, i * STAGGER);
  });

  // Wait for all exit animations to finish
  const totalDelay = approvedIdxs.length * STAGGER + 420;
  await new Promise(r => setTimeout(r, totalDelay));

  // Rebuild both sections
  _refreshReconView();
  const rosterSec = document.getElementById('rosterSection');
  if (rosterSec) {
    rosterSec.innerHTML = _buildRosterSectionHtml();
    // Mark all rows as new for entry animation
    setTimeout(() => {
      rosterSec.querySelectorAll('.roster-live-row').forEach((row, i) => {
        row.style.animationDelay = `${i * 30}ms`;
        row.classList.add('roster-row-new');
      });
      const rosterSec2 = document.getElementById('rosterSection');
      if (rosterSec2) rosterSec2.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    }, 20);
  }
  _refreshWorkspaceHeader();
  _refreshNppesSection();
  // Reload roster from DB (source of truth)
  setTimeout(_loadRosterTruth, 200);
}

// ── Inline re-match panel toggle ────────────────────────────────
function toggleRematchPanel(idx) {
  const p = document.getElementById(`rematch-panel-${idx}`);
  if (p) p.style.display = p.style.display === 'none' ? '' : 'none';
}

// ── NPI direct lookup ────────────────────────────────────────────
async function rematchLookupNpi(idx) {
  const npi = (document.getElementById(`rematch-npi-${idx}`)?.value || '').trim();
  if (npi.length !== 10) { _rematchShowError(idx, 'Enter a valid 10-digit NPI'); return; }
  _rematchSetLoading(idx, 'Looking up NPI…');
  try {
    const r = await fetch(`/chat/roster-reconcile/npi-search?npi=${encodeURIComponent(npi)}`);
    const d = await r.json();
    _rematchShowResults(idx, d.candidates || [], npi);
  } catch(e) { _rematchShowError(idx, 'Lookup failed: ' + e.message); }
}

// ── Name search ──────────────────────────────────────────────────
async function rematchSearchName(idx) {
  const name  = (document.getElementById(`rematch-name-${idx}`)?.value || '').trim();
  const state = (document.getElementById(`rematch-state-${idx}`)?.value || '').trim();
  if (!name) { _rematchShowError(idx, 'Enter a name to search'); return; }
  _rematchSetLoading(idx, `Searching NPPES for "${name}"…`);
  try {
    const r = await fetch(`/chat/roster-reconcile/npi-search?name=${encodeURIComponent(name)}&state=${encodeURIComponent(state)}`);
    const d = await r.json();
    _rematchShowResults(idx, d.candidates || [], null);
  } catch(e) { _rematchShowError(idx, 'Search failed: ' + e.message); }
}

function _rematchSetLoading(idx, msg) {
  const el = document.getElementById(`rematch-results-${idx}`);
  if (el) el.innerHTML = `<span style="font-size:.72rem;color:var(--text-3)">${msg}</span>`;
}
function _rematchShowError(idx, msg) {
  const el = document.getElementById(`rematch-results-${idx}`);
  if (el) el.innerHTML = `<span style="font-size:.72rem;color:var(--red)">${msg}</span>`;
}

function _rematchShowResults(idx, candidates, searchedNpi) {
  const el = document.getElementById(`rematch-results-${idx}`);
  if (!el) return;
  if (!candidates.length) { _rematchShowError(idx, searchedNpi ? 'NPI not found in NPPES' : 'No matches found'); return; }
  el.innerHTML = candidates.map(c => {
    const name  = esc(c.name || '—');
    const npi   = esc(c.npi  || '—');
    const spec  = esc(c.taxonomy || c.specialty || '—');
    const addr  = esc(c.address || '—');
    const stat  = (c.status || '').toUpperCase() === 'A' ? `<span style="color:var(--green);font-weight:700">Active</span>` : `<span style="color:var(--red)">Inactive</span>`;
    return `<div style="border:1px solid var(--border);border-radius:6px;padding:.4rem .55rem;margin-bottom:.3rem;background:white;display:flex;align-items:flex-start;gap:.5rem">
      <div style="flex:1;min-width:0">
        <div style="font-size:.78rem;font-weight:700">${name}</div>
        <div style="font-size:.68rem;color:var(--text-3);margin-top:.1rem">${npi} · ${spec}</div>
        <div style="font-size:.67rem;color:var(--text-3)">${addr} · ${stat}</div>
      </div>
      <button class="link-btn" style="font-size:.7rem;font-weight:700;color:var(--indigo);border:1px solid var(--indigo-border);border-radius:5px;padding:.2rem .6rem;white-space:nowrap;flex-shrink:0"
        onclick="rematchSelectCandidate(${idx}, ${JSON.stringify(JSON.stringify(c)).replace(/"/g,'&quot;')})"
        title="Use this NPI and re-run alignment checks">
        Use this NPI
      </button>
    </div>`;
  }).join('');
}

async function rematchSelectCandidate(idx, candidateJson) {
  const p = window._rosterUploadState?.report?.clean?.[idx];
  if (!p?.id) return;
  let c;
  try { c = JSON.parse(candidateJson); } catch { return; }
  _rematchSetLoading(idx, `Re-running checks for NPI ${c.npi}…`);
  try {
    const r = await fetch(`/chat/roster-reconcile/provider/${p.id}/revalidate`, {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({ npi_override: c.npi }),
    });
    const data = await r.json();
    if (!r.ok) throw new Error(data.detail || 'Revalidation failed');
    // Merge fresh validation back into the in-memory provider object
    if (data.validation) {
      p.latest_validation = data.validation;
      p.npi_uploaded = c.npi;
      p._dismissedDims = [];
      p._decision = null;
    }
    document.getElementById(`rematch-panel-${idx}`)?.style && (document.getElementById(`rematch-panel-${idx}`).style.display = 'none');
    const panelEl = document.getElementById(`recon-panel-${idx}`);
    if (panelEl) _renderNppesDetail(idx, panelEl);
    _refreshReconView();
  } catch(e) { _rematchShowError(idx, 'Re-validation failed: ' + e.message); }
}

function rosterToggleNppes(idx) {
  const detRow = document.getElementById(`nppes-detail-${idx}`);
  const btn    = document.getElementById(`ntb-${idx}`);
  if (!detRow) return;
  if (detRow.style.display !== 'none') {
    detRow.style.display = 'none';
    if (btn) btn.textContent = '+';
    return;
  }
  const panelEl = document.getElementById(`nppes-panel-${idx}`);
  if (panelEl) _renderNppesDetail(idx, panelEl);
  detRow.style.display = '';
  if (btn) btn.textContent = '−';
}

// Toggle inline detail in the reconciliation table (name click).
function reconToggleDetail(idx) {
  const detRow = document.getElementById(`recon-detail-${idx}`);
  if (!detRow) return;
  if (detRow.style.display !== 'none') {
    detRow.style.display = 'none';
    detRow.dataset.open = 'false';
    return;
  }
  const panelEl = document.getElementById(`recon-panel-${idx}`);
  if (panelEl) _renderNppesDetail(idx, panelEl);
  detRow.style.display = '';
  detRow.dataset.open = 'true';
}

function handleRosterDrop(event) {
  event.preventDefault();
  event.currentTarget.classList.remove('drag-over');
  const file = event.dataTransfer.files[0];
  if (file) handleRosterFile(file);
}

function handlePmlDrop(event) {
  event.preventDefault();
  event.currentTarget.classList.remove('drag-over');
  const file = event.dataTransfer.files[0];
  if (file) handlePmlFile(file);
}

async function handlePmlFile(file) {
  if (!file) return;
  chatAppend('assistant', `PML file **${esc(file.name)}** noted. PML ingestion pipeline coming in Step 7.`);
}

function refreshNppes() {
  chatAppend('assistant', 'Refreshing NPPES data for this org — re-running Step 4 lookup…');
  if (window._lastRunId) {
    fetch(`/chat/credentialing-runs/${window._lastRunId}/step/find_associated_providers/rerun`, { method: 'POST' })
      .catch(() => {});
  }
}

function openProvDetail(npi) {
  if (!npi) return;
  const p = (window._provData || []).find(x => x.npi === npi);
  if (!p) return;
  // Pre-fill the chat with context about this provider
  const chatInput = document.getElementById('chatInput');
  if (chatInput) {
    chatInput.value = `Tell me about provider ${p.name || npi} (NPI: ${npi}) — ${p.bucket === 'anomaly' ? 'investigate the anomalies: ' + (p.anomalies||[]).join(', ') : p.bucket === 'external_only' ? 'they appear in NPPES but not on our roster' : p.bucket === 'needs_attention' ? 'they need attention — ' + (p.roster_rationale || 'single source only') : 'confirm their credentialing status'}`;
    chatInput.focus();
    chatInput.style.height = 'auto';
    chatInput.style.height = Math.min(chatInput.scrollHeight, 120) + 'px';
  }
}

