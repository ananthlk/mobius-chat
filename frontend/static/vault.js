/* ══════════════════════════════════════════════════════════════════
   My Vault — personal workspace page logic.
   Owned by the Vault agent. Served same-origin by mobius-chat at /vault,
   so it reads the shared platform token from localStorage and hits the
   live /chat/* endpoints with a Bearer header.

   Data sources (all pre-existing):
     Recent  → GET /chat/history/threads
     Liked   → GET /chat/history/most-helpful-searches   (per-user thumbs)
     Tasks   → GET /chat/tasks?status=open&assignee=…     (bucket by kind)
     Uploads → GET /chat/uploads?user_id=…&include_inactive=true
   Contracts locked with Chat / Instant RAG / Task / Feedback / UX 2026-07-13.
   ══════════════════════════════════════════════════════════════════ */

// Portable auth + API base so the same file works two ways:
//   • same-origin (served by mobius-chat at /vault): API_BASE="" → relative
//     /chat/* calls; token from localStorage (shared with the chat SPA).
//   • standalone service (own origin, MOBIUS_VAULT_URL): inject
//     window.MOBIUS_CHAT_API_BASE = chat origin; token forwarded in the URL
//     fragment (#t=…) by the launching tile (never sent to servers/logs),
//     read once then stripped. Cross-origin reads need CORS on the chat API.
const API_BASE = ((typeof window !== "undefined" && window.MOBIUS_CHAT_API_BASE) || "").replace(/\/$/, "");
const TOKEN_KEY = "mobius.auth.accessToken";

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
const token = () => { try { return _fragTok || localStorage.getItem(TOKEN_KEY); } catch { return _fragTok; } };

async function authFetch(path, init = {}) {
  const t = token();
  const headers = Object.assign({}, init.headers || {}, t ? { Authorization: "Bearer " + t } : {});
  return fetch(API_BASE + path, Object.assign({}, init, { headers }));
}

// ── state ───────────────────────────────────────────────────────────
const state = {
  me: null,                 // {user_id, display_name, assignee_ref}
  tab: "recent",
  preview: false,
  recent: [], liked: [],
  tasksWork: [], tasksNotif: [], tasksOrg: [],
  uploads: [],
  uSort: { key: "ttl", dir: "asc" },   // UX default: soonest-expiring first
  showExpired: false,
  search: "",
  uFilter: null,                       // null | "expiring"  (set by urgency strip)
  tFilter: null,                       // null | "dueSoon"   (set by urgency strip)
};
const EXPIRING_DAYS = 3;               // UX: "expiring soon" = ≤ 3 days
function clearFilters() { state.uFilter = null; state.tFilter = null; }

const TABS = [
  { key: "recent",  ico: "🕘", label: "Recent" },
  { key: "liked",   ico: "★",  label: "Liked" },
  { key: "tasks",   ico: "✓",  label: "Tasks" },
  { key: "uploads", ico: "📄", label: "Uploads" },
];
const SOON = [ // ghost items — the growth roadmap (UX: left rail scales to 7+)
  "Bookmarks", "Saved reports", "My feedback",
];

// ── helpers ─────────────────────────────────────────────────────────
const $ = (sel, root = document) => root.querySelector(sel);
const el = (tag, cls, txt) => { const e = document.createElement(tag); if (cls) e.className = cls; if (txt != null) e.textContent = txt; return e; };
const esc = (s) => (s == null ? "" : String(s));
const snippet = (s, n = 90) => { s = esc(s).trim(); return s.length > n ? s.slice(0, n) + "…" : s; };

