/* ── Org Story Dashboard ─────────────────────────────────────────────── */
const API = window.location.origin;
let _storyData = null;

// ── Helpers ──
function fmt$(n) { return n == null ? '—' : '$' + Number(n).toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2}); }
function fmtM(n) { return n == null ? '—' : '$' + (n / 1e6).toFixed(2) + 'M'; }
function fmtN(n) { return n == null ? '—' : Number(n).toLocaleString(); }
function fmtIdx(n) { return n == null ? '—' : n.toFixed(3); }
function fmtPct(n) { return n == null ? '—' : (n >= 0 ? '+' : '') + n.toFixed(1) + '%'; }
function sigColor(val, threshold) {
  if (val > threshold) return 'os-cell-green';
  if (val < -threshold) return 'os-cell-red';
  return '';
}
function relColor(val) {
  if (val > 1.03) return 'os-cell-green';
  if (val < 0.97) return 'os-cell-red';
  return 'os-cell-muted';
}

// ── Load org list ──
async function loadOrgs() {
  const sel = document.getElementById('orgSelect');
  try {
    const r = await fetch(`${API}/chat/financial-strategy/orgs?include_community_bh=1`);
    const data = r.ok ? await r.json() : { orgs: [] };
    const orgs = data.orgs || [];
    sel.innerHTML = '<option value="">-- Select an organization --</option>';
    for (const o of orgs) {
      const opt = document.createElement('option');
      opt.value = o.org_name;
      opt.textContent = o.org_name + (o.org_city ? ` — ${o.org_city}` : '');
      sel.appendChild(opt);
    }
    sel.onchange = () => { document.getElementById('loadBtn').disabled = !sel.value; };

    // Auto-load if ?org= param is present
    const params = new URLSearchParams(window.location.search);
    const autoOrg = params.get('org');
    if (autoOrg) {
      // Find best match
      const match = orgs.find(o => o.org_name.toLowerCase().includes(autoOrg.toLowerCase()));
      if (match) {
        sel.value = match.org_name;
        sel.dispatchEvent(new Event('change'));
        loadStory();
      }
    }
  } catch (e) {
    sel.innerHTML = '<option value="">Failed to load organizations</option>';
  }
}

// ── Fetch and render ──
async function loadStory() {
  const orgName = document.getElementById('orgSelect').value;
  if (!orgName) return;

  const errEl = document.getElementById('selectorError');
  errEl.hidden = true;

  // Show loading
  document.getElementById('selectorPanel').innerHTML = `
    <div class="os-loading"><span class="os-spinner"></span> Loading Revenue Story for <strong>${orgName}</strong>... (this queries BigQuery, ~30s)</div>
  `;
  document.getElementById('headerOrg').textContent = orgName;

  try {
    const r = await fetch(`${API}/chat/org-story`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ org_name: orgName }),
    });
    if (!r.ok) {
      const err = await r.json().catch(() => ({ detail: r.statusText }));
      throw new Error(err.detail || r.statusText);
    }
    _storyData = await r.json();
    document.getElementById('selectorPanel').hidden = true;
    document.getElementById('dashboard').hidden = false;
    renderDashboard(_storyData);
  } catch (e) {
    document.getElementById('selectorPanel').innerHTML = `
      <div class="os-selector-inner">
        <div class="os-error">Error: ${e.message}</div>
        <button class="os-btn" onclick="location.reload()" style="margin-top:1rem">Retry</button>
      </div>
    `;
  }
}

// ── Main render ──
function renderDashboard(d) {
  renderNarrative(d);
  renderKPIs(d);
  renderBridge(d);
  renderFactorTable(d);
  renderRelative(d);
  renderYoY(d);
  renderConversion(d);
  renderLeakage(d);
  renderMix(d);
  renderCounty(d);
}

// ── Narrative ──
function renderNarrative(d) {
  const narr = d.narrative || {};
  const points = narr.summary_points || [];
  const el = document.getElementById('narrativeBanner');
  el.innerHTML = `
    <h3>The Story</h3>
    <ul>${points.map(p => `<li>${p}</li>`).join('')}</ul>
  `;
}

