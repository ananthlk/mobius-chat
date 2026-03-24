// ../../mobius-auth/dist/index.mjs
function normalizeUser(data) {
  const u = data;
  const pref = u.preference || {};
  const activities = u.activities || [];
  return {
    user_id: String(u.user_id ?? ""),
    tenant_id: u.tenant_id != null ? String(u.tenant_id) : "",
    email: u.email != null ? String(u.email) : void 0,
    display_name: u.display_name != null ? String(u.display_name) : void 0,
    first_name: u.first_name != null ? String(u.first_name) : void 0,
    preferred_name: u.preferred_name != null ? String(u.preferred_name) : void 0,
    greeting_name: String(u.preferred_name ?? u.first_name ?? u.display_name ?? u.email ?? "User"),
    avatar_url: u.avatar_url != null ? String(u.avatar_url) : void 0,
    timezone: String(u.timezone || "America/New_York"),
    locale: String(u.locale || "en-US"),
    is_onboarded: Boolean(u.is_onboarded),
    activities: activities.map((a) => a.activity_code || "").filter(Boolean),
    tone: pref.tone || "professional",
    greeting_enabled: pref.greeting_enabled !== false,
    autonomy_routine_tasks: pref.autonomy_routine_tasks || "confirm_first",
    autonomy_sensitive_tasks: pref.autonomy_sensitive_tasks || "manual"
  };
}
var STORAGE_KEYS = {
  accessToken: "mobius.auth.accessToken",
  refreshToken: "mobius.auth.refreshToken",
  expiresAt: "mobius.auth.expiresAt",
  userProfile: "mobius.auth.userProfile"
};
var AuthService = class {
  constructor(config) {
    this.listeners = /* @__PURE__ */ new Set();
    this.refreshTimer = null;
    this.apiBase = config.apiBase.replace(/\/$/, "");
    this.storage = config.storage;
  }
  on(callback) {
    this.listeners.add(callback);
    return () => this.listeners.delete(callback);
  }
  emit(event, data) {
    this.listeners.forEach((cb) => {
      try {
        cb(event, data);
      } catch (e) {
        console.error("[AuthService] listener error:", e);
      }
    });
  }
  async storeTokens(tokens) {
    const expiresAt = Date.now() + tokens.expires_in * 1e3;
    await this.storage.set({
      [STORAGE_KEYS.accessToken]: tokens.access_token,
      [STORAGE_KEYS.refreshToken]: tokens.refresh_token,
      [STORAGE_KEYS.expiresAt]: expiresAt
    });
    this.scheduleTokenRefresh(tokens.expires_in);
  }
  scheduleTokenRefresh(expiresIn) {
    if (this.refreshTimer)
      clearTimeout(this.refreshTimer);
    const ms = Math.max((expiresIn - 300) * 1e3, 6e4);
    this.refreshTimer = setTimeout(() => {
      this.refreshTimer = null;
      void this.refreshAccessToken();
    }, ms);
  }
  async getAccessToken() {
    const r = await this.storage.get([STORAGE_KEYS.accessToken, STORAGE_KEYS.expiresAt]);
    const token = r[STORAGE_KEYS.accessToken];
    const expiresAt = r[STORAGE_KEYS.expiresAt];
    if (!token) {
      const ok = await this.refreshAccessToken();
      return ok ? this.getAccessToken() : null;
    }
    if (expiresAt && Date.now() > expiresAt - 6e4) {
      const ok = await this.refreshAccessToken();
      return ok ? this.getAccessToken() : null;
    }
    return token;
  }
  async getRefreshToken() {
    const r = await this.storage.get([STORAGE_KEYS.refreshToken]);
    return r[STORAGE_KEYS.refreshToken] || null;
  }
  async refreshAccessToken() {
    const refreshToken = await this.getRefreshToken();
    if (!refreshToken)
      return false;
    try {
      const res = await fetch(`${this.apiBase}/auth/refresh`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ refresh_token: refreshToken })
      });
      const data = await res.json();
      if (!res.ok || !data.access_token) {
        await this.clearTokens();
        return false;
      }
      const expiresAt = Date.now() + (data.expires_in || 3600) * 1e3;
      await this.storage.set({
        [STORAGE_KEYS.accessToken]: data.access_token,
        [STORAGE_KEYS.expiresAt]: expiresAt
      });
      this.scheduleTokenRefresh(data.expires_in || 3600);
      this.emit("tokenRefreshed");
      return true;
    } catch {
      return false;
    }
  }
  async clearTokens() {
    await this.storage.remove([
      STORAGE_KEYS.accessToken,
      STORAGE_KEYS.refreshToken,
      STORAGE_KEYS.expiresAt,
      STORAGE_KEYS.userProfile
    ]);
    if (this.refreshTimer) {
      clearTimeout(this.refreshTimer);
      this.refreshTimer = null;
    }
  }
  async storeUserProfile(profile) {
    await this.storage.set({ [STORAGE_KEYS.userProfile]: profile });
  }
  async getUserProfile() {
    const r = await this.storage.get([STORAGE_KEYS.userProfile]);
    const p = r[STORAGE_KEYS.userProfile];
    return p || null;
  }
  /** Map demo shortcuts (admin, scheduler, etc.) to full email for convenience */
  resolveDemoEmail(email) {
    const trimmed = (email || "").trim().toLowerCase();
    if (!trimmed || trimmed.includes("@"))
      return trimmed;
    const shortcuts = {
      admin: "admin@demo.clinic",
      scheduler: "scheduler@demo.clinic",
      eligibility: "eligibility@demo.clinic",
      claims: "claims@demo.clinic",
      clinical: "clinical@demo.clinic",
      sarah: "sarah.chen@demo.clinic"
    };
    return shortcuts[trimmed] || trimmed;
  }
  async login(email, password, tenantId) {
    const resolvedEmail = this.resolveDemoEmail(email);
    try {
      const res = await fetch(`${this.apiBase}/auth/login`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email: resolvedEmail, password, tenant_id: tenantId })
      });
      let data = {};
      try {
        data = await res.json();
      } catch {
      }
      const dataTyped = data;
      if (!res.ok || !dataTyped.ok) {
        const errMsg = dataTyped.error || dataTyped.detail || (res.status === 404 ? "Auth not configured. Set MOBIUS_OS_AUTH_URL or USER_DATABASE_URL in mobius-chat/.env" : "Login failed");
        return { success: false, error: errMsg };
      }
      await this.storeTokens({
        access_token: dataTyped.access_token,
        refresh_token: dataTyped.refresh_token,
        expires_in: dataTyped.expires_in || 3600
      });
      if (dataTyped.user) {
        const profile = normalizeUser(dataTyped.user);
        await this.storeUserProfile(profile);
        this.emit("login", profile);
        return { success: true, user: profile };
      }
      this.emit("login");
      return { success: true };
    } catch (e) {
      console.error("[AuthService] login:", e);
      return { success: false, error: "Network error" };
    }
  }
  async register(email, password, displayName, firstName, tenantId) {
    try {
      const res = await fetch(`${this.apiBase}/auth/register`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          email,
          password,
          display_name: displayName,
          first_name: firstName,
          tenant_id: tenantId
        })
      });
      const data = await res.json();
      if (!res.ok || !data.ok) {
        return { success: false, error: data.error || "Registration failed" };
      }
      await this.storeTokens({
        access_token: data.access_token,
        refresh_token: data.refresh_token,
        expires_in: data.expires_in || 3600
      });
      if (data.user) {
        const profile = normalizeUser(data.user);
        await this.storeUserProfile(profile);
        this.emit("login", profile);
        return { success: true, user: profile };
      }
      this.emit("login");
      return { success: true };
    } catch (e) {
      console.error("[AuthService] register:", e);
      return { success: false, error: "Network error" };
    }
  }
  async logout() {
    const refreshToken = await this.getRefreshToken();
    if (refreshToken) {
      try {
        await fetch(`${this.apiBase}/auth/logout`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ refresh_token: refreshToken })
        });
      } catch {
      }
    }
    await this.clearTokens();
    this.emit("logout");
  }
  async isAuthenticated() {
    const token = await this.getAccessToken();
    return !!token;
  }
  /** Fetch current user from /auth/me and update stored profile */
  async getCurrentUser() {
    const token = await this.getAccessToken();
    if (!token)
      return null;
    try {
      const res = await fetch(`${this.apiBase}/auth/me`, {
        method: "GET",
        headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" }
      });
      if (!res.ok)
        return null;
      const data = await res.json();
      if (!data.ok || !data.user)
        return null;
      const profile = normalizeUser(data.user);
      await this.storeUserProfile(profile);
      return profile;
    } catch {
      return null;
    }
  }
  /** Auth state: unauthenticated | authenticated | onboarding */
  async getAuthState() {
    const token = await this.getAccessToken();
    if (!token)
      return "unauthenticated";
    const profile = await this.getUserProfile();
    if (profile && profile.is_onboarded === false)
      return "onboarding";
    return "authenticated";
  }
  /** Check if email exists (for page detection) */
  async checkEmail(email, tenantId) {
    try {
      const res = await fetch(`${this.apiBase}/auth/check-email`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email, tenant_id: tenantId })
      });
      const data = await res.json();
      return { exists: data.exists === true, user: data.user };
    } catch {
      return { exists: false };
    }
  }
  /** Get Authorization header for API calls */
  async getAuthHeader() {
    const token = await this.getAccessToken();
    return token ? { Authorization: `Bearer ${token}` } : null;
  }
};
var STORAGE_KEYS3 = {
  accessToken: "mobius.auth.accessToken",
  refreshToken: "mobius.auth.refreshToken",
  expiresAt: "mobius.auth.expiresAt",
  userProfile: "mobius.auth.userProfile"
};
var localStorageAdapter = {
  async get(keys) {
    const out = {};
    for (const k of keys) {
      try {
        const v = localStorage.getItem(k);
        if (v != null) {
          if (k === STORAGE_KEYS3.expiresAt)
            out[k] = Number(v);
          else if (k === STORAGE_KEYS3.userProfile)
            out[k] = JSON.parse(v);
          else
            out[k] = v;
        }
      } catch {
      }
    }
    return out;
  },
  async set(items) {
    for (const [k, v] of Object.entries(items)) {
      try {
        if (v == null)
          localStorage.removeItem(k);
        else if (typeof v === "object")
          localStorage.setItem(k, JSON.stringify(v));
        else
          localStorage.setItem(k, String(v));
      } catch {
      }
    }
  },
  async remove(keys) {
    for (const k of keys)
      localStorage.removeItem(k);
  }
};
function escapeHtml(s) {
  const div = document.createElement("div");
  div.textContent = s;
  return div.innerHTML;
}
function createAuthModal(options) {
  const { auth, showOAuth = true, demoEmail, onSuccess, onClose } = options;
  let currentUser = null;
  const overlay = document.createElement("div");
  overlay.className = "mobius-auth-overlay";
  overlay.setAttribute("aria-hidden", "true");
  const panel = document.createElement("div");
  panel.className = "mobius-auth-panel";
  panel.setAttribute("role", "dialog");
  panel.setAttribute("aria-labelledby", "mobius-auth-title");
  function close() {
    overlay.classList.remove("open");
    overlay.setAttribute("aria-hidden", "true");
    onClose?.();
  }
  function open(mode = "login") {
    currentUser = null;
    auth.getUserProfile().then((p) => {
      currentUser = p ?? null;
      const m = mode === "account" && currentUser ? "account" : mode;
      render(m);
    });
    overlay.classList.add("open");
    overlay.setAttribute("aria-hidden", "false");
  }
  function updateUser(user) {
    currentUser = user;
  }
  function render(mode) {
    const titleId = "mobius-auth-title";
    const hasOAuth = showOAuth;
    const loginHtml = `
      <button type="button" class="mobius-auth-close" aria-label="Close">&times;</button>
      <h2 id="${titleId}" class="mobius-auth-title">Sign in</h2>
      <div class="mobius-auth-form" data-mode="login">
        <input type="email" class="mobius-auth-email" placeholder="Email (or admin, scheduler for demo)" autocomplete="email" ${demoEmail ? `value="${escapeHtml(demoEmail)}"` : ""} />
        <input type="password" class="mobius-auth-password" placeholder="Password" autocomplete="current-password" />
        <button type="button" class="mobius-auth-btn mobius-auth-login-btn">Sign in</button>
        <div class="mobius-auth-error" style="display:none"></div>
        ${hasOAuth ? `
          <div class="mobius-auth-divider"><span>or continue with</span></div>
          <div class="mobius-auth-oauth">
            <button type="button" class="mobius-auth-oauth-btn" data-provider="google">Google</button>
            <button type="button" class="mobius-auth-oauth-btn" data-provider="microsoft">Microsoft</button>
            <button type="button" class="mobius-auth-sso-btn">Enterprise SSO</button>
          </div>
        ` : ""}
        <p class="mobius-auth-switch">No account? <button type="button" class="mobius-auth-switch-btn" data-to="signup">Sign up</button></p>
      </div>
    `;
    const signupHtml = `
      <button type="button" class="mobius-auth-close" aria-label="Close">&times;</button>
      <h2 id="${titleId}" class="mobius-auth-title">Create account</h2>
      <div class="mobius-auth-form" data-mode="signup">
        <input type="text" class="mobius-auth-display-name" placeholder="Display name (optional)" />
        <input type="text" class="mobius-auth-first-name" placeholder="First name (optional)" />
        <input type="email" class="mobius-auth-email" placeholder="Email" autocomplete="email" />
        <input type="password" class="mobius-auth-password" placeholder="Password (min 8 chars)" autocomplete="new-password" />
        <button type="button" class="mobius-auth-btn mobius-auth-signup-btn">Create account</button>
        <div class="mobius-auth-error" style="display:none"></div>
        <p class="mobius-auth-switch">Already have an account? <button type="button" class="mobius-auth-switch-btn" data-to="login">Sign in</button></p>
      </div>
    `;
    const accountHtml = `
      <button type="button" class="mobius-auth-close" aria-label="Close">&times;</button>
      <h2 id="${titleId}" class="mobius-auth-title">Account</h2>
      <div class="mobius-auth-form" data-mode="account">
        <p class="mobius-auth-user-info">${escapeHtml(currentUser?.greeting_name || currentUser?.email || currentUser?.display_name || "User")}</p>
        <a href="#" class="mobius-auth-prefs-link">Preferences</a>
        <button type="button" class="mobius-auth-btn mobius-auth-logout-btn">Sign out</button>
      </div>
    `;
    panel.innerHTML = mode === "login" ? loginHtml : mode === "signup" ? signupHtml : accountHtml;
    panel.querySelector(".mobius-auth-close")?.addEventListener("click", close);
    overlay.addEventListener("click", (e) => {
      if (e.target === overlay)
        close();
    });
    if (mode === "login") {
      const loginBtn = panel.querySelector(".mobius-auth-login-btn");
      const emailInput = panel.querySelector(".mobius-auth-email");
      const passwordInput = panel.querySelector(".mobius-auth-password");
      const errorEl = panel.querySelector(".mobius-auth-error");
      const doLogin = async () => {
        const email = emailInput?.value?.trim();
        const password = passwordInput?.value;
        if (!email || !password) {
          if (errorEl) {
            errorEl.textContent = "Email and password required";
            errorEl.style.display = "block";
          }
          return;
        }
        if (errorEl)
          errorEl.style.display = "none";
        if (loginBtn) {
          loginBtn.textContent = "Signing in...";
          loginBtn.disabled = true;
        }
        const result = await auth.login(email, password);
        if (result.success && result.user) {
          onSuccess?.(result.user);
          close();
        } else {
          if (errorEl) {
            errorEl.textContent = result.error || "Login failed";
            errorEl.style.display = "block";
          }
        }
        if (loginBtn) {
          loginBtn.textContent = "Sign in";
          loginBtn.disabled = false;
        }
      };
      loginBtn?.addEventListener("click", () => void doLogin());
      passwordInput?.addEventListener("keydown", (e) => {
        if (e.key === "Enter")
          void doLogin();
      });
      panel.querySelectorAll(".mobius-auth-oauth-btn, .mobius-auth-sso-btn").forEach((btn) => {
        btn.addEventListener("click", () => {
          if (typeof window.showToast === "function") {
            window.showToast("Coming soon");
          }
        });
      });
    }
    if (mode === "signup") {
      const signupBtn = panel.querySelector(".mobius-auth-signup-btn");
      const emailInput = panel.querySelector(".mobius-auth-email");
      const passwordInput = panel.querySelector(".mobius-auth-password");
      const displayNameInput = panel.querySelector(".mobius-auth-display-name");
      const firstNameInput = panel.querySelector(".mobius-auth-first-name");
      const errorEl = panel.querySelector(".mobius-auth-error");
      const doSignup = async () => {
        const email = emailInput?.value?.trim();
        const password = passwordInput?.value;
        if (!email || !password) {
          if (errorEl) {
            errorEl.textContent = "Email and password required";
            errorEl.style.display = "block";
          }
          return;
        }
        if (password.length < 8) {
          if (errorEl) {
            errorEl.textContent = "Password must be at least 8 characters";
            errorEl.style.display = "block";
          }
          return;
        }
        if (errorEl)
          errorEl.style.display = "none";
        if (signupBtn) {
          signupBtn.textContent = "Creating...";
          signupBtn.disabled = true;
        }
        const result = await auth.register(
          email,
          password,
          displayNameInput?.value?.trim() || void 0,
          firstNameInput?.value?.trim() || void 0
        );
        if (result.success && result.user) {
          onSuccess?.(result.user);
          close();
        } else {
          if (errorEl) {
            errorEl.textContent = result.error || "Sign up failed";
            errorEl.style.display = "block";
          }
        }
        if (signupBtn) {
          signupBtn.textContent = "Create account";
          signupBtn.disabled = false;
        }
      };
      signupBtn?.addEventListener("click", () => void doSignup());
      passwordInput?.addEventListener("keydown", (e) => {
        if (e.key === "Enter")
          void doSignup();
      });
    }
    if (mode === "account") {
      panel.querySelector(".mobius-auth-logout-btn")?.addEventListener("click", async () => {
        await auth.logout();
        updateUser(null);
        close();
      });
      panel.querySelector(".mobius-auth-prefs-link")?.addEventListener("click", (e) => {
        e.preventDefault();
        close();
        window.onOpenPreferences?.();
      });
    }
    panel.querySelectorAll(".mobius-auth-switch-btn").forEach((btn) => {
      btn.addEventListener("click", () => {
        const to = btn.getAttribute("data-to");
        render(to);
      });
    });
  }
  overlay.appendChild(panel);
  return { el: overlay, open, close, updateUser };
}
var AUTH_STYLES = `
.mobius-auth-overlay {
  display: none;
  position: fixed;
  inset: 0;
  background: rgba(0,0,0,0.4);
  z-index: 1000;
  align-items: center;
  justify-content: center;
}
.mobius-auth-overlay.open { display: flex; }
.mobius-auth-panel {
  background: #fafbfc;
  border-radius: 12px;
  padding: 1.5rem;
  max-width: 360px;
  width: 90%;
  box-shadow: 0 8px 24px rgba(0,0,0,0.08);
  position: relative;
}
.mobius-auth-close {
  position: absolute;
  top: 0.75rem;
  right: 0.75rem;
  background: none;
  border: none;
  font-size: 1.5rem;
  cursor: pointer;
  color: #64748b;
  line-height: 1;
  padding: 0;
}
.mobius-auth-close:hover { color: #1a1d21; }
.mobius-auth-title { margin: 0 0 1rem; font-size: 1.125rem; }
.mobius-auth-form input,
.mobius-auth-form .mobius-auth-btn {
  display: block;
  width: 100%;
  margin-bottom: 0.75rem;
  padding: 0.5rem 0.75rem;
  font-size: 0.9375rem;
  border: 1px solid #e2e8f0;
  border-radius: 8px;
}
.mobius-auth-form .mobius-auth-btn {
  background: #3b82f6;
  color: white;
  border: none;
  cursor: pointer;
  font-weight: 500;
}
.mobius-auth-form .mobius-auth-btn:hover { background: #2563eb; }
.mobius-auth-error { font-size: 0.8125rem; color: #dc2626; margin-top: 0.5rem; }
.mobius-auth-divider {
  display: flex;
  align-items: center;
  margin: 12px 0 10px;
}
.mobius-auth-divider::before,
.mobius-auth-divider::after {
  content: "";
  flex: 1;
  height: 1px;
  background: rgba(0,0,0,0.1);
}
.mobius-auth-divider span {
  padding: 0 8px;
  font-size: 0.7rem;
  color: #94a3b8;
}
.mobius-auth-oauth {
  display: flex;
  gap: 8px;
  margin-bottom: 10px;
}
.mobius-auth-oauth-btn,
.mobius-auth-sso-btn {
  flex: 1;
  padding: 8px;
  background: white;
  border: 1px solid rgba(0,0,0,0.15);
  border-radius: 6px;
  font-size: 0.7rem;
  cursor: pointer;
}
.mobius-auth-switch {
  margin: 1rem 0 0;
  font-size: 0.8125rem;
  color: #64748b;
}
.mobius-auth-switch-btn {
  background: none;
  border: none;
  color: #3b82f6;
  cursor: pointer;
  padding: 0;
  font-size: inherit;
}
.mobius-auth-switch-btn:hover { text-decoration: underline; }
.mobius-auth-user-info { margin: 0 0 1rem; font-size: 0.8125rem; }
.mobius-auth-prefs-link {
  display: block;
  margin-bottom: 1rem;
  color: #3b82f6;
  font-size: 0.8125rem;
}
`;
function createAuthService(config) {
  return new AuthService(config);
}