// Relative time, direction-aware: past → "… ago", future → "in …".
// Beyond a week (either direction) → absolute date.
function fmtDate(iso) {
  if (!iso) return "";
  const d = new Date(iso); if (isNaN(d)) return "";
  const ms = d - new Date();               // >0 future, <0 past
  const past = ms < 0; const a = Math.abs(ms); const day = 864e5;
  const rel = (n, unit) => past ? `${n}${unit} ago` : `in ${n}${unit}`;
  if (a >= 7 * day) return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
  if (a >= day) return rel(Math.round(a / day), "d");
  if (a >= 36e5) return rel(Math.round(a / 36e5), "h");
  return rel(Math.max(1, Math.round(a / 6e4)), "m");
}
// days-until (negative = past). Returns {label, cls, sortVal}
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

let toastTimer = null;
function toast(msg) {
  const t = $("#vaultToast"); t.textContent = msg; t.classList.add("show");
  clearTimeout(toastTimer); toastTimer = setTimeout(() => t.classList.remove("show"), 3200);
}

const CHAT_ORIGIN = API_BASE || "";   // where the chat SPA lives (same-origin when empty)
function openThread(threadId) {
  if (!threadId) { toast("No linked conversation for this item."); return; }
  // Deep-link param (Chat to honor ?thread=). Falls back to chat home.
  window.location.href = CHAT_ORIGIN + "/?thread=" + encodeURIComponent(threadId);
}

// ── load ────────────────────────────────────────────────────────────
async function loadAll() {
  // identity first — user_id gates the uploads query, assignee_ref gates tasks
  try {
    const r = await authFetch("/chat/whoami");
    if (r.ok) { const d = await r.json(); if (d.ok && d.user) state.me = d.user; }
  } catch { /* unknown identity */ }

  if (!state.me || !token()) { loadPreview(); return; }

  const uid = state.me.user_id;
  const aref = state.me.assignee_ref;
  const orgName = (state.me.org_memberships && state.me.org_memberships[0]?.display_name) || null;

  const jobs = [
    authFetch("/chat/history/threads?limit=25").then(r => r.ok ? r.json() : []).then(d => state.recent = arr(d, "threads")).catch(() => {}),
    authFetch("/chat/history/most-helpful-searches?limit=25").then(r => r.ok ? r.json() : []).then(d => state.liked = arr(d)).catch(() => {}),
    authFetch("/chat/uploads?user_id=" + encodeURIComponent(uid) + "&include_inactive=true&limit=200").then(r => r.ok ? r.json() : {}).then(d => state.uploads = arr(d, "uploads")).catch(() => {}),
  ];
  if (aref) {
    jobs.push(authFetch("/chat/tasks?status=open&assignee=" + encodeURIComponent(aref) + "&limit=100")
      .then(r => r.ok ? r.json() : {}).then(d => bucketTasks(arr(d, "tasks"))).catch(() => {}));
  }
  if (orgName) {
    jobs.push(authFetch("/chat/tasks?status=open&org_name=" + encodeURIComponent(orgName) + "&limit=100")
      .then(r => r.ok ? r.json() : {}).then(d => state.tasksOrg = arr(d, "tasks").filter(t => (t.kind || "work_item") === "work_item")).catch(() => {}));
  }
  await Promise.all(jobs);
  renderAll();
}

// unwrap {key:[...]} envelopes or bare arrays
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

// ── render: chrome (rail + summary) ─────────────────────────────────
function counts() {
  const expiringSoon = state.uploads.filter(u => !isExpired(u) && u.status !== "discarded" && ttlOf(u).cls === "soon").length;
  const activeUploads = state.uploads.filter(u => !isExpired(u) && u.status !== "discarded").length;
  const openTasks = state.tasksWork.length + state.tasksOrg.length + state.tasksNotif.length;
  return {
    recent: state.recent.length, liked: state.liked.length,
    tasks: openTasks, uploads: activeUploads, expiringSoon,
  };
}