// ── KPI cards ──
function renderKPIs(d) {
  const narr = d.narrative || {};
  const latest = (d.org?.series || []).slice(-1)[0] || {};
  const mktLatest = (d.market?.series || []).slice(-1)[0] || {};
  const comp = Object.values(d.comparison || {}).slice(-1)[0] || {};

  const revDelta = narr.revenue_delta || 0;
  const revPct = ((latest.rev_idx || 1) - 1) * 100;
  const mktRevPct = ((mktLatest.rev_idx || 1) - 1) * 100;
  const gap = revPct - mktRevPct;

  const cards = [
    { value: fmtM(latest.paid), label: `Revenue (${latest.year})`, sub: fmtPct(revPct) + ' vs base', color: revPct > 0 ? 'green' : 'red' },
    { value: fmtPct(gap) + 'pp', label: 'vs Market', sub: `Org ${fmtPct(revPct)} / Mkt ${fmtPct(mktRevPct)}`, color: gap > 0 ? 'green' : 'red' },
    { value: fmtIdx(latest.bene_idx), label: 'Panel Index', sub: `Rel: ${fmtIdx(comp.rel_bene)}x mkt`, color: (comp.rel_bene || 1) > 1.03 ? 'green' : (comp.rel_bene || 1) < 0.97 ? 'red' : 'blue' },
    { value: fmtIdx(latest.util_idx), label: 'Utilization Index', sub: `Rel: ${fmtIdx(comp.rel_util)}x mkt`, color: (comp.rel_util || 1) > 1.03 ? 'green' : (comp.rel_util || 1) < 0.97 ? 'red' : 'blue' },
    { value: fmtIdx(latest.rate_idx), label: 'Pure Rate Index', sub: `Rel: ${fmtIdx(comp.rel_rate)}x mkt`, color: (comp.rel_rate || 1) > 1.03 ? 'green' : (comp.rel_rate || 1) < 0.97 ? 'red' : 'blue' },
    { value: fmtIdx(latest.mix_idx), label: 'Service Mix Index', sub: `Rel: ${fmtIdx(comp.rel_mix)}x mkt`, color: (comp.rel_mix || 1) > 1.03 ? 'green' : (comp.rel_mix || 1) < 0.97 ? 'red' : 'blue' },
  ];

  document.getElementById('kpiRow').innerHTML = cards.map(c => `
    <div class="os-kpi">
      <div class="os-kpi-value ${c.color}">${c.value}</div>
      <div class="os-kpi-label">${c.label}</div>
      <div class="os-kpi-sub">${c.sub}</div>
    </div>
  `).join('');
}

// ── Revenue bridge ──
function renderBridge(d) {
  const bridge = d.narrative?.revenue_bridge || {};
  const baseMoney = d.narrative?.base_revenue || 0;
  const currMoney = d.narrative?.current_revenue || 0;
  const delta = d.narrative?.revenue_delta || 0;
  const period = d.narrative?.period || '';

  const factors = Object.entries(bridge);
  const maxAbs = Math.max(...factors.map(([,v]) => Math.abs(v.dollar_impact)), 1);

  let html = `<div style="font-size:.8rem;color:var(--text2);margin-bottom:1rem">
    ${fmtM(baseMoney)} &rarr; ${fmtM(currMoney)} = <strong style="color:${delta >= 0 ? 'var(--green)' : 'var(--red)'}">${fmtM(delta)}</strong> (${period})
  </div>`;

  for (const [name, v] of factors) {
    const pct = v.dollar_impact / maxAbs * 45; // max 45% width
    const isPos = v.dollar_impact >= 0;
    const barStyle = isPos
      ? `left:50%;width:${Math.abs(pct)}%`
      : `right:50%;width:${Math.abs(pct)}%`;
    const label = name.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase());

    html += `
      <div class="os-bridge-row">
        <div class="os-bridge-label">${label}</div>
        <div class="os-bridge-bar-wrap">
          <div class="os-bridge-bar ${isPos ? 'positive' : 'negative'}" style="${barStyle}"></div>
        </div>
        <div class="os-bridge-value" style="color:${isPos ? 'var(--green)' : 'var(--red)'}">${fmtM(v.dollar_impact)}</div>
        <div class="os-bridge-pct">${fmtPct(v.pct_change)}</div>
      </div>
    `;
  }

  document.getElementById('bridgeContent').innerHTML = html;
}