// src/app.ts
function adjudicationVerdictUi(qc) {
  const raw = (qc.adjudication_verdict || "").toString().trim().toUpperCase();
  if (raw === "PARTIAL") {
    return {
      shortLabel: "PARTIAL",
      verdictBadgeText: "Verdict: PARTIAL (acceptable)",
      badgeVariant: "partial"
    };
  }
  if (raw === "PASS") {
    return { shortLabel: "PASS", verdictBadgeText: "Verdict: PASS", badgeVariant: "pass" };
  }
  if (raw === "FAIL") {
    return { shortLabel: "FAIL", verdictBadgeText: "Verdict: FAIL", badgeVariant: "fail" };
  }
  return qc.passed ? { shortLabel: "PASS", verdictBadgeText: "Verdict: PASS", badgeVariant: "pass" } : { shortLabel: "FAIL", verdictBadgeText: "Verdict: FAIL", badgeVariant: "fail" };
}
var SECTION_INTENTS = ["process", "requirements", "definitions", "exceptions", "references"];
function isSectionIntent(s) {
  return typeof s === "string" && SECTION_INTENTS.includes(s);
}
var API_BASE = typeof window !== "undefined" && window.API_BASE && window.API_BASE.startsWith("http") ? window.API_BASE : "http://localhost:8000";
function renderLlmRouterReportCompositeSpec(parent, spec) {
  if (!spec || !spec.title)
    return;
  const details = document.createElement("details");
  details.className = "llm-router-report-composite";
  details.open = false;
  const summ = document.createElement("summary");
  summ.textContent = spec.title;
  details.appendChild(summ);
  if (spec.summary) {
    const p = document.createElement("p");
    p.className = "llm-router-report-composite-p";
    p.textContent = spec.summary;
    details.appendChild(p);
  }
  if (spec.formula) {
    const pre = document.createElement("pre");
    pre.className = "llm-router-report-composite-formula";
    pre.textContent = spec.formula;
    details.appendChild(pre);
  }
  const w = spec.weights;
  if (w && Object.keys(w).length) {
    const wp = document.createElement("p");
    wp.className = "llm-router-report-composite-p";
    wp.textContent = "Weights: " + Object.entries(w).map(([k, v]) => `${k}=${v}`).join(", ");
    details.appendChild(wp);
  }
  const defs = [
    { label: "Quality (q)", block: spec.quality },
    { label: "Reliability (rel)", block: spec.reliability },
    { label: "Latency term", block: spec.latency_term },
    { label: "Cost term", block: spec.cost_term }
  ];
  for (const { label, block } of defs) {
    const d = block?.definition;
    if (!d)
      continue;
    const h = document.createElement("div");
    h.className = "llm-router-report-composite-def";
    const strong = document.createElement("strong");
    strong.textContent = label + ": ";
    h.appendChild(strong);
    h.appendChild(document.createTextNode(d));
    details.appendChild(h);
  }
  const caps = spec.stage_caps;
  if (caps && Object.keys(caps).length) {
    const hc = document.createElement("p");
    hc.className = "llm-router-report-composite-p";
    hc.innerHTML = "<strong>Linear caps by stage bucket</strong> (for latTerm / costTerm):";
    details.appendChild(hc);
    const tw = document.createElement("div");
    tw.className = "llm-router-report-table-wrap";
    const tbl = document.createElement("table");
    tbl.className = "llm-router-report-table llm-router-report-table--caps";
    tbl.innerHTML = "<thead><tr><th>Bucket</th><th>Latency cap (ms)</th><th>Cost cap ($)</th></tr></thead><tbody></tbody>";
    const tb = tbl.querySelector("tbody");
    for (const name of Object.keys(caps).sort()) {
      const c = caps[name];
      const tr = document.createElement("tr");
      tr.innerHTML = `<td>${escapeHtml2(name)}</td><td>${c?.latency_cap_ms ?? "\u2014"}</td><td>${c?.cost_cap_usd ?? "\u2014"}</td>`;
      tb.appendChild(tr);
    }
    tw.appendChild(tbl);
    details.appendChild(tw);
  }
  if (spec.stage_bucket_rules) {
    const pr = document.createElement("p");
    pr.className = "llm-router-report-composite-p";
    pr.textContent = spec.stage_bucket_rules;
    details.appendChild(pr);
  }
  if (spec.token_pricing_note) {
    const pt = document.createElement("p");
    pt.className = "llm-router-report-composite-p";
    pt.textContent = spec.token_pricing_note;
    details.appendChild(pt);
  }
  if (spec.react_deep_rounds_note) {
    const prd = document.createElement("p");
    prd.className = "llm-router-report-composite-p";
    prd.textContent = spec.react_deep_rounds_note;
    details.appendChild(prd);
  }
  parent.appendChild(details);
}
function fmtRouterReportCompositeTerms(row) {
  const b = row.composite_breakdown;
  if (!b || typeof b !== "object")
    return "\u2014";
  const f = (k) => {
    const x = b[k];
    return typeof x === "number" && Number.isFinite(x) ? x.toFixed(2) : "\u2014";
  };
  return [f("term_quality"), f("term_reliability"), f("term_latency"), f("term_cost")].join(" / ");
}
function routerReportTermsTooltip(row) {
  const b = row.composite_breakdown;
  if (!b || typeof b !== "object")
    return "";
  try {
    return JSON.stringify(b, null, 2).slice(0, 4e3);
  } catch {
    return "";
  }
}
function setupLlmRouterReportUI() {
  const btn = document.getElementById("btnLlmRouterReport");
  const modal = document.getElementById("llmRouterReportModal");
  const body = document.getElementById("llmRouterReportBody");
  const closeBtn = document.getElementById("llmRouterReportClose");
  const backdrop = document.getElementById("llmRouterReportBackdrop");
  if (!btn || !modal || !body)
    return;
  const setOpen = (open) => {
    modal.classList.toggle("llm-router-report-modal--open", open);
    modal.setAttribute("aria-hidden", open ? "false" : "true");
  };
  const loadReport = () => {
    body.innerHTML = '<p class="llm-router-report-loading">Loading\u2026</p>';
    fetch(API_BASE + "/chat/llm-router-report?window_days=30").then((r) => r.json()).then((data) => {
      renderLlmRouterReportBody(body, data);
    }).catch(() => {
      body.innerHTML = '<p class="llm-router-report-error">Could not load report. Is the API up and <code>CHAT_RAG_DATABASE_URL</code> set?</p>';
    });
  };
  btn.addEventListener("click", () => {
    setOpen(true);
    loadReport();
  });
  closeBtn?.addEventListener("click", () => setOpen(false));
  backdrop?.addEventListener("click", () => setOpen(false));
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && modal.classList.contains("llm-router-report-modal--open"))
      setOpen(false);
  });
}
function renderLlmRouterReportBody(container, data) {
  container.replaceChildren();
  const meta = document.createElement("p");
  meta.className = "llm-router-report-meta";
  const gen = data.generated_at ? new Date(data.generated_at).toLocaleString() : "\u2014";
  meta.textContent = `Rolling window: ${data.window_days} days \xB7 Generated ${gen}`;
  container.appendChild(meta);
  if (data.warning) {
    const w = document.createElement("p");
    w.className = "llm-router-report-error";
    w.textContent = data.warning;
    container.appendChild(w);
  }
  renderLlmRouterReportCompositeSpec(container, data.composite_spec);
  const th = data.thompson;
  if (th) {
    const details = document.createElement("details");
    details.className = "llm-router-report-thompson";
    details.open = true;
    const summ = document.createElement("summary");
    summ.textContent = th.title || "How routing works";
    details.appendChild(summ);
    const p = document.createElement("p");
    p.className = "llm-router-report-thompson-summary";
    p.textContent = th.summary;
    details.appendChild(p);
    const ul = document.createElement("ul");
    ul.className = "llm-router-report-thompson-list";
    const li1 = document.createElement("li");
    li1.textContent = `Forced exploration: least-sampled model every ${th.exploration_interval_turns} turns per stage.`;
    ul.appendChild(li1);
    const li2 = document.createElement("li");
    li2.textContent = `Circuit breakers: pull models above ~${(th.circuit_breaker_hard_error_max * 100).toFixed(0)}% hard failures or ~${(th.circuit_breaker_24h_error_max * 100).toFixed(0)}% errors (24h).`;
    ul.appendChild(li2);
    const leg = th.confidence_legend || {};
    const li3 = document.createElement("li");
    li3.textContent = "Row shading: " + ["low", "medium", "high", "locked"].map((k) => `${k} \u2014 ${leg[k] || k}`).join(" ");
    ul.appendChild(li3);
    details.appendChild(ul);
    container.appendChild(details);
  }
  const legend = document.createElement("div");
  legend.className = "llm-router-report-legend";
  legend.innerHTML = '<span class="llm-router-report-legend-item llm-router-report-tr--low">Low data</span><span class="llm-router-report-legend-item llm-router-report-tr--medium">Medium</span><span class="llm-router-report-legend-item llm-router-report-tr--high">High</span><span class="llm-router-report-legend-item llm-router-report-tr--locked">Locked-in</span><span class="llm-router-report-legend-note">= adjudicated sample count (quality scores)</span>';
  container.appendChild(legend);
  if (!data.stages || data.stages.length === 0) {
    const empty = document.createElement("p");
    empty.className = "llm-router-report-empty";
    empty.textContent = data.ok ? "No llm_calls in this window yet. Chat to populate stats." : "No data.";
    container.appendChild(empty);
  }
  for (const block of data.stages || []) {
    const h3 = document.createElement("h3");
    h3.className = "llm-router-report-stage-title";
    if (block.stage_family === "react" && block.react_round != null && Number.isFinite(block.react_round)) {
      h3.textContent = `ReAct reasoning \xB7 round ${block.react_round} (${block.stage})`;
    } else {
      h3.textContent = block.stage || "\u2014";
    }
    container.appendChild(h3);
    const wrap = document.createElement("div");
    wrap.className = "llm-router-report-table-wrap";
    const table = document.createElement("table");
    table.className = "llm-router-report-table";
    const thead = document.createElement("thead");
    thead.innerHTML = `<tr><th title="Rank within stage">#</th><th>Model</th><th>Provider</th><th>Calls</th><th title='Adjudicated quality rows'>Scored</th><th title='Mean quality_score'>Avg Q</th><th title='Router composite [0,1]'>Comp</th><th title="q\xB7r / r / lat / cost weighted terms (hover row for JSON)">Terms</th><th title="stage_bucket">Bkt</th><th title="p95 latency ms (success)">p95</th><th title="Mean cost_usd (success)">Avg $</th><th title="Mean input_tokens">In tok</th><th title="Mean output_tokens">Out tok</th><th title="Registered $/1K input (cost_model)">$/1K in</th><th title="Registered $/1K output">$/1K out</th><th title="(In tok/1000)\xD7$/1K in + (Out tok/1000)\xD7$/1K out">List $</th><th title="Mean latency ms">Avg ms</th><th title="Hard error rate">Err %</th></tr>`;
    table.appendChild(thead);
    const tbody = document.createElement("tbody");
    (block.models || []).forEach((row, idx) => {
      const tr = document.createElement("tr");
      tr.className = "llm-router-report-tr llm-router-report-tr--" + (row.confidence || "low");
      const b = row.composite_breakdown || {};
      const bucket = typeof b.stage_bucket === "string" ? b.stage_bucket : "\u2014";
      const cells = [
        { text: String(idx + 1) },
        { text: row.model || "\u2014" },
        { text: row.provider || "\u2014" },
        { text: String(row.total_calls ?? 0) },
        { text: String(row.quality_samples ?? 0) },
        { text: row.avg_quality != null ? Number(row.avg_quality).toFixed(3) : "\u2014" },
        { text: row.composite_score != null ? Number(row.composite_score).toFixed(3) : "\u2014" },
        { text: fmtRouterReportCompositeTerms(row), title: routerReportTermsTooltip(row) },
        { text: bucket },
        { text: row.p95_latency_ms != null ? String(row.p95_latency_ms) : "\u2014" },
        {
          text: row.avg_cost_usd != null && Number(row.avg_cost_usd) > 0 ? Number(row.avg_cost_usd).toFixed(4) : row.avg_cost_usd != null ? String(row.avg_cost_usd) : "\u2014"
        },
        { text: row.avg_input_tokens != null ? String(row.avg_input_tokens) : "\u2014" },
        { text: row.avg_output_tokens != null ? String(row.avg_output_tokens) : "\u2014" },
        {
          text: row.usd_per_1k_input != null ? Number(row.usd_per_1k_input).toFixed(5) : "\u2014"
        },
        {
          text: row.usd_per_1k_output != null ? Number(row.usd_per_1k_output).toFixed(5) : "\u2014"
        },
        {
          text: row.avg_list_price_usd != null && row.avg_list_price_usd > 0 ? Number(row.avg_list_price_usd).toFixed(4) : row.avg_list_price_usd != null ? String(row.avg_list_price_usd) : "\u2014"
        },
        { text: row.avg_latency_ms != null ? String(row.avg_latency_ms) : "\u2014" },
        {
          text: row.hard_error_rate != null ? (Number(row.hard_error_rate) * 100).toFixed(1) + "%" : "\u2014"
        }
      ];
      cells.forEach(({ text, title }) => {
        const td = document.createElement("td");
        td.textContent = text;
        if (title)
          td.setAttribute("title", title);
        tr.appendChild(td);
      });
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    wrap.appendChild(table);
    container.appendChild(wrap);
  }
  const roster = data.roster_enabled || [];
  if (roster.length > 0) {
    const rd = document.createElement("details");
    rd.className = "llm-router-report-roster";
    const rs = document.createElement("summary");
    rs.textContent = `Currently enabled in router roster (${roster.length} models)`;
    rd.appendChild(rs);
    const pre = document.createElement("pre");
    pre.className = "llm-router-report-roster-pre";
    pre.textContent = roster.map((r) => `${r.model_id} (${r.provider}) \u2014 ${r.display_name}`).join("\n");
    rd.appendChild(pre);
    container.appendChild(rd);
  }
}
function el(id) {
  const e = document.getElementById(id);
  if (!e)
    throw new Error("Element not found: " + id);
  return e;
}
function normalizeMessageText(text) {
  return (text ?? "").replace(/\n{2,}/g, "\n").trim();
}
var SANITIZE_BLEED_FALLBACK = "We couldn\u2019t display this answer cleanly. Please try again or rephrase your question.";
function sanitizeDisplayMessage(raw) {
  const trimmed = (raw ?? "").trim();
  if (!trimmed)
    return "";
  const tryExtractFromJsonString = (jsonStr, depth) => {
    if (depth > 4)
      return null;
    let s2 = jsonStr.trim();
    if (/^json\s*\{/i.test(s2))
      s2 = s2.replace(/^json\s*/i, "").trim();
    s2 = s2.replace(/^```json\s*/i, "").replace(/^```\s*/i, "").replace(/\s*```\s*$/i, "").trim();
    if (!s2.startsWith("{") && !s2.startsWith("["))
      return null;
    try {
      const parsed = JSON.parse(s2);
      if (typeof parsed.answer === "string" && parsed.answer.trim()) {
        const inner = tryExtractFromJsonString(parsed.answer, depth + 1);
        return inner ?? parsed.answer.trim();
      }
      if (typeof parsed.direct_answer === "string" && parsed.direct_answer.trim()) {
        const inner = tryExtractFromJsonString(parsed.direct_answer, depth + 1);
        if (inner)
          return inner;
        const da = parsed.direct_answer.trim();
        if (!da.startsWith("{") && !da.startsWith("["))
          return da;
      }
      if (typeof parsed.message === "string" && parsed.message.trim()) {
        return parsed.message.trim();
      }
      const res = parsed.resolutions;
      if (Array.isArray(res) && res.length > 0) {
        const parts = [];
        for (const item of res) {
          if (!item || typeof item !== "object")
            continue;
          const o = item;
          const r = o.resolution;
          if (typeof r === "string" && r.trim())
            parts.push(r.trim());
          else if (r && typeof r === "object") {
            const rd = r.direct_answer;
            if (typeof rd === "string" && rd.trim())
              parts.push(rd.trim());
          }
          if (typeof o.text === "string" && o.text.trim())
            parts.push(o.text.trim());
          if (typeof o.answer === "string" && o.answer.trim())
            parts.push(o.answer.trim());
        }
        if (parts.length)
          return parts.join("\n\n");
      }
      return null;
    } catch {
      return null;
    }
  };
  let s = trimmed;
  if (/^json\s*\{/i.test(s))
    s = s.replace(/^json\s*/i, "").trim();
  s = s.replace(/^```json\s*/i, "").replace(/^```\s*/i, "").replace(/\s*```\s*$/i, "").trim();
  const extracted = tryExtractFromJsonString(s, 0);
  if (extracted)
    return extracted;
  if (s.startsWith("{") || s.startsWith("[")) {
    try {
      JSON.parse(s);
      return SANITIZE_BLEED_FALLBACK;
    } catch {
    }
  }
  if (/^\s*\{/.test(s) && /"direct_answer"\s*:/.test(s) && /"sections"\s*:/.test(s)) {
    return SANITIZE_BLEED_FALLBACK;
  }
  return s;
}
function isAllowedOpenHref(href) {
  const t = href.trim();
  if (!t || t.toLowerCase().startsWith("javascript:"))
    return false;
  if (t.startsWith("/"))
    return true;
  return /^https?:\/\//i.test(t);
}
function thinkingFriendlyStatus(line) {
  const l = (line ?? "").toLowerCase();
  if (l.includes("waiting for worker") || l.includes("request sent"))
    return "Connecting\u2026";
  if (l.includes("searching our materials") || l.includes("search_corpus") || l.includes("library research")) {
    return "Searching provider materials\u2026";
  }
  if (l.includes("google") || l.includes("web search") || l.includes("web_scrape") || l.includes("web page")) {
    return "Searching the web\u2026";
  }
  if (l.includes("npi") || l.includes("nppes") || l.includes("registry lookup"))
    return "Looking up provider registry\u2026";
  if (l.includes("credentialing") || l.includes("roster_report") || l.includes("roster report")) {
    return "Running credentialing report\u2026";
  }
  if (l.includes("draft composer") || l.includes("integrator") || l.includes("composing your answer")) {
    return "Composing your answer\u2026";
  }
  if (l.includes("validator") || l.includes("answer card"))
    return "Checking answer format\u2026";
  if (l.includes("quality") || l.includes("adjudicat"))
    return "Quality review\u2026";
  if (l.includes("model:"))
    return "Finishing up\u2026";
  return "Working on your answer\u2026";
}
function simpleMarkdownToHtml(text) {
  const s = (text ?? "").trim();
  if (!s)
    return "";
  const escape = (t) => t.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  const imgs = [];
  const imgRe = /!\[([^\]]*)\]\(([^)]+)\)/g;
  let out = s.replace(imgRe, (_m, alt, url) => {
    const escapedAlt = escape(alt || "");
    const i = imgs.length;
    imgs.push(`<img src="${url}" alt="${escapedAlt}" class="report-chart" loading="lazy" />`);
    return `\uE000${i}\uE001`;
  });
  out = escape(out);
  imgs.forEach((img, i) => {
    out = out.replace(`\uE000${i}\uE001`, img);
  });
  out = out.replace(/^#### (.+)$/gm, "<h4>$1</h4>");
  out = out.replace(/^### (.+)$/gm, "<h3>$1</h3>");
  out = out.replace(/^## (.+)$/gm, "<h2>$1</h2>");
  out = out.replace(/^# (.+)$/gm, "<h1>$1</h1>");
  out = out.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
  out = out.replace(/\n\n+/g, "</p><p>");
  out = out.replace(/\n/g, "<br>\n");
  return "<p>" + out + "</p>";
}
function simpleMarkdownToHtmlInner(text) {
  const s = (text ?? "").trim();
  if (!s)
    return "";
  let out = s;
  out = out.replace(/^#### (.+)$/gm, "<h4>$1</h4>");
  out = out.replace(/^### (.+)$/gm, "<h3>$1</h3>");
  out = out.replace(/^## (.+)$/gm, "<h2>$1</h2>");
  out = out.replace(/^# (.+)$/gm, "<h1>$1</h1>");
  out = out.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
  out = out.replace(/^- (.+)$/gm, "<li>$1</li>");
  out = out.replace(/\n\n+/g, "</p><p>");
  out = out.replace(/\n/g, "<br>\n");
  out = "<p>" + out + "</p>";
  out = out.replace(/((?:<li>[\s\S]*?<\/li>(?:<br>\s*)?)+)/g, "<ul>$1</ul>");
  return out;
}
function rosterStepMarkdownToHtml(text) {
  const s = (text ?? "").trim();
  if (!s)
    return "";
  if (!s.includes("npi-profile-card")) {
    return simpleMarkdownToHtml(s);
  }
  const cardBlocks = [];
  const placeholder = (i) => `\uE000CARD${i}\uE001`;
  const re = /<div class="npi-profile-card" markdown="1">\s*([\s\S]*?)<\/div>/g;
  let out = s.replace(re, (_full, inner) => {
    const i = cardBlocks.length;
    cardBlocks.push(inner);
    return placeholder(i);
  });
  out = simpleMarkdownToHtml(out);
  cardBlocks.forEach((inner, i) => {
    const cardHtml = '<div class="npi-profile-card">' + simpleMarkdownToHtmlInner(inner) + "</div>";
    out = out.replace(placeholder(i), cardHtml);
  });
  return out;
}
var MAX_SECTIONS = 4;
var MAX_BULLETS_PER_SECTION = 4;
function findMatchingCloseBrace(str, start) {
  let depth = 0;
  let inString = false;
  let escape = false;
  let quote = "";
  for (let i = start; i < str.length; i++) {
    const c = str[i];
    if (escape) {
      escape = false;
      continue;
    }
    if (inString) {
      if (c === "\\")
        escape = true;
      else if (c === quote)
        inString = false;
      continue;
    }
    if (c === '"' || c === "'") {
      inString = true;
      quote = c;
      continue;
    }
    if (c === "{")
      depth++;
    else if (c === "}") {
      depth--;
      if (depth === 0)
        return i;
    }
  }
  return -1;
}
function tryParseAnswerCard(message) {
  if (!message || !message.trim())
    return null;
  let raw = message.trim();
  if (raw.startsWith("```")) {
    const lines = raw.split("\n");
    if (lines[0].startsWith("```"))
      lines.shift();
    if (lines.length > 0 && lines[lines.length - 1].trim() === "```")
      lines.pop();
    raw = lines.join("\n").trim();
  }
  const parseOne = (str) => {
    try {
      const data = JSON.parse(str);
      if (data.mode !== "FACTUAL" && data.mode !== "CANONICAL" && data.mode !== "BLENDED")
        return null;
      if (typeof data.direct_answer !== "string")
        return null;
      if (!Array.isArray(data.sections))
        return null;
      const rawSections = data.sections.slice(0, MAX_SECTIONS);
      const sections = rawSections.map((sec) => ({
        intent: isSectionIntent(sec.intent) ? sec.intent : "process",
        label: typeof sec.label === "string" ? sec.label : "",
        bullets: Array.isArray(sec.bullets) ? sec.bullets : []
      }));
      return {
        mode: data.mode,
        direct_answer: data.direct_answer,
        sections,
        required_variables: Array.isArray(data.required_variables) ? data.required_variables : void 0,
        confidence_note: typeof data.confidence_note === "string" ? data.confidence_note : void 0,
        citations: Array.isArray(data.citations) ? data.citations : void 0,
        followups: Array.isArray(data.followups) ? data.followups : void 0
      };
    } catch {
      return null;
    }
  };
  if (raw.startsWith("{")) {
    const card = parseOne(raw);
    if (card)
      return card;
    const close = findMatchingCloseBrace(raw, 0);
    if (close !== -1) {
      const card2 = parseOne(raw.slice(0, close + 1));
      if (card2)
        return card2;
    }
    const fixed = raw.replace(/\}\]\}\],/g, "}],").replace(/\}\]\},/g, "}],");
    if (fixed !== raw) {
      const card3 = parseOne(fixed);
      if (card3)
        return card3;
    }
  }
  const modeRe = /["']mode["']\s*:\s*["'](FACTUAL|CANONICAL|BLENDED)["']/;
  const m = raw.match(modeRe);
  if (m) {
    const idx = raw.indexOf(m[0]);
    const start = raw.lastIndexOf("{", idx);
    if (start !== -1) {
      const end = findMatchingCloseBrace(raw, start);
      if (end !== -1) {
        const card = parseOne(raw.slice(start, end + 1));
        if (card)
          return card;
      }
    }
  }
  return null;
}
function splitSectionsByVisibility(sections, mode) {
  const all = sections.slice(0, MAX_SECTIONS);
  if (mode === "FACTUAL")
    return { visible: [], hidden: all };
  if (mode === "CANONICAL")
    return { visible: all, hidden: [] };
  const requirements = all.filter((s) => (s.intent ?? "process") === "requirements");
  const hidden = all.filter((s) => {
    const i = s.intent ?? "process";
    return i === "process" || i === "definitions" || i === "exceptions" || i === "references";
  });
  return { visible: requirements, hidden };
}
function renderOneSection(sec) {
  const sectionEl = document.createElement("div");
  sectionEl.className = "answer-card-section";
  const labelEl = document.createElement("div");
  labelEl.className = "answer-card-section-label";
  labelEl.textContent = sec.label || "";
  sectionEl.appendChild(labelEl);
  const bullets = (sec.bullets ?? []).slice(0, MAX_BULLETS_PER_SECTION);
  bullets.forEach((b) => {
    const li = document.createElement("div");
    li.className = "answer-card-bullet";
    li.textContent = b;
    sectionEl.appendChild(li);
  });
  if (bullets.length < (sec.bullets?.length ?? 0)) {
    const more = document.createElement("div");
    more.className = "answer-card-more";
    more.textContent = "Show more";
    more.setAttribute("aria-label", "Show more bullets");
    sectionEl.appendChild(more);
  }
  return sectionEl;
}
var CONFIDENCE_BADGE_MAP = {
  approved_authoritative: {
    label: "Approved \u2013 Authoritative",
    variant: "approved_authoritative",
    icon: "check"
  },
  approved_informational: {
    label: "Approved \u2013 Informational",
    variant: "approved_informational",
    icon: "shield"
  },
  proceed_with_caution: {
    label: "Proceed with Caution",
    variant: "proceed_with_caution",
    icon: "alert-triangle"
  },
  augmented_with_google: {
    label: "Augmented with External Search",
    variant: "augmented_with_google",
    icon: "globe"
  },
  informational_only: {
    label: "Informational Only",
    variant: "informational_only",
    icon: "info"
  },
  no_sources: {
    label: "No Sources",
    variant: "no_sources",
    icon: "alert-circle"
  }
};
function renderConfidenceBadge(strip) {
  const key = strip.toLowerCase().replace(/\s+/g, "_");
  const cfg = CONFIDENCE_BADGE_MAP[key] ?? {
    label: strip.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase()),
    variant: "unverified",
    icon: "info"
  };
  const wrap = document.createElement("div");
  wrap.className = "confidence-badge-wrap";
  const badge = document.createElement("span");
  badge.className = `confidence-badge confidence-badge--${cfg.variant}`;
  badge.setAttribute("aria-label", "Source confidence: " + cfg.label);
  const iconEl = document.createElement("span");
  iconEl.className = "confidence-badge-icon";
  iconEl.setAttribute("aria-hidden", "true");
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("fill", "none");
  svg.setAttribute("stroke", "currentColor");
  svg.setAttribute("stroke-width", "2");
  svg.setAttribute("stroke-linecap", "round");
  svg.setAttribute("stroke-linejoin", "round");
  svg.setAttribute("width", "14");
  svg.setAttribute("height", "14");
  const paths = {
    check: "M20 6L9 17l-5-5",
    shield: "M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z",
    "alert-triangle": "M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z M12 9v4 M12 17h.01",
    globe: "M21 12a9 9 0 01-9 9m9-9a9 9 0 00-9-9m9 9H3m9 9a9 9 0 01-9-9m9 9c1.657 0 3-4.03 3-9s-1.343-9-3-9m0 18c-1.657 0-3-4.03-3-9s1.343-9 3-9m-9 9a9 9 0 019-9",
    info: "M12 16v-4 M12 8h.01 M22 12c0 5.523-4.477 10-10 10S2 17.523 2 12 6.477 2 12 2s10 4.477 10 10z",
    "alert-circle": "M12 8v4m0 4h.01M22 12c0 5.523-4.477 10-10 10S2 17.523 2 12 6.477 2 12 2s10 4.477 10 10z"
  };
  const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
  path.setAttribute("d", paths[cfg.icon] ?? paths.info);
  svg.appendChild(path);
  iconEl.appendChild(svg);
  const labelEl = document.createElement("span");
  labelEl.className = "confidence-badge-label";
  labelEl.textContent = cfg.label;
  badge.appendChild(iconEl);
  badge.appendChild(labelEl);
  wrap.appendChild(badge);
  return wrap;
}
function createQcSampleShieldSvg() {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("class", "qc-audit-badge-shield-svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("width", "11");
  svg.setAttribute("height", "11");
  svg.setAttribute("aria-hidden", "true");
  const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
  path.setAttribute("fill", "none");
  path.setAttribute("stroke", "currentColor");
  path.setAttribute("stroke-width", "1.35");
  path.setAttribute("stroke-linejoin", "round");
  path.setAttribute(
    "d",
    "M12 2.5 19.5 5.2v5.8c0 3.2-2.4 6.5-7.5 8.5-5.1-2-7.5-5.3-7.5-8.5V5.2L12 2.5z"
  );
  svg.appendChild(path);
  return svg;
}
function renderQcAuditBadge(_qc) {
  const wrap = document.createElement("div");
  wrap.className = "qc-audit-badge-wrap";
  wrap.setAttribute("data-qc-sample", "1");
  const row = document.createElement("div");
  row.className = "qc-audit-badge-row";
  const badge = document.createElement("span");
  badge.className = "qc-audit-badge qc-audit-badge--neutral";
  badge.setAttribute(
    "aria-label",
    "This reply was checked by an automated quality review. It does not change your answer."
  );
  const iconEl = document.createElement("span");
  iconEl.className = "qc-audit-badge-icon";
  iconEl.setAttribute("aria-hidden", "true");
  iconEl.appendChild(createQcSampleShieldSvg());
  const labelEl = document.createElement("span");
  labelEl.className = "qc-audit-badge-label";
  labelEl.textContent = "Quality review completed";
  badge.appendChild(iconEl);
  badge.appendChild(labelEl);
  row.appendChild(badge);
  wrap.appendChild(row);
  const foot = document.createElement("p");
  foot.className = "qc-audit-badge-footnote";
  foot.textContent = "Does not change your answer.";
  wrap.appendChild(foot);
  return wrap;
}
function applyQcAuditToTurn(turnWrap, qc) {
  if (!qc)
    return;
  refreshLlmPerformanceQuality(turnWrap, qc);
  const assistantEl = turnWrap.querySelector(".message--assistant:last-of-type") ?? turnWrap.querySelector(".message--assistant");
  if (!assistantEl || assistantEl.querySelector(".qc-audit-badge-wrap"))
    return;
  const bubble = assistantEl.querySelector(".answer-card-bubble") ?? assistantEl.querySelector(".message-bubble");
  if (!bubble)
    return;
  const node = renderQcAuditBadge(qc);
  bubble.appendChild(node);
}
function refreshLlmPerformanceQuality(turnWrap, qc) {
  const panel = turnWrap.querySelector(".llm-performance");
  if (!panel)
    return;
  const eq = effectiveQcScore(qc);
  const qText = eq !== null ? eq.toFixed(2) : "\u2014";
  const oneline = panel.querySelector(".llm-performance-oneline");
  if (oneline) {
    const m = oneline.dataset.m || "\u2014";
    const sec = oneline.dataset.s || "0";
    const cost = oneline.dataset.c || "0";
    const leg = oneline.dataset.legacy === "1";
    oneline.textContent = `${leg ? "[LEGACY] " : ""}${m} \xB7 ${sec}s \xB7 $${cost} \xB7 quality ${qText}`;
  }
  const badgeQ = panel.querySelector("[data-llm-badge-quality]");
  if (badgeQ)
    badgeQ.textContent = `quality ${qText}`;
}
function renderAnswerCard(card, isError, opts) {
  const wrap = document.createElement("div");
  wrap.className = "message message--assistant answer-card answer-card--" + card.mode.toLowerCase() + (isError ? " message--error" : "");
  const bubble = document.createElement("div");
  bubble.className = "message-bubble answer-card-bubble";
  const direct = document.createElement("div");
  direct.className = "answer-card-direct";
  direct.textContent = card.direct_answer;
  bubble.appendChild(direct);
  if (opts?.showConfidenceBadge !== false && !opts?.suppressConfidenceForAdminQcFail) {
    bubble.appendChild(
      renderConfidenceBadge((opts?.sourceConfidenceStrip ?? "").trim() || "informational_only")
    );
  }
  const metaRow = document.createElement("div");
  metaRow.className = "answer-card-meta-row";
  if (card.required_variables && card.required_variables.length > 0) {
    const dep = document.createElement("span");
    dep.className = "answer-card-depends";
    dep.textContent = "Depends on: " + card.required_variables.join(", ");
    metaRow.appendChild(dep);
  }
  if (!opts?.suppressFollowups && card.followups && card.followups.length > 0 && metaRow.childNodes.length > 0) {
    const sep = document.createElement("span");
    sep.className = "answer-card-meta-sep";
    sep.textContent = " \xB7 ";
    metaRow.appendChild(sep);
  }
  if (!opts?.suppressFollowups && card.followups && card.followups.length > 0) {
    const confirmLabel = document.createElement("span");
    confirmLabel.className = "answer-card-confirm-label";
    confirmLabel.textContent = "Confirm";
    metaRow.appendChild(confirmLabel);
    card.followups.slice(0, 4).forEach((f) => {
      const chip = document.createElement("button");
      chip.type = "button";
      chip.className = "answer-card-followup-chip";
      const questionText = f.question || f.reason || f.field || "";
      chip.textContent = questionText;
      chip.setAttribute("aria-label", questionText);
      if (opts?.onFollowupClick && questionText) {
        chip.addEventListener("click", () => opts.onFollowupClick(questionText));
      }
      metaRow.appendChild(chip);
    });
  }
  if (metaRow.childNodes.length > 0)
    bubble.appendChild(metaRow);
  const { visible, hidden } = splitSectionsByVisibility(card.sections ?? [], card.mode);
  visible.forEach((sec) => bubble.appendChild(renderOneSection(sec)));
  if (hidden.length > 0) {
    const detailsBlock = document.createElement("div");
    detailsBlock.className = "answer-card-details";
    detailsBlock.setAttribute("aria-hidden", "true");
    hidden.forEach((sec) => detailsBlock.appendChild(renderOneSection(sec)));
    bubble.appendChild(detailsBlock);
    const toggleBtn = document.createElement("button");
    toggleBtn.type = "button";
    toggleBtn.className = "answer-card-show-details";
    toggleBtn.textContent = "Show details";
    toggleBtn.setAttribute("aria-label", "Show details");
    toggleBtn.setAttribute("aria-expanded", "false");
    toggleBtn.addEventListener("click", () => {
      const expanded = detailsBlock.classList.toggle("answer-card-details--expanded");
      detailsBlock.setAttribute("aria-hidden", expanded ? "false" : "true");
      toggleBtn.setAttribute("aria-expanded", String(expanded));
      toggleBtn.textContent = expanded ? "Hide details" : "Show details";
      toggleBtn.setAttribute("aria-label", expanded ? "Hide details" : "Show details");
    });
    bubble.appendChild(toggleBtn);
  }
  if (card.confidence_note && card.confidence_note.trim()) {
    const note = document.createElement("div");
    note.className = "answer-card-confidence";
    note.textContent = card.confidence_note;
    bubble.appendChild(note);
  }
  const followupQuestions = opts?.nextQuestions ?? [];
  if (followupQuestions.length > 0 && opts?.onFollowupClick) {
    const followupWrap = document.createElement("div");
    followupWrap.className = "answer-card-followups";
    const label = document.createElement("div");
    label.className = "answer-card-followups-label";
    label.textContent = "Follow-up questions";
    followupWrap.appendChild(label);
    const hint = document.createElement("div");
    hint.className = "answer-card-followups-hint";
    hint.textContent = "Tap a line to send it as your next message.";
    followupWrap.appendChild(hint);
    const chips = document.createElement("div");
    chips.className = "answer-card-followups-chips answer-card-followups-chips--stacked";
    followupQuestions.slice(0, 6).forEach((q) => {
      const btn = document.createElement("button");
      btn.type = "button";
      const text = q.trim() || "Ask this";
      btn.className = "answer-card-followup-chip answer-card-followup-chip--row";
      btn.textContent = text;
      btn.setAttribute("aria-label", "Send: " + text);
      btn.addEventListener("click", () => opts.onFollowupClick(text));
      chips.appendChild(btn);
    });
    followupWrap.appendChild(chips);
    bubble.appendChild(followupWrap);
  }
  if (opts?.qcAudit)
    bubble.appendChild(renderQcAuditBadge(opts.qcAudit));
  wrap.appendChild(bubble);
  return wrap;
}
function renderAssistantContent(body, isError, opts) {
  const card = tryParseAnswerCard(body);
  if (card)
    return renderAnswerCard(card, isError, { ...opts, nextQuestions: opts?.nextQuestions });
  const trimmed = (body ?? "").trim();
  if (trimmed.startsWith("{") && trimmed.length > 10) {
    const errWrap = document.createElement("div");
    errWrap.className = "message message--assistant" + (isError ? " message--error" : "");
    const errBubble = document.createElement("div");
    errBubble.className = "message-bubble";
    if (opts?.showConfidenceBadge !== false && !opts?.suppressConfidenceForAdminQcFail) {
      errBubble.appendChild(
        renderConfidenceBadge((opts?.sourceConfidenceStrip ?? "").trim() || "informational_only")
      );
    }
    const errText = document.createElement("div");
    errText.className = "message-bubble-text";
    errText.textContent = "Answer could not be displayed. Please try again.";
    errBubble.appendChild(errText);
    if (opts?.qcAudit)
      errBubble.appendChild(renderQcAuditBadge(opts.qcAudit));
    errWrap.appendChild(errBubble);
    return errWrap;
  }
  const wrap = document.createElement("div");
  wrap.className = "message message--assistant" + (isError ? " message--error" : "");
  const bubble = document.createElement("div");
  bubble.className = "message-bubble";
  if (opts?.showConfidenceBadge !== false && !opts?.suppressConfidenceForAdminQcFail) {
    bubble.appendChild(
      renderConfidenceBadge((opts?.sourceConfidenceStrip ?? "").trim() || "informational_only")
    );
  }
  const textEl = document.createElement("div");
  textEl.className = "message-bubble-text";
  if (opts?.renderAsMarkdown && trimmed.length > 0) {
    textEl.innerHTML = rosterStepMarkdownToHtml(body);
  } else {
    textEl.textContent = normalizeMessageText(sanitizeDisplayMessage(body));
  }
  bubble.appendChild(textEl);
  if (opts?.qcAudit)
    bubble.appendChild(renderQcAuditBadge(opts.qcAudit));
  wrap.appendChild(bubble);
  return wrap;
}
function renderRosterStepOutputs(stepOutputs) {
  const wrap = document.createElement("div");
  wrap.className = "roster-step-outputs";
  const header = document.createElement("div");
  header.className = "roster-step-outputs-header";
  header.setAttribute("role", "button");
  header.setAttribute("tabindex", "0");
  header.setAttribute("aria-expanded", "false");
  const headerTitle = document.createElement("span");
  headerTitle.className = "roster-step-outputs-title";
  headerTitle.textContent = "Step outputs (for validation)";
  const headerChevron = document.createElement("span");
  headerChevron.className = "roster-step-outputs-chevron";
  headerChevron.textContent = "\u25B6";
  header.appendChild(headerTitle);
  header.appendChild(headerChevron);
  const body = document.createElement("div");
  const hasFullReport = stepOutputs.length >= 12;
  body.className = hasFullReport ? "roster-step-outputs-body" : "roster-step-outputs-body roster-step-outputs-body--collapsed";
  if (hasFullReport) {
    header.setAttribute("aria-expanded", "true");
    headerChevron.textContent = "\u25BC";
  }
  for (const step of stepOutputs) {
    const section = document.createElement("div");
    section.className = "roster-step-section roster-step-section--collapsed";
    const stepLabel = (step.step_num ? `Step ${step.step_num}: ` : "") + (step.label || step.step_id);
    const rowHint = step.row_count > 0 ? ` (${step.row_count} row${step.row_count !== 1 ? "s" : ""})` : "";
    const sectionHeader = document.createElement("div");
    sectionHeader.className = "roster-step-section-header";
    sectionHeader.setAttribute("role", "button");
    sectionHeader.setAttribute("tabindex", "0");
    sectionHeader.setAttribute("aria-expanded", "false");
    sectionHeader.textContent = stepLabel + rowHint;
    const sectionBody = document.createElement("div");
    sectionBody.className = "roster-step-section-body";
    const hasMarkdown = !!(step.markdown_content && step.markdown_content.trim());
    const hasJson = !!(step.json_content && step.json_content.trim());
    if (hasMarkdown) {
      const mdWrap = document.createElement("div");
      mdWrap.className = "roster-step-markdown";
      mdWrap.innerHTML = rosterStepMarkdownToHtml(step.markdown_content.trim());
      sectionBody.appendChild(mdWrap);
      if (hasJson) {
        const dlBtn = document.createElement("button");
        dlBtn.type = "button";
        dlBtn.className = "roster-step-download-json";
        dlBtn.textContent = "Download JSON";
        dlBtn.setAttribute("aria-label", "Download NPI profile as JSON");
        dlBtn.addEventListener("click", () => {
          const blob = new Blob([step.json_content], { type: "application/json;charset=utf-8" });
          const url = URL.createObjectURL(blob);
          const a = document.createElement("a");
          a.href = url;
          a.download = "npi_profile.json";
          a.click();
          URL.revokeObjectURL(url);
        });
        sectionBody.appendChild(dlBtn);
      }
    } else {
      const pre = document.createElement("pre");
      pre.className = "roster-step-csv";
      pre.textContent = step.csv_content || "(no data)";
      sectionBody.appendChild(pre);
    }
    sectionHeader.addEventListener("click", () => {
      section.classList.toggle("roster-step-section--collapsed");
      sectionHeader.setAttribute("aria-expanded", section.classList.contains("roster-step-section--collapsed") ? "false" : "true");
    });
    sectionHeader.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        sectionHeader.click();
      }
    });
    section.appendChild(sectionHeader);
    section.appendChild(sectionBody);
    body.appendChild(section);
  }
  header.addEventListener("click", () => {
    body.classList.toggle("roster-step-outputs-body--collapsed");
    const collapsed = body.classList.contains("roster-step-outputs-body--collapsed");
    header.setAttribute("aria-expanded", collapsed ? "false" : "true");
    headerChevron.textContent = collapsed ? "\u25B6" : "\u25BC";
  });
  header.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      header.click();
    }
  });
  wrap.appendChild(header);
  wrap.appendChild(body);
  return wrap;
}
function renderRosterReportDownload(pdfBase64, reportMarkdown) {
  const wrap = document.createElement("div");
  wrap.className = "roster-report-download";
  const title = document.createElement("div");
  title.className = "roster-report-download-title";
  title.textContent = "Report";
  wrap.appendChild(title);
  const btns = document.createElement("div");
  btns.className = "roster-report-download-btns";
  const downloadIcon = () => {
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("width", "18");
    svg.setAttribute("height", "18");
    svg.setAttribute("aria-hidden", "true");
    svg.innerHTML = "<path fill='currentColor' d='M5 20h14v-2H5v2zM19 9h-4V3H9v6H5l7 7 7-7z'/>";
    return svg;
  };
  if (pdfBase64 && typeof pdfBase64 === "string" && pdfBase64.length > 0) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "roster-report-download-btn";
    btn.appendChild(downloadIcon());
    btn.appendChild(document.createTextNode(" Download report (PDF)"));
    btn.addEventListener("click", () => {
      try {
        const bytes = Uint8Array.from(atob(pdfBase64), (c) => c.charCodeAt(0));
        const blob = new Blob([bytes], { type: "application/pdf" });
        const url = URL.createObjectURL(blob);
        const a = document.createElement("a");
        a.href = url;
        a.download = "credentialing_report.pdf";
        a.click();
        URL.revokeObjectURL(url);
      } catch (e) {
        console.warn("PDF download failed:", e);
      }
    });
    btns.appendChild(btn);
  }
  if (reportMarkdown && typeof reportMarkdown === "string" && reportMarkdown.trim().length > 0) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "roster-report-download-btn";
    btn.appendChild(downloadIcon());
    btn.appendChild(document.createTextNode(" Download report (Markdown)"));
    btn.addEventListener("click", () => {
      const blob = new Blob([reportMarkdown], { type: "text/markdown;charset=utf-8" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = "credentialing_report.md";
      a.click();
      URL.revokeObjectURL(url);
    });
    btns.appendChild(btn);
  }
  wrap.appendChild(btns);
  return wrap;
}
function parseMessageAndSources(fullMessage) {
  const raw = (fullMessage ?? "").trim();
  const sourcesIdx = raw.search(/\nSources:\s*\n/i);
  if (sourcesIdx === -1) {
    return { body: raw, sources: [] };
  }
  const body = raw.slice(0, sourcesIdx).trim();
  const afterSources = raw.slice(sourcesIdx).replace(/^\s*Sources:\s*\n/i, "").trim();
  const sources = [];
  const lineRe = /^\s*\[\s*(\d+)\s*\]\s*(.+?)(?:\s*\(page\s+(\d+)\))?\s*[—–-]\s*(.+)$/gm;
  let m;
  while ((m = lineRe.exec(afterSources)) !== null) {
    sources.push({
      index: parseInt(m[1], 10),
      document_name: m[2].trim(),
      page_number: m[3] != null ? parseInt(m[3], 10) : null,
      snippet: (m[4] ?? "").trim()
    });
  }
  return { body, sources };
}
function renderUserMessage(text) {
  const wrap = document.createElement("div");
  wrap.className = "message message--user";
  const bubble = document.createElement("div");
  bubble.className = "message-bubble";
  bubble.textContent = text;
  wrap.appendChild(bubble);
  return wrap;
}
function renderThinkingBlock(initialLines, opts) {
  const block = document.createElement("div");
  block.className = "thinking-block thinking-block--compact" + (initialLines.length ? "" : " collapsed");
  const preview = document.createElement("div");
  preview.className = "thinking-preview";
  preview.setAttribute("role", "button");
  preview.setAttribute("tabindex", "0");
  preview.setAttribute("aria-expanded", initialLines.length > 0 ? "true" : "false");
  const word = document.createElement("span");
  word.className = "thinking-word";
  word.textContent = "Thinking";
  const lineEl = document.createElement("span");
  lineEl.className = "thinking-rule";
  preview.appendChild(word);
  preview.appendChild(lineEl);
  const body = document.createElement("div");
  body.className = "thinking-body";
  initialLines.forEach((line) => {
    const div = document.createElement("div");
    div.className = "thinking-line";
    div.textContent = line;
    body.appendChild(div);
  });
  function collapse() {
    block.classList.add("collapsed");
    preview.setAttribute("aria-expanded", "false");
  }
  function toggle() {
    block.classList.toggle("collapsed");
    const isExp = !block.classList.contains("collapsed");
    preview.setAttribute("aria-expanded", String(isExp));
    if (isExp)
      opts?.onExpand?.();
  }
  preview.addEventListener("click", toggle);
  preview.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      toggle();
    }
  });
  block.appendChild(preview);
  block.appendChild(body);
  let lastStatusLine = "";
  return {
    el: block,
    setPreview(text) {
      preview.replaceChildren();
      const w = document.createElement("span");
      w.className = "thinking-word";
      w.textContent = thinkingFriendlyStatus(text);
      const r = document.createElement("span");
      r.className = "thinking-rule";
      preview.appendChild(w);
      preview.appendChild(r);
    },
    addLine(line) {
      lastStatusLine = line;
      word.textContent = thinkingFriendlyStatus(line);
      const div = document.createElement("div");
      div.className = "thinking-line";
      div.textContent = line;
      body.appendChild(div);
      block.classList.remove("collapsed");
      preview.setAttribute("aria-expanded", "true");
      body.scrollTop = body.scrollHeight;
    },
    done(_lineCount) {
      word.textContent = lastStatusLine ? thinkingFriendlyStatus(lastStatusLine) : "Ready";
      block.classList.add("thinking-block--done");
      setTimeout(() => {
        collapse();
      }, 2500);
    }
  };
}
function renderNextQuestions(questions, onSelect) {
  if (!questions.length)
    return document.createElement("div");
  const wrap = document.createElement("div");
  wrap.className = "next-questions";
  const label = document.createElement("div");
  label.className = "next-questions-label";
  label.textContent = "Follow-up questions";
  wrap.appendChild(label);
  const hint = document.createElement("div");
  hint.className = "next-questions-hint";
  hint.textContent = "Tap a line to send it as your next message.";
  wrap.appendChild(hint);
  const chips = document.createElement("div");
  chips.className = "next-questions-chips next-questions-chips--stacked";
  questions.slice(0, 6).forEach((q) => {
    const btn = document.createElement("button");
    btn.type = "button";
    const text = q.trim() || "Ask this";
    btn.className = "next-questions-chip next-questions-chip--row";
    btn.textContent = text;
    btn.setAttribute("aria-label", "Send: " + text);
    btn.addEventListener("click", () => onSelect(text));
    chips.appendChild(btn);
  });
  wrap.appendChild(chips);
  return wrap;
}
function renderClarificationOptions(opts, onSelect) {
  const wrap = document.createElement("div");
  wrap.className = "clarification-options";
  for (const opt of opts) {
    const group = document.createElement("div");
    group.className = "clarification-option-group";
    const labelEl = document.createElement("div");
    labelEl.className = "clarification-option-label";
    labelEl.textContent = opt.label;
    group.appendChild(labelEl);
    const chips = document.createElement("div");
    chips.className = "clarification-option-chips";
    for (const c of opt.choices) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "clarification-option-chip";
      btn.textContent = c.label;
      btn.addEventListener("click", () => onSelect(c.value));
      chips.appendChild(btn);
    }
    group.appendChild(chips);
    wrap.appendChild(group);
  }
  return wrap;
}
function renderAssistantMessage(text, isError, opts) {
  const wrap = document.createElement("div");
  wrap.className = "message message--assistant" + (isError ? " message--error" : "");
  const bubble = document.createElement("div");
  bubble.className = "message-bubble";
  bubble.appendChild(
    renderConfidenceBadge((opts?.sourceConfidenceStrip ?? "").trim() || "informational_only")
  );
  const textEl = document.createElement("div");
  textEl.className = "message-bubble-text";
  textEl.textContent = normalizeMessageText(text);
  bubble.appendChild(textEl);
  wrap.appendChild(bubble);
  return wrap;
}
function createThumbIcon(type) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("fill", "none");
  svg.setAttribute("stroke", "currentColor");
  svg.setAttribute("stroke-width", "2");
  svg.setAttribute("stroke-linecap", "round");
  svg.setAttribute("stroke-linejoin", "round");
  svg.setAttribute("width", "18");
  svg.setAttribute("height", "18");
  svg.setAttribute("aria-hidden", "true");
  const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
  path.setAttribute(
    "d",
    type === "up" ? "M14 9V5a3 3 0 0 0-3-3l-4 9v11h11.28a2 2 0 0 0 2-1.7l1.38-9a2 2 0 0 0-2-2.3zM7 22H4a2 2 0 0 1-2-2v-7a2 2 0 0 1 2-2h3" : "M10 15v4a3 3 0 0 0 3 3l4-9V2H5.72a2 2 0 0 0-2 1.7l-1.38 9a2 2 0 0 0 2 2.3zm7-13h2.67A2.31 2.31 0 0 1 22 4v7a2.31 2.31 0 0 1-2.33 2H17"
  );
  svg.appendChild(path);
  return svg;
}
function renderFeedback(correlationId) {
  const bar = document.createElement("div");
  bar.className = "feedback";
  const left = document.createElement("div");
  left.className = "feedback-left";
  const actions = document.createElement("div");
  actions.className = "feedback-actions";
  const up = document.createElement("button");
  up.type = "button";
  up.className = "feedback-thumb";
  up.setAttribute("aria-label", "Good response");
  up.appendChild(createThumbIcon("up"));
  const down = document.createElement("button");
  down.type = "button";
  down.className = "feedback-thumb";
  down.setAttribute("aria-label", "Bad response");
  down.appendChild(createThumbIcon("down"));
  const commentArea = document.createElement("div");
  commentArea.className = "feedback-comment-area";
  commentArea.style.display = "none";
  const commentForm = document.createElement("div");
  commentForm.className = "feedback-comment-form";
  const textarea = document.createElement("textarea");
  textarea.placeholder = "What could we improve? (optional)";
  textarea.rows = 2;
  const commentBtns = document.createElement("div");
  commentBtns.className = "feedback-comment-buttons";
  const submitBtn = document.createElement("button");
  submitBtn.type = "button";
  submitBtn.textContent = "Submit";
  const cancelBtn = document.createElement("button");
  cancelBtn.type = "button";
  cancelBtn.textContent = "Cancel";
  commentBtns.appendChild(submitBtn);
  commentBtns.appendChild(cancelBtn);
  commentForm.appendChild(textarea);
  commentForm.appendChild(commentBtns);
  commentArea.appendChild(commentForm);
  function postFeedback(rating, comment) {
    fetch(API_BASE + "/chat/feedback/" + encodeURIComponent(correlationId), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ rating, comment })
    }).then(() => {
      up.disabled = true;
      down.disabled = true;
      up.classList.toggle("selected", rating === "up");
      down.classList.toggle("selected", rating === "down");
      commentArea.style.display = "none";
    }).catch(() => {
    });
  }
  up.addEventListener("click", () => {
    if (up.disabled)
      return;
    postFeedback("up", null);
  });
  down.addEventListener("click", () => {
    if (down.disabled)
      return;
    commentArea.style.display = "block";
    textarea.focus();
  });
  submitBtn.addEventListener("click", () => {
    postFeedback("down", textarea.value.trim() || null);
  });
  cancelBtn.addEventListener("click", () => {
    commentArea.style.display = "none";
  });
  const copy = document.createElement("button");
  copy.type = "button";
  copy.setAttribute("aria-label", "Copy");
  copy.textContent = "Copy";
  copy.addEventListener("click", () => {
    const msg = bar.closest(".chat-turn")?.querySelector(".message--assistant .message-bubble");
    if (msg?.textContent) {
      navigator.clipboard.writeText(msg.textContent).then(() => {
        copy.textContent = "Copied";
        setTimeout(() => copy.textContent = "Copy", 1500);
      });
    }
  });
  left.appendChild(up);
  left.appendChild(down);
  left.appendChild(commentArea);
  actions.appendChild(copy);
  bar.appendChild(left);
  bar.appendChild(actions);
  return bar;
}
function getRagDocumentUrl(documentId, pageNumber, citeText) {
  const rawBase = typeof window !== "undefined" ? window.RAG_APP_BASE : void 0;
  const base = typeof rawBase === "string" ? rawBase.trim() : "";
  if (!base || !documentId?.trim())
    return null;
  const params = new URLSearchParams({ tab: "read", documentId: documentId.trim() });
  if (pageNumber != null)
    params.set("pageNumber", String(pageNumber));
  const ct = (citeText ?? "").trim().slice(0, 400);
  if (ct)
    params.set("citeText", ct);
  return `${base.replace(/\/$/, "")}?${params.toString()}`;
}
function resolveSourceOpenHref(s) {
  if (s.open_href && isAllowedOpenHref(s.open_href))
    return s.open_href.trim();
  const cite = (s.cite_text ?? "").trim() || (s.snippet ?? "").trim().slice(0, 400);
  return getRagDocumentUrl(s.document_id, s.page_number, cite || null);
}
function openDocumentOrSnippet(s) {
  const cite = (s.cite_text ?? "").trim() || (s.snippet ?? "").trim().slice(0, 400);
  const url = getRagDocumentUrl(s.document_id, s.page_number, cite || null);
  if (url) {
    window.open(url, "_blank", "noopener,noreferrer");
  }
}
var LLM_PERF_LS = "mobius_show_llm_performance";
var LEGACY_LLM_INSIGHTS_LS = "mobius_show_answer_insights";
var LLM_PERF_ACTIVITY = "llm_performance";
var LLM_PERF_ACTIVITY_ALIASES = ["answer_insights", "technical", "developer"];
function getShowLlmPerformance(profile) {
  try {
    const v = localStorage.getItem(LLM_PERF_LS) ?? localStorage.getItem(LEGACY_LLM_INSIGHTS_LS);
    if (v === "1")
      return true;
    if (v === "0")
      return false;
  } catch {
  }
  const acts = profile?.activities ?? [];
  if (acts.includes(LLM_PERF_ACTIVITY))
    return true;
  return LLM_PERF_ACTIVITY_ALIASES.some((a) => acts.includes(a));
}
function adminShouldSuppressConfidenceForQc(profile, qc) {
  if (!getShowLlmPerformance(profile))
    return false;
  if (!qc || typeof qc.passed !== "boolean")
    return false;
  return qc.passed === false;
}
function removeConfidenceBadgesInTurn(turnWrap) {
  turnWrap.querySelectorAll(".confidence-badge-wrap").forEach((el2) => el2.remove());
}
function confidenceFromStrip(strip) {
  const s = (strip || "").toLowerCase().replace(/_/g, "_");
  if (!s)
    return "medium";
  if (s.includes("authoritative") || s.includes("approved") && !s.includes("caution"))
    return "high";
  if (s.includes("no_sources") || s.includes("informational_only"))
    return "low";
  if (s.includes("caution") || s.includes("augmented"))
    return "medium";
  return "medium";
}
function formatCostShort(n) {
  if (n <= 0)
    return "0.000";
  if (n < 1e-4)
    return n.toFixed(6);
  if (n < 0.01)
    return n.toFixed(4);
  return n.toFixed(3);
}
function formatRouterNote(meta, rows) {
  const fromMeta = meta?.router_by_stage;
  if (fromMeta && fromMeta.length > 0) {
    const lines = ["Why these models were picked (per LLM call):"];
    fromMeta.forEach((x) => {
      const bits = [];
      if (x.mode)
        bits.push(x.mode);
      if (x.exploration)
        bits.push("exploration round");
      if (x.circuit_relief)
        bits.push("circuit relief");
      const tag = bits.length ? `[${bits.join(" \xB7 ")}] ` : "";
      let comp = "";
      if (x.composite_pg != null || x.composite_call != null) {
        const pg = x.composite_pg != null && Number.isFinite(Number(x.composite_pg)) ? Number(x.composite_pg).toFixed(2) : "\u2014";
        const pc = x.composite_call != null && Number.isFinite(Number(x.composite_call)) ? Number(x.composite_call).toFixed(2) : "\u2014";
        comp = ` composite PG/call ${pg}/${pc}.`;
      }
      lines.push(
        `\u2022 ${(x.stage || "?").toString()} \xB7 ${(x.model || "?").toString()}: ${tag}${(x.reason || "\u2014").toString()}${comp}`
      );
    });
    return lines.join("\n");
  }
  const intRow = [...rows].reverse().find((r) => r.stage === "integrator");
  const intModel = intRow?.model || meta?.primary_model || "\u2014";
  const explore = meta?.integrator_exploration;
  const reactN = rows.filter((r) => (r.stage || "").startsWith("react_")).length;
  const conf = explore === true ? "medium, exploration band" : explore === false ? "building, exploitation" : "routing";
  if (meta?.pipeline === "legacy") {
    return `[LEGACY] Plan \u2192 resolve path (no ReAct tool rounds). Integrator: ${intModel}. Forced exploration (every 20 stage calls) applies on enabled pipelines.`;
  }
  let t = `Router decision \u2014 integrator: ${intModel} selected (confidence ${conf}`;
  t += explore === true ? "; model still gathering quality samples in router band." : ").";
  if (reactN > 0) {
    t += ` ReAct: ${reactN} reasoning round(s). Exploration round uses least-sampled model periodically (interval 20) for A/B calibration \u2014 compare stages in llm_calls.`;
  }
  t += " Stage table \u201CComposite PG / call\u201D: batch score at router pick vs same formula on this call (latency, cost, QA, error). Thompson blends priors with the batch composite (not QA alone).";
  return t;
}
function escapeHtml2(s) {
  return (s ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}
function parseScoreValue(v) {
  if (typeof v === "number" && Number.isFinite(v))
    return Math.max(0, Math.min(1, v));
  if (typeof v === "string" && v.trim()) {
    const n = parseFloat(v);
    if (Number.isFinite(n))
      return Math.max(0, Math.min(1, n));
  }
  return void 0;
}
function effectiveQcScore(qc) {
  if (!qc)
    return null;
  const u = parseScoreValue(qc.user_score);
  if (u !== void 0)
    return u;
  const a = parseScoreValue(qc.automated_score) ?? parseScoreValue(qc.score);
  if (a !== void 0)
    return a;
  return qc.passed ? 1 : 0;
}
function formatRubricDimensionLabel(key) {
  return key.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}
function getSubScoreEntries(qc) {
  const raw = qc.sub_scores;
  if (!raw || typeof raw !== "object")
    return [];
  return Object.keys(raw).sort().map((k) => {
    const n = parseScoreValue(raw[k]);
    return n !== void 0 ? [k, n] : null;
  }).filter((x) => x != null);
}
function buildAdjudicatorDetailWrap(qc) {
  const wrap = document.createElement("div");
  wrap.className = "adjudicator-scorecard-detail-wrap";
  const hSum = document.createElement("div");
  hSum.className = "adjudicator-scorecard-section-label";
  hSum.textContent = "Score summary";
  wrap.appendChild(hSum);
  const auto = parseScoreValue(qc.automated_score) ?? parseScoreValue(qc.score) ?? (qc.passed ? 1 : 0);
  const user = parseScoreValue(qc.user_score);
  const eff = effectiveQcScore(qc);
  const tbl = document.createElement("table");
  tbl.className = "adjudicator-scorecard-matrix";
  const addRow = (label, val) => {
    const tr = document.createElement("tr");
    const th = document.createElement("th");
    th.textContent = label;
    const td = document.createElement("td");
    td.className = "adjudicator-scorecard-matrix-val";
    td.textContent = val;
    tr.appendChild(th);
    tr.appendChild(td);
    tbl.appendChild(tr);
  };
  addRow("Automated (overall)", auto.toFixed(2));
  addRow("User override", user !== void 0 ? user.toFixed(2) : "\u2014");
  addRow("Effective (displayed)", eff !== null ? eff.toFixed(2) : "\u2014");
  if (user !== void 0) {
    const delta = user - auto;
    const sign = delta >= 0 ? "+" : "";
    addRow("\u0394 (user \u2212 automated)", `${sign}${delta.toFixed(2)}`);
  }
  wrap.appendChild(tbl);
  const hSub = document.createElement("div");
  hSub.className = "adjudicator-scorecard-section-label";
  hSub.textContent = "Rubric sub-scores";
  wrap.appendChild(hSub);
  const entries = getSubScoreEntries(qc);
  if (entries.length === 0) {
    const p = document.createElement("p");
    p.className = "adjudicator-scorecard-subscores-empty";
    p.textContent = "No rubric dimensions in this audit (older run, or adjudicator did not return JSON sub_scores).";
    wrap.appendChild(p);
  } else {
    const stbl = document.createElement("table");
    stbl.className = "adjudicator-scorecard-subscores";
    entries.forEach(([k, v]) => {
      const tr = document.createElement("tr");
      const th = document.createElement("th");
      th.textContent = formatRubricDimensionLabel(k);
      const td = document.createElement("td");
      const inner = document.createElement("div");
      inner.className = "adjudicator-scorecard-subscore-cell-inner";
      const pct = Math.round(Math.max(0, Math.min(1, v)) * 100);
      const valSpan = document.createElement("span");
      valSpan.className = "adjudicator-scorecard-subscore-val";
      valSpan.textContent = v.toFixed(2);
      const barWrap = document.createElement("span");
      barWrap.className = "adjudicator-scorecard-subscore-bar-wrap";
      const bar = document.createElement("span");
      bar.className = "adjudicator-scorecard-subscore-bar";
      bar.style.width = `${pct}%`;
      barWrap.appendChild(bar);
      inner.appendChild(valSpan);
      inner.appendChild(barWrap);
      td.appendChild(inner);
      tr.appendChild(th);
      tr.appendChild(td);
      stbl.appendChild(tr);
    });
    wrap.appendChild(stbl);
  }
  const hasTech = qc.adjudicator_model && String(qc.adjudicator_model).trim() || qc.adjudicator_llm_call_id && String(qc.adjudicator_llm_call_id).trim();
  if (hasTech) {
    const metaTech = document.createElement("div");
    metaTech.className = "adjudicator-scorecard-tech";
    if (qc.adjudicator_model && String(qc.adjudicator_model).trim()) {
      const line = document.createElement("div");
      line.className = "adjudicator-scorecard-tech-line";
      line.textContent = `Adjudicator model: ${String(qc.adjudicator_model).trim()}`;
      metaTech.appendChild(line);
    }
    if (qc.adjudicator_llm_call_id && String(qc.adjudicator_llm_call_id).trim()) {
      const line = document.createElement("div");
      line.className = "adjudicator-scorecard-tech-line adjudicator-scorecard-tech-line--mono";
      line.textContent = `Adjudicator call id: ${String(qc.adjudicator_llm_call_id).trim()}`;
      metaTech.appendChild(line);
    }
    wrap.appendChild(metaTech);
  }
  const raw = (qc.adjudicator_full_response || "").toString().trim();
  if (raw) {
    const det = document.createElement("details");
    det.className = "adjudicator-scorecard-raw-details";
    const summ = document.createElement("summary");
    summ.textContent = "Full adjudicator response (raw)";
    const pre = document.createElement("pre");
    pre.className = "adjudicator-scorecard-pre adjudicator-scorecard-pre--raw";
    pre.textContent = raw.slice(0, 8e3);
    det.appendChild(summ);
    det.appendChild(pre);
    wrap.appendChild(det);
  }
  return wrap;
}
function llmUsageBreakdownPatchSig(rows) {
  return rows.map(
    (r) => `${r.llm_call_id ?? ""}:${r.quality_score ?? ""}:${(r.quality_source ?? "").slice(0, 32)}:${r.router_composite_at_pick ?? ""}:${r.per_call_composite ?? ""}`
  ).join("|");
}
function formatCompositeTooltip(pg, pgBrk, pc, pcBrk) {
  const lines = [
    "Composite = q\xD70.25 + rel\xD70.25 + latTerm\xD70.25 + costTerm\xD70.25.",
    "Linear caps depend on stage type (planner/rag/integrator/cheap stages, \u2026).",
    "PG @ pick: p95 latency + avg cost vs those caps; per-call: this latency vs cap.",
    "Per-call cost term uses list $ from input/output tokens \xD7 registered $/1K when tokens > 0, else billed cost.",
    "rel=0 if call_status=error (per-call) or from batch hard_error_rate (PG)."
  ];
  if (pg !== null) {
    lines.push(`PG @ pick: ${pg.toFixed(3)}`);
    if (pgBrk && Object.keys(pgBrk).length)
      lines.push(JSON.stringify(pgBrk));
  } else
    lines.push("PG @ pick: \u2014 (no stats row yet)");
  if (pc !== null) {
    lines.push(`This call: ${pc.toFixed(3)}`);
    if (pcBrk && Object.keys(pcBrk).length)
      lines.push(JSON.stringify(pcBrk));
  }
  return lines.join("\n");
}
function fillLlmPerformanceTbody(tbody, rows) {
  const maxLat = Math.max(1, ...rows.map((r) => Math.max(0, Number(r.latency_ms) || 0)));
  tbody.replaceChildren();
  rows.forEach((r) => {
    const tr = document.createElement("tr");
    const stageName = (r.display_stage || r.stage || "\u2014").trim();
    const latMs = Math.max(0, Number(r.latency_ms) || 0);
    const latSec = latMs > 0 ? (latMs / 1e3).toFixed(1) : "\u2014";
    const rowCost = r.cost_usd != null && Number(r.cost_usd) > 0 ? formatCostShort(Number(r.cost_usd)) : "0.000";
    const pct = maxLat > 0 ? Math.round(latMs / maxLat * 100) : 0;
    const rawStatus = (r.call_status || "ok").toLowerCase();
    const stClass = rawStatus === "error" ? "llm-performance-status--error" : "llm-performance-status--ok";
    const stLabel = rawStatus === "error" ? "Error" : "OK";
    const whyFull = (r.router_reason || "").trim();
    const mode = (r.router_selection || "").trim();
    const qSamples = r.router_quality_samples_at_pick;
    const qAvg = r.router_avg_quality_at_pick;
    let whyLine = "";
    if (mode)
      whyLine += `[${mode}] `;
    if (r.router_exploration_round)
      whyLine += "exploration \xB7 ";
    if (r.router_circuit_relief)
      whyLine += "circuit relief \xB7 ";
    if (qSamples != null && Number.isFinite(qSamples))
      whyLine += `PG samples=${qSamples}${qAvg != null && Number.isFinite(qAvg) ? ` \xB7 avgQ\u2248${Number(qAvg).toFixed(2)}` : ""} \xB7 `;
    whyLine += whyFull || "\u2014";
    const whyShort = whyLine.length > 140 ? whyLine.slice(0, 137) + "\u2026" : whyLine;
    const whyTitle = escapeHtml2(whyLine.length > 200 ? whyLine.slice(0, 2e3) : whyLine);
    const qRaw = r.quality_score;
    const qNum = qRaw != null && Number.isFinite(Number(qRaw)) ? Number(qRaw) : null;
    const qDisp = qNum !== null ? qNum.toFixed(2) : "\u2014";
    const qSrc = (r.quality_source || "").trim();
    const qTitle = escapeHtml2(qSrc ? qSrc.slice(0, 500) : "");
    const pgN = r.router_composite_at_pick != null && Number.isFinite(Number(r.router_composite_at_pick)) ? Number(r.router_composite_at_pick) : null;
    const pcN = r.per_call_composite != null && Number.isFinite(Number(r.per_call_composite)) ? Number(r.per_call_composite) : null;
    const pgBrk = r.router_composite_breakdown;
    const pcBrk = r.per_call_composite_breakdown;
    const compTitle = escapeHtml2(
      formatCompositeTooltip(pgN, pgBrk, pcN, pcBrk).slice(0, 3500)
    );
    const compShort = (pgN !== null ? pgN.toFixed(2) : "\u2014") + " / " + (pcN !== null ? pcN.toFixed(2) : "\u2014");
    tr.innerHTML = `<td>${escapeHtml2(stageName)}</td><td class="llm-performance-mono">${escapeHtml2(
      (r.model || "\u2014").trim()
    )}</td><td class="llm-performance-why" title="${whyTitle}">${escapeHtml2(whyShort)}</td><td class="llm-performance-lat-cell"><span class="llm-performance-lat-bar-wrap"><span class="llm-performance-lat-bar" style="width:${pct}%"></span></span><span class="llm-performance-lat-num">${latSec}${latSec !== "\u2014" ? "s" : ""}</span></td><td class="llm-performance-mono">$${rowCost}</td><td class="llm-performance-composite-cell" title="${compTitle}">${escapeHtml2(
      compShort
    )}</td><td class="llm-performance-qa-cell" title="${qTitle}">${escapeHtml2(
      qDisp
    )}</td><td class="llm-performance-status-cell"><span class="${stClass}">${escapeHtml2(
      stLabel
    )}</span></td>`;
    tbody.appendChild(tr);
  });
}
function renderAdjudicatorScorecard(qc, correlationId, technicalFeedback) {
  const wrap = document.createElement("div");
  wrap.className = "adjudicator-scorecard collapsed";
  const auto = parseScoreValue(qc.automated_score) ?? parseScoreValue(qc.score);
  const userS = parseScoreValue(qc.user_score);
  const effective = effectiveQcScore(qc);
  const effStr = effective !== null ? effective.toFixed(2) : "\u2014";
  const autoStr = auto !== void 0 ? auto.toFixed(2) : qc.passed ? "1.00" : "0.00";
  const vUi = adjudicationVerdictUi(qc);
  const preview = document.createElement("div");
  preview.className = "adjudicator-scorecard-preview";
  preview.setAttribute("role", "button");
  preview.setAttribute("tabindex", "0");
  preview.setAttribute("aria-expanded", "false");
  const titleEl = document.createElement("span");
  titleEl.className = "adjudicator-scorecard-title";
  titleEl.textContent = "QA / Adjudicator";
  const oneline = document.createElement("span");
  oneline.className = "adjudicator-scorecard-oneline";
  oneline.dataset.effective = effStr;
  oneline.textContent = `${vUi.shortLabel} \xB7 score ${effStr} \xB7 ${(qc.source || "\u2014").toString().slice(0, 24)}`;
  const chev = document.createElement("span");
  chev.className = "adjudicator-scorecard-chevron";
  chev.setAttribute("aria-hidden", "true");
  chev.textContent = "\u25BC";
  preview.appendChild(titleEl);
  preview.appendChild(oneline);
  preview.appendChild(chev);
  const body = document.createElement("div");
  body.className = "adjudicator-scorecard-body";
  const badges = document.createElement("div");
  badges.className = "adjudicator-scorecard-badges";
  const b1 = document.createElement("span");
  b1.className = `adjudicator-scorecard-badge adjudicator-scorecard-badge--${vUi.badgeVariant}`;
  b1.textContent = vUi.verdictBadgeText;
  const b2 = document.createElement("span");
  b2.className = "adjudicator-scorecard-badge adjudicator-scorecard-badge--score";
  b2.textContent = `Effective score: ${effStr}`;
  const b3 = document.createElement("span");
  b3.className = "adjudicator-scorecard-badge adjudicator-scorecard-badge--auto";
  b3.textContent = `Automated: ${autoStr}`;
  const b4 = document.createElement("span");
  b4.className = "adjudicator-scorecard-badge adjudicator-scorecard-badge--user";
  b4.textContent = userS !== void 0 ? `User: ${userS.toFixed(2)}` : "User: \u2014";
  badges.appendChild(b1);
  badges.appendChild(b2);
  badges.appendChild(b3);
  badges.appendChild(b4);
  body.appendChild(badges);
  body.appendChild(buildAdjudicatorDetailWrap(qc));
  const reasonBox = document.createElement("div");
  reasonBox.className = "adjudicator-scorecard-reason";
  reasonBox.innerHTML = `<strong>Rationale</strong><pre class="adjudicator-scorecard-pre">${escapeHtml2(
    (qc.reason || "\u2014").toString().slice(0, 4e3)
  )}</pre>`;
  body.appendChild(reasonBox);
  const metaRow = document.createElement("div");
  metaRow.className = "adjudicator-scorecard-meta";
  metaRow.textContent = `Source: ${(qc.source || "\u2014").toString()} \xB7 ${(qc.audited_at || "\u2014").toString()}`;
  body.appendChild(metaRow);
  const editWrap = document.createElement("div");
  editWrap.className = "adjudicator-scorecard-edit";
  const editLabel = document.createElement("label");
  editLabel.className = "adjudicator-scorecard-edit-label";
  editLabel.htmlFor = `qc-user-score-${correlationId.slice(0, 8)}`;
  editLabel.textContent = "Your score (0\u20131, persisted)";
  const inputRow = document.createElement("div");
  inputRow.className = "adjudicator-scorecard-edit-row";
  const num = document.createElement("input");
  num.type = "number";
  num.className = "adjudicator-scorecard-score-input";
  num.id = `qc-user-score-${correlationId.slice(0, 8)}`;
  num.min = "0";
  num.max = "1";
  num.step = "0.01";
  num.value = userS !== void 0 ? String(userS) : effective !== null ? String(Math.round(effective * 100) / 100) : "0.8";
  const saveBtn = document.createElement("button");
  saveBtn.type = "button";
  saveBtn.className = "adjudicator-scorecard-save";
  saveBtn.textContent = "Save score";
  const note = document.createElement("textarea");
  note.className = "adjudicator-scorecard-note";
  note.rows = 2;
  note.placeholder = "Optional note (persisted)";
  note.value = (qc.user_score_comment || "").toString();
  inputRow.appendChild(num);
  inputRow.appendChild(saveBtn);
  editWrap.appendChild(editLabel);
  editWrap.appendChild(inputRow);
  editWrap.appendChild(note);
  body.appendChild(editWrap);
  saveBtn.addEventListener("click", () => {
    const raw = parseFloat(num.value);
    if (Number.isNaN(raw) || raw < 0 || raw > 1) {
      saveBtn.textContent = "0\u20131 only";
      window.setTimeout(() => {
        saveBtn.textContent = "Save score";
      }, 1500);
      return;
    }
    saveBtn.disabled = true;
    fetch(API_BASE + "/chat/qc-user-score/" + encodeURIComponent(correlationId), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        user_score: raw,
        user_score_comment: note.value.trim() || null
      })
    }).then((r) => r.json()).then((j) => {
      const nq = j.qc_audit;
      if (nq && typeof nq.passed === "boolean") {
        syncAdjudicatorScorecardDom(wrap, nq, oneline, badges);
        refreshLlmPerformanceQuality(wrap.closest(".chat-turn"), nq);
      }
      saveBtn.textContent = "Saved";
    }).catch(() => {
      saveBtn.textContent = "Error";
    }).finally(() => {
      window.setTimeout(() => {
        saveBtn.disabled = false;
        if (saveBtn.textContent === "Saved")
          saveBtn.textContent = "Save score";
        if (saveBtn.textContent === "Error")
          saveBtn.textContent = "Save score";
      }, 1200);
    });
  });
  const fbRow = document.createElement("div");
  fbRow.className = "adjudicator-scorecard-feedback";
  const fbLab = document.createElement("span");
  fbLab.className = "adjudicator-scorecard-feedback-label";
  fbLab.textContent = "Adjudicator helpful?";
  const fbTh = document.createElement("div");
  fbTh.className = "adjudicator-scorecard-feedback-thumbs";
  const upF = document.createElement("button");
  upF.type = "button";
  upF.setAttribute("aria-label", "Adjudicator assessment was helpful");
  upF.appendChild(createThumbIcon("up"));
  const downF = document.createElement("button");
  downF.type = "button";
  downF.setAttribute("aria-label", "Adjudicator assessment was not helpful");
  downF.appendChild(createThumbIcon("down"));
  function postAdj(r) {
    fetch(API_BASE + "/chat/adjudication-feedback/" + encodeURIComponent(correlationId), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ rating: r, comment: null })
    }).then(() => {
      upF.disabled = true;
      downF.disabled = true;
      upF.classList.toggle("selected", r === "up");
      downF.classList.toggle("selected", r === "down");
    }).catch(() => {
    });
  }
  upF.addEventListener("click", () => postAdj("up"));
  downF.addEventListener("click", () => postAdj("down"));
  fbTh.appendChild(upF);
  fbTh.appendChild(downF);
  fbRow.appendChild(fbLab);
  fbRow.appendChild(fbTh);
  body.appendChild(fbRow);
  const adjFb = technicalFeedback?.adjudication;
  if (adjFb && (adjFb.rating === "up" || adjFb.rating === "down")) {
    upF.disabled = true;
    downF.disabled = true;
    upF.classList.toggle("selected", adjFb.rating === "up");
    downF.classList.toggle("selected", adjFb.rating === "down");
  }
  const adminNote = document.createElement("p");
  adminNote.className = "adjudicator-scorecard-admin-note";
  adminNote.textContent = "QA / adjudicator details visible to admins only.";
  body.appendChild(adminNote);
  const setExpanded = (exp) => {
    if (exp) {
      wrap.classList.remove("collapsed");
      wrap.classList.add("adjudicator-scorecard--expanded");
    } else {
      wrap.classList.add("collapsed");
      wrap.classList.remove("adjudicator-scorecard--expanded");
    }
    preview.setAttribute("aria-expanded", String(exp));
    chev.textContent = exp ? "\u25B2" : "\u25BC";
    oneline.style.display = exp ? "none" : "";
  };
  const toggle = () => setExpanded(wrap.classList.contains("collapsed"));
  preview.addEventListener("click", toggle);
  preview.addEventListener("keydown", (e) => {
    const ke = e;
    if (ke.key === "Enter" || ke.key === " ") {
      ke.preventDefault();
      toggle();
    }
  });
  wrap.appendChild(preview);
  wrap.appendChild(body);
  return wrap;
}
function syncAdjudicatorScorecardDom(wrap, qc, oneline, badgesWrap) {
  const vUi = adjudicationVerdictUi(qc);
  const effective = effectiveQcScore(qc);
  const effStr = effective !== null ? effective.toFixed(2) : "\u2014";
  const auto = parseScoreValue(qc.automated_score) ?? parseScoreValue(qc.score) ?? (qc.passed ? 1 : 0);
  oneline.textContent = `${vUi.shortLabel} \xB7 score ${effStr} \xB7 ${(qc.source || "\u2014").toString().slice(0, 24)}`;
  oneline.dataset.effective = effStr;
  const spans = badgesWrap.querySelectorAll(".adjudicator-scorecard-badge");
  if (spans[0]) {
    spans[0].className = `adjudicator-scorecard-badge adjudicator-scorecard-badge--${vUi.badgeVariant}`;
    spans[0].textContent = vUi.verdictBadgeText;
  }
  if (spans[1])
    spans[1].textContent = `Effective score: ${effStr}`;
  if (spans[2])
    spans[2].textContent = `Automated: ${auto.toFixed(2)}`;
  const userS = parseScoreValue(qc.user_score);
  let userBadge = badgesWrap.querySelector(".adjudicator-scorecard-badge--user");
  if (!userBadge) {
    userBadge = document.createElement("span");
    userBadge.className = "adjudicator-scorecard-badge adjudicator-scorecard-badge--user";
    badgesWrap.appendChild(userBadge);
  }
  userBadge.textContent = userS !== void 0 ? `User: ${userS.toFixed(2)}` : "User: \u2014";
  const detailOld = wrap.querySelector(".adjudicator-scorecard-detail-wrap");
  if (detailOld?.parentNode) {
    detailOld.replaceWith(buildAdjudicatorDetailWrap(qc));
  }
  const pre = wrap.querySelector(".adjudicator-scorecard-reason .adjudicator-scorecard-pre");
  if (pre)
    pre.textContent = (qc.reason || "\u2014").toString().slice(0, 4e3);
  const note = wrap.querySelector(".adjudicator-scorecard-note");
  if (note && qc.user_score_comment != null)
    note.value = String(qc.user_score_comment);
}
function renderLlmPerformance(rows, meta, opts) {
  const wrap = document.createElement("div");
  wrap.className = "llm-performance collapsed";
  const primary = (meta?.primary_model || "").trim() || [...rows].reverse().find((r) => r.stage === "integrator")?.model || rows[0]?.model || "\u2014";
  const totalMs = meta?.total_latency_ms ?? 0;
  const totalSec = totalMs > 0 ? (totalMs / 1e3).toFixed(1) : "0.0";
  const costNum = meta?.total_cost_usd != null && meta.total_cost_usd > 0 ? meta.total_cost_usd : opts.totalCostFallback ?? 0;
  const costStr = formatCostShort(Number(costNum) || 0);
  const qc = opts.qc;
  const eqScore = effectiveQcScore(qc ?? void 0);
  const qCollapsed = eqScore !== null ? eqScore.toFixed(2) : "\u2014";
  const legacy = meta?.pipeline === "legacy";
  const preview = document.createElement("div");
  preview.className = "llm-performance-preview";
  preview.setAttribute("role", "button");
  preview.setAttribute("tabindex", "0");
  preview.setAttribute("aria-expanded", "false");
  const titleEl = document.createElement("span");
  titleEl.className = "llm-performance-title";
  titleEl.textContent = "LLM performance";
  const oneline = document.createElement("span");
  oneline.className = "llm-performance-oneline";
  oneline.dataset.m = primary;
  oneline.dataset.s = totalSec;
  oneline.dataset.c = costStr;
  oneline.dataset.legacy = legacy ? "1" : "0";
  oneline.textContent = `${legacy ? "[LEGACY] " : ""}${primary} \xB7 ${totalSec}s \xB7 $${costStr} \xB7 quality ${qCollapsed}`;
  const chev = document.createElement("span");
  chev.className = "llm-performance-chevron";
  chev.setAttribute("aria-hidden", "true");
  chev.textContent = "\u25BC";
  preview.appendChild(titleEl);
  preview.appendChild(oneline);
  preview.appendChild(chev);
  const body = document.createElement("div");
  body.className = "llm-performance-body";
  const badges = document.createElement("div");
  badges.className = "llm-performance-badges";
  const confLabel = confidenceFromStrip(opts.sourceConfidenceStrip ?? null);
  const qBadge = eqScore !== null ? eqScore.toFixed(2) : "\u2014";
  const badgeSpecs = [
    { className: "llm-performance-badge llm-performance-badge--model", text: primary },
    { className: "llm-performance-badge llm-performance-badge--latency", text: `${totalSec}s total` },
    { className: "llm-performance-badge llm-performance-badge--cost", text: `$${costStr}` },
    {
      className: "llm-performance-badge llm-performance-badge--quality",
      text: `quality ${qBadge}`,
      isQuality: true
    }
  ];
  badgeSpecs.forEach((b) => {
    const el2 = document.createElement("span");
    el2.className = b.className;
    el2.textContent = b.text;
    if (b.isQuality)
      el2.setAttribute("data-llm-badge-quality", "1");
    badges.appendChild(el2);
  });
  const confEl = document.createElement("span");
  confEl.className = "llm-performance-badge llm-performance-badge--confidence";
  confEl.textContent = `confidence: ${confLabel}`;
  badges.appendChild(confEl);
  body.appendChild(badges);
  const stageLabel = document.createElement("div");
  stageLabel.className = "llm-performance-section-label";
  stageLabel.textContent = "STAGE BREAKDOWN";
  body.appendChild(stageLabel);
  const tableWrap = document.createElement("div");
  tableWrap.className = "llm-performance-table-wrap";
  const table = document.createElement("table");
  table.className = "llm-performance-table";
  const thead = document.createElement("thead");
  thead.innerHTML = '<tr><th>Stage</th><th>Model</th><th>Why this model</th><th>Latency</th><th>Cost</th><th title="PG batch composite at pick / per-call composite (hover for terms)">Composite<br><span class="llm-performance-th-sub">PG / call</span></th><th>QA</th><th>Status</th></tr>';
  table.appendChild(thead);
  const tb = document.createElement("tbody");
  fillLlmPerformanceTbody(tb, rows);
  table.appendChild(tb);
  tableWrap.appendChild(table);
  body.appendChild(tableWrap);
  const tin = opts.inputTokens ?? 0;
  const tout = opts.outputTokens ?? 0;
  if (tin > 0 || tout > 0) {
    const tokFoot = document.createElement("div");
    tokFoot.className = "llm-performance-tokens-foot";
    tokFoot.textContent = `Tokens in / out: ${tin.toLocaleString()} / ${tout.toLocaleString()}`;
    body.appendChild(tokFoot);
  }
  const routerBox = document.createElement("div");
  routerBox.className = "llm-performance-router";
  routerBox.textContent = formatRouterNote(meta, rows);
  body.appendChild(routerBox);
  const j = meta?.jurisdiction;
  const payerSlug = (j?.payer || "" || "").toLowerCase().replace(/\s+/g, "_");
  const jurisLine = j ? `Jurisdiction: payer=${payerSlug || "\u2014"} \xB7 state=${(j.state || "\u2014").toString()}` : meta?.jurisdiction_summary ? `Jurisdiction: ${meta.jurisdiction_summary}` : "Jurisdiction: \u2014";
  const cfgShort = (meta?.config_sha || "\u2014").toString().slice(0, 12);
  const top = meta?.top_source;
  const corpusBit = top?.document_name ? `Corpus: ${top.document_name}${top.page_number != null ? ` p.${top.page_number}` : ""}${top.match_score != null ? ` \xB7 match=${Number(top.match_score).toFixed(2)}` : ""}` : "Corpus: \u2014";
  const footer = document.createElement("div");
  footer.className = "llm-performance-footer";
  const metaCol = document.createElement("div");
  metaCol.className = "llm-performance-footer-meta";
  metaCol.innerHTML = `${escapeHtml2(jurisLine)}<br/>Config: ${escapeHtml2(cfgShort)} \xB7 ${escapeHtml2(corpusBit)}`;
  footer.appendChild(metaCol);
  const routeFb = document.createElement("div");
  routeFb.className = "llm-performance-routing-feedback";
  const rfLabel = document.createElement("span");
  rfLabel.className = "llm-performance-routing-label";
  rfLabel.textContent = "Routing correct?";
  const thumbs = document.createElement("div");
  thumbs.className = "llm-performance-routing-thumbs";
  const upB = document.createElement("button");
  upB.type = "button";
  upB.setAttribute("aria-label", "Routing was appropriate");
  upB.appendChild(createThumbIcon("up"));
  const downB = document.createElement("button");
  downB.type = "button";
  downB.setAttribute("aria-label", "Routing was not appropriate");
  downB.appendChild(createThumbIcon("down"));
  const cid = opts.correlationId;
  function postPerf(r) {
    if (!cid)
      return;
    fetch(API_BASE + "/chat/llm-performance-feedback/" + encodeURIComponent(cid), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ rating: r })
    }).then(() => {
      upB.disabled = true;
      downB.disabled = true;
      upB.classList.toggle("selected", r === "up");
      downB.classList.toggle("selected", r === "down");
    }).catch(() => {
    });
  }
  upB.addEventListener("click", () => postPerf("up"));
  downB.addEventListener("click", () => postPerf("down"));
  thumbs.appendChild(upB);
  thumbs.appendChild(downB);
  routeFb.appendChild(rfLabel);
  routeFb.appendChild(thumbs);
  footer.appendChild(routeFb);
  body.appendChild(footer);
  const adminNote = document.createElement("p");
  adminNote.className = "llm-performance-admin-note";
  adminNote.textContent = "LLM performance visible to admins only.";
  body.appendChild(adminNote);
  const rf = opts.routingFeedback;
  if (rf && (rf.rating === "up" || rf.rating === "down")) {
    upB.disabled = true;
    downB.disabled = true;
    upB.classList.toggle("selected", rf.rating === "up");
    downB.classList.toggle("selected", rf.rating === "down");
  }
  const setExpanded = (exp) => {
    if (exp) {
      wrap.classList.remove("collapsed");
      wrap.classList.add("llm-performance--expanded");
    } else {
      wrap.classList.add("collapsed");
      wrap.classList.remove("llm-performance--expanded");
    }
    preview.setAttribute("aria-expanded", String(exp));
    chev.textContent = exp ? "\u25B2" : "\u25BC";
    oneline.style.display = exp ? "none" : "";
  };
  const toggle = () => {
    setExpanded(wrap.classList.contains("collapsed"));
  };
  preview.addEventListener("click", toggle);
  preview.addEventListener("keydown", (e) => {
    const ke = e;
    if (ke.key === "Enter" || ke.key === " ") {
      ke.preventDefault();
      toggle();
    }
  });
  wrap.setAttribute("data-usage-rows", String(rows.length));
  wrap.setAttribute("data-usage-sig", llmUsageBreakdownPatchSig(rows));
  wrap.appendChild(preview);
  wrap.appendChild(body);
  return wrap;
}
function renderSourceCiter(sources, citedSourceIndices, correlationId) {
  const wrap = document.createElement("div");
  wrap.className = "source-citer collapsed";
  const preview = document.createElement("div");
  preview.className = "source-citer-preview";
  preview.setAttribute("role", "button");
  preview.setAttribute("tabindex", "0");
  preview.setAttribute("aria-expanded", "false");
  const word = document.createElement("span");
  word.className = "source-citer-word";
  word.textContent = sources.length === 1 ? "Sources (1)" : `Sources (${sources.length})`;
  const rule = document.createElement("span");
  rule.className = "source-citer-rule";
  preview.appendChild(word);
  preview.appendChild(rule);
  preview.addEventListener("click", () => {
    wrap.classList.toggle("collapsed");
    preview.setAttribute("aria-expanded", String(!wrap.classList.contains("collapsed")));
  });
  preview.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      wrap.classList.toggle("collapsed");
      preview.setAttribute("aria-expanded", String(!wrap.classList.contains("collapsed")));
    }
  });
  const body = document.createElement("div");
  body.className = "source-citer-body";
  const citedSet = new Set((citedSourceIndices ?? []).map((n) => Number(n)));
  sources.forEach((s) => {
    const item = document.createElement("div");
    const isCited = citedSet.size > 0 && citedSet.has(Number(s.index));
    item.className = "source-item" + (isCited ? " source-item--cited" : "");
    const doc = document.createElement("div");
    doc.className = "source-doc";
    doc.textContent = `[${s.index}] ${s.document_name}` + (s.page_number != null ? ` (page ${s.page_number})` : "");
    item.appendChild(doc);
    if (s.source_type != null || s.match_score != null || s.confidence != null) {
      const metaLine = document.createElement("div");
      metaLine.className = "source-meta";
      const parts = [];
      if (s.source_type != null && s.source_type !== "")
        parts.push(`Type: ${s.source_type}`);
      if (s.match_score != null)
        parts.push(`Match: ${Number(s.match_score).toFixed(2)}`);
      if (s.confidence != null)
        parts.push(`Confidence: ${Number(s.confidence).toFixed(2)}`);
      metaLine.textContent = parts.join(" \xB7 ");
      item.appendChild(metaLine);
    }
    if (s.snippet) {
      const meta = document.createElement("div");
      meta.className = "source-snippet";
      meta.textContent = s.snippet;
      item.appendChild(meta);
    }
    const ragUrl = resolveSourceOpenHref(s);
    const ragApiRaw = typeof window !== "undefined" ? window.RAG_API_BASE : void 0;
    const ragApi = typeof ragApiRaw === "string" ? ragApiRaw.trim() : "";
    const docId = s.document_id?.trim();
    if (ragUrl || ragApi && docId) {
      const actions = document.createElement("div");
      actions.className = "source-doc-actions";
      if (ragUrl) {
        const link = document.createElement("a");
        link.href = ragUrl;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        link.className = "source-open-doc-link";
        link.textContent = "Open full document";
        link.addEventListener("click", (e) => e.stopPropagation());
        actions.appendChild(link);
      }
      if (ragApi && docId) {
        const dl = document.createElement("a");
        dl.href = `${ragApi.replace(/\/$/, "")}/documents/${encodeURIComponent(docId)}/download/pdf`;
        dl.target = "_blank";
        dl.rel = "noopener noreferrer";
        dl.className = "source-open-doc-link source-download-link";
        dl.textContent = "Download PDF";
        dl.addEventListener("click", (e) => e.stopPropagation());
        actions.appendChild(dl);
      }
      item.appendChild(actions);
    }
    if (correlationId) {
      let postSourceFeedback2 = function(r) {
        const cid = correlationId ?? "";
        if (!cid)
          return;
        fetch(API_BASE + "/chat/source-feedback/" + encodeURIComponent(cid), {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ source_index: srcIdx, rating: r })
        }).then(() => {
          upBtn.disabled = true;
          downBtn.disabled = true;
          upBtn.classList.toggle("selected", r === "up");
          downBtn.classList.toggle("selected", r === "down");
        }).catch(() => {
        });
      };
      var postSourceFeedback = postSourceFeedback2;
      const feedbackRow = document.createElement("div");
      feedbackRow.className = "source-feedback-row";
      const question = document.createElement("span");
      question.className = "source-feedback-question";
      question.textContent = "Helpful?";
      const thumbs = document.createElement("div");
      thumbs.className = "source-feedback-thumbs";
      const upBtn = document.createElement("button");
      upBtn.type = "button";
      upBtn.setAttribute("aria-label", "Helpful");
      upBtn.appendChild(createThumbIcon("up"));
      const downBtn = document.createElement("button");
      downBtn.type = "button";
      downBtn.setAttribute("aria-label", "Not helpful");
      downBtn.appendChild(createThumbIcon("down"));
      const srcIdx = s.index != null && s.index >= 1 ? s.index : sources.indexOf(s) + 1;
      upBtn.addEventListener("click", () => postSourceFeedback2("up"));
      downBtn.addEventListener("click", () => postSourceFeedback2("down"));
      thumbs.appendChild(upBtn);
      thumbs.appendChild(downBtn);
      feedbackRow.appendChild(question);
      feedbackRow.appendChild(thumbs);
      item.appendChild(feedbackRow);
    }
    body.appendChild(item);
  });
  wrap.appendChild(preview);
  wrap.appendChild(body);
  return wrap;
}
function renderAssistantFromEnvelope(envelope, opts) {
  const outer = document.createElement("div");
  outer.className = "assistant-envelope";
  const bubble = document.createElement("div");
  bubble.className = "message-bubble answer-card-bubble";
  let confidenceInjectedAfterDirectAnswer = false;
  for (const block of envelope.blocks || []) {
    if (!block || typeof block !== "object")
      continue;
    const t = block.type;
    if (t === "tool_attribution") {
      const b = block;
      const chip = document.createElement("div");
      chip.className = "envelope-tool-chip";
      chip.setAttribute("data-icon", b.icon || "search");
      chip.textContent = b.label || "Research";
      bubble.appendChild(chip);
    } else if (t === "direct_answer") {
      const b = block;
      const chrome2 = document.createElement("div");
      chrome2.className = "envelope-answer-chrome";
      const el2 = document.createElement("div");
      el2.className = "envelope-direct-answer";
      el2.textContent = sanitizeDisplayMessage(b.markdown || "");
      chrome2.appendChild(el2);
      bubble.appendChild(chrome2);
      if (opts.showConfidenceBadge !== false && !opts.suppressConfidenceForAdminQcFail) {
        chrome2.appendChild(
          renderConfidenceBadge((opts.sourceConfidenceStrip ?? "").trim() || "informational_only")
        );
        confidenceInjectedAfterDirectAnswer = true;
      }
    } else if (t === "detail") {
      const b = block;
      const details = document.createElement("details");
      details.className = "envelope-detail";
      details.open = b.collapsed_default === false;
      const sum = document.createElement("summary");
      sum.textContent = "Details";
      details.appendChild(sum);
      const body = document.createElement("div");
      body.className = "envelope-detail-body";
      body.innerHTML = simpleMarkdownToHtml(b.markdown || "");
      details.appendChild(body);
      bubble.appendChild(details);
    } else if (t === "chart") {
      const b = block;
      const wrap = document.createElement("div");
      wrap.className = "envelope-chart";
      if (b.title) {
        const h = document.createElement("div");
        h.className = "envelope-chart-title";
        h.textContent = b.title;
        wrap.appendChild(h);
      }
      const raw = (b.image_base64 || "").trim();
      const src = raw.startsWith("data:") ? raw : "data:image/png;base64," + raw;
      const img = document.createElement("img");
      img.className = "envelope-chart-img report-chart";
      img.src = src;
      img.alt = b.title || "Chart";
      img.loading = "lazy";
      wrap.appendChild(img);
      if (b.caption) {
        const cap = document.createElement("div");
        cap.className = "envelope-chart-caption";
        cap.textContent = b.caption;
        wrap.appendChild(cap);
      }
      bubble.appendChild(wrap);
    } else if (t === "table") {
      const b = block;
      const table = document.createElement("table");
      table.className = "envelope-table";
      if (b.headers?.length) {
        const thead = document.createElement("thead");
        const tr = document.createElement("tr");
        for (const h of b.headers) {
          const th = document.createElement("th");
          th.textContent = h;
          tr.appendChild(th);
        }
        thead.appendChild(tr);
        table.appendChild(thead);
      }
      const tbody = document.createElement("tbody");
      for (const row of b.rows || []) {
        const tr = document.createElement("tr");
        for (const c of row) {
          const td = document.createElement("td");
          td.textContent = c;
          tr.appendChild(td);
        }
        tbody.appendChild(tr);
      }
      table.appendChild(tbody);
      bubble.appendChild(table);
    } else if (t === "callout") {
      const b = block;
      const c = document.createElement("div");
      c.className = "envelope-callout envelope-callout--" + (b.variant || "info");
      c.textContent = b.body || "";
      bubble.appendChild(c);
    } else if (t === "sources") {
      const b = block;
      const parsed = (b.refs || []).map((r) => ({
        index: r.index,
        document_name: r.title || "Source",
        document_id: r.document_id ?? null,
        page_number: r.page ?? null,
        snippet: r.snippet ?? "",
        open_href: r.open?.href ?? null
      }));
      if (parsed.length > 0) {
        bubble.appendChild(renderSourceCiter(parsed, void 0, opts.correlationId ?? null));
      }
    } else if (t === "next_steps") {
      const b = block;
      const items = (b.items || []).filter((x) => typeof x === "string" && x.trim());
      if (items.length && opts.onFollowupClick) {
        const expanded = b.collapsed_default === false;
        const disclosure = document.createElement("details");
        disclosure.className = "envelope-followups-disclosure";
        disclosure.open = expanded;
        const sum = document.createElement("summary");
        sum.className = "envelope-followups-summary envelope-followups-summary--next-steps";
        sum.textContent = expanded ? "Next steps" : "Next steps (tap to expand)";
        disclosure.appendChild(sum);
        const w = document.createElement("div");
        w.className = "envelope-next-steps";
        const hint = document.createElement("div");
        hint.className = "envelope-next-steps-hint";
        hint.textContent = "Things to try outside this chat. Tap a line to paste into your message.";
        w.appendChild(hint);
        for (const q of items) {
          const btn = document.createElement("button");
          btn.type = "button";
          btn.className = "envelope-step-chip";
          btn.textContent = q.trim();
          btn.addEventListener("click", () => opts.onFollowupClick(q.trim()));
          w.appendChild(btn);
        }
        disclosure.appendChild(w);
        bubble.appendChild(disclosure);
      }
    } else if (t === "suggested_questions") {
      const b = block;
      const items = (b.items || []).filter((x) => typeof x === "string" && x.trim());
      if (items.length && opts.onFollowupClick) {
        const expanded = b.collapsed_default === false;
        const disclosure = document.createElement("details");
        disclosure.className = "envelope-followups-disclosure";
        disclosure.open = expanded;
        const sum = document.createElement("summary");
        sum.className = "envelope-followups-summary envelope-followups-summary--suggested";
        sum.textContent = expanded ? "Follow-up questions" : "Follow-up questions (tap to expand)";
        disclosure.appendChild(sum);
        const w = document.createElement("div");
        w.className = "envelope-suggested";
        const hint = document.createElement("div");
        hint.className = "envelope-suggested-hint";
        hint.textContent = "Tap a line to send it as your next message.";
        w.appendChild(hint);
        const chips = document.createElement("div");
        chips.className = "envelope-suggested-chips";
        for (const q of items.slice(0, 6)) {
          const btn = document.createElement("button");
          btn.type = "button";
          btn.className = "envelope-suggested-chip";
          const text = q.trim();
          btn.textContent = text;
          btn.setAttribute("aria-label", "Send: " + text);
          btn.addEventListener("click", () => opts.onFollowupClick(text));
          chips.appendChild(btn);
        }
        w.appendChild(chips);
        disclosure.appendChild(w);
        bubble.appendChild(disclosure);
      }
    } else if (t === "markdown_report") {
      const b = block;
      const div = document.createElement("div");
      div.className = "envelope-markdown-report";
      div.innerHTML = rosterStepMarkdownToHtml(b.markdown || "");
      bubble.appendChild(div);
    } else if (t === "attachments") {
      const b = block;
      if (b.has_pdf) {
        const note = document.createElement("div");
        note.className = "envelope-attachments-note";
        note.textContent = "Report attachments available below.";
        bubble.appendChild(note);
      }
    }
  }
  if (!confidenceInjectedAfterDirectAnswer && opts.showConfidenceBadge !== false && !opts.suppressConfidenceForAdminQcFail) {
    bubble.appendChild(
      renderConfidenceBadge((opts.sourceConfidenceStrip ?? "").trim() || "informational_only")
    );
  }
  if (opts.qcAudit)
    bubble.appendChild(renderQcAuditBadge(opts.qcAudit));
  const msg = document.createElement("div");
  msg.className = "message message--assistant answer-card";
  msg.appendChild(bubble);
  outer.appendChild(msg);
  return outer;
}
function scrollToBottom(container) {
  container.scrollTop = container.scrollHeight;
}
function run() {
  const messagesEl = el("messages");
  const inputEl = el("input");
  const sendBtn = el("send");
  let currentThreadId = null;
  const chatStatusBanner = document.getElementById("chatStatusBanner");
  const chatStatusBannerText = document.getElementById("chatStatusBannerText");
  let chatStatusBannerTimer = null;
  function hideChatStatusBanner() {
    if (chatStatusBannerTimer) {
      clearTimeout(chatStatusBannerTimer);
      chatStatusBannerTimer = null;
    }
    chatStatusBanner?.setAttribute("hidden", "");
  }
  function showChatStatusBanner(message, autoHideMs = 2e4) {
    if (!chatStatusBanner || !chatStatusBannerText)
      return;
    if (chatStatusBannerTimer)
      clearTimeout(chatStatusBannerTimer);
    chatStatusBannerText.textContent = message;
    chatStatusBanner.removeAttribute("hidden");
    if (autoHideMs > 0) {
      chatStatusBannerTimer = setTimeout(() => hideChatStatusBanner(), autoHideMs);
    }
  }
  document.getElementById("chatStatusBannerDismiss")?.addEventListener("click", hideChatStatusBanner);
  function hideRosterUploadReceipt() {
    document.getElementById("rosterReceipt")?.setAttribute("hidden", "");
  }
  function showRosterUploadReceipt(data) {
    hideChatStatusBanner();
    const root = document.getElementById("rosterReceipt");
    const headline = document.getElementById("rosterReceiptHeadline");
    const sub = document.getElementById("rosterReceiptSub");
    const checksEl = document.getElementById("rosterReceiptChecks");
    const alertsEl = document.getElementById("rosterReceiptAlerts");
    const nextEl = document.getElementById("rosterReceiptNext");
    const metaEl = document.getElementById("rosterReceiptMeta");
    if (!root || !headline || !sub || !checksEl || !alertsEl || !nextEl || !metaEl)
      return;
    const ack = data.acknowledgment;
    if (ack && Array.isArray(ack.checks) && ack.checks.length > 0) {
      headline.textContent = ack.headline || "Your roster is linked";
      sub.textContent = ack.subhead || "";
      checksEl.replaceChildren();
      for (const c of ack.checks) {
        const li = document.createElement("li");
        const t = document.createElement("span");
        t.className = "roster-receipt__check-title";
        t.textContent = c.title;
        const d = document.createElement("span");
        d.className = "roster-receipt__check-detail";
        d.textContent = c.detail;
        li.appendChild(t);
        li.appendChild(d);
        checksEl.appendChild(li);
      }
      alertsEl.replaceChildren();
      if (ack.alerts && ack.alerts.length > 0) {
        alertsEl.removeAttribute("hidden");
        for (const a of ack.alerts) {
          const div = document.createElement("div");
          div.className = a.tone === "warning" ? "roster-receipt__alert roster-receipt__alert--warning" : "roster-receipt__alert roster-receipt__alert--notice";
          div.textContent = a.message;
          alertsEl.appendChild(div);
        }
      } else {
        alertsEl.setAttribute("hidden", "");
      }
      nextEl.textContent = ack.next_step || "";
    } else {
      headline.textContent = "Upload complete";
      sub.textContent = "Your file was saved to this chat.";
      checksEl.replaceChildren();
      const li = document.createElement("li");
      const t = document.createElement("span");
      t.className = "roster-receipt__check-title";
      t.textContent = "Summary";
      const d = document.createElement("span");
      d.className = "roster-receipt__check-detail";
      d.textContent = `${data.filename ?? "File"} \u2014 ${data.row_count ?? 0} row(s) for ${data.org_name ?? ""}. Billing NPI ${data.default_billing_npi || data.org_id || "\u2014"}.`;
      li.appendChild(t);
      li.appendChild(d);
      checksEl.appendChild(li);
      alertsEl.replaceChildren();
      alertsEl.setAttribute("hidden", "");
      nextEl.textContent = "Press Send to run reconciliation, or wait if you turned on automatic send after upload.";
    }
    function addMeta(label, value) {
      if (!value)
        return;
      const dt = document.createElement("dt");
      dt.textContent = label;
      const dd = document.createElement("dd");
      dd.textContent = value;
      metaEl.appendChild(dt);
      metaEl.appendChild(dd);
    }
    metaEl.replaceChildren();
    addMeta("File", (data.filename ?? "").trim());
    if (data.row_count_cleansed != null)
      addMeta("Rows after cleanup", String(data.row_count_cleansed));
    if (data.row_count_resolved != null)
      addMeta("Rows checked in NPI registry", String(data.row_count_resolved));
    addMeta("Billing NPI", (data.default_billing_npi || data.org_id || "").trim());
    addMeta("Matched organization (registry)", (data.matched_organization_name ?? "").trim());
    if ((data.matched_practice_address ?? "").trim())
      addMeta("Practice address on file", (data.matched_practice_address ?? "").trim());
    addMeta("Process status", (data.process_status ?? "").trim());
    addMeta("Upload ID", (data.upload_id ?? "").trim());
    addMeta("Chat thread ID", (data.thread_id ?? "").trim());
    const rs = data.resolution_summary;
    if (rs && typeof rs === "object") {
      const parts = Object.entries(rs).filter(([, v]) => typeof v === "number" && v > 0).map(([k, v]) => `${k}: ${v}`);
      if (parts.length)
        addMeta("NPI match breakdown", parts.join(", "));
    }
    const details = root.querySelector("details");
    if (details)
      details.open = false;
    root.removeAttribute("hidden");
    document.getElementById("chatEmpty")?.classList.add("hidden");
    window.setTimeout(() => root.scrollIntoView({ block: "nearest", behavior: "smooth" }), 80);
  }
  document.getElementById("rosterReceiptDismiss")?.addEventListener("click", hideRosterUploadReceipt);
  const drawer = el("drawer");
  const drawerOverlay = el("drawerOverlay");
  const hamburger = el("hamburger");
  const drawerClose = el("drawerClose");
  const btnConfig = document.getElementById("btnConfig");
  const sidebarUser = document.getElementById("sidebarUser");
  const sidebarUserName = document.getElementById("sidebarUserName");
  const authApiBase = `${API_BASE.replace(/\/$/, "")}/api/v1`;
  const auth = createAuthService({ apiBase: authApiBase, storage: localStorageAdapter });
  const modal = createAuthModal({ auth, showOAuth: true });
  document.body.appendChild(modal.el);
  const styleEl = document.createElement("style");
  styleEl.textContent = AUTH_STYLES;
  document.head.appendChild(styleEl);
  function updateSidebarUser(user) {
    if (sidebarUserName)
      sidebarUserName.textContent = user?.greeting_name ?? "Guest";
  }
  let cachedProfile = null;
  function syncAnswerInsightsCheckbox() {
    const cb = document.getElementById("prefShowAnswerInsights");
    if (!cb)
      return;
    cb.checked = getShowLlmPerformance(cachedProfile);
  }
  function mergeLlmPerformanceUsageFromPoll(turnWrap, d) {
    const rows = d.usage_breakdown;
    if (!Array.isArray(rows) || rows.length === 0)
      return;
    if (!getShowLlmPerformance(cachedProfile))
      return;
    const panel = turnWrap.querySelector(".llm-performance");
    if (!panel)
      return;
    const sig = llmUsageBreakdownPatchSig(rows);
    const prevSig = panel.getAttribute("data-usage-sig") || "";
    if (sig === prevSig)
      return;
    const tbody = panel.querySelector(".llm-performance-table tbody");
    if (tbody)
      fillLlmPerformanceTbody(tbody, rows);
    panel.setAttribute("data-usage-sig", sig);
    panel.setAttribute("data-usage-rows", String(rows.length));
  }
  function ensureAdjudicatorScorecard(turnWrap, qc, correlationId, technicalFeedback) {
    if (!getShowLlmPerformance(cachedProfile))
      return;
    const existing = turnWrap.querySelector(".adjudicator-scorecard");
    if (!existing) {
      const el2 = renderAdjudicatorScorecard(qc, correlationId, technicalFeedback ?? null);
      const perf = turnWrap.querySelector(".llm-performance");
      const fb = turnWrap.querySelector(".feedback");
      if (perf)
        perf.insertAdjacentElement("afterend", el2);
      else if (fb)
        fb.insertAdjacentElement("beforebegin", el2);
      else
        turnWrap.appendChild(el2);
      return;
    }
    const oneline = existing.querySelector(".adjudicator-scorecard-oneline");
    const badges = existing.querySelector(".adjudicator-scorecard-badges");
    if (oneline && badges)
      syncAdjudicatorScorecardDom(existing, qc, oneline, badges);
  }
  function mergeTechnicalPanels(turnWrap, d) {
    const qc = d.qc_audit;
    if (!qc || typeof qc.passed !== "boolean")
      return;
    const cid = (d.correlation_id || turnWrap.getAttribute("data-correlation-id") || "").trim();
    if (!cid)
      return;
    ensureAdjudicatorScorecard(turnWrap, qc, cid, d.technical_feedback);
  }
  function mergeLlmPerformanceRoutingHydrate(turnWrap, d) {
    const lp = d.technical_feedback?.llm_performance;
    if (!lp || lp.rating !== "up" && lp.rating !== "down")
      return;
    const panel = turnWrap.querySelector(".llm-performance");
    if (!panel)
      return;
    const buttons = panel.querySelectorAll(".llm-performance-routing-thumbs button");
    const upB = buttons[0];
    const downB = buttons[1];
    if (!upB || !downB)
      return;
    upB.disabled = true;
    downB.disabled = true;
    upB.classList.toggle("selected", lp.rating === "up");
    downB.classList.toggle("selected", lp.rating === "down");
  }
  auth.on(() => {
    void auth.getUserProfile().then((p) => {
      cachedProfile = p;
      updateSidebarUser(p);
      syncAnswerInsightsCheckbox();
    });
  });
  void auth.getUserProfile().then((p) => {
    cachedProfile = p;
    updateSidebarUser(p);
    syncAnswerInsightsCheckbox();
  });
  const prefShowAnswerInsights = document.getElementById(
    "prefShowAnswerInsights"
  );
  prefShowAnswerInsights?.addEventListener("change", () => {
    try {
      localStorage.setItem(LLM_PERF_LS, prefShowAnswerInsights.checked ? "1" : "0");
    } catch {
    }
  });
  if (sidebarUser) {
    sidebarUser.addEventListener("click", () => {
      void auth.getUserProfile().then((user) => {
        modal.open(user ? "account" : "login");
      });
    });
  }
  function openDrawer() {
    drawer.classList.add("open");
    drawerOverlay.classList.add("open");
    loadChatConfig();
  }
  function closeDrawer() {
    drawer.classList.remove("open");
    drawerOverlay.classList.remove("open");
  }
  const sidebar = document.getElementById("sidebar");
  const mainEl = document.querySelector(".main");
  const sidebarChevron = document.getElementById("sidebarChevron");
  function toggleSidebar() {
    if (!sidebar || !mainEl)
      return;
    const collapsed = sidebar.classList.toggle("sidebar--collapsed");
    mainEl.classList.toggle("sidebar-collapsed", collapsed);
    if (sidebarChevron) {
      sidebarChevron.setAttribute("aria-label", collapsed ? "Expand sidebar" : "Collapse sidebar");
      sidebarChevron.setAttribute("title", collapsed ? "Expand sidebar" : "Collapse sidebar");
    }
  }
  sidebarChevron?.addEventListener("click", toggleSidebar);
  function initSidebarCollapsibles() {
    document.querySelectorAll(".sidebar-section-title.sidebar-section-toggle").forEach((titleEl) => {
      const toggle = () => {
        const controls = titleEl.getAttribute("aria-controls") || "";
        const body = controls ? document.getElementById(controls) : null;
        if (!body)
          return;
        const expanded = titleEl.getAttribute("aria-expanded") !== "false";
        const next = !expanded;
        titleEl.setAttribute("aria-expanded", String(next));
        body.classList.toggle("collapsed", !next);
      };
      titleEl.addEventListener("click", (e) => {
        e.preventDefault();
        toggle();
      });
      titleEl.addEventListener("keydown", (e) => {
        const ke = e;
        if (ke.key === "Enter" || ke.key === " ") {
          ke.preventDefault();
          toggle();
        }
      });
    });
  }
  initSidebarCollapsibles();
  hamburger.addEventListener("click", openDrawer);
  drawerClose.addEventListener("click", closeDrawer);
  drawerOverlay.addEventListener("click", closeDrawer);
  const configHistoryViewClose = document.getElementById("configHistoryViewClose");
  if (configHistoryViewClose) {
    configHistoryViewClose.addEventListener("click", () => {
      const viewEl = document.getElementById("configHistoryView");
      if (viewEl)
        viewEl.style.display = "none";
    });
  }
  if (btnConfig)
    btnConfig.addEventListener("click", openDrawer);
  setupLlmRouterReportUI();
  function loadConfigHistory() {
    const section = document.getElementById("configHistorySection");
    const listEl = document.getElementById("configHistoryList");
    if (!section || !listEl)
      return;
    fetch(API_BASE + "/chat/config/history?limit=20").then((r) => r.json()).then((entries) => {
      section.style.display = "";
      listEl.innerHTML = "";
      if (!Array.isArray(entries) || entries.length === 0) {
        listEl.innerHTML = '<p class="config-history-empty">No config history yet. Save config or restart the server to record a version.</p>';
        return;
      }
      entries.forEach((entry) => {
        const row = document.createElement("div");
        row.className = "config-history-row";
        const sha = (entry.config_sha ?? "").slice(0, 12);
        const date = entry.created_at ? new Date(entry.created_at).toLocaleString() : "\u2014";
        const meta = [entry.model ?? "", entry.provider ?? ""].filter(Boolean).join(" \xB7 ") || "\u2014";
        row.innerHTML = '<span class="config-history-sha">' + sha + '</span><span class="config-history-date">' + date + '</span><span class="config-history-meta">' + meta + '</span><button type="button" class="config-history-btn" data-sha="' + (entry.config_sha ?? "") + '" aria-label="View">View</button>';
        const btn = row.querySelector(".config-history-btn");
        if (btn && entry.config_sha) {
          btn.addEventListener("click", () => {
            fetch(API_BASE + "/chat/config/history/" + encodeURIComponent(entry.config_sha)).then((r) => r.json()).then((config) => {
              const viewEl = document.getElementById("configHistoryView");
              const bodyEl = document.getElementById("configHistoryViewBody");
              if (viewEl && bodyEl) {
                bodyEl.textContent = JSON.stringify(config, null, 2);
                viewEl.style.display = "";
              }
            }).catch(() => {
            });
          });
        }
        listEl.appendChild(row);
      });
    }).catch(() => {
      if (section)
        section.style.display = "";
      if (listEl)
        listEl.innerHTML = '<p class="config-history-empty">Config history unavailable (e.g. database not connected).</p>';
    });
  }
  function loadChatConfig() {
    fetch(API_BASE + "/chat/config").then((r) => r.json()).then((data) => {
      const p = data.prompts ?? {};
      const sysEl = document.getElementById("promptFirstGenSystem");
      const userEl = document.getElementById("promptFirstGenUser");
      if (sysEl)
        sysEl.textContent = p.first_gen_system ?? "\u2014";
      if (userEl)
        userEl.textContent = p.first_gen_user_template ?? "\u2014";
      const llm = data.llm ?? {};
      const llmSummary = "Provider: " + (llm.provider ?? "\u2014") + ", Model: " + (llm.model ?? "\u2014") + (llm.temperature != null ? ", Temp: " + llm.temperature : "");
      const llmEl = document.getElementById("configLlm");
      if (llmEl)
        llmEl.textContent = llmSummary;
      const drawerSummaryLlm = document.getElementById("drawerSummaryLlm");
      if (drawerSummaryLlm)
        drawerSummaryLlm.textContent = (llm.provider ?? "") + " / " + (llm.model ?? "\u2014");
      const configShaValue = document.getElementById("configShaValue");
      if (configShaValue)
        configShaValue.textContent = data.config_sha ?? "\u2014";
      const parser = data.parser ?? {};
      const parserEl = document.getElementById("configParser");
      if (parserEl)
        parserEl.textContent = "Patient keywords: " + (parser.patient_keywords?.length ? parser.patient_keywords.join(", ") : "\u2014");
      const drawerSummaryParser = document.getElementById("drawerSummaryParser");
      if (drawerSummaryParser)
        drawerSummaryParser.textContent = parser.patient_keywords?.length ? parser.patient_keywords.slice(0, 3).join(", ") + (parser.patient_keywords.length > 3 ? "\u2026" : "") : "\u2014";
      loadConfigHistory();
    }).catch(() => {
      const sysEl = document.getElementById("promptFirstGenSystem");
      const llmEl = document.getElementById("configLlm");
      const drawerSummaryLlm = document.getElementById("drawerSummaryLlm");
      if (sysEl)
        sysEl.textContent = "Failed to load config.";
      if (llmEl)
        llmEl.textContent = "Failed to load config.";
      if (drawerSummaryLlm)
        drawerSummaryLlm.textContent = "Failed to load config.";
    });
  }
  function pollResponse(correlationId, onThinking, onStreamingMessage) {
    return new Promise((resolve, reject) => {
      const maxAttempts = 4500;
      let attempts = 0;
      const seenLines = /* @__PURE__ */ new Set();
      function poll() {
        fetch(API_BASE + "/chat/response/" + correlationId).then((r) => r.json()).then((data) => {
          if (data.thinking_log?.length && onThinking) {
            data.thinking_log.forEach((line) => {
              if (!seenLines.has(line)) {
                seenLines.add(line);
                onThinking(line);
              }
            });
          }
          if (data.message != null && data.message !== "" && onStreamingMessage) {
            onStreamingMessage(data.message);
          }
          if (data.status === "completed" || data.status === "clarification" || data.status === "refinement_ask" || data.status === "failed") {
            resolve(data);
            return;
          }
          attempts++;
          if (attempts >= maxAttempts) {
            reject(new Error("Timeout waiting for response"));
            return;
          }
          setTimeout(poll, 400);
        }).catch(reject);
      }
      poll();
    });
  }
  function streamResponse(correlationId, onThinking, onStreamingMessage) {
    if (typeof EventSource === "undefined") {
      return pollResponse(correlationId, onThinking, onStreamingMessage);
    }
    const streamUrl = API_BASE + "/chat/stream/" + encodeURIComponent(correlationId);
    return new Promise((resolve, reject) => {
      let messageSoFar = "";
      let resolved = false;
      const es = new EventSource(streamUrl);
      es.onmessage = (e) => {
        try {
          const parsed = JSON.parse(e.data);
          const ev = parsed.event;
          const data = parsed.data ?? {};
          if (ev === "thinking" && data.line != null && onThinking) {
            onThinking(String(data.line));
          } else if (ev === "quality_audit" && data.line != null && onThinking) {
            onThinking(String(data.line));
          } else if (ev === "message" && data.chunk != null && onStreamingMessage) {
            messageSoFar += String(data.chunk);
            onStreamingMessage(messageSoFar);
          } else if (ev === "completed" && data) {
            resolved = true;
            es.close();
            resolve(data);
          } else if (ev === "error" && data.message != null) {
            resolved = true;
            es.close();
            reject(new Error(String(data.message)));
          }
        } catch (err) {
          resolved = true;
          es.close();
          reject(err instanceof Error ? err : new Error(String(err)));
        }
      };
      es.onerror = () => {
        es.close();
        if (resolved)
          return;
        pollResponse(correlationId, onThinking, onStreamingMessage).then(resolve).catch(reject);
      };
    });
  }
  const chatEmpty = document.getElementById("chatEmpty");
  function sendMessage(overrideMessage) {
    const message = (overrideMessage ?? (inputEl.value ?? "").trim()).trim();
    if (!message)
      return;
    if (sendBtn.disabled)
      return;
    if (chatEmpty)
      chatEmpty.classList.add("hidden");
    messagesEl.querySelectorAll(".thinking-block").forEach((block) => {
      block.classList.add("collapsed");
      const p = block.querySelector(".thinking-preview");
      if (p)
        p.setAttribute("aria-expanded", "false");
    });
    const turnWrap = document.createElement("div");
    turnWrap.className = "chat-turn";
    turnWrap.appendChild(renderUserMessage(message));
    messagesEl.appendChild(turnWrap);
    scrollToBottom(messagesEl);
    if (!overrideMessage)
      inputEl.value = "";
    updateSendState();
    sendBtn.disabled = true;
    inputEl.disabled = true;
    const thinkingLines = [];
    const { el: thinkingBlockEl, addLine: addThinkingLine, done: thinkingDone } = renderThinkingBlock(["Sending request\u2026"]);
    turnWrap.appendChild(thinkingBlockEl);
    scrollToBottom(messagesEl);
    function addThinkingLineAndScroll(line) {
      thinkingLines.push(line);
      addThinkingLine(line);
      scrollToBottom(messagesEl);
    }
    let messageWrapEl = null;
    function streamingDisplayText(text) {
      const t = (text ?? "").trim();
      if (t.startsWith("{"))
        return "Formatting answer\u2026";
      return normalizeMessageText(text);
    }
    function onStreamingMessage(text) {
      const display = streamingDisplayText(sanitizeDisplayMessage(text));
      if (!messageWrapEl) {
        messageWrapEl = renderAssistantMessage(display);
        turnWrap.appendChild(messageWrapEl);
      } else {
        const textEl = messageWrapEl.querySelector(".message-bubble-text");
        if (textEl)
          textEl.textContent = display;
      }
      scrollToBottom(messagesEl);
    }
    const payload = { message };
    if (currentThreadId)
      payload.thread_id = currentThreadId;
    let activeCorrelationId = "";
    fetch(API_BASE + "/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    }).then((r) => r.json()).then((data) => {
      if (data.thread_id)
        currentThreadId = data.thread_id;
      activeCorrelationId = data.correlation_id ?? "";
      addThinkingLineAndScroll("Request sent. Waiting for worker\u2026");
      return streamResponse(data.correlation_id, addThinkingLineAndScroll, onStreamingMessage);
    }).then(
      (data) => (
        // Refresh profile before admin-gated UI. Otherwise the first reply can render while
        // cachedProfile is still null (getUserProfile not resolved), hiding LLM performance.
        auth.getUserProfile().then((p) => {
          cachedProfile = p;
          syncAnswerInsightsCheckbox();
          return data;
        }).catch(() => data)
      )
    ).then((data) => {
      (data.thinking_log ?? []).forEach((line) => {
        if (!thinkingLines.includes(line))
          addThinkingLineAndScroll(line);
      });
      const fullMessage = data.message ?? "(No message)";
      const { body, sources } = parseMessageAndSources(fullMessage);
      if (data.response_source === "llm" && data.model_used) {
        addThinkingLineAndScroll("Model: " + data.model_used);
      }
      if (data.response_source === "stub" && data.llm_error) {
        addThinkingLineAndScroll("LLM failed (stub used): " + data.llm_error);
      }
      thinkingDone(thinkingLines.length);
      if (data.thread_id)
        currentThreadId = data.thread_id;
      const cidForTurn = (data.correlation_id || activeCorrelationId || "").trim();
      if (cidForTurn)
        turnWrap.setAttribute("data-correlation-id", cidForTurn);
      let nextQuestions = Array.isArray(data.next_questions_for_user) ? data.next_questions_for_user.filter((x) => typeof x === "string" && x.trim().length > 0) : data.user_ask && String(data.user_ask).trim() ? [String(data.user_ask).trim()] : [];
      if (nextQuestions.length === 0) {
        const card = tryParseAnswerCard(body || "");
        if (card?.followups?.length) {
          nextQuestions = card.followups.map((f) => (f.question || f.reason || f.field || "").trim()).filter(Boolean);
        }
      }
      if (messageWrapEl) {
        messageWrapEl.remove();
      }
      const reportMd = data.roster_report_final_md && typeof data.roster_report_final_md === "string" ? data.roster_report_final_md.trim() : "";
      const contentToShow = reportMd.length > 0 ? reportMd : body || "(No response)";
      const qcFromPayload = data.qc_audit && typeof data.qc_audit === "object" && typeof data.qc_audit.passed === "boolean" ? data.qc_audit : void 0;
      const suppressConf = adminShouldSuppressConfidenceForQc(cachedProfile, qcFromPayload);
      const envCandidate = data.assistant_envelope;
      const useEnvelope = envCandidate && typeof envCandidate === "object" && envCandidate.version === 1 && Array.isArray(envCandidate.blocks) && envCandidate.blocks.length > 0;
      const envBlocks = useEnvelope ? envCandidate.blocks : [];
      const envSourcesBlock = envBlocks.find((b) => b.type === "sources");
      const envelopeHasSources = useEnvelope && Array.isArray(envSourcesBlock?.refs) && envSourcesBlock.refs.length > 0;
      if (useEnvelope) {
        turnWrap.appendChild(
          renderAssistantFromEnvelope(envCandidate, {
            onFollowupClick: (q) => sendMessage(q),
            sourceConfidenceStrip: (data.source_confidence_strip ?? "").trim() || void 0,
            showConfidenceBadge: data.status !== "clarification" && data.status !== "refinement_ask",
            qcAudit: qcFromPayload,
            correlationId: cidForTurn || null,
            suppressConfidenceForAdminQcFail: suppressConf
          })
        );
      } else {
        turnWrap.appendChild(
          renderAssistantContent(contentToShow, !!data.llm_error, {
            onFollowupClick: (q) => sendMessage(q),
            sourceConfidenceStrip: (data.source_confidence_strip ?? "").trim() || void 0,
            showConfidenceBadge: data.status !== "clarification" && data.status !== "refinement_ask",
            suppressFollowups: nextQuestions.length > 0,
            nextQuestions,
            renderAsMarkdown: reportMd.length > 0 || !!(data.roster_report_final_md && (body || "").trim().length > 50),
            qcAudit: qcFromPayload,
            suppressConfidenceForAdminQcFail: suppressConf
          })
        );
      }
      const mergeQc = (d) => {
        const q = d.qc_audit && typeof d.qc_audit === "object" && typeof d.qc_audit.passed === "boolean" ? d.qc_audit : void 0;
        if (q) {
          applyQcAuditToTurn(turnWrap, q);
          if (adminShouldSuppressConfidenceForQc(cachedProfile, q))
            removeConfidenceBadgesInTurn(turnWrap);
        }
      };
      mergeQc(data);
      if (activeCorrelationId) {
        const refetchMerged = () => {
          if (!document.body.contains(turnWrap))
            return;
          fetch(API_BASE + "/chat/response/" + encodeURIComponent(activeCorrelationId)).then((r) => r.json()).then((d) => {
            mergeQc(d);
            mergeLlmPerformanceUsageFromPoll(turnWrap, d);
            mergeTechnicalPanels(turnWrap, d);
            mergeLlmPerformanceRoutingHydrate(turnWrap, d);
          }).catch(() => {
          });
        };
        const qcRefetchDelaysMs = [800, 2500, 6e3, 12e3, 25e3, 45e3, 75e3, 12e4];
        qcRefetchDelaysMs.forEach((ms) => window.setTimeout(refetchMerged, ms));
      }
      const rosterStepOutputs = data.roster_step_outputs;
      if (Array.isArray(rosterStepOutputs) && rosterStepOutputs.length > 0) {
        turnWrap.appendChild(renderRosterStepOutputs(rosterStepOutputs));
      }
      const pdfBase64 = data.roster_report_pdf_base64;
      const reportMarkdown = data.roster_report_final_md;
      if (pdfBase64 && typeof pdfBase64 === "string" && pdfBase64.length > 0 || reportMarkdown && typeof reportMarkdown === "string" && reportMarkdown.trim().length > 0) {
        turnWrap.appendChild(renderRosterReportDownload(pdfBase64, reportMarkdown));
      }
      const isCard = !!tryParseAnswerCard(body || "");
      if (nextQuestions.length > 0 && !isCard && !useEnvelope) {
        turnWrap.appendChild(
          renderNextQuestions(nextQuestions, (q) => sendMessage(q))
        );
      }
      if (data.clarification_options && data.clarification_options.length > 0) {
        turnWrap.appendChild(
          renderClarificationOptions(data.clarification_options, (value) => sendMessage(value))
        );
      }
      const sourceList = data.sources && data.sources.length > 0 ? data.sources.map((s) => ({
        index: s.index ?? 0,
        document_name: s.document_name ?? "document",
        document_id: s.document_id ?? null,
        page_number: s.page_number ?? null,
        snippet: (s.text ?? "").slice(0, 200),
        cite_text: (s.cite_text ?? s.text ?? "").trim().slice(0, 400) || null,
        source_type: s.source_type ?? null,
        match_score: s.match_score ?? null,
        confidence: s.confidence ?? null,
        open_href: s.open_href ?? null
      })) : sources.length > 0 ? sources.map((s) => ({
        index: s.index ?? 0,
        document_name: s.document_name ?? "document",
        document_id: s.document_id ?? null,
        page_number: s.page_number ?? null,
        snippet: (s.snippet ?? "").slice(0, 120),
        cite_text: (s.snippet ?? "").trim().slice(0, 400) || null,
        source_type: null,
        match_score: null,
        confidence: null
      })) : [];
      const cited = data.cited_source_indices ?? [];
      if (sourceList.length > 0 && !envelopeHasSources) {
        turnWrap.appendChild(
          renderSourceCiter(sourceList, cited, data.correlation_id ?? activeCorrelationId)
        );
      }
      const insightRows = data.usage_breakdown;
      const perfMeta = data.llm_performance;
      if (getShowLlmPerformance(cachedProfile) && Array.isArray(insightRows) && insightRows.length > 0 && data.status === "completed") {
        const tin = Number(data.tokens_used?.input_tokens) || 0;
        const tout = Number(data.tokens_used?.output_tokens) || 0;
        turnWrap.appendChild(
          renderLlmPerformance(insightRows, perfMeta, {
            qc: qcFromPayload,
            sourceConfidenceStrip: data.source_confidence_strip ?? null,
            correlationId: data.correlation_id ?? activeCorrelationId,
            totalCostFallback: data.cost_usd,
            inputTokens: tin,
            outputTokens: tout,
            routingFeedback: data.technical_feedback?.llm_performance ?? null
          })
        );
      }
      mergeTechnicalPanels(turnWrap, data);
      mergeLlmPerformanceRoutingHydrate(turnWrap, data);
      turnWrap.appendChild(renderFeedback(data.correlation_id ?? activeCorrelationId));
      loadSidebarHistory();
      scrollToBottom(messagesEl);
    }).catch((err) => {
      thinkingDone(thinkingLines.length);
      turnWrap.appendChild(
        renderAssistantMessage("Error: " + (err?.message ?? String(err)), true, {})
      );
      scrollToBottom(messagesEl);
    }).finally(() => {
      sendBtn.disabled = false;
      inputEl.disabled = false;
      updateSendState();
    });
  }
  function updateSendState() {
    const hasText = (inputEl.value ?? "").trim().length > 0;
    sendBtn.classList.toggle("active", hasText);
  }
  inputEl.addEventListener("input", updateSendState);
  inputEl.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      sendMessage();
    }
  });
  sendBtn.addEventListener("click", () => sendMessage());
  function openUploadModal() {
    hideRosterUploadReceipt();
    const modal2 = document.getElementById("uploadModal");
    const overlay = document.getElementById("uploadOverlay");
    const form = document.getElementById("uploadForm");
    const st = document.getElementById("uploadStatus");
    const progressWrap = document.getElementById("uploadProgressWrap");
    form?.removeAttribute("aria-busy");
    modal2?.classList.remove("upload-modal--busy");
    if (st) {
      st.textContent = "";
      st.classList.remove("upload-modal-status--working", "upload-modal-status--error");
      st.style.removeProperty("color");
    }
    progressWrap?.setAttribute("hidden", "");
    modal2?.removeAttribute("hidden");
    overlay?.classList.add("open");
    document.getElementById("uploadOrgName")?.focus();
  }
  function setupComposerOptionsMenu() {
    const optionsBtn = document.getElementById("composerOptions");
    const optionsMenu = document.getElementById("composerOptionsMenu");
    const uploadItem = document.getElementById("composerOptionUploadFile");
    function hideOptionsMenu() {
      optionsMenu?.setAttribute("hidden", "");
      optionsBtn?.setAttribute("aria-expanded", "false");
    }
    optionsBtn?.addEventListener("click", (e) => {
      e.stopPropagation();
      const isOpen = !optionsMenu?.hasAttribute("hidden");
      if (isOpen) {
        hideOptionsMenu();
      } else {
        optionsMenu?.removeAttribute("hidden");
        optionsBtn?.setAttribute("aria-expanded", "true");
      }
    });
    uploadItem?.addEventListener("click", () => {
      hideOptionsMenu();
      openUploadModal();
    });
    document.addEventListener("click", () => hideOptionsMenu());
  }
  setupComposerOptionsMenu();
  function setupUploadModal() {
    const uploadModal = document.getElementById("uploadModal");
    const uploadOverlay = document.getElementById("uploadOverlay");
    const uploadForm = document.getElementById("uploadForm");
    const uploadOrgName = document.getElementById("uploadOrgName");
    const uploadFile = document.getElementById("uploadFile");
    const uploadFilePurpose = document.getElementById("uploadFilePurpose");
    const uploadCancel = document.getElementById("uploadCancel");
    const uploadSubmit = document.getElementById("uploadSubmit");
    const uploadStatus = document.getElementById("uploadStatus");
    const uploadProgressWrap = document.getElementById("uploadProgressWrap");
    let uploadPhaseTimers = [];
    let uploadAbort = null;
    function stopUploadPhaseEmits() {
      uploadPhaseTimers.forEach((id) => window.clearTimeout(id));
      uploadPhaseTimers = [];
    }
    function startUploadPhaseEmits(purpose) {
      stopUploadPhaseEmits();
      const roster = purpose === "roster_reconciliation";
      const phases = roster ? [
        { ms: 0, text: "Step 1 of 3 \u2014 Looking up your organization (NPPES / PML)\u2026" },
        { ms: 2800, text: "Step 2 of 3 \u2014 Sending file to the roster service\u2026" },
        { ms: 7e3, text: "Step 3 of 3 \u2014 Parsing rows and resolving NPIs (often 30s\u20132 min)\u2026" },
        { ms: 45e3, text: "Still working \u2014 large rosters can take a bit longer\u2026" }
      ] : [{ ms: 0, text: "Uploading file\u2026" }];
      phases.forEach(({ ms, text }) => {
        const id = window.setTimeout(() => setStatus(text, false, true), ms);
        uploadPhaseTimers.push(id);
      });
    }
    function hideUploadModal() {
      if (uploadAbort) {
        uploadAbort.abort();
        uploadAbort = null;
      }
      stopUploadPhaseEmits();
      uploadModal?.classList.remove("upload-modal--busy");
      uploadForm?.removeAttribute("aria-busy");
      uploadProgressWrap?.setAttribute("hidden", "");
      uploadModal?.setAttribute("hidden", "");
      uploadOverlay?.classList.remove("open");
    }
    function setStatus(msg, isError = false, isWorking = false) {
      if (!uploadStatus)
        return;
      uploadStatus.textContent = msg;
      uploadStatus.classList.toggle("upload-modal-status--working", Boolean(isWorking) && !isError);
      uploadStatus.classList.toggle("upload-modal-status--error", isError);
      if (isError) {
        uploadStatus.style.setProperty("color", "var(--error-text, var(--error))");
      } else {
        uploadStatus.style.removeProperty("color");
      }
    }
    uploadCancel?.addEventListener("click", hideUploadModal);
    uploadOverlay?.addEventListener("click", hideUploadModal);
    function updateSubmitState() {
      const hasFile = !!uploadFile?.files?.length;
      const hasOrg = !!uploadOrgName?.value?.trim();
      if (uploadSubmit)
        uploadSubmit.disabled = !(hasFile && hasOrg);
    }
    uploadOrgName?.addEventListener("input", updateSubmitState);
    uploadFile?.addEventListener("change", updateSubmitState);
    uploadForm?.addEventListener("submit", (e) => {
      e.preventDefault();
      const orgName = uploadOrgName?.value?.trim();
      const file = uploadFile?.files?.[0];
      if (!orgName || !file)
        return;
      uploadSubmit?.setAttribute("disabled", "");
      uploadModal?.classList.add("upload-modal--busy");
      uploadForm?.setAttribute("aria-busy", "true");
      uploadProgressWrap?.removeAttribute("hidden");
      const purpose = (uploadFilePurpose?.value || "roster_reconciliation").trim();
      startUploadPhaseEmits(purpose);
      const formData = new FormData();
      formData.append("file", file);
      formData.append("org_name", orgName);
      formData.append("file_purpose", purpose);
      if (currentThreadId)
        formData.append("thread_id", currentThreadId);
      uploadAbort = new AbortController();
      const signal = uploadAbort.signal;
      fetch(API_BASE + "/chat/roster-upload", { method: "POST", body: formData, signal }).then((r) => {
        if (!r.ok)
          return r.json().then((d) => Promise.reject(d?.detail ?? r.statusText));
        return r.json();
      }).then(
        (data) => {
          const org = data.org_name ?? orgName;
          if (data.thread_id)
            currentThreadId = data.thread_id;
          stopUploadPhaseEmits();
          uploadModal?.classList.remove("upload-modal--busy");
          uploadForm?.removeAttribute("aria-busy");
          uploadProgressWrap?.setAttribute("hidden", "");
          uploadAbort = null;
          showRosterUploadReceipt(data);
          uploadForm?.reset();
          updateSubmitState();
          inputEl.value = `Run reconciliation report for ${org}`;
          updateSendState();
          hideUploadModal();
          const auto = document.getElementById("uploadAutoSendReconciliation");
          if ((uploadFilePurpose?.value || "roster_reconciliation").trim() === "roster_reconciliation" && auto?.checked) {
            window.setTimeout(() => sendMessage(), 0);
          }
        }
      ).catch((err) => {
        const aborted = err instanceof Error && err.name === "AbortError" || typeof DOMException !== "undefined" && err instanceof DOMException && err.name === "AbortError";
        if (aborted) {
          setStatus("Upload cancelled.", false, false);
          return;
        }
        let msg = "Upload failed";
        if (typeof err === "string")
          msg = err;
        else if (err && typeof err === "object" && "detail" in err && err.detail != null)
          msg = String(err.detail);
        else if (err instanceof Error)
          msg = err.message;
        setStatus(msg, true);
      }).finally(() => {
        uploadAbort = null;
        stopUploadPhaseEmits();
        uploadModal?.classList.remove("upload-modal--busy");
        uploadForm?.removeAttribute("aria-busy");
        uploadProgressWrap?.setAttribute("hidden", "");
        uploadSubmit?.removeAttribute("disabled");
      });
    });
  }
  setupUploadModal();
  const btnNewChat = document.getElementById("btnNewChat");
  if (btnNewChat) {
    btnNewChat.addEventListener("click", () => {
      currentThreadId = null;
      hideChatStatusBanner();
      hideRosterUploadReceipt();
      messagesEl.querySelectorAll(".chat-turn").forEach((n) => n.remove());
      if (chatEmpty)
        chatEmpty.classList.remove("hidden");
      loadSidebarHistory();
    });
  }
  function loadSidebarHistory() {
    const recentList = document.getElementById("recentList");
    const helpfulList = document.getElementById("helpfulList");
    const documentsList = document.getElementById("documentsList");
    if (!recentList)
      return;
    const snippet = (q, max = 80) => (q ?? "").trim().slice(0, max) + ((q ?? "").length > max ? "\u2026" : "");
    Promise.all([
      fetch(API_BASE + "/chat/history/recent?limit=20").then(
        (r) => r.json()
      ),
      helpfulList ? fetch(API_BASE + "/chat/history/most-helpful-searches?limit=10").then(
        (r) => r.json()
      ) : Promise.resolve([]),
      documentsList ? fetch(API_BASE + "/chat/history/most-helpful-documents?limit=10").then(
        (r) => r.json()
      ) : Promise.resolve([])
    ]).then(([recent, helpful, documents]) => {
      recentList.innerHTML = "";
      for (const t of recent) {
        const li = document.createElement("li");
        li.className = "recent-item";
        li.textContent = snippet(t.question || "(empty)");
        li.title = t.question || "";
        li.setAttribute("role", "button");
        li.setAttribute("tabindex", "0");
        li.addEventListener("click", () => {
          inputEl.value = t.question ?? "";
          updateSendState();
        });
        li.addEventListener("keydown", (e) => {
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            inputEl.value = t.question ?? "";
            updateSendState();
          }
        });
        recentList.appendChild(li);
      }
      if (helpfulList) {
        helpfulList.innerHTML = "";
        for (const t of helpful) {
          const li = document.createElement("li");
          li.className = "helpful-item";
          li.textContent = snippet(t.question || "(empty)");
          li.title = t.question || "";
          li.setAttribute("role", "button");
          li.setAttribute("tabindex", "0");
          li.addEventListener("click", () => {
            inputEl.value = t.question ?? "";
            updateSendState();
            sendMessage();
          });
          li.addEventListener("keydown", (e) => {
            if (e.key === "Enter" || e.key === " ") {
              e.preventDefault();
              inputEl.value = t.question ?? "";
              updateSendState();
              sendMessage();
            }
          });
          helpfulList.appendChild(li);
        }
      }
      if (documentsList) {
        documentsList.innerHTML = "";
        for (const item of documents) {
          const li = document.createElement("li");
          li.className = "documents-item documents-item--clickable";
          const nameSpan = document.createElement("span");
          nameSpan.textContent = item.document_name;
          li.appendChild(nameSpan);
          const n = item.cited_in_count ?? 0;
          if (n > 0) {
            const citedSpan = document.createElement("span");
            citedSpan.className = "documents-item-cited";
            citedSpan.textContent = n === 1 ? " \u2014 Cited in 1 recent answer." : ` \u2014 Cited in ${n} recent answers.`;
            li.appendChild(citedSpan);
          }
          li.title = "View document";
          li.setAttribute("role", "button");
          li.setAttribute("tabindex", "0");
          li.addEventListener(
            "click",
            () => openDocumentOrSnippet({
              document_id: item.document_id ?? null,
              document_name: item.document_name,
              page_number: null,
              snippet: ""
            })
          );
          li.addEventListener("keydown", (e) => {
            if (e.key === "Enter" || e.key === " ") {
              e.preventDefault();
              openDocumentOrSnippet({
                document_id: item.document_id ?? null,
                document_name: item.document_name,
                page_number: null,
                snippet: ""
              });
            }
          });
          documentsList.appendChild(li);
        }
      }
    }).catch(() => {
      recentList.innerHTML = "";
      if (helpfulList)
        helpfulList.innerHTML = "";
      if (documentsList)
        documentsList.innerHTML = "";
    });
  }
  const chatEmptyLanding = document.getElementById("chatEmpty");
  chatEmptyLanding?.addEventListener("click", (e) => {
    const t = e.target.closest(".landing-try-link");
    if (!t || !(t instanceof HTMLElement))
      return;
    const q = t.getAttribute("data-query")?.trim();
    if (!q)
      return;
    e.preventDefault();
    inputEl.value = q;
    updateSendState();
    sendMessage();
  });
  try {
    const u = new URL(window.location.href);
    const pq = u.searchParams.get("q")?.trim();
    if (pq) {
      u.searchParams.delete("q");
      const next = u.pathname + (u.search ? u.search : "") + u.hash;
      window.history.replaceState({}, "", next);
      inputEl.value = pq;
      updateSendState();
      sendMessage();
    }
  } catch {
  }
  loadSidebarHistory();
  updateSendState();
}
run();
