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
var activeClarificationDraft = null;
function buildWorkflowSelectionPreface() {
  if (!activeClarificationDraft?.length) {
    return null;
  }
  const blocks = [];
  for (const g of activeClarificationDraft) {
    if (g.mode === "multiple") {
      const n = g.multiSelected.size;
      if (n < g.minChoices || n > g.maxChoices) {
        continue;
      }
      const lines = [...g.multiSelected].map((v) => `\u2022 ${v}`);
      blocks.push(`[Mobius workflow_selection slot="${g.slot}"]
` + lines.join("\n"));
    } else {
      const v = (g.singleSelected || "").trim();
      if (v) {
        blocks.push(v);
      }
    }
  }
  if (!blocks.length) {
    return null;
  }
  return blocks.join("\n\n");
}
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
function thinkingLineFromEntry(entry) {
  if (typeof entry === "string") {
    return entry;
  }
  if (entry && typeof entry === "object" && "signal" in entry) {
    const env = entry;
    return (env.note ?? "").trim() || `[${env.signal}]`;
  }
  try {
    return JSON.stringify(entry);
  } catch {
    return String(entry);
  }
}
function normalizeFollowupLineItem(raw, defaultClickable) {
  if (typeof raw === "string") {
    const t = raw.trim();
    return t ? { text: t, clickable: defaultClickable } : null;
  }
  if (raw && typeof raw === "object") {
    const o = raw;
    const text = String(o.text ?? o.label ?? o.line ?? "").trim();
    if (!text)
      return null;
    let clickable = defaultClickable;
    if (typeof o.clickable === "boolean")
      clickable = o.clickable;
    else if (typeof o.tap_to_send === "boolean")
      clickable = o.tap_to_send;
    return { text, clickable };
  }
  return null;
}
function normalizeFollowupLineList(raw, defaultClickable) {
  if (!Array.isArray(raw))
    return [];
  const out = [];
  for (const x of raw) {
    const n = normalizeFollowupLineItem(x, defaultClickable);
    if (n)
      out.push(n);
  }
  return out;
}
function followupListHintLines(items) {
  if (!items.length)
    return "";
  const anyClick = items.some((i) => i.clickable);
  const allStatic = !anyClick;
  if (allStatic)
    return "Reference only\u2014not sent as a message unless you copy or type below.";
  if (items.every((i) => i.clickable))
    return "Tap a line to send it as your next message, or type below.";
  return "Tap lines marked as actions to send; others are for reference only.";
}
var CREDENTIALING_ROSTER_TRIGGERS = [
  "provider roster",
  "credentialing report",
  "roster report",
  "roster reconciliation",
  "reconciliation report",
  "medicaid roster",
  "roster for",
  "medicaid npi report",
  "create a medicaid npi report",
  "create medicaid npi report",
  "create a credentialing report",
  "create credentialing report",
  "i want to create a medicaid npi report",
  "i want to create a credentialing report"
];
var CREDENTIALING_ORG_PREFIXES = [
  "run roster reconciliation report for",
  "roster reconciliation report for",
  "reconciliation report for",
  "run reconciliation report for",
  "provider roster for",
  "credentialing report for",
  "roster report for",
  "medicaid roster for",
  "roster for",
  "create a medicaid npi report for",
  "create medicaid npi report for",
  "create a credentialing report for",
  "create credentialing report for",
  "i want to create a medicaid npi report for",
  "i want to create a credentialing report for",
  "medicaid npi report for"
];
function isCredentialingReportIntent(text) {
  const lower = (text || "").trim().toLowerCase();
  const wantsNewReport = [
    "run roster reconciliation report for",
    "roster reconciliation report for",
    "reconciliation report for",
    "run reconciliation report for",
    "provider roster for",
    "credentialing report for",
    "roster report for",
    "medicaid roster for",
    "roster for",
    "create a medicaid npi report for",
    "create medicaid npi report for",
    "create a credentialing report for",
    "create credentialing report for",
    "medicaid npi report for"
  ];
  if (wantsNewReport.some((t) => lower.includes(t)))
    return true;
  return CREDENTIALING_ROSTER_TRIGGERS.some((t) => lower.includes(t));
}
function orgHintMatchesUploadOrg(orgHint, uploadOrg) {
  const a = (orgHint || "").trim().toLowerCase();
  const b = (uploadOrg || "").trim().toLowerCase();
  if (!a || !b)
    return false;
  return a.includes(b) || b.includes(a);
}
function extractCredentialingOrgHint(text) {
  const rosterLower = text.trim().toLowerCase();
  const rosterCheckText = text.trim();
  for (const t of CREDENTIALING_ORG_PREFIXES) {
    if (rosterLower.includes(t)) {
      return rosterCheckText.slice(rosterLower.indexOf(t) + t.length).trim().replace(/[?.,;!]+$/, "");
    }
  }
  return "";
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
function initModelProfilePicker() {
  const wrap = document.getElementById("modelProfileWrap");
  const sel = document.getElementById("modelProfileSelect");
  const status = document.getElementById("modelProfileStatus");
  if (!wrap || !sel)
    return;
  const setStatus = (text, kind) => {
    if (!status)
      return;
    status.textContent = text || "";
    status.className = "sidebar-llm-status" + (kind ? " sidebar-llm-status--" + kind : "");
  };
  const render = (data) => {
    const profiles = data && data.available_profiles || [];
    const active = data && data.active_profile || "default";
    sel.innerHTML = "";
    profiles.forEach((p) => {
      const opt = document.createElement("option");
      opt.value = p;
      opt.textContent = p;
      if (p === active)
        opt.selected = true;
      sel.appendChild(opt);
    });
  };
  const load = () => {
    fetch(API_BASE + "/chat/admin/model-profile").then((r) => {
      if (r.status === 404) {
        wrap.hidden = true;
        return null;
      }
      if (!r.ok)
        throw new Error("HTTP " + r.status);
      return r.json();
    }).then((d) => {
      if (d)
        render(d);
    }).catch((e) => {
      console.warn("model-profile load failed:", e);
      wrap.hidden = true;
    });
  };
  sel.addEventListener("change", () => {
    const val = sel.value;
    setStatus("\u2026", null);
    fetch(API_BASE + "/chat/admin/model-profile", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ profile: val })
    }).then((r) => r.json().then((d) => ({ ok: r.ok, d }))).then(({ ok, d }) => {
      if (!ok) {
        setStatus(d && d.detail ? "!" : "err", "err");
        return;
      }
      render(d);
      setStatus("\u2713", "ok");
      setTimeout(() => setStatus("", null), 1500);
    }).catch((e) => {
      console.warn("model-profile switch failed:", e);
      setStatus("err", "err");
    });
  });
  load();
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
  const visibleIntents = /* @__PURE__ */ new Set(["requirements", "definitions"]);
  const visible = all.filter((s) => visibleIntents.has(s.intent ?? "process"));
  const hidden = all.filter((s) => !visibleIntents.has(s.intent ?? "process"));
  return { visible, hidden };
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
  if (followupQuestions.length > 0) {
    const followupWrap = document.createElement("div");
    followupWrap.className = "answer-card-followups";
    const label = document.createElement("div");
    label.className = "answer-card-followups-label";
    label.textContent = "Follow-up questions";
    followupWrap.appendChild(label);
    const hint = document.createElement("div");
    hint.className = "answer-card-followups-hint";
    hint.textContent = followupListHintLines(followupQuestions);
    followupWrap.appendChild(hint);
    const chips = document.createElement("div");
    chips.className = "answer-card-followups-chips answer-card-followups-chips--stacked";
    followupQuestions.slice(0, 6).forEach((line) => {
      const text = line.text.trim() || "Ask this";
      if (line.clickable && opts?.onFollowupClick) {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "answer-card-followup-chip answer-card-followup-chip--row";
        btn.textContent = text;
        btn.setAttribute("aria-label", "Send: " + text);
        btn.addEventListener("click", () => opts.onFollowupClick(text));
        chips.appendChild(btn);
      } else {
        const row = document.createElement("div");
        row.className = "answer-card-followup-line answer-card-followup-line--static";
        row.textContent = text;
        chips.appendChild(row);
      }
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
function rosterStepCsvDownloadName(stepId) {
  const raw = (stepId || "roster_step").trim().replace(/[/\\]+/g, "_");
  const base = raw.replace(/[^a-zA-Z0-9._-]+/g, "_").replace(/_+/g, "_").replace(/^_|_$/g, "") || "roster_step";
  return base.toLowerCase().endsWith(".csv") ? base : `${base}.csv`;
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
  const onlyLoc = stepOutputs.length === 1 && (stepOutputs[0].step_id || "").trim() === "find_locations";
  headerTitle.textContent = onlyLoc ? "Practice locations (expand for full list)" : "Step outputs (for validation)";
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
      const csvRaw = (step.csv_content || "").trim();
      if (csvRaw.length > 0) {
        const csvBtn = document.createElement("button");
        csvBtn.type = "button";
        csvBtn.className = "roster-step-download-csv";
        csvBtn.textContent = "Download CSV";
        csvBtn.setAttribute(
          "aria-label",
          `Download ${rosterStepCsvDownloadName(step.step_id || step.label || "step")}`
        );
        csvBtn.addEventListener("click", () => {
          const blob = new Blob([step.csv_content || ""], { type: "text/csv;charset=utf-8" });
          const url = URL.createObjectURL(blob);
          const a = document.createElement("a");
          a.href = url;
          a.download = rosterStepCsvDownloadName(step.step_id || step.label || "step");
          a.click();
          URL.revokeObjectURL(url);
        });
        sectionBody.appendChild(csvBtn);
      }
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
function workflowFollowUpsDraftToLines(raw) {
  if (!Array.isArray(raw))
    return "";
  const lines = [];
  for (const x of raw) {
    if (typeof x === "string" && x.trim())
      lines.push(x.trim());
    else if (x && typeof x === "object" && typeof x.text === "string") {
      const t = String(x.text).trim();
      if (t)
        lines.push(t);
    }
  }
  return lines.join("\n");
}
function parseFollowUpLines(text) {
  return text.split("\n").map((l) => l.trim()).filter((l) => l.length > 0);
}
function draftJsonForTextarea(draft) {
  const d = draft && typeof draft === "object" ? { ...draft } : {};
  delete d.workflow_follow_ups;
  delete d.workflow_follow_ups_hint;
  return JSON.stringify(d, null, 2);
}
function attachWorkflowFromDraft(base, draft) {
  const wf = draft.workflow_follow_ups;
  if (Array.isArray(wf) && wf.length > 0) {
    return { ...base, workflow_follow_ups: wf };
  }
  return base;
}
function draftToValidatedOutput(draft, stepId) {
  const d = draft && typeof draft === "object" ? draft : {};
  let result = {};
  if (stepId === "identify_org" && Array.isArray(d.org_npis)) {
    result = { org_npis: d.org_npis };
  } else if (stepId === "find_locations" && Array.isArray(d.locations)) {
    result = { locations: d.locations };
  } else if (stepId === "find_associated_providers") {
    const out = {};
    if (d.associated_providers && typeof d.associated_providers === "object") {
      out.associated_providers = d.associated_providers;
    }
    if (d.active_roster && typeof d.active_roster === "object") {
      out.active_roster = d.active_roster;
    }
    if (d.use_autopilot_active_cutoff === true) {
      out.use_autopilot_active_cutoff = true;
    }
    if (d.allow_empty_active_roster === true) {
      out.allow_empty_active_roster = true;
    }
    if (Array.isArray(d.roster_line_items)) {
      out.roster_line_items = d.roster_line_items;
    }
    result = out;
  }
  return attachWorkflowFromDraft(result, d);
}
function appendCredentialingWorkflowByStepSection(wrap, cc) {
  const rows = cc.workflow_follow_ups_by_step;
  if (!Array.isArray(rows) || rows.length === 0)
    return;
  const lines = [];
  for (const row of rows) {
    if (!row || typeof row !== "object")
      continue;
    const sid = String(row.step_id ?? "").trim();
    const wfu = row.workflow_follow_ups;
    if (!Array.isArray(wfu) || wfu.length === 0)
      continue;
    for (const item of wfu) {
      if (item && typeof item === "object" && typeof item.text === "string") {
        const src = String(item.source ?? "").trim();
        const tag = src ? ` [${src}]` : "";
        lines.push(`${sid}: ${String(item.text)}${tag}`);
      }
    }
  }
  if (!lines.length)
    return;
  const det = document.createElement("details");
  det.className = "credentialing-copilot-gates";
  const sum = document.createElement("summary");
  sum.textContent = "Workflow follow-ups by step";
  det.appendChild(sum);
  const ul = document.createElement("ul");
  ul.className = "credentialing-copilot-gates-list";
  for (const ln of lines.slice(0, 80)) {
    const li = document.createElement("li");
    li.textContent = ln;
    ul.appendChild(li);
  }
  det.appendChild(ul);
  wrap.appendChild(det);
}
function buildActiveRosterFromPicks(associated, picked) {
  const out = {};
  for (const [locId, rows] of Object.entries(associated)) {
    const want = picked.get(locId);
    const acc = [];
    for (const r of rows || []) {
      const npi = String(r.npi ?? "").trim().padStart(10, "0");
      if (!npi || npi.length !== 10)
        continue;
      if (want?.has(npi)) {
        const c = { ...r };
        c.roster_status = "active";
        acc.push(c);
      }
    }
    out[locId] = acc;
  }
  return out;
}
function renderFindAssociatedRosterEditor(draft, ta) {
  const wrap = document.createElement("div");
  wrap.className = "roster-review-editor";
  const assoc = draft.associated_providers || {};
  const cutoff = Number(draft.active_roster_cutoff ?? 50) || 50;
  const picked = /* @__PURE__ */ new Map();
  const syncTextarea = (flags) => {
    const active = buildActiveRosterFromPicks(assoc, picked);
    const payload = {
      associated_providers: assoc,
      active_roster: active
    };
    if (flags?.useCutoff)
      payload.use_autopilot_active_cutoff = true;
    if (flags?.allowEmpty)
      payload.allow_empty_active_roster = true;
    ta.value = JSON.stringify(payload, null, 2);
  };
  const intro = document.createElement("p");
  intro.className = "roster-review-intro";
  intro.textContent = "Select providers to include in the active panel for downstream steps. In copilot mode the server starts with evidence only; your selection becomes active_roster on Continue.";
  wrap.appendChild(intro);
  for (const [locId, rows] of Object.entries(assoc)) {
    if (!rows?.length)
      continue;
    const sec = document.createElement("div");
    sec.className = "roster-review-location";
    const h = document.createElement("div");
    h.className = "roster-review-location-title";
    h.textContent = `Location ${locId.slice(0, 12)}\u2026 (${rows.length} candidates)`;
    sec.appendChild(h);
    const tbl = document.createElement("table");
    tbl.className = "roster-review-table";
    const thead = document.createElement("thead");
    thead.innerHTML = "<tr><th>Active</th><th>NPI</th><th>Name</th><th>Score</th><th>Basis</th><th>Status</th></tr>";
    tbl.appendChild(thead);
    const tb = document.createElement("tbody");
    const setForLoc = /* @__PURE__ */ new Set();
    picked.set(locId, setForLoc);
    for (const r of rows) {
      const npi = String(r.npi ?? "").trim().padStart(10, "0");
      if (npi.length !== 10)
        continue;
      const score = Number(r.association_likelihood ?? 0);
      const rs = String(r.roster_status ?? "");
      const defaultOn = rs === "active" || rs === "pending_review" && score >= cutoff;
      if (defaultOn)
        setForLoc.add(npi);
      const tr = document.createElement("tr");
      const td0 = document.createElement("td");
      const cb = document.createElement("input");
      cb.type = "checkbox";
      cb.checked = defaultOn;
      cb.addEventListener("change", () => {
        if (cb.checked)
          setForLoc.add(npi);
        else
          setForLoc.delete(npi);
        syncTextarea();
      });
      td0.appendChild(cb);
      tr.appendChild(td0);
      const tdNpi = document.createElement("td");
      tdNpi.textContent = npi;
      tr.appendChild(tdNpi);
      const tdName = document.createElement("td");
      tdName.textContent = String(r.name ?? "");
      tr.appendChild(tdName);
      const tdSc = document.createElement("td");
      tdSc.textContent = String(score);
      tr.appendChild(tdSc);
      const tdBasis = document.createElement("td");
      tdBasis.textContent = String(r.basis_user ?? r.match_type ?? "");
      tr.appendChild(tdBasis);
      const tdSt = document.createElement("td");
      tdSt.textContent = rs || "\u2014";
      tr.appendChild(tdSt);
      tb.appendChild(tr);
    }
    tbl.appendChild(tb);
    sec.appendChild(tbl);
    wrap.appendChild(sec);
  }
  const toolbar = document.createElement("div");
  toolbar.className = "roster-review-toolbar";
  const btnCutoff = document.createElement("button");
  btnCutoff.type = "button";
  btnCutoff.className = "credentialing-copilot-btn credentialing-copilot-btn--secondary";
  btnCutoff.textContent = `Check all with score \u2265 ${cutoff}`;
  btnCutoff.addEventListener("click", () => {
    for (const [locId, rows] of Object.entries(assoc)) {
      const setForLoc = picked.get(locId);
      if (!setForLoc)
        continue;
      setForLoc.clear();
      for (const r of rows || []) {
        const npi = String(r.npi ?? "").trim().padStart(10, "0");
        if (npi.length !== 10)
          continue;
        const score = Number(r.association_likelihood ?? 0);
        if (score >= cutoff)
          setForLoc.add(npi);
      }
    }
    wrap.querySelectorAll("tbody tr").forEach((tr) => {
      const tds = tr.querySelectorAll("td");
      const cb = tds[0]?.querySelector("input");
      const sc = Number(tds[3]?.textContent ?? "");
      if (cb)
        cb.checked = sc >= cutoff;
    });
    syncTextarea();
  });
  const btnAll = document.createElement("button");
  btnAll.type = "button";
  btnAll.className = "credentialing-copilot-btn credentialing-copilot-btn--secondary";
  btnAll.textContent = "Check all candidates";
  btnAll.addEventListener("click", () => {
    for (const [locId, rows] of Object.entries(assoc)) {
      const setForLoc = picked.get(locId);
      if (!setForLoc)
        continue;
      setForLoc.clear();
      for (const r of rows || []) {
        const npi = String(r.npi ?? "").trim().padStart(10, "0");
        if (npi.length === 10)
          setForLoc.add(npi);
      }
    }
    wrap.querySelectorAll("input[type=checkbox]").forEach((cb) => {
      cb.checked = true;
    });
    syncTextarea();
  });
  const btnNone = document.createElement("button");
  btnNone.type = "button";
  btnNone.className = "credentialing-copilot-btn credentialing-copilot-btn--secondary";
  btnNone.textContent = "Clear all";
  btnNone.addEventListener("click", () => {
    picked.forEach((s) => s.clear());
    wrap.querySelectorAll("input[type=checkbox]").forEach((cb) => {
      cb.checked = false;
    });
    syncTextarea();
  });
  toolbar.appendChild(btnCutoff);
  toolbar.appendChild(btnAll);
  toolbar.appendChild(btnNone);
  wrap.appendChild(toolbar);
  syncTextarea();
  return wrap;
}
function appendCredentialingPrerequisitesSection(wrap, cc) {
  const pr = cc.credentialing_prerequisites;
  if (!pr || typeof pr !== "object")
    return;
  const recs = Array.isArray(pr.recommendations) ? pr.recommendations.filter((x) => typeof x === "string" && x.trim().length > 0) : [];
  const det = document.createElement("details");
  det.className = "credentialing-copilot-env";
  const sum = document.createElement("summary");
  sum.textContent = "Environment \u2014 what you need to run this";
  det.appendChild(sum);
  const body = document.createElement("div");
  body.className = "credentialing-copilot-env-body";
  if (recs.length) {
    const ul = document.createElement("ul");
    for (const r of recs) {
      const li = document.createElement("li");
      li.textContent = r;
      ul.appendChild(li);
    }
    body.appendChild(ul);
  } else {
    const ok = document.createElement("p");
    ok.className = "credentialing-copilot-env-ok";
    if (pr.ready_for_persisted_copilot_runs) {
      ok.textContent = "Roster skill URL and chat database look configured; co-pilot runs should persist across API and worker.";
    } else if (pr.ready_for_credentialing_api) {
      ok.textContent = "Roster skill URL is set. Add CHAT_RAG_DATABASE_URL (or RAG_DATABASE_URL) if you need persistence and DB-backed assertions.";
    } else {
      ok.textContent = "Set CHAT_SKILLS_PROVIDER_ROSTER_CREDENTIALING_URL before org/location/provider steps can call the skill API.";
    }
    body.appendChild(ok);
  }
  det.appendChild(body);
  wrap.appendChild(det);
}
function appendCredentialingGateTimeline(wrap, cc) {
  const evs = cc.gate_events;
  if (!Array.isArray(evs) || evs.length === 0)
    return;
  const det = document.createElement("details");
  det.className = "credentialing-copilot-gates";
  const sum = document.createElement("summary");
  sum.textContent = `Recent credentialing gates (${evs.length})`;
  det.appendChild(sum);
  const ol = document.createElement("ol");
  ol.className = "credentialing-copilot-gates-list";
  for (const raw of evs) {
    if (!raw || typeof raw !== "object")
      continue;
    const o = raw;
    const li = document.createElement("li");
    const sid = String(o.step_id ?? "").trim();
    const code = String(o.reason_code ?? "").trim();
    const detail = String(o.detail ?? "").trim();
    const head = [sid, code].filter(Boolean).join(" \u2014 ");
    li.textContent = head ? detail ? `${head}. ${detail}` : head : detail || "(gate)";
    ol.appendChild(li);
  }
  det.appendChild(ol);
  wrap.appendChild(det);
}
function renderCredentialingCopilotPanel(cc, threadId) {
  const wrap = document.createElement("div");
  wrap.className = "credentialing-copilot-panel";
  const title = document.createElement("div");
  title.className = "credentialing-copilot-title";
  title.textContent = "Credentialing co-pilot \u2014 validate step";
  wrap.appendChild(title);
  const meta = document.createElement("div");
  meta.className = "credentialing-copilot-meta";
  meta.textContent = `${cc.org_name || "\u2014"} \xB7 run ${cc.run_id.slice(0, 8)}\u2026 \xB7 ${cc.phase || "\u2014"}`;
  wrap.appendChild(meta);
  appendCredentialingPrerequisitesSection(wrap, cc);
  appendCredentialingGateTimeline(wrap, cc);
  appendCredentialingWorkflowByStepSection(wrap, cc);
  if (cc.phase === "complete") {
    const done = document.createElement("div");
    done.className = "credentialing-copilot-complete";
    done.textContent = "All steps complete. See the message above for the report summary.";
    wrap.appendChild(done);
    return wrap;
  }
  const pending = (cc.pending_step_id || "").trim();
  if (!pending) {
    const err = document.createElement("div");
    err.className = "credentialing-copilot-error";
    err.textContent = "No pending step.";
    wrap.appendChild(err);
    return wrap;
  }
  const stepLabel = document.createElement("div");
  stepLabel.className = "credentialing-copilot-step";
  stepLabel.textContent = `Pending step: ${pending}`;
  wrap.appendChild(stepLabel);
  const ta = document.createElement("textarea");
  ta.className = "credentialing-copilot-json";
  ta.rows = pending === "find_associated_providers" ? 6 : 12;
  ta.spellcheck = false;
  ta.value = draftJsonForTextarea(cc.draft_output ?? void 0);
  ta.setAttribute("aria-label", "Validated output JSON for this step");
  if (pending === "find_associated_providers") {
    wrap.appendChild(
      renderFindAssociatedRosterEditor(cc.draft_output ?? {}, ta)
    );
  }
  wrap.appendChild(ta);
  const followHint = document.createElement("div");
  followHint.className = "credentialing-copilot-meta";
  const hintText = String(cc.draft_output?.workflow_follow_ups_hint ?? "").trim();
  followHint.textContent = hintText || "Follow-up / next steps (optional, one per line) \u2014 stored on this step when you continue.";
  wrap.appendChild(followHint);
  const followTa = document.createElement("textarea");
  followTa.className = "credentialing-copilot-json credentialing-copilot-followups";
  followTa.rows = 3;
  followTa.spellcheck = false;
  followTa.value = workflowFollowUpsDraftToLines(cc.draft_output?.workflow_follow_ups);
  followTa.setAttribute("aria-label", "Workflow follow-up lines for this step");
  wrap.appendChild(followTa);
  const btnRow = document.createElement("div");
  btnRow.className = "credentialing-copilot-actions";
  const acceptBtn = document.createElement("button");
  acceptBtn.type = "button";
  acceptBtn.className = "credentialing-copilot-btn credentialing-copilot-btn--secondary";
  acceptBtn.textContent = "Accept draft as-is";
  acceptBtn.addEventListener("click", () => {
    ta.value = draftJsonForTextarea(cc.draft_output ?? void 0);
    followTa.value = workflowFollowUpsDraftToLines(cc.draft_output?.workflow_follow_ups);
  });
  const submitBtn = document.createElement("button");
  submitBtn.type = "button";
  submitBtn.className = "credentialing-copilot-btn credentialing-copilot-btn--primary";
  submitBtn.textContent = "Continue (submit validation)";
  submitBtn.addEventListener("click", async () => {
    let validated;
    try {
      validated = JSON.parse(ta.value);
    } catch {
      alert("Invalid JSON \u2014 fix the textarea or use Accept draft as-is.");
      return;
    }
    const fuLines = parseFollowUpLines(followTa.value);
    if (fuLines.length)
      validated.workflow_follow_ups = fuLines;
    submitBtn.disabled = true;
    acceptBtn.disabled = true;
    try {
      const r = await fetch(
        API_BASE + "/chat/credentialing-runs/" + encodeURIComponent(cc.run_id) + "/validate",
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ step_id: pending, validated_output: validated })
        }
      );
      const data = await r.json();
      if (!r.ok) {
        throw new Error(data.detail || data.error || r.statusText);
      }
      const next = {
        run_id: data.run_id || cc.run_id,
        pending_step_id: data.pending_step_id,
        phase: data.phase,
        draft_output: data.draft_output,
        mode: data.mode || "copilot",
        org_name: data.org_name ?? cc.org_name,
        final_report_text: data.final_report_text,
        gate_events: Array.isArray(data.gate_events) ? data.gate_events : cc.gate_events,
        last_gate_event: data.last_gate_event && typeof data.last_gate_event === "object" ? data.last_gate_event : data.last_gate_event === null ? null : cc.last_gate_event,
        credentialing_prerequisites: data.credentialing_prerequisites && typeof data.credentialing_prerequisites === "object" ? data.credentialing_prerequisites : cc.credentialing_prerequisites,
        workflow_follow_ups_by_step: Array.isArray(data.workflow_follow_ups_by_step) ? data.workflow_follow_ups_by_step : cc.workflow_follow_ups_by_step
      };
      const parent = wrap.parentElement;
      const replacement = renderCredentialingCopilotPanel(next, threadId);
      parent?.replaceChild(replacement, wrap);
    } catch (e) {
      alert("Validation failed: " + (e instanceof Error ? e.message : String(e)));
      submitBtn.disabled = false;
      acceptBtn.disabled = false;
    }
  });
  const quickAccept = document.createElement("button");
  quickAccept.type = "button";
  quickAccept.className = "credentialing-copilot-btn credentialing-copilot-btn--secondary";
  quickAccept.textContent = "Use curated fields only (recommended)";
  quickAccept.addEventListener("click", () => {
    const vo = draftToValidatedOutput(cc.draft_output ?? void 0, pending);
    const merged = { ...cc.draft_output ?? {}, ...vo };
    ta.value = draftJsonForTextarea(merged);
    followTa.value = workflowFollowUpsDraftToLines(merged.workflow_follow_ups);
  });
  btnRow.appendChild(quickAccept);
  btnRow.appendChild(acceptBtn);
  btnRow.appendChild(submitBtn);
  wrap.appendChild(btnRow);
  if (threadId) {
    const tidNote = document.createElement("div");
    tidNote.className = "credentialing-copilot-hint";
    tidNote.textContent = `Thread ${threadId.slice(0, 8)}\u2026 \u2014 you can also ask the assistant to validate this step in chat.`;
    wrap.appendChild(tidNote);
  }
  return wrap;
}
function renderRosterReportDownload(pdfBase64, reportMarkdown, attachmentsKind) {
  const wrap = document.createElement("div");
  wrap.className = "roster-report-download";
  const title = document.createElement("div");
  title.className = "roster-report-download-title";
  title.textContent = attachmentsKind === "reconciliation" ? "Roster alignment with NPPES (Phase 1)" : "Credentialing report";
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
  const pdfName = attachmentsKind === "reconciliation" ? "roster_reconciliation_report.pdf" : "credentialing_report.pdf";
  const mdName = attachmentsKind === "reconciliation" ? "roster_reconciliation_report.md" : "credentialing_report.md";
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
        a.download = pdfName;
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
      a.download = mdName;
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
function thinkingStreamSuggestsAnswering(raw) {
  const t = (raw ?? "").trim();
  const sanitized = sanitizeDisplayMessage(raw);
  const display = t.startsWith("{") ? "Formatting answer\u2026" : normalizeMessageText(sanitized);
  return display.trim().length > 0 && display !== "Formatting answer\u2026";
}
var MODE_LABELS = {
  quick: "\u26A1 Fast",
  copilot: "\u25C9 Normal",
  agentic: "\u2726 Thinking"
};
function renderUserMessage(text, mode) {
  const wrap = document.createElement("div");
  wrap.className = "message message--user";
  const bubble = document.createElement("div");
  bubble.className = "message-bubble";
  bubble.textContent = text;
  wrap.appendChild(bubble);
  if (mode && MODE_LABELS[mode]) {
    const badge = document.createElement("div");
    badge.className = "msg-mode-badge";
    badge.textContent = MODE_LABELS[mode];
    wrap.appendChild(badge);
  }
  return wrap;
}
function renderThinkingBlock(initialLines, opts) {
  const block = document.createElement("div");
  block.className = "thinking-block thinking-block--compact" + (initialLines.length ? "" : " collapsed");
  block.setAttribute("aria-busy", "true");
  const preview = document.createElement("div");
  preview.className = "thinking-preview";
  preview.setAttribute("role", "button");
  preview.setAttribute("tabindex", "0");
  preview.setAttribute("aria-expanded", initialLines.length > 0 ? "true" : "false");
  const phaseRow = document.createElement("span");
  phaseRow.className = "thinking-phase thinking-phase--live";
  phaseRow.setAttribute("aria-hidden", "true");
  const phaseDot = document.createElement("span");
  phaseDot.className = "thinking-phase-dot";
  const phaseLabel = document.createElement("span");
  phaseLabel.className = "thinking-phase-label";
  phaseLabel.textContent = "Queued";
  phaseRow.appendChild(phaseDot);
  phaseRow.appendChild(phaseLabel);
  const statusWord = document.createElement("span");
  statusWord.className = "thinking-word";
  statusWord.textContent = "Thinking";
  const lineEl = document.createElement("span");
  lineEl.className = "thinking-rule";
  preview.appendChild(phaseRow);
  preview.appendChild(statusWord);
  preview.appendChild(lineEl);
  const announcer = document.createElement("span");
  announcer.className = "thinking-phase-announcer";
  announcer.setAttribute("aria-live", "polite");
  announcer.setAttribute("aria-atomic", "true");
  const body = document.createElement("div");
  body.className = "thinking-body";
  initialLines.forEach((line) => {
    const div = document.createElement("div");
    div.className = "thinking-line";
    div.textContent = line;
    body.appendChild(div);
  });
  let lastStatusLine = "";
  let requestPhase = 0;
  let failedRequest = false;
  const PHASE_ARIA = [
    "Request queued",
    "Working on your request",
    "Composing answer",
    "Complete"
  ];
  function announcePhase() {
    if (failedRequest) {
      announcer.textContent = "Request ended with an error";
      return;
    }
    announcer.textContent = PHASE_ARIA[Math.min(requestPhase, 3)] ?? "";
  }
  function syncPhaseRow() {
    phaseRow.classList.remove("thinking-phase--live", "thinking-phase--done", "thinking-phase--error");
    if (failedRequest) {
      phaseRow.classList.add("thinking-phase--error");
      phaseLabel.textContent = "Error";
    } else if (requestPhase >= 3) {
      phaseRow.classList.add("thinking-phase--done");
      phaseLabel.textContent = "Done";
    } else {
      phaseRow.classList.add("thinking-phase--live");
      const labels = ["Queued", "Working", "Answering"];
      phaseLabel.textContent = labels[Math.min(requestPhase, 2)] ?? "Queued";
    }
    announcePhase();
  }
  syncPhaseRow();
  if (initialLines.length) {
    lastStatusLine = initialLines[initialLines.length - 1] ?? "";
    if (lastStatusLine)
      statusWord.textContent = thinkingFriendlyStatus(lastStatusLine);
  }
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
  block.appendChild(announcer);
  block.appendChild(body);
  return {
    el: block,
    setPreview(text) {
      lastStatusLine = text;
      statusWord.textContent = thinkingFriendlyStatus(text);
      syncPhaseRow();
    },
    addLine(line) {
      lastStatusLine = line;
      statusWord.textContent = thinkingFriendlyStatus(line);
      const div = document.createElement("div");
      div.className = "thinking-line";
      div.textContent = line;
      body.appendChild(div);
      block.classList.remove("collapsed");
      preview.setAttribute("aria-expanded", "true");
      body.scrollTop = body.scrollHeight;
    },
    done(_lineCount) {
      if (!failedRequest)
        requestPhase = 3;
      syncPhaseRow();
      statusWord.textContent = lastStatusLine ? thinkingFriendlyStatus(lastStatusLine) : "Ready";
      block.setAttribute("aria-busy", "false");
      block.classList.add("thinking-block--done");
      setTimeout(() => {
        collapse();
      }, 2500);
    },
    onRequestCorrelationId() {
      if (failedRequest || requestPhase >= 1)
        return;
      requestPhase = 1;
      syncPhaseRow();
    },
    onRequestStreamChunk(accumulatedRaw) {
      if (failedRequest || requestPhase >= 2)
        return;
      if (thinkingStreamSuggestsAnswering(accumulatedRaw)) {
        requestPhase = 2;
        syncPhaseRow();
      }
    },
    markRequestFailed() {
      failedRequest = true;
      block.setAttribute("aria-busy", "false");
      syncPhaseRow();
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
  hint.textContent = followupListHintLines(questions);
  wrap.appendChild(hint);
  const chips = document.createElement("div");
  chips.className = "next-questions-chips next-questions-chips--stacked";
  questions.slice(0, 6).forEach((line) => {
    const text = line.text.trim() || "Ask this";
    if (line.clickable) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "next-questions-chip next-questions-chip--row";
      btn.textContent = text;
      btn.setAttribute("aria-label", "Send: " + text);
      btn.addEventListener("click", () => onSelect(text));
      chips.appendChild(btn);
    } else {
      const row = document.createElement("div");
      row.className = "next-questions-line next-questions-line--static";
      row.textContent = text;
      chips.appendChild(row);
    }
  });
  wrap.appendChild(chips);
  return wrap;
}
function clarificationSelectionIsMultiple(opt) {
  const m = (opt.selection_mode || "single").toLowerCase();
  return m === "multiple" || m === "multi";
}
var CLARIFICATION_FREE_TEXT_FALLBACK = "You can also type your own answer in the box below (optional), then press Send.";
function clarificationShowsFreeTextHint(opt) {
  return opt.allow_free_text !== false;
}
function clarificationFreeTextHintLine(opt) {
  if (!clarificationShowsFreeTextHint(opt)) {
    return null;
  }
  const h = (opt.free_text_hint || "").trim();
  return h || CLARIFICATION_FREE_TEXT_FALLBACK;
}
function renderClarificationMultiGroup(opt) {
  const group = document.createElement("div");
  group.className = "clarification-option-group clarification-option-group--multi";
  const labelEl = document.createElement("div");
  labelEl.className = "clarification-option-label";
  labelEl.textContent = opt.label;
  group.appendChild(labelEl);
  const n = opt.choices.length;
  let minC = opt.min_choices != null ? Math.max(0, opt.min_choices) : 1;
  let maxC = opt.max_choices != null ? Math.max(0, opt.max_choices) : n;
  minC = Math.min(minC, n);
  maxC = Math.min(maxC, n);
  if (maxC < minC) {
    maxC = minC;
  }
  const selected = /* @__PURE__ */ new Set();
  const chips = document.createElement("div");
  chips.className = "clarification-option-chips clarification-option-chips--multi";
  const hint = document.createElement("div");
  hint.className = "clarification-option-multi-hint";
  const slot = (opt.slot || "workflow_selection").trim();
  const draft = {
    slot,
    mode: "multiple",
    multiSelected: selected,
    singleSelected: null,
    minChoices: minC,
    maxChoices: maxC
  };
  if (activeClarificationDraft) {
    activeClarificationDraft.push(draft);
  }
  function syncHintOnly() {
    if (minC === maxC) {
      hint.textContent = `Select exactly ${minC} option(s), add a message in the box below if you like, then press Send.`;
    } else {
      hint.textContent = `Select ${minC}\u2013${maxC} option(s), type below (optional), then press Send.`;
    }
  }
  function toggleChoice(value, btn) {
    if (selected.has(value)) {
      selected.delete(value);
      btn.classList.remove("clarification-option-chip--selected");
      btn.setAttribute("aria-pressed", "false");
    } else {
      if (selected.size >= maxC) {
        return;
      }
      selected.add(value);
      btn.classList.add("clarification-option-chip--selected");
      btn.setAttribute("aria-pressed", "true");
    }
    syncHintOnly();
  }
  for (const c of opt.choices) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "clarification-option-chip clarification-option-chip--toggle";
    btn.textContent = c.label;
    btn.setAttribute("aria-pressed", "false");
    const val = c.value;
    btn.addEventListener("click", () => toggleChoice(val, btn));
    chips.appendChild(btn);
  }
  group.appendChild(chips);
  const footer = document.createElement("div");
  footer.className = "clarification-option-multi-footer";
  footer.appendChild(hint);
  group.appendChild(footer);
  syncHintOnly();
  return group;
}
function renderClarificationOptions(opts) {
  activeClarificationDraft = [];
  const wrap = document.createElement("div");
  wrap.className = "clarification-options";
  for (const opt of opts) {
    if (clarificationSelectionIsMultiple(opt)) {
      wrap.appendChild(renderClarificationMultiGroup(opt));
      continue;
    }
    const group = document.createElement("div");
    group.className = "clarification-option-group";
    const labelEl = document.createElement("div");
    labelEl.className = "clarification-option-label";
    labelEl.textContent = opt.label;
    group.appendChild(labelEl);
    const chips = document.createElement("div");
    chips.className = "clarification-option-chips";
    group.appendChild(chips);
    const slot = (opt.slot || "workflow_selection").trim();
    const draft = {
      slot,
      mode: "single",
      multiSelected: /* @__PURE__ */ new Set(),
      singleSelected: null,
      minChoices: 0,
      maxChoices: 1
    };
    if (activeClarificationDraft) {
      activeClarificationDraft.push(draft);
    }
    for (const c of opt.choices) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "clarification-option-chip clarification-option-chip--toggle";
      btn.setAttribute("aria-pressed", "false");
      btn.textContent = c.label;
      btn.addEventListener("click", () => {
        chips.querySelectorAll("button.clarification-option-chip").forEach((b) => {
          b.classList.remove("clarification-option-chip--selected");
          b.setAttribute("aria-pressed", "false");
        });
        btn.classList.add("clarification-option-chip--selected");
        btn.setAttribute("aria-pressed", "true");
        draft.singleSelected = c.value;
      });
      chips.appendChild(btn);
    }
    const hintSingle = document.createElement("div");
    hintSingle.className = "clarification-option-free-text-hint";
    const freeLn = clarificationFreeTextHintLine(opt);
    hintSingle.textContent = freeLn || "Tap a choice, then press Send.";
    group.appendChild(hintSingle);
    wrap.appendChild(group);
  }
  if (!activeClarificationDraft.length) {
    activeClarificationDraft = null;
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
function _ensureDocReaderDOM() {
  if (document.getElementById("doc-reader-panel"))
    return;
  const overlay = document.createElement("div");
  overlay.id = "doc-reader-overlay";
  overlay.addEventListener("click", closeDocReaderPanel);
  document.body.appendChild(overlay);
  const panel = document.createElement("div");
  panel.id = "doc-reader-panel";
  panel.innerHTML = '<div class="doc-reader-header"><span class="doc-reader-title">Loading\u2026</span><span class="doc-reader-meta"></span><div class="doc-reader-header-actions"><button class="bookmarks-btn" title="Bookmarks">Bookmarks <span class="bm-count">0</span></button><a class="doc-reader-rag-link" href="#" target="_blank" rel="noopener noreferrer">Open in RAG &#8599;</a><button class="doc-reader-close" title="Close">&times;</button></div></div><div class="doc-reader-body"><nav class="doc-reader-toc"></nav><div class="doc-reader-content"></div></div>';
  panel.querySelector(".doc-reader-close").addEventListener("click", closeDocReaderPanel);
  const bmBtn = panel.querySelector(".bookmarks-btn");
  bmBtn.addEventListener("click", () => _toggleBookmarksDrawer(bmBtn));
  document.body.appendChild(panel);
}
function _updateBookmarksBadge(panel) {
  try {
    const bm = JSON.parse(localStorage.getItem(_BOOKMARKS_KEY) || "[]");
    const badge = panel.querySelector(".bm-count");
    if (badge)
      badge.textContent = String(bm.length);
  } catch {
  }
}
function openDocReaderPanel(documentId, pageNumber, citeText) {
  if (!documentId)
    return;
  _ensureDocReaderDOM();
  const panel = document.getElementById("doc-reader-panel");
  const overlay = document.getElementById("doc-reader-overlay");
  const content = panel.querySelector(".doc-reader-content");
  const tocEl = panel.querySelector(".doc-reader-toc");
  const titleEl = panel.querySelector(".doc-reader-title");
  const metaEl = panel.querySelector(".doc-reader-meta");
  const ragLink = panel.querySelector(".doc-reader-rag-link");
  requestAnimationFrame(() => {
    overlay.classList.add("open");
    panel.classList.add("open");
  });
  content.innerHTML = '<div class="doc-reader-loading">Loading document\u2026</div>';
  tocEl.innerHTML = "";
  titleEl.textContent = "Loading\u2026";
  metaEl.textContent = "";
  const ragUrl = getRagDocumentUrl(documentId, pageNumber, citeText ?? null);
  if (ragUrl) {
    ragLink.href = ragUrl;
    ragLink.style.display = "";
  } else {
    ragLink.style.display = "none";
  }
  _updateBookmarksBadge(panel);
  const apiBase = (typeof API_BASE === "string" ? API_BASE : "").replace(/\/$/, "");
  fetch(apiBase + "/chat/doc-reader/read", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ document_id: documentId, view: "full" })
  }).then((r) => {
    if (!r.ok)
      throw new Error(String(r.status));
    return r.json();
  }).then((env) => _renderDocReaderEnvelope(env, pageNumber ?? null, citeText ?? null)).catch((err) => {
    content.innerHTML = '<div class="doc-reader-error">Failed to load: ' + err.message + "</div>";
    titleEl.textContent = "Error";
  });
}
function _renderDocReaderEnvelope(env, scrollToPage, highlightText) {
  const panel = document.getElementById("doc-reader-panel");
  if (!panel)
    return;
  const content = panel.querySelector(".doc-reader-content");
  const tocEl = panel.querySelector(".doc-reader-toc");
  const titleEl = panel.querySelector(".doc-reader-title");
  const metaEl = panel.querySelector(".doc-reader-meta");
  titleEl.textContent = env.display_name || "Document";
  const parts = [];
  if (env.payer)
    parts.push(env.payer);
  if (env.authority_level)
    parts.push(env.authority_level);
  if (env.sections)
    parts.push(env.sections.length + " sections");
  metaEl.textContent = parts.join(" \xB7 ");
  panel.dataset.docId = env.document_id || "";
  panel.dataset.docName = env.display_name || "";
  tocEl.innerHTML = "";
  (env.toc || []).forEach((t) => {
    const a = document.createElement("a");
    a.className = "doc-reader-toc-item" + ((t.depth || 0) > 1 ? " depth-" + t.depth : "");
    a.textContent = t.heading || "(untitled)";
    a.title = t.page_range || "";
    a.addEventListener("click", () => {
      const target = content.querySelector('[data-section-id="' + (t.section_id ?? "") + '"]');
      if (target)
        target.scrollIntoView({ behavior: "smooth", block: "start" });
      tocEl.querySelectorAll(".active").forEach((el2) => el2.classList.remove("active"));
      a.classList.add("active");
    });
    tocEl.appendChild(a);
  });
  content.innerHTML = "";
  let scrollTarget = null;
  (env.sections || []).forEach((sec) => {
    const card = document.createElement("div");
    card.className = "doc-reader-section";
    card.dataset.sectionId = sec.section_id || "";
    card.dataset.pageStart = sec.page_start != null ? String(sec.page_start) : "";
    const header = document.createElement("div");
    header.className = "doc-reader-section-header";
    const hs = document.createElement("span");
    hs.textContent = sec.heading || "Section";
    const ps = document.createElement("span");
    ps.className = "doc-reader-section-page";
    ps.textContent = sec.page_start != null ? "p." + sec.page_start : "";
    header.appendChild(hs);
    header.appendChild(ps);
    const body = document.createElement("div");
    body.className = "doc-reader-section-body";
    let html = simpleMarkdownToHtml(sec.body_markdown || "");
    if (highlightText && highlightText.trim()) {
      const esc = highlightText.trim().replace(/[.*+?^${}()|[\]\\]/g, "\\$&").slice(0, 100);
      try {
        html = html.replace(new RegExp("(" + esc + ")", "gi"), '<mark class="doc-reader-highlight">$1</mark>');
      } catch {
      }
    }
    body.innerHTML = html;
    header.addEventListener("click", () => {
      body.style.display = body.style.display === "none" ? "" : "none";
    });
    card.appendChild(header);
    card.appendChild(body);
    if (sec.citations && sec.citations.length > 0) {
      const cr = document.createElement("div");
      cr.className = "doc-reader-section-citations";
      sec.citations.forEach((c) => {
        const badge = document.createElement("span");
        badge.className = "doc-reader-cite-badge";
        badge.textContent = c.display || "p." + (c.page ?? "");
        badge.title = (c.snippet || "").slice(0, 150);
        cr.appendChild(badge);
      });
      card.appendChild(cr);
    }
    content.appendChild(card);
    if (scrollToPage != null && String(sec.page_start) === String(scrollToPage)) {
      scrollTarget = card;
    }
  });
  if (scrollTarget) {
    setTimeout(() => scrollTarget.scrollIntoView({ behavior: "smooth", block: "start" }), 100);
  }
}
function closeDocReaderPanel() {
  const panel = document.getElementById("doc-reader-panel");
  const overlay = document.getElementById("doc-reader-overlay");
  if (panel)
    panel.classList.remove("open");
  if (overlay)
    overlay.classList.remove("open");
}
function _getPageFromElement(el2) {
  const card = el2.closest(".doc-reader-section");
  if (card && card.dataset.pageStart)
    return card.dataset.pageStart;
  return null;
}
function _toggleBookmarksDrawer(btn) {
  const existing = btn.querySelector(".bookmarks-drawer");
  if (existing) {
    existing.remove();
    return;
  }
  const drawer = document.createElement("div");
  drawer.className = "bookmarks-drawer";
  drawer.addEventListener("click", (e) => e.stopPropagation());
  let bm = [];
  try {
    bm = JSON.parse(localStorage.getItem(_BOOKMARKS_KEY) || "[]");
  } catch {
    bm = [];
  }
  if (bm.length === 0) {
    drawer.innerHTML = '<div class="bookmarks-drawer-empty">No bookmarks yet. Select text and click Bookmark.</div>';
  } else {
    bm.forEach((b, idx) => {
      const item = document.createElement("div");
      item.className = "bookmark-item";
      const te = document.createElement("div");
      te.className = "bookmark-text";
      te.textContent = b.text || "";
      const me = document.createElement("div");
      me.className = "bookmark-meta";
      const info = document.createElement("span");
      info.textContent = (b.documentName || "Doc") + (b.page ? ", p." + b.page : "") + " \xB7 " + new Date(b.timestamp || Date.now()).toLocaleDateString();
      const del = document.createElement("button");
      del.className = "bookmark-delete";
      del.textContent = "Remove";
      del.addEventListener("click", (e) => {
        e.stopPropagation();
        bm.splice(idx, 1);
        localStorage.setItem(_BOOKMARKS_KEY, JSON.stringify(bm));
        item.remove();
        if (bm.length === 0)
          drawer.innerHTML = '<div class="bookmarks-drawer-empty">No bookmarks.</div>';
        const p = document.getElementById("doc-reader-panel");
        if (p)
          _updateBookmarksBadge(p);
      });
      me.appendChild(info);
      me.appendChild(del);
      item.appendChild(te);
      item.appendChild(me);
      item.addEventListener("click", () => {
        if (b.documentId)
          openDocReaderPanel(b.documentId, b.page, (b.text || "").slice(0, 50));
        drawer.remove();
      });
      drawer.appendChild(item);
    });
  }
  btn.appendChild(drawer);
  const closeHandler = (e) => {
    const t = e.target;
    if (drawer.contains(t) || btn.contains(t))
      return;
    drawer.remove();
    document.removeEventListener("click", closeHandler);
  };
  setTimeout(() => document.addEventListener("click", closeHandler), 0);
}
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape")
    closeDocReaderPanel();
});
var _activeToolbar = null;
var _BOOKMARKS_KEY = "mobius_bookmarks";
function _svgIcon(name) {
  const icons = {
    copy: '<svg viewBox="0 0 16 16" fill="currentColor"><path d="M0 6.75C0 5.784.784 5 1.75 5h1.5a.75.75 0 010 1.5h-1.5a.25.25 0 00-.25.25v7.5c0 .138.112.25.25.25h7.5a.25.25 0 00.25-.25v-1.5a.75.75 0 011.5 0v1.5A1.75 1.75 0 019.25 16h-7.5A1.75 1.75 0 010 14.25z"/><path d="M5 1.75C5 .784 5.784 0 6.75 0h7.5C15.216 0 16 .784 16 1.75v7.5A1.75 1.75 0 0114.25 11h-7.5A1.75 1.75 0 015 9.25zm1.75-.25a.25.25 0 00-.25.25v7.5c0 .138.112.25.25.25h7.5a.25.25 0 00.25-.25v-7.5a.25.25 0 00-.25-.25z"/></svg>',
    bookmark: '<svg viewBox="0 0 16 16" fill="currentColor"><path d="M3 2.75C3 1.784 3.784 1 4.75 1h6.5c.966 0 1.75.784 1.75 1.75v11.5a.75.75 0 01-1.227.579L8 11.722l-3.773 3.107A.75.75 0 013 14.25zm1.75-.25a.25.25 0 00-.25.25v9.91l3.023-2.489a.75.75 0 01.954 0l3.023 2.49V2.75a.25.25 0 00-.25-.25z"/></svg>',
    cite: '<svg viewBox="0 0 16 16" fill="currentColor"><path d="M1.75 2h12.5c.966 0 1.75.784 1.75 1.75v8.5A1.75 1.75 0 0114.25 14H1.75A1.75 1.75 0 010 12.25v-8.5C0 2.784.784 2 1.75 2zm0 1.5a.25.25 0 00-.25.25v8.5c0 .138.112.25.25.25h12.5a.25.25 0 00.25-.25v-8.5a.25.25 0 00-.25-.25zM3.5 6.25a.75.75 0 01.75-.75h7.5a.75.75 0 010 1.5h-7.5a.75.75 0 01-.75-.75zm.75 2.25a.75.75 0 000 1.5h4a.75.75 0 000-1.5z"/></svg>'
  };
  return icons[name] || "";
}
function _removeToolbar() {
  if (_activeToolbar) {
    _activeToolbar.remove();
    _activeToolbar = null;
  }
}
function _showToast(msg) {
  const t = document.createElement("div");
  t.className = "tst-toast";
  t.textContent = msg;
  document.body.appendChild(t);
  setTimeout(() => t.remove(), 1800);
}
function _getDocContextFromElement(el2) {
  const panel = el2.closest("#doc-reader-panel");
  if (panel) {
    return {
      docName: panel.dataset.docName || "Document",
      docId: panel.dataset.docId || ""
    };
  }
  const envelope = el2.closest(".assistant-envelope");
  if (envelope) {
    const sourceDoc = envelope.querySelector(".source-doc");
    if (sourceDoc)
      return { docName: sourceDoc.textContent || "Document", docId: "" };
  }
  return { docName: "Document", docId: "" };
}
function initTextSelectionToolbar() {
  document.addEventListener("mouseup", () => {
    setTimeout(() => {
      _removeToolbar();
      const sel = window.getSelection();
      const text = (sel?.toString() || "").trim();
      if (!text || text.length < 3)
        return;
      const anchor = sel.anchorNode;
      if (!anchor)
        return;
      const container = anchor.nodeType === 3 ? anchor.parentElement : anchor;
      if (!container)
        return;
      if (!container.closest(".envelope-detail-body") && !container.closest("#doc-reader-panel .doc-reader-content"))
        return;
      const range = sel.getRangeAt(0);
      const rect = range.getBoundingClientRect();
      const ctx = _getDocContextFromElement(container);
      const page = _getPageFromElement(container);
      const toolbar = document.createElement("div");
      toolbar.className = "text-selection-toolbar";
      toolbar.style.top = window.scrollY + rect.top - 42 + "px";
      toolbar.style.left = window.scrollX + rect.left + rect.width / 2 - 100 + "px";
      const copyBtn = document.createElement("button");
      copyBtn.innerHTML = _svgIcon("copy") + " Copy";
      copyBtn.addEventListener("click", (ev) => {
        ev.stopPropagation();
        navigator.clipboard.writeText(text).then(() => _showToast("Copied to clipboard"));
        _removeToolbar();
      });
      toolbar.appendChild(copyBtn);
      const d1 = document.createElement("span");
      d1.className = "tst-divider";
      toolbar.appendChild(d1);
      const bmBtn = document.createElement("button");
      bmBtn.innerHTML = _svgIcon("bookmark") + " Bookmark";
      bmBtn.addEventListener("click", (ev) => {
        ev.stopPropagation();
        const bm = JSON.parse(localStorage.getItem(_BOOKMARKS_KEY) || "[]");
        bm.unshift({ text: text.slice(0, 500), documentName: ctx.docName, documentId: ctx.docId, page, timestamp: (/* @__PURE__ */ new Date()).toISOString() });
        if (bm.length > 50)
          bm.length = 50;
        localStorage.setItem(_BOOKMARKS_KEY, JSON.stringify(bm));
        _showToast("Bookmarked");
        _removeToolbar();
        const p = document.getElementById("doc-reader-panel");
        if (p)
          _updateBookmarksBadge(p);
      });
      toolbar.appendChild(bmBtn);
      const d2 = document.createElement("span");
      d2.className = "tst-divider";
      toolbar.appendChild(d2);
      const citeBtn = document.createElement("button");
      citeBtn.innerHTML = _svgIcon("cite") + " Cite";
      citeBtn.addEventListener("click", (ev) => {
        ev.stopPropagation();
        const citation = "\u201C" + text.slice(0, 300) + "\u201D \u2014 " + ctx.docName;
        navigator.clipboard.writeText(citation).then(() => _showToast("Citation copied"));
        _removeToolbar();
      });
      toolbar.appendChild(citeBtn);
      document.body.appendChild(toolbar);
      _activeToolbar = toolbar;
    }, 10);
  });
  document.addEventListener("mousedown", (e) => {
    if (_activeToolbar && !_activeToolbar.contains(e.target))
      _removeToolbar();
  });
}
if (typeof document !== "undefined") {
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initTextSelectionToolbar);
  } else {
    initTextSelectionToolbar();
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
      if (docId) {
        const readerLink = document.createElement("a");
        readerLink.href = "#";
        readerLink.className = "source-open-doc-link";
        readerLink.textContent = "Open document";
        readerLink.addEventListener("click", (e) => {
          e.preventDefault();
          e.stopPropagation();
          openDocReaderPanel(docId, s.page_number, (s.cite_text ?? s.snippet ?? "").slice(0, 100));
        });
        actions.appendChild(readerLink);
      }
      if (ragUrl) {
        const link = document.createElement("a");
        link.href = ragUrl;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        link.className = "source-open-doc-link";
        link.textContent = "Open in RAG \u2197";
        link.style.opacity = "0.6";
        link.style.fontSize = "11px";
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
    } else if (t === "task_list") {
      let parseDetail2 = function(raw) {
        if (!raw)
          return null;
        try {
          const d = JSON.parse(raw);
          const rec = d.recommendation || "";
          const issues = (d.issues || []).map((x) => String(x));
          const warns = (d.warnings || []).map((x) => String(x));
          const lines = [...issues, ...warns].filter(Boolean).slice(0, 6);
          return { summary: rec || lines[0] || raw.slice(0, 120), lines };
        } catch {
          return { summary: raw.slice(0, 200), lines: [] };
        }
      }, fmtModule2 = function(s) {
        return MOD_LABEL[s] || s.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
      };
      var parseDetail = parseDetail2, fmtModule = fmtModule2;
      const b = block;
      const SEV_LABEL = { critical: "Critical", warning: "Warning", info: "Info", low: "Low", none: "None" };
      const SEV_ORDER = { critical: 0, warning: 1, info: 2, low: 3, none: 4 };
      const MOD_LABEL = {
        roster_open: "Roster",
        roster_recon: "Reconciliation",
        credentialing: "Credentialing",
        manual: "Manual"
      };
      const tasks = (b.tasks || []).slice().sort(
        (a, b2) => (SEV_ORDER[a.severity] ?? 3) - (SEV_ORDER[b2.severity] ?? 3)
      );
      const wrap = document.createElement("div");
      wrap.className = "tm-envelope-wrap";
      const hdr = document.createElement("div");
      hdr.className = "tm-env-header";
      const hdrLeft = document.createElement("div");
      hdrLeft.className = "tm-env-header-left";
      const hdrTitle = document.createElement("span");
      hdrTitle.className = "tm-env-title";
      hdrTitle.textContent = "Tasks";
      hdrLeft.appendChild(hdrTitle);
      const sevCounts = {};
      for (const tk of tasks)
        sevCounts[tk.severity || "low"] = (sevCounts[tk.severity || "low"] || 0) + 1;
      for (const sev of ["critical", "warning", "info", "low"]) {
        if (!sevCounts[sev])
          continue;
        const chip = document.createElement("span");
        chip.className = `tm-env-sev-chip tm-env-sev-chip--${sev}`;
        chip.textContent = `${sevCounts[sev]} ${SEV_LABEL[sev]}`;
        hdrLeft.appendChild(chip);
      }
      hdr.appendChild(hdrLeft);
      const hdrRight = document.createElement("div");
      hdrRight.className = "tm-env-header-right";
      hdrRight.textContent = `${tasks.length} task${tasks.length !== 1 ? "s" : ""}`;
      hdr.appendChild(hdrRight);
      wrap.appendChild(hdr);
      const activeFilters = Object.entries(b.filters || {}).filter(([, v]) => v != null && v !== "").map(([k, v]) => `${k}: ${v}`);
      if (activeFilters.length) {
        const strip = document.createElement("div");
        strip.className = "tm-env-filter-strip";
        strip.textContent = `Filtered by: ${activeFilters.join(" \xB7 ")}`;
        wrap.appendChild(strip);
      }
      if (tasks.length === 0) {
        const empty = document.createElement("div");
        empty.className = "tm-env-empty";
        empty.textContent = "No tasks found.";
        wrap.appendChild(empty);
      } else {
        const list = document.createElement("div");
        list.className = "tm-env-list";
        for (const task of tasks) {
          const sev = task.severity || "low";
          const status = task.status || "open";
          const card = document.createElement("div");
          card.className = `tm-env-card tm-env-sev-${sev} tm-env-status-${status}`;
          card.setAttribute("data-task-id", task.task_id);
          const accent = document.createElement("div");
          accent.className = `tm-env-accent tm-env-accent--${sev}`;
          card.appendChild(accent);
          const inner = document.createElement("div");
          inner.className = "tm-env-card-inner";
          const topRow = document.createElement("div");
          topRow.className = "tm-env-top-row";
          const sevBadge = document.createElement("span");
          sevBadge.className = `tm-env-badge tm-env-badge--${sev}`;
          sevBadge.textContent = SEV_LABEL[sev] || sev;
          topRow.appendChild(sevBadge);
          if (task.source_module) {
            const modTag = document.createElement("span");
            modTag.className = "tm-env-mod-tag";
            modTag.textContent = fmtModule2(task.source_module);
            topRow.appendChild(modTag);
          }
          if (task.dim) {
            const dimTag = document.createElement("span");
            dimTag.className = "tm-env-dim-tag";
            dimTag.textContent = task.dim.replace(/_/g, " ");
            topRow.appendChild(dimTag);
          }
          const spacer = document.createElement("span");
          spacer.style.flex = "1";
          topRow.appendChild(spacer);
          const statusDot = document.createElement("span");
          statusDot.className = `tm-env-status-dot tm-env-status-dot--${status}`;
          statusDot.title = status === "in_progress" ? "In Progress" : status.charAt(0).toUpperCase() + status.slice(1);
          topRow.appendChild(statusDot);
          inner.appendChild(topRow);
          const title = document.createElement("div");
          title.className = "tm-env-card-title";
          title.textContent = task.text || "(no title)";
          inner.appendChild(title);
          if (task.provider_name || task.npi) {
            const provRow = document.createElement("div");
            provRow.className = "tm-env-prov-row";
            if (task.provider_name) {
              const icon = document.createElement("span");
              icon.className = "tm-env-prov-icon";
              icon.textContent = "person";
              provRow.appendChild(icon);
              const nameSpan = document.createElement("span");
              nameSpan.textContent = task.provider_name;
              provRow.appendChild(nameSpan);
            }
            if (task.npi) {
              const npiSpan = document.createElement("span");
              npiSpan.className = "tm-env-npi";
              npiSpan.textContent = `NPI ${task.npi}`;
              provRow.appendChild(npiSpan);
            }
            if (task.assignee) {
              const aSpan = document.createElement("span");
              aSpan.className = "tm-env-assignee";
              aSpan.textContent = `\u2192 ${task.assignee}`;
              provRow.appendChild(aSpan);
            }
            inner.appendChild(provRow);
          }
          const parsed = parseDetail2(task.detail);
          if (parsed) {
            const det = document.createElement("details");
            det.className = "tm-env-detail";
            const sum = document.createElement("summary");
            sum.className = "tm-env-detail-summary";
            const summaryText = parsed.summary.length > 100 ? parsed.summary.slice(0, 100) + "\u2026" : parsed.summary;
            sum.textContent = summaryText || "Detail";
            det.appendChild(sum);
            const detBody = document.createElement("div");
            detBody.className = "tm-env-detail-body";
            if (parsed.lines.length) {
              const ul = document.createElement("ul");
              ul.className = "tm-env-detail-list";
              for (const line of parsed.lines) {
                const li = document.createElement("li");
                li.textContent = line;
                ul.appendChild(li);
              }
              detBody.appendChild(ul);
              if (parsed.summary && parsed.lines.length) {
                const rec = document.createElement("p");
                rec.className = "tm-env-detail-rec";
                rec.textContent = parsed.summary;
                detBody.appendChild(rec);
              }
            } else {
              detBody.textContent = parsed.summary;
            }
            det.appendChild(detBody);
            inner.appendChild(det);
          }
          card.appendChild(inner);
          if (b.allow_resolve !== false && (status === "open" || status === "in_progress")) {
            const actions = document.createElement("div");
            actions.className = "tm-env-card-actions";
            const statusIcon = document.createElement("span");
            const resolveBtn = document.createElement("button");
            resolveBtn.type = "button";
            resolveBtn.className = "tm-env-btn tm-env-btn--resolve";
            resolveBtn.textContent = "Resolve";
            resolveBtn.addEventListener("click", async (e) => {
              e.stopPropagation();
              resolveBtn.disabled = true;
              resolveBtn.textContent = "\u2026";
              try {
                await fetch(`/chat/tasks/${task.task_id}/resolve`, {
                  method: "POST",
                  headers: { "Content-Type": "application/json" },
                  body: JSON.stringify({ resolved_by: "chat" })
                });
                card.classList.remove("tm-env-status-open", "tm-env-status-in_progress");
                card.classList.add("tm-env-status-resolved");
                statusDot.className = "tm-env-status-dot tm-env-status-dot--resolved";
                resolveBtn.remove();
              } catch {
                resolveBtn.disabled = false;
                resolveBtn.textContent = "Resolve";
              }
            });
            actions.appendChild(resolveBtn);
            card.appendChild(actions);
          }
          list.appendChild(card);
        }
        wrap.appendChild(list);
      }
      const footer = document.createElement("div");
      footer.className = "tm-env-footer";
      const countNote = document.createElement("span");
      countNote.className = "tm-env-footer-note";
      countNote.textContent = tasks.length >= 50 ? `Showing first 50 \xB7 more may exist` : `${tasks.length} task${tasks.length !== 1 ? "s" : ""} total`;
      footer.appendChild(countNote);
      const exportLink = document.createElement("a");
      exportLink.href = "/chat/tasks/export";
      exportLink.className = "tm-env-view-all";
      exportLink.target = "_blank";
      exportLink.rel = "noopener";
      exportLink.textContent = "\u2193 Export CSV";
      footer.appendChild(exportLink);
      wrap.appendChild(footer);
      bubble.appendChild(wrap);
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
      const items = normalizeFollowupLineList(b.items || [], false);
      if (items.length) {
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
        hint.textContent = followupListHintLines(items);
        w.appendChild(hint);
        for (const line of items.slice(0, 8)) {
          const text = line.text.trim();
          if (!text)
            continue;
          if (line.clickable && opts.onFollowupClick) {
            const btn = document.createElement("button");
            btn.type = "button";
            btn.className = "envelope-step-chip";
            btn.textContent = text;
            btn.addEventListener("click", () => opts.onFollowupClick(text));
            w.appendChild(btn);
          } else {
            const row = document.createElement("div");
            row.className = "envelope-step-line envelope-step-line--static";
            row.textContent = text;
            w.appendChild(row);
          }
        }
        disclosure.appendChild(w);
        bubble.appendChild(disclosure);
      }
    } else if (t === "suggested_questions") {
      const b = block;
      const items = normalizeFollowupLineList(b.items || [], true);
      if (items.length) {
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
        hint.textContent = followupListHintLines(items);
        w.appendChild(hint);
        const chips = document.createElement("div");
        chips.className = "envelope-suggested-chips";
        for (const line of items.slice(0, 6)) {
          const text = line.text.trim();
          if (!text)
            continue;
          if (line.clickable && opts.onFollowupClick) {
            const btn = document.createElement("button");
            btn.type = "button";
            btn.className = "envelope-suggested-chip";
            btn.textContent = text;
            btn.setAttribute("aria-label", "Send: " + text);
            btn.addEventListener("click", () => opts.onFollowupClick(text));
            chips.appendChild(btn);
          } else {
            const row = document.createElement("div");
            row.className = "envelope-suggested-line envelope-suggested-line--static";
            row.textContent = text;
            chips.appendChild(row);
          }
        }
        w.appendChild(chips);
        disclosure.appendChild(w);
        bubble.appendChild(disclosure);
      }
    } else if (t === "pipeline_human_gate") {
      const b = block;
      const g = b.gate;
      if (g && typeof g.run_id === "string" && g.run_id.length > 0) {
        const tid = (g.thread_id || opts.threadId || "").trim() || null;
        bubble.appendChild(renderCredentialingCopilotPanel(g, tid));
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
    const pipelineWrap = document.getElementById("rosterReceiptPipelineWrap");
    const pipelineSummaryEl = document.getElementById("rosterReceiptPipelineSummary");
    const pipelineListEl = document.getElementById("rosterReceiptPipeline");
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
      const isRAG = data.file_purpose === "instant_rag" || data.verification_tier === "instant";
      headline.textContent = isRAG ? "Document ready" : "Upload complete";
      sub.textContent = isRAG ? "Your document is ready to search in this chat." : "Your file was saved to this chat.";
      checksEl.replaceChildren();
      const li = document.createElement("li");
      const t = document.createElement("span");
      t.className = "roster-receipt__check-title";
      t.textContent = "Summary";
      const d = document.createElement("span");
      d.className = "roster-receipt__check-detail";
      d.textContent = isRAG ? `${data.filename ?? "File"} \u2014 ready to search. Kept for 7 days.` : `${data.filename ?? "File"} \u2014 ${data.row_count ?? 0} row(s) for ${data.org_name ?? ""}. Billing NPI ${data.default_billing_npi || data.org_id || "\u2014"}.`;
      li.appendChild(t);
      li.appendChild(d);
      checksEl.appendChild(li);
      alertsEl.replaceChildren();
      alertsEl.setAttribute("hidden", "");
      nextEl.textContent = isRAG ? "Ask a question about this document \u2014 it's ready now." : "Press Send to run reconciliation, or wait if you turned on automatic send after upload.";
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
    const _isRAG = data.file_purpose === "instant_rag" || data.verification_tier === "instant";
    addMeta("File", (data.filename ?? "").trim());
    if (_isRAG) {
      addMeta("Status", "Ready to search");
      console.debug("[upload-receipt] instant-rag meta:", {
        chunks_count: data.chunks_count ?? data.row_count ?? 0,
        verification_tier: data.verification_tier ?? "instant",
        envelope_id: data.envelope_id,
        document_id: data.document_id
      });
    } else {
      if (data.row_count_cleansed != null)
        addMeta("Rows after cleanup", String(data.row_count_cleansed));
      if (data.row_count_resolved != null)
        addMeta("Rows checked in NPI registry", String(data.row_count_resolved));
      addMeta("Billing NPI", (data.default_billing_npi || data.org_id || "").trim());
      addMeta("Matched organization (registry)", (data.matched_organization_name ?? "").trim());
      if ((data.matched_practice_address ?? "").trim())
        addMeta("Practice address on file", (data.matched_practice_address ?? "").trim());
      addMeta("Process status", (data.process_status ?? "").trim());
    }
    addMeta("Upload ID", (data.upload_id ?? "").trim());
    addMeta("Chat thread ID", (data.thread_id ?? "").trim());
    const rs = data.resolution_summary;
    if (rs && typeof rs === "object") {
      const parts = Object.entries(rs).filter(([, v]) => typeof v === "number" && v > 0).map(([k, v]) => `${k}: ${v}`);
      if (parts.length)
        addMeta("NPI match breakdown", parts.join(", "));
    }
    const pipe = data.pipeline_progress;
    const stages = pipe?.stages;
    if (pipelineWrap && pipelineSummaryEl && pipelineListEl && Array.isArray(stages) && stages.length > 0) {
      pipelineWrap.removeAttribute("hidden");
      pipelineSummaryEl.textContent = (pipe.summary ?? "").trim() || "Pipeline status";
      pipelineListEl.replaceChildren();
      const cur = (pipe.current_stage_id ?? "").trim();
      for (const s of stages) {
        const li = document.createElement("li");
        const isDone = Boolean(s.done);
        li.className = isDone ? "roster-receipt__pipeline--done" : "roster-receipt__pipeline--pending";
        if (!isDone && cur && s.id === cur) {
          li.classList.add("roster-receipt__pipeline--current");
        }
        const lab = document.createElement("span");
        lab.className = "roster-receipt__pipeline-stage";
        lab.textContent = s.label || s.id;
        const det = document.createElement("span");
        det.className = "roster-receipt__pipeline-detail";
        det.textContent = s.detail || "";
        li.appendChild(lab);
        li.appendChild(det);
        pipelineListEl.appendChild(li);
      }
    } else {
      pipelineWrap?.setAttribute("hidden", "");
      pipelineSummaryEl?.replaceChildren();
      pipelineListEl?.replaceChildren();
    }
    const rcWrap = document.getElementById("rosterReceiptReconciliationWrap");
    const rcLink = document.getElementById("rosterReceiptReconciliationLink");
    const rcUrlData = data.reconciliation_ui_url;
    if (rcWrap && rcLink && rcUrlData) {
      rcLink.href = rcUrlData;
      rcWrap.removeAttribute("hidden");
    } else {
      rcWrap?.setAttribute("hidden", "");
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
  initModelProfilePicker();
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
            data.thinking_log.forEach((entry) => {
              const line = thinkingLineFromEntry(entry);
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
  let credentialingPendingMessage = null;
  let credentialingReopenMessage = null;
  function hideCredentialingEnvelope() {
    credentialingPendingMessage = null;
    document.getElementById("credentialingModal")?.setAttribute("hidden", "");
    document.getElementById("credentialingOverlay")?.classList.remove("open");
  }
  function normalizeRosterFreshness(raw) {
    const s = typeof raw === "string" ? raw.trim().toLowerCase() : "";
    if (s === "fresh" || s === "stale" || s === "none")
      return s;
    return "none";
  }
  function formatRosterUploadInstant(iso) {
    if (!iso || typeof iso !== "string")
      return "";
    try {
      const d = new Date(iso.trim().replace(/Z$/, "+00:00"));
      if (Number.isNaN(d.getTime()))
        return "";
      return d.toLocaleString(void 0, { dateStyle: "medium", timeStyle: "short" });
    } catch {
      return "";
    }
  }
  function rosterLatestRowPresent(row) {
    return !!(row && (row.upload_id || "").trim() && (row.org_id || "").trim());
  }
  function messageForRosterThreadSignal(freshness, latest, thresholdDays) {
    const org = (latest?.org_name || "").trim();
    const fn = (latest?.filename || "").trim();
    const when = formatRosterUploadInstant(latest?.uploaded_at ?? void 0);
    const th = thresholdDays > 0 ? thresholdDays : 14;
    if (freshness === "none") {
      return "No roster on this chat yet \u2014 upload one to compare your file against external data, or continue with outside-in Medicaid NPI.";
    }
    if (freshness === "fresh") {
      const parts = ["Recent roster on this chat"];
      if (when)
        parts.push(`(${when})`);
      if (org)
        parts.push(`\u2014 ${org}`);
      parts.push("\u2014 you can run reconciliation without uploading again.");
      return parts.join(" ");
    }
    if (!when) {
      return `A roster is linked${org ? ` (${org})` : ""}` + (fn ? ` \u2014 ${fn}` : "") + ", but the upload date is missing \u2014 re-upload if the file may be outdated.";
    }
    return `Last roster upload ${when}${org ? ` \xB7 ${org}` : ""} \u2014 older than ${th} days. You can still use it or upload a newer file.`;
  }
  function setRosterThreadSignalBanner(root, variant, text) {
    if (!root)
      return;
    root.classList.remove(
      "roster-thread-signal--fresh",
      "roster-thread-signal--stale",
      "roster-thread-signal--none",
      "roster-thread-signal--muted"
    );
    root.classList.add(`roster-thread-signal--${variant}`);
    const p = root.querySelector(".roster-thread-signal__text");
    if (p)
      p.textContent = text;
    root.removeAttribute("hidden");
  }
  function refreshCredentialingRosterUi() {
    const panel = document.getElementById("credentialingRosterPanel");
    const signalEl = document.getElementById("credentialingRosterSignal");
    const titleEl = document.getElementById("credentialingRosterTitle");
    const listEl = document.getElementById("credentialingRosterList");
    const hintEl = document.getElementById("credentialingRosterHint");
    const outsideWrap = document.getElementById("credentialingPreferOutsideInWrap");
    const outsideCb = document.getElementById("credentialingPreferOutsideIn");
    const freshWrap = document.getElementById("credentialingPreferFreshWrap");
    const freshCb = document.getElementById("credentialingPreferFresh");
    const orgEl = document.getElementById("credentialingOrgName");
    if (!panel || !titleEl || !listEl || !hintEl || !outsideWrap || !outsideCb || !freshWrap || !freshCb)
      return;
    const orgHint = (orgEl?.value ?? "").trim();
    const tid = (currentThreadId || "").trim();
    if (!tid) {
      panel.removeAttribute("hidden");
      setRosterThreadSignalBanner(
        signalEl,
        "muted",
        "No chat thread yet \u2014 send a message first so roster uploads can attach here. Until then we treat this as outside-in Medicaid NPI only."
      );
      titleEl.textContent = "Roster files on this chat";
      listEl.innerHTML = "";
      listEl.setAttribute("hidden", "");
      hintEl.textContent = "No thread yet \u2014 send once so uploads attach to this chat. Without a roster file we run the outside-in Medicaid NPI pipeline.";
      hintEl.hidden = false;
      outsideWrap.setAttribute("hidden", "");
      freshWrap.removeAttribute("hidden");
      return;
    }
    fetch(API_BASE + "/chat/thread/" + encodeURIComponent(tid) + "/uploads").then(
      (r) => r.json()
    ).then((data) => {
      let rows = Array.isArray(data.roster_reconciliation_files) ? [...data.roster_reconciliation_files] : [];
      const hasTop = !!(data.reconciliation_upload_id && data.reconciliation_org_id);
      const files = Array.isArray(data.uploaded_files) ? data.uploaded_files : [];
      const hasFile = files.some(
        (u) => (u.purpose || "").trim() === "roster_reconciliation" && !!(u.upload_id || "").trim() && !!(u.org_id || "").trim()
      );
      const hasRoster = rows.length > 0 || hasTop || hasFile;
      if (rows.length === 0 && hasTop) {
        const rn = (data.reconciliation_org_name || "").trim();
        const rup = (data.reconciliation_upload_id || "").trim();
        const rid = (data.reconciliation_org_id || "").trim();
        if (rup && rn) {
          rows = [{ upload_id: rup, org_id: rid, org_name: rn, filename: "", purpose: "roster_reconciliation" }];
        }
      }
      const th = typeof data.roster_fresh_days_threshold === "number" && data.roster_fresh_days_threshold > 0 ? data.roster_fresh_days_threshold : 14;
      let latestRow = data.latest_roster_reconciliation && rosterLatestRowPresent(data.latest_roster_reconciliation) ? data.latest_roster_reconciliation : null;
      if (!latestRow && rows.length > 0 && rosterLatestRowPresent(rows[0])) {
        latestRow = rows[0];
      }
      const apiFresh = normalizeRosterFreshness(data.roster_freshness);
      const effectiveFresh = hasRoster && latestRow ? apiFresh : "none";
      setRosterThreadSignalBanner(
        signalEl,
        effectiveFresh,
        messageForRosterThreadSignal(effectiveFresh, latestRow, th)
      );
      const recName = (data.reconciliation_org_name || "").trim();
      let classification = "no_files";
      if (!hasRoster) {
        classification = "no_files";
      } else if (!orgHint) {
        classification = "ambiguous";
      } else {
        let matches = 0;
        for (const u of rows) {
          if (orgHintMatchesUploadOrg(orgHint, u.org_name || ""))
            matches += 1;
        }
        if (recName && orgHintMatchesUploadOrg(orgHint, recName))
          matches += 1;
        classification = matches >= 1 ? "matched" : "ambiguous";
      }
      panel.removeAttribute("hidden");
      listEl.innerHTML = "";
      if (rows.length > 0) {
        listEl.removeAttribute("hidden");
        for (const u of rows) {
          const li = document.createElement("li");
          const fn = (u.filename || "").trim() || "upload";
          const on = (u.org_name || "").trim() || "\u2014";
          const match = orgHint ? orgHintMatchesUploadOrg(orgHint, on) : false;
          if (match)
            li.classList.add("credentialing-roster-list__match");
          li.textContent = `${fn} \u2014 ${on}`;
          listEl.appendChild(li);
        }
      } else {
        listEl.setAttribute("hidden", "");
      }
      if (classification === "no_files") {
        titleEl.textContent = "No roster file on this chat";
        hintEl.textContent = "We will run the outside-in Medicaid NPI pipeline. Upload a roster below if you want reconciliation (your file vs external data), or use \u22EF \u2192 Upload file.";
      } else if (classification === "matched") {
        titleEl.textContent = "Roster files linked to this chat";
        hintEl.textContent = "Matching rows are highlighted. Default run is roster reconciliation unless you check \u201COutside-in Medicaid NPI only\u201D below.";
      } else {
        titleEl.textContent = "Roster files on this chat";
        hintEl.textContent = "No upload row matches the organization name above (or it is empty). Upload a roster or run with the server\u2019s latest reconciliation upload \u2014 we will pick the latest when appropriate.";
      }
      hintEl.hidden = false;
      if (hasRoster) {
        outsideWrap.removeAttribute("hidden");
      } else {
        outsideWrap.setAttribute("hidden", "");
      }
      const outsideInPath = !hasRoster || outsideCb.checked;
      if (outsideInPath) {
        freshWrap.removeAttribute("hidden");
      } else {
        freshWrap.setAttribute("hidden", "");
        freshCb.checked = false;
      }
    }).catch(() => {
      panel.removeAttribute("hidden");
      setRosterThreadSignalBanner(
        signalEl,
        "muted",
        "Could not load roster status from the server \u2014 reconciliation vs outside-in still follows thread state when you run."
      );
      titleEl.textContent = "Roster status";
      listEl.innerHTML = "";
      listEl.setAttribute("hidden", "");
      hintEl.textContent = "Could not load upload status; the server still chooses reconciliation vs outside-in from thread state.";
      hintEl.hidden = false;
      outsideWrap.setAttribute("hidden", "");
      freshWrap.setAttribute("hidden", "");
      freshCb.checked = false;
    });
  }
  function openCredentialingEnvelope(message) {
    credentialingPendingMessage = message;
    const orgEl = document.getElementById("credentialingOrgName");
    const modal2 = document.getElementById("credentialingModal");
    const overlay = document.getElementById("credentialingOverlay");
    if (!orgEl || !modal2 || !overlay) {
      sendMessage(message, { skipCredentialingEnvelope: true });
      return;
    }
    const hint = extractCredentialingOrgHint(message);
    orgEl.value = hint;
    const ap = document.querySelector('input[name="credentialingMode"][value="autopilot"]');
    if (ap)
      ap.checked = true;
    const fr = document.getElementById("credentialingForceRefresh");
    if (fr)
      fr.checked = false;
    const po = document.getElementById("credentialingPreferOutsideIn");
    if (po)
      po.checked = false;
    const pf = document.getElementById("credentialingPreferFresh");
    if (pf)
      pf.checked = false;
    refreshCredentialingRosterUi();
    modal2.removeAttribute("hidden");
    overlay.classList.add("open");
    orgEl.focus();
  }
  function sendMessage(overrideMessage, opts) {
    let message = (overrideMessage ?? (inputEl.value ?? "").trim()).trim();
    if (overrideMessage !== void 0 && overrideMessage !== null) {
      activeClarificationDraft = null;
    } else if (activeClarificationDraft?.length) {
      const preface = buildWorkflowSelectionPreface();
      if (preface && message) {
        message = `${preface}

${message}`;
      } else if (preface && !message) {
        message = preface;
      }
    }
    if (!message)
      return;
    if (sendBtn.disabled)
      return;
    activeClarificationDraft = null;
    if (!opts?.credentialing_options && !opts?.skipCredentialingEnvelope && isCredentialingReportIntent(message)) {
      openCredentialingEnvelope(message);
      return;
    }
    if (chatEmpty)
      chatEmpty.classList.add("hidden");
    const modeSelect = document.getElementById("composerMode");
    const selectedMode = modeSelect?.value || localStorage.getItem("_mobiusChatMode") || "copilot";
    messagesEl.querySelectorAll(".thinking-block").forEach((block) => {
      block.classList.add("collapsed");
      const p = block.querySelector(".thinking-preview");
      if (p)
        p.setAttribute("aria-expanded", "false");
    });
    const turnWrap = document.createElement("div");
    turnWrap.className = "chat-turn";
    turnWrap.appendChild(renderUserMessage(message, selectedMode));
    messagesEl.appendChild(turnWrap);
    scrollToBottom(messagesEl);
    if (!overrideMessage)
      inputEl.value = "";
    updateSendState();
    sendBtn.disabled = true;
    inputEl.disabled = true;
    const thinkingLines = [];
    const {
      el: thinkingBlockEl,
      addLine: addThinkingLine,
      done: thinkingDone,
      onRequestCorrelationId,
      onRequestStreamChunk,
      markRequestFailed
    } = renderThinkingBlock(["Sending request\u2026"]);
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
      onRequestStreamChunk(text);
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
    if (opts?.credentialing_options) {
      payload.credentialing_options = opts.credentialing_options;
    }
    payload.chat_mode = selectedMode;
    if (opts?.use_react !== void 0) {
      payload.use_react = opts.use_react;
    }
    let activeCorrelationId = "";
    fetch(API_BASE + "/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    }).then((r) => r.json()).then((data) => {
      if (data.thread_id)
        currentThreadId = data.thread_id;
      activeCorrelationId = data.correlation_id ?? "";
      if ((data.correlation_id || "").trim()) {
        onRequestCorrelationId();
      }
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
      (data.thinking_log ?? []).forEach((entry) => {
        const line = thinkingLineFromEntry(entry);
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
      let nextQuestions = normalizeFollowupLineList(
        data.next_questions_for_user,
        true
      );
      if (nextQuestions.length === 0 && data.user_ask && String(data.user_ask).trim()) {
        nextQuestions = [{ text: String(data.user_ask).trim(), clickable: true }];
      }
      if (nextQuestions.length === 0) {
        const card = tryParseAnswerCard(body || "");
        if (card?.followups?.length) {
          nextQuestions = card.followups.map((f) => (f.question || f.reason || f.field || "").trim()).filter(Boolean).map((text) => ({ text, clickable: true }));
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
      const envelopeHasPipelineGate = useEnvelope && envBlocks.some((b) => b.type === "pipeline_human_gate");
      if (useEnvelope) {
        turnWrap.appendChild(
          renderAssistantFromEnvelope(envCandidate, {
            onFollowupClick: (q) => sendMessage(q),
            sourceConfidenceStrip: (data.source_confidence_strip ?? "").trim() || void 0,
            showConfidenceBadge: data.status !== "clarification" && data.status !== "refinement_ask",
            qcAudit: qcFromPayload,
            correlationId: cidForTurn || null,
            suppressConfidenceForAdminQcFail: suppressConf,
            threadId: data.thread_id ?? currentThreadId ?? null
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
      const credCop = data.credentialing_copilot;
      if (!envelopeHasPipelineGate && credCop && typeof credCop === "object" && typeof credCop.run_id === "string" && credCop.run_id.length > 0) {
        turnWrap.appendChild(renderCredentialingCopilotPanel(credCop, data.thread_id ?? currentThreadId));
      }
      const pdfBase64 = data.roster_report_pdf_base64;
      const reportMarkdown = data.roster_report_final_md;
      const attachmentsKind = data.roster_report_attachments_kind === "reconciliation" ? "reconciliation" : data.roster_report_attachments_kind === "credentialing" ? "credentialing" : void 0;
      if (pdfBase64 && typeof pdfBase64 === "string" && pdfBase64.length > 0 || reportMarkdown && typeof reportMarkdown === "string" && reportMarkdown.trim().length > 0) {
        turnWrap.appendChild(renderRosterReportDownload(pdfBase64, reportMarkdown, attachmentsKind));
      }
      const isCard = !!tryParseAnswerCard(body || "");
      if (nextQuestions.length > 0 && !isCard && !useEnvelope) {
        turnWrap.appendChild(
          renderNextQuestions(nextQuestions, (q) => sendMessage(q))
        );
      }
      if (data.clarification_options && data.clarification_options.length > 0) {
        turnWrap.appendChild(renderClarificationOptions(data.clarification_options));
      } else {
        activeClarificationDraft = null;
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
      markRequestFailed();
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
  let composerStagedFile = null;
  const composerAttachBtn = document.getElementById("composerAttach");
  const composerAttachmentInput = document.getElementById("composerAttachmentInput");
  const composerAttachmentChip = document.getElementById("composerAttachmentChip");
  const composerAttachmentChipName = document.getElementById("composerAttachmentChipName");
  const composerAttachmentChipRemove = document.getElementById("composerAttachmentChipRemove");
  function showComposerAttachment(file) {
    composerStagedFile = file;
    if (composerAttachmentChipName)
      composerAttachmentChipName.textContent = file.name;
    if (composerAttachmentChip)
      composerAttachmentChip.hidden = false;
    if (composerAttachBtn)
      composerAttachBtn.setAttribute("aria-pressed", "true");
  }
  function clearComposerAttachment() {
    composerStagedFile = null;
    if (composerAttachmentChip) {
      composerAttachmentChip.hidden = true;
      composerAttachmentChip.classList.remove("is-uploading");
    }
    if (composerAttachmentInput)
      composerAttachmentInput.value = "";
    if (composerAttachBtn)
      composerAttachBtn.removeAttribute("aria-pressed");
  }
  composerAttachBtn?.addEventListener("click", () => {
    composerAttachmentInput?.click();
  });
  composerAttachmentInput?.addEventListener("change", (e) => {
    const f = e.target.files?.[0];
    if (!f) {
      clearComposerAttachment();
      return;
    }
    const maxBytes = 25 * 1024 * 1024;
    if (f.size > maxBytes) {
      alert(
        `File too large (${Math.round(f.size / 1024 / 1024)} MB). Limit is 25 MB for inline attach. Use the \u22EF \u2192 Upload file modal for larger files.`
      );
      clearComposerAttachment();
      return;
    }
    showComposerAttachment(f);
    inputEl?.focus();
  });
  composerAttachmentChipRemove?.addEventListener("click", () => clearComposerAttachment());
  const composerWrap = document.querySelector(".composer-wrap");
  if (composerWrap) {
    const stop = (e) => {
      e.preventDefault();
      e.stopPropagation();
    };
    ["dragenter", "dragover"].forEach(
      (evt) => composerWrap.addEventListener(evt, (e) => {
        stop(e);
        composerWrap.classList.add("composer-wrap--dragover");
      })
    );
    ["dragleave", "drop"].forEach(
      (evt) => composerWrap.addEventListener(evt, (e) => {
        stop(e);
        composerWrap.classList.remove("composer-wrap--dragover");
      })
    );
    composerWrap.addEventListener("drop", (e) => {
      const f = e.dataTransfer?.files?.[0];
      if (!f)
        return;
      if (composerAttachmentInput) {
        const dt = new DataTransfer();
        dt.items.add(f);
        composerAttachmentInput.files = dt.files;
        composerAttachmentInput.dispatchEvent(new Event("change"));
      }
    });
  }
  const LARGE_FILE_THRESHOLD_BYTES = 500 * 1024;
  function estimatePageCount(file) {
    const bytesPerPage = 4 * 1024;
    return Math.max(1, Math.round(file.size / bytesPerPage));
  }
  function showLargeUploadConfirm(file) {
    return new Promise((resolve) => {
      const overlay = document.getElementById("largeUploadOverlay");
      const modal2 = document.getElementById("largeUploadModal");
      const bodyEl = document.getElementById("largeUploadModalBody");
      const proceedInstant = document.getElementById("largeUploadProceedInstant");
      const proceedBatch = document.getElementById("largeUploadProceedBatch");
      const cancelBtn = document.getElementById("largeUploadCancel");
      if (!modal2 || !overlay || !proceedInstant || !cancelBtn) {
        resolve("instant");
        return;
      }
      const sizeMb = (file.size / (1024 * 1024)).toFixed(1);
      const pages = estimatePageCount(file);
      if (bodyEl) {
        bodyEl.innerHTML = `"<strong>${file.name}</strong>" is <strong>${sizeMb} MB</strong> (roughly <strong>${pages} pages</strong>). "Upload now" gets it ready to search in this chat \u2014 typically <strong>30 to 60 seconds</strong> for a document this size.<br><br>"Queue for batch processing" adds the doc to your permanent library so it's searchable from any chat. Coming soon.`;
      }
      const cleanup = () => {
        modal2.setAttribute("hidden", "");
        overlay.classList.remove("open");
        proceedInstant.removeEventListener("click", onInstant);
        proceedBatch?.removeEventListener("click", onBatch);
        cancelBtn.removeEventListener("click", onCancel);
        overlay.removeEventListener("click", onCancel);
        document.removeEventListener("keydown", onKey);
      };
      const onInstant = () => {
        cleanup();
        resolve("instant");
      };
      const onBatch = () => {
        cleanup();
        resolve("batch");
      };
      const onCancel = () => {
        cleanup();
        resolve("cancel");
      };
      const onKey = (e) => {
        if (e.key === "Escape")
          onCancel();
        if (e.key === "Enter")
          onInstant();
      };
      proceedInstant.addEventListener("click", onInstant);
      proceedBatch?.addEventListener("click", onBatch);
      cancelBtn.addEventListener("click", onCancel);
      overlay.addEventListener("click", onCancel);
      document.addEventListener("keydown", onKey);
      modal2.removeAttribute("hidden");
      overlay.classList.add("open");
      proceedInstant.focus();
    });
  }
  let composerUploadPhaseTimers = [];
  function stopComposerUploadPhaseEmits() {
    composerUploadPhaseTimers.forEach((id) => window.clearTimeout(id));
    composerUploadPhaseTimers = [];
  }
  function startComposerUploadPhaseEmits(filename) {
    stopComposerUploadPhaseEmits();
    const phases = [
      { ms: 0, text: `\u23F3 Uploading "${filename}"\u2026` },
      { ms: 4e3, text: `\u23F3 Reading "${filename}"\u2026` },
      { ms: 15e3, text: `\u23F3 Getting "${filename}" ready to search\u2026` },
      { ms: 4e4, text: `\u23F3 Still working on "${filename}" \u2014 larger docs take a bit longer\u2026` },
      { ms: 75e3, text: `\u23F3 Almost done with "${filename}"\u2026` }
    ];
    phases.forEach(({ ms, text }) => {
      const id = window.setTimeout(() => showChatStatusBanner(text, 0), ms);
      composerUploadPhaseTimers.push(id);
    });
  }
  async function uploadStagedAttachmentForInstantRag() {
    if (!composerStagedFile)
      return null;
    const filename = composerStagedFile.name;
    composerAttachmentChip?.classList.add("is-uploading");
    startComposerUploadPhaseEmits(filename);
    try {
      const formData = new FormData();
      formData.append("file", composerStagedFile);
      formData.append("org_name", "instant-rag");
      formData.append("file_purpose", "instant_rag");
      if (currentThreadId)
        formData.append("thread_id", currentThreadId);
      const resp = await fetch(API_BASE + "/chat/roster-upload", {
        method: "POST",
        body: formData
      });
      if (!resp.ok) {
        const detail = await resp.json().catch(() => null);
        throw new Error(detail?.detail || `Upload failed (${resp.status})`);
      }
      const data = await resp.json();
      if (data.thread_id)
        currentThreadId = data.thread_id;
      const chunks = typeof data.chunks_count === "number" ? data.chunks_count : 0;
      if (chunks > 0) {
        console.debug(`[composer-attach] "${filename}" ingested as ${chunks} chunk${chunks === 1 ? "" : "s"}`);
      }
      showChatStatusBanner(`\u2713 "${filename}" is ready \u2014 searching now\u2026`, 4e3);
      return data;
    } finally {
      stopComposerUploadPhaseEmits();
      composerAttachmentChip?.classList.remove("is-uploading");
    }
  }
  async function sendMessageWithAttachment() {
    if (!composerStagedFile) {
      sendMessage();
      return;
    }
    if (composerStagedFile.size > LARGE_FILE_THRESHOLD_BYTES) {
      const choice = await showLargeUploadConfirm(composerStagedFile);
      if (choice === "cancel") {
        return;
      }
      if (choice === "batch") {
        showChatStatusBanner(
          `Batch processing isn't available yet. Use "Upload now" to search "${composerStagedFile.name}" in this chat right now.`,
          15e3
        );
        return;
      }
    }
    sendBtn.disabled = true;
    inputEl.disabled = true;
    try {
      const uploadedName = composerStagedFile.name;
      await uploadStagedAttachmentForInstantRag();
      clearComposerAttachment();
      const typed = (inputEl.value ?? "").trim();
      const effective = typed || `I just uploaded "${uploadedName}" \u2014 what does it say?`;
      if (!typed)
        inputEl.value = effective;
      sendBtn.disabled = false;
      inputEl.disabled = false;
      sendMessage();
    } catch (err) {
      console.error("[composer-attach] upload failed:", err);
      stopComposerUploadPhaseEmits();
      const msg = err?.message || String(err);
      showChatStatusBanner(`\u2717 Couldn't upload "${composerStagedFile?.name ?? "the document"}": ${msg}`, 2e4);
      alert(`Couldn't upload the document: ${msg}`);
      sendBtn.disabled = false;
      inputEl.disabled = false;
    }
  }
  sendBtn.addEventListener(
    "click",
    (e) => {
      if (!composerStagedFile)
        return;
      e.stopImmediatePropagation();
      e.preventDefault();
      void sendMessageWithAttachment();
    },
    { capture: true }
  );
  inputEl.addEventListener(
    "keydown",
    (e) => {
      if (e.key !== "Enter" || e.shiftKey)
        return;
      if (!composerStagedFile)
        return;
      e.stopImmediatePropagation();
      e.preventDefault();
      void sendMessageWithAttachment();
    },
    { capture: true }
  );
  const uploadRestoreBanner = document.getElementById("uploadRestoreBanner");
  const uploadRestoreBannerList = document.getElementById("uploadRestoreBannerList");
  const uploadRestoreBannerDismiss = document.getElementById("uploadRestoreBannerDismiss");
  const restoreInFlight = /* @__PURE__ */ new Set();
  function hideRestoreBanner() {
    if (uploadRestoreBanner)
      uploadRestoreBanner.hidden = true;
  }
  function userDismissedRestoreBanner() {
    try {
      return sessionStorage.getItem("_mobiusRestoreBannerDismissed") === "1";
    } catch {
      return false;
    }
  }
  uploadRestoreBannerDismiss?.addEventListener("click", () => {
    hideRestoreBanner();
    try {
      sessionStorage.setItem("_mobiusRestoreBannerDismissed", "1");
    } catch {
    }
  });
  async function linkUploadToCurrentThread(documentId, filename, button) {
    if (!currentThreadId) {
      currentThreadId = crypto.randomUUID();
    }
    if (restoreInFlight.has(documentId))
      return;
    restoreInFlight.add(documentId);
    const originalText = button.textContent || "Attach";
    button.disabled = true;
    button.textContent = "Attaching\u2026";
    try {
      const resp = await fetch(
        API_BASE + "/chat/uploads/" + encodeURIComponent(documentId) + "/link-to-thread",
        {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ thread_id: currentThreadId })
        }
      );
      if (!resp.ok) {
        const detail = await resp.json().catch(() => null);
        throw new Error(detail?.detail || `Attach failed (${resp.status})`);
      }
      await resp.json();
      button.textContent = "Attached \u2713";
      showChatStatusBanner(`\u2713 "${filename}" attached to this chat \u2014 ask away.`, 5e3);
      setTimeout(() => {
        const row = button.closest(".upload-restore-banner__row");
        row?.remove();
        if (uploadRestoreBannerList && uploadRestoreBannerList.children.length === 0) {
          hideRestoreBanner();
        }
      }, 600);
    } catch (err) {
      console.error("[restore-banner] link failed:", err);
      showChatStatusBanner(`\u2717 Couldn't attach "${filename}": ${err?.message || err}`, 1e4);
      button.disabled = false;
      button.textContent = originalText;
    } finally {
      restoreInFlight.delete(documentId);
    }
  }
  async function maybeShowRestoreBanner() {
    if (!uploadRestoreBanner || !uploadRestoreBannerList)
      return;
    if (userDismissedRestoreBanner())
      return;
    if (currentThreadId) {
      try {
        const r = await fetch(
          API_BASE + "/chat/thread/" + encodeURIComponent(currentThreadId) + "/uploads"
        );
        if (r.ok) {
          const body = await r.json().catch(() => ({}));
          const md = String(body?.markdown || body?.result || body || "");
          if (/instant[-_ ]?rag|\.pdf\b|\.docx\b/i.test(md)) {
            hideRestoreBanner();
            return;
          }
        }
      } catch {
      }
    }
    let uploads = [];
    try {
      const params = new URLSearchParams({ limit: "5" });
      if (currentThreadId)
        params.set("current_thread_id", currentThreadId);
      const r = await fetch(API_BASE + "/chat/uploads/recent/for-restoration?" + params.toString());
      if (!r.ok)
        return;
      const body = await r.json();
      uploads = body?.uploads || [];
    } catch {
      return;
    }
    if (!uploads.length) {
      hideRestoreBanner();
      return;
    }
    uploadRestoreBannerList.replaceChildren();
    for (const u of uploads) {
      const row = document.createElement("div");
      row.className = "upload-restore-banner__row";
      const name = document.createElement("span");
      name.className = "upload-restore-banner__filename";
      name.textContent = String(u.filename || "upload");
      name.title = String(u.filename || "");
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "upload-restore-banner__attach";
      btn.textContent = "Attach to this chat";
      btn.addEventListener("click", () => {
        void linkUploadToCurrentThread(
          String(u.document_id || ""),
          String(u.filename || "upload"),
          btn
        );
      });
      row.appendChild(name);
      row.appendChild(btn);
      uploadRestoreBannerList.appendChild(row);
    }
    uploadRestoreBanner.hidden = false;
  }
  void maybeShowRestoreBanner();
  function openUploadModal() {
    hideRosterUploadReceipt();
    const modal2 = document.getElementById("uploadModal");
    const overlay = document.getElementById("uploadOverlay");
    const form = document.getElementById("uploadForm");
    const st = document.getElementById("uploadStatus");
    const progressWrap = document.getElementById("uploadProgressWrap");
    const uploadSig = document.getElementById("uploadRosterThreadSignal");
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
    const utid = (currentThreadId || "").trim();
    if (!utid) {
      setRosterThreadSignalBanner(
        uploadSig,
        "muted",
        "Send a message first so this upload attaches to a chat thread."
      );
    } else {
      setRosterThreadSignalBanner(uploadSig, "muted", "Checking roster on this chat\u2026");
      fetch(API_BASE + "/chat/thread/" + encodeURIComponent(utid) + "/uploads").then(
        (r) => r.json()
      ).then((data) => {
        const th = typeof data.roster_fresh_days_threshold === "number" && data.roster_fresh_days_threshold > 0 ? data.roster_fresh_days_threshold : 14;
        let latest = data.latest_roster_reconciliation && rosterLatestRowPresent(data.latest_roster_reconciliation) ? data.latest_roster_reconciliation : null;
        const rows = Array.isArray(data.roster_reconciliation_files) ? data.roster_reconciliation_files : [];
        if (!latest && rows.length > 0 && rosterLatestRowPresent(rows[0])) {
          latest = rows[0];
        }
        const apiF = normalizeRosterFreshness(data.roster_freshness);
        const effective = rosterLatestRowPresent(latest) ? apiF : "none";
        setRosterThreadSignalBanner(
          uploadSig,
          effective,
          messageForRosterThreadSignal(effective, latest, th)
        );
      }).catch(() => {
        setRosterThreadSignalBanner(
          uploadSig,
          "muted",
          "Could not check for an existing roster \u2014 you can still upload a file."
        );
      });
    }
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
  function setupCredentialingEnvelope() {
    const form = document.getElementById("credentialingForm");
    const credOverlay = document.getElementById("credentialingOverlay");
    const cancel = document.getElementById("credentialingCancel");
    const defaultsBtn = document.getElementById("credentialingDefaults");
    form?.addEventListener("submit", (e) => {
      e.preventDefault();
      const pending = credentialingPendingMessage;
      if (!pending)
        return;
      const org = document.getElementById("credentialingOrgName")?.value?.trim();
      if (!org)
        return;
      const modeEl = document.querySelector('input[name="credentialingMode"]:checked');
      const mode = modeEl?.value === "copilot" ? "copilot" : "autopilot";
      const forceRefresh = !!document.getElementById("credentialingForceRefresh")?.checked;
      const preferOutside = !!document.getElementById("credentialingPreferOutsideIn")?.checked;
      const preferFresh = !!document.getElementById("credentialingPreferFresh")?.checked;
      const freshHidden = document.getElementById("credentialingPreferFreshWrap")?.hasAttribute("hidden");
      hideCredentialingEnvelope();
      const credOpts = {
        org_name: org,
        mode,
        force_refresh: forceRefresh
      };
      if (preferOutside)
        credOpts.prefer_outside_in = true;
      if (preferFresh && !freshHidden)
        credOpts.prefer_fresh_report = true;
      sendMessage(pending, {
        credentialing_options: credOpts,
        use_react: true
      });
    });
    cancel?.addEventListener("click", () => hideCredentialingEnvelope());
    credOverlay?.addEventListener("click", () => hideCredentialingEnvelope());
    defaultsBtn?.addEventListener("click", () => {
      const ap = document.querySelector('input[name="credentialingMode"][value="autopilot"]');
      if (ap)
        ap.checked = true;
      const fr = document.getElementById("credentialingForceRefresh");
      if (fr)
        fr.checked = false;
      const po = document.getElementById("credentialingPreferOutsideIn");
      if (po)
        po.checked = false;
      const pf = document.getElementById("credentialingPreferFresh");
      if (pf)
        pf.checked = false;
      refreshCredentialingRosterUi();
    });
    const orgNameField = document.getElementById("credentialingOrgName");
    orgNameField?.addEventListener("input", () => refreshCredentialingRosterUi());
    document.getElementById("credentialingPreferOutsideIn")?.addEventListener("change", () => refreshCredentialingRosterUi());
    document.getElementById("credentialingUploadRoster")?.addEventListener("click", () => {
      const pending = credentialingPendingMessage;
      credentialingReopenMessage = pending;
      const orgEl = document.getElementById("credentialingOrgName");
      const uploadOrg = document.getElementById("uploadOrgName");
      if (uploadOrg && orgEl)
        uploadOrg.value = orgEl.value.trim();
      const auto = document.getElementById("uploadAutoSendReconciliation");
      if (auto)
        auto.checked = false;
      hideCredentialingEnvelope();
      openUploadModal();
    });
  }
  setupCredentialingEnvelope();
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
    const rosterFields = document.getElementById("uploadFieldRoster");
    uploadFilePurpose?.addEventListener("change", () => {
      const isRoster = uploadFilePurpose.value === "roster_reconciliation";
      if (rosterFields)
        rosterFields.hidden = !isRoster;
      if (uploadOrgName)
        uploadOrgName.required = isRoster;
      updateSubmitState();
    });
    function startUploadPhaseEmits(purpose) {
      stopUploadPhaseEmits();
      const roster = purpose === "roster_reconciliation";
      const phases = roster ? [
        { ms: 0, text: "Step 1 of 3 \u2014 Looking up your organization (NPPES / PML)\u2026" },
        { ms: 2800, text: "Step 2 of 3 \u2014 Sending file to the roster service\u2026" },
        { ms: 7e3, text: "Step 3 of 3 \u2014 Parsing rows and resolving NPIs (often 30s\u20132 min)\u2026" },
        { ms: 45e3, text: "Still working \u2014 large rosters can take a bit longer\u2026" }
      ] : [
        // 2026-04-18 copy revision (user flagged "publishing to RAG"
        // as jargon). Same user-friendly arc as the composer-attach
        // flow — one narrative, not four technical stages.
        { ms: 0, text: "Uploading\u2026" },
        { ms: 4e3, text: "Reading your document\u2026" },
        { ms: 15e3, text: "Getting it ready to search\u2026" },
        { ms: 4e4, text: "Still working \u2014 larger docs take a bit longer\u2026" },
        { ms: 75e3, text: "Almost done\u2026" }
      ];
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
      const isRoster = (uploadFilePurpose?.value || "roster_reconciliation") === "roster_reconciliation";
      const hasOrg = !!uploadOrgName?.value?.trim();
      if (uploadSubmit)
        uploadSubmit.disabled = !(hasFile && (hasOrg || !isRoster));
    }
    uploadOrgName?.addEventListener("input", updateSubmitState);
    uploadFile?.addEventListener("change", updateSubmitState);
    uploadForm?.addEventListener("submit", (e) => {
      e.preventDefault();
      const orgName = uploadOrgName?.value?.trim() || "";
      const file = uploadFile?.files?.[0];
      const purpose = (uploadFilePurpose?.value || "roster_reconciliation").trim();
      const isRoster = purpose === "roster_reconciliation";
      if (!file || isRoster && !orgName)
        return;
      uploadSubmit?.setAttribute("disabled", "");
      uploadModal?.classList.add("upload-modal--busy");
      uploadForm?.setAttribute("aria-busy", "true");
      uploadProgressWrap?.removeAttribute("hidden");
      startUploadPhaseEmits(purpose);
      const formData = new FormData();
      formData.append("file", file);
      formData.append("org_name", orgName || "instant-rag");
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
          const uploadPurpose = purpose;
          uploadForm?.reset();
          updateSubmitState();
          if (uploadPurpose === "instant_rag") {
            const fname = data.filename ?? file?.name ?? "document";
            inputEl.value = `I just uploaded "${fname}" \u2014 what does it say about eligibility and coverage?`;
          } else {
            inputEl.value = `Run reconciliation report for ${org}`;
          }
          updateSendState();
          hideUploadModal();
          if (rosterFields)
            rosterFields.hidden = false;
          if (uploadOrgName)
            uploadOrgName.required = true;
          if (uploadPurpose === "instant_rag") {
            return;
          }
          const reopen = credentialingReopenMessage;
          if (reopen) {
            credentialingReopenMessage = null;
            window.setTimeout(() => {
              openCredentialingEnvelope(reopen);
            }, 0);
            return;
          }
          const auto = document.getElementById("uploadAutoSendReconciliation");
          if (uploadPurpose === "roster_reconciliation" && auto?.checked) {
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
      // Phase 2.3: sidebar now shows deduplicated *threads* with real titles
      // instead of per-turn rows that exposed raw URLs / tool inputs. Endpoint
      // returns {thread_id, title, updated_at, turn_count}. Gracefully returns
      // [] if migration 030 hasn't run, so the list is empty rather than broken.
      fetch(API_BASE + "/chat/history/threads?limit=20").then(
        (r) => r.json()
      ),
      helpfulList ? fetch(API_BASE + "/chat/history/most-helpful-searches?limit=10").then(
        (r) => r.json()
      ) : Promise.resolve([]),
      documentsList ? fetch(API_BASE + "/chat/history/most-helpful-documents?limit=10").then(
        (r) => r.json()
      ) : Promise.resolve([])
    ]).then(([recentThreads, helpful, documents]) => {
      recentList.innerHTML = "";
      for (const th of recentThreads) {
        const li = document.createElement("li");
        li.className = "recent-item";
        const label = th.title || "Untitled chat";
        const countSuffix = th.turn_count > 1 ? `  (${th.turn_count})` : "";
        li.textContent = snippet(label) + countSuffix;
        li.title = label;
        li.setAttribute("role", "button");
        li.setAttribute("tabindex", "0");
        li.setAttribute("data-thread-id", th.thread_id);
        li.addEventListener("click", () => {
          inputEl.value = label;
          updateSendState();
        });
        li.addEventListener("keydown", (e) => {
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            inputEl.value = label;
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
  (function setupSkillsModal() {
    const overlay = document.getElementById("skillsOverlay");
    const modal2 = document.getElementById("skillsModal");
    function openSkillsModal() {
      overlay?.removeAttribute("hidden");
      modal2?.removeAttribute("hidden");
    }
    function closeSkillsModal() {
      overlay?.setAttribute("hidden", "");
      modal2?.setAttribute("hidden", "");
    }
    document.getElementById("btnOpenSkillPipeline")?.addEventListener("click", () => {
      closeSkillsModal();
      window.open("http://localhost:3999/credentialing-home.html", "_blank", "noopener");
    });
    document.getElementById("btnOpenFinancialStrategy")?.addEventListener("click", () => {
      closeSkillsModal();
      window.open("http://localhost:8099/financial-strategy", "_blank", "noopener");
    });
    document.getElementById("skillsModalClose")?.addEventListener("click", closeSkillsModal);
    overlay?.addEventListener("click", closeSkillsModal);
    document.getElementById("skillPipelineOpen")?.addEventListener("click", () => {
      closeSkillsModal();
      window.open("http://localhost:3999/credentialing-home.html", "_blank", "noopener");
    });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && !modal2?.hasAttribute("hidden"))
        closeSkillsModal();
    });
    function openRosterPage() {
      closeSkillsModal();
      const base = window.API_BASE || window.location.origin;
      const lastOrg = localStorage.getItem("lastOrg") || "";
      const url = base + "/roster" + (lastOrg ? "?org=" + encodeURIComponent(lastOrg) : "");
      window.open(url, "_blank", "noopener");
    }
    document.getElementById("btnOpenRoster")?.addEventListener("click", openRosterPage);
    document.getElementById("skillRosterOpen")?.addEventListener("click", openRosterPage);
  })();
  _initLandingDashboard();
}
run();
var _ldAllRuns = [];
function _initLandingDashboard() {
  function _openPipeline() {
    window.open("http://localhost:3999/credentialing-home.html", "_blank", "noopener");
  }
  function _openRoster() {
    const base = window.API_BASE || window.location.origin;
    const lastOrg = localStorage.getItem("lastOrg") || "";
    window.open(base + "/roster" + (lastOrg ? "?org=" + encodeURIComponent(lastOrg) : ""), "_blank", "noopener");
  }
  document.getElementById("ldNewRunBtn")?.addEventListener("click", _openPipeline);
  document.getElementById("ldStartRunBtn")?.addEventListener("click", _openPipeline);
  document.getElementById("ldSetupBtn")?.addEventListener("click", _openPipeline);
  document.getElementById("ldOrgSelect")?.addEventListener("change", function() {
    const org = this.value;
    if (!org)
      return;
    localStorage.setItem("lastOrg", org);
    _ldOnOrgSelected(org, window.API_BASE || window.location.origin);
  });
  document.getElementById("ldRosterOpenBtn")?.addEventListener("click", _openRoster);
  _ldBootstrap(window.API_BASE || window.location.origin);
}
async function _ldBootstrap(base) {
  const sel = document.getElementById("ldOrgSelect");
  try {
    const r = await fetch(`${base}/chat/credentialing-runs?limit=50`);
    if (r.ok)
      _ldAllRuns = await r.json();
  } catch {
    _ldAllRuns = [];
  }
  const seen = /* @__PURE__ */ new Set(), orgs = [];
  for (const run2 of _ldAllRuns) {
    const o = (run2.org_name || "").trim();
    if (o && !seen.has(o)) {
      seen.add(o);
      orgs.push(o);
    }
  }
  if (sel) {
    sel.innerHTML = orgs.length ? orgs.map((o) => `<option value="${_ldEsc(o)}">${_ldEsc(o)}</option>`).join("") : '<option value="">No orgs yet \u2014 start a run</option>';
    const last = localStorage.getItem("lastOrg") || "";
    if (last && orgs.includes(last))
      sel.value = last;
  }
  const activeOrg = sel?.value || orgs[0] || "";
  if (activeOrg) {
    if (activeOrg !== localStorage.getItem("lastOrg"))
      localStorage.setItem("lastOrg", activeOrg);
    _ldOnOrgSelected(activeOrg, base);
  } else {
    _ldRenderRunList([], base);
    _ldRosterNoData("Start your first credentialing run to populate.");
  }
}
function _ldOnOrgSelected(org, base) {
  const link = document.getElementById("ldRosterLink");
  if (link)
    link.href = `${base}/roster?org=${encodeURIComponent(org)}`;
  const orgRuns = _ldAllRuns.filter((r) => (r.org_name || "").trim() === org);
  _ldRenderRunList(orgRuns, base);
  _ldRenderOrgSteps(orgRuns);
  _ldFetchRosterStats(org, base);
}
function _ldRenderOrgSteps(orgRuns) {
  const vo = orgRuns[0]?.validated_outputs || {};
  const steps = [
    { chipId: "ldStep1Chip", valId: "ldStep1Val", key: "identify_org" },
    { chipId: "ldStep2Chip", valId: "ldStep2Val", key: "find_locations" }
  ];
  for (const s of steps) {
    const done = !!vo[s.key];
    const chip = document.getElementById(s.chipId);
    const val = document.getElementById(s.valId);
    if (chip)
      chip.className = "ld-step-chip " + (done ? "ld-step-chip--done" : "ld-step-chip--idle");
    if (val) {
      if (s.key === "identify_org") {
        const npi = typeof vo.identify_org === "object" && vo.identify_org?.npi ? vo.identify_org.npi : "";
        val.textContent = done ? npi || "\u2713" : "\u2014";
      } else {
        const d = typeof vo.find_locations === "object" ? vo.find_locations : {};
        const n = d.row_count ?? d.location_count ?? null;
        val.textContent = done ? n != null ? n + " loc" : "\u2713" : "\u2014";
      }
    }
  }
}
function _ldRenderRunList(runs, base) {
  const listEl = document.getElementById("ldRunList");
  if (!listEl)
    return;
  if (!runs.length) {
    listEl.innerHTML = '<div class="ld-empty-note">No runs for this org yet.</div>';
    return;
  }
  const STEP_META = [
    { id: "nppes_alignment", short: "NPPES", num: 3 },
    { id: "pml_alignment", short: "PML", num: 4 },
    { id: "find_associated_providers", short: "Compliance", num: 5 },
    { id: "taxonomy_optimization", short: "Taxonomy", num: 6 }
  ];
  listEl.innerHTML = runs.slice(0, 8).map((run2) => {
    const phase = run2.phase || "pending";
    const vo = run2.validated_outputs || {};
    const badgeCls = phase === "complete" ? "ld-cap-badge--complete" : phase === "error" || phase === "failed" ? "ld-cap-badge--error" : phase === "running" || phase === "in_progress" ? "ld-cap-badge--running" : "ld-cap-badge--pending";
    const badgeLbl = phase === "complete" ? "\u2713 Complete" : phase === "error" || phase === "failed" ? "\u2717 Error" : phase === "running" ? "\u25CF Running" : phase === "in_progress" ? "\u2192 In progress" : "Pending";
    const capCls = phase === "complete" ? "ld-run-capsule--complete" : phase === "error" || phase === "failed" ? "ld-run-capsule--error" : "ld-run-capsule--active";
    const mode = run2.mode === "autopilot" ? "autopilot" : run2.mode === "copilot" ? "co-pilot" : run2.mode || "";
    const dt = run2.updated_at ? new Date(run2.updated_at).toLocaleDateString("en-US", { month: "short", day: "numeric" }) : "";
    const pills = STEP_META.map(
      (s) => `<span class="ld-step-pill${vo[s.id] ? " ld-step-pill--done" : ""}" title="Step ${s.num}: ${s.short}">${s.short}</span>`
    ).join("");
    const runUrl = `${base}/pipeline?run_id=${encodeURIComponent(run2.run_id)}`;
    return `<a class="ld-run-capsule ${capCls}" href="${runUrl}" target="_blank" rel="noopener">
      <div class="ld-cap-head">
        <div class="ld-cap-date">${dt}${mode ? " \xB7 " + _ldEsc(mode) : ""}</div>
        <span class="ld-cap-badge ${badgeCls}">${badgeLbl}</span>
      </div>
      <div class="ld-cap-steps-row">${pills}</div>
    </a>`;
  }).join("");
}
async function _ldFetchRosterStats(org, base) {
  ["ldStatTotal", "ldStatBillable", "ldStatAtRisk", "ldStatBlocked", "ldStatTasks"].forEach((id) => {
    const el2 = document.getElementById(id);
    if (el2)
      el2.textContent = "\u2026";
  });
  try {
    const r = await fetch(`${base}/chat/roster-truth/${encodeURIComponent(org)}?limit=500`);
    if (!r.ok)
      throw new Error(String(r.status));
    const data = await r.json();
    _ldRenderRosterStats(Array.isArray(data) ? data : data.providers || data.items || []);
  } catch {
    _ldRosterNoData("Could not load roster.");
  }
}
function _ldRenderRosterStats(providers) {
  const total = providers.length;
  const tasks = providers.filter((p) => {
    const t = p.open_tasks;
    return Array.isArray(t) ? t.length > 0 : false;
  }).length;
  let billable = 0, atRisk = 0, blocked = 0;
  for (const p of providers) {
    const snap = typeof p.nppes_snapshot === "object" && p.nppes_snapshot ? p.nppes_snapshot : {};
    const nppesOk = (snap.nppes_status || "").toUpperCase() === "A";
    const openCnt = Array.isArray(p.open_tasks) ? p.open_tasks.length : 0;
    const valid = p.decision === "validated";
    if (valid && nppesOk && openCnt === 0)
      billable++;
    else if (valid)
      atRisk++;
    else
      blocked++;
  }
  if (billable + atRisk + blocked === 0 && total > 0) {
    billable = providers.filter((p) => p.decision === "validated").length;
    atRisk = providers.filter((p) => p.decision === "flagged" || p.decision === "review").length;
    blocked = total - billable - atRisk;
  }
  const ids = { ldStatTotal: total, ldStatBillable: billable, ldStatAtRisk: atRisk, ldStatBlocked: blocked, ldStatTasks: tasks };
  Object.entries(ids).forEach(([id, v]) => {
    const el2 = document.getElementById(id);
    if (el2)
      _ldCountUp(el2, v);
  });
  if (total > 0) {
    const bw = document.getElementById("ldBarWrap");
    if (bw) {
      bw.style.display = "";
      setTimeout(() => {
        const g = document.getElementById("ldBarGreen"), a = document.getElementById("ldBarAmber"), rd = document.getElementById("ldBarRed");
        if (g)
          g.style.width = (billable / total * 100).toFixed(1) + "%";
        if (a)
          a.style.width = (atRisk / total * 100).toFixed(1) + "%";
        if (rd)
          rd.style.width = (blocked / total * 100).toFixed(1) + "%";
      }, 30);
      const leg = document.getElementById("ldBarLegend");
      if (leg)
        leg.textContent = `${Math.round(billable / total * 100)}% billable \xB7 ${atRisk} at risk \xB7 ${blocked} blocked`;
    }
  }
  const issueEl = document.getElementById("ldIssueList");
  if (issueEl) {
    const chips = [];
    if (blocked > 0)
      chips.push({ cls: "ld-issue-chip--crit", icon: "\u2717", text: `${blocked} provider${blocked > 1 ? "s" : ""} blocked from billing` });
    if (atRisk > 0)
      chips.push({ cls: "ld-issue-chip--warn", icon: "\u26A0", text: `${atRisk} provider${atRisk > 1 ? "s" : ""} at risk \u2014 gaps exist` });
    if (tasks > 0)
      chips.push({ cls: "ld-issue-chip--warn", icon: "\u25CE", text: `${tasks} open credentialing task${tasks > 1 ? "s" : ""}` });
    if (!chips.length && total > 0)
      chips.push({ cls: "ld-issue-chip--ok", icon: "\u2713", text: "All providers clean \u2014 no gaps detected" });
    if (!total)
      chips.push({ cls: "ld-issue-chip", icon: "\xB7", text: "No providers in roster yet" });
    issueEl.innerHTML = chips.map((c) => `<div class="ld-issue-chip ${c.cls}"><span>${c.icon}</span><span>${c.text}</span></div>`).join("");
  }
  const lr = document.getElementById("ldLastRun");
  if (lr)
    lr.textContent = `${total} provider${total !== 1 ? "s" : ""} on record`;
}
function _ldRosterNoData(msg) {
  ["ldStatTotal", "ldStatBillable", "ldStatAtRisk", "ldStatBlocked", "ldStatTasks"].forEach((id) => {
    const el2 = document.getElementById(id);
    if (el2)
      el2.textContent = "\u2014";
  });
  const issueEl = document.getElementById("ldIssueList");
  if (issueEl)
    issueEl.innerHTML = `<div class="ld-issue-chip">${_ldEsc(msg)}</div>`;
}
function _ldCountUp(el2, target) {
  el2.textContent = "0";
  if (!target) {
    el2.textContent = "0";
    return;
  }
  const steps = 18, dur = 500;
  let cur = 0;
  const iv = setInterval(() => {
    cur = Math.min(cur + Math.ceil(target / steps), target);
    el2.textContent = String(cur);
    if (cur >= target)
      clearInterval(iv);
  }, dur / steps);
}
function _ldEsc(str) {
  return String(str || "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}