// ── 5-Factor table ──
function renderFactorTable(d) {
  const orgS = d.org?.series || [];
  const mktS = d.market?.series || [];
  const mktByYear = Object.fromEntries(mktS.map(s => [s.year, s]));

  let rows = '';
  for (const o of orgS) {
    const m = mktByYear[o.year] || {};
    rows += `<tr>
      <td>${o.year}</td>
      <td class="${sigColor((o.bene_idx - 1) * 100, 5)}">${fmtIdx(o.bene_idx)}</td>
      <td class="${sigColor((o.util_idx - 1) * 100, 5)}">${fmtIdx(o.util_idx)}</td>
      <td class="${sigColor((o.rate_idx - 1) * 100, 3)}">${fmtIdx(o.rate_idx)}</td>
      <td class="${sigColor((o.mix_idx - 1) * 100, 3)}">${fmtIdx(o.mix_idx)}</td>
      <td class="os-cell-muted">${fmtIdx(o.interact_idx)}</td>
      <td style="font-weight:600">${fmtIdx(o.rev_idx)}</td>
      <td style="border-left:2px solid var(--border)" class="os-cell-muted">${fmtIdx(m.bene_idx)}</td>
      <td class="os-cell-muted">${fmtIdx(m.rate_idx)}</td>
      <td class="os-cell-muted">${fmtIdx(m.mix_idx)}</td>
      <td class="os-cell-muted" style="font-weight:600">${fmtIdx(m.rev_idx)}</td>
    </tr>`;
  }

  document.getElementById('factorContent').innerHTML = `
    <div style="overflow-x:auto">
    <table class="os-table">
      <thead>
        <tr>
          <th>Year</th>
          <th colspan="6" style="text-align:center;color:var(--accent)">Org</th>
          <th colspan="4" style="text-align:center;color:var(--text3);border-left:2px solid var(--border)">Market</th>
        </tr>
        <tr>
          <th></th><th>Bene</th><th>Util</th><th>Rate*</th><th>Mix*</th><th>Inter</th><th>Rev</th>
          <th style="border-left:2px solid var(--border)">Bene</th><th>Rate*</th><th>Mix*</th><th>Rev</th>
        </tr>
      </thead>
      <tbody>${rows}</tbody>
    </table>
    </div>
    <div style="font-size:.7rem;color:var(--text3);margin-top:.5rem">
      Rate* = pure rate change (holding mix constant) &nbsp; Mix* = pure mix shift (holding rates constant) &nbsp; Inter = compounding interaction
    </div>
  `;
}

// ── Relative position ──
function renderRelative(d) {
  const comp = d.comparison || {};
  let rows = '';
  for (const [year, c] of Object.entries(comp).sort()) {
    rows += `<tr>
      <td>${year}</td>
      <td class="${relColor(c.rel_bene)}">${fmtIdx(c.rel_bene)}</td>
      <td class="${relColor(c.rel_util)}">${fmtIdx(c.rel_util)}</td>
      <td class="${relColor(c.rel_rate)}">${fmtIdx(c.rel_rate)}</td>
      <td class="${relColor(c.rel_mix)}">${fmtIdx(c.rel_mix)}</td>
      <td class="${relColor(c.rel_rev)}" style="font-weight:600">${fmtIdx(c.rel_rev)}</td>
      <td class="os-cell-muted">${c.rev_share_pct?.toFixed(3) || '—'}%</td>
      <td class="os-cell-muted">${c.bene_share_pct?.toFixed(3) || '—'}%</td>
    </tr>`;
  }

  document.getElementById('relativeContent').innerHTML = `
    <div style="font-size:.8rem;color:var(--text2);margin-bottom:.75rem">
      Values &gt; 1.0 = outperforming market. Values &lt; 1.0 = underperforming.
    </div>
    <div style="overflow-x:auto">
    <table class="os-table">
      <thead><tr><th>Year</th><th>Rel Bene</th><th>Rel Util</th><th>Rel Rate</th><th>Rel Mix</th><th>Rel Rev</th><th>Rev Share</th><th>Bene Share</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>
    </div>
  `;
}

