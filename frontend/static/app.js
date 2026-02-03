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
var STORAGE_KEYS2 = {
  accessToken: "mobius.auth.accessToken",
  refreshToken: "mobius.auth.refreshToken",
  expiresAt: "mobius.auth.expiresAt",
  userProfile: "mobius.auth.userProfile"
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
  const { auth: auth2, showOAuth = true, demoEmail, onSuccess, onClose } = options;
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
    auth2.getUserProfile().then((p) => {
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
        const result = await auth2.login(email, password);
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
        const result = await auth2.register(
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
        await auth2.logout();
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
var SECTION_INTENTS = ["process", "requirements", "definitions", "exceptions", "references"];
function isSectionIntent(s) {
  return typeof s === "string" && SECTION_INTENTS.includes(s);
}
var API_BASE = typeof window !== "undefined" && window.API_BASE && window.API_BASE.startsWith("http") ? window.API_BASE : "http://localhost:8000";
var apiBase = `${API_BASE}/api/v1`;
var auth = createAuthService({ apiBase, storage: localStorageAdapter });
function getAuthHeaders() {
  const token = localStorage.getItem(STORAGE_KEYS2.accessToken);
  if (token)
    return { Authorization: `Bearer ${token}` };
  return {};
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
        required_variables: Array.isArray(data.required_variables) ? data.required_variables : [],
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
    let start = raw.lastIndexOf("{", idx);
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
  if (mode === "FACTUAL") {
    return { visible: [], hidden: all };
  }
  if (mode === "CANONICAL") {
    return { visible: all, hidden: [] };
  }
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
function renderAnswerCard(card, isError) {
  const wrap = document.createElement("div");
  wrap.className = "message message--assistant answer-card answer-card--" + card.mode.toLowerCase() + (isError ? " message--error" : "");
  const bubble = document.createElement("div");
  bubble.className = "message-bubble answer-card-bubble";
  const direct = document.createElement("div");
  direct.className = "answer-card-direct";
  direct.textContent = card.direct_answer;
  bubble.appendChild(direct);
  const metaRow = document.createElement("div");
  metaRow.className = "answer-card-meta-row";
  if (card.required_variables && card.required_variables.length > 0) {
    const dep = document.createElement("span");
    dep.className = "answer-card-depends";
    dep.textContent = "Depends on: " + card.required_variables.join(", ");
    metaRow.appendChild(dep);
  }
  if (card.followups && card.followups.length > 0 && metaRow.childNodes.length > 0) {
    const sep = document.createElement("span");
    sep.className = "answer-card-meta-sep";
    sep.textContent = " \xB7 ";
    metaRow.appendChild(sep);
  }
  if (card.followups && card.followups.length > 0) {
    const confirmLabel = document.createElement("span");
    confirmLabel.className = "answer-card-confirm-label";
    confirmLabel.textContent = "Confirm";
    metaRow.appendChild(confirmLabel);
    card.followups.slice(0, 2).forEach((f) => {
      const chip = document.createElement("button");
      chip.type = "button";
      chip.className = "answer-card-followup-chip";
      chip.textContent = f.question || f.reason || f.field || "";
      chip.setAttribute("aria-label", chip.textContent);
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
  wrap.appendChild(bubble);
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
var PROGRESS_MAX_LINES = 3;
function renderProgressStack() {
  const block = document.createElement("div");
  block.className = "progress-stack";
  block.setAttribute("aria-live", "polite");
  block.setAttribute("aria-label", "Progress");
  const linesContainer = document.createElement("div");
  linesContainer.className = "progress-stack-lines";
  const lineEls = [];
  for (let i = 0; i < PROGRESS_MAX_LINES; i++) {
    const div = document.createElement("div");
    div.className = "progress-stack-line";
    div.textContent = "";
    lineEls.push(div);
    linesContainer.appendChild(div);
  }
  block.appendChild(linesContainer);
  const dotsEl = document.createElement("span");
  dotsEl.className = "progress-stack-dots";
  dotsEl.setAttribute("aria-hidden", "true");
  dotsEl.textContent = "...";
  const buffer = [];
  function addLine(line) {
    const trimmed = (line ?? "").trim();
    if (!trimmed)
      return;
    buffer.push(trimmed);
    const last3 = buffer.slice(-PROGRESS_MAX_LINES);
    for (let i = 0; i < PROGRESS_MAX_LINES; i++) {
      const text = last3[i] ?? "";
      lineEls[i].textContent = text;
      lineEls[i].classList.toggle("empty", !text);
    }
    dotsEl.remove();
    const lastIdx = last3.length - 1;
    if (lastIdx >= 0)
      lineEls[lastIdx].appendChild(dotsEl);
  }
  return {
    el: block,
    addLine
  };
}
function renderAssistantMessage(text, isError) {
  const wrap = document.createElement("div");
  wrap.className = "message message--assistant" + (isError ? " message--error" : "");
  const bubble = document.createElement("div");
  bubble.className = "message-bubble";
  bubble.textContent = normalizeMessageText(text);
  wrap.appendChild(bubble);
  return wrap;
}
function renderAssistantContent(body, isError) {
  const card = tryParseAnswerCard(body);
  if (typeof console !== "undefined" && console.log) {
    console.log("[AnswerCard] renderAssistantContent: card=", card ? "yes (mode=" + card.mode + ")" : "no");
  }
  if (card)
    return renderAnswerCard(card, isError);
  const trimmed = (body ?? "").trim();
  if (trimmed.startsWith("{") && trimmed.length > 10) {
    console.warn("[AnswerCard] Invalid JSON, showing fallback. Raw:", trimmed.slice(0, 500));
    return renderAssistantMessage("Answer could not be displayed. Please try again.", isError);
  }
  if (typeof console !== "undefined" && console.log) {
    console.log("[AnswerCard] rendering as prose (plain text)");
  }
  return renderAssistantMessage(body, isError);
}
var FEEDBACK_COMMENT_MAX_LENGTH = 500;
function svgIcon(className, paths) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("class", className);
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("fill", "none");
  svg.setAttribute("stroke", "currentColor");
  svg.setAttribute("stroke-width", "2");
  svg.setAttribute("stroke-linecap", "round");
  svg.setAttribute("stroke-linejoin", "round");
  svg.setAttribute("aria-hidden", "true");
  paths.forEach((d) => {
    const p = document.createElementNS("http://www.w3.org/2000/svg", "path");
    p.setAttribute("d", d);
    svg.appendChild(p);
  });
  return svg;
}
function thumbsUpIcon(className) {
  return svgIcon(className, [
    "M14 9V5a3 3 0 0 0-3-3l-4 9v11h11.28a2 2 0 0 0 2-1.7l1.38-9a2 2 0 0 0-2-2.3zM7 22H4a2 2 0 0 1-2-2v-7a2 2 0 0 1 2-2h3"
  ]);
}
function thumbsDownIcon(className) {
  return svgIcon(className, [
    "M10 15v4a3 3 0 0 0 3 3l4-9V2H5.72a2 2 0 0 0-2 1.7l-1.38 9a2 2 0 0 0 2 2.3zm7-13h2.67A2.31 2.31 0 0 1 22 4v7a2.31 2.31 0 0 1-2.33 2H17"
  ]);
}
function copyIcon(className) {
  return svgIcon(className, [
    "M16 4h4v4h-4V4z",
    "M20 10v10a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V10a2 2 0 0 1 2-2h12a2 2 0 0 1 2 2z",
    "M6 10v10h12V10H6z"
  ]);
}
function renderFeedback(correlationId, options) {
  const bar = document.createElement("div");
  bar.className = "feedback";
  const up = document.createElement("button");
  up.type = "button";
  up.setAttribute("aria-label", "Good response");
  up.appendChild(thumbsUpIcon("feedback-icon"));
  const down = document.createElement("button");
  down.type = "button";
  down.setAttribute("aria-label", "Bad response");
  down.appendChild(thumbsDownIcon("feedback-icon"));
  const copyBtn = document.createElement("button");
  copyBtn.type = "button";
  copyBtn.setAttribute("aria-label", "Copy");
  copyBtn.appendChild(copyIcon("feedback-icon"));
  copyBtn.addEventListener("click", () => {
    const msg = bar.closest(".chat-turn")?.querySelector(".message--assistant .message-bubble");
    if (msg?.textContent) {
      navigator.clipboard.writeText(msg.textContent).then(() => {
        const label = copyBtn.getAttribute("aria-label");
        copyBtn.setAttribute("aria-label", "Copied");
        const icon = copyBtn.querySelector(".feedback-icon");
        if (icon)
          copyBtn.removeChild(icon);
        const span = document.createElement("span");
        span.className = "feedback-copy-label";
        span.textContent = "Copied";
        copyBtn.appendChild(span);
        setTimeout(() => {
          copyBtn.removeChild(span);
          copyBtn.appendChild(copyIcon("feedback-icon"));
          if (label)
            copyBtn.setAttribute("aria-label", label);
        }, 1500);
      });
    }
  });
  const commentEl = document.createElement("div");
  commentEl.className = "feedback-comment";
  commentEl.style.display = "none";
  const commentForm = document.createElement("div");
  commentForm.className = "feedback-comment-form";
  commentForm.style.display = "none";
  const commentInput = document.createElement("textarea");
  commentInput.placeholder = "What went wrong? (optional)";
  commentInput.rows = 2;
  commentInput.maxLength = FEEDBACK_COMMENT_MAX_LENGTH;
  const btnRow = document.createElement("div");
  btnRow.className = "feedback-comment-buttons";
  const submitBtn = document.createElement("button");
  submitBtn.type = "button";
  submitBtn.textContent = "Submit";
  const cancelBtn = document.createElement("button");
  cancelBtn.type = "button";
  cancelBtn.textContent = "Cancel";
  btnRow.appendChild(submitBtn);
  btnRow.appendChild(cancelBtn);
  commentForm.appendChild(commentInput);
  commentForm.appendChild(btnRow);
  function setSelected(rating) {
    up.classList.toggle("selected", rating === "up");
    down.classList.toggle("selected", rating === "down");
  }
  function setCommentVisible(text) {
    commentEl.textContent = text;
    commentEl.style.display = text ? "block" : "none";
  }
  function disableThumbs() {
    up.disabled = true;
    down.disabled = true;
  }
  function updateFeedback(rating, comment) {
    setSelected(rating);
    setCommentVisible(comment);
    commentForm.style.display = "none";
    disableThumbs();
  }
  if (options?.initialRating) {
    setSelected(options.initialRating);
    if (options.initialComment)
      setCommentVisible(options.initialComment);
    disableThumbs();
  }
  up.addEventListener("click", () => {
    if (up.disabled)
      return;
    fetch(API_BASE + "/chat/feedback", {
      method: "POST",
      headers: { "Content-Type": "application/json", ...getAuthHeaders() },
      body: JSON.stringify({ correlation_id: correlationId, rating: "up", comment: null })
    }).then((r) => {
      if (r.ok)
        updateFeedback("up", "");
    }).catch(() => {
    });
  });
  down.addEventListener("click", () => {
    if (down.disabled)
      return;
    commentForm.style.display = "block";
    commentInput.value = "";
    commentInput.focus();
  });
  cancelBtn.addEventListener("click", () => {
    commentForm.style.display = "none";
  });
  commentInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      submitBtn.click();
    }
  });
  submitBtn.addEventListener("click", () => {
    const comment = commentInput.value.trim().slice(0, FEEDBACK_COMMENT_MAX_LENGTH);
    fetch(API_BASE + "/chat/feedback", {
      method: "POST",
      headers: { "Content-Type": "application/json", ...getAuthHeaders() },
      body: JSON.stringify({
        correlation_id: correlationId,
        rating: "down",
        comment: comment || null
      })
    }).then((r) => {
      if (r.ok)
        updateFeedback("down", comment);
    }).catch(() => {
    });
  });
  const leftGroup = document.createElement("div");
  leftGroup.className = "feedback-left";
  leftGroup.appendChild(up);
  leftGroup.appendChild(down);
  leftGroup.appendChild(commentEl);
  leftGroup.appendChild(commentForm);
  const actionsGroup = document.createElement("div");
  actionsGroup.className = "feedback-actions";
  actionsGroup.appendChild(copyBtn);
  bar.appendChild(leftGroup);
  bar.appendChild(actionsGroup);
  return { el: bar, updateFeedback };
}
function renderSourceCiter(sources, onSourceClick, correlationId, initialRatings) {
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
  sources.forEach((s) => {
    const item = document.createElement("div");
    item.className = "source-item" + (onSourceClick ? " source-item--clickable" : "");
    if (onSourceClick) {
      item.setAttribute("role", "button");
      item.setAttribute("tabindex", "0");
      item.title = "View document";
      item.addEventListener("click", (e) => {
        if (e.target.closest(".source-feedback-row") || e.target.closest(".source-open-doc"))
          return;
        onSourceClick(s);
      });
      item.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          if (!e.target.closest(".source-feedback-row"))
            onSourceClick(s);
        }
      });
    }
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
      if (s.match_score != null) {
        const matchNum = Number(s.match_score);
        const matchLabel = matchNum >= 0.8 ? "Strong match" : matchNum >= 0.5 ? "Moderate match" : "Weak match";
        const matchSpan = document.createElement("span");
        matchSpan.className = "source-meta-badge source-meta-badge--match";
        matchSpan.textContent = matchLabel;
        matchSpan.title = `Match: ${matchNum.toFixed(2)}`;
        parts.push(matchSpan);
      }
      if (s.confidence != null) {
        const confNum = Number(s.confidence);
        const confLabel = confNum >= 0.8 ? "High confidence" : confNum >= 0.5 ? "Medium confidence" : "Low confidence";
        const confSpan = document.createElement("span");
        confSpan.className = "source-meta-badge source-meta-badge--confidence";
        confSpan.textContent = confLabel;
        confSpan.title = `Confidence: ${confNum.toFixed(2)}`;
        parts.push(confSpan);
      }
      parts.forEach((p) => {
        if (typeof p === "string") {
          const t = document.createTextNode(p);
          metaLine.appendChild(t);
        } else {
          if (metaLine.childNodes.length)
            metaLine.appendChild(document.createTextNode(" \xB7 "));
          metaLine.appendChild(p);
        }
      });
      item.appendChild(metaLine);
    }
    if (s.snippet) {
      const meta = document.createElement("div");
      meta.className = "source-snippet";
      meta.textContent = s.snippet;
      item.appendChild(meta);
    }
    const sourceIndex = s.index >= 1 ? s.index : 1;
    const existingRating = initialRatings?.[sourceIndex];
    const feedbackRow = document.createElement("div");
    feedbackRow.className = "source-feedback-row";
    const question = document.createElement("span");
    question.className = "source-feedback-question";
    question.textContent = "Was this helpful and accurate?";
    const thumbsWrap = document.createElement("div");
    thumbsWrap.className = "source-feedback-thumbs";
    const upBtn = document.createElement("button");
    upBtn.type = "button";
    upBtn.setAttribute("aria-label", "Yes, helpful");
    upBtn.appendChild(thumbsUpIcon("source-feedback-icon"));
    const downBtn = document.createElement("button");
    downBtn.type = "button";
    downBtn.setAttribute("aria-label", "No, not helpful");
    downBtn.appendChild(thumbsDownIcon("source-feedback-icon"));
    thumbsWrap.appendChild(upBtn);
    thumbsWrap.appendChild(downBtn);
    feedbackRow.appendChild(question);
    feedbackRow.appendChild(thumbsWrap);
    item.appendChild(feedbackRow);
    if (existingRating) {
      upBtn.classList.toggle("selected", existingRating === "up");
      downBtn.classList.toggle("selected", existingRating === "down");
      upBtn.disabled = true;
      downBtn.disabled = true;
    } else if (correlationId) {
      upBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        e.preventDefault();
        fetch(API_BASE + "/chat/feedback/source", {
          method: "POST",
          headers: { "Content-Type": "application/json", ...getAuthHeaders() },
          body: JSON.stringify({ correlation_id: correlationId, source_index: sourceIndex, rating: "up" })
        }).then((r) => {
          if (r.ok) {
            upBtn.classList.add("selected");
            downBtn.classList.remove("selected");
            upBtn.disabled = true;
            downBtn.disabled = true;
          }
        });
      });
      downBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        e.preventDefault();
        fetch(API_BASE + "/chat/feedback/source", {
          method: "POST",
          headers: { "Content-Type": "application/json", ...getAuthHeaders() },
          body: JSON.stringify({ correlation_id: correlationId, source_index: sourceIndex, rating: "down" })
        }).then((r) => {
          if (r.ok) {
            downBtn.classList.add("selected");
            upBtn.classList.remove("selected");
            upBtn.disabled = true;
            downBtn.disabled = true;
          }
        });
      });
    }
    const ragUrl = onSourceClick ? getRagDocumentUrl(s.document_id, s.page_number) : null;
    if (ragUrl) {
      const linkWrap = document.createElement("div");
      linkWrap.className = "source-open-doc";
      const link = document.createElement("a");
      link.href = ragUrl;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      link.className = "source-open-doc-link";
      link.textContent = "Open full document";
      link.addEventListener("click", (e) => e.stopPropagation());
      linkWrap.appendChild(link);
      item.appendChild(linkWrap);
    }
    body.appendChild(item);
  });
  wrap.appendChild(preview);
  wrap.appendChild(body);
  return wrap;
}
function getRagDocumentUrl(documentId, pageNumber) {
  const base = typeof window !== "undefined" && window.RAG_APP_BASE ? window.RAG_APP_BASE.trim() : "";
  if (!base || !documentId || !documentId.trim())
    return null;
  const params = new URLSearchParams({ tab: "read", documentId: documentId.trim() });
  if (pageNumber != null)
    params.set("pageNumber", String(pageNumber));
  return `${base.replace(/\/$/, "")}?${params.toString()}`;
}
function openDocumentOrSnippet(s) {
  const url = getRagDocumentUrl(s.document_id, s.page_number);
  if (url) {
    window.open(url, "_blank", "noopener,noreferrer");
    return;
  }
  openMiniReaderSnippetOnly(s.document_name, s.page_number, s.snippet);
}
function openMiniReaderSnippetOnly(documentName, pageNumber, snippet) {
  const docName = documentName || "Document";
  const title = pageNumber != null ? `${docName} (page ${pageNumber})` : docName;
  let overlay = document.getElementById("mini-reader-overlay");
  if (!overlay) {
    overlay = document.createElement("div");
    overlay.id = "mini-reader-overlay";
    overlay.className = "mini-reader-overlay";
    overlay.setAttribute("aria-hidden", "true");
    const panel = document.createElement("div");
    panel.className = "mini-reader-panel";
    panel.setAttribute("role", "dialog");
    panel.setAttribute("aria-labelledby", "mini-reader-title");
    const header = document.createElement("div");
    header.className = "mini-reader-header";
    const titleEl2 = document.createElement("h2");
    titleEl2.id = "mini-reader-title";
    titleEl2.className = "mini-reader-title";
    const closeBtn = document.createElement("button");
    closeBtn.type = "button";
    closeBtn.className = "mini-reader-close";
    closeBtn.setAttribute("aria-label", "Close");
    closeBtn.textContent = "\xD7";
    const contentEl2 = document.createElement("div");
    contentEl2.className = "mini-reader-content";
    header.appendChild(titleEl2);
    header.appendChild(closeBtn);
    panel.appendChild(header);
    panel.appendChild(contentEl2);
    overlay.appendChild(panel);
    closeBtn.addEventListener("click", () => {
      overlay?.classList.remove("open");
      overlay?.setAttribute("aria-hidden", "true");
    });
    overlay.addEventListener("click", (e) => {
      if (e.target === overlay) {
        overlay.classList.remove("open");
        overlay.setAttribute("aria-hidden", "true");
      }
    });
    document.body.appendChild(overlay);
  }
  const titleEl = overlay.querySelector("#mini-reader-title");
  const contentEl = overlay.querySelector(".mini-reader-content");
  titleEl.textContent = title;
  contentEl.textContent = snippet || "(No snippet)";
  overlay.classList.add("open");
  overlay.setAttribute("aria-hidden", "false");
}
function scrollToBottom(container) {
  container.scrollTop = container.scrollHeight;
}
function run() {
  const messagesEl = el("messages");
  const inputEl = el("input");
  const sendBtn = el("send");
  const drawer = el("drawer");
  const drawerOverlay = el("drawerOverlay");
  const hamburger = el("hamburger");
  const drawerClose = el("drawerClose");
  const sidebar = document.getElementById("sidebar");
  const mainEl = document.querySelector(".main");
  const sidebarChevron = document.getElementById("sidebarChevron");
  function setSidebarCollapsed(collapsed) {
    if (!sidebar || !mainEl)
      return;
    if (collapsed) {
      sidebar.classList.add("sidebar--collapsed");
      mainEl.classList.add("sidebar-collapsed");
      if (sidebarChevron) {
        sidebarChevron.setAttribute("aria-label", "Expand sidebar");
        sidebarChevron.setAttribute("title", "Expand sidebar");
      }
    } else {
      sidebar.classList.remove("sidebar--collapsed");
      mainEl.classList.remove("sidebar-collapsed");
      if (sidebarChevron) {
        sidebarChevron.setAttribute("aria-label", "Collapse sidebar");
        sidebarChevron.setAttribute("title", "Collapse sidebar");
      }
    }
  }
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
  function openDrawer() {
    drawer.classList.add("open");
    drawerOverlay.classList.add("open");
    loadChatConfig();
  }
  function closeDrawer() {
    drawer.classList.remove("open");
    drawerOverlay.classList.remove("open");
  }
  hamburger.addEventListener("click", openDrawer);
  drawerClose.addEventListener("click", closeDrawer);
  drawerOverlay.addEventListener("click", closeDrawer);
  let currentAuthUser = null;
  function updateSidebarUser(user) {
    currentAuthUser = user;
    const nameEl = document.getElementById("sidebarUserName");
    if (nameEl)
      nameEl.textContent = user ? user.preferred_name || user.first_name || user.display_name || user.email || "User" : "Guest";
  }
  const authModal = createAuthModal({
    auth,
    showOAuth: true,
    onSuccess: (u) => updateSidebarUser(u)
  });
  document.body.appendChild(authModal.el);
  document.head.insertAdjacentHTML("beforeend", `<style>${AUTH_STYLES}</style>`);
  window.onOpenPreferences = openDrawer;
  auth.on((event, u) => {
    if (event === "login")
      updateSidebarUser(u);
    else if (event === "logout")
      updateSidebarUser(null);
  });
  const sidebarUser = document.getElementById("sidebarUser");
  sidebarUser?.addEventListener("click", () => authModal.open(currentAuthUser ? "account" : "login"));
  sidebarUser?.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      authModal.open(currentAuthUser ? "account" : "login");
    }
  });
  auth.getUserProfile().then((u) => updateSidebarUser(u ?? null));
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
      const llmEl = document.getElementById("configLlm");
      if (llmEl)
        llmEl.textContent = "Provider: " + (llm.provider ?? "\u2014") + ", Model: " + (llm.model ?? "\u2014") + (llm.temperature != null ? ", Temp: " + llm.temperature : "");
      const parser = data.parser ?? {};
      const parserEl = document.getElementById("configParser");
      if (parserEl)
        parserEl.textContent = "Patient keywords: " + (parser.patient_keywords?.length ? parser.patient_keywords.join(", ") : "\u2014");
      loadSidebarLlm(data);
    }).catch(() => {
      const sysEl = document.getElementById("promptFirstGenSystem");
      const llmEl = document.getElementById("configLlm");
      if (sysEl)
        sysEl.textContent = "Failed to load config.";
      if (llmEl)
        llmEl.textContent = "Failed to load config.";
    });
  }
  function loadSidebarLlm(config) {
    const el2 = document.getElementById("sidebarLlmLabel");
    if (!el2)
      return;
    if (config?.llm) {
      const p = config.llm.provider ?? "\u2014";
      const m = config.llm.model ?? "\u2014";
      el2.textContent = "LLM: " + p + " / " + m;
    } else {
      fetch(API_BASE + "/chat/config").then((r) => r.json()).then((data) => {
        if (data.llm)
          el2.textContent = "LLM: " + (data.llm.provider ?? "\u2014") + " / " + (data.llm.model ?? "\u2014");
      }).catch(() => {
        el2.textContent = "LLM: \u2014";
      });
    }
  }
  function loadSidebarHistory() {
    const recentList = document.getElementById("recentList");
    const helpfulList = document.getElementById("helpfulList");
    const documentsList = document.getElementById("documentsList");
    if (!recentList || !helpfulList || !documentsList)
      return;
    const snippet = (q, max = 50) => (q ?? "").trim().slice(0, max) + ((q ?? "").length > max ? "\u2026" : "");
    Promise.all([
      fetch(API_BASE + "/chat/history/recent?limit=10").then(
        (r) => r.json()
      ),
      fetch(API_BASE + "/chat/history/most-helpful-searches?limit=10").then(
        (r) => r.json()
      ),
      fetch(API_BASE + "/chat/history/most-helpful-documents?limit=10").then(
        (r) => r.json()
      )
    ]).then(([recent, helpful, documents]) => {
      recentList.innerHTML = "";
      recent.forEach((item) => {
        const li = document.createElement("li");
        li.className = "recent-item";
        li.textContent = snippet(item.question);
        li.title = item.question;
        li.setAttribute("data-correlation-id", item.correlation_id);
        li.addEventListener("click", () => {
          const q = (item.question ?? "").trim();
          if (!q)
            return;
          inputEl.value = q;
          updateSendState();
          sendMessage();
        });
        recentList.appendChild(li);
      });
      helpfulList.innerHTML = "";
      helpful.forEach((item) => {
        const li = document.createElement("li");
        li.className = "helpful-item";
        li.textContent = snippet(item.question);
        li.title = item.question;
        li.setAttribute("data-correlation-id", item.correlation_id);
        li.addEventListener("click", () => {
          const q = (item.question ?? "").trim();
          if (!q)
            return;
          inputEl.value = q;
          updateSendState();
          sendMessage();
        });
        helpfulList.appendChild(li);
      });
      documentsList.innerHTML = "";
      documents.forEach((item) => {
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
          () => openDocumentOrSnippet({ document_id: item.document_id ?? null, document_name: item.document_name, page_number: null, snippet: "" })
        );
        li.addEventListener("keydown", (e) => {
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            openDocumentOrSnippet({ document_id: item.document_id ?? null, document_name: item.document_name, page_number: null, snippet: "" });
          }
        });
        documentsList.appendChild(li);
      });
    }).catch(() => {
      recentList.innerHTML = "";
      helpfulList.innerHTML = "";
      documentsList.innerHTML = "";
    });
  }
  function pollResponse(correlationId, onThinking, onStreamingMessage) {
    return new Promise((resolve, reject) => {
      const maxAttempts = 120;
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
          if (data.status === "completed") {
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
          const data = parsed.data;
          const writtenAt = data?.ts_readable ?? data?.ts;
          if (typeof console !== "undefined") {
            console.log(`[stream] ${ev} received_at=${(/* @__PURE__ */ new Date()).toISOString().slice(11, 23)} written_at=${String(writtenAt ?? "\u2014")}`);
          }
          if (ev === "thinking" && data?.line != null && onThinking) {
            onThinking(String(data.line));
          } else if (ev === "message" && data?.chunk != null && onStreamingMessage) {
            messageSoFar += String(data.chunk);
            onStreamingMessage(messageSoFar);
          } else if (ev === "completed" && data != null) {
            resolved = true;
            es.close();
            resolve(data);
          } else if (ev === "error" && data?.message != null) {
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
  function sendMessage() {
    const message = (inputEl.value ?? "").trim();
    if (!message)
      return;
    if (sendBtn.disabled)
      return;
    if (chatEmpty)
      chatEmpty.classList.add("hidden");
    const turnWrap = document.createElement("div");
    turnWrap.className = "chat-turn";
    turnWrap.appendChild(renderUserMessage(message));
    messagesEl.appendChild(turnWrap);
    scrollToBottom(messagesEl);
    inputEl.value = "";
    updateSendState();
    sendBtn.disabled = true;
    inputEl.disabled = true;
    const { el: progressStackEl, addLine: progressAddLine } = renderProgressStack();
    turnWrap.appendChild(progressStackEl);
    scrollToBottom(messagesEl);
    function onThinkingLine(line) {
      progressAddLine(line);
      scrollToBottom(messagesEl);
    }
    let messageWrapEl = null;
    function onStreamingMessage(_text) {
      scrollToBottom(messagesEl);
    }
    fetch(API_BASE + "/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json", ...getAuthHeaders() },
      body: JSON.stringify({ message })
    }).then((r) => r.json()).then((postData) => {
      progressAddLine("Request sent. Waiting for worker\u2026");
      const correlationId = postData.correlation_id;
      return streamResponse(correlationId, onThinkingLine, onStreamingMessage).then(
        (streamData) => ({ streamData, correlationId })
      );
    }).then(({ streamData: data, correlationId }) => {
      if (typeof console !== "undefined" && console.log) {
        console.log("[AnswerCard] stream completed, processing final message\u2026");
      }
      const fullMessage = data.message ?? "(No message)";
      const { body, sources } = parseMessageAndSources(fullMessage);
      if (typeof console !== "undefined" && console.log) {
        console.log("[AnswerCard] fullMessage length:", fullMessage.length, "starts:", (fullMessage || "").slice(0, 120));
        console.log("[AnswerCard] body length:", (body || "").length, "starts:", (body || "").slice(0, 120));
      }
      progressStackEl.remove();
      const finalBody = body || "(No response)";
      const parsedCard = tryParseAnswerCard(finalBody);
      if (typeof console !== "undefined" && console.log) {
        console.log("[AnswerCard] tryParseAnswerCard:", parsedCard ? "card (mode=" + parsedCard.mode + ")" : "null");
      }
      const contentEl = renderAssistantContent(finalBody, !!data.llm_error);
      if (messageWrapEl) {
        messageWrapEl.replaceWith(contentEl);
      } else {
        turnWrap.appendChild(contentEl);
      }
      turnWrap.appendChild(renderFeedback(correlationId).el);
      const sourceList = data.sources && data.sources.length > 0 ? data.sources.map((s) => ({
        index: s.index ?? 0,
        document_id: s.document_id ?? null,
        document_name: s.document_name ?? "document",
        page_number: s.page_number ?? null,
        snippet: (s.text ?? "").slice(0, 200),
        source_type: s.source_type ?? null,
        match_score: s.match_score ?? null,
        confidence: s.confidence ?? null
      })) : sources.length > 0 ? sources.map((s) => ({
        index: s.index ?? 0,
        document_id: s.document_id ?? null,
        document_name: s.document_name ?? "document",
        page_number: s.page_number ?? null,
        snippet: (s.snippet ?? "").slice(0, 120),
        source_type: null,
        match_score: null,
        confidence: null
      })) : [];
      if (sourceList.length > 0) {
        const appendSourceCiter = (ratings) => {
          turnWrap.appendChild(
            renderSourceCiter(
              sourceList,
              (s) => openDocumentOrSnippet({
                document_id: s.document_id,
                document_name: s.document_name,
                page_number: s.page_number,
                snippet: s.snippet
              }),
              correlationId,
              ratings
            )
          );
        };
        fetch(API_BASE + "/chat/feedback/source/" + encodeURIComponent(correlationId)).then((r) => r.ok ? r.json() : { ratings: [] }).then((data2) => {
          const ratings = {};
          (data2.ratings || []).forEach((x) => {
            if (x.rating === "up" || x.rating === "down")
              ratings[x.source_index] = x.rating;
          });
          appendSourceCiter(ratings);
        }).catch(() => appendSourceCiter({}));
      }
      scrollToBottom(messagesEl);
      loadSidebarHistory();
    }).catch((err) => {
      progressStackEl.remove();
      turnWrap.appendChild(
        renderAssistantMessage("Error: " + (err?.message ?? String(err)), true)
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
  updateSendState();
  loadSidebarHistory();
  loadSidebarLlm();
}
run();
