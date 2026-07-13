/* ══════════════════════════════════════════════════════════════════
   Mobius My Vault — mountable panel component.
   Owned by the Vault agent (repo: mobius-vault).

   Chat (or any host) loads this one file and "pings" it:

       <script src="/static/vault-panel.js"></script>
       MobiusVault.open();            // slide-in drawer; sidebar stays visible
       MobiusVault.close();
       MobiusVault.open({ tab: "uploads" });

   The component owns its own drawer chrome, styles, data fetching, and
   actions — no host layout knowledge, no CSS bleed (styles are scoped under
   .mv-root and injected once). Right-anchored drawer, so a left sidebar
   remains visible automatically.

   Config (all optional):
     apiBase   base for /chat/* calls; default window.MOBIUS_CHAT_API_BASE || "" (same-origin)
     token     bearer token; default fragment #t= then localStorage("mobius.auth.accessToken")
     tab       initial tab: recent | liked | tasks | uploads (default uploads)
   ══════════════════════════════════════════════════════════════════ */
(function () {
  "use strict";
  if (window.MobiusVault) return; // singleton

  // ── config / auth ────────────────────────────────────────────────
  const cfg = { apiBase: "", token: null, tab: "uploads" };
  const TOKEN_KEY = "mobius.auth.accessToken";
  function resolveApiBase() {
    return (cfg.apiBase || window.MOBIUS_CHAT_API_BASE || "").replace(/\/$/, "");
  }
  function captureFragmentToken() {
    try {
      const m = /[#&]t=([^&]+)/.exec(location.hash || "");
      if (m) {
        const tok = decodeURIComponent(m[1]);
        try { localStorage.setItem(TOKEN_KEY, tok); } catch { /* ignore */ }
        history.replaceState(null, "", location.pathname + location.search);
        return tok;
      }
    } catch { /* ignore */ }
    return null;
  }
  let _fragTok = captureFragmentToken();
  function token() {
    if (cfg.token) return cfg.token;
    try { return _fragTok || localStorage.getItem(TOKEN_KEY); } catch { return _fragTok; }
  }
  async function authFetch(path, init = {}) {
    const t = token();
    const headers = Object.assign({}, init.headers || {}, t ? { Authorization: "Bearer " + t } : {});
    return fetch(resolveApiBase() + path, Object.assign({}, init, { headers }));
  }
  function chatOrigin() { return resolveApiBase() || ""; }

  // ── state ────────────────────────────────────────────────────────
  const state = {
    me: null, tab: "uploads", preview: false, open: false,
    recent: [], liked: [], tasksWork: [], tasksNotif: [], tasksOrg: [], uploads: [],
    uSort: { key: "ttl", dir: "asc" }, showExpired: false, search: "",
    uFilter: null, tFilter: null, loaded: false, docked: false,
  };
  const EXPIRING_DAYS = 3;
  function clearFilters() { state.uFilter = null; state.tFilter = null; }

  const TABS = [
    { key: "recent", ico: "🕘", label: "Recent" },
    { key: "liked", ico: "★", label: "Liked" },
    { key: "tasks", ico: "✓", label: "Tasks" },
    { key: "uploads", ico: "📄", label: "Uploads" },
  ];
  const SOON = ["Bookmarks", "Saved reports", "My feedback"];

  // ── dom helpers ──────────────────────────────────────────────────
  let root = null; // .mv-root overlay
  const el = (tag, cls, txt) => { const e = document.createElement(tag); if (cls) e.className = cls; if (txt != null) e.textContent = txt; return e; };
  const q = (sel) => root && root.querySelector(sel);
  const qa = (sel) => root ? [...root.querySelectorAll(sel)] : [];
  const esc = (s) => (s == null ? "" : String(s));
  const snippet = (s, n = 90) => { s = esc(s).trim(); return s.length > n ? s.slice(0, n) + "…" : s; };

  function fmtDate(iso) {
    if (!iso) return "";
    const d = new Date(iso); if (isNaN(d)) return "";
    const ms = d - new Date(); const past = ms < 0; const a = Math.abs(ms); const day = 864e5;
    const rel = (n, u) => past ? `${n}${u} ago` : `in ${n}${u}`;
    if (a >= 7 * day) return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
    if (a >= day) return rel(Math.round(a / day), "d");
    if (a >= 36e5) return rel(Math.round(a / 36e5), "h");
    return rel(Math.max(1, Math.round(a / 6e4)), "m");
  }
  function ttlOf(u) {
    if (!u.expires_at) return { label: "—", cls: "", sortVal: Infinity };
    const exp = new Date(u.expires_at); if (isNaN(exp)) return { label: "—", cls: "", sortVal: Infinity };
    const ms = exp - new Date(); const day = 864e5;
    if (ms <= 0) return { label: "Expired " + Math.max(1, Math.round(-ms / day)) + "d ago", cls: "gone", sortVal: ms };
    const days = ms / day;
    if (days < 1) return { label: Math.max(1, Math.round(ms / 36e5)) + "h left", cls: "soon", sortVal: ms };
    return { label: Math.round(days) + "d left", cls: days <= 1.5 ? "soon" : "", sortVal: ms };
  }
  const isExpired = (u) => u.status === "expired" || (u.expires_at && new Date(u.expires_at) <= new Date());
  function daysLeft(u) { if (!u.expires_at) return Infinity; return (new Date(u.expires_at) - new Date()) / 864e5; }
  function expiringSoonList() { return state.uploads.filter(u => u.status !== "discarded" && !isExpired(u) && daysLeft(u) <= EXPIRING_DAYS); }
  function dueTodayList() {
    const end = new Date(); end.setHours(23, 59, 59, 999);
    return [...state.tasksWork, ...state.tasksOrg].filter(t => { const d = t.deadline || t.due_at; return d && new Date(d) <= end; });
  }

  let toastTimer = null;
  function toast(msg) { const t = q(".mv-toast"); if (!t) return; t.textContent = msg; t.classList.add("show"); clearTimeout(toastTimer); toastTimer = setTimeout(() => t.classList.remove("show"), 3200); }

  // In-panel confirm (window.confirm is blocked inside iframes and is jarring here).
  function confirmDialog(message) {
    return new Promise((resolve) => {
      const back = el("div", "mv-confirm-back");
      const box = el("div", "mv-confirm");
      box.appendChild(el("p", "mv-confirm-msg", message));
      const row = el("div", "mv-confirm-row");
      const cancel = el("button", "mv-btn mv-btn-ghost", "Cancel");
      const ok = el("button", "mv-btn mv-btn-danger", "Remove");
      row.appendChild(cancel); row.appendChild(ok); box.appendChild(row); back.appendChild(box); root.appendChild(back);
      const done = (v) => { back.remove(); resolve(v); };
      cancel.addEventListener("click", () => done(false));
      ok.addEventListener("click", () => done(true));
      back.addEventListener("click", (e) => { if (e.target === back) done(false); });
    });
  }

  function openThread(threadId) {
    if (!threadId) { toast("No linked conversation for this item."); return; }
    window.location.href = chatOrigin() + "/?thread=" + encodeURIComponent(threadId);
  }

  // ── data load ────────────────────────────────────────────────────
  async function loadAll() {
    try { const r = await authFetch("/chat/whoami"); if (r.ok) { const d = await r.json(); if (d.ok && d.user) state.me = d.user; } } catch { /* unknown */ }
    if (!state.me || !token()) { loadPreview(); return; }
    const uid = state.me.user_id, aref = state.me.assignee_ref;
    const orgName = (state.me.org_memberships && state.me.org_memberships[0] && state.me.org_memberships[0].display_name) || null;
    const jobs = [
      authFetch("/chat/history/threads?limit=25").then(r => r.ok ? r.json() : []).then(d => state.recent = arr(d, "threads")).catch(() => {}),
      authFetch("/chat/history/most-helpful-searches?limit=25").then(r => r.ok ? r.json() : []).then(d => state.liked = arr(d)).catch(() => {}),
      authFetch("/chat/uploads?user_id=" + encodeURIComponent(uid) + "&include_inactive=true&limit=200").then(r => r.ok ? r.json() : {}).then(d => state.uploads = arr(d, "uploads")).catch(() => {}),
    ];
    if (aref) jobs.push(authFetch("/chat/tasks?status=open&assignee=" + encodeURIComponent(aref) + "&limit=100").then(r => r.ok ? r.json() : {}).then(d => bucketTasks(arr(d, "tasks"))).catch(() => {}));
    if (orgName) jobs.push(authFetch("/chat/tasks?status=open&org_name=" + encodeURIComponent(orgName) + "&limit=100").then(r => r.ok ? r.json() : {}).then(d => state.tasksOrg = arr(d, "tasks").filter(t => (t.kind || "work_item") === "work_item")).catch(() => {}));
    await Promise.all(jobs);
    state.loaded = true; renderAll();
  }
  function arr(d, key) {
    if (Array.isArray(d)) return d;
    if (d && key && Array.isArray(d[key])) return d[key];
    if (d && Array.isArray(d.tasks)) return d.tasks;
    return [];
  }
  function bucketTasks(tasks) {
    state.tasksWork = tasks.filter(t => (t.kind || "work_item") === "work_item");
    state.tasksNotif = tasks.filter(t => t.kind === "notification" || t.kind === "reminder");
  }

  // ── render ───────────────────────────────────────────────────────
  function counts() {
    return {
      recent: state.recent.length, liked: state.liked.length,
      tasks: state.tasksWork.length + state.tasksOrg.length + state.tasksNotif.length,
      uploads: state.uploads.filter(u => !isExpired(u) && u.status !== "discarded").length,
    };
  }

  function renderUrgency() {
    const strip = q(".mv-urgency"); if (!strip) return; strip.innerHTML = "";
    const exp = expiringSoonList().length, due = dueTodayList().length;
    if (!exp && !due) { strip.classList.remove("show"); return; }
    strip.classList.add("show");
    const line = el("span", "mv-urgency-line");
    line.appendChild(el("span", "mv-urgency-ico", "⚠"));
    if (exp) { const b = el("button", "mv-urgency-seg", exp + " document" + (exp > 1 ? "s" : "") + " expiring soon"); b.addEventListener("click", () => { clearFilters(); state.uFilter = "expiring"; switchTab("uploads"); }); line.appendChild(b); }
    if (exp && due) line.appendChild(el("span", "mv-urgency-sep", "·"));
    if (due) { const b = el("button", "mv-urgency-seg", due + " task" + (due > 1 ? "s" : "") + " due today"); b.addEventListener("click", () => { clearFilters(); state.tFilter = "dueSoon"; switchTab("tasks"); }); line.appendChild(b); }
    strip.appendChild(line);
  }

  function renderRail() {
    const c = counts(); const rail = q(".mv-rail"); if (!rail) return; rail.innerHTML = "";
    for (const t of TABS) {
      const item = el("button", "mv-rail-item" + (state.tab === t.key ? " active" : ""));
      item.appendChild(el("span", "mv-rail-ico", t.ico));
      item.appendChild(el("span", "mv-rail-label", t.label));
      const n = c[t.key] || 0; if (n) item.appendChild(el("span", "mv-rail-count", String(n)));
      item.addEventListener("click", () => { clearFilters(); switchTab(t.key); });
      rail.appendChild(item);
    }
    rail.appendChild(el("div", "mv-rail-sep", "Coming soon"));
    for (const label of SOON) { const item = el("button", "mv-rail-item is-soon"); item.appendChild(el("span", "mv-rail-ico", "○")); item.appendChild(el("span", "mv-rail-label", label)); item.disabled = true; rail.appendChild(item); }
  }

  function emptyState(copy, chipLabel) {
    const wrap = el("div", "mv-empty"); wrap.appendChild(el("div", "mv-empty-copy", copy));
    if (chipLabel) { const a = el("button", "mv-empty-chip", chipLabel); a.addEventListener("click", () => { close(); }); wrap.appendChild(a); }
    return wrap;
  }
  const matchesSearch = (text) => !state.search || esc(text).toLowerCase().includes(state.search.toLowerCase());

  function renderRecent() {
    const p = q('[data-panel="recent"]'); if (!p) return; p.innerHTML = "";
    const rows = state.recent.filter(t => matchesSearch(t.summary || t.title));
    if (!rows.length) { p.appendChild(emptyState("No recent searches — start a conversation.", "New chat →")); return; }
    const ul = el("ul", "mv-row-list");
    for (const th of rows) {
      const label = (th.summary && th.summary.trim()) || th.title || "Untitled chat";
      const li = el("li", "mv-row"); li.appendChild(el("span", "mv-row-ico", "🕘"));
      const main = el("div", "mv-row-main"); main.appendChild(el("div", "mv-row-title", snippet(label)));
      const meta = el("div", "mv-row-meta"); meta.appendChild(el("span", null, fmtDate(th.updated_at)));
      if (th.turn_count > 1) meta.appendChild(el("span", null, th.turn_count + " turns"));
      main.appendChild(meta); li.appendChild(main);
      li.addEventListener("click", () => openThread(th.thread_id)); ul.appendChild(li);
    }
    p.appendChild(ul);
  }
  function renderLiked() {
    const p = q('[data-panel="liked"]'); if (!p) return; p.innerHTML = "";
    const rows = state.liked.filter(t => matchesSearch(t.question));
    if (!rows.length) { p.appendChild(emptyState("Nothing liked yet — give a thumbs up on any answer to save it here.")); return; }
    const ul = el("ul", "mv-row-list");
    for (const t of rows) {
      const li = el("li", "mv-row"); li.appendChild(el("span", "mv-row-ico", "★"));
      const main = el("div", "mv-row-main"); main.appendChild(el("div", "mv-row-title", snippet(t.question || "(empty)")));
      const meta = el("div", "mv-row-meta"); meta.appendChild(el("span", null, "👍 " + fmtDate(t.created_at)));
      main.appendChild(meta); li.appendChild(main);
      li.addEventListener("click", () => openThread(t.thread_id)); ul.appendChild(li);
    }
    p.appendChild(ul);
  }
  function renderTasks() {
    const p = q('[data-panel="tasks"]'); if (!p) return; p.innerHTML = "";
    if (state.tFilter === "dueSoon") {
      const bar = el("div", "mv-toolbar"); const chip = el("button", "mv-active-filter", "Due soon ✕");
      chip.addEventListener("click", () => { state.tFilter = null; renderAll(); }); bar.appendChild(chip); p.appendChild(bar);
    }
    const total = state.tasksWork.length + state.tasksOrg.length + state.tasksNotif.length;
    if (!total) { p.appendChild(emptyState("All clear — no open tasks.")); return; }
    const dueIds = state.tFilter === "dueSoon" ? new Set(dueTodayList().map(t => t.task_id || t.id)) : null;
    const group = (title, tasks, cls) => {
      if (dueIds && cls === "notifications") return;
      let rows = tasks.filter(t => matchesSearch((t.title || "") + " " + (t.body || "")));
      if (dueIds) rows = rows.filter(t => dueIds.has(t.task_id || t.id));
      if (!rows.length) return;
      const g = el("div", "mv-task-group" + (cls ? " " + cls : "")); g.appendChild(el("h4", "mv-task-group-title", title));
      const ul = el("ul", "mv-row-list"); for (const t of rows) ul.appendChild(taskRow(t, cls === "notifications")); g.appendChild(ul); p.appendChild(g);
    };
    group("Assigned to me", state.tasksWork);
    group("My org's open tasks", state.tasksOrg);
    group("Notifications", state.tasksNotif, "notifications");
  }
  function taskRow(t, isNotif) {
    const li = el("li", "mv-row"); li.appendChild(el("span", "mv-row-ico", isNotif ? "🔔" : "✓"));
    const main = el("div", "mv-row-main"); main.appendChild(el("div", "mv-row-title", snippet(t.title || t.body || "(untitled task)")));
    const meta = el("div", "mv-row-meta");
    if (t.severity) meta.appendChild(el("span", "mv-sev " + esc(t.severity).toLowerCase(), t.severity));
    if (t.type) meta.appendChild(el("span", null, t.type));
    const due = t.deadline || t.due_at; if (due) meta.appendChild(el("span", null, "due " + fmtDate(due)));
    if (t.org_name) meta.appendChild(el("span", null, t.org_name));
    main.appendChild(meta); li.appendChild(main);
    const threadId = (t.extra && t.extra.origin && t.extra.origin.thread_id) || (t.detail_payload && t.detail_payload.thread_id) || null;
    if (threadId) { li.style.cursor = "pointer"; li.addEventListener("click", (e) => { if (!e.target.closest(".mv-actions")) openThread(threadId); }); }
    const actions = el("div", "mv-actions");
    if (isNotif) actions.appendChild(iconBtn("✕", "Dismiss", () => taskAction(t, "dismiss")));
    else actions.appendChild(iconBtn("✓", "Resolve", () => taskAction(t, "resolve")));
    li.appendChild(actions); return li;
  }
  async function taskAction(t, action) {
    const id = t.task_id || t.id; if (!id) return;
    const who = "vault:" + (state.me && state.me.user_id || "unknown");
    const bodyKey = action === "resolve" ? "resolved_by" : "dismissed_by";
    try {
      const r = await authFetch("/chat/tasks/" + encodeURIComponent(id) + "/" + action, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ [bodyKey]: who }) });
      if (r.ok) {
        state.tasksWork = state.tasksWork.filter(x => (x.task_id || x.id) !== id);
        state.tasksOrg = state.tasksOrg.filter(x => (x.task_id || x.id) !== id);
        state.tasksNotif = state.tasksNotif.filter(x => (x.task_id || x.id) !== id);
        renderAll(); toast(action === "resolve" ? "Task resolved." : "Dismissed.");
      } else toast("Couldn't " + action + " — try again.");
    } catch { toast("Couldn't " + action + " — network error."); }
  }

  function renderUploads() {
    const p = q('[data-panel="uploads"]'); if (!p) return; p.innerHTML = "";
    const bar = el("div", "mv-toolbar");
    if (state.uFilter === "expiring") { const chip = el("button", "mv-active-filter", "Expiring soon ✕"); chip.addEventListener("click", () => { state.uFilter = null; renderAll(); }); bar.appendChild(chip); }
    const toggle = el("button", "mv-chip" + (state.showExpired ? " on" : ""), (state.showExpired ? "✓ " : "") + "Show expired");
    toggle.addEventListener("click", () => { state.showExpired = !state.showExpired; renderUploads(); });
    bar.appendChild(toggle); p.appendChild(bar);

    let rows = state.uploads.filter(u => u.status !== "discarded" && matchesSearch(u.filename));
    if (state.uFilter === "expiring") rows = rows.filter(u => !isExpired(u) && daysLeft(u) <= EXPIRING_DAYS);
    const live = rows.filter(u => !isExpired(u)); const expired = rows.filter(isExpired);
    if (!live.length && !expired.length) { p.appendChild(emptyState("No documents yet — upload a file in chat to add it to your Vault. Documents stay for 7 days.", "Start a chat →")); return; }
    sortUploads(live);
    const table = el("table", "mv-table"); const thead = el("thead"); const htr = el("tr");
    const cols = [{ k: "", l: "Status" }, { k: "filename", l: "Filename" }, { k: "created_at", l: "Uploaded", s: true }, { k: "last_queried_at", l: "Last used", s: true }, { k: "ttl", l: "TTL", s: true }, { k: "vis", l: "Visibility" }, { k: "", l: "" }];
    for (const c of cols) {
      const th = el("th", c.s ? "sortable" : null, c.l);
      if (c.s) { if (state.uSort.key === c.k) th.appendChild(el("span", "mv-sort", state.uSort.dir === "asc" ? " ▲" : " ▼")); th.addEventListener("click", () => { if (state.uSort.key === c.k) state.uSort.dir = state.uSort.dir === "asc" ? "desc" : "asc"; else state.uSort = { key: c.k, dir: "asc" }; renderUploads(); }); }
      htr.appendChild(th);
    }
    thead.appendChild(htr); table.appendChild(thead);
    const tbody = el("tbody"); for (const u of live) tbody.appendChild(uploadRow(u, false));
    if (state.showExpired) for (const u of expired) tbody.appendChild(uploadRow(u, true));
    table.appendChild(tbody);
    const scroll = el("div", "mv-table-scroll"); scroll.appendChild(table); p.appendChild(scroll);
  }
  function sortUploads(rows) {
    const { key, dir } = state.uSort; const sign = dir === "asc" ? 1 : -1;
    rows.sort((a, b) => {
      let av, bv;
      if (key === "ttl") { av = ttlOf(a).sortVal; bv = ttlOf(b).sortVal; }
      else if (key === "filename") { av = esc(a.filename).toLowerCase(); bv = esc(b.filename).toLowerCase(); }
      else { av = a[key] ? new Date(a[key]).getTime() : 0; bv = b[key] ? new Date(b[key]).getTime() : 0; }
      return av < bv ? -sign : av > bv ? sign : 0;
    });
  }
  const PROCESSING = new Set(["processing", "indexing", "pending", "uploading", "queued", "extracting", "chunking", "embedding", "publishing"]);
  function humanBytes(n) {
    if (n == null || n === "" || isNaN(n)) return "";
    n = Number(n); const u = ["B", "KB", "MB", "GB"]; let i = 0;
    while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
    return (i ? n.toFixed(n < 10 ? 1 : 0) : n) + " " + u[i];
  }
  function uploadRow(u, expired) {
    const tr = el("tr", expired ? "is-expired" : "");
    const indexing = !expired && PROCESSING.has(String(u.status || "").toLowerCase());
    const failed = !expired && String(u.status || "").toLowerCase() === "failed";
    const statusClass = expired ? "expired" : failed ? "failed" : indexing ? "processing" : "ready";
    const statusLabel = expired ? "Expired" : failed ? "Failed" : indexing ? "Indexing" : "Ready";
    // Status chip (UX P1): replaces the bare dot with a readable colored chip.
    const tdStatus = el("td"); tdStatus.appendChild(el("span", "mv-status " + statusClass, statusLabel)); tr.appendChild(tdStatus);
    const tdName = el("td");
    const name = el("span", "mv-name", u.filename || "(unnamed)");
    name.title = (u.filename || "") + (u.byte_size ? " · " + humanBytes(u.byte_size) : "");
    tdName.appendChild(name);
    if (failed) { const err = el("div", "mv-name-err", "couldn't be read" + (u.error ? " — " + u.error : " — may be password-protected or corrupt")); tdName.appendChild(err); }
    const tags = [u.confirmed_payer || u.suggested_payer, u.confirmed_state || u.suggested_state, u.confirmed_program || u.suggested_program].filter(Boolean);
    if (tags.length) { const t = el("span", "mv-tag", tags.join(" · ")); tdName.appendChild(t); }
    const sz = humanBytes(u.byte_size); if (sz) tdName.appendChild(el("span", "mv-size", sz));  // Size inline-dim (UX left placement to me)
    tr.appendChild(tdName);
    tr.appendChild(el("td", null, fmtDate(u.created_at) || "—"));
    tr.appendChild(el("td", null, u.last_queried_at ? fmtDate(u.last_queried_at) : "—"));
    const ttl = ttlOf(u); tr.appendChild(el("td", "mv-ttl " + ttl.cls, ttl.label));
    const tdVis = el("td"); tdVis.appendChild(el("span", "mv-vis", "Private")); tr.appendChild(tdVis);
    const tdAct = el("td"); const acts = el("div", "mv-actions");
    if (!expired) {
      if (u.thread_id) acts.appendChild(iconBtn("↗", "Open conversation", () => openThread(u.thread_id), "", indexing));
      acts.appendChild(iconBtn("⤓", "Download original", () => downloadUpload(u), "", indexing || failed));
      acts.appendChild(iconBtn("＋", "Extend 7 days", () => extendUpload(u), "", indexing));
      acts.appendChild(iconBtn("🗑", "Delete", () => deleteUpload(u), "danger"));
      const promote = iconBtn("↑", "Promote to corpus — available when org corpus is enabled", () => {}, "", true); acts.appendChild(promote);
    } else { acts.appendChild(iconBtn("🗑", "Delete", () => deleteUpload(u), "danger")); }
    tdAct.appendChild(acts); tr.appendChild(tdAct); return tr;
  }
  function iconBtn(glyph, tip, fn, extra, disabled) {
    const b = el("button", "mv-icon" + (extra ? " " + extra : ""), glyph); b.title = tip; b.setAttribute("aria-label", tip);
    if (disabled) b.disabled = true; else b.addEventListener("click", (e) => { e.stopPropagation(); fn(); });
    return b;
  }
  async function downloadUpload(u) {
    const id = u.document_id; if (!id) return; toast("Preparing download…");
    try {
      const r = await authFetch("/chat/uploads/" + encodeURIComponent(id) + "/download");
      if (!r.ok) { toast("Download failed (" + r.status + ")."); return; }
      const blob = await r.blob(); const a = document.createElement("a"); a.href = URL.createObjectURL(blob);
      a.download = u.filename || "document"; document.body.appendChild(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(a.href), 4000);
    } catch { toast("Download failed — network error."); }
  }
  async function extendUpload(u) {
    const id = u.document_id; if (!id) return;
    try {
      const r = await authFetch("/chat/uploads/" + encodeURIComponent(id) + "/extend", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ days: 7 }) });
      if (r.ok) { const d = await r.json().catch(() => ({})); u.expires_at = d.expires_at || new Date(Date.now() + 7 * 864e5).toISOString(); renderAll(); toast("Extended 7 days."); }
      else if (r.status === 404 || r.status === 405 || r.status === 501) toast("Extend isn't live yet — Instant RAG is deploying it.");
      else toast("Couldn't extend (" + r.status + ").");
    } catch { toast("Couldn't extend — network error."); }
  }
  async function deleteUpload(u) {
    const id = u.document_id; if (!id) return;
    if (!(await confirmDialog('Remove "' + (u.filename || "this document") + '" from your Vault?'))) return;
    try {
      const r = await authFetch("/chat/uploads/" + encodeURIComponent(id), { method: "DELETE" });
      if (r.ok) { state.uploads = state.uploads.filter(x => x.document_id !== id); renderAll(); toast("Removed."); }
      else if (r.status === 404 || r.status === 405 || r.status === 501) toast("Delete isn't live yet — Instant RAG is deploying it.");
      else toast("Couldn't delete (" + r.status + ").");
    } catch { toast("Couldn't delete — network error."); }
  }

  function renderAll() {
    if (!root) return;
    renderUrgency(); renderRail(); renderRecent(); renderLiked(); renderTasks(); renderUploads();
    qa(".mv-panel-body [data-panel]").forEach(p => p.classList.toggle("active", p.dataset.panel === state.tab));
    const banner = q(".mv-preview"); if (banner) banner.style.display = state.preview ? "block" : "none";
  }
  function switchTab(key) { state.tab = key; renderAll(); }

  // ── preview data (unauthenticated) ───────────────────────────────
  function loadPreview() {
    state.preview = true;
    const iso = (d) => new Date(Date.now() + d * 864e5).toISOString();
    const todayAt = (h) => { const d = new Date(); d.setHours(h, 0, 0, 0); return d.toISOString(); };
    state.recent = [
      { thread_id: "t1", summary: "Sunshine Health prior-auth turnaround by CPT", updated_at: iso(-0.08), turn_count: 4 },
      { thread_id: "t2", title: "FL Medicaid H0018 rate vs benchmark", updated_at: iso(-1), turn_count: 2 },
      { thread_id: "t3", title: "Aetna appeal deadline for denied claim", updated_at: iso(-1.2), turn_count: 1 },
    ];
    state.liked = [
      { thread_id: "t4", question: "Explain the IOP eligibility 180-day rule", created_at: iso(-2) },
      { thread_id: "t5", question: "Peer-support billing codes for Sunshine", created_at: iso(-4) },
    ];
    state.tasksWork = [{ task_id: "k1", title: "Review MCN-7701 authorization", type: "review", kind: "work_item", severity: "high", deadline: todayAt(17), extra: { origin: { thread_id: "t1" } } }];
    state.tasksOrg = [{ task_id: "k2", title: "Brightwater credentialing blocker — NPI mismatch", type: "blocker", kind: "work_item", severity: "blocker", org_name: "Brightwater Behavioral" }];
    state.tasksNotif = [{ task_id: "n1", title: '"Aetna_policy.pdf" is ready — ask about it', kind: "notification", detail_payload: { thread_id: "t1", filename: "Aetna_policy.pdf" } }];
    state.uploads = [
      { document_id: "d1", filename: "Aetna_policy.pdf", status: "active", byte_size: 1258291, created_at: iso(-1), last_queried_at: iso(-0.1), expires_at: iso(6), thread_id: "t1", suggested_payer: "Aetna" },
      { document_id: "d2", filename: "Brightwater_intake_2026.docx", status: "active", byte_size: 84213, created_at: iso(-6), last_queried_at: iso(-5), expires_at: iso(0.6), thread_id: "t3" },
      { document_id: "d3", filename: "Sunshine_fee_schedule.pdf", status: "processing", byte_size: 522240, created_at: iso(-0.02) },
      { document_id: "d4", filename: "scan_2026_07_10.pdf", status: "failed", byte_size: 3407872, created_at: iso(-0.5), expires_at: iso(6) },
      { document_id: "d5", filename: "old_scan_2026_06.pdf", status: "expired", byte_size: 291840, created_at: iso(-10), expires_at: iso(-3) },
    ];
    state.loaded = true; renderAll();
  }

  // ── drawer chrome ────────────────────────────────────────────────
  function build() {
    if (root) return;
    injectStyles();
    // Docked mode: Chat provides #vaultPanelMount between <aside> and <main>.
    // We fill the area to the right of the sidebar (sidebar stays visible +
    // interactive, no backdrop). Standalone (demo / no mount) = right drawer.
    const mountHost = document.getElementById("vaultPanelMount");
    state.docked = !!mountHost;
    root = el("div", "mv-root" + (state.docked ? " mv-docked" : ""));
    if (state.docked) {
      const aside = mountHost.previousElementSibling;
      const asideW = aside ? Math.round(aside.getBoundingClientRect().width) : 0;
      root.style.setProperty("--mv-left", (asideW || 232) + "px");
    }
    root.innerHTML =
      '<div class="mv-backdrop"></div>' +
      '<aside class="mv-panel" role="dialog" aria-label="My Vault" aria-modal="false">' +
        // No "My Vault" title bar here — the sidebar block already labels it
        // (UX: redundant). Slim head = search + close only.
        '<header class="mv-panel-head">' +
          '<input type="search" class="mv-search" placeholder="Search your vault…" aria-label="Search vault" />' +
          '<button class="mv-close" aria-label="Close">✕</button>' +
        '</header>' +
        '<div class="mv-preview">Preview mode — not signed in, showing sample data.</div>' +
        '<div class="mv-urgency"></div>' +
        '<div class="mv-panel-body">' +
          '<nav class="mv-rail"></nav>' +
          '<main class="mv-content">' +
            '<section class="mv-tab active" data-panel="recent"></section>' +
            '<section class="mv-tab" data-panel="liked"></section>' +
            '<section class="mv-tab" data-panel="tasks"></section>' +
            '<section class="mv-tab" data-panel="uploads"></section>' +
          '</main>' +
        '</div>' +
        '<div class="mv-toast" role="status" aria-live="polite"></div>' +
      '</aside>';
    document.body.appendChild(root);
    root.querySelector(".mv-backdrop").addEventListener("click", close);
    root.querySelector(".mv-close").addEventListener("click", close);
    root.querySelector(".mv-search").addEventListener("input", (e) => { state.search = e.target.value; renderAll(); });
    document.addEventListener("keydown", onKey);
  }
  function onKey(e) { if (e.key === "Escape" && state.open) close(); }

  function open(opts) {
    opts = opts || {};
    if (opts.apiBase != null) cfg.apiBase = opts.apiBase;
    if (opts.token != null) cfg.token = opts.token;
    if (opts.tab) state.tab = opts.tab;
    build();
    state.open = true;
    void root.offsetWidth;            // force reflow so the slide-in transition plays
    root.classList.add("open");       // synchronous (rAF is throttled in background tabs)
    if (!state.loaded) { renderAll(); loadAll(); } else renderAll();
  }
  function close() { if (!root) return; state.open = false; root.classList.remove("open"); }
  function toggle(opts) { state.open ? close() : open(opts); }

  // ── styles (scoped under .mv-root) ───────────────────────────────
  function injectStyles() {
    if (document.getElementById("mv-styles")) return;
    const s = document.createElement("style"); s.id = "mv-styles";
    s.textContent = MV_CSS;
    document.head.appendChild(s);
  }
  const MV_CSS = `
.mv-root{position:fixed;inset:0;z-index:9000;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif;font-size:14px;line-height:1.45;pointer-events:none;}
.mv-root .mv-backdrop{position:absolute;inset:0;background:rgba(15,15,20,.28);opacity:0;transition:opacity .25s ease;pointer-events:none;}
.mv-root.open .mv-backdrop{opacity:1;pointer-events:auto;}
.mv-root .mv-panel{position:absolute;top:0;right:0;height:100%;width:min(900px,94vw);background:var(--mobius-bg-primary,#fafbfc);color:var(--mobius-text-primary,#1a1d21);box-shadow:-8px 0 32px rgba(0,0,0,.14);transform:translateX(100%);transition:transform .28s cubic-bezier(.4,0,.2,1);display:flex;flex-direction:column;pointer-events:auto;}
.mv-root.open .mv-panel{transform:translateX(0);}
/* Docked mode (hosted in chat, #vaultPanelMount): fill area right of the
   sidebar, no backdrop (sidebar stays interactive). display-gated so it's
   reliably visible even where CSS transitions are throttled. */
.mv-root.mv-docked{pointer-events:none;background:transparent;}
.mv-root.mv-docked .mv-backdrop{display:none;}
.mv-root.mv-docked .mv-panel{left:var(--mv-left,232px);right:0;width:auto;transform:none;display:none;box-shadow:-4px 0 24px rgba(0,0,0,.10);}
.mv-root.mv-docked.open .mv-panel{display:flex;animation:mvSlideIn .24s ease;}
@keyframes mvSlideIn{from{transform:translateX(24px);opacity:.5;}to{transform:translateX(0);opacity:1;}}
.mv-panel-head{display:flex;align-items:center;gap:12px;padding:12px 16px;border-bottom:1px solid var(--mobius-border,#e2e8f0);background:var(--mobius-bg-secondary,#f8fafc);flex-shrink:0;}
.mv-brand{font-weight:600;font-size:15px;white-space:nowrap;}
.mv-lock{font-size:11px;color:var(--mobius-text-muted,#64748b);font-weight:400;}
.mv-search{margin-left:auto;padding:6px 10px;border:1px solid var(--mobius-border-medium,#d1d5db);border-radius:8px;background:var(--mobius-bg-primary,#fff);color:inherit;font-size:13px;width:220px;max-width:38vw;}
.mv-search:focus{outline:none;border-color:var(--mobius-violet,#7C3AED);}
.mv-close{border:none;background:none;font-size:18px;line-height:1;cursor:pointer;color:var(--mobius-text-muted,#64748b);padding:4px 8px;border-radius:8px;}
.mv-close:hover{background:var(--mobius-bg-tertiary,#f1f5f9);color:var(--mobius-text-primary,#1a1d21);}
.mv-preview{display:none;padding:6px 16px;font-size:12px;background:color-mix(in srgb,var(--mobius-warning,#f59e0b) 14%,transparent);color:var(--mobius-warning,#b45309);}
.mv-urgency{display:none;padding:10px 16px 2px;}
.mv-urgency.show{display:block;}
.mv-urgency-line{display:inline-flex;align-items:center;gap:10px;font-size:13px;}
.mv-urgency-ico{color:var(--mobius-warning,#f59e0b);}
.mv-urgency-seg{background:none;border:none;padding:0;cursor:pointer;color:var(--mobius-warning,#b45309);font-size:13px;font-weight:600;font-family:inherit;}
.mv-urgency-seg:hover{text-decoration:underline;}
.mv-urgency-sep{color:var(--mobius-text-muted,#64748b);}
.mv-panel-body{flex:1;display:grid;grid-template-columns:176px 1fr;min-height:0;}
.mv-rail{padding:12px 8px;border-right:1px solid var(--mobius-border,#e2e8f0);background:var(--mobius-bg-secondary,#f8fafc);overflow:auto;}
.mv-rail-item{display:flex;align-items:center;gap:10px;width:100%;padding:9px 12px;margin-bottom:2px;border:none;background:none;border-left:3px solid transparent;border-radius:0 8px 8px 0;color:var(--mobius-text-secondary,#374151);cursor:pointer;font-size:13.5px;text-align:left;font-family:inherit;}
.mv-rail-item:hover{background:var(--mobius-bg-tertiary,#f1f5f9);}
.mv-rail-item.active{border-left-color:var(--mobius-violet,#7C3AED);color:var(--mobius-violet,#7C3AED);background:var(--mobius-bg-tertiary,#f1f5f9);font-weight:600;}
.mv-rail-ico{width:18px;text-align:center;}
.mv-rail-label{flex:1;}
.mv-rail-count{font-size:11px;padding:1px 8px;border-radius:9999px;background:var(--mobius-bg-primary,#fff);color:var(--mobius-text-muted,#64748b);border:1px solid var(--mobius-border,#e2e8f0);}
.mv-rail-item.active .mv-rail-count{color:var(--mobius-violet,#7C3AED);border-color:var(--mobius-violet,#7C3AED);}
.mv-rail-sep{margin:12px 12px 6px;font-size:10px;text-transform:uppercase;letter-spacing:.06em;color:var(--mobius-text-muted,#64748b);}
.mv-rail-item.is-soon{color:var(--mobius-text-muted,#64748b);cursor:default;}
.mv-rail-item.is-soon:hover{background:none;}
.mv-content{padding:14px 18px 32px;overflow:auto;}
.mv-tab{display:none;}
.mv-tab.active{display:block;}
.mv-toolbar{display:flex;justify-content:flex-end;gap:8px;margin-bottom:10px;}
.mv-row-list{list-style:none;margin:0;padding:0;}
.mv-row{display:flex;align-items:flex-start;gap:10px;padding:10px 12px;border:1px solid var(--mobius-border-light,#e5e7eb);border-radius:8px;margin-bottom:6px;cursor:pointer;background:var(--mobius-bg-secondary,#f8fafc);}
.mv-row:hover{border-color:var(--mobius-violet,#7C3AED);}
.mv-row-ico{flex:0 0 auto;color:var(--mobius-text-muted,#64748b);}
.mv-row-main{flex:1;min-width:0;}
.mv-row-title{color:var(--mobius-text-primary,#1a1d21);}
.mv-row-meta{font-size:12px;color:var(--mobius-text-muted,#64748b);margin-top:2px;display:flex;gap:8px;flex-wrap:wrap;align-items:center;}
.mv-table-scroll{overflow-x:auto;}
.mv-table{width:100%;min-width:640px;border-collapse:collapse;font-size:13px;}
.mv-table th{text-align:left;padding:8px 10px;border-bottom:1px solid var(--mobius-border,#e2e8f0);color:var(--mobius-text-muted,#64748b);font-size:11px;text-transform:uppercase;letter-spacing:.04em;font-weight:600;cursor:default;white-space:nowrap;}
.mv-table th.sortable{cursor:pointer;}
.mv-table th.sortable:hover{color:var(--mobius-text-primary,#1a1d21);}
.mv-sort{opacity:.6;font-size:10px;}
.mv-table td{padding:9px 10px;border-bottom:1px solid var(--mobius-border-light,#e5e7eb);vertical-align:middle;}
.mv-table tr:hover td{background:var(--mobius-bg-secondary,#f8fafc);}
.mv-name{color:var(--mobius-text-primary,#1a1d21);}
.mv-name-sub{font-size:11px;color:var(--mobius-warning,#b45309);}
.mv-name-err{font-size:11px;color:var(--mobius-error,#dc2626);}
.mv-tag{font-size:11px;padding:1px 7px;margin-left:8px;border-radius:9999px;background:var(--mobius-bg-tertiary,#f1f5f9);color:var(--mobius-text-secondary,#374151);}
.mv-status{font-size:10px;padding:1px 8px;border-radius:9999px;border:1px solid var(--mobius-border-medium,#d1d5db);white-space:nowrap;}
.mv-status.ready{color:var(--mobius-success,#16a34a);border-color:color-mix(in srgb,var(--mobius-success,#16a34a) 45%,transparent);}
.mv-status.processing{color:var(--mobius-warning,#b45309);border-color:var(--mobius-warning,#f59e0b);}
.mv-status.failed{color:var(--mobius-error,#dc2626);border-color:var(--mobius-error,#dc2626);}
.mv-status.expired{color:var(--mobius-text-muted,#64748b);}
.mv-size{font-size:11px;color:var(--mobius-text-muted,#64748b);margin-left:8px;}
.mv-ttl.soon{color:var(--mobius-warning,#b45309);font-weight:600;}
.mv-ttl.gone{color:var(--mobius-error,#dc2626);}
.mv-vis{font-size:11px;padding:1px 8px;border-radius:9999px;border:1px solid var(--mobius-border,#e2e8f0);color:var(--mobius-text-secondary,#374151);}
tr.is-expired .mv-name{text-decoration:line-through;color:var(--mobius-text-muted,#64748b);}
.mv-actions{display:flex;gap:2px;white-space:nowrap;}
.mv-icon{border:none;background:none;cursor:pointer;padding:4px 6px;border-radius:8px;color:var(--mobius-text-muted,#64748b);font-size:14px;line-height:1;font-family:inherit;}
.mv-icon:hover{background:var(--mobius-bg-tertiary,#f1f5f9);color:var(--mobius-text-primary,#1a1d21);}
.mv-icon:disabled{opacity:.35;cursor:not-allowed;}
.mv-icon.danger:hover{color:var(--mobius-error,#dc2626);}
.mv-task-group{margin-bottom:18px;}
.mv-task-group-title{font-size:12px;text-transform:uppercase;letter-spacing:.05em;color:var(--mobius-text-muted,#64748b);margin:0 0 8px;}
.mv-task-group.notifications .mv-row{background:var(--mobius-bg-tertiary,#f1f5f9);border-style:dashed;}
.mv-sev{font-size:10px;padding:1px 6px;border-radius:9999px;border:1px solid var(--mobius-border-medium,#d1d5db);color:var(--mobius-text-secondary,#374151);}
.mv-sev.blocker,.mv-sev.critical{color:var(--mobius-error,#dc2626);border-color:var(--mobius-error,#dc2626);}
.mv-sev.high{color:var(--mobius-warning,#b45309);border-color:var(--mobius-warning,#f59e0b);}
.mv-chip{font-size:12px;padding:4px 10px;border-radius:9999px;border:1px solid var(--mobius-border-medium,#d1d5db);background:var(--mobius-bg-secondary,#f8fafc);color:var(--mobius-text-muted,#64748b);cursor:pointer;font-family:inherit;}
.mv-chip.on{color:var(--mobius-violet,#7C3AED);border-color:var(--mobius-violet,#7C3AED);}
.mv-active-filter{display:inline-flex;align-items:center;gap:6px;font-size:12px;padding:4px 10px;border-radius:9999px;border:1px solid var(--mobius-violet,#7C3AED);color:var(--mobius-violet,#7C3AED);background:color-mix(in srgb,var(--mobius-violet,#7C3AED) 8%,transparent);cursor:pointer;font-family:inherit;}
.mv-empty{text-align:center;padding:44px 20px;color:var(--mobius-text-muted,#64748b);}
.mv-empty-copy{margin-bottom:12px;}
.mv-empty-chip{background:none;padding:6px 14px;border-radius:8px;border:1px solid var(--mobius-violet,#7C3AED);color:var(--mobius-violet,#7C3AED);font-size:13px;cursor:pointer;font-family:inherit;}
.mv-toast{position:absolute;bottom:18px;left:50%;transform:translateX(-50%);background:var(--mobius-text-primary,#1a1d21);color:var(--mobius-bg-primary,#fff);padding:10px 18px;border-radius:8px;font-size:13px;opacity:0;pointer-events:none;transition:opacity .25s ease;max-width:80%;}
.mv-toast.show{opacity:1;}
.mv-confirm-back{position:absolute;inset:0;background:rgba(15,15,20,.3);display:flex;align-items:center;justify-content:center;z-index:5;}
.mv-confirm{background:var(--mobius-bg-primary,#fff);border-radius:12px;padding:18px 20px;max-width:340px;box-shadow:0 8px 24px rgba(0,0,0,.18);}
.mv-confirm-msg{margin:0 0 16px;}
.mv-confirm-row{display:flex;justify-content:flex-end;gap:8px;}
.mv-btn{padding:6px 14px;border-radius:8px;font-size:13px;cursor:pointer;font-family:inherit;border:1px solid var(--mobius-border-medium,#d1d5db);background:var(--mobius-bg-secondary,#f8fafc);color:inherit;}
.mv-btn-ghost:hover{background:var(--mobius-bg-tertiary,#f1f5f9);}
.mv-btn-danger{border-color:var(--mobius-error,#dc2626);color:#fff;background:var(--mobius-error,#dc2626);}
@media (max-width:640px){.mv-panel-body{grid-template-columns:56px 1fr;}.mv-rail-label,.mv-rail-sep{display:none;}.mv-search{width:120px;}}
`;

  // ── public API ───────────────────────────────────────────────────
  window.MobiusVault = { open, close, toggle, isOpen: () => state.open };
  // Chat's sidebar block ("⤢ Open" / "Manage in Vault ↗") calls this hook;
  // define it so the component mounts instead of the default new-tab fallback.
  window.mobiusOpenVaultPanel = function (tab) { open(tab ? { tab } : {}); };
})();
