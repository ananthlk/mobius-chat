/* ── FL BH Market Map ─────────────────────────────────────────────────── */
const API = window.location.origin;
let _map = null;
let _markers = [];
let _allOrgs = [];

function fmtM(n) { return n == null ? '—' : '$' + (n / 1e6).toFixed(2) + 'M'; }
function fmtK(n) { return n == null ? '—' : '$' + (n / 1e3).toFixed(0) + 'K'; }
function fmtRev(n) { if (!n) return '$0'; return n >= 1e6 ? fmtM(n) : fmtK(n); }
function fmtN(n) { return n == null ? '—' : Number(n).toLocaleString(); }

// ── Color by revenue tier ──
function revColor(rev) {
  if (rev >= 1e6) return '#818cf8';   // purple — major
  if (rev >= 5e5) return '#60a5fa';   // blue
  if (rev >= 1e5) return '#34d399';   // green
  if (rev >= 1e4) return '#fbbf24';   // yellow
  return '#94a3b8';                   // gray — small
}

// ── Radius by revenue ──
function revRadius(rev, maxRev) {
  const minR = 5, maxR = 30;
  return minR + (maxR - minR) * Math.sqrt((rev || 0) / (maxRev || 1));
}

// ── Init map ──
function initMap() {
  _map = L.map('mapContainer', { scrollWheelZoom: true, zoomControl: true })
    .setView([27.8, -81.8], 7);  // Center on FL

  L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png', {
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OSM</a> &copy; <a href="https://carto.com/">CARTO</a>',
    subdomains: 'abcd', maxZoom: 19
  }).addTo(_map);
}

// ── Load data and plot ──
async function loadMarketData() {
  try {
    const r = await fetch(`${API}/chat/market-map`);
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const data = await r.json();
    _allOrgs = data.orgs || [];
    plotOrgs(_allOrgs);
    document.getElementById('mapLoading').style.display = 'none';
  } catch (e) {
    document.getElementById('mapLoading').textContent = 'Failed to load market data: ' + e.message;
    console.error('Market map load error:', e);
  }
}

// ── Plot org markers ──
function plotOrgs(orgs) {
  // Clear existing
  _markers.forEach(m => _map.removeLayer(m));
  _markers = [];

  if (!orgs.length) return;
  const maxRev = Math.max(...orgs.map(o => o.revenue || 0), 1);

  for (const org of orgs) {
    if (!org.lat || !org.lng) continue;
    const color = revColor(org.revenue || 0);
    const marker = L.circleMarker([org.lat, org.lng], {
      radius: revRadius(org.revenue || 0, maxRev),
      fillColor: color,
      color: color,
      weight: 1.5,
      opacity: 0.85,
      fillOpacity: 0.5,
    }).addTo(_map);

    // Store org data on marker
    marker._orgData = org;

    marker.on('click', () => openSidebar(org));
    marker.on('mouseover', function() {
      this.setStyle({ weight: 3, fillOpacity: 0.8 });
      this.bindTooltip(org.org_name, {
        direction: 'top', className: 'os-map-popup', offset: [0, -8]
      }).openTooltip();
    });
    marker.on('mouseout', function() {
      this.setStyle({ weight: 1.5, fillOpacity: 0.5 });
      this.closeTooltip();
    });

    _markers.push(marker);
  }
}

// ── Sidebar ──
function openSidebar(org) {
  const sb = document.getElementById('sidebar');
  document.getElementById('sidebarTitle').textContent = org.org_name;

  const body = document.getElementById('sidebarBody');
  body.innerHTML = `
    <div class="mm-stat"><span class="mm-stat-label">County</span><span class="mm-stat-val">${org.county || '—'}</span></div>
    <div class="mm-stat"><span class="mm-stat-label">ZIP</span><span class="mm-stat-val">${org.zip5 || '—'}</span></div>
    <div class="mm-stat"><span class="mm-stat-label">Revenue (2023)</span><span class="mm-stat-val">${fmtRev(org.revenue)}</span></div>
    <div class="mm-stat"><span class="mm-stat-label">Claims</span><span class="mm-stat-val">${fmtN(org.claims)}</span></div>
    <div class="mm-stat"><span class="mm-stat-label">Beneficiaries</span><span class="mm-stat-val">${fmtN(org.benes)}</span></div>
    <div class="mm-stat"><span class="mm-stat-label">ZIPs Served</span><span class="mm-stat-val">${org.n_zips || '—'}</span></div>
    <a class="mm-nav-btn" href="/org-story?org=${encodeURIComponent(org.org_name)}">
      View Revenue Story &rarr;
    </a>
  `;

  sb.classList.add('open');
}

function closeSidebar() {
  document.getElementById('sidebar').classList.remove('open');
}

// ── Search ──
document.getElementById('orgSearch').addEventListener('input', function() {
  const q = this.value.toLowerCase().trim();
  if (!q) {
    // Show all
    _markers.forEach(m => {
      m.setStyle({ opacity: 0.85, fillOpacity: 0.5 });
      _map.addLayer(m);
    });
    return;
  }

  _markers.forEach(m => {
    const name = (m._orgData.org_name || '').toLowerCase();
    const county = (m._orgData.county || '').toLowerCase();
    const match = name.includes(q) || county.includes(q);
    if (match) {
      m.setStyle({ opacity: 0.85, fillOpacity: 0.5 });
      if (!_map.hasLayer(m)) _map.addLayer(m);
    } else {
      m.setStyle({ opacity: 0.15, fillOpacity: 0.08 });
    }
  });

  // If few matches, zoom to fit them
  const matches = _markers.filter(m => {
    const name = (m._orgData.org_name || '').toLowerCase();
    const county = (m._orgData.county || '').toLowerCase();
    return name.includes(q) || county.includes(q);
  });
  if (matches.length === 1) {
    _map.setView(matches[0].getLatLng(), 12);
    openSidebar(matches[0]._orgData);
  } else if (matches.length > 1 && matches.length <= 20) {
    const group = L.featureGroup(matches);
    _map.fitBounds(group.getBounds().pad(0.2));
    // Open sidebar for the highest-revenue match
    const best = matches.reduce((a, b) => (a._orgData.revenue || 0) > (b._orgData.revenue || 0) ? a : b);
    openSidebar(best._orgData);
  }
});

// ── Auto-load org from URL param ──
function checkUrlParam() {
  const params = new URLSearchParams(window.location.search);
  const orgName = params.get('org');
  if (orgName) {
    // Wait for data to load, then highlight
    const check = setInterval(() => {
      if (_allOrgs.length) {
        clearInterval(check);
        const match = _markers.find(m =>
          m._orgData.org_name.toLowerCase().includes(orgName.toLowerCase())
        );
        if (match) {
          _map.setView(match.getLatLng(), 11);
          openSidebar(match._orgData);
          match.setStyle({ weight: 4, fillOpacity: 0.9 });
        }
      }
    }, 200);
  }
}

// ── Boot ──
initMap();
loadMarketData();
checkUrlParam();