// ── YoY ──
function renderYoY(d) {
  const yoy = d.yoy || [];
  let rows = '';
  for (const y of yoy) {
    const o = y.org || {};
    const m = y.market || {};
    const dl = y.delta || {};
    rows += `<tr>
      <td>${y.period}</td>
      <td class="${sigColor(o.rev, 5)}" style="font-weight:600">${fmtPct(o.rev)}</td>
      <td class="${sigColor(o.bene, 5)}">${fmtPct(o.bene)}</td>
      <td class="${sigColor(o.util, 3)}">${fmtPct(o.util)}</td>
      <td class="${sigColor(o.rate, 3)}">${fmtPct(o.rate)}</td>
      <td class="${sigColor(o.mix, 2)}">${fmtPct(o.mix)}</td>
      <td style="border-left:2px solid var(--border)" class="os-cell-muted">${fmtPct(m.rev)}</td>
      <td class="os-cell-muted">${fmtPct(m.bene)}</td>
      <td class="os-cell-muted">${fmtPct(m.rate)}</td>
      <td style="border-left:2px solid var(--border)" class="${sigColor(dl.rev, 3)}">${fmtPct(dl.rev)}</td>
    </tr>`;
  }

  document.getElementById('yoyContent').innerHTML = `
    <div style="overflow-x:auto">
    <table class="os-table">
      <thead>
        <tr>
          <th>Period</th>
          <th colspan="5" style="text-align:center;color:var(--accent)">Org YoY %</th>
          <th colspan="3" style="text-align:center;color:var(--text3);border-left:2px solid var(--border)">Market YoY %</th>
          <th style="text-align:center;border-left:2px solid var(--border)">Delta</th>
        </tr>
        <tr>
          <th></th><th>Rev</th><th>Bene</th><th>Util</th><th>Rate</th><th>Mix</th>
          <th style="border-left:2px solid var(--border)">Rev</th><th>Bene</th><th>Rate</th>
          <th style="border-left:2px solid var(--border)">Rev Gap</th>
        </tr>
      </thead>
      <tbody>${rows}</tbody>
    </table>
    </div>
  `;
}

// ── Conversion ──
function renderConversion(d) {
  const conv = d.conversion || {};
  const orgConv = conv.org || {};
  const mktConv = conv.market || {};
  const years = Object.keys(orgConv).sort();

  let rows = '';
  for (const y of years) {
    const o = orgConv[y] || {};
    const m = mktConv[y] || {};
    const rel = m.conversion_ratio ? (o.conversion_ratio / m.conversion_ratio) : 0;
    rows += `<tr>
      <td>${y}</td>
      <td>${fmtN(o.intake_claims)}</td>
      <td>${fmtN(o.treatment_claims)}</td>
      <td style="font-weight:600">${o.conversion_ratio?.toFixed(2) || '—'}</td>
      <td class="os-cell-muted">${o.conversion_idx?.toFixed(3) || '—'}</td>
      <td style="border-left:2px solid var(--border)" class="os-cell-muted">${m.conversion_ratio?.toFixed(2) || '—'}</td>
      <td class="${relColor(rel)}" style="font-weight:600">${rel ? rel.toFixed(3) + 'x' : '—'}</td>
    </tr>`;
  }

  document.getElementById('conversionContent').innerHTML = `
    <div style="font-size:.8rem;color:var(--text2);margin-bottom:.75rem">
      Conversion = treatment claims / intake claims. Higher = better retention of assessed patients into ongoing care.
    </div>
    <div style="overflow-x:auto">
    <table class="os-table">
      <thead>
        <tr>
          <th>Year</th>
          <th colspan="4" style="text-align:center;color:var(--accent)">Org</th>
          <th colspan="2" style="text-align:center;color:var(--text3);border-left:2px solid var(--border)">Market</th>
        </tr>
        <tr>
          <th></th><th>Intake</th><th>Treatment</th><th>Ratio</th><th>Index</th>
          <th style="border-left:2px solid var(--border)">Ratio</th><th>Relative</th>
        </tr>
      </thead>
      <tbody>${rows}</tbody>
    </table>
    </div>
  `;
}

