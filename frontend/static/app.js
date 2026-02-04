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
function escapeHtml2(text) {
  const div = document.createElement("div");
  div.textContent = text;
  return div.innerHTML;
}
function prefsFromProfile(profile) {
  return {
    preferred_name: profile.preferred_name ?? "",
    timezone: profile.timezone ?? "America/New_York",
    activities: profile.activities ?? [],
    tone: profile.tone ?? "professional",
    greeting_enabled: profile.greeting_enabled !== false,
    autonomy_routine_tasks: profile.autonomy_routine_tasks ?? "confirm_first",
    autonomy_sensitive_tasks: profile.autonomy_sensitive_tasks ?? "manual"
  };
}
function createPreferencesModal(apiBase2, auth2, options) {
  const base = apiBase2.replace(/\/$/, "");
  const authBase = `${base}/auth`;
  let modalEl = null;
  let stylesInjected = false;
  function ensureStyles() {
    if (stylesInjected || document.getElementById("mobius-prefs-styles")) {
      stylesInjected = true;
      return;
    }
    const style = document.createElement("style");
    style.id = "mobius-prefs-styles";
    style.textContent = PREFERENCES_MODAL_STYLES;
    document.head.appendChild(style);
    stylesInjected = true;
  }
  function close() {
    if (modalEl && modalEl.parentNode) {
      modalEl.parentNode.removeChild(modalEl);
      modalEl = null;
    }
    options?.onClose?.();
  }
  async function open() {
    const token = await auth2.getAccessToken();
    if (!token) {
      console.warn("[PreferencesModal] Not signed in");
      return;
    }
    ensureStyles();
    let activities = [];
    try {
      const res = await fetch(`${authBase}/activities`);
      const data = await res.json();
      if (data.ok && Array.isArray(data.activities)) {
        activities = data.activities.map((a) => ({
          activity_code: a.activity_code,
          label: a.label,
          description: a.description
        }));
      }
    } catch (e) {
      console.error("[PreferencesModal] Error fetching activities:", e);
    }
    const profile = await auth2.getCurrentUser();
    const initialPrefs = profile ? prefsFromProfile(profile) : prefsFromProfile({});
    const prefs = { ...initialPrefs, activities: [...initialPrefs.activities ?? []] };
    const selectedActivities = [...prefs.activities ?? []];
    const modal = document.createElement("div");
    modal.className = "mobius-prefs-modal";
    let activeTab = "profile";
    function render() {
      modal.innerHTML = `
      <div class="mobius-prefs-backdrop"></div>
      <div class="mobius-prefs-container">
        <div class="mobius-prefs-header">
          <h2>My Preferences</h2>
          <button class="mobius-prefs-close" type="button">
            <svg viewBox="0 0 24 24" width="18" height="18">
              <path fill="currentColor" d="M19 6.41L17.59 5 12 10.59 6.41 5 5 6.41 10.59 12 5 17.59 6.41 19 12 13.41 17.59 19 19 17.59 13.41 12z"/>
            </svg>
          </button>
        </div>
        <div class="mobius-prefs-tabs">
          <button class="mobius-prefs-tab ${activeTab === "profile" ? "active" : ""}" data-tab="profile">Profile</button>
          <button class="mobius-prefs-tab ${activeTab === "activities" ? "active" : ""}" data-tab="activities">Activities</button>
          <button class="mobius-prefs-tab ${activeTab === "ai" ? "active" : ""}" data-tab="ai">AI Comfort</button>
          <button class="mobius-prefs-tab ${activeTab === "display" ? "active" : ""}" data-tab="display">Display</button>
        </div>
        <div class="mobius-prefs-content">
          ${renderTabContent()}
        </div>
        <div class="mobius-prefs-footer">
          <button class="mobius-prefs-btn-cancel" type="button">Cancel</button>
          <button class="mobius-prefs-btn-save" type="button">Save Changes</button>
        </div>
      </div>
    `;
      wireEvents();
    }
    function renderTabContent() {
      const tone = prefs.tone ?? "professional";
      const routine = prefs.autonomy_routine_tasks ?? "confirm_first";
      const sensitive = prefs.autonomy_sensitive_tasks ?? "manual";
      const greeting = prefs.greeting_enabled !== false;
      switch (activeTab) {
        case "profile":
          return `
          <div class="mobius-prefs-section">
            <label class="mobius-prefs-label">Preferred Name</label>
            <input type="text" class="mobius-prefs-input" id="pref-name"
                   value="${escapeHtml2(prefs.preferred_name ?? "")}"
                   placeholder="How should we greet you?" />
          </div>
          <div class="mobius-prefs-section">
            <label class="mobius-prefs-label">Timezone</label>
            <select class="mobius-prefs-select" id="pref-timezone">
              <option value="America/New_York" ${prefs.timezone === "America/New_York" ? "selected" : ""}>Eastern Time (ET)</option>
              <option value="America/Chicago" ${prefs.timezone === "America/Chicago" ? "selected" : ""}>Central Time (CT)</option>
              <option value="America/Denver" ${prefs.timezone === "America/Denver" ? "selected" : ""}>Mountain Time (MT)</option>
              <option value="America/Los_Angeles" ${prefs.timezone === "America/Los_Angeles" ? "selected" : ""}>Pacific Time (PT)</option>
            </select>
          </div>
        `;
        case "activities":
          return `
          <p class="mobius-prefs-desc">Select the activities you work on. This helps Mobius show you relevant quick actions and tasks.</p>
          <div class="mobius-prefs-activities">
            ${activities.map((a) => `
              <label class="mobius-prefs-activity ${selectedActivities.includes(a.activity_code) ? "selected" : ""}">
                <input type="checkbox" value="${escapeHtml2(a.activity_code)}" ${selectedActivities.includes(a.activity_code) ? "checked" : ""} />
                <span class="mobius-prefs-activity-check">
                  <svg viewBox="0 0 24 24" width="14" height="14">
                    <path fill="currentColor" d="M9 16.17L4.83 12l-1.42 1.41L9 19 21 7l-1.41-1.41z"/>
                  </svg>
                </span>
                <span>${escapeHtml2(a.label)}</span>
              </label>
            `).join("")}
          </div>
        `;
        case "ai":
          return `
          <div class="mobius-prefs-section">
            <label class="mobius-prefs-label">For routine tasks (eligibility checks, status updates):</label>
            <div class="mobius-prefs-options">
              <label class="mobius-prefs-option ${routine === "automatic" ? "selected" : ""}">
                <input type="radio" name="routine" value="automatic" ${routine === "automatic" ? "checked" : ""} />
                <span>Do it automatically</span>
              </label>
              <label class="mobius-prefs-option ${routine === "confirm_first" ? "selected" : ""}">
                <input type="radio" name="routine" value="confirm_first" ${routine === "confirm_first" ? "checked" : ""} />
                <span>Show me first, then confirm</span>
              </label>
              <label class="mobius-prefs-option ${routine === "manual" ? "selected" : ""}">
                <input type="radio" name="routine" value="manual" ${routine === "manual" ? "checked" : ""} />
                <span>Just guide me, I'll do it</span>
              </label>
            </div>
          </div>
          <div class="mobius-prefs-section">
            <label class="mobius-prefs-label">For sensitive tasks (billing, patient records):</label>
            <div class="mobius-prefs-options">
              <label class="mobius-prefs-option ${sensitive === "automatic" ? "selected" : ""}">
                <input type="radio" name="sensitive" value="automatic" ${sensitive === "automatic" ? "checked" : ""} />
                <span>Do it automatically</span>
              </label>
              <label class="mobius-prefs-option ${sensitive === "confirm_first" ? "selected" : ""}">
                <input type="radio" name="sensitive" value="confirm_first" ${sensitive === "confirm_first" ? "checked" : ""} />
                <span>Always show me before acting</span>
              </label>
              <label class="mobius-prefs-option ${sensitive === "manual" ? "selected" : ""}">
                <input type="radio" name="sensitive" value="manual" ${sensitive === "manual" ? "checked" : ""} />
                <span>Never act without my approval</span>
              </label>
            </div>
          </div>
        `;
        case "display":
          return `
          <div class="mobius-prefs-section">
            <label class="mobius-prefs-label">Communication Tone</label>
            <div class="mobius-prefs-options">
              <label class="mobius-prefs-option ${tone === "professional" ? "selected" : ""}">
                <input type="radio" name="tone" value="professional" ${tone === "professional" ? "checked" : ""} />
                <span>Professional</span>
              </label>
              <label class="mobius-prefs-option ${tone === "friendly" ? "selected" : ""}">
                <input type="radio" name="tone" value="friendly" ${tone === "friendly" ? "checked" : ""} />
                <span>Friendly</span>
              </label>
              <label class="mobius-prefs-option ${tone === "concise" ? "selected" : ""}">
                <input type="radio" name="tone" value="concise" ${tone === "concise" ? "checked" : ""} />
                <span>Concise</span>
              </label>
            </div>
          </div>
          <div class="mobius-prefs-section">
            <label class="mobius-prefs-toggle">
              <input type="checkbox" id="pref-greeting" ${greeting ? "checked" : ""} />
              <span class="mobius-prefs-toggle-slider"></span>
              <span class="mobius-prefs-toggle-label">Show personalized greeting</span>
            </label>
          </div>
        `;
        default:
          return "";
      }
    }
    function updateRadioStyles(name) {
      modal.querySelectorAll(`input[name="${name}"]`).forEach((r) => {
        const label = r.closest(".mobius-prefs-option");
        if (r.checked)
          label?.classList.add("selected");
        else
          label?.classList.remove("selected");
      });
    }
    function wireEvents() {
      modal.querySelector(".mobius-prefs-close")?.addEventListener("click", close);
      modal.querySelector(".mobius-prefs-backdrop")?.addEventListener("click", close);
      modal.querySelector(".mobius-prefs-btn-cancel")?.addEventListener("click", close);
      modal.querySelector(".mobius-prefs-btn-save")?.addEventListener("click", async () => {
        const saveBtn = modal.querySelector(".mobius-prefs-btn-save");
        if (saveBtn) {
          saveBtn.textContent = "Saving...";
          saveBtn.disabled = true;
        }
        try {
          const t = await auth2.getAccessToken();
          if (!t) {
            close();
            return;
          }
          const response = await fetch(`${authBase}/preferences`, {
            method: "PUT",
            headers: {
              Authorization: `Bearer ${t}`,
              "Content-Type": "application/json"
            },
            body: JSON.stringify({
              preferred_name: prefs.preferred_name,
              timezone: prefs.timezone,
              activities: selectedActivities,
              tone: prefs.tone,
              greeting_enabled: prefs.greeting_enabled,
              autonomy_routine_tasks: prefs.autonomy_routine_tasks,
              autonomy_sensitive_tasks: prefs.autonomy_sensitive_tasks
            })
          });
          if (response.ok) {
            prefs.activities = [...selectedActivities];
            await auth2.getCurrentUser();
            options?.onSave?.(prefs);
            close();
          } else {
            const errData = await response.json().catch(() => ({}));
            console.error("[PreferencesModal] Error saving preferences:", errData);
            alert("Failed to save preferences. Please try again.");
          }
        } catch (error) {
          console.error("[PreferencesModal] Error saving preferences:", error);
          alert("Failed to save preferences. Please try again.");
        } finally {
          const btn = modal.querySelector(".mobius-prefs-btn-save");
          if (btn) {
            btn.textContent = "Save Changes";
            btn.disabled = false;
          }
        }
      });
      modal.querySelectorAll(".mobius-prefs-tab").forEach((tab) => {
        tab.addEventListener("click", () => {
          activeTab = tab.dataset.tab ?? "profile";
          render();
        });
      });
      modal.querySelector("#pref-name")?.addEventListener("input", (e) => {
        prefs.preferred_name = e.target.value;
      });
      modal.querySelector("#pref-timezone")?.addEventListener("change", (e) => {
        prefs.timezone = e.target.value;
      });
      modal.querySelectorAll(".mobius-prefs-activity input").forEach((cb) => {
        cb.addEventListener("change", (e) => {
          const code = e.target.value;
          const checked = e.target.checked;
          const label = e.target.closest(".mobius-prefs-activity");
          if (checked) {
            if (!selectedActivities.includes(code))
              selectedActivities.push(code);
            label?.classList.add("selected");
          } else {
            const idx = selectedActivities.indexOf(code);
            if (idx > -1)
              selectedActivities.splice(idx, 1);
            label?.classList.remove("selected");
          }
        });
      });
      ["routine", "sensitive", "tone"].forEach((name) => {
        modal.querySelectorAll(`input[name="${name}"]`).forEach((r) => {
          r.addEventListener("change", (e) => {
            const value = e.target.value;
            if (name === "routine")
              prefs.autonomy_routine_tasks = value;
            if (name === "sensitive")
              prefs.autonomy_sensitive_tasks = value;
            if (name === "tone")
              prefs.tone = value;
            updateRadioStyles(name);
          });
        });
      });
      modal.querySelector("#pref-greeting")?.addEventListener("change", (e) => {
        prefs.greeting_enabled = e.target.checked;
      });
    }
    render();
    modalEl = modal;
    document.body.appendChild(modal);
  }
  return { open, close };
}
var PREFERENCES_MODAL_STYLES = `
.mobius-prefs-modal {
  position: fixed;
  inset: 0;
  z-index: 10000;
}
.mobius-prefs-backdrop {
  position: absolute;
  inset: 0;
  background: rgba(0, 0, 0, 0.5);
}
.mobius-prefs-container {
  position: absolute;
  top: 50%;
  left: 50%;
  transform: translate(-50%, -50%);
  background: white;
  border-radius: 12px;
  width: 90%;
  max-width: 420px;
  max-height: 85vh;
  display: flex;
  flex-direction: column;
  box-shadow: 0 8px 32px rgba(0, 0, 0, 0.2);
}
.mobius-prefs-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 16px 20px;
  border-bottom: 1px solid #e2e8f0;
}
.mobius-prefs-header h2 {
  margin: 0;
  font-size: 14px;
  font-weight: 600;
  color: #0b1220;
}
.mobius-prefs-close {
  background: none;
  border: none;
  cursor: pointer;
  padding: 4px;
  color: #64748b;
}
.mobius-prefs-close:hover {
  color: #374151;
}
.mobius-prefs-tabs {
  display: flex;
  border-bottom: 1px solid #e2e8f0;
  padding: 0 12px;
}
.mobius-prefs-tab {
  padding: 10px 12px;
  background: none;
  border: none;
  border-bottom: 2px solid transparent;
  font-size: 11px;
  color: #64748b;
  cursor: pointer;
  transition: all 0.15s;
}
.mobius-prefs-tab:hover {
  color: #374151;
}
.mobius-prefs-tab.active {
  color: #3b82f6;
  border-bottom-color: #3b82f6;
}
.mobius-prefs-content {
  flex: 1;
  overflow-y: auto;
  padding: 16px 20px;
}
.mobius-prefs-section {
  margin-bottom: 16px;
}
.mobius-prefs-section:last-child {
  margin-bottom: 0;
}
.mobius-prefs-label {
  display: block;
  font-size: 11px;
  font-weight: 500;
  color: #374151;
  margin-bottom: 8px;
}
.mobius-prefs-desc {
  font-size: 10px;
  color: #64748b;
  margin: 0 0 12px;
}
.mobius-prefs-input,
.mobius-prefs-select {
  width: 100%;
  padding: 8px 10px;
  border: 1px solid #e2e8f0;
  border-radius: 6px;
  font-size: 12px;
  box-sizing: border-box;
}
.mobius-prefs-input:focus,
.mobius-prefs-select:focus {
  outline: none;
  border-color: #3b82f6;
}
.mobius-prefs-activities {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
.mobius-prefs-activity {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 6px 10px;
  background: #f8fafc;
  border: 1px solid #e2e8f0;
  border-radius: 6px;
  cursor: pointer;
  font-size: 10px;
  color: #374151;
  transition: all 0.15s;
}
.mobius-prefs-activity:hover {
  background: #f1f5f9;
}
.mobius-prefs-activity.selected {
  background: #eff6ff;
  border-color: #3b82f6;
}
.mobius-prefs-activity input {
  display: none;
}
.mobius-prefs-activity-check {
  width: 14px;
  height: 14px;
  border: 1px solid #cbd5e1;
  border-radius: 3px;
  display: flex;
  align-items: center;
  justify-content: center;
  color: white;
}
.mobius-prefs-activity.selected .mobius-prefs-activity-check {
  background: #3b82f6;
  border-color: #3b82f6;
}
.mobius-prefs-options {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.mobius-prefs-option {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 10px 12px;
  background: #f8fafc;
  border: 1px solid #e2e8f0;
  border-radius: 6px;
  cursor: pointer;
  font-size: 11px;
  color: #374151;
  transition: all 0.15s;
}
.mobius-prefs-option:hover {
  background: #f1f5f9;
}
.mobius-prefs-option.selected {
  background: #eff6ff;
  border-color: #3b82f6;
}
.mobius-prefs-option input {
  display: none;
}
.mobius-prefs-toggle {
  display: flex;
  align-items: center;
  gap: 10px;
  cursor: pointer;
}
.mobius-prefs-toggle input {
  display: none;
}
.mobius-prefs-toggle-slider {
  width: 36px;
  height: 20px;
  background: #e2e8f0;
  border-radius: 10px;
  position: relative;
  transition: background 0.2s;
}
.mobius-prefs-toggle-slider::after {
  content: '';
  position: absolute;
  width: 16px;
  height: 16px;
  background: white;
  border-radius: 50%;
  top: 2px;
  left: 2px;
  transition: transform 0.2s;
  box-shadow: 0 1px 3px rgba(0,0,0,0.2);
}
.mobius-prefs-toggle input:checked + .mobius-prefs-toggle-slider {
  background: #3b82f6;
}
.mobius-prefs-toggle input:checked + .mobius-prefs-toggle-slider::after {
  transform: translateX(16px);
}
.mobius-prefs-toggle-label {
  font-size: 11px;
  color: #374151;
}
.mobius-prefs-footer {
  display: flex;
  justify-content: flex-end;
  gap: 10px;
  padding: 16px 20px;
  border-top: 1px solid #e2e8f0;
}
.mobius-prefs-btn-cancel {
  padding: 8px 16px;
  background: none;
  border: 1px solid #e2e8f0;
  border-radius: 6px;
  font-size: 11px;
  color: #64748b;
  cursor: pointer;
}
.mobius-prefs-btn-cancel:hover {
  background: #f8fafc;
}
.mobius-prefs-btn-save {
  padding: 8px 16px;
  background: #3b82f6;
  border: none;
  border-radius: 6px;
  font-size: 11px;
  font-weight: 500;
  color: white;
  cursor: pointer;
}
.mobius-prefs-btn-save:hover {
  background: #2563eb;
}
`;
function escapeHtml3(text) {
  const div = document.createElement("div");
  div.textContent = text;
  return div.innerHTML;
}
function getDropdownPosition(anchorRect, dropdownWidth, dropdownHeight, options = {}) {
  const { preferAbove = false, gap = 8 } = options;
  const vw = window.innerWidth;
  const vh = window.innerHeight;
  let top;
  let left;
  let transformOrigin = "top left";
  const spaceAbove = anchorRect.top;
  const spaceBelow = vh - anchorRect.bottom;
  if (preferAbove && spaceAbove >= dropdownHeight + gap) {
    top = anchorRect.top - dropdownHeight - gap;
    transformOrigin = "bottom left";
  } else if (spaceBelow >= dropdownHeight + gap) {
    top = anchorRect.bottom + gap;
    transformOrigin = "top left";
  } else if (spaceAbove > spaceBelow) {
    top = Math.max(gap, anchorRect.top - dropdownHeight - gap);
    transformOrigin = "bottom left";
  } else {
    top = anchorRect.bottom + gap;
    transformOrigin = "top left";
  }
  const spaceRight = vw - anchorRect.left;
  const spaceLeft = anchorRect.right;
  if (spaceRight >= dropdownWidth) {
    left = anchorRect.left;
  } else if (spaceLeft >= dropdownWidth) {
    left = anchorRect.right - dropdownWidth;
    transformOrigin = transformOrigin.replace("left", "right");
  } else {
    left = Math.max(gap, Math.min(anchorRect.left, vw - dropdownWidth - gap));
  }
  return { top, left, transformOrigin };
}
function createUserMenu(options) {
  const { auth: auth2, onOpenPreferences, onSignOut, onSwitchAccount } = options;
  let menuEl = null;
  let closeListener = null;
  let stylesInjected = false;
  function ensureStyles() {
    if (stylesInjected || document.getElementById("mobius-user-menu-styles")) {
      stylesInjected = true;
      return;
    }
    const style = document.createElement("style");
    style.id = "mobius-user-menu-styles";
    style.textContent = USER_MENU_STYLES;
    document.head.appendChild(style);
    stylesInjected = true;
  }
  function hide() {
    if (closeListener) {
      document.removeEventListener("click", closeListener);
      closeListener = null;
    }
    if (menuEl?.parentNode) {
      menuEl.parentNode.removeChild(menuEl);
      menuEl = null;
    }
  }
  async function show(anchor) {
    hide();
    ensureStyles();
    const user = await auth2.getUserProfile();
    if (!user)
      return;
    const displayName = user.preferred_name || user.first_name || user.display_name || user.email || "User";
    const email = user.email || "";
    const initial = (displayName || "?")[0].toUpperCase();
    const dropdownWidth = Math.max(anchor.getBoundingClientRect().width, 220);
    const dropdownHeight = 200;
    const rect = anchor.getBoundingClientRect();
    const pos = getDropdownPosition(rect, dropdownWidth, dropdownHeight, {
      preferAbove: false,
      gap: 4
    });
    menuEl = document.createElement("div");
    menuEl.className = "mobius-user-menu";
    menuEl.setAttribute("role", "menu");
    menuEl.style.cssText = `
      position: fixed;
      top: ${pos.top}px;
      left: ${pos.left}px;
      width: ${dropdownWidth}px;
      background: white;
      border-radius: 8px;
      box-shadow: 0 4px 16px rgba(0, 0, 0, 0.15);
      z-index: 10001;
      overflow: hidden;
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
      transform-origin: ${pos.transformOrigin};
    `;
    menuEl.innerHTML = `
      <div class="mobius-user-menu-header">
        <div class="mobius-user-menu-avatar">${escapeHtml3(initial)}</div>
        <div class="mobius-user-menu-info">
          <div class="mobius-user-menu-name">${escapeHtml3(displayName)}</div>
          ${email ? `<div class="mobius-user-menu-email">${escapeHtml3(email)}</div>` : ""}
        </div>
      </div>
      <div class="mobius-user-menu-divider"></div>
      <button type="button" class="mobius-user-menu-item" data-action="preferences">
        <svg viewBox="0 0 24 24" width="14" height="14" class="mobius-user-menu-icon"><path fill="currentColor" d="M19.14 12.94c.04-.31.06-.63.06-.94 0-.31-.02-.63-.06-.94l2.03-1.58c.18-.14.23-.41.12-.61l-1.92-3.32c-.12-.22-.37-.29-.59-.22l-2.39.96c-.5-.38-1.03-.7-1.62-.94l-.36-2.54c-.04-.24-.24-.41-.48-.41h-3.84c-.24 0-.43.17-.47.41l-.36 2.54c-.59.24-1.13.57-1.62.94l-2.39-.96c-.22-.08-.47 0-.59.22L2.74 8.87c-.12.21-.08.47.12.61l2.03 1.58c-.04.31-.06.63-.06.94s.02.63.06.94l-2.03 1.58c-.18.14-.23.41-.12.61l1.92 3.32c.12.22.37.29.59.22l2.39-.96c.5.38 1.03.7 1.62.94l.36 2.54c.05.24.24.41.48.41h3.84c.24 0 .44-.17.47-.41l.36-2.54c.59-.24 1.13-.56 1.62-.94l2.39.96c.22.08.47 0 .59-.22l1.92-3.32c.12-.22.07-.47-.12-.61l-2.01-1.58zM12 15.6c-1.98 0-3.6-1.62-3.6-3.6s1.62-3.6 3.6-3.6 3.6 1.62 3.6 3.6-1.62 3.6-3.6 3.6z"/></svg>
        <span>My Preferences</span>
      </button>
      <button type="button" class="mobius-user-menu-item" data-action="switch">
        <svg viewBox="0 0 24 24" width="14" height="14" class="mobius-user-menu-icon"><path fill="currentColor" d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm-1 17.93c-3.95-.49-7-3.85-7-7.93 0-.62.08-1.21.21-1.79L9 15v1c0 1.1.9 2 2 2v1.93zm6.9-2.54c-.26-.81-1-1.39-1.9-1.39h-1v-3c0-.55-.45-1-1-1H8v-2h2c.55 0 1-.45 1-1V7h2c1.1 0 2-.9 2-2v-.41c2.93 1.19 5 4.06 5 7.41 0 2.08-.8 3.97-2.1 5.39z"/></svg>
        <span>Not you? Sign in differently</span>
      </button>
      <div class="mobius-user-menu-divider"></div>
      <button type="button" class="mobius-user-menu-item mobius-user-menu-item--danger" data-action="signout">
        <svg viewBox="0 0 24 24" width="14" height="14" class="mobius-user-menu-icon"><path fill="currentColor" d="M17 7l-1.41 1.41L18.17 11H8v2h10.17l-2.58 2.58L17 17l5-5zM4 5h8V3H4c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h8v-2H4V5z"/></svg>
        <span>Sign out</span>
      </button>
    `;
    menuEl.querySelectorAll(".mobius-user-menu-item").forEach((btn) => {
      btn.addEventListener("mouseenter", () => {
        btn.style.background = "#f8fafc";
      });
      btn.addEventListener("mouseleave", () => {
        btn.style.background = "transparent";
      });
      btn.addEventListener("click", async (e) => {
        e.preventDefault();
        const action = btn.dataset.action;
        hide();
        if (action === "preferences") {
          onOpenPreferences?.();
        } else if (action === "signout") {
          await auth2.logout();
          onSignOut?.();
        } else if (action === "switch") {
          await auth2.logout();
          (onSwitchAccount ?? onSignOut)?.();
        }
      });
    });
    document.body.appendChild(menuEl);
    const listener = (e) => {
      if (menuEl && !menuEl.contains(e.target) && !anchor.contains(e.target)) {
        hide();
      }
    };
    closeListener = listener;
    setTimeout(() => document.addEventListener("click", listener), 0);
  }
  return { show, hide };
}
var USER_MENU_STYLES = `
.mobius-user-menu-header {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 12px;
  background: #f8fafc;
}
.mobius-user-menu-avatar {
  width: 36px;
  height: 36px;
  border-radius: 50%;
  background: #3b82f6;
  color: white;
  display: flex;
  align-items: center;
  justify-content: center;
  font-weight: 600;
  font-size: 14px;
  flex-shrink: 0;
}
.mobius-user-menu-info {
  flex: 1;
  min-width: 0;
}
.mobius-user-menu-name {
  font-size: 11px;
  font-weight: 600;
  color: #0b1220;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.mobius-user-menu-email {
  font-size: 9px;
  color: #64748b;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.mobius-user-menu-divider {
  height: 1px;
  background: #e2e8f0;
}
.mobius-user-menu-item {
  display: flex;
  align-items: center;
  gap: 10px;
  width: 100%;
  padding: 10px 12px;
  background: none;
  border: none;
  cursor: pointer;
  font-size: 10px;
  color: #374151;
  text-align: left;
  font-family: inherit;
}
.mobius-user-menu-item:hover {
  background: #f8fafc;
}
.mobius-user-menu-icon {
  color: #64748b;
  flex-shrink: 0;
}
.mobius-user-menu-item--danger .mobius-user-menu-icon,
.mobius-user-menu-item--danger {
  color: #dc2626;
}
`;
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
function renderAnswerCard(card, isError, sourceConfidenceStrip) {
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
  if (sourceConfidenceStrip != null && sourceConfidenceStrip !== "") {
    const badgeWrap = document.createElement("div");
    badgeWrap.className = "answer-card-badge-wrap";
    badgeWrap.appendChild(renderConfidenceBadge(sourceConfidenceStrip));
    bubble.appendChild(badgeWrap);
  }
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
var CONFIDENCE_BADGE = {
  approved_authoritative: { icon: "\u2714", label: "Approved" },
  approved_informational: { icon: "\u2139", label: "Informational" },
  pending: { icon: "\u26A0", label: "Unverified" },
  partial_pending: { icon: "\u26A0", label: "Unverified" },
  unverified: { icon: "\u26D4", label: "No source" }
};
function renderConfidenceBadge(value) {
  const key = (value || "").trim() || "unverified";
  const { icon, label } = CONFIDENCE_BADGE[key] ?? CONFIDENCE_BADGE.unverified;
  const badge = document.createElement("span");
  badge.className = "confidence-badge confidence-badge--" + key;
  badge.setAttribute("aria-label", label);
  badge.setAttribute("role", "status");
  const iconEl = document.createElement("span");
  iconEl.className = "confidence-badge-icon";
  iconEl.setAttribute("aria-hidden", "true");
  iconEl.textContent = icon;
  const textEl = document.createElement("span");
  textEl.className = "confidence-badge-label";
  textEl.textContent = label;
  badge.appendChild(iconEl);
  badge.appendChild(textEl);
  return badge;
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
  function replaceWithLine(line) {
    const trimmed = (line ?? "").trim();
    if (!trimmed)
      return;
    buffer.length = 0;
    buffer.push(trimmed);
    for (let i = 0; i < PROGRESS_MAX_LINES; i++) {
      const text = i === 0 ? trimmed : "";
      lineEls[i].textContent = text;
      lineEls[i].classList.toggle("empty", !text);
    }
    dotsEl.remove();
    lineEls[0].appendChild(dotsEl);
  }
  return {
    el: block,
    addLine,
    replaceWithLine
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
function renderAssistantContent(body, isError, sourceConfidenceStrip) {
  const card = tryParseAnswerCard(body);
  if (typeof console !== "undefined" && console.log) {
    console.log("[AnswerCard] renderAssistantContent: card=", card ? "yes (mode=" + card.mode + ")" : "no");
  }
  if (card)
    return renderAnswerCard(card, isError, sourceConfidenceStrip);
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
    const overlayEl = overlay;
    closeBtn.addEventListener("click", () => {
      overlayEl.classList.remove("open");
      overlayEl.setAttribute("aria-hidden", "true");
    });
    overlayEl.addEventListener("click", (e) => {
      if (e.target === overlayEl) {
        overlayEl.classList.remove("open");
        overlayEl.setAttribute("aria-hidden", "true");
      }
    });
    document.body.appendChild(overlayEl);
  }
  if (!overlay)
    return;
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
    loadConfigHistory();
  }
  function closeDrawer() {
    drawer.classList.remove("open");
    drawerOverlay.classList.remove("open");
  }
  hamburger.addEventListener("click", openDrawer);
  drawerClose.addEventListener("click", closeDrawer);
  drawerOverlay.addEventListener("click", closeDrawer);
  const drawerSaveConfig = document.getElementById("drawerSaveConfig");
  const drawerLoadConfig = document.getElementById("drawerLoadConfig");
  if (drawerSaveConfig)
    drawerSaveConfig.addEventListener("click", saveChatConfig);
  if (drawerLoadConfig)
    drawerLoadConfig.addEventListener("click", loadChatConfig);
  function getElValue(id) {
    const el2 = getEl(id);
    if (!el2 || !("value" in el2))
      return "";
    return String(el2.value ?? "").trim();
  }
  function buildCopyTextForSection(section) {
    switch (section) {
      case "parser":
        return "Parser config:\npatient_keywords: " + getElValue("editParserKeywords") + "\ndecomposition_separators: " + getElValue("editParserSeparators");
      case "planner": {
        const sys = getElValue("editDecomposeSystem");
        const user = getElValue("editDecomposeUserTemplate");
        return "--- decompose_system ---\n" + sys + "\n\n--- decompose_user_template (placeholder: {message}) ---\n" + user;
      }
      case "first_gen": {
        const sys = getElValue("editFirstGenSystem");
        const user = getElValue("editFirstGenUser");
        return "--- first_gen_system ---\n" + sys + "\n\n--- first_gen_user_template (placeholders: {message}, {plan_summary}) ---\n" + user;
      }
      case "rag_answering": {
        const t = getElValue("editRagAnsweringUserTemplate");
        return "--- rag_answering_user_template (placeholders: {context}, {question}) ---\n" + t;
      }
      case "integrator": {
        const sys = getElValue("editIntegratorSystem");
        const user = getElValue("editIntegratorUserTemplate");
        const repair = getElValue("editIntegratorRepairSystem");
        return "--- integrator_system ---\n" + sys + "\n\n--- integrator_user_template (placeholder: {consolidator_input_json}) ---\n" + user + "\n\n--- integrator_repair_system ---\n" + repair;
      }
      case "consolidator": {
        const factualMax = getElValue("editConsolidatorFactualMax");
        const canonicalMin = getElValue("editConsolidatorCanonicalMin");
        const factual = getElValue("editIntegratorFactualSystem");
        const canonical = getElValue("editIntegratorCanonicalSystem");
        const blended = getElValue("editIntegratorBlendedSystem");
        return "consolidator_factual_max: " + factualMax + "\nconsolidator_canonical_min: " + canonicalMin + "\n\n--- integrator_factual_system ---\n" + factual + "\n\n--- integrator_canonical_system ---\n" + canonical + "\n\n--- integrator_blended_system ---\n" + blended;
      }
      default:
        return "";
    }
  }
  document.querySelectorAll(".config-copy-prompt-btn").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      const section = btn.getAttribute("data-copy-section");
      if (!section)
        return;
      const text = buildCopyTextForSection(section);
      if (!text)
        return;
      navigator.clipboard.writeText(text).then(() => {
        const label = btn;
        const orig = label.textContent;
        label.textContent = "Copied";
        label.disabled = true;
        window.setTimeout(() => {
          label.textContent = orig;
          label.disabled = false;
        }, 1500);
      });
    });
  });
  function buildPayloadForSection(section) {
    const payload = {};
    if (section === "parser") {
      const keywordsEl = getEl("editParserKeywords");
      const separatorsEl = getEl("editParserSeparators");
      payload.parser = {};
      if (keywordsEl?.value.trim())
        payload.parser.patient_keywords = keywordsEl.value.split(",").map((s) => s.trim()).filter(Boolean);
      if (separatorsEl?.value.trim())
        payload.parser.decomposition_separators = separatorsEl.value.split(/[,\n]/).map((s) => s.trim()).filter(Boolean);
      return Object.keys(payload.parser).length ? payload : null;
    }
    if (section === "planner") {
      payload.prompts = {
        decompose_system: getElValue("editDecomposeSystem"),
        decompose_user_template: getElValue("editDecomposeUserTemplate")
      };
      return payload;
    }
    if (section === "first_gen") {
      payload.prompts = {
        first_gen_system: getElValue("editFirstGenSystem"),
        first_gen_user_template: getElValue("editFirstGenUser")
      };
      return payload;
    }
    if (section === "rag_answering") {
      payload.prompts = { rag_answering_user_template: getElValue("editRagAnsweringUserTemplate") };
      return payload;
    }
    if (section === "integrator") {
      payload.prompts = {
        integrator_system: getElValue("editIntegratorSystem"),
        integrator_user_template: getElValue("editIntegratorUserTemplate"),
        integrator_repair_system: getElValue("editIntegratorRepairSystem")
      };
      return payload;
    }
    if (section === "consolidator") {
      const prompts = {
        integrator_factual_system: getElValue("editIntegratorFactualSystem"),
        integrator_canonical_system: getElValue("editIntegratorCanonicalSystem"),
        integrator_blended_system: getElValue("editIntegratorBlendedSystem")
      };
      const factualMax = getEl("editConsolidatorFactualMax");
      const canonicalMin = getEl("editConsolidatorCanonicalMin");
      if (factualMax?.value) {
        const v = parseFloat(factualMax.value);
        if (!Number.isNaN(v))
          prompts.consolidator_factual_max = v;
      }
      if (canonicalMin?.value) {
        const v = parseFloat(canonicalMin.value);
        if (!Number.isNaN(v))
          prompts.consolidator_canonical_min = v;
      }
      payload.prompts = prompts;
      return payload;
    }
    return null;
  }
  function saveSection(section, btn) {
    const pl = buildPayloadForSection(section);
    if (!pl || Object.keys(pl).length === 0) {
      loadChatConfig();
      return;
    }
    const origText = btn.textContent;
    btn.textContent = "Saving\u2026";
    btn.disabled = true;
    fetch(API_BASE + "/chat/config", {
      method: "PATCH",
      headers: { "Content-Type": "application/json", ...getAuthHeaders() },
      body: JSON.stringify(pl)
    }).then((r) => {
      if (!r.ok)
        throw new Error(String(r.status));
      return r.json();
    }).then((data) => {
      const shaEl = document.getElementById("configShaValue");
      if (shaEl && data.config_sha)
        shaEl.textContent = data.config_sha;
      btn.textContent = "Saved";
      loadChatConfig();
      loadConfigHistory();
      window.setTimeout(() => {
        btn.textContent = origText;
        btn.disabled = false;
      }, 1500);
    }).catch(() => {
      btn.textContent = origText ?? "Save";
      btn.disabled = false;
      loadChatConfig();
    });
  }
  document.querySelectorAll(".config-save-section-btn").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      const section = btn.getAttribute("data-save-section");
      if (!section)
        return;
      saveSection(section, btn);
    });
  });
  function getSampleInputForTest(section) {
    if (section === "planner") {
      const msg = document.getElementById("testPlannerMessage")?.value?.trim();
      return { message: msg || "What is prior authorization?" };
    }
    if (section === "first_gen") {
      const msg = document.getElementById("testFirstGenMessage")?.value?.trim();
      const plan = document.getElementById("testFirstGenPlanSummary")?.value?.trim();
      return { message: msg || "What is prior authorization?", plan_summary: plan || "One sub-question." };
    }
    if (section === "rag_answering") {
      const ctx = document.getElementById("testRagContext")?.value?.trim();
      const q = document.getElementById("testRagQuestion")?.value?.trim();
      return {
        context: ctx || "(Sample context: Prior authorization is required for certain services.)",
        question: q || "What is prior authorization?"
      };
    }
    if (section === "integrator" || section === "consolidator") {
      return {
        consolidator_input_json: JSON.stringify({
          user_message: "What is prior authorization?",
          subquestions: [{ id: "sq1", text: "What is prior authorization?" }],
          answers: [{ sq_id: "sq1", answer: "Prior authorization is a process where your doctor gets approval from your health plan before certain services." }]
        }, null, 2)
      };
    }
    return {};
  }
  function getPromptKeyForTest(section) {
    if (section === "integrator" || section === "consolidator") {
      const modeEl = document.getElementById(section === "integrator" ? "testIntegratorMode" : "testConsolidatorMode");
      return modeEl?.value || "integrator_factual";
    }
    return section;
  }
  function getResultElForTest(section) {
    const id = section === "planner" ? "testResultPlanner" : section === "first_gen" ? "testResultFirstGen" : section === "rag_answering" ? "testResultRagAnswering" : section === "integrator" ? "testResultIntegrator" : section === "consolidator" ? "testResultConsolidator" : null;
    return id ? document.getElementById(id) : null;
  }
  function runPromptTest(section, btn) {
    const promptKey = getPromptKeyForTest(section);
    const sampleInput = getSampleInputForTest(section);
    const resultEl = getResultElForTest(section);
    if (!resultEl)
      return;
    const origText = btn.textContent;
    btn.textContent = "Running\u2026";
    btn.disabled = true;
    resultEl.textContent = "";
    resultEl.className = "config-test-result";
    fetch(API_BASE + "/chat/config/test-prompt", {
      method: "POST",
      headers: { "Content-Type": "application/json", ...getAuthHeaders() },
      body: JSON.stringify({ prompt_key: promptKey, sample_input: sampleInput })
    }).then((r) => {
      if (!r.ok)
        throw new Error(String(r.status));
      return r.json();
    }).then((data) => {
      if (data.error) {
        resultEl.textContent = `Error: ${data.error}`;
        resultEl.classList.add("config-test-result--error");
      } else {
        const out = (data.output ?? "").trim();
        const meta = [data.model_used, data.duration_ms != null ? `${data.duration_ms} ms` : ""].filter(Boolean).join(" \xB7 ");
        resultEl.innerHTML = meta ? `<div class="config-test-meta">${escapeHtml4(meta)}</div><pre class="config-test-output">${escapeHtml4(out || "(empty)")}</pre>` : `<pre class="config-test-output">${escapeHtml4(out || "(empty)")}</pre>`;
        resultEl.classList.add("config-test-result--ok");
      }
      btn.textContent = origText ?? "Run test";
      btn.disabled = false;
    }).catch(() => {
      resultEl.textContent = "Request failed.";
      resultEl.classList.add("config-test-result--error");
      btn.textContent = origText ?? "Run test";
      btn.disabled = false;
    });
  }
  document.querySelectorAll(".config-run-test-btn").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      const section = btn.getAttribute("data-test-section");
      if (!section)
        return;
      runPromptTest(section, btn);
    });
  });
  loadChatConfig();
  const configSummaryRow = document.getElementById("configSummaryRow");
  const configPreferencesExpanded = document.getElementById("configPreferencesExpanded");
  const configPrefArrow = document.getElementById("configPrefArrow");
  const configHistorySection = document.getElementById("configHistorySection");
  const configTestSection = document.getElementById("configTestSection");
  const configNamedRunsSection = document.getElementById("configNamedRunsSection");
  if (configSummaryRow && configPreferencesExpanded && configPrefArrow) {
    configSummaryRow.addEventListener("click", () => {
      const show = !configPreferencesExpanded.classList.contains("show");
      configPreferencesExpanded.classList.toggle("show", show);
      configPrefArrow.textContent = show ? "\u25B2" : "\u25BC";
      configSummaryRow.setAttribute("aria-expanded", String(show));
      if (configHistorySection)
        configHistorySection.style.display = show ? "block" : "none";
      if (configTestSection)
        configTestSection.style.display = show ? "block" : "none";
      if (configNamedRunsSection)
        configNamedRunsSection.style.display = show ? "block" : "none";
      if (show) {
        loadConfigHistory();
        loadNamedRuns();
      }
    });
    configSummaryRow.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        configSummaryRow.click();
      }
    });
  }
  const editLlmModelSelect = document.getElementById("editLlmModel");
  const editLlmModelCustom = document.getElementById("editLlmModelCustom");
  if (editLlmModelSelect && editLlmModelCustom) {
    editLlmModelSelect.addEventListener("change", () => {
      const isCustom = editLlmModelSelect.value === "__custom__";
      editLlmModelCustom.style.display = isCustom ? "block" : "none";
      if (!isCustom)
        editLlmModelCustom.value = "";
    });
  }
  document.querySelectorAll(".config-section-title.config-section-toggle, .config-subsection-title.config-section-toggle").forEach((el2) => {
    el2.addEventListener("click", () => {
      const body = el2.nextElementSibling;
      if (body?.classList.contains("config-section-body") || body?.classList.contains("config-subsection-body")) {
        body.classList.toggle("collapsed");
        el2.classList.toggle("collapsed");
      }
    });
  });
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
  document.head.insertAdjacentHTML("beforeend", `<style id="mobius-prefs-styles">${PREFERENCES_MODAL_STYLES}</style>`);
  document.head.insertAdjacentHTML("beforeend", `<style id="mobius-user-menu-styles">${USER_MENU_STYLES}</style>`);
  const preferencesModal = createPreferencesModal(apiBase, auth, {
    onSave: () => auth.getUserProfile().then((u) => updateSidebarUser(u ?? null))
  });
  window.onOpenPreferences = () => preferencesModal.open();
  const userMenu = createUserMenu({
    auth,
    onOpenPreferences: () => preferencesModal.open(),
    onSignOut: () => updateSidebarUser(null),
    onSwitchAccount: () => {
      updateSidebarUser(null);
      authModal.open("login");
    }
  });
  auth.on((event, u) => {
    if (event === "login")
      updateSidebarUser(u);
    else if (event === "logout")
      updateSidebarUser(null);
  });
  const sidebarUser = document.getElementById("sidebarUser");
  sidebarUser?.addEventListener("click", () => {
    if (currentAuthUser)
      userMenu.show(sidebarUser);
    else
      authModal.open("login");
  });
  sidebarUser?.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      if (currentAuthUser)
        userMenu.show(sidebarUser);
      else
        authModal.open("login");
    }
  });
  auth.getCurrentUser().then((u) => updateSidebarUser(u ?? null));
  const MODEL_OPTIONS = ["gemini-2.5-flash", "gemini-2.0-flash", "llama3.1:8b"];
  function setEl(id, value, attr = "value") {
    const el2 = document.getElementById(id);
    if (!el2)
      return;
    if (attr === "value" && "value" in el2)
      el2.value = value;
    else
      el2.textContent = value;
  }
  function getEl(id) {
    return document.getElementById(id);
  }
  const CONFIG_FETCH_TIMEOUT_MS = 8e3;
  function loadChatConfig() {
    setEl("drawerSummaryLlm", "Loading\u2026", "textContent");
    const timeoutPromise = new Promise((_, reject) => {
      window.setTimeout(() => reject(new Error("CONFIG_TIMEOUT")), CONFIG_FETCH_TIMEOUT_MS);
    });
    const fetchPromise = fetch(API_BASE + "/chat/config").then((r) => {
      if (!r.ok)
        throw new Error(`Config failed (${r.status})`);
      return r.json();
    });
    Promise.race([fetchPromise, timeoutPromise]).then((data) => {
      const shaEl = document.getElementById("configShaValue");
      if (shaEl)
        shaEl.textContent = data.config_sha && data.config_sha.trim() ? data.config_sha : "\u2014";
      const llm = data.llm ?? {};
      const parser = data.parser ?? {};
      const p = data.prompts ?? {};
      const summaryLlm = (llm.provider ?? "\u2014") + " / " + (llm.model ?? "\u2014");
      const summaryParser = parser.patient_keywords?.length ? String(parser.patient_keywords.length) + " keywords" : "\u2014";
      setEl("drawerSummaryLlm", summaryLlm, "textContent");
      setEl("drawerSummaryParser", summaryParser, "textContent");
      const editProvider = getEl("editLlmProvider");
      const editModel = getEl("editLlmModel");
      const editModelCustom = getEl("editLlmModelCustom");
      const editTemp = getEl("editLlmTemperature");
      if (editProvider)
        editProvider.value = (llm.provider ?? "vertex").toLowerCase();
      const modelVal = (llm.model ?? "").trim();
      if (editModel && editModelCustom) {
        if (MODEL_OPTIONS.includes(modelVal)) {
          editModel.value = modelVal;
          editModelCustom.style.display = "none";
          editModelCustom.value = "";
        } else {
          editModel.value = "__custom__";
          editModelCustom.style.display = "block";
          editModelCustom.value = modelVal;
        }
      }
      if (editTemp)
        editTemp.value = llm.temperature != null ? String(llm.temperature) : "0.1";
      setEl("editParserKeywords", (parser.patient_keywords ?? []).join(", "));
      setEl("editParserSeparators", (parser.decomposition_separators ?? [" and ", " also ", " then "]).join(", "));
      setEl("editDecomposeSystem", p.decompose_system ?? "");
      setEl("editDecomposeUserTemplate", p.decompose_user_template ?? "");
      setEl("editFirstGenSystem", p.first_gen_system ?? "");
      setEl("editFirstGenUser", p.first_gen_user_template ?? "");
      setEl("editRagAnsweringUserTemplate", p.rag_answering_user_template ?? "");
      setEl("editIntegratorSystem", p.integrator_system ?? "");
      setEl("editIntegratorUserTemplate", p.integrator_user_template ?? "");
      setEl("editIntegratorRepairSystem", p.integrator_repair_system ?? "");
      const editFactualMax = getEl("editConsolidatorFactualMax");
      const editCanonicalMin = getEl("editConsolidatorCanonicalMin");
      if (editFactualMax)
        editFactualMax.value = p.consolidator_factual_max != null ? String(p.consolidator_factual_max) : "0.4";
      if (editCanonicalMin)
        editCanonicalMin.value = p.consolidator_canonical_min != null ? String(p.consolidator_canonical_min) : "0.6";
      setEl("editIntegratorFactualSystem", p.integrator_factual_system ?? "");
      setEl("editIntegratorCanonicalSystem", p.integrator_canonical_system ?? "");
      setEl("editIntegratorBlendedSystem", p.integrator_blended_system ?? "");
      loadSidebarLlm(data);
    }).catch((err) => {
      setEl("configShaValue", "\u2014", "textContent");
      const msg = err instanceof Error && err.message === "CONFIG_TIMEOUT" ? "Timeout \u2014 click Load from server to retry" : err instanceof Error ? err.message : "Failed to load";
      setEl("drawerSummaryLlm", msg, "textContent");
      setEl("drawerSummaryParser", "\u2014", "textContent");
    });
  }
  function saveChatConfig() {
    const editProvider = getEl("editLlmProvider");
    const editModel = getEl("editLlmModel");
    const editModelCustom = getEl("editLlmModelCustom");
    const editTemp = getEl("editLlmTemperature");
    const modelVal = editModel?.value === "__custom__" ? (editModelCustom?.value ?? "").trim() : (editModel?.value ?? "").trim();
    const payload = {};
    const llm = {};
    if (editProvider?.value.trim())
      llm.provider = editProvider.value.trim();
    if (modelVal)
      llm.model = modelVal;
    if (editTemp?.value.trim()) {
      const t = parseFloat(editTemp.value);
      if (!Number.isNaN(t))
        llm.temperature = t;
    }
    if (Object.keys(llm).length)
      payload.llm = llm;
    const keywordsEl = getEl("editParserKeywords");
    const separatorsEl = getEl("editParserSeparators");
    if (keywordsEl?.value.trim() || separatorsEl?.value.trim()) {
      payload.parser = {};
      if (keywordsEl?.value.trim())
        payload.parser.patient_keywords = keywordsEl.value.split(",").map((s) => s.trim()).filter(Boolean);
      if (separatorsEl?.value.trim())
        payload.parser.decomposition_separators = separatorsEl.value.split(/[,\n]/).map((s) => s.trim()).filter(Boolean);
    }
    const prompts = {};
    const promptIds = [
      ["editDecomposeSystem", "decompose_system"],
      ["editDecomposeUserTemplate", "decompose_user_template"],
      ["editFirstGenSystem", "first_gen_system"],
      ["editFirstGenUser", "first_gen_user_template"],
      ["editRagAnsweringUserTemplate", "rag_answering_user_template"],
      ["editIntegratorSystem", "integrator_system"],
      ["editIntegratorUserTemplate", "integrator_user_template"],
      ["editIntegratorRepairSystem", "integrator_repair_system"],
      ["editIntegratorFactualSystem", "integrator_factual_system"],
      ["editIntegratorCanonicalSystem", "integrator_canonical_system"],
      ["editIntegratorBlendedSystem", "integrator_blended_system"]
    ];
    for (const [id, key] of promptIds) {
      const el2 = getEl(id);
      if (el2 && "value" in el2 && el2.value !== void 0)
        prompts[key] = el2.value;
    }
    const factualMax = getEl("editConsolidatorFactualMax");
    const canonicalMin = getEl("editConsolidatorCanonicalMin");
    if (factualMax?.value) {
      const v = parseFloat(factualMax.value);
      if (!Number.isNaN(v))
        prompts.consolidator_factual_max = v;
    }
    if (canonicalMin?.value) {
      const v = parseFloat(canonicalMin.value);
      if (!Number.isNaN(v))
        prompts.consolidator_canonical_min = v;
    }
    if (Object.keys(prompts).length)
      payload.prompts = prompts;
    if (Object.keys(payload).length === 0) {
      loadChatConfig();
      return;
    }
    fetch(API_BASE + "/chat/config", {
      method: "PATCH",
      headers: { "Content-Type": "application/json", ...getAuthHeaders() },
      body: JSON.stringify(payload)
    }).then((r) => {
      if (!r.ok)
        throw new Error(String(r.status));
      return r.json();
    }).then((data) => {
      const shaEl = document.getElementById("configShaValue");
      if (shaEl && data.config_sha)
        shaEl.textContent = data.config_sha;
      loadChatConfig();
      loadConfigHistory();
    }).catch(() => {
      loadChatConfig();
    });
  }
  function loadConfigHistory() {
    const listEl = document.getElementById("configHistoryList");
    if (!listEl)
      return;
    fetch(API_BASE + "/chat/config/history?limit=50").then((r) => r.json()).then((entries) => {
      listEl.innerHTML = "";
      if (!entries.length) {
        listEl.textContent = "No history yet. Save config to create an entry.";
        return;
      }
      const formatDate = (iso) => {
        try {
          const d = new Date(iso);
          return Number.isNaN(d.getTime()) ? iso : d.toLocaleString();
        } catch {
          return iso;
        }
      };
      entries.forEach((entry) => {
        const row = document.createElement("div");
        row.className = "config-history-row";
        const shaShort = (entry.config_sha || "").slice(0, 8);
        row.innerHTML = `<span class="config-history-sha" title="${escapeHtml4(entry.config_sha)}">${escapeHtml4(shaShort)}</span> <span class="config-history-date">${escapeHtml4(formatDate(entry.created_at))}</span> <button type="button" class="config-history-btn config-history-view-btn">View</button> <button type="button" class="config-history-btn config-history-restore-btn">Restore</button>`;
        const viewBtn = row.querySelector(".config-history-view-btn");
        const restoreBtn = row.querySelector(".config-history-restore-btn");
        viewBtn?.addEventListener("click", () => {
          fetch(API_BASE + "/chat/config/history/" + encodeURIComponent(entry.config_sha)).then((r) => r.json()).then((data) => {
            const viewPanel = document.getElementById("configHistoryView");
            const viewBody = document.getElementById("configHistoryViewBody");
            if (viewPanel && viewBody) {
              viewBody.textContent = JSON.stringify(data.config ?? data, null, 2);
              viewPanel.style.display = "block";
            }
          }).catch(() => {
            const viewBody = document.getElementById("configHistoryViewBody");
            if (viewBody)
              viewBody.textContent = "Failed to load snapshot.";
            const viewPanel = document.getElementById("configHistoryView");
            if (viewPanel)
              viewPanel.style.display = "block";
          });
        });
        restoreBtn?.addEventListener("click", () => {
          if (!confirm("Restore this config version? Current form will be replaced."))
            return;
          fetch(API_BASE + "/chat/config/restore", {
            method: "POST",
            headers: { "Content-Type": "application/json", ...getAuthHeaders() },
            body: JSON.stringify({ config_sha: entry.config_sha })
          }).then((r) => {
            if (!r.ok)
              throw new Error(String(r.status));
            return r.json();
          }).then((data) => {
            const shaEl = document.getElementById("configShaValue");
            if (shaEl && data.config_sha)
              shaEl.textContent = data.config_sha;
            loadChatConfig();
            loadConfigHistory();
          }).catch(() => {
            loadConfigHistory();
          });
        });
        listEl.appendChild(row);
      });
    }).catch(() => {
      listEl.textContent = "Failed to load history.";
    });
  }
  function loadNamedRuns() {
    const listEl = document.getElementById("configNamedRunsList");
    if (!listEl)
      return;
    fetch(API_BASE + "/chat/config/test-runs?limit=50").then((r) => r.json()).then((entries) => {
      listEl.innerHTML = "";
      if (!entries.length) {
        listEl.textContent = "No named runs yet. Run a test with a version name to save one.";
        return;
      }
      const formatDate = (iso) => {
        try {
          const d = new Date(iso);
          return Number.isNaN(d.getTime()) ? iso : d.toLocaleString();
        } catch {
          return iso;
        }
      };
      entries.forEach((entry) => {
        const row = document.createElement("div");
        row.className = "config-named-run-row";
        const name = (entry.name || "").trim() || "Unnamed";
        const desc = (entry.description || "").trim();
        const shaShort = (entry.config_sha || "").slice(0, 8);
        row.innerHTML = `<span class="config-named-run-name" title="${escapeHtml4(name)}">${escapeHtml4(name)}</span>` + (desc ? ` <span class="config-named-run-desc">${escapeHtml4(desc)}</span>` : "") + ` <span class="config-named-run-meta">${escapeHtml4(shaShort)} \xB7 ${escapeHtml4(formatDate(entry.created_at))}</span> <button type="button" class="config-history-btn config-named-run-view-btn">View</button>`;
        const viewBtn = row.querySelector(".config-named-run-view-btn");
        viewBtn?.addEventListener("click", () => {
          fetch(API_BASE + "/chat/config/test-runs/" + encodeURIComponent(entry.id)).then((r) => {
            if (!r.ok)
              throw new Error("Not found");
            return r.json();
          }).then((data) => {
            const viewPanel = document.getElementById("configNamedRunView");
            const viewTitle = document.getElementById("configNamedRunViewTitle");
            const viewBody = document.getElementById("configNamedRunViewBody");
            if (!viewPanel || !viewBody)
              return;
            if (viewTitle)
              viewTitle.textContent = data.name || "Run";
            viewBody.innerHTML = "";
            const addBlock = (label, content) => {
              const block = document.createElement("div");
              block.className = "config-named-run-view-block";
              const h4 = document.createElement("h4");
              h4.textContent = label;
              const pre = document.createElement("pre");
              pre.textContent = content;
              block.appendChild(h4);
              block.appendChild(pre);
              viewBody.appendChild(block);
            };
            if (data.message != null)
              addBlock("Message", String(data.message));
            if (data.reply != null)
              addBlock("Reply", String(data.reply));
            if (data.config_sha != null)
              addBlock("Config SHA", String(data.config_sha));
            if (data.model_used != null)
              addBlock("Model", String(data.model_used));
            if (data.duration_ms != null)
              addBlock("Duration (ms)", String(data.duration_ms));
            if (data.stages != null && typeof data.stages === "object") {
              addBlock("Stages", JSON.stringify(data.stages, null, 2));
            }
            viewPanel.style.display = "block";
          }).catch(() => {
            const viewBody = document.getElementById("configNamedRunViewBody");
            if (viewBody)
              viewBody.textContent = "Failed to load run.";
            const viewPanel = document.getElementById("configNamedRunView");
            if (viewPanel)
              viewPanel.style.display = "block";
          });
        });
        listEl.appendChild(row);
      });
    }).catch(() => {
      listEl.textContent = "Failed to load named runs.";
    });
  }
  function escapeHtml4(s) {
    const div = document.createElement("div");
    div.textContent = s;
    return div.innerHTML;
  }
  const configHistoryViewClose = document.getElementById("configHistoryViewClose");
  const configHistoryView = document.getElementById("configHistoryView");
  if (configHistoryViewClose && configHistoryView) {
    configHistoryViewClose.addEventListener("click", () => {
      configHistoryView.style.display = "none";
    });
  }
  const configNamedRunViewClose = document.getElementById("configNamedRunViewClose");
  const configNamedRunView = document.getElementById("configNamedRunView");
  if (configNamedRunViewClose && configNamedRunView) {
    configNamedRunViewClose.addEventListener("click", () => {
      configNamedRunView.style.display = "none";
    });
  }
  const configTestRun = document.getElementById("configTestRun");
  const configTestMessage = document.getElementById("configTestMessage");
  const configTestVersionName = document.getElementById("configTestVersionName");
  const configTestDescription = document.getElementById("configTestDescription");
  const configTestSavedAs = document.getElementById("configTestSavedAs");
  const configTestResult = document.getElementById("configTestResult");
  if (configTestRun && configTestResult) {
    configTestRun.addEventListener("click", () => {
      const message = (configTestMessage?.value ?? "").trim() || "What is prior authorization?";
      const name = (configTestVersionName?.value ?? "").trim();
      const description = (configTestDescription?.value ?? "").trim();
      if (configTestSavedAs) {
        configTestSavedAs.style.display = "none";
        configTestSavedAs.textContent = "";
      }
      configTestResult.textContent = "Running test\u2026";
      configTestResult.classList.remove("config-test-error");
      fetch(API_BASE + "/chat/config/test", {
        method: "POST",
        headers: { "Content-Type": "application/json", ...getAuthHeaders() },
        body: JSON.stringify({ message, name: name || void 0, description: description || void 0 })
      }).then((r) => r.json().then((data) => ({ ok: r.ok, data }))).then(({ ok, data }) => {
        if (!ok)
          throw new Error(data.detail || "Test failed");
        if ((data.run_id || data.name) && configTestSavedAs) {
          configTestSavedAs.style.display = "block";
          configTestSavedAs.textContent = "Saved as: " + (data.name || data.run_id);
          loadNamedRuns();
        }
        const stages = data.stages;
        if (stages) {
          configTestResult.innerHTML = "";
          const wrap = document.createElement("div");
          wrap.className = "config-test-stages";
          const meta = document.createElement("div");
          meta.className = "config-test-meta";
          const metaParts = [];
          if (data.model_used != null)
            metaParts.push(`Model: ${data.model_used}`);
          if (data.config_sha != null)
            metaParts.push(`Config: ${data.config_sha}`);
          if (data.duration_ms != null)
            metaParts.push(`${data.duration_ms} ms`);
          meta.textContent = metaParts.join(" \xB7 ");
          wrap.appendChild(meta);
          const addSection = (title, content, collapsed = false) => {
            const block = document.createElement("div");
            block.className = "config-test-stage-block";
            const h4 = document.createElement("h4");
            h4.className = "config-section-title config-section-toggle config-test-stage-title";
            h4.setAttribute("role", "button");
            h4.setAttribute("tabindex", "0");
            h4.innerHTML = `${title} <span class="config-toggle-arrow">\u25BC</span>`;
            const body = document.createElement("div");
            body.className = "config-section-body" + (collapsed ? " collapsed" : "");
            if (collapsed)
              h4.classList.add("collapsed");
            const pre = document.createElement("pre");
            pre.textContent = content;
            pre.className = "config-test-stage-content";
            body.appendChild(pre);
            block.appendChild(h4);
            block.appendChild(body);
            h4.addEventListener("click", () => {
              body.classList.toggle("collapsed");
              h4.classList.toggle("collapsed");
            });
            h4.addEventListener("keydown", (e) => {
              if (e.key === "Enter" || e.key === " ") {
                e.preventDefault();
                h4.click();
              }
            });
            wrap.appendChild(block);
          };
          if (stages.planner != null) {
            addSection(
              "Planner (subquestions)",
              typeof stages.planner === "string" ? stages.planner : JSON.stringify(stages.planner, null, 2),
              false
            );
          }
          if (stages.rag_answers != null && stages.rag_answers.length > 0) {
            const lines = stages.rag_answers.map(
              (a) => `[${a.sq_id ?? "?"}] ${a.kind ?? "\u2014"}
  Q: ${(a.text ?? "").trim() || "\u2014"}
  A: ${(a.answer_preview ?? "").trim() || "\u2014"}`
            );
            addSection("RAG answers (per subquestion)", lines.join("\n\n"), false);
          }
          if (stages.integrator_raw != null) {
            addSection("Integrator (raw output)", stages.integrator_raw, true);
          }
          if (stages.final_answer != null) {
            addSection("Final answer", stages.final_answer, false);
          }
          configTestResult.appendChild(wrap);
        } else {
          const lines = [];
          if (data.reply != null)
            lines.push(String(data.reply));
          if (data.model_used != null)
            lines.push(`
Model: ${data.model_used}`);
          if (data.config_sha != null)
            lines.push(`Config: ${data.config_sha}`);
          if (data.duration_ms != null)
            lines.push(`Duration: ${data.duration_ms} ms`);
          configTestResult.textContent = lines.join("\n") || "No reply.";
        }
      }).catch((err) => {
        configTestResult.textContent = "Test failed: " + (err?.message || String(err));
        configTestResult.classList.add("config-test-error");
      });
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
        if (onThinking)
          onThinking("Reconnecting\u2026");
        pollResponse(correlationId, onThinking, onStreamingMessage).then(resolve).catch(reject);
      };
    });
  }
  const chatEmpty = document.getElementById("chatEmpty");
  let threadId = null;
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
    const { el: progressStackEl, addLine: progressAddLine, replaceWithLine: progressReplaceWithLine } = renderProgressStack();
    turnWrap.appendChild(progressStackEl);
    scrollToBottom(messagesEl);
    let firstThinking = true;
    function onThinkingLine(line) {
      if (firstThinking) {
        firstThinking = false;
        progressReplaceWithLine(line);
      } else {
        progressAddLine(line);
      }
      scrollToBottom(messagesEl);
    }
    function onStreamingMessage(_text) {
      scrollToBottom(messagesEl);
    }
    const body = { message };
    if (threadId != null && threadId !== "")
      body.thread_id = threadId;
    fetch(API_BASE + "/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json", ...getAuthHeaders() },
      body: JSON.stringify(body)
    }).then((r) => r.json()).then((postData) => {
      progressAddLine("Request sent. Waiting for worker\u2026");
      if (postData.thread_id != null && postData.thread_id !== "")
        threadId = postData.thread_id;
      const correlationId = postData.correlation_id;
      return streamResponse(correlationId, onThinkingLine, onStreamingMessage).then(
        (streamData) => ({ streamData, correlationId })
      );
    }).then(({ streamData: data, correlationId }) => {
      if (typeof console !== "undefined" && console.log) {
        console.log("[AnswerCard] stream completed, processing final message\u2026");
      }
      const fullMessage = data.message ?? "(No message)";
      const { body: body2, sources } = parseMessageAndSources(fullMessage);
      if (typeof console !== "undefined" && console.log) {
        console.log("[AnswerCard] fullMessage length:", fullMessage.length, "starts:", (fullMessage || "").slice(0, 120));
        console.log("[AnswerCard] body length:", (body2 || "").length, "starts:", (body2 || "").slice(0, 120));
      }
      progressStackEl.remove();
      const finalBody = body2 || "(No response)";
      const parsedCard = tryParseAnswerCard(finalBody);
      if (typeof console !== "undefined" && console.log) {
        console.log("[AnswerCard] tryParseAnswerCard:", parsedCard ? "card (mode=" + parsedCard.mode + ")" : "null");
      }
      const stripValue = data.source_confidence_strip ?? "unverified";
      const contentEl = renderAssistantContent(finalBody, !!data.llm_error, stripValue);
      turnWrap.appendChild(contentEl);
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
  const composerOptionsBtn = document.getElementById("composerOptions");
  const composerWrap = document.querySelector(".composer-wrap");
  let composerOptionsMenu = null;
  function closeComposerOptionsMenu() {
    if (composerOptionsMenu)
      composerOptionsMenu.hidden = true;
  }
  function openComposerOptionsMenu() {
    if (!composerOptionsMenu && composerWrap) {
      composerOptionsMenu = document.createElement("div");
      composerOptionsMenu.className = "composer-options-menu";
      composerOptionsMenu.setAttribute("role", "menu");
      composerOptionsMenu.hidden = true;
      composerOptionsMenu.innerHTML = `
        <button type="button" class="composer-option-item" data-action="new-chat" role="menuitem">New chat</button>
        <button type="button" class="composer-option-item" data-action="chat-config" role="menuitem">Chat config</button>
        <button type="button" class="composer-option-item" data-action="preferences" role="menuitem">Preferences</button>
      `;
      composerOptionsMenu.querySelectorAll(".composer-option-item").forEach((item) => {
        item.addEventListener("click", () => {
          const action = item.dataset.action;
          closeComposerOptionsMenu();
          if (action === "new-chat") {
            threadId = null;
            if (messagesEl && chatEmpty) {
              messagesEl.innerHTML = "";
              messagesEl.appendChild(chatEmpty);
              chatEmpty.classList.remove("hidden");
            }
            loadSidebarHistory();
          } else if (action === "chat-config")
            openDrawer();
          else if (action === "preferences")
            preferencesModal.open();
        });
      });
      composerWrap.appendChild(composerOptionsMenu);
    }
    if (composerOptionsMenu)
      composerOptionsMenu.hidden = false;
  }
  composerOptionsBtn?.addEventListener("click", (e) => {
    e.stopPropagation();
    if (composerOptionsMenu?.hidden !== false)
      openComposerOptionsMenu();
    else
      closeComposerOptionsMenu();
  });
  document.addEventListener("click", (e) => {
    const target = e.target;
    if (composerOptionsMenu && !composerOptionsMenu.contains(target) && !composerOptionsBtn?.contains(target))
      closeComposerOptionsMenu();
  });
  const btnNewChat = document.getElementById("btnNewChat");
  btnNewChat?.addEventListener("click", () => {
    threadId = null;
    if (messagesEl && chatEmpty) {
      messagesEl.innerHTML = "";
      messagesEl.appendChild(chatEmpty);
      chatEmpty.classList.remove("hidden");
    }
    loadSidebarHistory();
  });
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
  function initWelcomeSubtitleRotator() {
    const el2 = document.getElementById("welcomeRotator");
    if (!el2)
      return;
    const lines = [
      "Some questions should take seconds, not three phone calls.",
      "If it takes more than one portal, it probably shouldn\u2019t.",
      "You shouldn\u2019t need a PDF to keep care moving.",
      "Most delays aren\u2019t clinical\u2014they\u2019re administrative.",
      "If the answer exists somewhere, it shouldn\u2019t be hard to find.",
      "Not every problem needs a meeting. Some just need the right answer.",
      "Paperwork has a way of expanding to fill the day.",
      "The hard part is rarely the patient\u2014it\u2019s everything around them.",
      "Finding the right policy shouldn\u2019t feel like detective work.",
      "Care works better when answers show up on time."
    ];
    let idx = Math.max(0, lines.indexOf((el2.textContent || "").trim()));
    let timer = null;
    const fadeMs = 180;
    const setLine = (s) => {
      el2.classList.add("is-fading");
      window.setTimeout(() => {
        el2.textContent = s;
        el2.classList.remove("is-fading");
      }, fadeMs);
    };
    const scheduleNext = () => {
      const ms = 1e4 + Math.floor(Math.random() * 5001);
      timer = window.setTimeout(() => {
        idx = (idx + 1) % lines.length;
        setLine(lines[idx]);
        scheduleNext();
      }, ms);
    };
    const stop = () => {
      if (timer == null)
        return;
      window.clearTimeout(timer);
      timer = null;
    };
    el2.addEventListener("mouseenter", stop);
    el2.addEventListener("mouseleave", () => {
      if (timer != null)
        return;
      scheduleNext();
    });
    el2.addEventListener("focusin", stop);
    el2.addEventListener("focusout", () => {
      if (timer != null)
        return;
      scheduleNext();
    });
    scheduleNext();
  }
  function initReleaseUpdatesCarousel() {
    const container = document.querySelector(".landing-updates");
    if (!container)
      return;
    const items = Array.from(container.querySelectorAll(".landing-update"));
    if (items.length <= 1)
      return;
    container.classList.add("landing-updates--carousel");
    items.forEach((el2) => {
      el2.style.position = "relative";
      el2.style.inset = "auto";
      el2.style.opacity = "1";
      el2.style.pointerEvents = "auto";
    });
    const maxH = Math.max(...items.map((el2) => el2.getBoundingClientRect().height));
    if (Number.isFinite(maxH) && maxH > 0)
      container.style.minHeight = `${Math.ceil(maxH)}px`;
    items.forEach((el2) => {
      el2.style.position = "";
      el2.style.inset = "";
      el2.style.opacity = "";
      el2.style.pointerEvents = "";
    });
    let idx = 0;
    const show = (i) => {
      items.forEach((el2, j) => el2.classList.toggle("is-active", j === i));
    };
    show(idx);
    let timer = null;
    const intervalMs = 7e3;
    const start = () => {
      if (timer != null)
        return;
      timer = window.setInterval(() => {
        idx = (idx + 1) % items.length;
        show(idx);
      }, intervalMs);
    };
    const stop = () => {
      if (timer == null)
        return;
      window.clearInterval(timer);
      timer = null;
    };
    container.addEventListener("mouseenter", stop);
    container.addEventListener("mouseleave", start);
    container.addEventListener("focusin", stop);
    container.addEventListener("focusout", (e) => {
      const next = e.relatedTarget;
      if (next && container.contains(next))
        return;
      start();
    });
    start();
  }
  document.querySelectorAll(".landing-try-link").forEach((chip) => {
    chip.addEventListener("click", () => {
      window.activePlanContext = "Sunshine Health";
      const query = chip.getAttribute("data-query");
      if (query != null) {
        inputEl.value = query;
        updateSendState();
        inputEl.focus();
      }
    });
  });
  initReleaseUpdatesCarousel();
  initWelcomeSubtitleRotator();
  initSidebarCollapsibles();
  updateSendState();
  loadSidebarHistory();
  loadSidebarLlm();
}
run();