// Uploads expiring within EXPIRING_DAYS (live, not discarded).
function expiringSoonList() {
  return state.uploads.filter(u => u.status !== "discarded" && !isExpired(u) && daysLeft(u) <= EXPIRING_DAYS);
}
function daysLeft(u) {
  if (!u.expires_at) return Infinity;
  const ms = new Date(u.expires_at) - new Date();
  return ms / 864e5;
}
// Work items (assigned + org) past due or due today.
function dueTodayList() {
  const end = new Date(); end.setHours(23, 59, 59, 999);
  return [...state.tasksWork, ...state.tasksOrg].filter(t => {
    const d = t.deadline || t.due_at;
    return d && new Date(d) <= end;
  });
}

// Conditional urgency strip (UX spec C): present ONLY when something is
// actionable; single line, warning-colored, each segment a shortcut.
function renderUrgency() {
  const strip = $("#summaryStrip"); strip.innerHTML = "";
  const exp = expiringSoonList().length;
  const due = dueTodayList().length;
  if (!exp && !due) { strip.classList.remove("show"); return; }
  strip.classList.add("show");
  const line = el("span", "urgency-line");
  line.appendChild(el("span", "urgency-ico", "⚠"));
  if (exp) {
    const b = el("button", "urgency-seg", exp + " document" + (exp > 1 ? "s" : "") + " expiring soon");
    b.addEventListener("click", () => { clearFilters(); state.uFilter = "expiring"; switchTab("uploads"); });
    line.appendChild(b);
  }
  if (exp && due) line.appendChild(el("span", "urgency-sep", "·"));
  if (due) {
    const b = el("button", "urgency-seg", due + " task" + (due > 1 ? "s" : "") + " due today");
    b.addEventListener("click", () => { clearFilters(); state.tFilter = "dueSoon"; switchTab("tasks"); });
    line.appendChild(b);
  }
  strip.appendChild(line);
}

function renderRail() {
  const c = counts();
  const rail = $("#vaultRail"); rail.innerHTML = "";
  for (const t of TABS) {
    const item = el("button", "rail-item" + (state.tab === t.key ? " active" : ""));
    item.appendChild(el("span", "rail-ico", t.ico));
    item.appendChild(el("span", "rail-label", t.label));
    const n = c[t.key] ?? 0;
    if (n) item.appendChild(el("span", "rail-count", String(n)));
    // Manual nav = fresh view: drop any urgency-applied filter.
    item.addEventListener("click", () => { clearFilters(); switchTab(t.key); });
    rail.appendChild(item);
  }
  rail.appendChild(el("div", "rail-sep", "Coming soon"));
  for (const label of SOON) {
    const item = el("button", "rail-item is-soon");
    item.appendChild(el("span", "rail-ico", "○"));
    item.appendChild(el("span", "rail-label", label));
    item.disabled = true;
    rail.appendChild(item);
  }
}

function emptyState(copy, chipLabel, chipHref) {
  const wrap = el("div", "empty-state");
  wrap.appendChild(el("div", "empty-copy", copy));
  if (chipLabel) { const a = el("a", "empty-chip", chipLabel); a.href = chipHref || "/"; wrap.appendChild(a); }
  return wrap;
}

// ── render: tabs ────────────────────────────────────────────────────
function matchesSearch(text) {
  if (!state.search) return true;
  return esc(text).toLowerCase().includes(state.search.toLowerCase());
}

function renderRecent() {
  const p = $('[data-panel="recent"]'); p.innerHTML = "";
  const head = el("div", "panel-head"); head.appendChild(el("h2", null, "Recent")); p.appendChild(head);
  const rows = state.recent.filter(t => matchesSearch(t.summary || t.title));
  if (!rows.length) { p.appendChild(emptyState("No recent searches — start a conversation.", "New chat →", "/")); return; }
  const ul = el("ul", "row-list");
  for (const th of rows) {
    const label = (th.summary && th.summary.trim()) || th.title || "Untitled chat";
    const li = el("li", "row-item");
    li.appendChild(el("span", "row-ico", "🕘"));
    const main = el("div", "row-main");
    main.appendChild(el("div", "row-title", snippet(label)));
    const meta = el("div", "row-meta");
    meta.appendChild(el("span", null, fmtDate(th.updated_at)));
    if (th.turn_count > 1) meta.appendChild(el("span", null, th.turn_count + " turns"));
    main.appendChild(meta); li.appendChild(main);
    li.addEventListener("click", () => openThread(th.thread_id));
    ul.appendChild(li);
  }
  p.appendChild(ul);
}