// ── Leakage ──
function renderLeakage(d) {
  const lk = d.leakage || {};
  const orgLk = lk.org || {};
  const years = Object.keys(orgLk).sort();
  const zips = lk.catchment_zips || [];

  if (!years.length) {
    document.getElementById('leakageContent').innerHTML = '<div style="color:var(--text3);font-size:.85rem">No ZIP catchment data available.</div>';
    return;
  }

  let rows = '';
  for (const y of years) {
    const l = orgLk[y] || {};
    rows += `<tr>
      <td>${y}</td>
      <td>${fmtN(l.org_claims)}</td>
      <td>${fmtN(l.catchment_claims)}</td>
      <td>${l.catchment_npis || '—'}</td>
      <td style="font-weight:600" class="${l.market_share_claims > 0.5 ? 'os-cell-green' : l.market_share_claims > 0.3 ? 'os-cell-yellow' : 'os-cell-red'}">${(l.market_share_claims * 100).toFixed(1)}%</td>
      <td class="os-cell-muted">${l.share_idx?.toFixed(3) || '—'}</td>
    </tr>`;
  }

  document.getElementById('leakageContent').innerHTML = `
    <div style="font-size:.8rem;color:var(--text2);margin-bottom:.75rem">
      Catchment: ${zips.length} ZIP${zips.length !== 1 ? 's' : ''} (${zips.join(', ')}). Market share = org BH claims / all BH claims by providers in those ZIPs.
    </div>
    <div style="overflow-x:auto">
    <table class="os-table">
      <thead><tr><th>Year</th><th>Org Claims</th><th>Catchment</th><th>Providers</th><th>Share in Catchment</th><th>Share Idx</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>
    </div>
  `;
}

// ── Service Mix ──
function renderMix(d) {
  const orgS = d.org?.series || [];
  if (!orgS.length) return;

  // Get base and latest year code details
  const base = orgS.find(s => s.year === (d.org?.base_year || '2020')) || orgS[0];
  const latest = orgS[orgS.length - 1];
  const baseDetail = base.code_detail || [];
  const latestDetail = latest.code_detail || [];

  // Get market for comparison
  const mktS = d.market?.series || [];
  const mktLatest = mktS[mktS.length - 1] || {};
  const mktDetail = mktLatest.code_detail || [];
  const mktByCode = Object.fromEntries(mktDetail.map(c => [c.code, c]));

  // Merge codes, sort by base weight desc
  const allCodes = new Map();
  for (const c of baseDetail) allCodes.set(c.code, { ...c, base: c });
  for (const c of latestDetail) {
    const existing = allCodes.get(c.code) || {};
    allCodes.set(c.code, { ...existing, latest: c, code: c.code, name: c.name });
  }

  const sorted = [...allCodes.values()]
    .filter(c => (c.base?.w_now || 0) > 0.005 || (c.latest?.w_now || 0) > 0.005 ||
                 (mktByCode[c.code]?.w_now || 0) > 0.01)
    .sort((a, b) => ((b.base?.w_now || 0) + (b.latest?.w_now || 0)) - ((a.base?.w_now || 0) + (a.latest?.w_now || 0)));

  let rows = '';
  for (const c of sorted.slice(0, 20)) {
    const bw = c.base?.w_now || 0;
    const lw = c.latest?.w_now || 0;
    const delta = lw - bw;
    const mc = mktByCode[c.code] || {};
    const mw = mc.w_now || 0;
    const orgRpb = c.latest?.rpb_now || 0;
    const mktRpb = mc.rpb_now || 0;
    const rpbGap = mktRpb ? ((orgRpb / mktRpb) - 1) * 100 : 0;

    rows += `<tr>
      <td>${c.code}</td>
      <td style="text-align:left;color:var(--text2)">${c.name || c.latest?.name || ''}</td>
      <td>${(bw * 100).toFixed(1)}%</td>
      <td>${(lw * 100).toFixed(1)}%</td>
      <td class="${sigColor(delta * 100, 1)}">${fmtPct(delta * 100)}</td>
      <td style="border-left:2px solid var(--border)" class="os-cell-muted">${(mw * 100).toFixed(1)}%</td>
      <td>${orgRpb ? fmt$(orgRpb) : '—'}</td>
      <td class="os-cell-muted">${mktRpb ? fmt$(mktRpb) : '—'}</td>
      <td class="${rpbGap > -10 ? '' : 'os-cell-red'}">${orgRpb && mktRpb ? fmtPct(rpbGap) : '—'}</td>
    </tr>`;
  }

  document.getElementById('mixContent').innerHTML = `
    <div style="font-size:.8rem;color:var(--text2);margin-bottom:.75rem">
      Claim share by HCPCS code: ${base.year} vs ${latest.year}. Shows which services are gaining/losing volume and how org rates compare to market.
    </div>
    <div style="overflow-x:auto">
    <table class="os-table">
      <thead>
        <tr>
          <th style="text-align:left">Code</th><th style="text-align:left">Service</th>
          <th colspan="3" style="text-align:center;color:var(--accent)">Org Claim Share</th>
          <th style="text-align:center;color:var(--text3);border-left:2px solid var(--border)">Mkt</th>
          <th colspan="3" style="text-align:center">Revenue / Bene</th>
        </tr>
        <tr>
          <th></th><th></th>
          <th>${base.year}</th><th>${latest.year}</th><th>Delta</th>
          <th style="border-left:2px solid var(--border)">${latest.year}</th>
          <th>Org</th><th>Mkt</th><th>Gap</th>
        </tr>
      </thead>
      <tbody>${rows}</tbody>
    </table>
    </div>
  `;
}