function renderLiked() {
  const p = $('[data-panel="liked"]'); p.innerHTML = "";
  const head = el("div", "panel-head"); head.appendChild(el("h2", null, "Liked"));
  head.appendChild(el("span", "panel-sub", "Answers you gave a thumbs up")); p.appendChild(head);
  const rows = state.liked.filter(t => matchesSearch(t.question));
  if (!rows.length) { p.appendChild(emptyState("Nothing liked yet — give a thumbs up on any answer to save it here.")); return; }
  const ul = el("ul", "row-list");
  for (const t of rows) {
    const li = el("li", "row-item");
    li.appendChild(el("span", "row-ico", "★"));
    const main = el("div", "row-main");
    main.appendChild(el("div", "row-title", snippet(t.question || "(empty)")));
    const meta = el("div", "row-meta"); meta.appendChild(el("span", null, "👍 " + fmtDate(t.created_at)));
    main.appendChild(meta); li.appendChild(main);
    li.addEventListener("click", () => openThread(t.thread_id));
    ul.appendChild(li);
  }
  p.appendChild(ul);
}

function renderTasks() {
  const p = $('[data-panel="tasks"]'); p.innerHTML = "";
  const head = el("div", "panel-head"); head.appendChild(el("h2", null, "Tasks"));
  if (state.tFilter === "dueSoon") {
    const toolbar = el("div", "panel-toolbar");
    const chip = el("button", "active-filter", "Due soon ✕");
    chip.title = "Clear filter";
    chip.addEventListener("click", () => { state.tFilter = null; renderAll(); });
    toolbar.appendChild(chip); head.appendChild(toolbar);
  }
  p.appendChild(head);
  const total = state.tasksWork.length + state.tasksOrg.length + state.tasksNotif.length;
  if (!total) { p.appendChild(emptyState("All clear — no open tasks.")); return; }

  const dueIds = state.tFilter === "dueSoon" ? new Set(dueTodayList().map(t => t.task_id || t.id)) : null;
  const group = (title, tasks, cls) => {
    // "Due soon" preset filters work items to due-today/overdue; notifications
    // have no due semantics, so that group is dropped under the preset.
    if (dueIds && cls === "notifications") return;
    let rows = tasks.filter(t => matchesSearch((t.title || "") + " " + (t.body || "")));
    if (dueIds) rows = rows.filter(t => dueIds.has(t.task_id || t.id));
    if (!rows.length) return;
    const g = el("div", "task-group" + (cls ? " " + cls : ""));
    g.appendChild(el("h3", "task-group-title", title));
    const ul = el("ul", "row-list");
    for (const t of rows) ul.appendChild(taskRow(t, cls === "notifications"));
    g.appendChild(ul); p.appendChild(g);
  };
  group("Assigned to me", state.tasksWork);
  group("My org's open tasks", state.tasksOrg);
  group("Notifications", state.tasksNotif, "notifications");
}

function taskRow(t, isNotif) {
  const li = el("li", "row-item");
  li.appendChild(el("span", "row-ico", isNotif ? "🔔" : "✓"));
  const main = el("div", "row-main");
  main.appendChild(el("div", "row-title", snippet(t.title || t.body || "(untitled task)")));
  const meta = el("div", "row-meta");
  if (t.severity) { const s = el("span", "sev-badge " + esc(t.severity).toLowerCase(), t.severity); meta.appendChild(s); }
  if (t.type) meta.appendChild(el("span", null, t.type));
  const due = t.deadline || t.due_at;
  if (due) meta.appendChild(el("span", null, "due " + fmtDate(due)));
  if (t.org_name) meta.appendChild(el("span", null, t.org_name));
  main.appendChild(meta); li.appendChild(main);

  // deep-link target (never source_ref — per Task Agent)
  const threadId = (t.extra && t.extra.origin && t.extra.origin.thread_id) ||
                   (t.detail_payload && t.detail_payload.thread_id) || null;
  if (threadId) { li.style.cursor = "pointer"; li.addEventListener("click", (e) => { if (!e.target.closest(".u-actions")) openThread(threadId); }); }
  else li.style.cursor = "default";

  const actions = el("div", "u-actions");
  if (isNotif) actions.appendChild(iconBtn("✕", "Dismiss", () => taskAction(t, "dismiss")));
  else actions.appendChild(iconBtn("✓", "Resolve", () => taskAction(t, "resolve")));
  li.appendChild(actions);
  return li;
}

async function taskAction(t, action) {
  const id = t.task_id || t.id;
  if (!id) return;
  const who = "vault:" + (state.me?.user_id || "unknown");
  const bodyKey = action === "resolve" ? "resolved_by" : "dismissed_by";
  try {
    const r = await authFetch("/chat/tasks/" + encodeURIComponent(id) + "/" + action, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ [bodyKey]: who }),
    });
    if (r.ok) {
      state.tasksWork = state.tasksWork.filter(x => (x.task_id || x.id) !== id);
      state.tasksOrg = state.tasksOrg.filter(x => (x.task_id || x.id) !== id);
      state.tasksNotif = state.tasksNotif.filter(x => (x.task_id || x.id) !== id);
      renderAll(); toast(action === "resolve" ? "Task resolved." : "Dismissed.");
    } else toast("Couldn't " + action + " — try again.");
  } catch { toast("Couldn't " + action + " — network error."); }
}