// ── Map (Leaflet) ──
let _leafletMap = null;
function renderMap(d) {
  const mm = d.micro_market;
  const el = document.getElementById('mapSection');
  if (!mm || !mm.zips || mm.zips.length === 0) { el.hidden = true; return; }
  el.hidden = false;

  // Destroy previous map if reloading
  if (_leafletMap) { _leafletMap.remove(); _leafletMap = null; }

  const zips = mm.zips;
  const orgName = (mm.org_name || d.org_name || '').toUpperCase();

  // Center on avg of org-present ZIPs, fallback to all
  const orgZips = zips.filter(z => z.org_share_pct > 0);
  const centerSet = orgZips.length ? orgZips : zips;
  const cLat = centerSet.reduce((s, z) => s + z.lat, 0) / centerSet.length;
  const cLng = centerSet.reduce((s, z) => s + z.lng, 0) / centerSet.length;

  _leafletMap = L.map('mapContainer', { scrollWheelZoom: true }).setView([cLat, cLng], 9);

  // Dark tile layer
  L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png', {
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OSM</a> &copy; <a href="https://carto.com/">CARTO</a>',
    subdomains: 'abcd', maxZoom: 19
  }).addTo(_leafletMap);

  // Scale: circle radius proportional to sqrt of paid
  const maxPaid = Math.max(...zips.map(z => z.total_paid), 1);
  const minR = 4, maxR = 28;
  function radius(paid) { return minR + (maxR - minR) * Math.sqrt(paid / maxPaid); }

  // Color by org share
  function shareColor(pct) {
    if (pct >= 50) return '#34d399';  // green — dominant
    if (pct >= 20) return '#86efac';  // light green
    if (pct >= 5)  return '#fbbf24';  // yellow — some presence
    if (pct > 0)   return '#fb923c';  // orange — minimal
    return '#f87171';                 // red — no presence
  }

  for (const z of zips) {
    if (!z.lat || !z.lng) continue;
    const color = shareColor(z.org_share_pct);
    const circle = L.circleMarker([z.lat, z.lng], {
      radius: radius(z.total_paid),
      fillColor: color,
      color: color,
      weight: 1,
      opacity: 0.8,
      fillOpacity: 0.55,
    }).addTo(_leafletMap);

    // Is top org the target org?
    const topIsOrg = z.top_org && orgName && z.top_org.toUpperCase().includes(orgName.split(' ')[0]);
    const topLabel = topIsOrg
      ? `<span style="color:#34d399">${z.top_org}</span> (this org)`
      : (z.top_org || 'Unknown');

    circle.bindPopup(`
      <div class="os-map-popup">
        <strong>${z.zip}</strong> — ${z.city || 'Unknown'}<br>
        <span style="color:var(--text3)">${z.county || ''} County</span><br><br>
        <b>Market:</b> <span class="pop-val">${fmtM(z.total_paid)}</span> paid · <span class="pop-val">${fmtN(z.total_benes)}</span> benes · ${z.n_orgs} orgs<br>
        <b>Org Share:</b> <span class="pop-val" style="color:${color}">${z.org_share_pct.toFixed(1)}%</span>
        ${z.org_paid > 0 ? ` (<span class="pop-val">${fmtM(z.org_paid)}</span>)` : ''}<br>
        <b>Top Provider:</b> ${topLabel}
      </div>
    `, { maxWidth: 320 });
  }

  // Legend
  document.getElementById('mapLegend').innerHTML = `
    <div class="os-map-legend">
      <span><span class="dot" style="background:#34d399"></span> ≥50% share</span>
      <span><span class="dot" style="background:#86efac"></span> 20–50%</span>
      <span><span class="dot" style="background:#fbbf24"></span> 5–20%</span>
      <span><span class="dot" style="background:#fb923c"></span> &lt;5%</span>
      <span><span class="dot" style="background:#f87171"></span> No presence</span>
      <span style="margin-left:1rem">Circle size = market volume</span>
    </div>
  `;
}

// ── County Competition Cards ──
function renderCounty(d) {
  const mm = d.micro_market;
  const el = document.getElementById('countyContent');
  const section = document.getElementById('countySection');
  if (!mm || !mm.org_counties || mm.org_counties.length === 0) { section.hidden = true; return; }
  section.hidden = false;

  const trends = mm.county_trends || {};
  const allComps = mm.county_competitors || {};

  let html = '';
  for (const county of mm.org_counties) {
    const ct = (trends[county] || []).sort((a, b) => a.year.localeCompare(b.year));
    const comps = allComps[county] || [];

    // Trend summary
    let trendHtml = '';
    if (ct.length >= 2) {
      const first = ct[0], last = ct[ct.length - 1];
      const mktGrowth = first.total_paid ? ((last.total_paid / first.total_paid - 1) * 100) : 0;
      const shareFirst = first.org_share_pct;
      const shareLast = last.org_share_pct;
      const shareDelta = shareLast - shareFirst;
      const shareColor = shareDelta >= 0 ? 'var(--green)' : 'var(--red)';
      const mktColor = mktGrowth >= 0 ? 'var(--green)' : 'var(--red)';
      trendHtml = `
        <div class="os-county-stats">
          <div>Market ${first.year}→${last.year}: <span class="stat-val" style="color:${mktColor}">${mktGrowth >= 0 ? '+' : ''}${mktGrowth.toFixed(0)}%</span></div>
          <div>Org Share: <span class="stat-val">${shareFirst.toFixed(1)}%</span> → <span class="stat-val" style="color:${shareColor}">${shareLast.toFixed(1)}%</span> (${shareDelta >= 0 ? '+' : ''}${shareDelta.toFixed(1)}pp)</div>
          <div>Latest Mkt: <span class="stat-val">${fmtM(last.total_paid)}</span> · ${fmtN(last.total_benes)} benes</div>
          <div>Orgs: <span class="stat-val">${last.n_orgs}</span></div>
        </div>
      `;
    } else if (ct.length === 1) {
      const yr = ct[0];
      trendHtml = `
        <div class="os-county-stats">
          <div>${yr.year}: <span class="stat-val">${fmtM(yr.total_paid)}</span></div>
          <div>Org Share: <span class="stat-val">${yr.org_share_pct.toFixed(1)}%</span></div>
          <div>Orgs: <span class="stat-val">${yr.n_orgs}</span></div>
        </div>
      `;
    }

    // Build competitor comparison: 2020 vs latest
    const years = [...new Set(comps.map(c => c.year))].sort();
    const baseYear = years.find(y => y === '2020') || years[0] || '2020';
    const latestYear = years[years.length - 1] || '2023';
    const baseComps = comps.filter(c => c.year === baseYear);
    const latestComps = comps.filter(c => c.year === latestYear);

    // Build lookup: name → {base_paid, latest_paid}
    const compMap = {};
    for (const c of baseComps) {
      compMap[c.name] = compMap[c.name] || {};
      compMap[c.name].base = c.paid;
      compMap[c.name].base_benes = c.benes;
    }
    for (const c of latestComps) {
      compMap[c.name] = compMap[c.name] || {};
      compMap[c.name].latest = c.paid;
      compMap[c.name].latest_benes = c.benes;
    }

    // Sort by latest paid desc, take top 8
    const compList = Object.entries(compMap)
      .map(([name, v]) => ({
        name,
        base: v.base || 0,
        latest: v.latest || 0,
        base_benes: v.base_benes || 0,
        latest_benes: v.latest_benes || 0,
        isNew: !v.base && v.latest > 0,
        isGone: v.base > 0 && !v.latest,
        growth: v.base ? ((v.latest || 0) / v.base - 1) * 100 : null,
      }))
      .sort((a, b) => b.latest - a.latest)
      .slice(0, 8);

    let compHtml = '';
    if (compList.length) {
      const countyLatestPaid = ct.length ? ct[ct.length - 1].total_paid : 1;
      compHtml = `
        <div class="os-county-competitors">
          <div class="comp-row" style="font-size:.7rem;color:var(--text3);border-bottom:1px solid var(--border);padding-bottom:.35rem;margin-bottom:.2rem">
            <span>Competitor</span>
            <span style="display:flex;gap:1.5rem">
              <span style="width:70px;text-align:right">${baseYear}</span>
              <span style="width:70px;text-align:right">${latestYear}</span>
              <span style="width:70px;text-align:right">Change</span>
              <span style="width:50px;text-align:right">Share</span>
            </span>
          </div>`;
      for (const c of compList) {
        const share = countyLatestPaid ? (c.latest / countyLatestPaid * 100).toFixed(1) : '?';
        let tag = '';
        let changeStr = '';
        if (c.isNew) {
          tag = '<span style="color:var(--yellow);font-size:.65rem;margin-left:.3rem">NEW</span>';
          changeStr = '<span style="color:var(--yellow)">new</span>';
        } else if (c.isGone) {
          tag = '<span style="color:var(--text3);font-size:.65rem;margin-left:.3rem">EXITED</span>';
          changeStr = '<span style="color:var(--text3)">exited</span>';
        } else if (c.growth !== null) {
          const gColor = c.growth > 0 ? 'var(--green)' : 'var(--red)';
          changeStr = `<span style="color:${gColor}">${c.growth >= 0 ? '+' : ''}${c.growth.toFixed(0)}%</span>`;
        }
        compHtml += `
          <div class="comp-row">
            <span>${c.name || '?'}${tag}</span>
            <span style="display:flex;gap:1.5rem;font-family:'JetBrains Mono',monospace;font-size:.75rem">
              <span style="width:70px;text-align:right;color:var(--text3)">${c.base ? fmtM(c.base) : '—'}</span>
              <span style="width:70px;text-align:right">${c.latest ? fmtM(c.latest) : '—'}</span>
              <span style="width:70px;text-align:right">${changeStr}</span>
              <span style="width:50px;text-align:right">${share}%</span>
            </span>
          </div>`;
      }
      compHtml += '</div>';
    }

    html += `
      <div class="os-county-card">
        <h4>${county}</h4>
        ${trendHtml}
        ${compHtml}
      </div>
    `;
  }

  el.innerHTML = html;
}

// ── Init ──
loadOrgs();