// ── uploads (table) ─────────────────────────────────────────────────
function renderUploads() {
  const p = $('[data-panel="uploads"]'); p.innerHTML = "";
  const head = el("div", "panel-head");
  head.appendChild(el("h2", null, "Uploads"));
  head.appendChild(el("span", "panel-sub", "Documents you uploaded in chat · kept 7 days"));
  const toolbar = el("div", "panel-toolbar");
  if (state.uFilter === "expiring") {
    const chip = el("button", "active-filter", "Expiring soon ✕");
    chip.title = "Clear filter";
    chip.addEventListener("click", () => { state.uFilter = null; renderAll(); });
    toolbar.appendChild(chip);
  }
  const toggle = el("button", "chip-toggle" + (state.showExpired ? " on" : ""), (state.showExpired ? "✓ " : "") + "Show expired");
  toggle.addEventListener("click", () => { state.showExpired = !state.showExpired; renderUploads(); });
  toolbar.appendChild(toggle); head.appendChild(toolbar); p.appendChild(head);

  let rows = state.uploads.filter(u => u.status !== "discarded" && matchesSearch(u.filename));
  if (state.uFilter === "expiring") rows = rows.filter(u => !isExpired(u) && daysLeft(u) <= EXPIRING_DAYS);
  const live = rows.filter(u => !isExpired(u));
  const expired = rows.filter(isExpired);
  if (!live.length && !expired.length) {
    p.appendChild(emptyState("No documents yet — upload a file in chat to add it to your Vault. Documents stay for 7 days.", "Start a chat →", "/"));
    return;
  }
  sortUploads(live);

  const table = el("table", "uploads-table");
  const thead = el("thead"); const htr = el("tr");
  const cols = [
    { k: "", label: "" }, { k: "filename", label: "Filename" },
    { k: "created_at", label: "Uploaded", sortable: true },
    { k: "last_queried_at", label: "Last used", sortable: true },
    { k: "ttl", label: "TTL", sortable: true },
    { k: "vis", label: "Visibility" }, { k: "", label: "" },
  ];
  for (const c of cols) {
    const th = el("th", c.sortable ? "sortable" : null, c.label);
    if (c.sortable) {
      if (state.uSort.key === c.k) th.appendChild(el("span", "sort-arrow", state.uSort.dir === "asc" ? " ▲" : " ▼"));
      th.addEventListener("click", () => {
        if (state.uSort.key === c.k) state.uSort.dir = state.uSort.dir === "asc" ? "desc" : "asc";
        else state.uSort = { key: c.k, dir: "asc" };
        renderUploads();
      });
    }
    htr.appendChild(th);
  }
  thead.appendChild(htr); table.appendChild(thead);
  const tbody = el("tbody");
  for (const u of live) tbody.appendChild(uploadRow(u, false));
  if (state.showExpired) for (const u of expired) tbody.appendChild(uploadRow(u, true));
  table.appendChild(tbody); p.appendChild(table);
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

function uploadRow(u, expired) {
  const tr = el("tr", expired ? "is-expired" : "");
  const statusClass = expired ? "expired" : (u.status || "active");
  const tdDot = el("td"); tdDot.appendChild(el("span", "u-dot " + statusClass)); tr.appendChild(tdDot);

  const tdName = el("td"); const name = el("span", "u-name", u.filename || "(unnamed)"); name.title = u.filename || ""; tdName.appendChild(name);
  const tags = [u.confirmed_payer || u.suggested_payer, u.confirmed_state || u.suggested_state, u.confirmed_program || u.suggested_program].filter(Boolean);
  if (tags.length) { const t = el("span", "tag-pill", " " + tags.join(" · ")); t.style.marginLeft = "8px"; tdName.appendChild(t); }
  tr.appendChild(tdName);

  tr.appendChild(el("td", null, fmtDate(u.created_at) || "—"));
  tr.appendChild(el("td", null, u.last_queried_at ? fmtDate(u.last_queried_at) : "—"));
  const ttl = ttlOf(u); tr.appendChild(el("td", "u-ttl " + ttl.cls, expired ? ttl.label : ttl.label));

  const tdVis = el("td"); tdVis.appendChild(el("span", "u-vis", "Private")); tr.appendChild(tdVis);

  const tdAct = el("td"); const acts = el("div", "u-actions");
  if (!expired) {
    if (u.thread_id) acts.appendChild(iconBtn("↗", "Open conversation", () => openThread(u.thread_id)));
    acts.appendChild(iconBtn("⤓", "Download original", () => downloadUpload(u)));
    acts.appendChild(iconBtn("＋", "Extend 7 days", () => extendUpload(u)));
    acts.appendChild(iconBtn("🗑", "Delete", () => deleteUpload(u), "danger"));
    const promote = iconBtn("↑", "Promote to corpus — coming with org corpus (P2)", () => {});
    promote.disabled = true; acts.appendChild(promote);
  } else {
    acts.appendChild(iconBtn("🗑", "Delete", () => deleteUpload(u), "danger"));
  }
  tdAct.appendChild(acts); tr.appendChild(tdAct);
  return tr;
}

function iconBtn(glyph, tip, fn, extra) {
  const b = el("button", "icon-btn" + (extra ? " " + extra : ""), glyph);
  b.title = tip; b.setAttribute("aria-label", tip);
  b.addEventListener("click", (e) => { e.stopPropagation(); fn(); });
  return b;
}

async function downloadUpload(u) {
  const id = u.document_id; if (!id) return;
  toast("Preparing download…");
  try {
    const r = await authFetch("/chat/uploads/" + encodeURIComponent(id) + "/download");
    if (!r.ok) { toast("Download failed (" + r.status + ")."); return; }
    const blob = await r.blob();
    const a = document.createElement("a"); a.href = URL.createObjectURL(blob);
    a.download = u.filename || "document"; document.body.appendChild(a); a.click();
    a.remove(); setTimeout(() => URL.revokeObjectURL(a.href), 4000);
  } catch { toast("Download failed — network error."); }
}

async function extendUpload(u) {
  const id = u.document_id; if (!id) return;
  try {
    const r = await authFetch("/chat/uploads/" + encodeURIComponent(id) + "/extend", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ days: 7 }),
    });
    if (r.ok) {
      const d = await r.json().catch(() => ({}));
      if (d.expires_at) u.expires_at = d.expires_at;
      else u.expires_at = new Date(Date.now() + 7 * 864e5).toISOString();
      renderAll(); toast("Extended 7 days.");
    } else if (r.status === 404 || r.status === 405 || r.status === 501) {
      toast("Extend isn't live yet — Instant RAG is deploying it.");
    } else toast("Couldn't extend (" + r.status + ").");
  } catch { toast("Couldn't extend — network error."); }
}

async function deleteUpload(u) {
  const id = u.document_id; if (!id) return;
  if (!window.confirm('Remove "' + (u.filename || "this document") + '" from your Vault?')) return;
  try {
    const r = await authFetch("/chat/uploads/" + encodeURIComponent(id), { method: "DELETE" });
    if (r.ok) { state.uploads = state.uploads.filter(x => x.document_id !== id); renderAll(); toast("Removed."); }
    else if (r.status === 404 || r.status === 405 || r.status === 501) toast("Delete isn't live yet — Instant RAG is deploying it.");
    else toast("Couldn't delete (" + r.status + ").");
  } catch { toast("Couldn't delete — network error."); }
}

// ── orchestration ───────────────────────────────────────────────────
function renderAll() {
  renderUrgency(); renderRail();
  renderRecent(); renderLiked(); renderTasks(); renderUploads();
  document.querySelectorAll(".tab-panel").forEach(p => p.classList.toggle("active", p.dataset.panel === state.tab));
}
// Re-renders everything so active filters (uFilter/tFilter) are honored.
function switchTab(key) {
  state.tab = key;
  renderAll();
}

// ── preview (standalone / unauthenticated) ──────────────────────────
function loadPreview() {
  state.preview = true; document.body.classList.add("is-preview");
  state.me = { user_id: "preview", display_name: "You", assignee_ref: "user:preview" };
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
  state.tasksWork = [
    { task_id: "k1", title: "Review MCN-7701 authorization", type: "review", kind: "work_item", severity: "high", deadline: todayAt(17), extra: { origin: { thread_id: "t1" } } },
  ];
  state.tasksOrg = [
    { task_id: "k2", title: "Brightwater credentialing blocker — NPI mismatch", type: "blocker", kind: "work_item", severity: "blocker", org_name: "Brightwater Behavioral" },
  ];
  state.tasksNotif = [
    { task_id: "n1", title: '"Aetna_policy.pdf" is ready — ask about it', kind: "notification", detail_payload: { thread_id: "t1", filename: "Aetna_policy.pdf" } },
  ];
  state.uploads = [
    { document_id: "d1", filename: "Aetna_policy.pdf", status: "active", created_at: iso(-1), last_queried_at: iso(-0.1), expires_at: iso(6), thread_id: "t1", suggested_payer: "Aetna" },
    { document_id: "d2", filename: "Brightwater_intake_2026.docx", status: "active", created_at: iso(-6), last_queried_at: iso(-5), expires_at: iso(0.6), thread_id: "t3" },
    { document_id: "d3", filename: "Sunshine_fee_schedule.pdf", status: "active", created_at: iso(-3), expires_at: iso(4), suggested_payer: "Sunshine", suggested_state: "FL" },
    { document_id: "d4", filename: "old_scan_2026_06.pdf", status: "expired", created_at: iso(-10), expires_at: iso(-3) },
  ];
  renderAll();
}

// ── boot ────────────────────────────────────────────────────────────
if (CHAT_ORIGIN) { const bl = $("#backToChat"); if (bl) bl.href = CHAT_ORIGIN + "/"; }
$("#vaultSearch").addEventListener("input", (e) => { state.search = e.target.value; renderAll(); });
loadAll();
