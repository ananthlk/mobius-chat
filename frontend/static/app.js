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
    ai_experience_level: pref.ai_experience_level || "beginner",
    // Defaults must mirror the backend column defaults (confirm_first) or
    // the modal shows — and then saves — a preference the user never chose.
    autonomy_routine_tasks: pref.autonomy_routine_tasks || "confirm_first",
    autonomy_sensitive_tasks: pref.autonomy_sensitive_tasks || "confirm_first",
    org_memberships: u.org_memberships || []
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
      const isNewUser = data.is_new_user !== false;
      if (data.user) {
        const profile = normalizeUser(data.user);
        await this.storeUserProfile(profile);
        this.emit("login", profile);
        return { success: true, user: profile, isNewUser };
      }
      this.emit("login");
      return { success: true, isNewUser };
    } catch (e) {
      console.error("[AuthService] register:", e);
      return { success: false, error: "Network error" };
    }
  }
  async loginWithGoogle(idToken, tenantId) {
    if (!idToken)
      return { success: false, error: "Missing Google ID token" };
    try {
      const res = await fetch(`${this.apiBase}/auth/google`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ id_token: idToken, tenant_id: tenantId })
      });
      let data = {};
      try {
        data = await res.json();
      } catch {
      }
      const dataTyped = data;
      if (!res.ok || !dataTyped.ok) {
        return { success: false, error: dataTyped.error || "Google sign-in failed" };
      }
      await this.storeTokens({
        access_token: dataTyped.access_token,
        refresh_token: dataTyped.refresh_token,
        expires_in: dataTyped.expires_in || 3600
      });
      const isNewUser = !!dataTyped.is_new_user;
      if (dataTyped.user) {
        const profile = normalizeUser(dataTyped.user);
        await this.storeUserProfile(profile);
        this.emit("login", profile);
        return { success: true, user: profile, isNewUser };
      }
      this.emit("login");
      return { success: true, isNewUser };
    } catch (e) {
      console.error("[AuthService] loginWithGoogle:", e);
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
var GIS_SCRIPT_SRC = "https://accounts.google.com/gsi/client";
var scriptPromise = null;
function loadGisScript() {
  if (window.google?.accounts?.id)
    return Promise.resolve();
  if (scriptPromise)
    return scriptPromise;
  scriptPromise = new Promise((resolve, reject) => {
    const existing = document.querySelector(`script[src="${GIS_SCRIPT_SRC}"]`);
    if (existing) {
      existing.addEventListener("load", () => resolve());
      existing.addEventListener("error", () => reject(new Error("Failed to load Google Identity Services")));
      if (window.google?.accounts?.id)
        resolve();
      return;
    }
    const s = document.createElement("script");
    s.src = GIS_SCRIPT_SRC;
    s.async = true;
    s.defer = true;
    s.onload = () => resolve();
    s.onerror = () => {
      scriptPromise = null;
      reject(new Error("Failed to load Google Identity Services"));
    };
    document.head.appendChild(s);
  });
  return scriptPromise;
}
async function getGoogleIdToken(clientId) {
  if (!clientId)
    throw new Error("Google client ID not configured");
  await loadGisScript();
  const accountsId = window.google?.accounts?.id;
  if (!accountsId)
    throw new Error("Google Identity Services unavailable");
  return new Promise((resolve, reject) => {
    let settled = false;
    accountsId.initialize({
      client_id: clientId,
      ux_mode: "popup",
      auto_select: false,
      cancel_on_tap_outside: true,
      callback: (resp) => {
        if (settled)
          return;
        settled = true;
        if (resp && typeof resp.credential === "string" && resp.credential) {
          resolve(resp.credential);
        } else {
          reject(new Error("Google did not return a credential"));
        }
      }
    });
    accountsId.prompt((notification) => {
      const n = notification;
      try {
        if (n.isDismissedMoment?.() || n.isSkippedMoment?.() || n.isNotDisplayed?.()) {
          if (settled)
            return;
          settled = true;
          reject(new Error("Google sign-in was dismissed"));
        }
      } catch {
      }
    });
  });
}
var hostEl = null;
function ensureHost() {
  if (hostEl && hostEl.isConnected)
    return hostEl;
  const existing = document.querySelector(".mobius-auth-toast-host");
  if (existing) {
    hostEl = existing;
    return hostEl;
  }
  const el2 = document.createElement("div");
  el2.className = "mobius-auth-toast-host";
  document.body.appendChild(el2);
  hostEl = el2;
  return el2;
}
function showToast(message, variant = "info", durationMs = 2800) {
  if (!message)
    return;
  const hostToast = window.showToast;
  if (typeof hostToast === "function") {
    try {
      hostToast(message);
      return;
    } catch {
    }
  }
  const host = ensureHost();
  const toast = document.createElement("div");
  toast.className = `mobius-auth-toast mobius-auth-toast--${variant}`;
  toast.setAttribute("role", variant === "error" ? "alert" : "status");
  toast.textContent = message;
  host.appendChild(toast);
  void toast.offsetWidth;
  toast.classList.add("open");
  const remove = () => {
    toast.classList.remove("open");
    setTimeout(() => toast.remove(), 200);
  };
  setTimeout(remove, durationMs);
}
function escapeHtml(s) {
  const div = document.createElement("div");
  div.textContent = s;
  return div.innerHTML;
}
function createAuthModal(options) {
  const { auth, showOAuth = true, demoEmail, googleClientId, onSuccess, onClose } = options;
  let currentUser = null;
  let pendingWelcomeName = null;
  const overlay = document.createElement("div");
  overlay.className = "mobius-auth-overlay";
  overlay.setAttribute("aria-hidden", "true");
  overlay.dataset.tourId = "modal-auth";
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
        ${hasOAuth ? `
          <div class="mobius-auth-divider"><span>or continue with</span></div>
          <div class="mobius-auth-oauth">
            <button type="button" class="mobius-auth-oauth-btn" data-provider="google">Google</button>
            <button type="button" class="mobius-auth-oauth-btn" data-provider="microsoft">Microsoft</button>
            <button type="button" class="mobius-auth-sso-btn">Enterprise SSO</button>
          </div>
        ` : ""}
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
        <div class="mobius-auth-confirm" data-role="logout-confirm" style="display:none">
          <p class="mobius-auth-confirm-text">Sign out of Mobius?</p>
          <div class="mobius-auth-confirm-actions">
            <button type="button" class="mobius-auth-btn mobius-auth-btn-secondary" data-confirm="cancel">Cancel</button>
            <button type="button" class="mobius-auth-btn mobius-auth-btn-danger" data-confirm="ok">Sign out</button>
          </div>
        </div>
      </div>
    `;
    const welcomeName = pendingWelcomeName || currentUser?.first_name || currentUser?.greeting_name || "";
    const welcomeHtml = `
      <button type="button" class="mobius-auth-close" aria-label="Close">&times;</button>
      <h2 id="${titleId}" class="mobius-auth-title">Welcome to Mobius${welcomeName ? `, ${escapeHtml(welcomeName)}` : ""}</h2>
      <div class="mobius-auth-form mobius-auth-welcome" data-mode="welcome">
        <div class="mobius-auth-welcome-emoji" aria-hidden="true">\u{1F44B}</div>
        <p class="mobius-auth-welcome-body">
          Thanks for signing up. Take a minute to set your preferences so
          Mobius can tailor itself to how you work \u2014 or skip and do it later.
        </p>
        <button type="button" class="mobius-auth-btn mobius-auth-welcome-btn">Set up preferences</button>
        <button type="button" class="mobius-auth-btn mobius-auth-btn-secondary mobius-auth-welcome-skip-btn">Skip for now</button>
      </div>
    `;
    panel.innerHTML = mode === "login" ? loginHtml : mode === "signup" ? signupHtml : mode === "welcome" ? welcomeHtml : accountHtml;
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
          showToast(`Signed in as ${result.user.greeting_name || result.user.email || "user"}`, "success");
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
        const provider = btn.getAttribute("data-provider") || "";
        btn.addEventListener("click", () => {
          if (provider === "google" && googleClientId) {
            void doGoogleSignIn(btn, errorEl);
            return;
          }
          showToast("Coming soon", "info");
        });
      });
    }
    async function doGoogleSignIn(btn, errorEl) {
      if (!googleClientId)
        return;
      const originalText = btn.textContent;
      btn.disabled = true;
      btn.textContent = "Connecting\u2026";
      if (errorEl)
        errorEl.style.display = "none";
      try {
        const idToken = await getGoogleIdToken(googleClientId);
        const result = await auth.loginWithGoogle(idToken);
        if (!result.success) {
          if (errorEl) {
            errorEl.textContent = result.error || "Google sign-in failed";
            errorEl.style.display = "block";
          } else {
            showToast(result.error || "Google sign-in failed", "error");
          }
          return;
        }
        if (result.isNewUser) {
          pendingWelcomeName = result.user?.first_name || result.user?.greeting_name || null;
          showToast("Account created", "success");
          if (result.user)
            onSuccess?.(result.user);
          render("welcome");
          return;
        }
        showToast(`Signed in as ${result.user?.greeting_name || result.user?.email || "user"}`, "success");
        if (result.user)
          onSuccess?.(result.user);
        close();
      } catch (e) {
        const msg = e instanceof Error ? e.message : "Google sign-in failed";
        if (!/dismissed|skipped|not displayed/i.test(msg)) {
          if (errorEl) {
            errorEl.textContent = msg;
            errorEl.style.display = "block";
          } else {
            showToast(msg, "error");
          }
        }
      } finally {
        btn.disabled = false;
        btn.textContent = originalText || "Google";
      }
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
          pendingWelcomeName = result.user.first_name || firstNameInput?.value?.trim() || result.user.greeting_name || null;
          showToast("Account created", "success");
          onSuccess?.(result.user);
          render("welcome");
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
      panel.querySelectorAll(".mobius-auth-oauth-btn, .mobius-auth-sso-btn").forEach((btn) => {
        const provider = btn.getAttribute("data-provider") || "";
        btn.addEventListener("click", () => {
          if (provider === "google" && googleClientId) {
            void doGoogleSignIn(btn, errorEl);
            return;
          }
          showToast("Coming soon", "info");
        });
      });
    }
    if (mode === "welcome") {
      panel.querySelector(".mobius-auth-welcome-prefs-btn")?.addEventListener("click", () => {
        pendingWelcomeName = null;
        close();
        const fn = window.onOpenPreferences;
        if (typeof fn === "function") {
          fn();
        }
      });
      panel.querySelector(".mobius-auth-welcome-btn")?.addEventListener("click", () => {
        pendingWelcomeName = null;
        close();
        const open2 = window.onOpenPreferences;
        if (typeof open2 === "function") {
          try {
            open2();
          } catch (e) {
            console.error("[AuthModal] onOpenPreferences threw:", e);
          }
        }
      });
      panel.querySelector(".mobius-auth-welcome-skip-btn")?.addEventListener("click", () => {
        pendingWelcomeName = null;
        close();
      });
    }
    if (mode === "account") {
      const logoutBtn = panel.querySelector(".mobius-auth-logout-btn");
      const confirmEl = panel.querySelector('[data-role="logout-confirm"]');
      logoutBtn?.addEventListener("click", () => {
        if (!confirmEl)
          return;
        confirmEl.style.display = "block";
        logoutBtn.disabled = true;
      });
      confirmEl?.querySelector('[data-confirm="cancel"]')?.addEventListener("click", () => {
        confirmEl.style.display = "none";
        if (logoutBtn)
          logoutBtn.disabled = false;
      });
      confirmEl?.querySelector('[data-confirm="ok"]')?.addEventListener("click", async () => {
        await auth.logout();
        updateUser(null);
        showToast("Signed out", "info");
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
    organization: profile.org_memberships?.[0]?.display_name ?? "",
    preferred_name: profile.preferred_name ?? "",
    timezone: profile.timezone ?? "America/New_York",
    activities: profile.activities ?? [],
    tone: profile.tone ?? "professional",
    greeting_enabled: profile.greeting_enabled !== false,
    ai_experience_level: profile.ai_experience_level ?? "beginner",
    // Fallbacks must match the backend column defaults (confirm_first) —
    // a mismatched fallback silently rewrites the stored preference the
    // next time the user saves without touching this control.
    autonomy_routine_tasks: profile.autonomy_routine_tasks ?? "confirm_first",
    autonomy_sensitive_tasks: profile.autonomy_sensitive_tasks ?? "confirm_first"
  };
}
function createPreferencesModal(apiBase, auth, options) {
  const base = apiBase.replace(/\/$/, "");
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
    const token = await auth.getAccessToken();
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
    const profile = await auth.getCurrentUser();
    const initialPrefs = profile ? prefsFromProfile(profile) : prefsFromProfile({});
    const prefs = { ...initialPrefs, activities: [...initialPrefs.activities ?? []] };
    const selectedActivities = [...prefs.activities ?? []];
    const modal = document.createElement("div");
    modal.className = "mobius-prefs-modal";
    modal.dataset.tourId = "modal-preferences";
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
      const sensitive = prefs.autonomy_sensitive_tasks ?? "confirm_first";
      const experience = prefs.ai_experience_level ?? "beginner";
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
            <label class="mobius-prefs-label">Organization</label>
            <input type="text" class="mobius-prefs-input" id="pref-organization"
                   value="${escapeHtml2(prefs.organization ?? "")}"
                   placeholder="Your organization's name" />
            <p class="mobius-prefs-desc">Matched against the Mobius org registry when you save.</p>
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
                <span>Just guide me, I'll do it myself</span>
              </label>
            </div>
          </div>
          <div class="mobius-prefs-section">
            <label class="mobius-prefs-label">How familiar are you with AI assistants?</label>
            <div class="mobius-prefs-options">
              <label class="mobius-prefs-option ${experience === "beginner" ? "selected" : ""}">
                <input type="radio" name="experience" value="beginner" ${experience === "beginner" ? "checked" : ""} />
                <span>New to this \u2014 explain as you go</span>
              </label>
              <label class="mobius-prefs-option ${experience === "regular" ? "selected" : ""}">
                <input type="radio" name="experience" value="regular" ${experience === "regular" ? "checked" : ""} />
                <span>Comfortable \u2014 skip the basics</span>
              </label>
              <label class="mobius-prefs-option ${experience === "expert" ? "selected" : ""}">
                <input type="radio" name="experience" value="expert" ${experience === "expert" ? "checked" : ""} />
                <span>Expert \u2014 just the tradeoffs</span>
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
          const t = await auth.getAccessToken();
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
              organization: prefs.organization,
              preferred_name: prefs.preferred_name,
              timezone: prefs.timezone,
              activities: selectedActivities,
              tone: prefs.tone,
              greeting_enabled: prefs.greeting_enabled,
              ai_experience_level: prefs.ai_experience_level,
              autonomy_routine_tasks: prefs.autonomy_routine_tasks,
              autonomy_sensitive_tasks: prefs.autonomy_sensitive_tasks
            })
          });
          if (response.ok) {
            prefs.activities = [...selectedActivities];
            await auth.getCurrentUser();
            options?.onSave?.(prefs);
            showToast("Preferences saved", "success");
            close();
          } else {
            const errData = await response.json().catch(() => ({}));
            console.error("[PreferencesModal] Error saving preferences:", errData);
            showToast("Failed to save preferences", "error");
          }
        } catch (error) {
          console.error("[PreferencesModal] Error saving preferences:", error);
          showToast("Failed to save preferences", "error");
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
      modal.querySelector("#pref-organization")?.addEventListener("input", (e) => {
        prefs.organization = e.target.value;
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
      ["routine", "sensitive", "tone", "experience"].forEach((name) => {
        modal.querySelectorAll(`input[name="${name}"]`).forEach((r) => {
          r.addEventListener("change", (e) => {
            const value = e.target.value;
            if (name === "routine")
              prefs.autonomy_routine_tasks = value;
            if (name === "sensitive")
              prefs.autonomy_sensitive_tasks = value;
            if (name === "experience")
              prefs.ai_experience_level = value;
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
  const { auth, onOpenPreferences, onSignOut, onSwitchAccount } = options;
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
    const user = await auth.getUserProfile();
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
      <button type="button" class="mobius-user-menu-item" data-action="preferences" data-tour-id="menu-preferences">
        <svg viewBox="0 0 24 24" width="14" height="14" class="mobius-user-menu-icon"><path fill="currentColor" d="M19.14 12.94c.04-.31.06-.63.06-.94 0-.31-.02-.63-.06-.94l2.03-1.58c.18-.14.23-.41.12-.61l-1.92-3.32c-.12-.22-.37-.29-.59-.22l-2.39.96c-.5-.38-1.03-.7-1.62-.94l-.36-2.54c-.04-.24-.24-.41-.48-.41h-3.84c-.24 0-.43.17-.47.41l-.36 2.54c-.59.24-1.13.57-1.62.94l-2.39-.96c-.22-.08-.47 0-.59.22L2.74 8.87c-.12.21-.08.47.12.61l2.03 1.58c-.04.31-.06.63-.06.94s.02.63.06.94l-2.03 1.58c-.18.14-.23.41-.12.61l1.92 3.32c.12.22.37.29.59.22l2.39-.96c.5.38 1.03.7 1.62.94l.36 2.54c.05.24.24.41.48.41h3.84c.24 0 .44-.17.47-.41l.36-2.54c.59-.24 1.13-.56 1.62-.94l2.39.96c.22.08.47 0 .59-.22l1.92-3.32c.12-.22.07-.47-.12-.61l-2.01-1.58zM12 15.6c-1.98 0-3.6-1.62-3.6-3.6s1.62-3.6 3.6-3.6 3.6 1.62 3.6 3.6-1.62 3.6-3.6 3.6z"/></svg>
        <span>My Preferences</span>
      </button>
      <button type="button" class="mobius-user-menu-item" data-action="switch" data-tour-id="menu-sign-in">
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
          await auth.logout();
          onSignOut?.();
        } else if (action === "switch") {
          await auth.logout();
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
  background: var(--mobius-bg-primary, #fafbfc);
  border-radius: var(--mobius-radius-md, 12px);
  padding: 1.5rem;
  max-width: 360px;
  width: 90%;
  box-shadow: var(--mobius-shadow-lg, 0 8px 24px rgba(0,0,0,0.08));
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
  color: var(--mobius-text-muted, #64748b);
  line-height: 1;
  padding: 0;
}
.mobius-auth-close:hover { color: var(--mobius-text-primary, #1a1d21); }
.mobius-auth-title { margin: 0 0 1rem; font-size: var(--mobius-text-lg, 1.125rem); }
.mobius-auth-form input,
.mobius-auth-form .mobius-auth-btn {
  display: block;
  width: 100%;
  margin-bottom: 0.75rem;
  padding: 0.5rem 0.75rem;
  font-size: var(--mobius-text-base, 0.9375rem);
  border: 1px solid var(--mobius-border, #e2e8f0);
  border-radius: var(--mobius-radius-base, 8px);
}
.mobius-auth-form .mobius-auth-btn {
  background: var(--mobius-accent, #3b82f6);
  color: var(--mobius-accent-text, white);
  border: none;
  cursor: pointer;
  font-weight: 500;
}
.mobius-auth-form .mobius-auth-btn:hover { background: var(--mobius-accent-hover, #2563eb); }
.mobius-auth-error { font-size: var(--mobius-text-sm, 0.8125rem); color: var(--mobius-error, #dc2626); margin-top: 0.5rem; }
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
  font-size: var(--mobius-text-xs, 0.7rem);
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
  border-radius: var(--mobius-radius-sm, 6px);
  font-size: var(--mobius-text-xs, 0.7rem);
  cursor: pointer;
}
.mobius-auth-switch {
  margin: 1rem 0 0;
  font-size: var(--mobius-text-sm, 0.8125rem);
  color: var(--mobius-text-muted, #64748b);
}
.mobius-auth-switch-btn {
  background: none;
  border: none;
  color: var(--mobius-accent, #3b82f6);
  cursor: pointer;
  padding: 0;
  font-size: inherit;
}
.mobius-auth-switch-btn:hover { text-decoration: underline; }
.mobius-auth-user-info { margin: 0 0 1rem; font-size: var(--mobius-text-sm, 0.8125rem); }
.mobius-auth-prefs-link {
  display: block;
  margin-bottom: 1rem;
  color: var(--mobius-accent, #3b82f6);
  font-size: var(--mobius-text-sm, 0.8125rem);
}

/* Welcome panel (post-signup) */
.mobius-auth-welcome { text-align: center; padding: 0.5rem 0 0; }
.mobius-auth-welcome-emoji { font-size: 2rem; margin-bottom: 0.5rem; }
.mobius-auth-welcome-body {
  margin: 0 0 1rem;
  font-size: var(--mobius-text-sm, 0.8125rem);
  color: var(--mobius-text-muted, #64748b);
  line-height: 1.5;
}

/* Inline confirm (logout, etc.) */
.mobius-auth-confirm {
  margin-top: 0.5rem;
  padding: 0.75rem;
  background: var(--mobius-bg-muted, #f3f4f6);
  border: 1px solid var(--mobius-border, #e2e8f0);
  border-radius: var(--mobius-radius-base, 8px);
}
.mobius-auth-confirm-text {
  margin: 0 0 0.75rem;
  font-size: var(--mobius-text-sm, 0.8125rem);
}
.mobius-auth-confirm-actions { display: flex; gap: 8px; }
.mobius-auth-confirm-actions .mobius-auth-btn { margin: 0; flex: 1; }
/* Generic secondary-button style \u2014 quiet alternate when a primary CTA
 * sits next to it (welcome panel "Skip for now", confirm "Cancel"). */
.mobius-auth-btn-secondary {
  background: white;
  color: var(--mobius-text-primary, #1a1d21);
  border: 1px solid var(--mobius-border, #e2e8f0);
}
.mobius-auth-btn-secondary:hover {
  background: var(--mobius-bg-muted, #f8fafc);
}
.mobius-auth-confirm-actions .mobius-auth-btn-danger {
  background: var(--mobius-error, #dc2626);
}
.mobius-auth-confirm-actions .mobius-auth-btn-danger:hover {
  background: var(--mobius-error-hover, #b91c1c);
}

/* Toasts */
.mobius-auth-toast-host {
  position: fixed;
  bottom: 24px;
  right: 24px;
  display: flex;
  flex-direction: column;
  gap: 8px;
  z-index: 1100;
  pointer-events: none;
}
.mobius-auth-toast {
  pointer-events: auto;
  min-width: 220px;
  max-width: 320px;
  padding: 10px 14px;
  border-radius: var(--mobius-radius-base, 8px);
  background: var(--mobius-bg-primary, #1a1d21);
  color: var(--mobius-bg-inverse-text, #fafbfc);
  box-shadow: var(--mobius-shadow-lg, 0 8px 24px rgba(0,0,0,0.18));
  font-size: var(--mobius-text-sm, 0.8125rem);
  opacity: 0;
  transform: translateY(8px);
  transition: opacity 160ms ease, transform 160ms ease;
}
.mobius-auth-toast.open { opacity: 1; transform: translateY(0); }
.mobius-auth-toast--success { background: #166534; color: #f0fdf4; }
.mobius-auth-toast--error { background: #991b1b; color: #fef2f2; }
.mobius-auth-toast--info { background: #1e3a8a; color: #eff6ff; }
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
      tr.innerHTML = `<td>${escapeHtml4(name)}</td><td>${c?.latency_cap_ms ?? "\u2014"}</td><td>${c?.cost_cap_usd ?? "\u2014"}</td>`;
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
  const HIDDEN_PROFILES = /* @__PURE__ */ new Set(["default", "bandit"]);
  const LEGACY_TO_DISPLAY = {
    default: "auto",
    bandit: "auto"
  };
  const render = (data) => {
    const profilesRaw = data && data.available_profiles || [];
    const activeRaw = data && data.active_profile || "default";
    const seen = /* @__PURE__ */ new Set();
    const display = [];
    if (profilesRaw.includes("auto") || profilesRaw.includes("default") || profilesRaw.includes("bandit")) {
      display.push("auto");
      seen.add("auto");
    }
    for (const p of profilesRaw) {
      if (HIDDEN_PROFILES.has(p) || p === "auto")
        continue;
      if (!seen.has(p)) {
        display.push(p);
        seen.add(p);
      }
    }
    const activeDisplay = LEGACY_TO_DISPLAY[activeRaw] || activeRaw;
    sel.innerHTML = "";
    display.forEach((p) => {
      const opt = document.createElement("option");
      opt.value = p;
      opt.textContent = p;
      if (p === activeDisplay)
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
function initSidebarRailIcons(authService) {
  const sidebar = document.getElementById("sidebar");
  if (!sidebar)
    return;
  const icons = Array.from(sidebar.querySelectorAll(".sidebar-rail-icon"));
  if (!icons.length)
    return;
  icons.forEach((btn) => {
    btn.addEventListener("click", (e) => {
      e.preventDefault();
      e.stopPropagation();
      const targetId = btn.dataset.target || "";
      if (sidebar.classList.contains("sidebar--collapsed")) {
        sidebar.classList.remove("sidebar--collapsed");
        const main = document.querySelector(".main");
        if (main)
          main.classList.remove("sidebar-collapsed");
      }
      if (!targetId)
        return;
      requestAnimationFrame(() => {
        const target = document.getElementById(targetId);
        if (!target)
          return;
        const section = target.closest(".sidebar-recent, .sidebar-needs-answer, .sidebar-skills, .sidebar-toast-master");
        if (section) {
          section.scrollIntoView({ behavior: "smooth", block: "start" });
          section.classList.add("sidebar-section--flash");
          setTimeout(() => section.classList.remove("sidebar-section--flash"), 1200);
        }
      });
    });
  });
  const updateRecentBadge = () => {
    const badge = document.getElementById("railBadgeRecent");
    if (!badge)
      return;
    void Promise.resolve(authService?.getAuthHeader?.() ?? {}).then((hdrs) => fetch(API_BASE + "/chat/history/recent?limit=20", { headers: hdrs ?? {} })).then((r) => r.ok ? r.json() : []).then((rows) => {
      const n = Array.isArray(rows) ? rows.length : 0;
      if (n > 0) {
        badge.textContent = String(n > 99 ? "99+" : n);
        badge.hidden = false;
      } else {
        badge.hidden = true;
      }
    }).catch(() => {
    });
  };
  updateRecentBadge();
}
var QD_AUTO_REFRESH_MS = 3e4;
var QD_SINCE_DELTAS = {
  "1h": 60 * 60 * 1e3,
  "24h": 24 * 60 * 60 * 1e3,
  "7d": 7 * 24 * 60 * 60 * 1e3,
  "30d": 30 * 24 * 60 * 60 * 1e3,
  "all": null
};
function setupQueriesDumpUI() {
  const launch = document.getElementById("drawerQueriesDumpLaunch");
  const btn = document.getElementById("btnQueriesDump");
  const modal = document.getElementById("queriesDumpModal");
  const body = document.getElementById("queriesDumpBody");
  const closeBtn = document.getElementById("queriesDumpClose");
  const backdrop = document.getElementById("queriesDumpBackdrop");
  const summary = document.getElementById("queriesDumpSummary");
  const status = document.getElementById("queriesDumpStatus");
  const fSince = document.getElementById("qdSince");
  const fUser = document.getElementById("qdUser");
  const fErr = document.getElementById("qdHasError");
  const fFb = document.getElementById("qdHasFeedback");
  const fLimit = document.getElementById("qdLimit");
  const btnApply = document.getElementById("qdApply");
  const btnReset = document.getElementById("qdReset");
  const btnPrev = document.getElementById("qdPrev");
  const btnNext = document.getElementById("qdNext");
  const jsonLink = document.getElementById("qdJson");
  const autoRefresh = document.getElementById("queriesDumpAutoRefresh");
  if (!launch || !btn || !modal || !body || !fSince || !fLimit)
    return;
  let offset = 0;
  let lastCount = 0;
  let refreshTimer = null;
  const setOpen = (open) => {
    modal.classList.toggle("llm-router-report-modal--open", open);
    modal.setAttribute("aria-hidden", open ? "false" : "true");
    if (!open && refreshTimer !== null) {
      window.clearInterval(refreshTimer);
      refreshTimer = null;
    }
    if (open)
      scheduleAutoRefresh();
  };
  const buildParams = () => {
    const p = new URLSearchParams();
    const limit = Math.max(1, Math.min(1e3, parseInt(fLimit.value, 10) || 100));
    p.set("limit", String(limit));
    p.set("offset", String(offset));
    const sinceKey = fSince.value;
    const delta = QD_SINCE_DELTAS[sinceKey];
    if (delta !== null && delta !== void 0) {
      p.set("since", new Date(Date.now() - delta).toISOString());
    }
    const u = (fUser?.value || "").trim();
    if (u)
      p.set("user_id", u);
    if (fErr?.checked)
      p.set("has_error", "true");
    if (fFb?.checked)
      p.set("has_feedback", "true");
    return p;
  };
  const updateJsonLink = () => {
    if (!jsonLink)
      return;
    const p = buildParams();
    p.set("format", "json");
    jsonLink.href = API_BASE + "/chat/admin/queries?" + p.toString();
  };
  const load = () => {
    body.innerHTML = '<p class="llm-router-report-loading" style="padding:1rem">Loading\u2026</p>';
    if (status)
      status.textContent = "loading\u2026";
    updateJsonLink();
    const p = buildParams();
    fetch(API_BASE + "/chat/admin/queries?" + p.toString(), {
      headers: { Accept: "application/json" }
    }).then((r) => {
      if (r.status === 404) {
        throw new Error("Endpoint disabled (set MOBIUS_ADMIN_ENABLED=1).");
      }
      return r.json();
    }).then((data) => {
      renderQueriesDumpBody(body, summary, data);
      lastCount = data.count;
      if (status) {
        const limit = parseInt(fLimit.value, 10) || 100;
        status.textContent = `rows ${offset + 1}\u2013${offset + data.count} (limit ${limit})`;
      }
      if (btnPrev)
        btnPrev.disabled = offset === 0;
      if (btnNext)
        btnNext.disabled = data.count < (parseInt(fLimit.value, 10) || 100);
    }).catch((err) => {
      body.innerHTML = '<p class="llm-router-report-error" style="padding:1rem">Could not load: ' + (err && err.message ? String(err.message) : "request failed") + "</p>";
      if (status)
        status.textContent = "error";
    });
  };
  const scheduleAutoRefresh = () => {
    if (refreshTimer !== null) {
      window.clearInterval(refreshTimer);
      refreshTimer = null;
    }
    if (autoRefresh?.checked && modal.classList.contains("llm-router-report-modal--open")) {
      refreshTimer = window.setInterval(load, QD_AUTO_REFRESH_MS);
    }
  };
  btn.addEventListener("click", () => {
    offset = 0;
    setOpen(true);
    load();
  });
  closeBtn?.addEventListener("click", () => setOpen(false));
  backdrop?.addEventListener("click", () => setOpen(false));
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && modal.classList.contains("llm-router-report-modal--open"))
      setOpen(false);
  });
  btnApply?.addEventListener("click", () => {
    offset = 0;
    load();
  });
  btnReset?.addEventListener("click", () => {
    offset = 0;
    fSince.value = "24h";
    if (fUser)
      fUser.value = "";
    if (fErr)
      fErr.checked = false;
    if (fFb)
      fFb.checked = false;
    fLimit.value = "100";
    load();
  });
  btnPrev?.addEventListener("click", () => {
    const limit = parseInt(fLimit.value, 10) || 100;
    offset = Math.max(0, offset - limit);
    load();
  });
  btnNext?.addEventListener("click", () => {
    const limit = parseInt(fLimit.value, 10) || 100;
    if (lastCount < limit)
      return;
    offset = offset + limit;
    load();
  });
  autoRefresh?.addEventListener("change", scheduleAutoRefresh);
  fUser?.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      offset = 0;
      load();
    }
  });
}
function renderQueriesDumpBody(container, summaryEl, data) {
  const rows = data.rows || [];
  if (summaryEl) {
    if (rows.length === 0) {
      summaryEl.hidden = true;
    } else {
      const totalCost = rows.reduce((s, r) => s + (Number(r.cost_usd) || 0), 0);
      const totalIn = rows.reduce((s, r) => s + (r.input_tokens || 0), 0);
      const totalOut = rows.reduce((s, r) => s + (r.output_tokens || 0), 0);
      const errCount = rows.reduce((s, r) => s + (r.llm_error_count > 0 ? 1 : 0), 0);
      const fbUp = rows.filter((r) => r.feedback_rating === "up").length;
      const fbDown = rows.filter((r) => r.feedback_rating === "down").length;
      const lats = rows.map((r) => r.total_latency_ms || 0).filter((n) => n > 0).sort((a, b) => a - b);
      const pct = (arr, p) => arr.length === 0 ? 0 : arr[Math.min(arr.length - 1, Math.floor(arr.length * p))] || 0;
      const p50 = pct(lats, 0.5);
      const p95 = pct(lats, 0.95);
      summaryEl.innerHTML = [
        `<div class="qd-stat"><span class="qd-n">${rows.length}</span><span class="qd-label">turns</span></div>`,
        `<div class="qd-stat"><span class="qd-n">${formatMs(p50)}</span><span class="qd-label">p50 latency</span></div>`,
        `<div class="qd-stat"><span class="qd-n">${formatMs(p95)}</span><span class="qd-label">p95 latency</span></div>`,
        `<div class="qd-stat"><span class="qd-n">$${totalCost.toFixed(4)}</span><span class="qd-label">total cost</span></div>`,
        `<div class="qd-stat"><span class="qd-n">${formatTok(totalIn + totalOut)}</span><span class="qd-label">total tokens</span></div>`,
        `<div class="qd-stat"><span class="qd-n">${errCount}</span><span class="qd-label">errors</span></div>`,
        `<div class="qd-stat"><span class="qd-n">${fbUp} / ${fbDown}</span><span class="qd-label">feedback \u2191/\u2193</span></div>`
      ].join("");
      summaryEl.hidden = false;
    }
  }
  const escapeHtml5 = (s) => s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  const fbPill = (rating) => {
    if (rating === "up")
      return '<span class="qd-pill qd-pill-up">\u2191</span>';
    if (rating === "down")
      return '<span class="qd-pill qd-pill-down">\u2193</span>';
    return "";
  };
  const formatTime = (iso) => {
    try {
      return new Date(iso).toLocaleString();
    } catch {
      return iso;
    }
  };
  if (rows.length === 0) {
    container.innerHTML = data.warning ? `<p class="llm-router-report-error" style="padding:1rem">${escapeHtml5(data.warning)}</p>` : '<p class="llm-router-report-meta" style="padding:1rem">No turns match the current filters.</p>';
    return;
  }
  const renderRow = (r) => {
    const ms = r.total_latency_ms || 0;
    const slowCls = ms >= 2e3 ? " qd-slow" : "";
    const errDot = r.llm_error_count > 0 ? `<span class="qd-err-dot" title="${escapeHtml5(r.last_error_type || "error")}"></span>` : "";
    const cost = Number(r.cost_usd || 0).toFixed(4);
    const userLabel = r.user_id || "\u2014";
    const question = r.question_preview || "(no question)";
    const fb = fbPill(r.feedback_rating);
    const detailRows = [
      `<dt>question</dt><dd class="qd-full-q">${escapeHtml5(question)}</dd>`
    ];
    if (r.thread_id) {
      detailRows.push(`<dt>thread</dt><dd><span class="qd-mono-dim">${escapeHtml5(String(r.thread_id))}</span></dd>`);
    }
    if (r.models_used) {
      detailRows.push(`<dt>models</dt><dd>${escapeHtml5(r.models_used)}</dd>`);
    }
    detailRows.push(`<dt>llm calls</dt><dd>${r.llm_call_count}</dd>`);
    detailRows.push(
      `<dt>tokens</dt><dd>${Number(r.input_tokens || 0).toLocaleString()} in <span class="qd-mono-dim">\xB7</span> ${Number(r.output_tokens || 0).toLocaleString()} out</dd>`
    );
    detailRows.push(
      `<dt>rag</dt><dd>${r.chunks_assembled} chunk${r.chunks_assembled === 1 ? "" : "s"} <span class="qd-mono-dim">\xB7</span> ${r.retrieval_runs_count} run${r.retrieval_runs_count === 1 ? "" : "s"}</dd>`
    );
    if (r.cache_mode) {
      const sim = r.cache_top_similarity != null ? ` <span class="qd-mono-dim">sim ${Number(r.cache_top_similarity).toFixed(2)}</span>` : "";
      detailRows.push(
        `<dt>cache</dt><dd><span class="qd-pill qd-pill-cache-${escapeHtml5(r.cache_mode)}">${escapeHtml5(r.cache_mode)}</span>${sim}</dd>`
      );
    }
    if (r.llm_error_count > 0) {
      detailRows.push(
        `<dt>errors</dt><dd class="qd-err-line">${r.llm_error_count}${r.last_error_type ? " (" + escapeHtml5(r.last_error_type) + ")" : ""}</dd>`
      );
    }
    if (r.feedback_comment) {
      detailRows.push(
        `<dt>feedback</dt><dd>${fb} ${escapeHtml5(r.feedback_comment)}</dd>`
      );
    }
    detailRows.push(
      `<dt>correlation</dt><dd><span class="qd-mono-dim">${escapeHtml5(r.correlation_id)}</span></dd>`
    );
    return `
      <details class="qd-row">
        <summary>
          <span class="qd-col-time">${escapeHtml5(formatTime(r.created_at))}</span>
          <span class="qd-col-user">${escapeHtml5(userLabel)}</span>
          <span class="qd-col-q">${errDot}${escapeHtml5(question)}</span>
          <span class="qd-col-ms${slowCls}">${formatMs(ms)}</span>
          <span class="qd-col-cost">$${cost}</span>
          <span class="qd-col-fb">${fb}</span>
          <span class="qd-col-chev">\u25B6</span>
        </summary>
        <dl class="qd-row-detail">${detailRows.join("")}</dl>
      </details>`;
  };
  const warn = data.warning ? `<div class="llm-router-report-error" style="padding:0.5rem 1rem">DB warning: ${escapeHtml5(data.warning)}</div>` : "";
  container.innerHTML = warn + rows.map(renderRow).join("");
}
function formatMs(ms) {
  if (!ms)
    return "\u2014";
  if (ms < 1e3)
    return `${ms} ms`;
  return `${(ms / 1e3).toFixed(2)} s`;
}
function formatTok(n) {
  if (n >= 1e6)
    return `${(n / 1e6).toFixed(1)}M`;
  if (n >= 1e3)
    return `${(n / 1e3).toFixed(1)}K`;
  return String(n);
}
function syncQueriesDumpVisibility(profile) {
  const launch = document.getElementById("drawerQueriesDumpLaunch");
  if (!launch)
    return;
  launch.hidden = !getShowLlmPerformance(profile);
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
  const raw = typeof line === "string" ? line : line == null ? "" : String(line);
  const l = raw.toLowerCase();
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
  const links = [];
  const stashLink = (html) => {
    const i = links.length;
    links.push(html);
    return `\uE010${i}\uE011`;
  };
  const MOBIUS_URL_RE = /https:\/\/mobius-[a-z0-9\-]+\.(?:a\.run\.app|us-central1\.run\.app)[^\s"'<>()[\]]*[^\s"'<>()[\].,!?;:]/g;
  const EMAIL_RE = /[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}/g;
  const PHONE_RE = /(?:\+?1[\s.\-]?)?\(?[2-9]\d{2}\)?[\s.\-]\d{3}[\s.\-]\d{4}/g;
  out = out.replace(/\[([^\]]+)\]\((https:\/\/[^)]+)\)/g, (_m, linkText, url) => {
    if (/^https:\/\/mobius-/.test(url)) {
      return stashLink(`<a href="${url}" class="chat-link chat-link--url" target="_blank" rel="noopener noreferrer" title="${url}">${linkText} \u2197</a>`);
    }
    return linkText;
  });
  out = out.replace(MOBIUS_URL_RE, (url) => {
    let display = url;
    try {
      display = new URL(url).hostname.replace(/\.(?:a\.run|us-central1\.run)\.app$/, "").replace(/^mobius-/, "").replace(/-[a-z0-9]+-uc$/, "");
    } catch {
      display = url.length > 40 ? url.slice(0, 39) + "\u2026" : url;
    }
    return stashLink(`<a href="${url}" class="chat-link chat-link--url" target="_blank" rel="noopener noreferrer" title="${url}">${display} \u2197</a>`);
  });
  out = out.replace(
    EMAIL_RE,
    (email) => stashLink(`<a href="mailto:${email}" class="chat-link chat-link--email">${email}</a>`)
  );
  out = out.replace(PHONE_RE, (raw) => {
    const digits = raw.replace(/[^\d+]/g, "");
    return stashLink(`<a href="tel:${digits}" class="chat-link chat-link--tel">${raw}</a>`);
  });
  out = escape(out);
  imgs.forEach((img, i) => {
    out = out.replace(`\uE000${i}\uE001`, img);
  });
  links.forEach((html, i) => {
    out = out.replace(`\uE010${i}\uE011`, html);
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
      if (data.mode !== "FACTUAL" && data.mode !== "CANONICAL" && data.mode !== "BLENDED" && data.mode !== "RECITAL")
        return null;
      if (typeof data.direct_answer !== "string")
        return null;
      if (data.mode === "RECITAL") {
        const rec = data.recital;
        if (!rec || typeof rec.verbatim !== "string" || !rec.verbatim.trim())
          return null;
        return {
          mode: "RECITAL",
          direct_answer: data.direct_answer,
          sections: [],
          recital: {
            verbatim: rec.verbatim,
            document_id: typeof rec.document_id === "string" ? rec.document_id : void 0,
            section: typeof rec.section === "string" ? rec.section : void 0
          }
        };
      }
      if (!Array.isArray(data.sections))
        return null;
      const rawSections = data.sections.slice(0, MAX_SECTIONS);
      const VALID_FORMATS = ["bullets", "table", "steps", "stats", "bars", "conditions"];
      const sections = rawSections.map((sec) => ({
        intent: isSectionIntent(sec.intent) ? sec.intent : "process",
        label: typeof sec.label === "string" ? sec.label : "",
        format: VALID_FORMATS.includes(sec.format) ? sec.format : "bullets",
        bullets: Array.isArray(sec.bullets) ? sec.bullets : [],
        data: sec.data && typeof sec.data === "object" ? sec.data : void 0
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
  const modeRe = /["']mode["']\s*:\s*["'](FACTUAL|CANONICAL|BLENDED|RECITAL)["']/;
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
  const visibleIntents = /* @__PURE__ */ new Set(["definitions"]);
  const visible = all.filter((s) => visibleIntents.has(s.intent ?? "process"));
  const hidden = all.filter((s) => !visibleIntents.has(s.intent ?? "process"));
  return { visible, hidden };
}
function _renderSectionBody(sec, body) {
  const fmt = sec.format ?? "bullets";
  const data = sec.data;
  if (fmt === "table" && data?.headers && data?.rows) {
    const tbl = document.createElement("table");
    tbl.className = "ac-fmt-table";
    const thead = tbl.createTHead();
    const hRow = thead.insertRow();
    data.headers.forEach((h) => {
      const th = document.createElement("th");
      th.textContent = h;
      hRow.appendChild(th);
    });
    const tbody = tbl.createTBody();
    data.rows.forEach((row) => {
      const tr = tbody.insertRow();
      row.forEach((cell) => {
        const td = tr.insertCell();
        td.textContent = cell;
      });
    });
    body.appendChild(tbl);
    return;
  }
  if (fmt === "steps" && data?.items) {
    const ol = document.createElement("ol");
    ol.className = "ac-fmt-steps";
    data.items.forEach((item) => {
      const li = document.createElement("li");
      li.className = "ac-fmt-step";
      li.textContent = typeof item === "string" ? item : item.label ?? "";
      ol.appendChild(li);
    });
    body.appendChild(ol);
    return;
  }
  if (fmt === "stats" && data?.items) {
    const grid = document.createElement("div");
    grid.className = "ac-fmt-stats";
    data.items.slice(0, 4).forEach((item) => {
      const tile = document.createElement("div");
      tile.className = "ac-fmt-stat-tile";
      const val = document.createElement("div");
      val.className = "ac-fmt-stat-value";
      val.textContent = item.value ?? "";
      const lbl = document.createElement("div");
      lbl.className = "ac-fmt-stat-label";
      lbl.textContent = item.label ?? "";
      tile.appendChild(val);
      tile.appendChild(lbl);
      if (item.note) {
        const note = document.createElement("div");
        note.className = "ac-fmt-stat-note";
        note.textContent = item.note;
        tile.appendChild(note);
      }
      grid.appendChild(tile);
    });
    body.appendChild(grid);
    return;
  }
  if (fmt === "bars" && data?.items) {
    const list = document.createElement("div");
    list.className = "ac-fmt-bars";
    data.items.forEach((item) => {
      const row = document.createElement("div");
      row.className = "ac-fmt-bar-row";
      const lbl = document.createElement("div");
      lbl.className = "ac-fmt-bar-label";
      lbl.textContent = item.label ?? "";
      const track = document.createElement("div");
      track.className = "ac-fmt-bar-track";
      const fill = document.createElement("div");
      fill.className = "ac-fmt-bar-fill";
      const pct = Math.round(Math.min(1, Math.max(0, item.weight ?? 0)) * 100);
      fill.style.width = `${pct}%`;
      track.appendChild(fill);
      row.appendChild(lbl);
      row.appendChild(track);
      if (item.note) {
        const note = document.createElement("div");
        note.className = "ac-fmt-bar-note";
        note.textContent = item.note;
        row.appendChild(note);
      }
      list.appendChild(row);
    });
    body.appendChild(list);
    return;
  }
  if (fmt === "conditions" && data?.items) {
    const list = document.createElement("div");
    list.className = "ac-fmt-conditions";
    data.items.forEach((item) => {
      const row = document.createElement("div");
      row.className = "ac-fmt-condition-row";
      const cond = document.createElement("div");
      cond.className = "ac-fmt-condition-if";
      cond.textContent = item.condition ?? "";
      const result = document.createElement("div");
      result.className = "ac-fmt-condition-then";
      result.textContent = item.result ?? "";
      row.appendChild(cond);
      row.appendChild(result);
      list.appendChild(row);
    });
    body.appendChild(list);
    return;
  }
  const bullets = (sec.bullets ?? []).slice(0, MAX_BULLETS_PER_SECTION);
  bullets.forEach((b) => {
    const li = document.createElement("div");
    li.className = "answer-card-bullet";
    li.textContent = b;
    body.appendChild(li);
  });
  if (bullets.length < (sec.bullets?.length ?? 0)) {
    const more = document.createElement("div");
    more.className = "answer-card-more";
    more.textContent = "Show more";
    more.setAttribute("aria-label", "Show more bullets");
    body.appendChild(more);
  }
}
function renderOneSection(sec) {
  const sectionEl = document.createElement("div");
  sectionEl.className = `answer-card-section answer-card-section--${sec.format ?? "bullets"}`;
  const labelEl = document.createElement("div");
  labelEl.className = "answer-card-section-label";
  labelEl.textContent = sec.label || "";
  sectionEl.appendChild(labelEl);
  _renderSectionBody(sec, sectionEl);
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
  if (card.mode === "RECITAL" && card.recital?.verbatim) {
    const attr = document.createElement("div");
    attr.className = "recital-attr";
    attr.textContent = "From the Mobius founding essay:";
    bubble.appendChild(attr);
    const RECITAL_PARA_LIMIT = 3;
    const stripSeparators = (t) => t.replace(/^[ \t]*[-*_]{3,}[ \t]*$/gm, "").trim();
    const fullText = stripSeparators(card.recital.verbatim);
    const allParas = fullText.split(/\n\n+/);
    const clipped = allParas.length > RECITAL_PARA_LIMIT;
    const proseText = clipped ? allParas.slice(0, RECITAL_PARA_LIMIT).join("\n\n") : fullText;
    const prose = document.createElement("div");
    prose.className = "recital-prose";
    prose.innerHTML = simpleMarkdownToHtml(proseText);
    bubble.appendChild(prose);
    if (clipped) {
      const readMore = document.createElement("button");
      readMore.type = "button";
      readMore.className = "recital-read-more";
      readMore.textContent = "Read the full essay \u2197";
      let expanded = false;
      readMore.addEventListener("click", () => {
        expanded = !expanded;
        prose.innerHTML = simpleMarkdownToHtml(expanded ? fullText : proseText);
        readMore.textContent = expanded ? "Collapse \u2191" : "Read the full essay \u2197";
        const container = readMore.closest(".answer-card--recital") ?? wrap;
        container.classList.toggle("recital-expanded", expanded);
      });
      bubble.appendChild(readMore);
    }
    if (opts?.showConfidenceBadge !== false && !opts?.suppressConfidenceForAdminQcFail) {
      bubble.appendChild(renderConfidenceBadge("approved_authoritative"));
    }
    wrap.appendChild(bubble);
    return wrap;
  }
  const direct = document.createElement("div");
  direct.className = "answer-card-direct";
  direct.innerHTML = simpleMarkdownToHtml(card.direct_answer);
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
  const answerPanel = document.createElement("div");
  answerPanel.className = "ac-tab-panel ac-tab-panel--summary ac-tab-panel--active";
  answerPanel.setAttribute("role", "tabpanel");
  if (metaRow.childNodes.length > 0)
    answerPanel.appendChild(metaRow);
  const { visible, hidden } = splitSectionsByVisibility(card.sections ?? [], card.mode);
  visible.forEach((sec) => answerPanel.appendChild(renderOneSection(sec)));
  if (hidden.length > 0) {
    const detailsBlock = document.createElement("div");
    detailsBlock.className = "answer-card-details";
    detailsBlock.setAttribute("aria-hidden", "true");
    hidden.forEach((sec) => detailsBlock.appendChild(renderOneSection(sec)));
    answerPanel.appendChild(detailsBlock);
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
    answerPanel.appendChild(toggleBtn);
  }
  if (card.confidence_note && card.confidence_note.trim()) {
    const note = document.createElement("div");
    note.className = "answer-card-confidence";
    note.textContent = card.confidence_note;
    answerPanel.appendChild(note);
  }
  const _corrections = opts?.corrections ?? [];
  const _nextStepQuestions = opts?.nextQuestions ?? [];
  const _nextStepTasks = opts?.nextStepTasks ?? [];
  const hasCitations = Array.isArray(card.citations) && card.citations.length > 0;
  const hasCorrections = _corrections.length > 0;
  const hasNextSteps = _nextStepQuestions.length > 0;
  const hasTasks = _nextStepTasks.length > 0;
  const showTabBar = hasCitations || hasCorrections || hasNextSteps || hasTasks;
  const citationsPanel = document.createElement("div");
  citationsPanel.className = "ac-tab-panel ac-tab-panel--citations";
  citationsPanel.setAttribute("role", "tabpanel");
  citationsPanel.setAttribute("hidden", "");
  if (hasCitations) {
    const citList = document.createElement("div");
    citList.className = "ac-citations-list";
    (card.citations ?? []).forEach((cit) => {
      const row = document.createElement("div");
      row.className = "ac-citation-row";
      const title = document.createElement("div");
      title.className = "ac-citation-title";
      title.textContent = cit.doc_title || "";
      const meta = document.createElement("div");
      meta.className = "ac-citation-meta";
      if (cit.locator)
        meta.textContent = cit.locator;
      const snippet = document.createElement("div");
      snippet.className = "ac-citation-snippet";
      snippet.textContent = cit.snippet || "";
      row.appendChild(title);
      if (cit.locator)
        row.appendChild(meta);
      if (cit.snippet)
        row.appendChild(snippet);
      citList.appendChild(row);
    });
    citationsPanel.appendChild(citList);
  }
  const correctionsPanel = document.createElement("div");
  correctionsPanel.className = "ac-tab-panel ac-tab-panel--corrections";
  correctionsPanel.setAttribute("role", "tabpanel");
  correctionsPanel.setAttribute("hidden", "");
  if (hasCorrections) {
    const corrList = document.createElement("div");
    corrList.className = "ac-correction-list";
    _corrections.forEach(({ label, text }) => {
      const row = document.createElement("div");
      row.className = "ac-correction-row";
      const lbl = document.createElement("div");
      lbl.className = "ac-correction-label";
      lbl.textContent = label;
      row.appendChild(lbl);
      row.appendChild(document.createTextNode(text));
      corrList.appendChild(row);
    });
    correctionsPanel.appendChild(corrList);
    const corrCallout = document.createElement("div");
    corrCallout.className = "ac-answer-correction-callout";
    const corrIcon = document.createElement("span");
    corrIcon.className = "ac-answer-correction-icon";
    corrIcon.textContent = "\u26A0";
    corrIcon.setAttribute("aria-hidden", "true");
    const corrBody = document.createElement("div");
    const corrLbl = document.createElement("div");
    corrLbl.className = "ac-answer-correction-callout-label";
    corrLbl.textContent = _corrections[0].label;
    const corrP = document.createElement("p");
    corrP.className = "ac-answer-correction-callout-text";
    corrP.appendChild(document.createTextNode(
      _corrections.length === 1 ? _corrections[0].text.slice(0, 120) + (_corrections[0].text.length > 120 ? "\u2026" : "") + " \u2014 " : `${_corrections.length} corrections noted \u2014 `
    ));
    const corrTabLink = document.createElement("button");
    corrTabLink.type = "button";
    corrTabLink.className = "ac-correction-tab-link";
    corrTabLink.textContent = "see Corrections tab";
    corrTabLink.addEventListener("click", () => {
      const liveBubble = corrTabLink.closest(".answer-card-bubble") ?? bubble;
      liveBubble.querySelector('[data-panel="corrections"]')?.click();
    });
    corrP.appendChild(corrTabLink);
    corrP.appendChild(document.createTextNode(" for details."));
    corrBody.appendChild(corrLbl);
    corrBody.appendChild(corrP);
    corrCallout.appendChild(corrIcon);
    corrCallout.appendChild(corrBody);
    answerPanel.appendChild(corrCallout);
  }
  const nextStepsPanel = document.createElement("div");
  nextStepsPanel.className = "ac-tab-panel ac-tab-panel--next-steps";
  nextStepsPanel.setAttribute("role", "tabpanel");
  nextStepsPanel.setAttribute("hidden", "");
  if (_nextStepQuestions.length > 0) {
    const nsWrap = document.createElement("div");
    nsWrap.className = "ac-next-steps";
    _nextStepQuestions.forEach((q) => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "ac-next-step-question";
      btn.textContent = q.text;
      if (opts?.onFollowupClick && q.text) {
        btn.addEventListener("click", () => opts.onFollowupClick(q.text));
      }
      nsWrap.appendChild(btn);
    });
    nextStepsPanel.appendChild(nsWrap);
  }
  const tasksPanel = document.createElement("div");
  tasksPanel.className = "ac-tab-panel ac-tab-panel--tasks";
  tasksPanel.setAttribute("role", "tabpanel");
  tasksPanel.setAttribute("hidden", "");
  if (_nextStepTasks.length > 0) {
    const tWrap = document.createElement("div");
    tWrap.className = "ac-tasks-list";
    _nextStepTasks.forEach(({ text, taskType }) => {
      const row = document.createElement("div");
      row.className = "ac-next-step-task-row";
      const taskText = document.createElement("span");
      taskText.className = "ac-next-step-task-text";
      taskText.textContent = text;
      const createBtn = document.createElement("button");
      createBtn.type = "button";
      createBtn.className = "ac-next-step-create-btn";
      createBtn.setAttribute("data-task-type", taskType || "general");
      createBtn.setAttribute("data-task-text", text);
      createBtn.textContent = "+ Add to my tasks";
      createBtn.addEventListener("click", () => {
        openCreateTaskDialog({
          title: text.slice(0, 60),
          excerpt: text,
          sourceModule: "next_steps",
          onCreated: () => {
            createBtn.textContent = "Added \u2713";
            createBtn.disabled = true;
            createBtn.classList.add("ac-next-step-create-btn--done");
          }
        });
      });
      row.appendChild(taskText);
      row.appendChild(createBtn);
      tWrap.appendChild(row);
    });
    tasksPanel.appendChild(tWrap);
  }
  if (showTabBar) {
    const tabBar = document.createElement("div");
    tabBar.className = "ac-tab-bar";
    tabBar.setAttribute("role", "tablist");
    const mkTab = (label, panelKey, count, active) => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "ac-tab" + (active ? " ac-tab--active" : "");
      btn.setAttribute("role", "tab");
      btn.setAttribute("aria-selected", String(active));
      btn.setAttribute("data-panel", panelKey);
      if (count !== void 0 && count === 0)
        btn.setAttribute("data-empty", "1");
      if (count !== void 0 && count > 0) {
        btn.appendChild(document.createTextNode(label + " "));
        const badge = document.createElement("span");
        badge.className = "ac-tab-count";
        badge.textContent = String(count);
        btn.appendChild(badge);
      } else {
        btn.textContent = label;
      }
      btn.addEventListener("click", () => {
        const liveBubble = btn.closest(".answer-card-bubble") ?? bubble;
        tabBar.querySelectorAll(".ac-tab").forEach((t) => {
          t.classList.remove("ac-tab--active");
          t.setAttribute("aria-selected", "false");
        });
        liveBubble.querySelectorAll(".ac-tab-panel").forEach((p) => {
          p.hidden = true;
          p.classList.remove("ac-tab-panel--active");
        });
        btn.classList.add("ac-tab--active");
        btn.setAttribute("aria-selected", "true");
        const targetPanel = liveBubble.querySelector(`.ac-tab-panel--${panelKey}`);
        if (targetPanel) {
          targetPanel.hidden = false;
          targetPanel.classList.add("ac-tab-panel--active");
        }
      });
      return btn;
    };
    tabBar.appendChild(mkTab("Summary", "summary", void 0, true));
    tabBar.appendChild(mkTab("Citations", "citations", (card.citations ?? []).length, false));
    tabBar.appendChild(mkTab("Corrections", "corrections", _corrections.length, false));
    tabBar.appendChild(mkTab("Follow-up", "next-steps", _nextStepQuestions.length, false));
    tabBar.appendChild(mkTab("Tasks", "tasks", _nextStepTasks.length, false));
    bubble.appendChild(tabBar);
  }
  bubble.appendChild(answerPanel);
  bubble.appendChild(citationsPanel);
  bubble.appendChild(correctionsPanel);
  bubble.appendChild(nextStepsPanel);
  bubble.appendChild(tasksPanel);
  if (card.suggested_actions && card.suggested_actions.length > 0) {
    const actionsWrap = document.createElement("div");
    actionsWrap.className = "answer-card-actions";
    card.suggested_actions.forEach((action) => {
      if (action.type === "external_link" && action.url && action.label) {
        const a = document.createElement("a");
        a.href = action.url;
        a.target = "_blank";
        a.rel = "noopener noreferrer";
        a.className = "answer-card-action-chip";
        a.textContent = (action.icon ? action.icon + " " : "") + action.label + " \u2197";
        a.setAttribute("aria-label", action.label + " (opens in new tab)");
        actionsWrap.appendChild(a);
      }
    });
    if (actionsWrap.childNodes.length > 0)
      wrap.appendChild(actionsWrap);
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
  const inlineErr = document.createElement("div");
  inlineErr.className = "credentialing-copilot-error credentialing-copilot-inline-err";
  inlineErr.style.display = "none";
  wrap.appendChild(inlineErr);
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
      inlineErr.textContent = "Invalid JSON \u2014 fix the textarea or use Accept draft as-is.";
      inlineErr.style.display = "";
      return;
    }
    inlineErr.style.display = "none";
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
      inlineErr.textContent = "Submission failed \u2014 please try again or accept the draft as-is.";
      inlineErr.style.display = "";
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
function followupChipToQuery(text) {
  const t = text.trim().replace(/\?$/, "");
  let m;
  m = t.match(/^Would you like (?:me )?to (.+)$/i);
  if (m)
    return "Please " + m[1].charAt(0).toLowerCase() + m[1].slice(1) + ".";
  m = t.match(/^Do you want (?:me )?to (.+)$/i);
  if (m)
    return "Please " + m[1].charAt(0).toLowerCase() + m[1].slice(1) + ".";
  m = t.match(/^Shall I (?:show|walk) you (.+)$/i);
  if (m)
    return "Please show me " + m[1] + ".";
  m = t.match(/^(?:Can|Shall) I help you with (.+)$/i);
  if (m)
    return "Help me with " + m[1] + ".";
  return text.trim();
}
function updateChatSuggestions(questions, onSelect) {
  const slot = document.getElementById("chat-suggestions");
  if (!slot)
    return;
  slot.innerHTML = "";
  const clickable = questions.filter((q) => q.clickable && q.text.trim());
  if (!clickable.length) {
    slot.hidden = true;
    return;
  }
  const chips = document.createElement("div");
  chips.className = "chat-suggestions-chips";
  for (const q of clickable.slice(0, 4)) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "chat-suggestions-chip";
    btn.textContent = q.text.trim();
    btn.setAttribute("aria-label", "Ask: " + q.text.trim());
    btn.addEventListener("click", () => {
      slot.innerHTML = "";
      slot.hidden = true;
      onSelect(followupChipToQuery(q.text));
    });
    chips.appendChild(btn);
  }
  slot.appendChild(chips);
  slot.hidden = false;
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
  const variantClass = opts?.variant === "warn" ? " message--warn" : isError || opts?.variant === "error" ? " message--error" : "";
  const wrap = document.createElement("div");
  wrap.className = "message message--assistant" + variantClass;
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
  up.dataset.tourId = "msg-thumbs-up";
  up.appendChild(createThumbIcon("up"));
  const down = document.createElement("button");
  down.type = "button";
  down.className = "feedback-thumb";
  down.setAttribute("aria-label", "Bad response");
  down.dataset.tourId = "msg-thumbs-down";
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
      if (rating === "up") {
        window.dispatchEvent(new CustomEvent("mobiusFeedbackUp"));
      }
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
  const emailBtn = document.createElement("button");
  emailBtn.type = "button";
  emailBtn.setAttribute("aria-label", "Email this conversation");
  emailBtn.dataset.tourId = "msg-email";
  emailBtn.textContent = "Email";
  emailBtn.addEventListener("click", () => {
    const tid = window.__mobiusChatThreadId || null;
    if (!tid) {
      _showToast("No active thread to email");
      return;
    }
    openEmailThreadDialog(tid);
  });
  const taskActionBtn = document.createElement("button");
  taskActionBtn.type = "button";
  taskActionBtn.setAttribute("aria-label", "Create or review tasks");
  taskActionBtn.textContent = "Task";
  taskActionBtn.addEventListener("click", () => {
    const msg = bar.closest(".chat-turn")?.querySelector(".message--assistant .message-bubble");
    const excerpt = (msg?.textContent || "").trim().slice(0, 400);
    openCreateTaskDialog(excerpt ? { excerpt, title: excerpt.slice(0, 60), sourceModule: "chat_action" } : void 0);
  });
  left.appendChild(up);
  left.appendChild(down);
  left.appendChild(commentArea);
  actions.appendChild(copy);
  actions.appendChild(emailBtn);
  actions.appendChild(taskActionBtn);
  bar.appendChild(left);
  bar.appendChild(actions);
  return bar;
}
var _PF_CATEGORY_LABELS = {
  accuracy_trust: "Accuracy",
  coverage_gap: "Coverage gap",
  bug: "Bug",
  speed: "Speed",
  usability: "Usability",
  feature_request: "Feature request",
  praise: "Praise",
  other: "Other",
  docs_gap: "Docs gap",
  doc_stale: "Stale doc"
};
function renderCaptureCard(card, meta) {
  const wrap = document.createElement("div");
  wrap.className = "pf-capture-card";
  wrap.dataset.tourId = "msg-capture-card";
  const header = document.createElement("div");
  header.className = "pf-capture-card__header";
  const title = document.createElement("span");
  title.innerHTML = '<span class="pf-capture-card__check">\u2713</span> Feedback captured';
  const xBtn = document.createElement("button");
  xBtn.type = "button";
  xBtn.className = "pf-capture-card__x";
  xBtn.setAttribute("aria-label", "Dismiss");
  xBtn.textContent = "\u2715";
  header.appendChild(title);
  header.appendChild(xBtn);
  wrap.appendChild(header);
  const body = document.createElement("div");
  body.className = "pf-capture-card__body";
  const catChips = document.createElement("div");
  catChips.className = "pf-capture-card__cat-chips";
  let selectedCat = card.category;
  for (const c of card.categories) {
    const chip = document.createElement("button");
    chip.type = "button";
    chip.className = "pf-cat-chip" + (c === selectedCat ? " pf-cat-chip--active" : "");
    chip.textContent = _PF_CATEGORY_LABELS[c] ?? c;
    chip.dataset.cat = c;
    if (!card.editable)
      chip.disabled = true;
    chip.addEventListener("click", () => {
      selectedCat = c;
      catChips.querySelectorAll(".pf-cat-chip").forEach((b) => b.classList.remove("pf-cat-chip--active"));
      chip.classList.add("pf-cat-chip--active");
    });
    catChips.appendChild(chip);
  }
  body.appendChild(catChips);
  const ta = document.createElement("textarea");
  ta.className = "pf-capture-card__text";
  ta.value = card.tidied;
  ta.rows = 3;
  ta.readOnly = !card.editable;
  body.appendChild(ta);
  const btnRow = document.createElement("div");
  btnRow.className = "pf-capture-card__btns";
  const doneBtn = document.createElement("button");
  doneBtn.type = "button";
  doneBtn.className = "pf-capture-card__done";
  doneBtn.textContent = "Done";
  btnRow.appendChild(doneBtn);
  body.appendChild(btnRow);
  wrap.appendChild(body);
  let _isDirty = false;
  ta.addEventListener("input", () => {
    _isDirty = true;
  });
  function pfEvent(action) {
    fetch(API_BASE + "/chat/product-feedback/event", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        trigger: "inline",
        action,
        feedback_id: card.feedback_id,
        thread_id: meta.threadId
      })
    }).catch(() => {
    });
  }
  if (card.editable) {
    const updateBtn = document.createElement("button");
    updateBtn.type = "button";
    updateBtn.className = "pf-capture-card__update";
    updateBtn.textContent = "Update";
    updateBtn.style.display = "none";
    btnRow.insertBefore(updateBtn, doneBtn);
    ta.addEventListener("input", () => {
      updateBtn.style.display = "";
    });
    updateBtn.addEventListener("click", () => {
      const txt = ta.value.trim();
      if (!txt)
        return;
      const url = card.update_url ?? "/chat/product-feedback/update";
      fetch(API_BASE + url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          feedback_id: card.feedback_id,
          category: selectedCat,
          tidied: txt
        })
      }).catch(() => {
      });
      wrap.remove();
    });
  }
  function dismiss() {
    pfEvent("dismissed");
    wrap.remove();
  }
  doneBtn.addEventListener("click", dismiss);
  xBtn.addEventListener("click", dismiss);
  pfEvent("shown");
  return wrap;
}
function renderDemoChip(demo, meta) {
  const wrap = document.createElement("div");
  wrap.className = "demo-chip";
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "demo-chip__btn";
  btn.textContent = "\u25B6 Show me";
  btn.title = demo.title;
  wrap.appendChild(btn);
  const INTERACT_BASE = "https://mobius-interact-ortabkknqa-uc.a.run.app";
  btn.addEventListener("click", () => {
    btn.disabled = true;
    btn.textContent = "Loading\u2026";
    fetch(INTERACT_BASE + "/scripts/" + encodeURIComponent(demo.script_id)).then((r) => {
      if (!r.ok)
        throw new Error("script fetch " + r.status);
      return r.json();
    }).then((script) => {
      const MI = window["MobiusInteract"];
      if (!MI)
        throw new Error("MobiusInteract runner not loaded");
      btn.textContent = "\u25B6 Show me";
      btn.disabled = false;
      MI.run(script, {
        correlationId: meta.correlationId,
        onAbort: () => {
          btn.disabled = false;
        },
        onDone: () => {
          btn.disabled = false;
        }
      });
    }).catch(() => {
      btn.textContent = "\u25B6 Show me";
      btn.disabled = false;
    });
  });
  return wrap;
}
function renderOfferFeedback(offer, meta) {
  const wrap = document.createElement("div");
  wrap.className = "pf-offer-chip";
  const FALLBACK_PROMPTS = {
    nps: "How likely are you to recommend Mobius to a colleague?",
    csat: "How satisfied are you with this answer?",
    targeted_miss: "What were you trying to find?",
    generic: "Any feedback for us?"
  };
  const promptText = offer.prompt ?? FALLBACK_PROMPTS[offer.kind] ?? "Any feedback?";
  const header = document.createElement("div");
  header.className = "pf-offer-chip__header";
  const q = document.createElement("span");
  q.textContent = promptText;
  const xBtn = document.createElement("button");
  xBtn.type = "button";
  xBtn.className = "pf-offer-chip__x";
  xBtn.setAttribute("aria-label", "No thanks");
  xBtn.textContent = "\u2715";
  header.appendChild(q);
  header.appendChild(xBtn);
  wrap.appendChild(header);
  const body = document.createElement("div");
  body.className = "pf-offer-chip__body";
  wrap.appendChild(body);
  function pfEvent(action, score) {
    fetch(API_BASE + "/chat/product-feedback/event", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        trigger: offer.trigger,
        action,
        kind: offer.kind,
        score,
        thread_id: meta.threadId
      })
    }).catch(() => {
    });
  }
  function showThanks() {
    body.innerHTML = "";
    const t = document.createElement("span");
    t.className = "pf-offer-chip__thanks";
    t.textContent = "Thanks for your feedback!";
    body.appendChild(t);
    xBtn.remove();
    setTimeout(() => wrap.remove(), 2500);
  }
  function showFollowup(followupPrompt, parentFeedbackId) {
    body.innerHTML = "";
    const ta = document.createElement("textarea");
    ta.className = "pf-offer-chip__text";
    ta.rows = 2;
    ta.placeholder = followupPrompt;
    const row = document.createElement("div");
    row.className = "pf-offer-chip__followup-row";
    const skip = document.createElement("button");
    skip.type = "button";
    skip.className = "pf-offer-chip__skip";
    skip.textContent = "Skip";
    const submit = document.createElement("button");
    submit.type = "button";
    submit.className = "pf-offer-chip__submit";
    submit.textContent = "Send";
    row.appendChild(skip);
    row.appendChild(submit);
    body.appendChild(ta);
    body.appendChild(row);
    submit.addEventListener("click", () => {
      const txt = ta.value.trim();
      if (!txt) {
        showThanks();
        return;
      }
      fetch(API_BASE + "/chat/product-feedback", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          verbatim: txt,
          category: "other",
          trigger: offer.trigger,
          parent_feedback_id: parentFeedbackId,
          thread_id: meta.threadId,
          correlation_id: meta.correlationId
        })
      }).catch(() => {
      });
      showThanks();
    });
    skip.addEventListener("click", showThanks);
  }
  const isNumeric = offer.kind === "nps" || offer.kind === "csat";
  if (isNumeric) {
    const sc = offer.scale ?? (offer.kind === "nps" ? { min: 0, max: 10, min_label: "Not likely", max_label: "Very likely" } : { min: 1, max: 5, min_label: "Poor", max_label: "Great" });
    const scaleEl = document.createElement("div");
    scaleEl.className = "pf-offer-chip__scale";
    for (let i = sc.min; i <= sc.max; i++) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "pf-offer-chip__score-btn";
      btn.textContent = String(i);
      btn.addEventListener("click", () => {
        pfEvent("scored", i);
        const postTo = offer.post_to ?? "/chat/product-feedback/score";
        fetch(API_BASE + postTo, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            survey_type: offer.survey_type ?? offer.kind,
            score: i,
            trigger: offer.trigger,
            thread_id: meta.threadId,
            correlation_id: meta.correlationId
          })
        }).then((r) => r.json()).then((data) => {
          if (data.followup_prompt && data.feedback_id) {
            showFollowup(data.followup_prompt, data.feedback_id);
          } else {
            showThanks();
          }
        }).catch(showThanks);
      });
      scaleEl.appendChild(btn);
    }
    body.appendChild(scaleEl);
    const lbl = document.createElement("div");
    lbl.className = "pf-offer-chip__scale-labels";
    const lo = document.createElement("span");
    lo.textContent = sc.min_label;
    const hi = document.createElement("span");
    hi.textContent = sc.max_label;
    lbl.appendChild(lo);
    lbl.appendChild(hi);
    body.appendChild(lbl);
  } else {
    const ctaBtn = document.createElement("button");
    ctaBtn.type = "button";
    ctaBtn.className = "pf-offer-chip__cta";
    ctaBtn.textContent = offer.cta ?? "Share feedback";
    body.appendChild(ctaBtn);
    ctaBtn.addEventListener("click", () => {
      body.innerHTML = "";
      pfEvent("opened");
      const ta = document.createElement("textarea");
      ta.className = "pf-offer-chip__text";
      ta.rows = 2;
      ta.placeholder = "Your feedback\u2026";
      const submitBtn = document.createElement("button");
      submitBtn.type = "button";
      submitBtn.className = "pf-offer-chip__submit";
      submitBtn.textContent = "Submit";
      submitBtn.addEventListener("click", () => {
        const txt = ta.value.trim();
        if (!txt)
          return;
        const postTo = offer.post_to ?? "/chat/product-feedback";
        fetch(API_BASE + postTo, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            verbatim: txt,
            category: "other",
            trigger: offer.trigger,
            thread_id: meta.threadId,
            correlation_id: meta.correlationId
          })
        }).catch(() => {
        });
        pfEvent("submitted");
        showThanks();
      });
      body.appendChild(ta);
      body.appendChild(submitBtn);
      ta.focus();
    });
  }
  xBtn.addEventListener("click", () => {
    pfEvent("dismissed");
    wrap.remove();
  });
  pfEvent("shown");
  return wrap;
}
function openEmailThreadDialog(threadId) {
  if (document.querySelector(".email-thread-dialog"))
    return;
  const overlay = document.createElement("div");
  overlay.className = "email-thread-dialog-overlay";
  Object.assign(overlay.style, {
    position: "fixed",
    inset: "0",
    background: "rgba(0,0,0,0.4)",
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
    zIndex: "10000"
  });
  const dialog = document.createElement("div");
  dialog.className = "email-thread-dialog";
  dialog.setAttribute("role", "dialog");
  dialog.setAttribute("aria-modal", "true");
  dialog.setAttribute("aria-label", "Email this conversation");
  Object.assign(dialog.style, {
    background: "var(--main-bg, #fff)",
    color: "var(--main-text, #111)",
    borderRadius: "8px",
    padding: "20px",
    width: "min(560px, 92vw)",
    maxHeight: "92vh",
    overflowY: "auto",
    boxShadow: "0 8px 32px rgba(0,0,0,0.18)",
    fontFamily: "inherit"
  });
  const title = document.createElement("h3");
  title.textContent = "Email this conversation";
  Object.assign(title.style, { margin: "0 0 12px 0", fontSize: "1.05rem" });
  dialog.appendChild(title);
  const toLabel = document.createElement("label");
  toLabel.textContent = "Send to";
  Object.assign(toLabel.style, {
    display: "block",
    fontSize: "0.85rem",
    marginBottom: "4px",
    color: "var(--sidebar-text-muted, #555)"
  });
  const toInput = document.createElement("input");
  toInput.type = "email";
  toInput.placeholder = "name@example.com";
  toInput.required = true;
  Object.assign(toInput.style, {
    width: "100%",
    boxSizing: "border-box",
    padding: "8px 10px",
    border: "1px solid var(--border, #ccc)",
    borderRadius: "4px",
    fontSize: "0.95rem",
    marginBottom: "14px"
  });
  const scopeLabel = document.createElement("div");
  scopeLabel.textContent = "What to include";
  Object.assign(scopeLabel.style, {
    fontSize: "0.85rem",
    marginBottom: "4px",
    color: "var(--sidebar-text-muted, #555)"
  });
  const scopeWrap = document.createElement("div");
  Object.assign(scopeWrap.style, { display: "flex", gap: "16px", marginBottom: "14px" });
  const scopeThread = _radio("scope", "thread", "Whole thread", true);
  const scopeLast = _radio("scope", "last", "Last exchange", false);
  scopeWrap.appendChild(scopeThread.wrap);
  scopeWrap.appendChild(scopeLast.wrap);
  const modeLabel = document.createElement("div");
  modeLabel.textContent = "How to format";
  Object.assign(modeLabel.style, {
    fontSize: "0.85rem",
    marginBottom: "4px",
    color: "var(--sidebar-text-muted, #555)"
  });
  const modeWrap = document.createElement("div");
  Object.assign(modeWrap.style, { display: "flex", gap: "16px", marginBottom: "14px" });
  const modeSummary = _radio("mode", "summary", "Summarize (LLM)", true);
  const modeFull = _radio("mode", "full", "Full transcript", false);
  modeWrap.appendChild(modeSummary.wrap);
  modeWrap.appendChild(modeFull.wrap);
  const preview = document.createElement("div");
  preview.className = "email-thread-preview";
  Object.assign(preview.style, {
    display: "none",
    border: "1px solid var(--border, #ccc)",
    borderRadius: "4px",
    padding: "10px 12px",
    marginBottom: "12px",
    background: "var(--thinking-bg, #fafafa)",
    maxHeight: "260px",
    overflowY: "auto",
    whiteSpace: "pre-wrap",
    fontSize: "0.85rem"
  });
  const status = document.createElement("div");
  Object.assign(status.style, {
    fontSize: "0.85rem",
    marginBottom: "10px",
    color: "var(--sidebar-text-muted, #666)",
    minHeight: "18px"
  });
  const btnRow = document.createElement("div");
  Object.assign(btnRow.style, { display: "flex", gap: "8px", justifyContent: "flex-end" });
  const cancelBtn = document.createElement("button");
  cancelBtn.type = "button";
  cancelBtn.textContent = "Cancel";
  Object.assign(cancelBtn.style, _btnStyle("secondary"));
  const previewBtn = document.createElement("button");
  previewBtn.type = "button";
  previewBtn.textContent = "Preview";
  Object.assign(previewBtn.style, _btnStyle("primary"));
  const sendBtn = document.createElement("button");
  sendBtn.type = "button";
  sendBtn.textContent = "Send";
  Object.assign(sendBtn.style, _btnStyle("primary"));
  sendBtn.style.display = "none";
  btnRow.appendChild(cancelBtn);
  btnRow.appendChild(previewBtn);
  btnRow.appendChild(sendBtn);
  dialog.appendChild(toLabel);
  dialog.appendChild(toInput);
  dialog.appendChild(scopeLabel);
  dialog.appendChild(scopeWrap);
  dialog.appendChild(modeLabel);
  dialog.appendChild(modeWrap);
  dialog.appendChild(preview);
  dialog.appendChild(status);
  dialog.appendChild(btnRow);
  overlay.appendChild(dialog);
  document.body.appendChild(overlay);
  setTimeout(() => toInput.focus(), 50);
  const close = () => overlay.remove();
  cancelBtn.addEventListener("click", close);
  overlay.addEventListener("click", (ev) => {
    if (ev.target === overlay)
      close();
  });
  let lockedPayload = null;
  const setBusy = (busy) => {
    previewBtn.disabled = busy;
    sendBtn.disabled = busy;
    toInput.disabled = busy;
    [scopeThread.input, scopeLast.input, modeSummary.input, modeFull.input].forEach((el2) => {
      el2.disabled = busy;
    });
  };
  previewBtn.addEventListener("click", async () => {
    const to = (toInput.value || "").trim();
    if (!to || !to.includes("@")) {
      status.textContent = "Enter a valid email address.";
      status.style.color = "#c0392b";
      return;
    }
    const scope = scopeThread.input.checked ? "thread" : "last";
    const mode = modeSummary.input.checked ? "summary" : "full";
    status.textContent = "Drafting\u2026";
    status.style.color = "var(--sidebar-text-muted, #666)";
    setBusy(true);
    try {
      const res = await fetch(`${API_BASE}/chat/thread/${encodeURIComponent(threadId)}/email`, {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ to: [to], scope, mode, confirm_before_send: true })
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        status.textContent = `Failed: ${data && (data.detail?.message || data.detail) || res.statusText}`;
        status.style.color = "#c0392b";
        return;
      }
      const draft = data.draft || {};
      preview.style.display = "block";
      preview.textContent = `To: ${(draft.to || []).join(", ")}
Subject: ${draft.subject || ""}

${draft.body || ""}`;
      status.textContent = "Review the draft, then click Send.";
      status.style.color = "var(--sidebar-text-muted, #666)";
      sendBtn.style.display = "";
      previewBtn.textContent = "Re-draft";
      lockedPayload = { to: [to], scope, mode };
    } catch (err) {
      status.textContent = `Error: ${err?.message || err}`;
      status.style.color = "#c0392b";
    } finally {
      setBusy(false);
    }
  });
  sendBtn.addEventListener("click", async () => {
    if (!lockedPayload)
      return;
    setBusy(true);
    status.textContent = "Sending\u2026";
    status.style.color = "var(--sidebar-text-muted, #666)";
    try {
      const res = await fetch(`${API_BASE}/chat/thread/${encodeURIComponent(threadId)}/email`, {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ...lockedPayload, confirm_before_send: false })
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok || !data.sent) {
        status.textContent = `Send failed: ${data && (data.error || data.detail?.message || data.detail) || res.statusText}`;
        status.style.color = "#c0392b";
        sendBtn.disabled = false;
        return;
      }
      _showToast("Email sent");
      close();
    } catch (err) {
      status.textContent = `Error: ${err?.message || err}`;
      status.style.color = "#c0392b";
      setBusy(false);
    }
  });
}
function _radio(name, value, label, checked) {
  const wrap = document.createElement("label");
  Object.assign(wrap.style, {
    display: "flex",
    alignItems: "center",
    gap: "6px",
    fontSize: "0.9rem",
    cursor: "pointer"
  });
  const input = document.createElement("input");
  input.type = "radio";
  input.name = name;
  input.value = value;
  input.checked = checked;
  const span = document.createElement("span");
  span.textContent = label;
  wrap.appendChild(input);
  wrap.appendChild(span);
  return { wrap, input };
}
function _btnStyle(variant) {
  const base = {
    padding: "8px 14px",
    borderRadius: "4px",
    border: "1px solid",
    fontSize: "0.9rem",
    cursor: "pointer"
  };
  if (variant === "primary") {
    base.background = "var(--primary, #2563eb)";
    base.color = "#fff";
    base.borderColor = "var(--primary, #2563eb)";
  } else {
    base.background = "transparent";
    base.color = "var(--foreground, #111)";
    base.borderColor = "var(--border, #ccc)";
  }
  return base;
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
function openRosterPanel(url) {
  let overlay = document.getElementById("roster-panel-overlay");
  let panel = document.getElementById("roster-panel");
  if (!overlay) {
    overlay = document.createElement("div");
    overlay.id = "roster-panel-overlay";
    overlay.addEventListener("click", closeRosterPanel);
    document.body.appendChild(overlay);
  }
  if (!panel) {
    panel = document.createElement("div");
    panel.id = "roster-panel";
    panel.innerHTML = '<div class="roster-panel-header"><span class="roster-panel-title">Roster</span><div class="roster-panel-header-actions"><a class="roster-panel-external" href="#" target="_blank" rel="noopener noreferrer" title="Open in new tab">&#8599;</a><button class="roster-panel-close" title="Close">&times;</button></div></div><iframe class="roster-panel-frame" src="" allow="same-origin" sandbox="allow-same-origin allow-scripts allow-forms allow-popups"></iframe>';
    panel.querySelector(".roster-panel-close").addEventListener("click", closeRosterPanel);
    document.body.appendChild(panel);
  }
  const frame = panel.querySelector(".roster-panel-frame");
  const extLink = panel.querySelector(".roster-panel-external");
  frame.src = url;
  extLink.href = url;
  requestAnimationFrame(() => {
    overlay.classList.add("open");
    panel.classList.add("open");
  });
}
function closeRosterPanel() {
  document.getElementById("roster-panel-overlay")?.classList.remove("open");
  document.getElementById("roster-panel")?.classList.remove("open");
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
    cite: '<svg viewBox="0 0 16 16" fill="currentColor"><path d="M1.75 2h12.5c.966 0 1.75.784 1.75 1.75v8.5A1.75 1.75 0 0114.25 14H1.75A1.75 1.75 0 010 12.25v-8.5C0 2.784.784 2 1.75 2zm0 1.5a.25.25 0 00-.25.25v8.5c0 .138.112.25.25.25h12.5a.25.25 0 00.25-.25v-8.5a.25.25 0 00-.25-.25zM3.5 6.25a.75.75 0 01.75-.75h7.5a.75.75 0 010 1.5h-7.5a.75.75 0 01-.75-.75zm.75 2.25a.75.75 0 000 1.5h4a.75.75 0 000-1.5z"/></svg>',
    task: '<svg viewBox="0 0 16 16" fill="currentColor"><path d="M2.5 1.75a.25.25 0 01.25-.25h8.5a.25.25 0 01.25.25v.5h1.5v-.5A1.75 1.75 0 0011.25 0h-8.5A1.75 1.75 0 001 1.75v12.5c0 .966.784 1.75 1.75 1.75h4.5a.75.75 0 000-1.5h-4.5a.25.25 0 01-.25-.25zM4.75 4a.75.75 0 000 1.5h4.5a.75.75 0 000-1.5zm0 3a.75.75 0 000 1.5h2.5a.75.75 0 000-1.5zm10.28 2.72a.75.75 0 00-1.06-1.06L10.5 12.13l-1.47-1.47a.75.75 0 10-1.06 1.06l2 2a.75.75 0 001.06 0z"/></svg>'
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
      if (container.closest(".pf-capture-card") || container.closest(".pf-offer-chip") || container.closest(".pf-survey") || container.closest(".feedback"))
        return;
      if (!container.closest(".envelope-detail-body") && !container.closest("#doc-reader-panel .doc-reader-content") && !container.closest(".message-bubble"))
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
      const d3 = document.createElement("span");
      d3.className = "tst-divider";
      toolbar.appendChild(d3);
      const taskBtn = document.createElement("button");
      taskBtn.innerHTML = _svgIcon("task") + " Create task";
      taskBtn.addEventListener("click", (ev) => {
        ev.stopPropagation();
        const tid = window.__mobiusChatThreadId || "";
        let h = 0;
        for (let i = 0; i < text.length; i++) {
          h = (h << 5) - h + text.charCodeAt(i) | 0;
        }
        openCreateTaskDialog({
          excerpt: text.slice(0, 600),
          title: text.slice(0, 60),
          sourceModule: "chat_highlight",
          sourceRef: `highlight:${tid || "nothread"}:${(h >>> 0).toString(16)}`
        });
        _removeToolbar();
      });
      toolbar.appendChild(taskBtn);
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
var _tasksModalEl = null;
var _tasksEscHandler = null;
var _TASK_SEVERITIES = ["critical", "warning", "info", "low", "none"];
var _ctdOverlayEl = null;
var _ctdEscHandler = null;
function closeCreateTaskDialog() {
  if (_ctdOverlayEl) {
    _ctdOverlayEl.remove();
    _ctdOverlayEl = null;
  }
  if (_ctdEscHandler) {
    document.removeEventListener("keydown", _ctdEscHandler);
    _ctdEscHandler = null;
  }
}
function openCreateTaskDialog(opts) {
  closeCreateTaskDialog();
  const overlay = document.createElement("div");
  overlay.className = "ctd-overlay";
  overlay.addEventListener("mousedown", (e) => {
    if (e.target === overlay)
      closeCreateTaskDialog();
  });
  _ctdEscHandler = (e) => {
    if (e.key === "Escape")
      closeCreateTaskDialog();
  };
  document.addEventListener("keydown", _ctdEscHandler);
  const dialog = document.createElement("div");
  dialog.className = "ctd-dialog";
  dialog.setAttribute("role", "dialog");
  dialog.setAttribute("aria-modal", "true");
  dialog.setAttribute("aria-label", "Create task");
  const header = document.createElement("div");
  header.className = "ctd-header";
  const titleEl = document.createElement("span");
  titleEl.className = "ctd-title";
  titleEl.textContent = "Create task";
  const closeBtn = document.createElement("button");
  closeBtn.type = "button";
  closeBtn.className = "ctd-close";
  closeBtn.setAttribute("aria-label", "Close");
  closeBtn.innerHTML = "&times;";
  closeBtn.addEventListener("click", closeCreateTaskDialog);
  header.appendChild(titleEl);
  header.appendChild(closeBtn);
  dialog.appendChild(header);
  const excerptEl = document.createElement("div");
  excerptEl.className = "ctd-excerpt";
  if (opts?.excerpt) {
    const bar = document.createElement("div");
    bar.className = "ctd-excerpt__bar";
    const txt = document.createElement("div");
    txt.className = "ctd-excerpt__text";
    txt.textContent = opts.excerpt;
    excerptEl.appendChild(bar);
    excerptEl.appendChild(txt);
  } else {
    excerptEl.hidden = true;
  }
  dialog.appendChild(excerptEl);
  const body = document.createElement("div");
  body.className = "ctd-body";
  body.innerHTML = `
    <input type="text" class="ctd-input" data-f="title" placeholder="Task title" maxlength="160">
    <textarea class="ctd-input ctd-textarea" data-f="text" placeholder="What needs to be done?" rows="3"></textarea>
    <input type="text" class="ctd-input" data-f="org" placeholder="Organization (required)">
    <details class="ctd-advanced">
      <summary class="ctd-advanced__trigger">Advanced</summary>
      <div class="ctd-advanced__body">
        <div class="ctd-row">
          <select class="ctd-input" data-f="severity">
            ${_TASK_SEVERITIES.map((s) => `<option value="${s}" ${s === "low" ? "selected" : ""}>${s}</option>`).join("")}
          </select>
          <input type="text" class="ctd-input" data-f="assignee" placeholder="Assignee (optional)">
        </div>
        <div class="ctd-row">
          <select class="ctd-input" data-f="kind">
            <option value="work_item" selected>Task</option>
            <option value="reminder">Reminder</option>
          </select>
          <input type="date" class="ctd-input" data-f="deadline">
        </div>
      </div>
    </details>`;
  const cf = (k) => body.querySelector(`[data-f="${k}"]`);
  cf("title").value = opts?.title || (opts?.excerpt || "").slice(0, 60);
  cf("text").value = opts?.excerpt || "";
  cf("org").value = localStorage.getItem("lastOrg") || "";
  dialog.appendChild(body);
  const footer = document.createElement("div");
  footer.className = "ctd-footer";
  const errEl = document.createElement("span");
  errEl.className = "ctd-err";
  const cancelBtn = document.createElement("button");
  cancelBtn.type = "button";
  cancelBtn.className = "ctd-btn ctd-btn--cancel";
  cancelBtn.textContent = "Cancel";
  cancelBtn.addEventListener("click", closeCreateTaskDialog);
  const submitBtn = document.createElement("button");
  submitBtn.type = "button";
  submitBtn.className = "ctd-btn ctd-btn--create";
  submitBtn.textContent = "Create task";
  footer.appendChild(errEl);
  footer.appendChild(cancelBtn);
  footer.appendChild(submitBtn);
  dialog.appendChild(footer);
  submitBtn.addEventListener("click", async () => {
    const text = cf("text").value.trim();
    const org = cf("org").value.trim();
    if (!text || !org) {
      errEl.textContent = "Organization and description are required.";
      return;
    }
    errEl.textContent = "";
    submitBtn.disabled = true;
    const body2 = {
      org_name: org,
      text,
      title: cf("title").value.trim() || text.slice(0, 60),
      severity: cf("severity").value,
      source_module: opts?.sourceModule || "manual",
      kind: cf("kind").value || "work_item",
      audience: "user"
    };
    const deadline = cf("deadline").value;
    if (deadline)
      body2.deadline = deadline;
    const assignee = cf("assignee").value.trim();
    if (assignee)
      body2.assignee = assignee;
    if (opts?.sourceRef)
      body2.source_ref = opts.sourceRef;
    const tid = window.__mobiusChatThreadId;
    if (tid)
      body2.extra = { origin: { thread_id: tid } };
    try {
      const r = await apiFetch(`${API_BASE}/chat/tasks`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body2)
      });
      if (!r.ok) {
        errEl.textContent = `Create failed (${r.status}).`;
        submitBtn.disabled = false;
        return;
      }
      localStorage.setItem("lastOrg", org);
      submitBtn.classList.add("ctd-btn--success");
      submitBtn.textContent = "Created \u2713";
      try {
        opts?.onCreated?.();
      } catch {
      }
      setTimeout(closeCreateTaskDialog, 900);
    } catch {
      errEl.textContent = "Create failed \u2014 network error.";
      submitBtn.disabled = false;
    }
  });
  overlay.appendChild(dialog);
  document.body.appendChild(overlay);
  _ctdOverlayEl = overlay;
  setTimeout(() => cf("title").focus(), 50);
}
function closeTasksModal() {
  if (_tasksModalEl) {
    _tasksModalEl.remove();
    _tasksModalEl = null;
  }
  if (_tasksEscHandler) {
    document.removeEventListener("keydown", _tasksEscHandler);
    _tasksEscHandler = null;
  }
}
function openTasksModal(prefill) {
  closeTasksModal();
  const overlay = document.createElement("div");
  overlay.className = "tasks-modal-overlay";
  overlay.addEventListener("mousedown", (e) => {
    if (e.target === overlay)
      closeTasksModal();
  });
  _tasksEscHandler = (e) => {
    if (e.key === "Escape")
      closeTasksModal();
  };
  document.addEventListener("keydown", _tasksEscHandler);
  const panel = document.createElement("div");
  panel.className = "tasks-modal";
  const header = document.createElement("div");
  header.className = "tasks-modal-header";
  header.innerHTML = `<span class="tasks-modal-title">${_svgIcon("task")} Tasks</span>`;
  const headerBtns = document.createElement("div");
  headerBtns.className = "tasks-modal-header-btns";
  const newBtn = document.createElement("button");
  newBtn.type = "button";
  newBtn.className = "tm-env-btn tm-env-btn--create-action";
  newBtn.textContent = "+ New task";
  const closeBtn = document.createElement("button");
  closeBtn.type = "button";
  closeBtn.className = "tasks-modal-close";
  closeBtn.innerHTML = "&times;";
  closeBtn.addEventListener("click", closeTasksModal);
  headerBtns.appendChild(newBtn);
  headerBtns.appendChild(closeBtn);
  header.appendChild(headerBtns);
  panel.appendChild(header);
  const openCreate = () => openCreateTaskDialog({
    title: prefill?.title,
    excerpt: prefill?.text,
    sourceModule: prefill?.sourceModule,
    sourceRef: prefill?.sourceRef,
    onCreated: () => void loadList()
  });
  newBtn.addEventListener("click", openCreate);
  const presets = document.createElement("div");
  presets.className = "tasks-modal-presets";
  const PRESET_DEFS = [
    { key: "mine", label: "My open tasks" },
    { key: "due", label: "Due soon" },
    { key: "all", label: "All" }
  ];
  let activePreset = "mine";
  const presetBtns = {};
  for (const p of PRESET_DEFS) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "tasks-modal-preset";
    b.textContent = p.label;
    b.addEventListener("click", () => {
      void applyPreset(p.key);
    });
    presets.appendChild(b);
    presetBtns[p.key] = b;
  }
  panel.appendChild(presets);
  const moreFilters = document.createElement("details");
  moreFilters.className = "tasks-modal-more-filters";
  moreFilters.innerHTML = `<summary>More filters</summary>`;
  const filters = document.createElement("div");
  filters.className = "tasks-modal-filters";
  filters.innerHTML = `
    <select class="tasks-modal-input" data-f="status">
      <option value="open" selected>Open</option>
      <option value="in_progress">In progress</option>
      <option value="resolved">Resolved</option>
      <option value="dismissed">Dismissed</option>
      <option value="">All</option>
    </select>
    <select class="tasks-modal-input" data-f="audience" title="System tasks (telemetry, pipeline signals) are hidden by default">
      <option value="user" selected>My tasks</option>
      <option value="developer">System (dev)</option>
      <option value="all">All audiences</option>
    </select>
    <select class="tasks-modal-input" data-f="kind">
      <option value="" selected>Any kind</option>
      <option value="work_item">Work items</option>
      <option value="reminder">Reminders</option>
      <option value="signal">Signals</option>
    </select>
    <input type="text" class="tasks-modal-input" data-f="org" placeholder="Org filter">
    <input type="text" class="tasks-modal-input" data-f="assignee" placeholder="Assignee filter">
    <button type="button" class="tm-env-btn" data-f="apply">Apply</button>`;
  moreFilters.appendChild(filters);
  panel.appendChild(moreFilters);
  const fEl = (k) => filters.querySelector(`[data-f="${k}"]`);
  const setSelect = (k, v) => {
    fEl(k).value = v;
  };
  function markPreset(key) {
    activePreset = key || "";
    for (const [k, b] of Object.entries(presetBtns)) {
      b.classList.toggle("tasks-modal-preset--active", k === key);
    }
  }
  async function applyPreset(key) {
    markPreset(key);
    if (key === "mine") {
      setSelect("status", "open");
      setSelect("audience", "user");
      setSelect("kind", "");
      fEl("org").value = "";
      const me = await _getWhoami();
      fEl("assignee").value = me ? me.assignee_ref : "";
    } else if (key === "due") {
      setSelect("status", "open");
      setSelect("audience", "all");
      setSelect("kind", "reminder");
      fEl("org").value = "";
      fEl("assignee").value = "";
    } else {
      setSelect("status", "");
      setSelect("audience", "all");
      setSelect("kind", "");
      fEl("org").value = "";
      fEl("assignee").value = "";
    }
    void loadList();
  }
  const listWrap = document.createElement("div");
  listWrap.className = "tasks-modal-list";
  panel.appendChild(listWrap);
  const SEV_BUCKETS = [
    { label: "critical", sevs: ["critical"] },
    { label: "warning", sevs: ["warning"] },
    { label: "info", sevs: ["info", "low", "none"] }
  ];
  async function loadList() {
    listWrap.innerHTML = `<div class="tasks-modal-skeleton-row"></div><div class="tasks-modal-skeleton-row"></div><div class="tasks-modal-skeleton-row"></div>`;
    const ff = (k) => fEl(k).value.trim();
    const params = new URLSearchParams({ limit: "100" });
    if (ff("status"))
      params.set("status", ff("status"));
    params.set("audience", ff("audience") || "user");
    if (ff("kind"))
      params.set("kind", ff("kind"));
    if (ff("org"))
      params.set("org_name", ff("org"));
    if (ff("assignee"))
      params.set("assignee", ff("assignee"));
    try {
      const r = await apiFetch(`${API_BASE}/chat/tasks?${params.toString()}`);
      const data = await r.json();
      let tasks = data.tasks || [];
      if (activePreset === "due") {
        const horizon = new Date(Date.now() + 7 * 24 * 60 * 60 * 1e3).toISOString().slice(0, 10);
        tasks = tasks.filter((t) => {
          const d = String(t.deadline || t.due_at || "").slice(0, 10);
          return d && d <= horizon;
        });
      }
      listWrap.innerHTML = "";
      if (!tasks.length) {
        listWrap.innerHTML = `
          <div class="tasks-modal-empty">
            <svg class="tasks-modal-empty-icon" viewBox="0 0 16 16" fill="currentColor"><path d="M13.78 4.22a.75.75 0 010 1.06l-7.25 7.25a.75.75 0 01-1.06 0L2.22 9.28a.75.75 0 011.06-1.06L6 10.94l6.72-6.72a.75.75 0 011.06 0z"/></svg>
            <p class="tasks-modal-empty-headline">All clear</p>
            <p class="tasks-modal-empty-sub">No tasks match \xB7 try another view or create one</p>
          </div>`;
        return;
      }
      const open = tasks.filter((t) => t.status === "open" || t.status === "in_progress" || t.status === "running");
      const closed = tasks.filter((t) => !open.includes(t));
      for (const bucket of SEV_BUCKETS) {
        const rows = open.filter((t) => bucket.sevs.includes((t.severity || "low").toLowerCase()));
        if (!rows.length)
          continue;
        const gh = document.createElement("div");
        gh.className = "tasks-modal-group-header";
        gh.innerHTML = `<span class="tm-env-badge tm-env-badge--${bucket.label}">${bucket.label}</span><span class="tasks-modal-group-count">${rows.length}</span>`;
        listWrap.appendChild(gh);
        for (const t of rows)
          listWrap.appendChild(_taskModalRow(t, loadList));
      }
      if (closed.length) {
        const det = document.createElement("details");
        det.className = "tasks-modal-closed";
        det.innerHTML = `<summary>Closed \u2014 ${closed.length} item${closed.length > 1 ? "s" : ""}</summary>`;
        for (const t of closed)
          det.appendChild(_taskModalRow(t, loadList));
        listWrap.appendChild(det);
      }
    } catch {
      listWrap.innerHTML = `<div class="tasks-modal-loading">Failed to load tasks.</div>`;
    }
  }
  filters.querySelector('[data-f="apply"]').addEventListener("click", () => {
    markPreset(null);
    void loadList();
  });
  overlay.appendChild(panel);
  document.body.appendChild(overlay);
  _tasksModalEl = overlay;
  if (prefill?.createOpen) {
    openCreate();
  }
  if (prefill?.filterKind === "reminder" && !prefill?.filterAssignee) {
    void applyPreset("due");
  } else if (prefill?.filterAssignee && !prefill?.filterKind) {
    markPreset("mine");
    setSelect("status", "open");
    setSelect("audience", "user");
    setSelect("kind", "");
    fEl("assignee").value = prefill.filterAssignee;
    void loadList();
  } else if (prefill?.filterKind || prefill?.filterAssignee) {
    markPreset(null);
    moreFilters.open = true;
    if (prefill.filterKind)
      setSelect("kind", prefill.filterKind);
    if (prefill.filterAssignee)
      fEl("assignee").value = prefill.filterAssignee;
    void loadList();
  } else {
    void applyPreset("mine");
  }
}
var _NUDGE_LAST_KEY = "mobius_reminder_nudge_last";
var _NUDGE_SNOOZE_KEY = "mobius_reminder_nudge_snooze";
var _NUDGE_MIN_GAP_MS = 4 * 60 * 60 * 1e3;
var _NUDGE_SNOOZE_MS = 24 * 60 * 60 * 1e3;
var _nudgeInFlight = false;
var _authRef = null;
async function apiFetch(url, init = {}) {
  const authHdrs = _authRef?.getAuthHeader ? await _authRef.getAuthHeader() : null;
  const merged = {
    ...init,
    headers: { ...authHdrs ?? {}, ...init.headers ?? {} }
  };
  return fetch(url, merged);
}
var _whoami = null;
var _whoamiFetched = false;
async function _getWhoami() {
  if (_whoamiFetched === true)
    return _whoami;
  _whoamiFetched = true;
  try {
    const r = await apiFetch(`${API_BASE}/chat/whoami`);
    if (r.ok) {
      const d = await r.json();
      if (d.ok && d.user?.assignee_ref) {
        _whoami = d.user;
        return _whoami;
      }
    }
  } catch {
  }
  _whoamiFetched = "miss";
  return _whoami;
}
async function _maybeShowReminderNudge() {
  if (_nudgeInFlight || document.querySelector(".reminder-nudge"))
    return;
  const now = Date.now();
  const last = Number(localStorage.getItem(_NUDGE_LAST_KEY) || 0);
  const snooze = Number(localStorage.getItem(_NUDGE_SNOOZE_KEY) || 0);
  if (now - last < _NUDGE_MIN_GAP_MS || now < snooze)
    return;
  _nudgeInFlight = true;
  try {
    const me = await _getWhoami();
    const scope = me ? `&assignee=${encodeURIComponent(me.assignee_ref)}` : "";
    const r = await apiFetch(`${API_BASE}/chat/tasks?kind=reminder&status=open&limit=20${scope}`);
    if (!r.ok)
      return;
    const tasks = (await r.json()).tasks || [];
    const today = (/* @__PURE__ */ new Date()).toISOString().slice(0, 10);
    const due = tasks.filter((t) => {
      const d = String(t.deadline || t.due_at || "").slice(0, 10);
      return d && d <= today;
    });
    if (!due.length)
      return;
    const anchor = document.querySelector(".composer-wrap");
    if (!anchor || !anchor.parentElement)
      return;
    localStorage.setItem(_NUDGE_LAST_KEY, String(now));
    const chip = document.createElement("div");
    chip.className = "reminder-nudge";
    const label = document.createElement("span");
    label.className = "reminder-nudge-label";
    label.innerHTML = `${_svgIcon("task")} <strong>${due.length}</strong> reminder${due.length > 1 ? "s" : ""} due \u2014 ${(due[0].title || due[0].text || "").slice(0, 60)}${due.length > 1 ? ", \u2026" : ""}`;
    const viewBtn = document.createElement("button");
    viewBtn.type = "button";
    viewBtn.className = "reminder-nudge-view";
    viewBtn.textContent = "View";
    viewBtn.addEventListener("click", () => {
      chip.remove();
      openTasksModal({ filterKind: "reminder" });
    });
    const dismissBtn = document.createElement("button");
    dismissBtn.type = "button";
    dismissBtn.className = "reminder-nudge-dismiss";
    dismissBtn.setAttribute("aria-label", "Dismiss for a day");
    dismissBtn.innerHTML = "&times;";
    dismissBtn.addEventListener("click", () => {
      localStorage.setItem(_NUDGE_SNOOZE_KEY, String(Date.now() + _NUDGE_SNOOZE_MS));
      chip.remove();
    });
    chip.appendChild(label);
    chip.appendChild(viewBtn);
    chip.appendChild(dismissBtn);
    anchor.parentElement.insertBefore(chip, anchor);
    setTimeout(() => chip.remove(), 3e4);
  } catch {
  } finally {
    _nudgeInFlight = false;
  }
}
var _BANNER_LAST_KEY = "mobius_assigned_banner_last";
var _BANNER_SNOOZE_KEY = "mobius_assigned_banner_snooze";
async function _maybeShowAssignedBanner() {
  if (document.querySelector(".reminder-nudge--assigned"))
    return;
  const now = Date.now();
  if (now - Number(localStorage.getItem(_BANNER_LAST_KEY) || 0) < _NUDGE_MIN_GAP_MS)
    return;
  if (now < Number(localStorage.getItem(_BANNER_SNOOZE_KEY) || 0))
    return;
  const me = await _getWhoami();
  if (!me)
    return;
  try {
    const r = await apiFetch(`${API_BASE}/chat/tasks?status=open&kind=work_item&assignee=${encodeURIComponent(me.assignee_ref)}&limit=50`);
    if (!r.ok)
      return;
    const tasks = (await r.json()).tasks || [];
    if (!tasks.length)
      return;
    const anchor = document.querySelector(".composer-wrap");
    if (!anchor || !anchor.parentElement)
      return;
    localStorage.setItem(_BANNER_LAST_KEY, String(now));
    const chip = document.createElement("div");
    chip.className = "reminder-nudge reminder-nudge--assigned";
    const label = document.createElement("span");
    label.className = "reminder-nudge-label";
    label.innerHTML = `${_svgIcon("task")} <strong>${tasks.length}</strong> open task${tasks.length > 1 ? "s" : ""} assigned to you`;
    const viewBtn = document.createElement("button");
    viewBtn.type = "button";
    viewBtn.className = "reminder-nudge-view";
    viewBtn.textContent = "View";
    viewBtn.addEventListener("click", () => {
      chip.remove();
      openTasksModal({ filterAssignee: me.assignee_ref });
    });
    const dismissBtn = document.createElement("button");
    dismissBtn.type = "button";
    dismissBtn.className = "reminder-nudge-dismiss";
    dismissBtn.setAttribute("aria-label", "Dismiss for a day");
    dismissBtn.innerHTML = "&times;";
    dismissBtn.addEventListener("click", () => {
      localStorage.setItem(_BANNER_SNOOZE_KEY, String(Date.now() + _NUDGE_SNOOZE_MS));
      chip.remove();
    });
    chip.appendChild(label);
    chip.appendChild(viewBtn);
    chip.appendChild(dismissBtn);
    anchor.parentElement.insertBefore(chip, anchor);
    setTimeout(() => chip.remove(), 3e4);
  } catch {
  }
}
async function _maybeShowDocReadyNudge() {
  if (document.querySelector(".rag-doc-ready-nudge"))
    return;
  const me = await _getWhoami();
  if (!me)
    return;
  try {
    const r = await apiFetch(`${API_BASE}/chat/tasks?kind=notification&status=open&limit=10`);
    if (!r.ok)
      return;
    const tasks = (await r.json()).tasks || [];
    const docReadyTasks = tasks.filter((t) => t.type === "doc_ready");
    if (!docReadyTasks.length)
      return;
    const anchor = document.querySelector(".composer-wrap");
    if (!anchor || !anchor.parentElement)
      return;
    for (const task of docReadyTasks.slice(0, 3)) {
      const detail = task.detail_payload || {};
      const fname = detail.filename || task.title || "Document";
      const docId = detail.document_id || "";
      const tid = detail.thread_id || "";
      const chip = document.createElement("div");
      chip.className = "reminder-nudge rag-doc-ready-nudge";
      const label = document.createElement("span");
      label.className = "reminder-nudge-label";
      label.textContent = `\u{1F4C4} "${fname}" is ready`;
      const askBtn = document.createElement("button");
      askBtn.type = "button";
      askBtn.className = "reminder-nudge-view";
      askBtn.textContent = "Ask now";
      askBtn.addEventListener("click", () => {
        chip.remove();
        apiFetch(`${API_BASE}/chat/tasks/${task.id}/dismiss`, { method: "POST" }).catch(() => {
        });
        const inputEl = document.getElementById("input");
        if (inputEl && !inputEl.value.trim()) {
          inputEl.value = `Tell me about "${fname}"`;
          inputEl.dispatchEvent(new Event("input"));
          inputEl.focus();
        }
      });
      const dismissBtn = document.createElement("button");
      dismissBtn.type = "button";
      dismissBtn.className = "reminder-nudge-dismiss";
      dismissBtn.setAttribute("aria-label", "Dismiss");
      dismissBtn.innerHTML = "&times;";
      dismissBtn.addEventListener("click", () => {
        chip.remove();
        apiFetch(`${API_BASE}/chat/tasks/${task.id}/dismiss`, { method: "POST" }).catch(() => {
        });
      });
      chip.appendChild(label);
      chip.appendChild(askBtn);
      chip.appendChild(dismissBtn);
      anchor.parentElement.insertBefore(chip, anchor);
      setTimeout(() => chip.remove(), 3e4);
    }
  } catch {
  }
}
function _initReminderNudge() {
  setTimeout(() => void _maybeShowReminderNudge(), 2500);
  setTimeout(() => void _maybeShowAssignedBanner(), 4e3);
  setTimeout(() => void _maybeShowDocReadyNudge(), 5e3);
  document.getElementById("send")?.addEventListener("click", () => void _maybeShowReminderNudge());
  document.getElementById("input")?.addEventListener("keydown", (e) => {
    if (e.key === "Enter")
      void _maybeShowReminderNudge();
  });
}
if (typeof document !== "undefined") {
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", _initReminderNudge);
  } else {
    _initReminderNudge();
  }
}
async function _maybeShowGreeting() {
  const el2 = document.getElementById("mainHeaderTitle");
  if (!el2)
    return;
  const me = await _getWhoami();
  if (!me || !me.greeting?.enabled || !me.greeting?.name)
    return;
  const h = (/* @__PURE__ */ new Date()).getHours();
  const salutation = h < 12 ? "Good morning" : h < 17 ? "Good afternoon" : "Good evening";
  el2.textContent = `${salutation}, ${me.greeting.name}.`;
  el2.classList.add("chat-greeting");
}
function _taskModalRow(t, reload) {
  const row = document.createElement("div");
  row.className = "tasks-modal-row";
  const sev = (t.severity || "low").toLowerCase();
  const status = (t.status || "open").toLowerCase();
  const title = t.title || t.text || "(no title)";
  const head = document.createElement("div");
  head.className = "tasks-modal-row-head";
  const due = t.kind === "reminder" && (t.deadline || t.due_at) ? ` \u23F0 ${String(t.deadline || t.due_at).slice(0, 10)}` : "";
  head.innerHTML = `
    <span class="tm-env-badge tm-env-badge--${sev}">${sev}</span>
    <span class="tasks-modal-row-title"></span>
    <span class="tm-env-mod-tag">${(t.source_module || "").replace(/_/g, " ")}</span>
    <span class="tasks-modal-row-status">${status}${t.assignee ? " \u2192 " + t.assignee : ""}${due}</span>`;
  head.querySelector(".tasks-modal-row-title").textContent = title;
  row.appendChild(head);
  const actions = document.createElement("div");
  actions.className = "tasks-modal-row-actions";
  row.appendChild(actions);
  const mkBtn = (label, cls, fn) => {
    const b = document.createElement("button");
    b.type = "button";
    b.className = `tm-env-btn ${cls}`;
    b.textContent = label;
    b.addEventListener("click", fn);
    actions.appendChild(b);
    return b;
  };
  const isOpen = status === "open" || status === "in_progress";
  if (isOpen) {
    const resolveBtn = mkBtn("Resolve", "tm-env-btn--resolve", async () => {
      await apiFetch(`${API_BASE}/chat/tasks/${t.task_id}/resolve`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ resolved_by: "chat" })
      }).catch(() => null);
      void reload();
    });
    resolveBtn.dataset.tourId = "task-resolve";
    mkBtn("Dismiss", "tm-env-btn--dismiss", async () => {
      await apiFetch(`${API_BASE}/chat/tasks/${t.task_id}/dismiss`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ dismissed_by: "chat" })
      }).catch(() => null);
      void reload();
    });
    mkBtn("Assign to\u2026", "tm-env-btn--assign", () => {
      if (actions.querySelector(".tasks-modal-assign-input"))
        return;
      const inp = document.createElement("input");
      inp.type = "text";
      inp.className = "tasks-modal-input tasks-modal-assign-input";
      inp.placeholder = "Type a name\u2026";
      inp.value = t.assignee || "";
      let _assignRef = null;
      let _assignDd = null;
      const _closeAssignDd = () => {
        _assignDd?.remove();
        _assignDd = null;
      };
      inp.addEventListener("input", () => {
        _assignRef = null;
        const q = inp.value.trim();
        if (!q) {
          _closeAssignDd();
          return;
        }
        void apiFetch(`${API_BASE}/chat/coworkers?q=${encodeURIComponent(q)}&limit=6`).then(async (r) => {
          if (!r.ok)
            return;
          const d = await r.json();
          const list = d.coworkers || [];
          _closeAssignDd();
          if (!list.length)
            return;
          const dd = document.createElement("div");
          dd.className = "at-mention-dropdown";
          const rect = inp.getBoundingClientRect();
          dd.style.cssText = `position:fixed;top:${rect.bottom + 2}px;left:${rect.left}px;min-width:${rect.width}px;z-index:9999;`;
          list.forEach((c) => {
            const item = document.createElement("button");
            item.type = "button";
            item.className = "at-mention-item";
            item.textContent = c.display_name;
            item.addEventListener("mousedown", (e) => {
              e.preventDefault();
              inp.value = c.display_name;
              _assignRef = c.assignee_ref;
              _closeAssignDd();
            });
            dd.appendChild(item);
          });
          document.body.appendChild(dd);
          _assignDd = dd;
        });
      });
      inp.addEventListener("blur", () => setTimeout(_closeAssignDd, 150));
      const save = async () => {
        const who = inp.value.trim();
        if (!who)
          return;
        _closeAssignDd();
        await apiFetch(`${API_BASE}/chat/tasks/${t.task_id}`, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ assigned_to: _assignRef || who, assignee: who })
        }).catch(() => null);
        void reload();
      };
      inp.addEventListener("keydown", (e) => {
        if (e.key === "Escape") {
          _closeAssignDd();
          inp.remove();
          okBtn.remove();
          return;
        }
        if (e.key === "Enter")
          void save();
      });
      const okBtn = document.createElement("button");
      okBtn.type = "button";
      okBtn.className = "tm-env-btn";
      okBtn.title = "Save assignment";
      okBtn.textContent = "\u2713";
      okBtn.addEventListener("click", () => void save());
      actions.appendChild(inp);
      actions.appendChild(okBtn);
      inp.focus();
    });
    mkBtn("Edit", "", () => {
      if (row.querySelector(".tasks-modal-editor"))
        return;
      const ed = document.createElement("div");
      ed.className = "tasks-modal-editor";
      ed.innerHTML = `
        <div class="tasks-modal-editor-fields">
          <input type="text" class="tasks-modal-input" data-e="title" placeholder="Title">
          <div class="tasks-modal-create-row">
            <select class="tasks-modal-input" data-e="severity">
              ${_TASK_SEVERITIES.map((s) => `<option value="${s}" ${s === sev ? "selected" : ""}>${s}</option>`).join("")}
            </select>
            <input type="date" class="tasks-modal-input" data-e="deadline">
            <input type="text" class="tasks-modal-input" data-e="note" placeholder="Add note (optional)">
          </div>
        </div>
        <div class="tasks-modal-editor-actions">
          <button type="button" class="tm-env-btn tm-env-btn--create-action" data-e="save">Save</button>
          <button type="button" class="tm-env-btn" data-e="cancel">Cancel</button>
        </div>`;
      ed.querySelector('[data-e="title"]').value = title;
      const closeEditor = () => {
        ed.remove();
        row.classList.remove("tasks-modal-row--editing");
      };
      ed.querySelector('[data-e="cancel"]').addEventListener("click", closeEditor);
      ed.querySelector('[data-e="save"]').addEventListener("click", async () => {
        const val = (k) => ed.querySelector(`[data-e="${k}"]`).value.trim();
        const body = {};
        if (val("title") && val("title") !== title) {
          body.title = val("title");
          body.text = val("title");
        }
        if (val("severity") !== sev)
          body.severity = val("severity");
        if (val("deadline"))
          body.deadline = val("deadline");
        if (val("note"))
          body.note = val("note");
        if (Object.keys(body).length) {
          await apiFetch(`${API_BASE}/chat/tasks/${t.task_id}`, {
            method: "PATCH",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body)
          }).catch(() => null);
        }
        closeEditor();
        void reload();
      });
      row.appendChild(ed);
      row.classList.add("tasks-modal-row--editing");
      ed.querySelector('[data-e="title"]').focus();
    });
  }
  return row;
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
function escapeHtml4(s) {
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
    const whyTitle = escapeHtml4(whyLine.length > 200 ? whyLine.slice(0, 2e3) : whyLine);
    const qRaw = r.quality_score;
    const qNum = qRaw != null && Number.isFinite(Number(qRaw)) ? Number(qRaw) : null;
    const qDisp = qNum !== null ? qNum.toFixed(2) : "\u2014";
    const qSrc = (r.quality_source || "").trim();
    const qTitle = escapeHtml4(qSrc ? qSrc.slice(0, 500) : "");
    const pgN = r.router_composite_at_pick != null && Number.isFinite(Number(r.router_composite_at_pick)) ? Number(r.router_composite_at_pick) : null;
    const pcN = r.per_call_composite != null && Number.isFinite(Number(r.per_call_composite)) ? Number(r.per_call_composite) : null;
    const pgBrk = r.router_composite_breakdown;
    const pcBrk = r.per_call_composite_breakdown;
    const compTitle = escapeHtml4(
      formatCompositeTooltip(pgN, pgBrk, pcN, pcBrk).slice(0, 3500)
    );
    const compShort = (pgN !== null ? pgN.toFixed(2) : "\u2014") + " / " + (pcN !== null ? pcN.toFixed(2) : "\u2014");
    tr.innerHTML = `<td>${escapeHtml4(stageName)}</td><td class="llm-performance-mono">${escapeHtml4(
      (r.model || "\u2014").trim()
    )}</td><td class="llm-performance-why" title="${whyTitle}">${escapeHtml4(whyShort)}</td><td class="llm-performance-lat-cell"><span class="llm-performance-lat-bar-wrap"><span class="llm-performance-lat-bar" style="width:${pct}%"></span></span><span class="llm-performance-lat-num">${latSec}${latSec !== "\u2014" ? "s" : ""}</span></td><td class="llm-performance-mono">$${rowCost}</td><td class="llm-performance-composite-cell" title="${compTitle}">${escapeHtml4(
      compShort
    )}</td><td class="llm-performance-qa-cell" title="${qTitle}">${escapeHtml4(
      qDisp
    )}</td><td class="llm-performance-status-cell"><span class="${stClass}">${escapeHtml4(
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
  reasonBox.innerHTML = `<strong>Rationale</strong><pre class="adjudicator-scorecard-pre">${escapeHtml4(
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
function renderRetrievalTrace(thinkingLog) {
  if (!Array.isArray(thinkingLog) || thinkingLog.length === 0)
    return null;
  const traces = [];
  for (const entry of thinkingLog) {
    if (entry && typeof entry === "object" && entry.signal === "retrieval_trace") {
      const e = entry;
      traces.push({
        data: e.data ?? {},
        step_id: e.step_id,
        note: e.note
      });
    }
  }
  if (traces.length === 0)
    return null;
  const wrap = document.createElement("div");
  wrap.className = "llm-performance retrieval-trace collapsed";
  const last = traces[traces.length - 1];
  const tel = last.data ?? {};
  const armHits = tel.arm_hits ?? tel.arms ?? {};
  const bm25 = Number(armHits.bm25 ?? armHits.bm25_hits ?? 0);
  const vec = Number(armHits.vector ?? armHits.vec_hits ?? 0);
  const totalMs = Number(
    tel.total_ms ?? (tel.timing && tel.timing.total_ms) ?? 0
  );
  const totalSec = totalMs > 0 ? (totalMs / 1e3).toFixed(2) : "0.00";
  const k = Number(tel.k ?? 0);
  const mode = String(tel.mode ?? "corpus");
  const preview = document.createElement("div");
  preview.className = "llm-performance-preview";
  preview.setAttribute("role", "button");
  preview.setAttribute("tabindex", "0");
  preview.setAttribute("aria-expanded", "false");
  const titleEl = document.createElement("span");
  titleEl.className = "llm-performance-title";
  titleEl.textContent = "Retrieval";
  const oneline = document.createElement("span");
  oneline.className = "llm-performance-oneline";
  oneline.textContent = `${mode} \xB7 BM25 ${bm25} \xB7 pgvector ${vec} \xB7 ${totalSec}s` + (traces.length > 1 ? ` \xB7 ${traces.length} rounds` : "") + (k ? ` \xB7 k=${k}` : "");
  const chev = document.createElement("span");
  chev.className = "llm-performance-chevron";
  chev.setAttribute("aria-hidden", "true");
  chev.textContent = "\u25BC";
  preview.appendChild(titleEl);
  preview.appendChild(oneline);
  preview.appendChild(chev);
  const body = document.createElement("div");
  body.className = "llm-performance-body";
  traces.forEach((t, idx) => {
    const data = t.data ?? {};
    const arms = data.arm_hits ?? data.arms ?? {};
    const ah_b = Number(arms.bm25 ?? arms.bm25_hits ?? 0);
    const ah_v = Number(arms.vector ?? arms.vec_hits ?? 0);
    const overlap = Number(arms.overlap ?? 0);
    const tim = data.timing ?? data;
    const embed_ms = Number(tim.embed_ms ?? 0);
    const bm25_ms = Number(tim.bm25_ms ?? 0);
    const vec_ms = Number(tim.vec_ms ?? 0);
    const rerank_ms = Number(tim.rerank_ms ?? 0);
    const total_ms = Number(data.total_ms ?? tim.total_ms ?? 0);
    const norm_q = data.bm25_normalized_query;
    const orig_q = data.query ?? "";
    const search_id = String(data.search_id ?? "").slice(0, 12);
    const round = document.createElement("div");
    round.className = "retrieval-trace-round";
    if (traces.length > 1) {
      const h = document.createElement("div");
      h.className = "retrieval-trace-round-header";
      h.textContent = `Round ${idx + 1}${t.step_id ? `  \xB7  ${t.step_id}` : ""}${search_id ? `  \xB7  search_id=${search_id}` : ""}`;
      round.appendChild(h);
    }
    const badges = document.createElement("div");
    badges.className = "llm-performance-badges";
    const specs = [
      { cls: "llm-performance-badge llm-performance-badge--model", text: `mode: ${data.mode || "corpus"}` },
      { cls: "llm-performance-badge llm-performance-badge--latency", text: `${(total_ms / 1e3).toFixed(2)}s` },
      { cls: "llm-performance-badge", text: `BM25 ${ah_b}` },
      { cls: "llm-performance-badge", text: `pgvector ${ah_v}` }
    ];
    if (overlap)
      specs.push({ cls: "llm-performance-badge", text: `overlap ${overlap}` });
    specs.forEach((s) => {
      const el2 = document.createElement("span");
      el2.className = s.cls;
      el2.textContent = s.text;
      badges.appendChild(el2);
    });
    round.appendChild(badges);
    if (embed_ms || bm25_ms || vec_ms || rerank_ms) {
      const tdiv = document.createElement("div");
      tdiv.className = "retrieval-trace-timing";
      const stages = [
        ["embed", embed_ms],
        ["BM25", bm25_ms],
        ["vector", vec_ms],
        ["rerank", rerank_ms]
      ];
      stages.filter(([, ms]) => ms > 0).forEach(([label, ms]) => {
        const cell = document.createElement("span");
        cell.className = "retrieval-trace-timing-cell";
        cell.textContent = `${label} ${ms.toFixed(0)}ms`;
        tdiv.appendChild(cell);
      });
      round.appendChild(tdiv);
    }
    if (orig_q) {
      const q = document.createElement("div");
      q.className = "retrieval-trace-query";
      q.textContent = `query: ${orig_q}`;
      round.appendChild(q);
    }
    if (norm_q && norm_q !== orig_q) {
      const nq = document.createElement("div");
      nq.className = "retrieval-trace-query retrieval-trace-query--norm";
      nq.textContent = `bm25 normalized: ${norm_q}`;
      round.appendChild(nq);
    }
    const bm25Exp = data.bm25_expansion;
    if (bm25Exp && typeof bm25Exp === "object") {
      const sec = rtMakeSection(
        "Query Rewrite",
        bm25Exp.matched_codes?.length > 0 ? `${bm25Exp.matched_codes.length} lex hit \xB7 +${bm25Exp.expansion_phrases_count ?? 0} phrases` : "no lexicon match (raw fallback)",
        /* collapsed= */
        true
      );
      const expDiv = document.createElement("div");
      expDiv.className = "rt-expansion";
      const rwBlock = document.createElement("div");
      rwBlock.className = "rt-rewrite-block";
      const orig = data.query || "";
      const norm = data.bm25_normalized_query;
      const tsq = bm25Exp.final_tsquery || "";
      [
        { label: "user typed", text: orig || "(empty)", cls: "" },
        ...norm && norm !== orig ? [{ label: "stripped to", text: norm, cls: "" }] : [],
        { label: "tsquery run", text: tsq || "(empty)", cls: "rt-rw-final" }
      ].forEach(({ label, text, cls }) => {
        const row = document.createElement("div");
        row.className = "rt-rewrite-row";
        const lbl = document.createElement("span");
        lbl.className = `rt-rewrite-label ${cls}`;
        lbl.textContent = label;
        const val = document.createElement("code");
        val.className = "rt-rewrite-val";
        val.title = text;
        val.textContent = text.length > 80 ? text.slice(0, 80) + "\u2026" : text;
        row.appendChild(lbl);
        row.appendChild(val);
        rwBlock.appendChild(row);
      });
      expDiv.appendChild(rwBlock);
      const tagKinds = [
        ["domain", bm25Exp.domain_tags ?? [], "rt-code-pill--d"],
        ["jurisdiction", bm25Exp.jurisdiction_tags ?? [], "rt-code-pill--j"],
        ["process", bm25Exp.process_tags ?? [], "rt-code-pill--p"]
      ];
      tagKinds.forEach(([kind, tags, pillCls]) => {
        if (!tags.length)
          return;
        const row = document.createElement("div");
        row.className = "rt-codes-row";
        const kindEl = document.createElement("span");
        kindEl.className = `rt-codes-kind rt-codes-kind--${kind[0]}`;
        kindEl.textContent = kind;
        row.appendChild(kindEl);
        tags.forEach((code) => {
          const p = document.createElement("span");
          p.className = `rt-code-pill ${pillCls}`;
          p.textContent = code;
          row.appendChild(p);
        });
        expDiv.appendChild(row);
      });
      const phrases = bm25Exp.expansion_phrases ?? [];
      if (phrases.length > 0) {
        const phDiv = document.createElement("div");
        phDiv.className = "rt-phrases";
        const phLabel = document.createElement("div");
        phLabel.className = "rt-phrases-label";
        phLabel.textContent = `+${phrases.length} expansion phrases`;
        phDiv.appendChild(phLabel);
        const phCloud = document.createElement("div");
        phCloud.className = "rt-phrases-cloud";
        phrases.forEach((ph) => {
          const chip = document.createElement("span");
          chip.className = "rt-phrase-chip";
          chip.textContent = ph;
          phCloud.appendChild(chip);
        });
        phDiv.appendChild(phCloud);
        expDiv.appendChild(phDiv);
      }
      if (!bm25Exp.matched_codes?.length) {
        const hint = document.createElement("div");
        hint.className = "rt-expansion-hint";
        hint.textContent = "\u26A0 No lexicon entry matched \u2014 falling back to OR-joined raw tokens. Candidate for lexicon addition.";
        expDiv.appendChild(hint);
      }
      sec.body.appendChild(expDiv);
      round.appendChild(sec.el);
    }
    const qp = data.query_profile;
    if (qp && typeof qp === "object") {
      const qtype = String(qp.query_type ?? "");
      const coverage = typeof qp.coverage === "number" ? `cov=${qp.coverage.toFixed(2)}` : "";
      const tags = Array.isArray(qp.tag_matches) ? qp.tag_matches : [];
      const anchors = Array.isArray(qp.literal_anchors) ? qp.literal_anchors : [];
      const badge = [qtype, coverage].filter(Boolean).join(" \xB7 ") || "classified";
      const sec = rtMakeSection(
        "Parser",
        badge,
        /* collapsed= */
        true
      );
      const pDiv = document.createElement("div");
      pDiv.className = "rt-parser";
      if (qtype) {
        const typeRow = document.createElement("div");
        typeRow.className = "rt-kv";
        typeRow.innerHTML = `<span class="rt-kv-k">type</span><span class="rt-kv-v">${rtEscapeAttr(qtype)}</span>`;
        pDiv.appendChild(typeRow);
      }
      if (typeof qp.coverage === "number") {
        const covRow = document.createElement("div");
        covRow.className = "rt-kv";
        covRow.innerHTML = `<span class="rt-kv-k">coverage</span><span class="rt-kv-v">${qp.coverage.toFixed(3)}</span>`;
        pDiv.appendChild(covRow);
      }
      if (anchors.length) {
        const aRow = document.createElement("div");
        aRow.className = "rt-kv";
        aRow.innerHTML = `<span class="rt-kv-k">anchors</span><span class="rt-kv-v">${rtEscapeAttr(anchors.join(" \xB7 "))}</span>`;
        pDiv.appendChild(aRow);
      }
      if (tags.length) {
        const tRow = document.createElement("div");
        tRow.className = "rt-codes-row";
        tags.forEach((t2) => {
          const prefix = t2.split(":")[0] ?? "";
          const pill = document.createElement("span");
          pill.className = `rt-code-pill rt-code-pill--${prefix === "d" ? "d" : prefix === "j" ? "j" : "p"}`;
          pill.textContent = t2;
          tRow.appendChild(pill);
        });
        pDiv.appendChild(tRow);
      }
      const untagged = Array.isArray(qp.untagged_meaningful_tokens) ? qp.untagged_meaningful_tokens : [];
      if (untagged.length) {
        const uRow = document.createElement("div");
        uRow.className = "rt-kv";
        uRow.innerHTML = `<span class="rt-kv-k">untagged tokens</span><span class="rt-kv-v">${rtEscapeAttr(untagged.join(" "))}</span>`;
        pDiv.appendChild(uRow);
      }
      sec.body.appendChild(pDiv);
      round.appendChild(sec.el);
    }
    const routing = data.routing;
    if (routing && typeof routing === "object") {
      const strat = String(routing.strategy ?? routing.executed_strategy ?? "?");
      const method = String(routing.method ?? "");
      const qclass = String(routing.query_class ?? "");
      const badge = `\u2192 ${strat}${qclass ? ` (${qclass})` : ""}${method ? ` via ${method}` : ""}`;
      const sec = rtMakeSection(
        "Router",
        badge,
        /* collapsed= */
        true
      );
      const rDiv = document.createElement("div");
      rDiv.className = "rt-router";
      const stratRow = document.createElement("div");
      stratRow.className = "rt-kv";
      stratRow.innerHTML = `<span class="rt-kv-k">strategy</span><span class="rt-kv-v">${rtEscapeAttr(strat)}` + (routing.fallback ? ` \u2192 fallback: ${rtEscapeAttr(String(routing.fallback))}` : "") + `</span>`;
      rDiv.appendChild(stratRow);
      const scores = routing.scores ?? {};
      if (typeof scores === "object" && Object.keys(scores).length > 0) {
        const scRow = document.createElement("div");
        scRow.className = "rt-kv";
        const scoreStr = Object.entries(scores).map(([k2, v]) => `${k2}=${typeof v === "number" ? v.toFixed(2) : v}`).join("  ");
        scRow.innerHTML = `<span class="rt-kv-k">scores</span><span class="rt-kv-v rt-mono">${rtEscapeAttr(scoreStr)}</span>`;
        rDiv.appendChild(scRow);
      }
      const sa = routing.self_assessments ?? {};
      if (typeof sa === "object" && Object.keys(sa).length > 0) {
        const saRow = document.createElement("div");
        saRow.className = "rt-kv";
        const saStr = Object.entries(sa).map(([k2, v]) => {
          const arr = Array.isArray(v) ? v : [v, ""];
          return `${k2}=${typeof arr[0] === "number" ? arr[0].toFixed(2) : arr[0]}`;
        }).join("  ");
        saRow.innerHTML = `<span class="rt-kv-k">self-assess</span><span class="rt-kv-v rt-mono">${rtEscapeAttr(saStr)}</span>`;
        rDiv.appendChild(saRow);
      }
      const withdrawn = Array.isArray(routing.withdrawn) ? routing.withdrawn : [];
      if (withdrawn.length) {
        const wRow = document.createElement("div");
        wRow.className = "rt-kv";
        wRow.innerHTML = `<span class="rt-kv-k">withdrawn</span><span class="rt-kv-v">${rtEscapeAttr(withdrawn.join(", "))}</span>`;
        rDiv.appendChild(wRow);
      }
      const pool = data.candidate_pool;
      if (pool && typeof pool === "object") {
        const poolRow = document.createElement("div");
        poolRow.className = "rt-kv";
        poolRow.innerHTML = `<span class="rt-kv-k">pool</span><span class="rt-kv-v">${rtEscapeAttr(String(pool.cascade_level ?? "?"))} \xB7 ${pool.size ?? "?"} docs</span>`;
        rDiv.appendChild(poolRow);
      }
      const sb = routing.score_breakdown;
      if (sb && typeof sb === "object" && Object.keys(sb).length > 0) {
        const picked = String(routing.strategy ?? routing.executed_strategy ?? "");
        const fmt = (x) => typeof x === "number" ? x.toFixed(2) : x ?? "\xB7";
        const contrib = (c) => c && typeof c === "object" ? fmt(c.contrib) : "\xB7";
        const tbl = document.createElement("table");
        tbl.className = "rt-score-table rt-mono";
        tbl.innerHTML = "<thead><tr><th>strat</th><th>accuracy</th><th>recall</th><th>speed</th><th>shape</th><th>total</th></tr></thead>";
        const tb = document.createElement("tbody");
        for (const [strat2, raw] of Object.entries(sb)) {
          const b = raw || {};
          const tr = document.createElement("tr");
          if (strat2 === picked)
            tr.className = "rt-score-winner";
          tr.innerHTML = `<td>${strat2 === picked ? "\u2605 " : ""}${rtEscapeAttr(strat2)}${b.withdrawn ? " \u2298" : ""}</td><td>${contrib(b.accuracy)}</td><td>${contrib(b.recall)}</td><td>${contrib(b.speed)}</td><td>${contrib(b.shape)}</td><td><b>${fmt(b.total)}</b></td>`;
          tb.appendChild(tr);
        }
        tbl.appendChild(tb);
        rDiv.appendChild(tbl);
      }
      const saFull = routing.self_assessments;
      if (saFull && typeof saFull === "object" && Object.keys(saFull).length > 0) {
        const wdl = Array.isArray(routing.withdrawn) ? routing.withdrawn : [];
        const tbl = document.createElement("table");
        tbl.className = "rt-sa-table rt-mono";
        tbl.innerHTML = "<thead><tr><th>strat</th><th>est</th><th>static</th><th>\u0394</th><th>reason</th></tr></thead>";
        const tb = document.createElement("tbody");
        for (const [strat2, raw] of Object.entries(saFull)) {
          const v = raw || {};
          const est = typeof v.est_recall === "number" ? v.est_recall : Array.isArray(v) && typeof v[0] === "number" ? v[0] : null;
          const stat = typeof v.static_recall === "number" ? v.static_recall : typeof v.static === "number" ? v.static : null;
          const delta = typeof est === "number" && typeof stat === "number" ? est - stat : null;
          const reason = String(v.reason ?? "");
          const tr = document.createElement("tr");
          if (wdl.includes(strat2))
            tr.className = "rt-sa-withdrawn";
          tr.innerHTML = `<td>${rtEscapeAttr(strat2)}</td><td>${typeof est === "number" ? est.toFixed(2) : "\xB7"}</td><td>${typeof stat === "number" ? stat.toFixed(2) : "\xB7"}</td><td>${typeof delta === "number" ? (delta >= 0 ? "+" : "") + delta.toFixed(2) : "\xB7"}</td><td class="rt-sa-reason" title="${rtEscapeAttr(reason)}">${rtEscapeAttr(reason.length > 64 ? reason.slice(0, 64) + "\u2026" : reason)}</td>`;
          tb.appendChild(tr);
        }
        tbl.appendChild(tb);
        rDiv.appendChild(tbl);
      }
      sec.body.appendChild(rDiv);
      round.appendChild(sec.el);
    }
    const stExec = Array.isArray(data.strategies_tried) ? data.strategies_tried : [];
    if (stExec.length > 0) {
      const sec = rtMakeSection(
        "Strategy",
        `${stExec.length} tried`,
        /* collapsed= */
        true
      );
      const eDiv = document.createElement("div");
      eDiv.className = "rt-strat-exec";
      stExec.forEach((s) => {
        const arms2 = s.arms || {};
        const rb = arms2.result_breakdown || {};
        const tim2 = arms2.timing_ms || {};
        const ok = s.succeeded ? "\u2713" : "\xB7";
        const armStr = `bm25_pool=${arms2.bm25_pool_hits ?? 0} vec_pool=${arms2.vector_pool_hits ?? 0}` + (rb && Object.keys(rb).length ? ` \xB7 split b=${rb.bm25_only ?? 0}/v=${rb.vector_only ?? 0}/both=${rb.both ?? 0}` : "");
        const timStr = Object.entries(tim2).filter(([, m]) => typeof m === "number" && m > 0).map(([key, m]) => `${key} ${Math.round(m)}ms`).join(" ");
        const row = document.createElement("div");
        row.className = "rt-strat-row";
        row.innerHTML = `<div class="rt-strat-head">${ok} <b>${rtEscapeAttr(String(s.strategy ?? "?"))}</b> \xB7 ${s.n_chunks ?? 0} chunks \xB7 top ${typeof s.top_rerank === "number" ? s.top_rerank.toFixed(2) : "\xB7"} \xB7 ${Math.round(s.elapsed_ms ?? 0)}ms</div><div class="rt-strat-arms rt-mono">${rtEscapeAttr(armStr)}</div>` + (timStr ? `<div class="rt-strat-tim rt-mono">${rtEscapeAttr(timStr)}</div>` : "") + (s.note ? `<div class="rt-strat-note">${rtEscapeAttr(String(s.note))}</div>` : "");
        eDiv.appendChild(row);
      });
      sec.body.appendChild(eDiv);
      round.appendChild(sec.el);
    }
    const rrc = Array.isArray(data.reranked_chunks) ? data.reranked_chunks : [];
    if (rrc.length > 0) {
      const topR = rrc.reduce(
        (m, c) => Math.max(m, typeof c.rerank_score === "number" ? c.rerank_score : 0),
        0
      );
      const armSplit = {};
      rrc.forEach((c) => {
        const a = (Array.isArray(c.retrieval_arms) ? c.retrieval_arms : []).join("+") || "\u2014";
        armSplit[a] = (armSplit[a] ?? 0) + 1;
      });
      const splitStr = Object.entries(armSplit).map(([k2, v]) => `${k2}=${v}`).join(" ");
      const sec = rtMakeSection(
        "Reranking",
        `${rrc.length} chunks \xB7 top ${topR.toFixed(2)}${splitStr ? ` \xB7 ${splitStr}` : ""}`,
        /* collapsed= */
        true
      );
      const tbl = document.createElement("table");
      tbl.className = "rt-rerank-table rt-mono";
      tbl.innerHTML = "<thead><tr><th>#</th><th>arms</th><th>rerank</th><th>sim</th><th>auth</th><th>doc</th><th>p</th></tr></thead>";
      const tb = document.createElement("tbody");
      rrc.forEach((c) => {
        const arms2 = Array.isArray(c.retrieval_arms) ? c.retrieval_arms.join("+") : "";
        const rr = typeof c.rerank_score === "number" ? c.rerank_score.toFixed(3) : "\xB7";
        const sim = typeof c.similarity === "number" ? c.similarity.toFixed(3) : "\xB7";
        const auth = String(c.authority_level ?? "").replace(/_/g, " ");
        const doc = String(c.document_name ?? "");
        const docShort = doc.length > 24 ? doc.slice(0, 24) + "\u2026" : doc;
        const tr = document.createElement("tr");
        tr.innerHTML = `<td>${c.rank ?? ""}</td><td>${rtEscapeAttr(arms2)}</td><td>${rr}</td><td>${sim}</td><td class="rt-rr-auth">${rtEscapeAttr(auth)}</td><td class="rt-rr-doc" title="${rtEscapeAttr(doc)}">${rtEscapeAttr(docShort)}</td><td>${c.page_number ?? ""}</td>`;
        tb.appendChild(tr);
      });
      tbl.appendChild(tb);
      sec.body.appendChild(tbl);
      round.appendChild(sec.el);
    }
    const themes = Array.isArray(data.themes) ? data.themes : [];
    const themeDiag = data.theme_diagnostic;
    if (themes.length > 0) {
      const domShare = typeof themeDiag?.dominant_theme_share === "number" ? ` \xB7 dom ${(themeDiag.dominant_theme_share * 100).toFixed(0)}%` : "";
      const sec = rtMakeSection(
        "Themes",
        `${themes.length} theme${themes.length !== 1 ? "s" : ""}${domShare}`,
        /* collapsed= */
        true
      );
      const tDiv = document.createElement("div");
      tDiv.className = "rt-themes";
      themes.forEach((th) => {
        const row = document.createElement("div");
        row.className = "rt-kv";
        const n = th.n_chunks_seen ?? th.top_chunks?.length ?? 0;
        const rerank = typeof th.top_rerank === "number" ? ` \xB7 rerank=${th.top_rerank.toFixed(2)}` : "";
        row.innerHTML = `<span class="rt-kv-k">${rtEscapeAttr(th.label ?? th.full_code ?? "?")}</span><span class="rt-kv-v">${n} chunks${rerank}</span>`;
        tDiv.appendChild(row);
      });
      sec.body.appendChild(tDiv);
      round.appendChild(sec.el);
    }
    const topChunks = data.top_chunks ?? data.scoring_trace ?? [];
    if (Array.isArray(topChunks) && topChunks.length > 0) {
      const weights = data.rerank_weights || data.weights || {};
      const wLabel = (k2) => {
        const v = Number(weights[k2]);
        return Number.isFinite(v) && v > 0 ? ` \xD7${v.toFixed(2).replace(/^0/, "")}` : "";
      };
      const table = document.createElement("table");
      table.className = "retrieval-trace-chunks retrieval-trace-chunks--rich";
      const head = document.createElement("thead");
      head.innerHTML = `<tr><th>#</th><th>doc</th><th class="rt-col-p">p</th><th>arms</th><th>conf</th><th class="rt-col-num">rerank</th><th class="rt-col-bar">sim${wLabel("sim")}</th><th class="rt-col-bar">auth${wLabel("auth")}</th><th class="rt-col-bar">jpd${wLabel("jpd")}</th></tr>`;
      table.appendChild(head);
      const tb = document.createElement("tbody");
      topChunks.slice(0, 10).forEach((c, i) => {
        const sig = c.signals ?? c.rerank_signals ?? {};
        const arms2 = Array.isArray(c.retrieval_arms) ? c.retrieval_arms : [];
        const armBadges = (() => {
          if (!arms2.length)
            return "\u2014";
          const both = arms2.length >= 2;
          if (both) {
            return '<span class="rt-arm rt-arm--both">BOTH</span>';
          }
          const a = arms2[0];
          if (a === "bm25")
            return '<span class="rt-arm rt-arm--bm25">BM25</span>';
          if (a === "vector")
            return '<span class="rt-arm rt-arm--vec">VEC</span>';
          return `<span class="rt-arm">${rtEscapeAttr(a.toUpperCase())}</span>`;
        })();
        const sim = Number(sig.sim_weighted ?? sig.sim_raw ?? 0);
        const auth = Number(sig.auth_weighted ?? sig.authority_weighted ?? 0);
        const jpd = Number(sig.jpd_weighted ?? 0);
        const tr = document.createElement("tr");
        tr.innerHTML = `<td>${i + 1}</td><td title="${rtEscapeAttr(c.document_name || "")}" class="rt-col-doc">${rtEscapeAttr((c.document_name || "").slice(0, 32))}</td><td class="rt-col-p">${c.page ?? c.page_number ?? "\u2014"}</td><td>${armBadges}</td><td>${rtConfBadge(c.confidence_label)}</td><td class="rt-col-num">${rtFormatSig(c.rerank_score)}</td><td class="rt-col-bar">${rtBar(sim, "sim")}</td><td class="rt-col-bar">${rtBar(auth, "auth")}</td><td class="rt-col-bar">${rtBar(jpd, "jpd")}</td>`;
        tb.appendChild(tr);
      });
      table.appendChild(tb);
      round.appendChild(table);
    }
    const assembly = data.assembly;
    if (assembly && typeof assembly === "object") {
      const canonPct = Math.round(Math.min(100, Math.max(0, (assembly.canonical_ratio ?? 0) * 100)));
      const strictPct = Math.round(Math.min(100, Math.max(0, (assembly.strict_canonical_ratio ?? 0) * 100)));
      const sec = rtMakeSection(
        "Assembly",
        `${assembly.strategy ?? "score"} \xB7 ${canonPct}% canonical`,
        /* collapsed= */
        true
      );
      const asmDiv = document.createElement("div");
      asmDiv.className = "rt-assembly";
      const metaRow = document.createElement("div");
      metaRow.className = "rt-assembly-meta";
      [
        ["strategy", assembly.strategy ?? "score"],
        ...assembly.canonical_floor != null ? [["floor", `${Math.round(assembly.canonical_floor * 100)}%`]] : [],
        ["selected", String(assembly.total_selected ?? "?")]
      ].forEach(([k2, v]) => {
        const kv = document.createElement("span");
        kv.className = "rt-kv";
        kv.innerHTML = `<span class="rt-k">${k2}</span><code class="rt-v">${v}</code>`;
        metaRow.appendChild(kv);
      });
      asmDiv.appendChild(metaRow);
      [
        { label: "Canonical (CoT + PP)", pct: canonPct, color: "#2563eb" },
        { label: "Strict (CoT only)", pct: strictPct, color: "#16a34a" }
      ].forEach(({ label, pct, color }) => {
        const row = document.createElement("div");
        row.className = "rt-ratio-row";
        const lbl = document.createElement("span");
        lbl.className = "rt-ratio-label";
        lbl.textContent = label;
        const track = document.createElement("div");
        track.className = "rt-ratio-track";
        const fill = document.createElement("div");
        fill.className = "rt-ratio-fill";
        fill.style.cssText = `width:${pct}%;background:${color}`;
        track.appendChild(fill);
        const pctEl = document.createElement("span");
        pctEl.className = "rt-ratio-pct";
        pctEl.textContent = `${pct}%`;
        row.appendChild(lbl);
        row.appendChild(track);
        row.appendChild(pctEl);
        asmDiv.appendChild(row);
      });
      const tierOrder = ["contract_source_of_truth", "payer_policy", "operational_suggested", "fyi_not_citable"];
      const tierLabel = { contract_source_of_truth: "CoT", payer_policy: "PP", operational_suggested: "Ops", fyi_not_citable: "FYI" };
      const tierColor = { contract_source_of_truth: "#16a34a", payer_policy: "#2563eb", operational_suggested: "#0891b2", fyi_not_citable: "#d97706" };
      const breakdown = assembly.tier_breakdown ?? {};
      const tierRow = document.createElement("div");
      tierRow.className = "rt-tier-row";
      tierOrder.forEach((tier) => {
        const n = breakdown[tier] ?? 0;
        if (!n)
          return;
        const pill = document.createElement("span");
        pill.className = "rt-tier-pill";
        pill.style.cssText = `border-color:${tierColor[tier]};color:${tierColor[tier]}`;
        pill.title = tier;
        pill.textContent = `${tierLabel[tier]} \xD7${n}`;
        tierRow.appendChild(pill);
      });
      const untagged = (breakdown["untagged"] ?? 0) + (breakdown["null"] ?? 0) + (breakdown["None"] ?? 0);
      if (untagged) {
        const pill = document.createElement("span");
        pill.className = "rt-tier-pill";
        pill.style.cssText = "border-color:#9ca3af;color:#9ca3af";
        pill.textContent = `untagged \xD7${untagged}`;
        tierRow.appendChild(pill);
      }
      if (tierRow.children.length)
        asmDiv.appendChild(tierRow);
      sec.body.appendChild(asmDiv);
      round.appendChild(sec.el);
    }
    body.appendChild(round);
  });
  wrap.appendChild(preview);
  wrap.appendChild(body);
  const toggle = () => {
    const expanded = wrap.classList.toggle("collapsed");
    preview.setAttribute("aria-expanded", expanded ? "false" : "true");
    chev.textContent = expanded ? "\u25BC" : "\u25B2";
  };
  preview.addEventListener("click", toggle);
  preview.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      toggle();
    }
  });
  return wrap;
}
function rtMakeSection(title, badge, collapsed = false) {
  const el2 = document.createElement("div");
  el2.className = "rt-section" + (collapsed ? " rt-section--collapsed" : "");
  const hdr = document.createElement("button");
  hdr.type = "button";
  hdr.className = "rt-section-hdr";
  hdr.setAttribute("aria-expanded", String(!collapsed));
  const chev = document.createElement("span");
  chev.className = "rt-section-chev";
  chev.setAttribute("aria-hidden", "true");
  chev.textContent = collapsed ? "\u25B6" : "\u25BC";
  const titleEl = document.createElement("span");
  titleEl.className = "rt-section-title";
  titleEl.textContent = title;
  const badgeEl = document.createElement("span");
  badgeEl.className = "rt-section-badge";
  badgeEl.textContent = badge;
  hdr.appendChild(chev);
  hdr.appendChild(titleEl);
  hdr.appendChild(badgeEl);
  const body = document.createElement("div");
  body.className = "rt-section-body";
  if (collapsed)
    body.style.display = "none";
  hdr.addEventListener("click", () => {
    const isCollapsed = el2.classList.toggle("rt-section--collapsed");
    body.style.display = isCollapsed ? "none" : "";
    chev.textContent = isCollapsed ? "\u25B6" : "\u25BC";
    hdr.setAttribute("aria-expanded", String(!isCollapsed));
  });
  el2.appendChild(hdr);
  el2.appendChild(body);
  return { el: el2, body };
}
function rtEscapeAttr(s) {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}
function rtFormatSig(v) {
  if (typeof v !== "number")
    return "\u2014";
  return v.toFixed(3);
}
function rtBar(value, kind) {
  if (!Number.isFinite(value) || value <= 0) {
    return '<span class="rt-bar rt-bar--empty">\u2014</span>';
  }
  const pct = Math.max(0, Math.min(100, value * 100));
  return `<span class="rt-bar rt-bar--${kind}"><span class="rt-bar-track"><span class="rt-bar-fill" style="width:${pct.toFixed(1)}%"></span></span><span class="rt-bar-val">${value.toFixed(3)}</span></span>`;
}
function rtConfBadge(label) {
  if (typeof label !== "string" || !label)
    return "\u2014";
  const lc = label.toLowerCase();
  let cls = "rt-conf";
  if (lc === "high")
    cls += " rt-conf--high";
  else if (lc === "medium" || lc === "med")
    cls += " rt-conf--med";
  else if (lc === "low")
    cls += " rt-conf--low";
  return `<span class="${cls}">${rtEscapeAttr(label)}</span>`;
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
  metaCol.innerHTML = `${escapeHtml4(jurisLine)}<br/>Config: ${escapeHtml4(cfgShort)} \xB7 ${escapeHtml4(corpusBit)}`;
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
async function downloadDocumentFile(d, btn) {
  const idleLabel = btn.textContent || "Download";
  btn.disabled = true;
  btn.textContent = "Downloading\u2026";
  const sameOriginAuthHeaders = (url) => {
    if (!url.startsWith("/"))
      return {};
    try {
      const tok = localStorage.getItem("mobius.auth.accessToken");
      return tok ? { Authorization: "Bearer " + tok } : {};
    } catch {
      return {};
    }
  };
  const tryFetch = async (url) => {
    try {
      const r = await fetch(url, { headers: sameOriginAuthHeaders(url) });
      if (!r.ok)
        return { blob: null, blocked: false };
      return { blob: await r.blob(), blocked: false };
    } catch {
      return { blob: null, blocked: true };
    }
  };
  let name = (d.filename || d.title || "document").trim() || "document";
  const first = await tryFetch(d.download_url);
  let blob = first.blob;
  if (!blob && !first.blocked && d.fallback_download_url) {
    blob = (await tryFetch(d.fallback_download_url)).blob;
    if (blob && !/\.pdf$/i.test(name))
      name = name.replace(/\.[A-Za-z0-9]+$/, "") + ".pdf";
  }
  if (blob) {
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = name;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 3e4);
    btn.textContent = "Downloaded \u2713";
    setTimeout(() => {
      btn.textContent = idleLabel;
      btn.disabled = false;
    }, 4e3);
  } else {
    const openUrl = first.blocked ? d.download_url : d.fallback_download_url || d.download_url;
    window.open(openUrl, "_blank", "noopener");
    btn.textContent = idleLabel;
    btn.disabled = false;
  }
}
function renderDocumentDownloadBlock(entries) {
  const wrap = document.createElement("div");
  wrap.className = "doc-download-block";
  for (const d of entries || []) {
    if (!d || !d.download_url || !d.title)
      continue;
    const card = document.createElement("div");
    card.className = "doc-download-card";
    const icon = document.createElement("div");
    icon.className = "doc-download-icon";
    icon.innerHTML = '<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"></path><polyline points="14 2 14 8 20 8"></polyline><line x1="12" y1="12" x2="12" y2="18"></line><polyline points="9 15 12 18 15 15"></polyline></svg>';
    const info = document.createElement("div");
    info.className = "doc-download-info";
    const title = document.createElement("div");
    title.className = "doc-download-title";
    title.textContent = d.title;
    info.appendChild(title);
    const metaParts = [d.filename, d.host, d.payer, d.state, d.program, d.authority_level].filter(
      (x) => typeof x === "string" && x.trim() !== "" && x !== d.title
    );
    if (metaParts.length) {
      const meta = document.createElement("div");
      meta.className = "doc-download-meta";
      meta.textContent = metaParts.join(" \xB7 ");
      info.appendChild(meta);
    }
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "doc-download-btn";
    btn.textContent = "Download";
    btn.addEventListener("click", () => {
      void downloadDocumentFile(d, btn);
    });
    card.appendChild(icon);
    card.appendChild(info);
    card.appendChild(btn);
    wrap.appendChild(card);
  }
  return wrap;
}
function renderAssistantFromEnvelope(envelope, opts) {
  const outer = document.createElement("div");
  outer.className = "assistant-envelope";
  const bubble = document.createElement("div");
  bubble.className = "message-bubble answer-card-bubble";
  let confidenceInjectedAfterDirectAnswer = false;
  const pendingActionChips = [];
  for (const block of envelope.blocks || []) {
    if (!block || typeof block !== "object")
      continue;
    const t = block.type;
    if (t === "correction") {
      const b = block;
      const orig = (b.original || "").trim();
      const fixed = (b.corrected || "").trim();
      if (orig && fixed) {
        const line = document.createElement("div");
        line.className = "envelope-correction-inline";
        const icon = document.createElement("span");
        icon.className = "envelope-correction-inline-icon";
        icon.textContent = "\u26A0";
        const origSpan = document.createElement("span");
        origSpan.className = "envelope-correction-inline-orig";
        origSpan.textContent = orig;
        const arrow = document.createElement("span");
        arrow.className = "envelope-correction-inline-arrow";
        arrow.textContent = " \u2192 ";
        const fixedSpan = document.createElement("span");
        fixedSpan.className = "envelope-correction-inline-fixed";
        fixedSpan.textContent = fixed;
        line.appendChild(icon);
        line.appendChild(document.createTextNode(" "));
        line.appendChild(origSpan);
        line.appendChild(arrow);
        line.appendChild(fixedSpan);
        bubble.appendChild(line);
      }
    } else if (t === "takeaways") {
      const b = block;
      if (Array.isArray(b.items) && b.items.length > 0) {
        const wrap = document.createElement("div");
        wrap.className = "envelope-takeaways";
        const hdr = document.createElement("div");
        hdr.className = "envelope-takeaways-header";
        hdr.textContent = "Key takeaways";
        wrap.appendChild(hdr);
        const ul = document.createElement("ul");
        ul.className = "envelope-takeaways-list";
        for (const item of b.items) {
          const li = document.createElement("li");
          li.textContent = item;
          ul.appendChild(li);
        }
        wrap.appendChild(ul);
        bubble.appendChild(wrap);
      }
    } else if (t === "tool_attribution") {
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
    } else if (t === "document_download") {
      const b = block;
      if (Array.isArray(b.documents) && b.documents.length) {
        bubble.appendChild(renderDocumentDownloadBlock(b.documents));
      }
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
          if (status === "open" || status === "in_progress") {
            const actions = document.createElement("div");
            actions.className = "tm-env-card-actions";
            const settle = (newStatus) => {
              card.classList.remove("tm-env-status-open", "tm-env-status-in_progress");
              card.classList.add(`tm-env-status-${newStatus}`);
              statusDot.className = `tm-env-status-dot tm-env-status-dot--${newStatus}`;
              actions.remove();
            };
            if (b.allow_resolve !== false) {
              const resolveBtn = document.createElement("button");
              resolveBtn.type = "button";
              resolveBtn.className = "tm-env-btn tm-env-btn--resolve";
              resolveBtn.textContent = "Resolve";
              resolveBtn.addEventListener("click", async (e) => {
                e.stopPropagation();
                resolveBtn.disabled = true;
                resolveBtn.textContent = "\u2026";
                try {
                  await apiFetch(`${API_BASE}/chat/tasks/${task.task_id}/resolve`, {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ resolved_by: "chat" })
                  });
                  settle("resolved");
                } catch {
                  resolveBtn.disabled = false;
                  resolveBtn.textContent = "Resolve";
                }
              });
              actions.appendChild(resolveBtn);
            }
            if (b.allow_dismiss !== false) {
              const dismissBtn = document.createElement("button");
              dismissBtn.type = "button";
              dismissBtn.className = "tm-env-btn tm-env-btn--dismiss";
              dismissBtn.textContent = "Dismiss";
              dismissBtn.addEventListener("click", async (e) => {
                e.stopPropagation();
                dismissBtn.disabled = true;
                dismissBtn.textContent = "\u2026";
                try {
                  await apiFetch(`${API_BASE}/chat/tasks/${task.task_id}/dismiss`, {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ dismissed_by: "chat" })
                  });
                  settle("dismissed");
                } catch {
                  dismissBtn.disabled = false;
                  dismissBtn.textContent = "Dismiss";
                }
              });
              actions.appendChild(dismissBtn);
            }
            if (b.allow_assign !== false) {
              const assignBtn = document.createElement("button");
              assignBtn.type = "button";
              assignBtn.className = "tm-env-btn tm-env-btn--assign";
              assignBtn.textContent = "Assign";
              assignBtn.addEventListener("click", (e) => {
                e.stopPropagation();
                if (actions.querySelector(".tm-env-assign-input"))
                  return;
                const inp = document.createElement("input");
                inp.type = "text";
                inp.className = "tm-env-assign-input";
                inp.placeholder = "assignee \u2014 Enter to save";
                inp.addEventListener("click", (ev) => ev.stopPropagation());
                inp.addEventListener("keydown", async (ev) => {
                  if (ev.key === "Escape") {
                    inp.remove();
                    return;
                  }
                  if (ev.key !== "Enter")
                    return;
                  const who = inp.value.trim();
                  if (!who)
                    return;
                  inp.disabled = true;
                  try {
                    await apiFetch(`${API_BASE}/chat/tasks/${task.task_id}`, {
                      method: "PATCH",
                      headers: { "Content-Type": "application/json" },
                      body: JSON.stringify({ assigned_to: who, assignee: who })
                    });
                    inp.remove();
                    assignBtn.textContent = `\u2192 ${who}`;
                    assignBtn.disabled = true;
                  } catch {
                    inp.disabled = false;
                  }
                });
                actions.appendChild(inp);
                inp.focus();
              });
              actions.appendChild(assignBtn);
            }
            if (b.allow_edit !== false) {
              const editBtn = document.createElement("button");
              editBtn.type = "button";
              editBtn.className = "tm-env-btn";
              editBtn.textContent = "Edit";
              editBtn.addEventListener("click", (e) => {
                e.stopPropagation();
                openTasksModal();
              });
              actions.appendChild(editBtn);
            }
            if (actions.childElementCount)
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
      c.innerHTML = simpleMarkdownToHtml(b.body || "");
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
        const disclosure = document.createElement("details");
        disclosure.className = "envelope-followups-disclosure";
        disclosure.open = false;
        const sum = document.createElement("summary");
        sum.className = "envelope-followups-summary envelope-followups-summary--next-steps";
        sum.textContent = "Next steps (tap to expand)";
        disclosure.appendChild(sum);
        const w = document.createElement("div");
        w.className = "envelope-next-steps";
        const hint = document.createElement("div");
        hint.className = "envelope-next-steps-hint";
        hint.textContent = "Suggested actions \u2014 not auto-sent.";
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
      if (items.length && opts.onFollowupClick) {
        const onSelect = opts.onFollowupClick;
        updateChatSuggestions(items, onSelect);
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
    } else if (t === "action_chips") {
      const b = block;
      if (Array.isArray(b.chips) && b.chips.length > 0) {
        const actionsWrap = document.createElement("div");
        actionsWrap.className = "answer-card-actions";
        for (const action of b.chips) {
          if (action.type === "external_link" && action.url && action.label) {
            const a = document.createElement("a");
            a.href = action.url;
            a.target = "_blank";
            a.rel = "noopener noreferrer";
            a.className = "answer-card-action-chip";
            a.textContent = (action.icon ? action.icon + " " : "") + action.label + " \u2197";
            actionsWrap.appendChild(a);
          }
        }
        if (actionsWrap.childNodes.length > 0)
          pendingActionChips.push(actionsWrap);
      }
    } else if (t === "credentialing_card") {
      const b = block;
      const card = document.createElement("div");
      card.className = "cred-card" + (b.org_summary ? " cred-card--org-summary" : "");
      const header = document.createElement("div");
      header.className = "cred-card-header";
      const nameEl = document.createElement("div");
      nameEl.className = "cred-card-name";
      const displayName = b.org_summary ? b.provider_name || (b.org ?? "").replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase()) || "Organization" : b.provider_name ?? "Provider";
      nameEl.textContent = displayName;
      const statusKey = (b.status ?? "unknown").toLowerCase();
      const statusLabel = {
        enrolled: "Enrolled",
        pending: "Pending",
        flagged: "Flagged",
        not_enrolled: "Not Enrolled",
        unknown: "Unknown"
      };
      const statusEl = document.createElement("span");
      statusEl.className = `cred-card-status cred-card-status--${statusKey}`;
      statusEl.textContent = statusLabel[statusKey] ?? b.status ?? "Unknown";
      header.appendChild(nameEl);
      header.appendChild(statusEl);
      card.appendChild(header);
      if (b.npi || b.org) {
        const meta = document.createElement("div");
        meta.className = "cred-card-meta";
        if (b.npi) {
          const npiEl = document.createElement("span");
          npiEl.className = "cred-card-npi";
          npiEl.textContent = "NPI " + b.npi;
          meta.appendChild(npiEl);
        }
        if (b.org) {
          const orgEl = document.createElement("span");
          orgEl.className = "cred-card-org";
          orgEl.textContent = b.org;
          meta.appendChild(orgEl);
        }
        card.appendChild(meta);
      }
      if (Array.isArray(b.flags) && b.flags.length > 0) {
        const flagList = document.createElement("ul");
        flagList.className = "cred-card-flags";
        b.flags.forEach((f) => {
          const li = document.createElement("li");
          li.className = `cred-card-flag cred-card-flag--${f.severity ?? "info"}`;
          const dot = document.createElement("span");
          dot.className = "cred-flag-dot";
          dot.setAttribute("aria-hidden", "true");
          li.appendChild(dot);
          li.appendChild(document.createTextNode(f.text));
          flagList.appendChild(li);
        });
        card.appendChild(flagList);
      }
      if (b.action_url) {
        const link = document.createElement("a");
        link.href = b.action_url;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        link.className = "cred-card-action";
        link.textContent = "View full report \u2197";
        card.appendChild(link);
      }
      bubble.appendChild(card);
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
  pendingActionChips.forEach((el2) => msg.appendChild(el2));
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
  _authRef = auth;
  void _maybeShowGreeting();
  const authGateEl = document.getElementById("authGate");
  const appLayoutEl = document.querySelector(".app-layout");
  function _setAuthGate(visible) {
    if (!authGateEl)
      return;
    authGateEl.classList.toggle("auth-gate--visible", visible);
    authGateEl.inert = !visible;
    if (appLayoutEl) {
      appLayoutEl.inert = visible;
    }
  }
  const _authStyleEl = document.createElement("style");
  _authStyleEl.textContent = AUTH_STYLES + (PREFERENCES_MODAL_STYLES || "");
  document.head.appendChild(_authStyleEl);
  let modal = createAuthModal({ auth, showOAuth: false });
  document.body.appendChild(modal.el);
  document.getElementById("authGateBtn")?.addEventListener("click", () => {
    modal.open("login");
  });
  const alphaBanner = document.getElementById("alphaBanner");
  const alphaModal = document.getElementById("alphaModal");
  const openAlphaModal = () => {
    if (alphaModal)
      alphaModal.hidden = false;
  };
  const closeAlphaModal = () => {
    if (alphaModal)
      alphaModal.hidden = true;
  };
  if (alphaBanner) {
    if (localStorage.getItem("alpha_banner_dismissed") === "1") {
      alphaBanner.hidden = true;
    } else {
      document.getElementById("alphaBannerDismiss")?.addEventListener("click", () => {
        alphaBanner.hidden = true;
        localStorage.setItem("alpha_banner_dismissed", "1");
      });
    }
  }
  document.getElementById("alphaBannerTag")?.addEventListener("click", openAlphaModal);
  document.getElementById("alphaBannerTagLink")?.addEventListener("click", openAlphaModal);
  document.getElementById("alphaModalClose")?.addEventListener("click", closeAlphaModal);
  alphaModal?.addEventListener("click", (e) => {
    if (e.target === alphaModal)
      closeAlphaModal();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && alphaModal && !alphaModal.hidden)
      closeAlphaModal();
  });
  const prefsModal = createPreferencesModal(authApiBase, auth, {
    onSave: () => {
      void _fetchNestedUserProfile();
    }
  });
  window.onOpenPreferences = () => {
    void prefsModal.open();
  };
  document.getElementById("onboardingNudge")?.addEventListener("click", (e) => {
    e.stopPropagation();
    void prefsModal.open();
  });
  fetch(`${authApiBase}/public-config`, { method: "GET" }).then((r) => r.ok ? r.json() : null).then((cfg) => {
    const gid = cfg && cfg.google_client_id ? String(cfg.google_client_id).trim() : "";
    if (!gid)
      return;
    const oldEl = modal.el;
    modal = createAuthModal({ auth, showOAuth: true, googleClientId: gid });
    if (oldEl.parentNode)
      oldEl.parentNode.replaceChild(modal.el, oldEl);
    else
      document.body.appendChild(modal.el);
  }).catch((e) => {
    console.warn("[auth] public-config fetch failed; Google sign-in disabled:", e);
  });
  function updateSidebarUser(user) {
    if (!sidebarUserName)
      return;
    const name = user?.greeting_name || user?.preferred_name || user?.first_name || user?.display_name || (user?.email ? user.email.split("@")[0] : null) || "Guest";
    sidebarUserName.textContent = name;
  }
  function _syncOnboardingNudge(isOnboarded) {
    const nudge = document.getElementById("onboardingNudge");
    if (nudge)
      nudge.hidden = isOnboarded;
  }
  let _tmShownThisSession = false;
  function _showTrainingMode(name, arrival, force = false) {
    const wrap = document.getElementById("trainingMode");
    if (!wrap)
      return;
    if (!force) {
      if (_tmShownThisSession)
        return;
      if (sessionStorage.getItem("_tm_skip") === "1")
        return;
    }
    _tmShownThisSession = true;
    const esc = (s) => String(s).replace(
      /[&<>"']/g,
      (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]
    );
    const SCENARIOS = [
      {
        k: "transport",
        label: "\u{1F68C} A patient can't get to appointments",
        act: "patient_outreach",
        q: "Does Medicaid cover rides to behavioral-health appointments?",
        tones: [
          ["professional", "Yes. Florida Medicaid covers non-emergency medical transportation to behavioral-health appointments. Rides are arranged through the plan's contracted transportation broker, and most plans require one to three days' advance notice. I can retrieve this patient's plan and its booking procedure."],
          ["friendly", "Yes, and this one's easy \u2014 Medicaid rides are free for covered visits \u{1F68C} You just book through the plan's ride line a couple of days ahead. Want me to grab the booking number for this patient's plan?"],
          ["concise", "Covered (NEMT). Plan broker, 1\u20133d notice. Number?"]
        ],
        depths: [
          ["beginner", "Yes, covered \u2705 Every FL Medicaid plan includes free rides to covered appointments \u2014 it's called NEMT. To set one up, call the plan's ride line ideally 3 days ahead. Want me to pull this patient's plan, get the booking number, and walk you through it?"],
          ["regular", "Yes \u2014 NEMT is covered \u2705 Book through the plan's broker ~1\u20133 days ahead. Want the plan-specific booking line?"],
          ["expert", "Covered \u2705 NEMT via plan broker; 1\u20133d notice; standing-order option for recurring visits. Pull member plan \u2192 broker line?"]
        ]
      },
      {
        k: "pcp",
        label: "\u{1FA7A} A patient needs a PCP assigned or switched",
        act: "check_in_patients",
        q: "How does a member change their PCP?",
        tones: [
          ["professional", "Members may change their primary care provider through the plan's member portal or by calling member services. Changes take effect on the first of the following month; urgent assignments for unassigned members can be expedited."],
          ["friendly", "Happens all the time \u2014 quick fix! The member calls member services or uses the portal, picks the new PCP, and it kicks in on the 1st of next month. No PCP at all? The plan can rush it."],
          ["concise", "Portal or member services. Effective 1st next month. Expedite if unassigned. [src]"]
        ],
        depths: [
          ["beginner", "Here's the whole path \u2705 The member calls the plan's member-services line (or uses the portal) and requests the change; it usually takes effect the 1st of the following month. If urgent \u2014 like no PCP at all \u2014 plans can expedite. Want me to look up the plan's number?"],
          ["regular", "Plan portal or member services; effective 1st of next month; expedite path for unassigned \u2705 Want the plan's number?"],
          ["expert", "Portal/MS line; eff. 1st next mo.; expedite path for unassigned members. Registry has the MS number. Pull it?"]
        ]
      },
      {
        k: "denial",
        label: "\u274C A claim came back denied \u2014 now what?",
        act: "rework_denials",
        q: "Why was this claim denied and how do I fix it?",
        tones: [
          ["professional", "This claim was denied with CARC 197: prior authorization not on file. The denial is typically recoverable through a retroactive authorization request, where the payer permits it, or a formal appeal supported by medical-necessity documentation. I can prepare the appeal letter."],
          ["friendly", "Okay, decoded it \u2014 the payer says nobody got prior auth first (code 197). Don't worry, this one's usually saveable: retro-auth or appeal. I can draft the letter with you \u{1F4AA}"],
          ["concise", "CARC 197 \u2014 no PA. Retro-auth or appeal. Draft?"]
        ],
        depths: [
          ["beginner", "The code (CARC 197) means the payer didn't find a prior authorization \u2705 Two ways forward \u2014 ask for a retroactive auth (some payers allow it) or appeal with documentation. I can check this payer's exact rules and draft the appeal with you. Start there?"],
          ["regular", "CARC 197 \u2014 missing prior auth \u2705 This payer allows retro-auth requests; otherwise appeal. Draft the letter?"],
          ["expert", "197 \xB7 no PA on file. Retro window per payer playbook; else appeal w/ med-nec. appeals_assemble_letter ready."]
        ]
      },
      {
        k: "newprov",
        label: "\u{1FAAA} A new clinician needs to start billing",
        act: "credentialing",
        q: "Is our new clinician enrolled with Medicaid yet?",
        tones: [
          ["professional", "The clinician does not yet appear on the state's Provider Master List; enrollment remains pending. Claims submitted before the effective date will be denied. I will monitor the roster and notify you when the status changes."],
          ["friendly", "Checked \u2014 they're not on the state roster quite yet, so hold their claims for now (billing early = automatic denials). I'll keep an eye on it and ping you the day they flip to payable!"],
          ["concise", "Not on PML. Hold claims. Watching; will notify on flip."]
        ],
        depths: [
          ["beginner", "Not enrolled yet \u2705 Until the state lists them (the PML), any claim under their NPI will deny. Hold their claims \u2014 I'll watch the roster and tell you the day they're payable. Want me to show you their full credentialing card?"],
          ["regular", "Pending \u2014 not on PML yet \u2705 Hold claims; I'll notify on the flip. Want the credentialing card?"],
          ["expert", "PML: absent. NPPES: active. Hold claims; watcher set on status flip. check_provider_credentialing for full panel view."]
        ]
      },
      {
        k: "coverage",
        label: "\u{1F4C4} Not sure what a payer actually covers",
        act: "submit_claims",
        q: "Does this payer cover this service via telehealth?",
        tones: [
          ["professional", "Yes. This payer covers the service when delivered via telehealth, subject to the telehealth modifier requirement, and reimburses at parity with in-person delivery. Source: payer telehealth policy, page 12."],
          ["friendly", "Good news \u2014 covered over telehealth! One gotcha: the claim needs the telehealth modifier or it'll bounce. Want me to pin the policy page so your team has the receipt?"],
          ["concise", "Covered via telehealth. Modifier req'd. Parity. [src p.12]"]
        ],
        depths: [
          ["beginner", "Yes, it's covered via telehealth \u2705 One thing to get right: the claim needs a telehealth modifier or it may deny. Here's the policy page as your receipt. Want me to note which of your common services have telehealth quirks?"],
          ["regular", "Covered via telehealth, modifier required \u2705 Policy page attached. Want the full telehealth rules for this payer?"],
          ["expert", "Covered; parity; GT/95 modifier req. Source pinned. Cross-payer telehealth matrix available on ask."]
        ]
      },
      {
        k: "rates",
        label: "\u{1F4C8} Are we getting paid fairly?",
        act: "strategy",
        q: "What's the market rate for this service code?",
        tones: [
          ["professional", "Your realized rate for this code is at the 34th percentile of comparable providers. Closing the gap to the market median would represent a material per-unit increase. I can quantify the annualized difference and identify the peer group used for comparison."],
          ["friendly", "Honest answer? You're leaving money on the table here \u2014 34th percentile for this code. The median would mean real dollars per visit. Want the yearly number? It tends to get people's attention \u{1F609}"],
          ["concise", "P34 vs peer P50. Gap material. Annualized number?"]
        ],
        depths: [
          ["beginner", "You're being paid below market on this code \u2705 Comparable providers get more per unit \u2014 you're at the 34th percentile, from real claims data. Want me to show the annual dollar gap and which peer group I used?"],
          ["regular", "P34 vs peers on this code \u2705 Median would mean more per unit. Annualized gap + peer group on request."],
          ["expert", "P34 realized vs peer P50; claims-level basis; get_org_rate_gap for annualized + get_rate_trends for trajectory."]
        ]
      }
    ];
    const AUTONOMY = [
      { k: "automatic", b: "Just handle it", s: "I'll act on routine things and tell you after." },
      { k: "confirm_first", b: "Show me before you act", s: "I'll line it up, you press go." },
      { k: "manual", b: "Walk me through it", s: "We do it together, step by step." }
    ];
    const HESITATIONS = [
      { k: "wrong", b: "It'll get things wrong", emo: "\u{1F62C}", fearQ: "\u201CWhat if it\u2019s wrong \u2014 it\u2019s my name on this.\u201D", fearA: "No source, no claim. Every answer shows receipts \u2014 click any citation. Unsure? I say so out loud." },
      { k: "phi", b: "Patient data safety", emo: "\u{1F512}", fearQ: "\u201CIs patient information safe in here?\u201D", fearA: "Uploads get scanned for PHI automatically and stay private by default. Nothing shares itself." },
      { k: "complex", b: "Too complicated for me", emo: "\u{1F92F}", fearQ: "\u201CThis looks complicated\u2026\u201D", fearA: "You just did the hardest part \u2014 clicking buttons. Say \u201Cshow me\u201D anytime and I\u2019ll walk you through it." },
      { k: "none", b: "Honestly? Nothing \u{1F60E}", emo: "\u{1F60E}", fearQ: "\u201CImpress me.\u201D", fearA: "Open the platform schematic \u2014 30 modules, honest live/planned status on every one. Then ask me anything on it." }
    ];
    const PERSONAS = {
      patient_outreach: { hook: "Ask freely. The compliance worrying is my job.", tryits: ["Upload this document and tell me what\u2019s in it", "What\u2019s the prior auth rule for outpatient?", "Who can see my uploads?"] },
      check_in_patients: { hook: "The coworker who always knows \u2014 and never sighs.", tryits: ["What does code H0019 mean?", "Show me how to change my answer style", "Where did my last conversation go?"] },
      rework_denials: { hook: "Denials, codes, timely filing \u2014 answered with receipts.", tryits: ["What\u2019s the timely filing rule for Sunshine?", "Is Dr. Chen enrolled with Medicaid?", "Remind me to rework that H0019 denial"] },
      credentialing: { hook: "\u201CIs this provider payable?\u201D \u2014 one question, whole answer.", tryits: ["How is our panel doing?", "NPPES errors for Acme Health?", "Show me the credentialing report"] },
      submit_claims: { hook: "Denials, codes, timely filing \u2014 answered with receipts.", tryits: ["Does this payer cover telehealth?", "What\u2019s the timely filing rule here?", "Draft an appeal for CARC 197"] },
      strategy: { hook: "Real claims data. Real benchmarks. Zero slideware.", tryits: ["How big is the FL Medicaid BH market?", "Benchmark my organization", "Where are we underpaid?"] },
      default: { hook: "Ask me anything about payers, policies, or your documents.", tryits: ["What can Mobius do for me?", "What does code H0019 mean?", "Show me around"] }
    };
    let step = 0;
    let acts = [];
    let toneKey = null;
    let autoKey = null;
    let expLevel = null;
    let hesList = [];
    function _writePrefs(body) {
      void apiFetch(`${authApiBase}/auth/preferences`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ...body, source: "training_mode" })
      }).catch(() => {
      });
    }
    function _sendTrainingEvent(eventType, source, text) {
      void apiFetch(`${API_BASE}/chat/training-event`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ event_type: eventType, source, text })
      }).catch(() => {
      });
    }
    function _finishOnboarding() {
      void apiFetch(`${authApiBase}/auth/onboarding`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({})
      }).then(() => {
        void _fetchNestedUserProfile();
      }).catch(() => {
      });
      _syncOnboardingNudge(true);
    }
    function _dismiss(permanent) {
      wrap.hidden = true;
      wrap.innerHTML = "";
      if (permanent) {
        _sendTrainingEvent("training_dismissed");
        _finishOnboarding();
      } else {
        _sendTrainingEvent("training_skipped");
        sessionStorage.setItem("_tm_skip", "1");
      }
    }
    function prog(n) {
      return `<div class="tm-prog">${[0, 1, 2, 3, 4].map(
        (i) => `<span class="tm-prog__dot${i < n ? " tm-prog__dot--on" : ""}"></span>`
      ).join("")}</div>`;
    }
    function mainScenario() {
      return SCENARIOS.find((s) => s.k === acts[0]) ?? SCENARIOS[2];
    }
    function _bindX() {
      wrap.querySelector(".tm-x")?.addEventListener("click", () => _dismiss(true));
    }
    function _render() {
      if (step === 0) {
        wrap.innerHTML = `<div class="tm-card">
          <button class="tm-x" aria-label="Don't show again">&times;</button>
          ${prog(0)}
          <h2 class="tm-h2">Hey ${esc(name)} \u{1F44B} I'm Mobius.</h2>
          <p class="tm-sub">${arrival === "invited" ? "Your org set you up \u2014 zero forms." : "Welcome in."} Give me <strong>90 seconds</strong>: you click, I learn how you like to work. Retrain me anytime.</p>
          <div class="tm-row">
            <button class="tm-primary" data-go>Let\u2019s go \u2192</button>
            <button class="tm-ghost" data-skip>skip \u2014 I\u2019ll explore on my own</button>
          </div></div>`;
        _bindX();
        wrap.querySelector("[data-go]")?.addEventListener("click", () => {
          step = 1;
          _render();
        });
        wrap.querySelector("[data-skip]")?.addEventListener("click", () => _dismiss(false));
      } else if (step === 1) {
        wrap.innerHTML = `<div class="tm-card">
          <button class="tm-x" aria-label="Don't show again">&times;</button>
          ${prog(1)}
          <h2 class="tm-h2">What walked through your door this week?</h2>
          <p class="tm-sub">Pick the situations you actually deal with \u2014 first pick becomes the thread we use to tune everything.</p>
          <div class="tm-grid">${SCENARIOS.map((a) => `<button class="tm-act${acts.includes(a.k) ? " tm-act--on" : ""}" data-k="${esc(a.k)}">${esc(a.label)}${acts[0] === a.k ? '<span class="tm-act__star">\u2605 your main thing</span>' : ""}</button>`).join("")}</div>
          <div class="tm-row"><button class="tm-primary" data-next${acts.length ? "" : " disabled"}>That\u2019s me \u2192</button></div></div>`;
        _bindX();
        wrap.querySelectorAll(".tm-act").forEach((b) => b.addEventListener("click", () => {
          const k = b.dataset.k;
          acts = acts.includes(k) ? acts.filter((x) => x !== k) : [...acts, k];
          _render();
        }));
        wrap.querySelector("[data-next]")?.addEventListener("click", () => {
          if (!acts.length)
            return;
          _writePrefs({ activities: acts.map((k) => SCENARIOS.find((s) => s.k === k)?.act ?? k) });
          step = 2;
          _render();
        });
      } else if (step === 2) {
        const sc = mainScenario();
        wrap.innerHTML = `<div class="tm-card">
          <button class="tm-x" aria-label="Don't show again">&times;</button>
          ${prog(2)}
          <h2 class="tm-h2">Your situation. Three ways to answer it.</h2>
          <p class="tm-sub">You asked: <strong>\u201C${esc(sc.q)}\u201D</strong> \u2014 no labels, no right answer. Tap the reply you\u2019d rather read:</p>
          ${sc.tones.map((t) => `<button class="tm-tone" data-k="${esc(t[0])}"><p>${esc(t[1])}</p></button>`).join("")}</div>`;
        _bindX();
        wrap.querySelectorAll(".tm-tone").forEach((b) => b.addEventListener("click", () => {
          toneKey = b.dataset.k;
          _writePrefs({ tone: toneKey });
          step = 3;
          _render();
        }));
      } else if (step === 3 && !autoKey) {
        wrap.innerHTML = `<div class="tm-card">
          <button class="tm-x" aria-label="Don't show again">&times;</button>
          ${prog(3)}
          <h2 class="tm-h2">A denial needs reworking.</h2>
          <p class="tm-sub">Real scenario \u2014 this is <strong>sensitive</strong> territory (billing). I found the fix. What should I do?</p>
          ${AUTONOMY.map((a) => `<button class="tm-bigchip" data-k="${esc(a.k)}"><strong>${esc(a.b)}</strong><span>${esc(a.s)}</span></button>`).join("")}</div>`;
        _bindX();
        wrap.querySelectorAll(".tm-bigchip").forEach((b) => b.addEventListener("click", () => {
          autoKey = b.dataset.k;
          _writePrefs({ autonomy_sensitive: autoKey });
          _render();
        }));
      } else if (step === 3 && autoKey) {
        const sc = mainScenario();
        const autoLabel = AUTONOMY.find((a) => a.k === autoKey)?.b ?? "show me first";
        wrap.innerHTML = `<div class="tm-card">
          <button class="tm-x" aria-label="Don't show again">&times;</button>
          ${prog(3)}
          <h2 class="tm-h2">Deal: \u201C${esc(autoLabel)}\u201D for sensitive work.</h2>
          <p class="tm-sub">One more \u2014 <strong>how much should I explain?</strong> Same question, three depths. Tap what you\u2019d actually want:</p>
          ${sc.depths.map((d) => `<button class="tm-tone" data-k="${esc(d[0])}"><p>${esc(d[1])}</p></button>`).join("")}</div>`;
        _bindX();
        wrap.querySelectorAll(".tm-tone").forEach((b) => b.addEventListener("click", () => {
          expLevel = b.dataset.k;
          _writePrefs({ experience_level: expLevel });
          step = 4;
          _render();
        }));
      } else if (step === 4) {
        wrap.innerHTML = `<div class="tm-card">
          <button class="tm-x" aria-label="Don't show again">&times;</button>
          ${prog(4)}
          <h2 class="tm-h2">Last one. Anything make you hesitant?</h2>
          <p class="tm-sub">Pick all that apply \u2014 honest answers get honest features. (Optional.)</p>
          ${HESITATIONS.map((h) => `<button class="tm-bigchip${hesList.includes(h.k) ? " tm-bigchip--on" : ""}" data-k="${esc(h.k)}"><strong>${esc(h.b)}${hesList.includes(h.k) ? " \u2713" : ""}</strong></button>`).join("")}
          <div class="tm-row">
            <button class="tm-primary" data-done>${hesList.length ? "That\u2019s them \u2192" : "Nothing, honestly \u2192"}</button>
            <button class="tm-ghost" data-skiph>skip this one</button>
          </div></div>`;
        _bindX();
        wrap.querySelectorAll(".tm-bigchip").forEach((b) => b.addEventListener("click", () => {
          const k = b.dataset.k;
          hesList = hesList.includes(k) ? hesList.filter((x) => x !== k) : [...hesList, k];
          _render();
        }));
        const advance = () => {
          if (hesList.length)
            _writePrefs({ hesitations: hesList });
          _sendTrainingEvent("training_completed");
          _finishOnboarding();
          step = 5;
          _render();
        };
        wrap.querySelector("[data-done]")?.addEventListener("click", advance);
        wrap.querySelector("[data-skiph]")?.addEventListener("click", advance);
      } else {
        _renderGraduation();
      }
    }
    function _renderGraduation() {
      const sc = mainScenario();
      const actKey = SCENARIOS.find((s) => s.k === acts[0])?.act ?? "default";
      const pa = PERSONAS[actKey] ?? PERSONAS["default"];
      const hes = HESITATIONS.find((h) => h.k === hesList[0]) ?? HESITATIONS[0];
      const autoLabel = AUTONOMY.find((a) => a.k === autoKey)?.b;
      const learned = [
        acts.length ? `\u2605 ${esc(acts[0])}${acts.length > 1 ? ` +${acts.length - 1}` : ""}` : "explorer",
        toneKey ? `\u{1F5E3} ${esc(toneKey)}` : "\u{1F5E3} professional",
        autoLabel ? `\u{1F39A} ${esc(autoLabel)}` : "\u{1F39A} show me first",
        expLevel ? `\u{1F9E0} ${esc(expLevel)}` : null,
        hesList.length ? `\u{1F62C} ${esc(HESITATIONS.find((h) => h.k === hesList[0])?.b ?? hesList[0])}${hesList.length > 1 ? ` +${hesList.length - 1}` : ""}` : null
      ].filter((x) => x !== null);
      const tryIts = acts.length ? acts.slice(0, 3).map((k) => SCENARIOS.find((s) => s.k === k)?.q ?? "").filter(Boolean) : pa.tryits.slice(0, 3);
      wrap.innerHTML = `<div class="tm-card tm-card--graduation">
        <button class="tm-x" aria-label="Close">&times;</button>
        ${prog(5)}
        <h2 class="tm-h2">Trained. Here\u2019s your Mobius, ${esc(name)} \u{1F393}</h2>
        <div class="tm-learned">${learned.map((t) => `<span>${t}</span>`).join("")}</div>
        <p class="tm-edit-note">Edit any of it in Preferences, or retrain by sending <code>/training</code>.</p>
        <p class="tm-hook">${esc(pa.hook)}</p>
        <div class="tm-flip" data-flipped="false">
          <div class="tm-flip-inner">
            <div class="tm-face tm-face--q">${esc(hes.emo)} ${esc(hes.fearQ)}<span class="tm-tap">tap to flip</span></div>
            <div class="tm-face tm-face--a">\u2705 ${esc(hes.fearA)}</div>
          </div>
        </div>
        <div class="tm-tryits">${tryIts.map((q) => `<button class="tm-try" data-q="${esc(q)}">${esc(q)}</button>`).join("")}</div>
        <div class="tm-composer">
          <input id="tmInput" class="tm-composer-input" placeholder="ask what you came for \u2014 or tap a starter above">
          <button class="tm-composer-send" id="tmSend">\u27A4</button>
        </div></div>`;
      wrap.querySelector(".tm-x")?.addEventListener("click", () => {
        wrap.hidden = true;
        wrap.innerHTML = "";
      });
      wrap.querySelector(".tm-flip")?.addEventListener("click", (e) => {
        const f = e.currentTarget;
        f.dataset.flipped = f.dataset.flipped === "true" ? "false" : "true";
      });
      let _fromChip = false;
      wrap.querySelectorAll(".tm-try").forEach((b) => b.addEventListener("click", () => {
        const ci = document.getElementById("tmInput");
        if (ci) {
          ci.value = b.dataset.q;
          _fromChip = true;
        }
      }));
      document.getElementById("tmInput")?.addEventListener("input", () => {
        _fromChip = false;
      });
      const fire = () => {
        const ci = document.getElementById("tmInput");
        const v = (ci?.value ?? "").trim();
        if (!v)
          return;
        const src = _fromChip ? "chip" : "typed";
        wrap.hidden = true;
        wrap.innerHTML = "";
        _sendTrainingEvent("graduation_question_fired", src, v);
        if (src === "typed") {
          const _gradAreaTags = {
            rework_denials: "appeals",
            credentialing: "credentialing"
          };
          const _gradTag = _gradAreaTags[actKey] ?? "rag";
          void apiFetch(`${API_BASE}/chat/product-feedback`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ verbatim: v, category: "feature_request", trigger: "graduation", area_tags: [_gradTag] })
          }).catch(() => {
          });
        }
        sendMessage(v);
      };
      document.getElementById("tmSend")?.addEventListener("click", fire);
      document.getElementById("tmInput")?.addEventListener("keydown", (e) => {
        if (e.key === "Enter")
          fire();
      });
    }
    wrap.hidden = false;
    _render();
  }
  let cachedProfile = null;
  function syncAnswerInsightsCheckbox() {
    const cb = document.getElementById("prefShowAnswerInsights");
    if (!cb)
      return;
    cb.checked = getShowLlmPerformance(cachedProfile);
    syncQueriesDumpVisibility(cachedProfile);
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
      const diagPanel = turnWrap.querySelector(".ac-tab-panel--diagnostics");
      const perf = turnWrap.querySelector(".llm-performance");
      const fb = turnWrap.querySelector(".feedback");
      if (perf)
        perf.insertAdjacentElement("afterend", el2);
      else if (diagPanel)
        diagPanel.appendChild(el2);
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
  function _injectDiagnosticsTab(bubble, opts) {
    if (bubble.querySelector(".ac-tab-panel--diagnostics"))
      return;
    const diagPanel = document.createElement("div");
    diagPanel.className = "ac-tab-panel ac-tab-panel--diagnostics";
    diagPanel.setAttribute("role", "tabpanel");
    diagPanel.setAttribute("hidden", "");
    if (opts.insightRows.length > 0) {
      const perfEl = renderLlmPerformance(
        opts.insightRows,
        opts.perfMeta,
        {
          qc: opts.qc ?? void 0,
          sourceConfidenceStrip: opts.sourceConfidenceStrip,
          correlationId: opts.correlationId,
          totalCostFallback: opts.totalCostFallback,
          inputTokens: opts.inputTokens,
          outputTokens: opts.outputTokens,
          routingFeedback: opts.routingFeedback
        }
      );
      diagPanel.appendChild(perfEl);
    }
    const traceEl = renderRetrievalTrace(
      opts.thinkingLog
    );
    if (traceEl)
      diagPanel.appendChild(traceEl);
    if (opts.hipaaDiagnostics) {
      const hd = opts.hipaaDiagnostics;
      const hipaaSection = document.createElement("div");
      hipaaSection.className = "diag-hipaa-section collapsed";
      const gateLabel = hd.gate === "clean" ? "clean" : hd.gate === "indeterminate" ? "indeterminate" : "phi";
      const gateColor = hd.gate === "clean" ? "#22c55e" : hd.gate === "indeterminate" ? "#f59e0b" : "#ef4444";
      const isPublicEligible = hd.action_taken === "published";
      let ceilingLabel = "\u2014";
      if (hd.action_taken === "published")
        ceilingLabel = "public-eligible";
      else if (hd.action_taken === "published_private")
        ceilingLabel = "private (PHI suspected)";
      else if (hd.action_taken === "blocked")
        ceilingLabel = "blocked";
      else if (hd.action_taken === "blocked_indeterminate")
        ceilingLabel = "blocked (indeterminate)";
      const header = document.createElement("button");
      header.type = "button";
      header.className = "diag-hipaa-toggle";
      header.innerHTML = `
        <span class="diag-hipaa-chevron">\u25B6</span>
        <span class="diag-hipaa-title">HIPAA Screening</span>
        <span class="diag-hipaa-gate" style="color:${gateColor};">${gateLabel.toUpperCase()}</span>
        <span class="diag-hipaa-summary-pill">${escapeHtml4(ceilingLabel)}</span>`;
      header.addEventListener("click", () => {
        const collapsed = hipaaSection.classList.toggle("collapsed");
        header.querySelector(".diag-hipaa-chevron").textContent = collapsed ? "\u25B6" : "\u25BC";
      });
      const body = document.createElement("div");
      body.className = "diag-hipaa-body";
      const table = document.createElement("table");
      table.className = "diag-hipaa-table";
      table.innerHTML = `
        <tr><td class="diag-hipaa-key">Document</td><td class="diag-hipaa-val">${escapeHtml4(hd.document_name)}</td></tr>
        <tr><td class="diag-hipaa-key">PHI detected</td><td class="diag-hipaa-val">${hd.phi_flag ? "Yes" : "No"}</td></tr>
        <tr><td class="diag-hipaa-key">Classification ceiling</td><td class="diag-hipaa-val">${escapeHtml4(ceilingLabel)}</td></tr>
        <tr><td class="diag-hipaa-key">HIPAA mode</td><td class="diag-hipaa-val">${hd.hipaa_mode_allowed ? "ON" : "OFF"}</td></tr>
        ${hd.identifier_labels.length ? `<tr><td class="diag-hipaa-key">Identifiers</td><td class="diag-hipaa-val">${hd.identifier_labels.map((l) => `<span class="diag-hipaa-pill">${escapeHtml4(l)}</span>`).join(" ")}</td></tr>` : ""}
        <tr><td class="diag-hipaa-key">Transaction</td><td class="diag-hipaa-val diag-hipaa-mono">${escapeHtml4(hd.transaction_id || "\u2014")}</td></tr>`;
      body.appendChild(table);
      if (isPublicEligible && hd.transaction_id) {
        const docIdForPromote = hd.document_id || "";
        const promoteRow = document.createElement("div");
        promoteRow.className = "diag-hipaa-promote-row";
        const promoteBtn = document.createElement("button");
        promoteBtn.type = "button";
        promoteBtn.className = "diag-hipaa-promote-btn";
        promoteBtn.textContent = "Make public";
        promoteBtn.title = "Promote this document to the shared corpus (admin only)";
        promoteBtn.addEventListener("click", async () => {
          if (!docIdForPromote) {
            showChatStatusBanner("Cannot promote \u2014 document ID unknown.", 4e3);
            return;
          }
          promoteBtn.disabled = true;
          promoteBtn.textContent = "Promoting\u2026";
          try {
            const token = window.__mobiusAuthToken || "";
            const res = await fetch(`/chat/documents/${encodeURIComponent(docIdForPromote)}/promote`, {
              method: "POST",
              headers: { "Authorization": `Bearer ${token}`, "Content-Type": "application/json" },
              body: JSON.stringify({ visibility: "public" })
            });
            if (res.ok) {
              promoteBtn.textContent = "\u2713 Public";
              promoteBtn.classList.add("diag-hipaa-promote-btn--done");
            } else {
              const err = await res.json().catch(() => ({}));
              promoteBtn.textContent = "Make public";
              promoteBtn.disabled = false;
              showChatStatusBanner(`Promote failed: ${err.detail || res.status}`, 5e3);
            }
          } catch (_e) {
            promoteBtn.textContent = "Make public";
            promoteBtn.disabled = false;
            showChatStatusBanner("Promote request failed \u2014 check connection.", 4e3);
          }
        });
        promoteRow.appendChild(promoteBtn);
        body.appendChild(promoteRow);
      }
      hipaaSection.appendChild(header);
      hipaaSection.appendChild(body);
      diagPanel.appendChild(hipaaSection);
    }
    const tabBar = bubble.querySelector(".ac-tab-bar");
    if (tabBar) {
      const diagBtn = document.createElement("button");
      diagBtn.type = "button";
      diagBtn.className = "ac-tab ac-tab--diagnostics";
      diagBtn.setAttribute("role", "tab");
      diagBtn.setAttribute("aria-selected", "false");
      diagBtn.setAttribute("data-panel", "diagnostics");
      diagBtn.textContent = "Diagnostics";
      diagBtn.addEventListener("click", () => {
        const liveBubble = diagBtn.closest(".answer-card-bubble") ?? bubble;
        tabBar.querySelectorAll(".ac-tab").forEach((t) => {
          t.classList.remove("ac-tab--active");
          t.setAttribute("aria-selected", "false");
        });
        liveBubble.querySelectorAll(".ac-tab-panel").forEach((p) => {
          p.hidden = true;
          p.classList.remove("ac-tab-panel--active");
        });
        diagBtn.classList.add("ac-tab--active");
        diagBtn.setAttribute("aria-selected", "true");
        diagPanel.hidden = false;
        diagPanel.classList.add("ac-tab-panel--active");
      });
      tabBar.appendChild(diagBtn);
    }
    bubble.appendChild(diagPanel);
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
  let cachedUserProfileNested = null;
  async function _fetchNestedUserProfile() {
    try {
      const headers = await auth.getAuthHeader?.();
      if (!headers) {
        cachedUserProfileNested = null;
        return;
      }
      const r = await fetch(`${authApiBase}/auth/me`, { headers });
      if (!r.ok) {
        cachedUserProfileNested = null;
        return;
      }
      const data = await r.json();
      const user = data && data.user ? data.user : null;
      const p = user && user.profile || null;
      cachedUserProfileNested = p && typeof p === "object" ? p : null;
      if (user) {
        const nameFromMe = user.preferred_name || user.first_name || user.display_name || (user.email ? user.email.split("@")[0] : null);
        if (sidebarUserName && (!sidebarUserName.textContent || sidebarUserName.textContent === "Guest")) {
          if (nameFromMe)
            sidebarUserName.textContent = nameFromMe;
        }
        const tmName = nameFromMe ?? "there";
        if (user.is_onboarded === false) {
          _showTrainingMode(tmName, "invited");
        } else if (new URL(location.href).searchParams.get("welcome") === "1") {
          _showTrainingMode(tmName, "invited", true);
        }
        _syncOnboardingNudge(user.is_onboarded !== false);
      }
    } catch {
      cachedUserProfileNested = null;
    }
  }
  auth.on(() => {
    void auth.getUserProfile().then((p) => {
      cachedProfile = p;
      updateSidebarUser(p);
      syncAnswerInsightsCheckbox();
      _setAuthGate(!p);
      if (!p)
        _syncOnboardingNudge(true);
      loadSidebarHistory();
    });
    void _fetchNestedUserProfile();
  });
  void auth.getUserProfile().then((p) => {
    cachedProfile = p;
    updateSidebarUser(p);
    syncAnswerInsightsCheckbox();
    _setAuthGate(!p);
    if (p)
      loadSidebarHistory();
  });
  void _fetchNestedUserProfile();
  const prefShowAnswerInsights = document.getElementById(
    "prefShowAnswerInsights"
  );
  prefShowAnswerInsights?.addEventListener("change", () => {
    try {
      localStorage.setItem(LLM_PERF_LS, prefShowAnswerInsights.checked ? "1" : "0");
    } catch {
    }
    syncQueriesDumpVisibility(cachedProfile);
  });
  if (sidebarUser) {
    const userMenu = createUserMenu({
      auth,
      onOpenPreferences: () => {
        prefsModal.open();
      },
      onSignOut: () => {
        modal.open("login");
      }
    });
    sidebarUser.addEventListener("click", () => {
      void auth.getUserProfile().then((user) => {
        if (user) {
          void userMenu.show(sidebarUser);
        } else {
          modal.open("login");
        }
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
  initSidebarRailIcons(auth);
  hamburger.addEventListener("click", openDrawer);
  document.getElementById("btnTasksModal")?.addEventListener("click", () => {
    closeDrawer();
    openTasksModal();
  });
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
  setupQueriesDumpUI();
  syncQueriesDumpVisibility(cachedProfile);
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
      const STALL_MS = 9e4;
      let attempts = 0;
      const seenLines = /* @__PURE__ */ new Set();
      let lastMessageLen = 0;
      let lastStatus;
      let lastProgressMs = Date.now();
      function poll() {
        fetch(API_BASE + "/chat/response/" + correlationId).then((r) => r.json()).then((data) => {
          let progressed = false;
          if (data.thinking_log?.length && onThinking) {
            data.thinking_log.forEach((entry) => {
              const line = thinkingLineFromEntry(entry);
              if (!seenLines.has(line)) {
                seenLines.add(line);
                onThinking(line);
                progressed = true;
              }
            });
          }
          if (data.message != null && data.message !== "" && onStreamingMessage) {
            onStreamingMessage(data.message);
            if (data.message.length !== lastMessageLen) {
              lastMessageLen = data.message.length;
              progressed = true;
            }
          }
          if (data.status && data.status !== lastStatus) {
            lastStatus = data.status;
            progressed = true;
          }
          if (progressed) {
            lastProgressMs = Date.now();
          }
          if (data.status === "completed" || data.status === "clarification" || data.status === "refinement_ask" || data.status === "failed") {
            resolve(data);
            return;
          }
          if (Date.now() - lastProgressMs > STALL_MS) {
            reject(new Error(
              "Request appears to have been lost (no progress for " + Math.round(STALL_MS / 1e3) + "s). Please retry."
            ));
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
  function streamResponse(correlationId, onThinking, onStreamingMessage, onDraftReady) {
    if (typeof EventSource === "undefined") {
      return pollResponse(correlationId, onThinking, onStreamingMessage);
    }
    const streamUrl = API_BASE + "/chat/stream/" + encodeURIComponent(correlationId);
    return new Promise((resolve, reject) => {
      let messageSoFar = "";
      let resolved = false;
      let draftEmitted = false;
      const STALL_MS = 9e4;
      let lastEventMs = Date.now();
      const es = new EventSource(streamUrl);
      const stallTimer = window.setInterval(() => {
        if (resolved)
          return;
        if (Date.now() - lastEventMs > STALL_MS) {
          resolved = true;
          es.close();
          window.clearInterval(stallTimer);
          reject(new Error(
            "Request appears to have been lost (no progress for " + Math.round(STALL_MS / 1e3) + "s). Please retry."
          ));
        }
      }, 5e3);
      es.onmessage = (e) => {
        lastEventMs = Date.now();
        try {
          const parsed = JSON.parse(e.data);
          const ev = parsed.event;
          const data = parsed.data ?? {};
          if (ev === "thinking" && data.line != null && onThinking) {
            onThinking(String(data.line));
          } else if (ev === "quality_audit" && data.line != null && onThinking) {
            onThinking(String(data.line));
          } else if (ev === "draft_ready" && data.text != null) {
            draftEmitted = true;
            if (onDraftReady)
              onDraftReady(String(data.text), data.mode_hint ? String(data.mode_hint) : void 0);
          } else if (ev === "message" && data.chunk != null && !draftEmitted && onStreamingMessage) {
            messageSoFar += String(data.chunk);
            onStreamingMessage(messageSoFar);
          } else if (ev === "completed" && data) {
            resolved = true;
            es.close();
            window.clearInterval(stallTimer);
            resolve(data);
          } else if (ev === "error" && data.message != null) {
            resolved = true;
            es.close();
            window.clearInterval(stallTimer);
            reject(new Error(String(data.message)));
          }
        } catch (err) {
          resolved = true;
          es.close();
          window.clearInterval(stallTimer);
          reject(err instanceof Error ? err : new Error(String(err)));
        }
      };
      es.onerror = () => {
        es.close();
        if (resolved)
          return;
        window.clearInterval(stallTimer);
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
        hintEl.textContent = "We will run the outside-in Medicaid NPI pipeline. Upload a roster below if you want reconciliation (your file vs external data), or use the \u{1F4CE} paperclip to attach a file.";
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
  let _pendingMentions = [];
  let _pendingHipaaDiagnostics = null;
  let _coworkerFetchTimer = null;
  let _coworkerDropdown = null;
  async function _fetchCoworkers(q) {
    try {
      const params = q.trim() ? `?q=${encodeURIComponent(q.trim())}&limit=8` : "?limit=8";
      const r = await apiFetch(`${API_BASE}/chat/coworkers${params}`);
      if (!r.ok)
        return [];
      const d = await r.json();
      return Array.isArray(d.coworkers) ? d.coworkers : [];
    } catch {
      return [];
    }
  }
  function _closeAtDropdown() {
    _coworkerDropdown?.remove();
    _coworkerDropdown = null;
  }
  function _openAtDropdown(anchor, coworkers, atStart, atEnd, query) {
    _closeAtDropdown();
    if (!coworkers.length && !query.trim())
      return;
    const dd = document.createElement("div");
    dd.className = "at-mention-dropdown";
    dd.setAttribute("role", "listbox");
    const rect = anchor.getBoundingClientRect();
    dd.style.cssText = `position:fixed;bottom:${window.innerHeight - rect.top + 4}px;left:${rect.left}px;min-width:220px;max-width:320px;z-index:9999;`;
    if (!coworkers.length) {
      const empty = document.createElement("div");
      empty.className = "at-mention-empty";
      empty.textContent = "No matches";
      dd.appendChild(empty);
    } else {
      coworkers.forEach((c, i) => {
        const item = document.createElement("button");
        item.type = "button";
        item.className = "at-mention-item";
        item.setAttribute("role", "option");
        const agentBadge = c.is_agent ? `<span class="at-mention-badge">agent</span>` : "";
        item.innerHTML = `<span class="at-mention-name">${escapeHtml4(c.display_name)}${agentBadge}</span>${c.email ? `<span class="at-mention-email">${escapeHtml4(c.email)}</span>` : ""}`;
        item.addEventListener("mousedown", (e) => {
          e.preventDefault();
          const val = inputEl.value;
          const inserted = `@${c.display_name} `;
          inputEl.value = val.slice(0, atStart) + inserted + val.slice(atEnd);
          inputEl.selectionStart = inputEl.selectionEnd = atStart + inserted.length;
          _pendingMentions.push({ display_name: c.display_name, assignee_ref: c.assignee_ref });
          _closeAtDropdown();
          inputEl.focus();
        });
        if (i === 0)
          item.classList.add("at-mention-item--focused");
        dd.appendChild(item);
      });
    }
    document.body.appendChild(dd);
    _coworkerDropdown = dd;
  }
  inputEl.addEventListener("input", () => {
    const val = inputEl.value;
    const pos = inputEl.selectionStart ?? val.length;
    const before = val.slice(0, pos);
    const atMatch = before.match(/@(\w*)$/);
    if (!atMatch) {
      _closeAtDropdown();
      return;
    }
    const q = atMatch[1];
    const atStart = pos - atMatch[0].length;
    if (_coworkerFetchTimer)
      clearTimeout(_coworkerFetchTimer);
    _coworkerFetchTimer = setTimeout(async () => {
      const results = await _fetchCoworkers(q);
      _openAtDropdown(inputEl, results, atStart, pos, q);
    }, 120);
  });
  inputEl.addEventListener("keydown", (e) => {
    if (!_coworkerDropdown)
      return;
    const items = Array.from(_coworkerDropdown.querySelectorAll(".at-mention-item"));
    const focused = _coworkerDropdown.querySelector(".at-mention-item--focused");
    const idx = focused ? items.indexOf(focused) : -1;
    if (e.key === "ArrowDown") {
      e.preventDefault();
      focused?.classList.remove("at-mention-item--focused");
      items[Math.min(idx + 1, items.length - 1)]?.classList.add("at-mention-item--focused");
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      focused?.classList.remove("at-mention-item--focused");
      items[Math.max(idx - 1, 0)]?.classList.add("at-mention-item--focused");
    } else if (e.key === "Enter" && focused) {
      e.preventDefault();
      focused.dispatchEvent(new MouseEvent("mousedown", { bubbles: true }));
    } else if (e.key === "Escape") {
      _closeAtDropdown();
    }
  });
  document.addEventListener("click", (e) => {
    if (_coworkerDropdown && !_coworkerDropdown.contains(e.target))
      _closeAtDropdown();
  });
  function sendMessage(overrideMessage, opts) {
    void _sendMessageAsync(overrideMessage, opts);
  }
  async function _sendMessageAsync(overrideMessage, opts) {
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
    if (message === "/training" || message === "/welcome") {
      inputEl.value = "";
      const tmName = cachedProfile?.["preferred_name"] || cachedProfile?.["first_name"] || (sidebarUserName?.textContent ?? "there");
      _showTrainingMode(tmName.trim() || "there", "invited", true);
      return;
    }
    if (sendBtn.disabled)
      return;
    activeClarificationDraft = null;
    if (!opts?.credentialing_options && !opts?.skipCredentialingEnvelope && isCredentialingReportIntent(message)) {
      openCredentialingEnvelope(message);
      return;
    }
    if (chatEmpty)
      chatEmpty.classList.add("hidden");
    if (alphaBanner && !alphaBanner.hidden) {
      alphaBanner.hidden = true;
      localStorage.setItem("alpha_banner_dismissed", "1");
    }
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
    const _sugSlot = document.getElementById("chat-suggestions");
    if (_sugSlot) {
      _sugSlot.innerHTML = "";
      _sugSlot.hidden = true;
    }
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
    let draftStreamCancel = null;
    let composerReleased = false;
    function releaseComposer() {
      if (composerReleased)
        return;
      composerReleased = true;
      sendBtn.disabled = false;
      inputEl.disabled = false;
      updateSendState();
    }
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
    function onDraftReady(text, modeHint) {
      if (messageWrapEl) {
        messageWrapEl.remove();
        messageWrapEl = null;
      }
      if (modeHint === "RECITAL") {
        let recitalStreamStep2 = function() {
          if (cancelled2)
            return;
          wi2 = Math.min(wi2 + 5, words2.length);
          prose2.innerHTML = simpleMarkdownToHtml(words2.slice(0, wi2).join(" "));
          scrollToBottom(messagesEl);
          if (wi2 < words2.length)
            window.setTimeout(recitalStreamStep2, 18);
          else {
            draftStreamCancel = null;
            cursor2.remove();
          }
        };
        var recitalStreamStep = recitalStreamStep2;
        const wrap2 = document.createElement("div");
        wrap2.className = "message message--assistant answer-card answer-card--recital is-streaming";
        const bubble2 = document.createElement("div");
        bubble2.className = "message-bubble answer-card-bubble";
        const attr = document.createElement("div");
        attr.className = "recital-attr";
        attr.textContent = "From the Mobius founding essay:";
        bubble2.appendChild(attr);
        const prose2 = document.createElement("div");
        prose2.className = "recital-prose";
        const cursor2 = document.createElement("span");
        cursor2.className = "ac-streaming-cursor";
        cursor2.setAttribute("aria-hidden", "true");
        bubble2.appendChild(prose2);
        bubble2.appendChild(cursor2);
        wrap2.appendChild(bubble2);
        messageWrapEl = wrap2;
        turnWrap.appendChild(messageWrapEl);
        releaseComposer();
        const words2 = text.split(" ");
        let wi2 = 0;
        let cancelled2 = false;
        draftStreamCancel = () => {
          cancelled2 = true;
          prose2.innerHTML = simpleMarkdownToHtml(sanitizeDisplayMessage(text));
          cursor2.remove();
          scrollToBottom(messagesEl);
        };
        recitalStreamStep2();
        return;
      }
      const wrap = document.createElement("div");
      wrap.className = "message message--assistant answer-card answer-card--blended is-streaming";
      const bubble = document.createElement("div");
      bubble.className = "message-bubble answer-card-bubble";
      const streamTabBar = document.createElement("div");
      streamTabBar.className = "ac-tab-bar";
      streamTabBar.setAttribute("role", "tablist");
      const _mkStreamBtn = (label, panel, active) => {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "ac-tab" + (active ? " ac-tab--active" : "");
        btn.setAttribute("role", "tab");
        btn.setAttribute("aria-selected", String(active));
        btn.setAttribute("data-panel", panel);
        if (!active)
          btn.setAttribute("data-empty", "1");
        btn.textContent = label;
        btn.addEventListener("click", () => {
          const lb = btn.closest(".answer-card-bubble") ?? bubble;
          streamTabBar.querySelectorAll(".ac-tab").forEach((t) => {
            t.classList.remove("ac-tab--active");
            t.setAttribute("aria-selected", "false");
          });
          lb.querySelectorAll(".ac-tab-panel").forEach((p) => {
            p.hidden = true;
            p.classList.remove("ac-tab-panel--active");
          });
          btn.classList.add("ac-tab--active");
          btn.setAttribute("aria-selected", "true");
          const tp = lb.querySelector(`.ac-tab-panel--${panel}`);
          if (tp) {
            tp.hidden = false;
            tp.classList.add("ac-tab-panel--active");
          }
        });
        return btn;
      };
      streamTabBar.appendChild(_mkStreamBtn("Summary", "summary", true));
      streamTabBar.appendChild(_mkStreamBtn("Citations", "citations", false));
      streamTabBar.appendChild(_mkStreamBtn("Corrections", "corrections", false));
      streamTabBar.appendChild(_mkStreamBtn("Follow-up", "next-steps", false));
      streamTabBar.appendChild(_mkStreamBtn("Tasks", "tasks", false));
      bubble.appendChild(streamTabBar);
      const summaryPanel = document.createElement("div");
      summaryPanel.className = "ac-tab-panel ac-tab-panel--summary ac-tab-panel--active";
      summaryPanel.setAttribute("role", "tabpanel");
      const prose = document.createElement("div");
      prose.className = "ac-summary-prose";
      const cursor = document.createElement("span");
      cursor.className = "ac-streaming-cursor";
      cursor.setAttribute("aria-hidden", "true");
      summaryPanel.appendChild(prose);
      summaryPanel.appendChild(cursor);
      const statusEl = document.createElement("span");
      statusEl.className = "ac-streaming-status";
      const _statusPhrases = ["Searching sources\u2026", "Refining answer\u2026", "Checking accuracy\u2026", "Summarizing\u2026"];
      let _statusIdx = 0;
      statusEl.textContent = _statusPhrases[0];
      summaryPanel.appendChild(statusEl);
      const _statusInterval = window.setInterval(() => {
        statusEl.classList.add("ac-status-fade");
        window.setTimeout(() => {
          _statusIdx = (_statusIdx + 1) % _statusPhrases.length;
          statusEl.textContent = _statusPhrases[_statusIdx];
          statusEl.classList.remove("ac-status-fade");
        }, 400);
      }, 3e3);
      bubble.dataset.statusInterval = String(_statusInterval);
      bubble.appendChild(summaryPanel);
      ["citations", "corrections", "next-steps", "tasks"].forEach((p) => {
        const panel = document.createElement("div");
        panel.className = `ac-tab-panel ac-tab-panel--${p}`;
        panel.setAttribute("role", "tabpanel");
        panel.setAttribute("hidden", "");
        bubble.appendChild(panel);
      });
      wrap.appendChild(bubble);
      messageWrapEl = wrap;
      turnWrap.appendChild(messageWrapEl);
      releaseComposer();
      const words = text.split(" ");
      let wi = 0;
      let cancelled = false;
      draftStreamCancel = () => {
        cancelled = true;
        prose.innerHTML = simpleMarkdownToHtml(sanitizeDisplayMessage(text));
        cursor.remove();
        scrollToBottom(messagesEl);
      };
      function streamStep() {
        if (cancelled)
          return;
        wi = Math.min(wi + 5, words.length);
        prose.innerHTML = simpleMarkdownToHtml(words.slice(0, wi).join(" "));
        scrollToBottom(messagesEl);
        if (wi < words.length)
          window.setTimeout(streamStep, 18);
        else {
          draftStreamCancel = null;
          cursor.remove();
        }
      }
      streamStep();
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
    {
      const sel = document.getElementById("modelProfileSelect");
      const v = (sel && sel.value || "").trim();
      if (v)
        payload.model_profile = v;
    }
    if (cachedUserProfileNested) {
      payload.profile = cachedUserProfileNested;
    }
    if (_pendingMentions.length) {
      payload.mentions = _pendingMentions.slice();
      _pendingMentions = [];
    }
    let activeCorrelationId = "";
    const _chatAuthHeaders = await auth.getAuthHeader?.() ?? {};
    fetch(API_BASE + "/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json", ..._chatAuthHeaders },
      body: JSON.stringify(payload)
    }).then((r) => r.json()).then((data) => {
      if (data.thread_id)
        currentThreadId = data.thread_id;
      window.__mobiusChatThreadId = currentThreadId;
      activeCorrelationId = data.correlation_id ?? "";
      if ((data.correlation_id || "").trim()) {
        onRequestCorrelationId();
      }
      addThinkingLineAndScroll("Request sent. Waiting for worker\u2026");
      return streamResponse(data.correlation_id, addThinkingLineAndScroll, onStreamingMessage, onDraftReady);
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
      if (draftStreamCancel) {
        draftStreamCancel();
        draftStreamCancel = null;
      }
      const isStreamingCard = !!messageWrapEl?.classList.contains("is-streaming");
      if (data.thread_id)
        currentThreadId = data.thread_id;
      window.__mobiusChatThreadId = currentThreadId;
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
      if (isStreamingCard && messageWrapEl) {
        messageWrapEl.classList.remove("is-streaming");
        const existingBubble = messageWrapEl.querySelector(".answer-card-bubble");
        if (existingBubble?.dataset.statusInterval) {
          window.clearInterval(Number(existingBubble.dataset.statusInterval));
          existingBubble.querySelector(".ac-streaming-status")?.remove();
        }
        const _extractedCorrections = [];
        const _extractedNextStepTasks = [];
        if (useEnvelope) {
          for (const _eb of envCandidate.blocks || []) {
            const _ebt = _eb.type;
            if (_ebt === "callout") {
              const _cb = _eb;
              const _cbText = (_cb.body || "").trim();
              if (_cbText)
                _extractedCorrections.push({
                  label: _cb.variant === "warning" ? "Warning" : _cb.variant === "error" ? "Error" : "Note",
                  text: _cbText
                });
            } else if (_ebt === "correction") {
              const _cb = _eb;
              const _orig = (_cb.original || "").trim();
              const _fixed = (_cb.corrected || "").trim();
              if (_orig && _fixed)
                _extractedCorrections.push({ label: "Correction", text: _orig + " \u2192 " + _fixed });
            } else if (_ebt === "next_steps") {
              const _cb = _eb;
              normalizeFollowupLineList(_cb.items || [], false).forEach((item) => {
                if (item.text)
                  _extractedNextStepTasks.push({ text: item.text, taskType: "follow_up" });
              });
            }
          }
        }
        const fullCard = tryParseAnswerCard(fullMessage);
        const _isRecitalShell = !!existingBubble?.querySelector(".recital-prose");
        if (fullCard && existingBubble) {
          messageWrapEl.classList.remove("answer-card--blended");
          if (!_isRecitalShell)
            messageWrapEl.classList.remove("answer-card--recital");
          messageWrapEl.classList.add(`answer-card--${fullCard.mode.toLowerCase()}`);
          if (_isRecitalShell) {
            const prose = existingBubble.querySelector(".recital-prose");
            if (prose && fullCard.recital?.verbatim) {
              const PARA_LIMIT = 3;
              const stripped = fullCard.recital.verbatim.replace(/^[ \t]*[-*_]{3,}[ \t]*$/gm, "").trim();
              const allParas = stripped.split(/\n\n+/);
              if (allParas.length > PARA_LIMIT) {
                const clippedText = allParas.slice(0, PARA_LIMIT).join("\n\n");
                prose.innerHTML = simpleMarkdownToHtml(clippedText);
                const readMore = document.createElement("button");
                readMore.type = "button";
                readMore.className = "recital-read-more";
                readMore.textContent = "Read the full essay \u2197";
                let expanded = false;
                readMore.addEventListener("click", () => {
                  expanded = !expanded;
                  prose.innerHTML = simpleMarkdownToHtml(expanded ? stripped : clippedText);
                  readMore.textContent = expanded ? "Collapse \u2191" : "Read the full essay \u2197";
                  (readMore.closest(".answer-card--recital") ?? messageWrapEl).classList.toggle("recital-expanded", expanded);
                });
                existingBubble.appendChild(readMore);
              }
            }
          } else {
            const renderedCard = renderAnswerCard(fullCard, false, {
              onFollowupClick: (q) => sendMessage(q),
              sourceConfidenceStrip: (data.source_confidence_strip ?? "").trim() || void 0,
              showConfidenceBadge: data.status !== "clarification" && data.status !== "refinement_ask",
              suppressFollowups: nextQuestions.length > 0,
              nextQuestions,
              qcAudit: qcFromPayload,
              suppressConfidenceForAdminQcFail: suppressConf,
              corrections: _extractedCorrections,
              nextStepTasks: _extractedNextStepTasks
            });
            const renderedBubble = renderedCard.querySelector(".answer-card-bubble");
            if (renderedBubble) {
              const streamingTabBar = existingBubble.querySelector(".ac-tab-bar");
              const renderedTabBar = renderedBubble.querySelector(".ac-tab-bar");
              if (streamingTabBar && renderedTabBar) {
                existingBubble.replaceChild(renderedTabBar, streamingTabBar);
              }
              const existingSummaryPanel = existingBubble.querySelector(".ac-tab-panel--summary");
              const renderedSummaryPanel = renderedBubble.querySelector(".ac-tab-panel--summary");
              if (existingSummaryPanel && renderedSummaryPanel) {
                Array.from(renderedSummaryPanel.children).forEach((child) => {
                  existingSummaryPanel.appendChild(child);
                });
              }
              ["citations", "corrections", "next-steps", "tasks"].forEach((panelName) => {
                const existing = existingBubble.querySelector(`.ac-tab-panel--${panelName}`);
                const rendered = renderedBubble.querySelector(`.ac-tab-panel--${panelName}`);
                if (existing && rendered)
                  existingBubble.replaceChild(rendered, existing);
              });
              const actionsEl = renderedCard.querySelector(".answer-card-actions");
              if (actionsEl)
                turnWrap.appendChild(actionsEl);
              const inlineChips = Array.from(existingBubble.querySelectorAll(".answer-card-action-chip"));
              if (inlineChips.length > 0) {
                let hoistWrap = turnWrap.querySelector(".answer-card-actions");
                if (!hoistWrap) {
                  hoistWrap = document.createElement("div");
                  hoistWrap.className = "answer-card-actions";
                  turnWrap.appendChild(hoistWrap);
                }
                inlineChips.forEach((chip) => hoistWrap.appendChild(chip));
              }
            }
          }
        } else if (existingBubble) {
          messageWrapEl.classList.remove("answer-card");
          Array.from(messageWrapEl.classList).filter((c) => c.startsWith("answer-card--")).forEach((c) => messageWrapEl.classList.remove(c));
          existingBubble.classList.remove("answer-card-bubble");
          existingBubble.querySelector(".ac-tab-bar")?.remove();
          const prose = existingBubble.querySelector(".ac-summary-prose");
          if (prose && contentToShow && contentToShow !== "Formatting answer\u2026") {
            prose.innerHTML = simpleMarkdownToHtml(sanitizeDisplayMessage(contentToShow));
          }
          if (data.status !== "clarification" && !suppressConf) {
            const badgeEl = renderConfidenceBadge((data.source_confidence_strip ?? "").trim() || "informational_only");
            existingBubble.insertBefore(badgeEl, existingBubble.firstChild);
          }
        }
        if (useEnvelope && existingBubble) {
          const _hasTabs = !!(fullCard && (fullCard.citations && fullCard.citations.length > 0 || _extractedCorrections.length > 0 || _extractedNextStepTasks.length > 0 || nextQuestions.length > 0));
          const _suppressedChrome = new Set(
            _hasTabs ? ["tool_attribution", "detail", "callout", "correction", "next_steps"] : []
          );
          const toolBlocks = envCandidate.blocks.filter((b) => {
            const bt = b.type;
            return bt !== "direct_answer" && bt !== "sources" && !_suppressedChrome.has(bt);
          });
          if (toolBlocks.length > 0) {
            const toolEnv = { ...envCandidate, blocks: toolBlocks };
            const toolRendered = renderAssistantFromEnvelope(toolEnv, {
              onFollowupClick: (q) => sendMessage(q),
              sourceConfidenceStrip: (data.source_confidence_strip ?? "").trim() || void 0,
              showConfidenceBadge: false,
              qcAudit: qcFromPayload,
              correlationId: cidForTurn || null,
              suppressConfidenceForAdminQcFail: suppressConf,
              threadId: data.thread_id ?? currentThreadId ?? null
            });
            const innerBubble = toolRendered.querySelector(".message-bubble");
            if (innerBubble) {
              Array.from(innerBubble.children).forEach((child) => existingBubble.appendChild(child));
            }
          }
        }
        messageWrapEl.querySelectorAll(".envelope-takeaways").forEach((el2) => el2.remove());
        turnWrap.classList.add("turn-meta-revealing");
        window.setTimeout(() => turnWrap.classList.remove("turn-meta-revealing"), 1200);
      } else {
        if (messageWrapEl)
          messageWrapEl.remove();
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
        } else if (data.response_source === "content_filtered") {
          turnWrap.appendChild(
            renderAssistantMessage(contentToShow, false, { variant: "warn" })
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
      if (nextQuestions.length > 0) {
        updateChatSuggestions(nextQuestions, (q) => sendMessage(q));
      }
      if (data.clarification_options && data.clarification_options.length > 0) {
        turnWrap.appendChild(renderClarificationOptions(data.clarification_options));
      } else {
        activeClarificationDraft = null;
      }
      const hoistAct = turnWrap.querySelector(".answer-card-actions");
      if (hoistAct)
        turnWrap.appendChild(hoistAct);
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
      if (sourceList.length > 0 && (!envelopeHasSources || isStreamingCard)) {
        turnWrap.appendChild(
          renderSourceCiter(sourceList, cited, data.correlation_id ?? activeCorrelationId)
        );
      }
      const insightRows = data.usage_breakdown;
      const perfMeta = data.llm_performance;
      const hipaaForTab = _pendingHipaaDiagnostics;
      _pendingHipaaDiagnostics = null;
      if (getShowLlmPerformance(cachedProfile) && data.status === "completed") {
        const tin = Number(data.tokens_used?.input_tokens) || 0;
        const tout = Number(data.tokens_used?.output_tokens) || 0;
        const cardBubble = messageWrapEl?.querySelector(".answer-card-bubble");
        if (isStreamingCard && cardBubble) {
          _injectDiagnosticsTab(cardBubble, {
            insightRows: Array.isArray(insightRows) ? insightRows : [],
            perfMeta,
            thinkingLog: data.thinking_log,
            qc: qcFromPayload,
            sourceConfidenceStrip: data.source_confidence_strip ?? null,
            correlationId: data.correlation_id ?? activeCorrelationId,
            totalCostFallback: data.cost_usd,
            inputTokens: tin,
            outputTokens: tout,
            routingFeedback: data.technical_feedback?.llm_performance ?? null,
            hipaaDiagnostics: hipaaForTab
          });
        } else if (Array.isArray(insightRows) && insightRows.length > 0) {
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
          const retrievalPanel = renderRetrievalTrace(data.thinking_log);
          if (retrievalPanel)
            turnWrap.appendChild(retrievalPanel);
        }
      }
      mergeTechnicalPanels(turnWrap, data);
      mergeLlmPerformanceRoutingHydrate(turnWrap, data);
      turnWrap.appendChild(renderFeedback(data.correlation_id ?? activeCorrelationId));
      if (data.capture_card) {
        turnWrap.appendChild(renderCaptureCard(data.capture_card, {
          threadId: data.thread_id,
          correlationId: data.correlation_id ?? activeCorrelationId
        }));
      }
      if (data.offer_feedback) {
        turnWrap.appendChild(renderOfferFeedback(data.offer_feedback, {
          threadId: data.thread_id,
          correlationId: data.correlation_id ?? activeCorrelationId
        }));
      }
      if (data.demo) {
        turnWrap.appendChild(renderDemoChip(data.demo, {
          correlationId: data.correlation_id ?? activeCorrelationId
        }));
      }
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
      releaseComposer();
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
  function _looksLikeRosterCsv(firstLine) {
    const ROSTER_COLS = ["npi", "provider_name", "license_type", "license_number", "specialty", "taxonomy"];
    const lower = firstLine.toLowerCase();
    return ROSTER_COLS.filter((c) => lower.includes(c)).length >= 2;
  }
  composerAttachmentInput?.addEventListener("change", (e) => {
    const f = e.target.files?.[0];
    if (!f) {
      clearComposerAttachment();
      return;
    }
    const WARN_BYTES = 25 * 1024 * 1024;
    const MAX_BYTES = 100 * 1024 * 1024;
    if (f.size > MAX_BYTES) {
      alert(`File too large (${Math.round(f.size / 1024 / 1024)} MB). Maximum is 100 MB.`);
      clearComposerAttachment();
      return;
    }
    if (f.size > WARN_BYTES) {
      const ok = window.confirm(
        `This file is ${Math.round(f.size / 1024 / 1024)} MB \u2014 processing will take ~${Math.round(f.size / (1024 * 1024 * 2))} min in the background. Continue?`
      );
      if (!ok) {
        clearComposerAttachment();
        return;
      }
    }
    const isCsv = f.name.toLowerCase().endsWith(".csv") || f.type === "text/csv";
    if (isCsv) {
      const reader = new FileReader();
      reader.onload = (ev) => {
        const firstLine = (ev.target?.result || "").split(/\r?\n/)[0] || "";
        if (_looksLikeRosterCsv(firstLine)) {
          const chip = document.getElementById("composerAttachmentChip");
          if (chip) {
            let hint = chip.querySelector(".composer-attach-roster-hint");
            if (!hint) {
              hint = document.createElement("span");
              hint.className = "composer-attach-roster-hint";
              chip.appendChild(hint);
            }
            hint.textContent = "Looks like a roster \u2014 use Credentialing to reconcile.";
          }
        }
      };
      reader.readAsText(f.slice(0, 512));
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
  const FOREGROUND_CUTOFF_S = 12;
  let _ragProgressEs = null;
  let _ragProgressCutoffTimer = null;
  const _STAGE_MICROCOPY = {
    queued: "Queued\u2026",
    extracting: "Extracting pages\u2026",
    chunking: "Splitting into chunks\u2026",
    embedding: "Indexing\u2026",
    publishing: "Almost ready\u2026",
    ready: "Ready \u2713"
  };
  function _stageMicrocopy(stage, chunks_done, chunks_total) {
    if (stage === "chunking" && typeof chunks_done === "number" && typeof chunks_total === "number" && chunks_total > 0) {
      return `Chunking \xB7 ${chunks_done}/${chunks_total}`;
    }
    return _STAGE_MICROCOPY[stage] ?? stage;
  }
  function _closeRagProgressStrip() {
    if (_ragProgressEs) {
      _ragProgressEs.close();
      _ragProgressEs = null;
    }
    if (_ragProgressCutoffTimer !== null) {
      clearTimeout(_ragProgressCutoffTimer);
      _ragProgressCutoffTimer = null;
    }
    document.getElementById("ragProgressStrip")?.classList.add("rag-progress-strip--collapsed");
  }
  function _showHipaaDiagnosticsBubble(d) {
    const anchor = document.querySelector(".composer-wrap");
    if (!anchor || !anchor.parentElement)
      return;
    const bubble = document.createElement("div");
    const isBlocked = d.action_taken === "blocked_phi" || d.action_taken === "blocked_indeterminate";
    const isPrivate = d.action_taken === "published_private";
    bubble.className = "hipaa-diag-bubble" + (isBlocked && d.gate === "phi" ? " hipaa-diag-bubble--phi" : "") + (isBlocked && d.gate === "indeterminate" ? " hipaa-diag-bubble--indeterminate" : "") + (isPrivate ? " hipaa-diag-bubble--private" : "");
    const icon = document.createElement("span");
    icon.className = "hipaa-diag-icon";
    icon.setAttribute("aria-hidden", "true");
    icon.textContent = isBlocked && d.gate === "phi" ? "\u{1F6E1}\u2717" : isBlocked ? "\u26A0" : "\u{1F512}";
    const body = document.createElement("div");
    body.className = "hipaa-diag-body";
    const title = document.createElement("div");
    title.className = "hipaa-diag-title";
    if (isBlocked && d.gate === "phi") {
      title.textContent = `"${d.document_name}" contains PHI \u2014 not stored`;
    } else if (isBlocked) {
      title.textContent = `"${d.document_name}" couldn't be verified \u2014 not stored`;
    } else {
      title.textContent = `"${d.document_name}" stored in your private vault`;
    }
    body.appendChild(title);
    if (isBlocked) {
      const msg = document.createElement("div");
      msg.className = "hipaa-diag-msg";
      if (d.gate === "phi") {
        msg.textContent = "This document contains protected health information and cannot be processed in the current mode. It was not stored.";
      } else {
        msg.textContent = "We couldn't verify this document's safety right now. It was not stored. Please try again shortly.";
      }
      body.appendChild(msg);
    } else if (isPrivate) {
      const msg = document.createElement("div");
      msg.className = "hipaa-diag-msg";
      msg.textContent = "PHI found \u2014 stored privately (not shared to the corpus).";
      body.appendChild(msg);
    }
    const labels = d.identifier_labels.length ? d.identifier_labels : d.evidence_categories;
    if (labels.length > 0 && d.gate === "phi") {
      const pills = document.createElement("div");
      pills.className = "hipaa-diag-pills";
      labels.slice(0, 8).forEach((lbl) => {
        const pill = document.createElement("span");
        pill.className = "hipaa-diag-pill";
        pill.textContent = lbl;
        pills.appendChild(pill);
      });
      body.appendChild(pills);
    }
    const chrome2 = document.createElement("div");
    chrome2.className = "hipaa-diag-chrome";
    const gateBadge = document.createElement("span");
    gateBadge.className = `hipaa-diag-gate hipaa-diag-gate--${d.gate}`;
    gateBadge.textContent = `gate: ${d.gate}`;
    chrome2.appendChild(gateBadge);
    const modeBadge = document.createElement("span");
    modeBadge.className = "hipaa-diag-mode";
    modeBadge.textContent = `HIPAA mode: ${d.hipaa_mode_allowed ? "ON" : "OFF"}`;
    chrome2.appendChild(modeBadge);
    if (d.transaction_id) {
      const txn = document.createElement("span");
      txn.className = "hipaa-diag-txn";
      txn.textContent = `txn ${d.transaction_id.slice(0, 8)}`;
      chrome2.appendChild(txn);
    }
    body.appendChild(chrome2);
    const dismiss = document.createElement("button");
    dismiss.type = "button";
    dismiss.className = "hipaa-diag-dismiss";
    dismiss.setAttribute("aria-label", "Dismiss");
    dismiss.innerHTML = "&times;";
    dismiss.addEventListener("click", () => bubble.remove());
    bubble.appendChild(icon);
    bubble.appendChild(body);
    bubble.appendChild(dismiss);
    anchor.parentElement.insertBefore(bubble, anchor);
    if (!isBlocked)
      setTimeout(() => bubble.remove(), 3e4);
  }
  function _showPhiRecommendationCard(filename, documentId) {
    if (document.querySelector(".phi-rec-card"))
      return;
    const anchor = document.querySelector(".composer-wrap");
    if (!anchor?.parentElement)
      return;
    const card = document.createElement("div");
    card.className = "phi-rec-card phi-rec-card--checking";
    const label = document.createElement("span");
    label.className = "phi-rec-card__label";
    label.textContent = "Checking document sensitivity\u2026";
    const actions = document.createElement("span");
    actions.className = "phi-rec-card__actions";
    const dismissBtn = document.createElement("button");
    dismissBtn.type = "button";
    dismissBtn.className = "reminder-nudge-dismiss";
    dismissBtn.setAttribute("aria-label", "Dismiss");
    dismissBtn.innerHTML = "&times;";
    dismissBtn.addEventListener("click", () => card.remove());
    card.appendChild(label);
    card.appendChild(actions);
    card.appendChild(dismissBtn);
    anchor.parentElement.insertBefore(card, anchor);
    function _render(row) {
      const phiFlag = Boolean(row["phi_flag"]);
      const vis = String(row["suggested_visibility"] || "private");
      const evidence = row["phi_evidence"] || [];
      card.className = "phi-rec-card";
      if (phiFlag || vis === "private") {
        card.classList.add("phi-rec-card--phi");
        label.textContent = "\u26A0 Contains patient information \u2014 kept private.";
        if (evidence.length > 0) {
          const chips = document.createElement("span");
          chips.className = "phi-rec-card__chips";
          const seen = /* @__PURE__ */ new Set();
          for (const ev of evidence.slice(0, 6)) {
            const cat = String(ev.category || "").replace(/_/g, " ");
            if (!cat || seen.has(cat))
              continue;
            seen.add(cat);
            const chip = document.createElement("span");
            chip.className = "phi-rec-card__chip";
            chip.textContent = cat;
            chips.appendChild(chip);
          }
          card.insertBefore(chips, actions);
        }
        const keepBtn = document.createElement("button");
        keepBtn.type = "button";
        keepBtn.className = "phi-rec-card__action phi-rec-card__action--primary";
        keepBtn.textContent = "Keep private";
        keepBtn.addEventListener("click", () => card.remove());
        actions.appendChild(keepBtn);
      } else if (vis === "org") {
        card.classList.add("phi-rec-card--org");
        label.textContent = "\u{1F3E2} Shareable with your org.";
        const keepBtn = document.createElement("button");
        keepBtn.type = "button";
        keepBtn.className = "phi-rec-card__action phi-rec-card__action--secondary";
        keepBtn.textContent = "Keep private";
        keepBtn.addEventListener("click", () => card.remove());
        const shareBtn = document.createElement("button");
        shareBtn.type = "button";
        shareBtn.className = "phi-rec-card__action phi-rec-card__action--primary";
        shareBtn.textContent = "Share with org";
        shareBtn.setAttribute("disabled", "");
        shareBtn.title = "Coming soon";
        actions.appendChild(shareBtn);
        actions.appendChild(keepBtn);
      } else {
        card.classList.add("phi-rec-card--clean");
        label.textContent = "\u2713 No sensitive info found \u2014 safe to share.";
        const shareBtn = document.createElement("button");
        shareBtn.type = "button";
        shareBtn.className = "phi-rec-card__action phi-rec-card__action--primary";
        shareBtn.textContent = "Make public";
        shareBtn.setAttribute("disabled", "");
        shareBtn.title = "Coming soon \u2014 promote actions in P2";
        const keepBtn = document.createElement("button");
        keepBtn.type = "button";
        keepBtn.className = "phi-rec-card__action phi-rec-card__action--secondary";
        keepBtn.textContent = "Keep private";
        keepBtn.addEventListener("click", () => card.remove());
        actions.appendChild(shareBtn);
        actions.appendChild(keepBtn);
      }
      setTimeout(() => card.remove(), 6e4);
    }
    let attempts = 0;
    async function _poll() {
      attempts++;
      try {
        const resp = await apiFetch(`${API_BASE}/chat/uploads/${documentId}`);
        if (resp.ok) {
          const row = await resp.json();
          if (row["classified_at"]) {
            _render(row);
            return;
          }
        }
      } catch (_e) {
      }
      if (attempts >= 10) {
        card.remove();
        return;
      }
      setTimeout(() => {
        void _poll();
      }, 3e3);
    }
    setTimeout(() => {
      void _poll();
    }, 3e3);
  }
  function _showReadyNudge(filename, documentId, threadId) {
    if (document.querySelector(".rag-ready-nudge"))
      return;
    const anchor = document.querySelector(".composer-wrap");
    if (!anchor || !anchor.parentElement)
      return;
    const chip = document.createElement("div");
    chip.className = "reminder-nudge rag-ready-nudge";
    const label = document.createElement("span");
    label.className = "reminder-nudge-label";
    label.textContent = `\u{1F4C4} "${filename}" is ready`;
    const askBtn = document.createElement("button");
    askBtn.type = "button";
    askBtn.className = "reminder-nudge-view";
    askBtn.textContent = "Ask now";
    askBtn.addEventListener("click", () => {
      chip.remove();
      if (threadId && currentThreadId !== threadId) {
      }
      const inputEl2 = document.getElementById("input");
      if (inputEl2 && !inputEl2.value.trim()) {
        inputEl2.value = `Tell me about "${filename}"`;
        inputEl2.dispatchEvent(new Event("input"));
        inputEl2.focus();
      }
    });
    const dismissBtn = document.createElement("button");
    dismissBtn.type = "button";
    dismissBtn.className = "reminder-nudge-dismiss";
    dismissBtn.setAttribute("aria-label", "Dismiss");
    dismissBtn.innerHTML = "&times;";
    dismissBtn.addEventListener("click", () => chip.remove());
    chip.appendChild(label);
    chip.appendChild(askBtn);
    chip.appendChild(dismissBtn);
    anchor.parentElement.insertBefore(chip, anchor);
  }
  function _openRagProgressStrip(filename, progressChannel, documentId, threadId) {
    const strip = document.getElementById("ragProgressStrip");
    const bar = document.getElementById("ragProgressBar");
    const name = document.getElementById("ragProgressName");
    const stage = document.getElementById("ragProgressStage");
    const action = document.getElementById("ragProgressAction");
    const closeBtn = document.getElementById("ragProgressClose");
    if (!strip)
      return;
    if (bar) {
      bar.style.width = "0%";
      bar.className = "rag-progress-strip__bar";
    }
    if (name)
      name.textContent = `\u{1F4C4} ${filename}`;
    if (stage)
      stage.textContent = "Queued\u2026";
    if (action) {
      action.setAttribute("hidden", "");
      action.onclick = null;
    }
    strip.classList.remove("rag-progress-strip--collapsed");
    const _escape = (toBackground) => {
      _closeRagProgressStrip();
      if (toBackground)
        _showToast(`"${filename}" is processing \u2014 I'll let you know when it's ready`);
    };
    if (closeBtn) {
      closeBtn.onclick = () => _escape(true);
    }
    const es = new EventSource(API_BASE + progressChannel);
    _ragProgressEs = es;
    es.onmessage = (evt) => {
      try {
        const p = JSON.parse(evt.data);
        const pct = typeof p.pct === "number" ? Math.min(100, Math.max(0, p.pct)) : null;
        if (bar && pct !== null)
          bar.style.width = `${pct}%`;
        if (stage)
          stage.textContent = _stageMicrocopy(p.stage ?? "", p.chunks_done, p.chunks_total);
        if (!p.terminal)
          return;
        _ragProgressEs = null;
        es.close();
        if (_ragProgressCutoffTimer !== null) {
          clearTimeout(_ragProgressCutoffTimer);
          _ragProgressCutoffTimer = null;
        }
        if (p.stage === "ready") {
          if (bar) {
            bar.style.width = "100%";
            bar.classList.add("rag-progress-strip__bar--ready");
          }
          if (stage)
            stage.textContent = "Ready \u2713";
          if (action)
            action.setAttribute("hidden", "");
          window.setTimeout(() => {
            strip.classList.add("rag-progress-strip--collapsed");
            const inputEl2 = document.getElementById("input");
            if (inputEl2 && !inputEl2.value.trim()) {
              inputEl2.value = `Tell me about "${filename}"`;
              inputEl2.dispatchEvent(new Event("input"));
              inputEl2.focus();
            }
          }, 700);
        } else {
          if (bar)
            bar.classList.add("rag-progress-strip__bar--failed");
          if (stage)
            stage.textContent = p.error ? `Couldn't process \xB7 ${p.error}` : "Couldn't process";
          if (action) {
            action.removeAttribute("hidden");
            if (p.retryable !== false) {
              action.textContent = "Retry";
              action.onclick = async () => {
                action.setAttribute("hidden", "");
                if (stage)
                  stage.textContent = "Retrying\u2026";
                try {
                  const retryResp = await apiFetch(`${API_BASE}/documents/${documentId}/retry`, { method: "POST" });
                  if (!retryResp.ok)
                    throw new Error(`${retryResp.status}`);
                  const retryData = await retryResp.json();
                  const retryChannel = String(retryData.progress_channel || "");
                  if (retryChannel) {
                    _openRagProgressStrip(filename, retryChannel, documentId, threadId);
                  } else {
                    _showToast(`Retry queued for "${filename}" \u2014 I'll let you know when it's ready`);
                    _closeRagProgressStrip();
                  }
                } catch (_e) {
                  if (stage)
                    stage.textContent = "Retry failed \u2014 try again";
                  action.removeAttribute("hidden");
                }
              };
            } else {
              action.textContent = "Remove";
              action.onclick = () => {
                _closeRagProgressStrip();
              };
            }
          }
        }
      } catch (_e) {
      }
    };
    es.onerror = () => {
      _closeRagProgressStrip();
      _showToast(`"${filename}" is processing \u2014 I'll let you know when it's ready`);
    };
    _ragProgressCutoffTimer = window.setTimeout(() => {
      if (_ragProgressEs)
        _escape(false);
    }, FOREGROUND_CUTOFF_S * 1e3);
  }
  let composerUploadPhaseTimers = [];
  function stopComposerUploadPhaseEmits() {
    composerUploadPhaseTimers.forEach((id) => window.clearTimeout(id));
    composerUploadPhaseTimers = [];
    hideChatStatusBanner();
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
      if (currentThreadId)
        formData.append("thread_id", currentThreadId);
      const resp = await apiFetch(API_BASE + "/chat/upload", {
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
      window.__mobiusChatThreadId = currentThreadId;
      const chunks = typeof data.chunks_count === "number" ? data.chunks_count : 0;
      if (chunks > 0) {
        console.debug(`[composer-attach] "${filename}" ingested as ${chunks} chunk${chunks === 1 ? "" : "s"}`);
      }
      const etaSecs = Number(data.estimated_seconds) || 0;
      const etaMin = Number(data.eta_minutes) || 0;
      const pageCount = Number(data.page_count) || 0;
      const redirectUrl = String(data.redirect_url || "");
      const progressChannel = String(data.progress_channel || "");
      const uploadedDocId = String(data.document_id || "");
      const uploadedThreadId = String(data.thread_id || currentThreadId || "");
      const uxPath = String(data.ux_path || "blocking");
      const hipaaD = data.hipaa_diagnostics;
      if (uxPath === "blocked" || data.status === "blocked") {
        _showHipaaDiagnosticsBubble(hipaaD ?? {
          gate: data.gate || "indeterminate",
          phi_flag: true,
          evidence_categories: [],
          identifier_labels: [],
          hipaa_mode_allowed: false,
          action_taken: data.action_taken || "blocked_indeterminate",
          reason: "",
          transaction_id: "",
          document_name: filename
        });
        return data;
      }
      if (uploadedDocId && !redirectUrl && !hipaaD) {
        _showPhiRecommendationCard(filename, uploadedDocId);
      } else if (hipaaD && hipaaD.action_taken === "published_private") {
        _showHipaaDiagnosticsBubble(hipaaD);
        _pendingHipaaDiagnostics = hipaaD;
      } else if (hipaaD && hipaaD.gate === "clean") {
        showChatStatusBanner(`\u2713 "${filename}" screened \u2014 no PHI detected.`, 4e3);
        _pendingHipaaDiagnostics = hipaaD;
      }
      if (uxPath === "duplicate") {
        showChatStatusBanner(`\u2713 "${filename}" is ready \u2014 already in our corpus.`, 5e3);
      } else if (redirectUrl) {
        const sub = pageCount ? `${pageCount}-page document \u2014 ~${etaMin} min` : `~${etaMin} min`;
        showChatStatusBanner(
          `"${filename}" is large (${sub}). Open Mobius RAG \u2192 <a href="${redirectUrl}" target="_blank" rel="noopener">${redirectUrl}</a>`,
          2e4
        );
      } else if (progressChannel) {
        stopComposerUploadPhaseEmits();
        _openRagProgressStrip(filename, progressChannel, uploadedDocId, uploadedThreadId);
      } else if (!uploadedDocId) {
        _showToast(`"${filename}" is processing \u2014 I'll let you know when it's ready`);
      }
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
      window.__mobiusChatThreadId = currentThreadId;
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
    uploadFile?.addEventListener("change", () => {
      updateSubmitState();
      const f = uploadFile?.files?.[0];
      const rosterHint = document.getElementById("uploadRosterHint");
      if (!rosterHint)
        return;
      if (!f) {
        rosterHint.hidden = true;
        rosterHint.textContent = "";
        return;
      }
      const isCsv = f.name.toLowerCase().endsWith(".csv") || f.type === "text/csv";
      if (!isCsv) {
        rosterHint.hidden = true;
        rosterHint.textContent = "";
        return;
      }
      const reader = new FileReader();
      reader.onload = (ev) => {
        const firstLine = (ev.target?.result || "").split(/\r?\n/)[0] || "";
        if (_looksLikeRosterCsv(firstLine)) {
          rosterHint.textContent = "This looks like a roster file. To reconcile providers, use the Credentialing module instead.";
          rosterHint.hidden = false;
        } else {
          rosterHint.hidden = true;
          rosterHint.textContent = "";
        }
      };
      reader.readAsText(f.slice(0, 512));
    });
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
      if (currentThreadId)
        formData.append("thread_id", currentThreadId);
      uploadAbort = new AbortController();
      const signal = uploadAbort.signal;
      apiFetch(API_BASE + "/chat/upload", { method: "POST", body: formData, signal }).then((r) => {
        if (!r.ok)
          return r.json().then((d) => Promise.reject(d?.detail ?? r.statusText));
        return r.json();
      }).then(
        (data) => {
          const org = data.org_name ?? orgName;
          if (data.thread_id)
            currentThreadId = data.thread_id;
          window.__mobiusChatThreadId = currentThreadId;
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
      window.__mobiusChatThreadId = currentThreadId;
      hideChatStatusBanner();
      hideRosterUploadReceipt();
      messagesEl.querySelectorAll(".chat-turn").forEach((n) => n.remove());
      if (chatEmpty)
        chatEmpty.classList.remove("hidden");
      loadSidebarHistory();
    });
  }
  async function loadAndRenderThread(threadId) {
    const tid = (threadId || "").trim();
    if (!tid)
      return;
    let turns;
    try {
      const r = await fetch(
        API_BASE + "/chat/history/threads/" + encodeURIComponent(tid) + "/turns?limit=50",
        { headers: await auth.getAuthHeader?.() ?? {} }
      );
      if (!r.ok) {
        console.warn("[loadAndRenderThread] HTTP", r.status, "for", tid);
        _showToast(`Couldn't load thread (HTTP ${r.status}). Please retry.`);
        return;
      }
      turns = await r.json();
    } catch (err) {
      console.warn("[loadAndRenderThread] fetch failed:", err);
      _showToast("Couldn't load thread. Check your connection and retry.");
      return;
    }
    if (!Array.isArray(turns)) {
      console.warn("[loadAndRenderThread] non-array response", typeof turns);
      _showToast("Thread response was unexpected. Please retry.");
      return;
    }
    currentThreadId = tid;
    window.__mobiusChatThreadId = currentThreadId;
    if (chatEmpty)
      chatEmpty.classList.add("hidden");
    messagesEl.querySelectorAll(".chat-turn").forEach((n) => n.remove());
    hideChatStatusBanner();
    hideRosterUploadReceipt();
    for (const turn of turns) {
      const turnWrap = document.createElement("div");
      turnWrap.className = "chat-turn";
      turnWrap.appendChild(renderUserMessage(turn.question || "", void 0));
      if (Array.isArray(turn.thinking_log) && turn.thinking_log.length > 0) {
        const lines = [];
        for (const entry of turn.thinking_log) {
          if (typeof entry === "string") {
            const s = entry.trim();
            if (s)
              lines.push(s);
          } else if (entry && typeof entry === "object") {
            const e = entry;
            const msg = typeof e.message === "string" ? e.message : typeof e.line === "string" ? e.line : "";
            if (msg && msg.trim()) {
              lines.push(msg.trim());
            } else {
              try {
                lines.push(JSON.stringify(entry).slice(0, 200));
              } catch {
              }
            }
          }
        }
        if (lines.length > 0) {
          const tb = renderThinkingBlock(lines);
          try {
            tb.done(lines.length);
          } catch {
          }
          turnWrap.appendChild(tb.el);
        }
      }
      const finalBody = turn.final_message || "";
      if (finalBody.trim()) {
        turnWrap.appendChild(
          renderAssistantContent(finalBody, false, {
            onFollowupClick: (q) => sendMessage(q),
            sourceConfidenceStrip: turn.source_confidence_strip || void 0
          })
        );
      }
      if (Array.isArray(turn.sources) && turn.sources.length > 0) {
        const sourceList = turn.sources.map((s) => ({
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
        }));
        turnWrap.appendChild(
          renderSourceCiter(sourceList, [], turn.correlation_id)
        );
      }
      if (turn.correlation_id) {
        turnWrap.appendChild(renderFeedback(turn.correlation_id));
      }
      messagesEl.appendChild(turnWrap);
    }
    scrollToBottom(messagesEl);
    try {
      inputEl.focus();
    } catch {
    }
  }
  function openVaultPanel(tab) {
    const w = window;
    if (typeof w.MobiusVault?.open === "function") {
      const opts = {};
      if (tab)
        opts.tab = tab;
      if (currentThreadId)
        opts.currentThreadId = currentThreadId;
      w.MobiusVault.open(Object.keys(opts).length ? opts : void 0);
    } else {
      window.open("/vault", "_blank", "noopener");
    }
  }
  let _vaultActiveTab = "recent";
  function initVaultBlock() {
    const vaultBlock = document.getElementById("sidebarVaultBlock");
    if (!vaultBlock)
      return;
    document.getElementById("vaultOpenBtn")?.addEventListener("click", () => openVaultPanel(_vaultActiveTab));
    document.getElementById("vaultManageBtn")?.addEventListener("click", () => openVaultPanel("recent"));
    document.getElementById("vaultRailBtn")?.addEventListener("click", () => {
      const sidebar2 = document.getElementById("sidebar");
      if (sidebar2?.classList.contains("sidebar--collapsed")) {
        document.getElementById("sidebarChevron")?.click();
      }
      vaultBlock.scrollIntoView({ behavior: "smooth", block: "nearest" });
    });
    vaultBlock.querySelectorAll(".vault-tab").forEach((btn) => {
      btn.addEventListener("click", () => {
        const tab = btn.dataset.vaultTab || "recent";
        vaultBlock.querySelectorAll(".vault-tab").forEach((t) => {
          t.classList.toggle("vault-tab--active", t === btn);
          t.setAttribute("aria-selected", t === btn ? "true" : "false");
        });
        _vaultActiveTab = tab;
        void loadVaultTab(tab);
      });
    });
    void loadVaultCounts();
    void loadVaultTab("recent");
  }
  async function loadVaultCounts() {
    const _authHeaders = await auth.getAuthHeader?.() ?? {};
    const [threads, liked, tasksResp, uploadsResp] = await Promise.allSettled([
      fetch(API_BASE + "/chat/history/threads?limit=1", { headers: _authHeaders }).then((r) => r.json()),
      fetch(API_BASE + "/chat/history/most-helpful-searches?limit=1", { headers: _authHeaders }).then((r) => r.json()),
      fetch(API_BASE + "/chat/tasks?limit=1&assigned_to=user:me", { headers: _authHeaders }).then((r) => r.json()),
      fetch(API_BASE + "/chat/uploads?limit=1", { headers: _authHeaders }).then((r) => r.json())
    ]);
  }
  async function loadVaultTab(tab) {
    const list = document.getElementById("vaultItemList");
    if (!list)
      return;
    const _authHeaders = await auth.getAuthHeader?.() ?? {};
    const snippet = (s, max = 72) => (s ?? "").trim().slice(0, max) + ((s ?? "").length > max ? "\u2026" : "");
    const setCount = (id, n) => {
      const el2 = document.getElementById(id);
      if (el2)
        el2.textContent = n != null ? ` ${n}` : "";
    };
    list.innerHTML = `<li class="vault-item vault-item--muted">Loading\u2026</li>`;
    try {
      if (tab === "recent") {
        const threads = await fetch(API_BASE + "/chat/history/threads?limit=20", { headers: _authHeaders }).then((r) => r.json());
        setCount("vaultCountRecent", threads.length);
        list.innerHTML = "";
        if (!threads.length) {
          list.innerHTML = `<li class="vault-item vault-item--muted">No recent chats yet</li>`;
          return;
        }
        for (const th of threads) {
          const li = document.createElement("li");
          li.className = "vault-item";
          li.textContent = snippet(th.summary && th.summary.trim() || th.title || "Untitled chat");
          li.title = th.summary || th.title || "";
          li.setAttribute("role", "button");
          li.setAttribute("tabindex", "0");
          li.addEventListener("click", () => void loadAndRenderThread(th.thread_id));
          li.addEventListener("keydown", (e) => {
            if (e.key === "Enter" || e.key === " ") {
              e.preventDefault();
              void loadAndRenderThread(th.thread_id);
            }
          });
          list.appendChild(li);
        }
      } else if (tab === "liked") {
        const liked = await fetch(API_BASE + "/chat/history/most-helpful-searches?limit=20", { headers: _authHeaders }).then((r) => r.json());
        setCount("vaultCountLiked", liked.length);
        list.innerHTML = "";
        if (!liked.length) {
          list.innerHTML = `<li class="vault-item vault-item--muted">No liked answers yet \u2014 thumb up a helpful response</li>`;
          return;
        }
        for (const t of liked) {
          const li = document.createElement("li");
          li.className = "vault-item";
          li.textContent = snippet(t.question || "(empty)");
          li.title = t.question || "";
          li.setAttribute("role", "button");
          li.setAttribute("tabindex", "0");
          const tid = (t.thread_id || "").trim();
          li.addEventListener("click", () => {
            if (tid)
              void loadAndRenderThread(tid);
            else {
              inputEl.value = t.question ?? "";
              updateSendState();
              sendMessage();
            }
          });
          list.appendChild(li);
        }
      } else if (tab === "tasks") {
        const data = await fetch(API_BASE + "/chat/tasks?limit=20", { headers: _authHeaders }).then((r) => r.json());
        const tasks = data.tasks || [];
        const open = tasks.filter((t) => t.status !== "completed" && t.status !== "closed");
        setCount("vaultCountTasks", open.length || null);
        list.innerHTML = "";
        if (!open.length) {
          list.innerHTML = `<li class="vault-item vault-item--muted">No open tasks</li>`;
          return;
        }
        for (const t of open) {
          const li = document.createElement("li");
          li.className = "vault-item";
          li.textContent = snippet(t.title || t.kind || "Task");
          li.title = t.title || "";
          list.appendChild(li);
        }
      } else if (tab === "uploads") {
        const data = await fetch(API_BASE + "/chat/uploads?limit=20", { headers: _authHeaders }).then((r) => r.json());
        const uploads = data.uploads || [];
        setCount("vaultCountUploads", uploads.length || null);
        list.innerHTML = "";
        if (!uploads.length) {
          list.innerHTML = `<li class="vault-item vault-item--muted">No uploads yet</li>`;
          return;
        }
        for (const u of uploads) {
          const li = document.createElement("li");
          li.className = "vault-item";
          li.textContent = snippet(u.filename || u.document_id);
          li.title = u.filename || u.document_id;
          list.appendChild(li);
        }
      }
    } catch {
      list.innerHTML = `<li class="vault-item vault-item--muted">Failed to load \u2014 try again</li>`;
    }
  }
  function loadSidebarHistory() {
    void loadVaultTab(_vaultActiveTab);
  }
  function _loadSidebarHistoryFull() {
    const recentList = document.getElementById("recentList");
    const helpfulList = document.getElementById("helpfulList");
    const documentsList = document.getElementById("documentsList");
    if (!recentList)
      return;
    const snippet = (q, max = 80) => (q ?? "").trim().slice(0, max) + ((q ?? "").length > max ? "\u2026" : "");
    void (async () => {
      const _authHeaders = await auth.getAuthHeader?.() ?? {};
      Promise.all([
        // Phase 2.3: sidebar now shows deduplicated *threads* with real titles
        // instead of per-turn rows that exposed raw URLs / tool inputs. Endpoint
        // returns {thread_id, title, updated_at, turn_count}. Gracefully returns
        // [] if migration 030 hasn't run, so the list is empty rather than broken.
        // Auth header required — history is user-scoped (fix 2026-05-06).
        fetch(API_BASE + "/chat/history/threads?limit=20", { headers: _authHeaders }).then(
          (r) => r.json()
        ),
        helpfulList ? fetch(API_BASE + "/chat/history/most-helpful-searches?limit=10", { headers: _authHeaders }).then(
          (r) => r.json()
        ) : Promise.resolve([]),
        documentsList ? fetch(API_BASE + "/chat/history/most-helpful-documents?limit=10", { headers: _authHeaders }).then(
          (r) => r.json()
        ) : Promise.resolve([])
      ]).then(([recentThreads, helpful, documents]) => {
        recentList.innerHTML = "";
        for (const th of recentThreads) {
          const li = document.createElement("li");
          li.className = "recent-item";
          const label = th.summary && th.summary.trim() || th.title || "Untitled chat";
          const countSuffix = th.turn_count > 1 ? `  (${th.turn_count})` : "";
          li.textContent = snippet(label) + countSuffix;
          li.title = label;
          li.setAttribute("role", "button");
          li.setAttribute("tabindex", "0");
          li.setAttribute("data-thread-id", th.thread_id);
          li.addEventListener("click", () => {
            void loadAndRenderThread(th.thread_id);
          });
          li.addEventListener("keydown", (e) => {
            if (e.key === "Enter" || e.key === " ") {
              e.preventDefault();
              void loadAndRenderThread(th.thread_id);
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
            const tid = (t.thread_id || "").trim();
            const openOrReSubmit = () => {
              if (tid) {
                void loadAndRenderThread(tid);
              } else {
                inputEl.value = t.question ?? "";
                updateSendState();
                sendMessage();
              }
            };
            li.addEventListener("click", openOrReSubmit);
            li.addEventListener("keydown", (e) => {
              if (e.key === "Enter" || e.key === " ") {
                e.preventDefault();
                openOrReSubmit();
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
    })();
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
    const pThread = u.searchParams.get("thread")?.trim();
    if (pq) {
      u.searchParams.delete("q");
      const next = u.pathname + (u.search ? u.search : "") + u.hash;
      window.history.replaceState({}, "", next);
      inputEl.value = pq;
      updateSendState();
      sendMessage();
    } else if (pThread) {
      u.searchParams.delete("thread");
      const next = u.pathname + (u.search ? u.search : "") + u.hash;
      window.history.replaceState({}, "", next);
      void loadAndRenderThread(pThread);
    }
  } catch {
  }
  initVaultBlock();
  window.addEventListener("mobiusFeedbackUp", () => {
    if (_vaultActiveTab === "liked")
      void loadVaultTab("liked");
    void (async () => {
      const el2 = document.getElementById("vaultCountRecent");
      if (el2) {
      }
    })();
  });
  updateSendState();
  (function setupSkillsModal() {
    const overlay = document.getElementById("skillsOverlay");
    const modal2 = document.getElementById("skillsModal");
    const modalBody = document.getElementById("skillsModalBody");
    const sidebarTilesContainer = document.getElementById("suiteTilesContainer");
    const learnMoreBtn = document.getElementById("suiteLearnMore");
    const SUITE_TILES = [
      {
        // 2026-05-05: strategy agent (mobius-story-ui) is now deployed
        // and reachable. Removed comingSoon so the sidebar tile + skills
        // modal can open it in a new tab. Backend URL configurable via
        // MOBIUS_STRATEGY_URL env (window-injected) — fallback points at
        // the dev Cloud Run service.
        key: "strategy",
        label: "Strategy",
        tagline: "Benchmarking + KPIs",
        accent: "indigo",
        urlEnvKey: "MOBIUS_STRATEGY_URL",
        fallbackUrl: "https://mobius-story-ui-ortabkknqa-uc.a.run.app"
      },
      {
        key: "roster",
        label: "Roster",
        tagline: "Provider directory + credentialing",
        accent: "emerald",
        urlEnvKey: "MOBIUS_CREDENTIALING_URL",
        fallbackUrl: "https://mobius-provider-roster-credentialing-ortabkknqa-uc.a.run.app/index.html"
      },
      {
        key: "library",
        label: "Public Library",
        tagline: "Shared corpus \u2014 payer manuals, regs, public sources",
        accent: "accent",
        urlEnvKey: "MOBIUS_LIBRARY_URL",
        fallbackUrl: "https://mobius-rag-ortabkknqa-uc.a.run.app"
      },
      {
        key: "platform",
        label: "Platform",
        tagline: "Architecture schematic",
        accent: "violet",
        urlEnvKey: "MOBIUS_PLATFORM_URL",
        fallbackUrl: "/platform"
      }
      // Vault is now the sidebar block above this section; not a tile.
    ];
    function tileUrl(t) {
      const winAny = window;
      const fromEnv = winAny[t.urlEnvKey] || "";
      let url = fromEnv && fromEnv.trim() ? fromEnv.trim() : t.fallbackUrl;
      try {
        const tok = localStorage.getItem("mobius.auth.accessToken");
        if (tok && /(^|\/\/)([^/]*\.)?(run\.app|localhost|127\.0\.0\.1)/.test(url)) {
          url += (url.includes("#") ? "&" : "#") + "t=" + encodeURIComponent(tok);
        }
      } catch {
      }
      return url;
    }
    const CHAT_THEMES = [
      {
        title: "Healthcare lookup",
        tagline: "Codes, NPIs, payer policies",
        description: "Look up procedure and diagnosis codes, verify NPI registry entries, and pull authoritative payer documents from your corpus \u2014 all with source citations you can defend.",
        examplePrompt: "What's Sunshine Health's prior authorization timeline for H0036?",
        selected: true
      },
      {
        title: "External search",
        tagline: "Search beyond your library",
        description: "When the answer isn't in your corpus yet, Mobius searches the web, reads specific pages, and can permanently add authoritative sources to your library \u2014 so the next person asking gets an indexed answer.",
        examplePrompt: "Find Sunshine's dental plan transition dates and add the page to our library",
        selected: true
      },
      {
        title: "Document chat",
        tagline: "Ask about a file you uploaded",
        description: "Upload a denial letter, provider manual, or policy PDF and ask questions about it directly. Mobius keeps it on the thread and searches inside it alongside the broader corpus.",
        examplePrompt: "What does the attached denial letter say about timely filing?",
        selected: true
      },
      {
        title: "Task management",
        tagline: "Make conversations actionable",
        description: "Convert answers into letters, emails, or memos. Track follow-up tasks. Reshape a prior answer without re-running the whole research process.",
        examplePrompt: "Convert this to an appeal letter for Sunshine Health",
        selected: true
      },
      {
        title: "PHI guardrail",
        tagline: "Refuses questions about specific patients",
        description: "Mobius will not answer questions tied to specific named patients, MRNs, or identifying combinations. The refusal happens up-front \u2014 before any retrieval or model call \u2014 and is consistent across every model the bandit might pick.",
        examplePrompt: "(Mobius will refuse questions like 'Has patient John Doe had his colonoscopy approved?')",
        selected: true
      }
    ];
    const COMING_SOON = [
      {
        title: "Denial management",
        tagline: "Build defendable appeals end-to-end",
        description: `Intake the denial, retrieve the contract and regulatory rules that apply, construct the argument, run a counterpoint check ("what's the payer's likely rebuttal?"), and assemble the submission packet \u2014 letter, form, supporting documents, timeline.`
      }
    ];
    function escapeHtml5(s) {
      return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&#39;");
    }
    function renderSidebarSuiteTiles() {
      if (!sidebarTilesContainer)
        return;
      sidebarTilesContainer.innerHTML = "";
      for (const t of SUITE_TILES) {
        const btn = document.createElement("button");
        btn.type = "button";
        const baseCls = `suite-tile suite-tile--${t.accent}`;
        btn.className = t.comingSoon ? `${baseCls} suite-tile--coming-soon` : baseCls;
        btn.setAttribute("aria-label", t.comingSoon ? `${t.label} (coming soon)` : `Open ${t.label}`);
        btn.dataset.tourId = `sidebar-suite-${t.key}`;
        if (t.comingSoon) {
          btn.disabled = true;
          btn.setAttribute("aria-disabled", "true");
          btn.title = "Coming soon";
        }
        const arrowOrBadge = t.comingSoon ? `<span class="suite-tile-coming-soon" aria-hidden="true">Coming soon</span>` : `<span class="suite-tile-arrow" aria-hidden="true">\u2197</span>`;
        btn.innerHTML = `<span class="suite-tile-label">${escapeHtml5(t.label)}</span><span class="suite-tile-tagline">${escapeHtml5(t.tagline)}</span>` + arrowOrBadge;
        if (!t.comingSoon) {
          btn.addEventListener("click", () => {
            const url = tileUrl(t);
            window.open(url, "_blank", "noopener");
          });
        }
        sidebarTilesContainer.appendChild(btn);
      }
    }
    const SUITE_LONG_DESC = {
      strategy: "Benchmarks your organization against peer CMHCs on revenue, denials, panel mix, and credentialing throughput. Pulls from our public payer + DOGE rate datasets and overlays your roster to show where you sit on each KPI. Useful when board / leadership asks 'how do we compare?'.",
      roster: "Single source of truth for your provider directory + the credentialing pipeline. Tracks who's enrolled with which payer, what's pending, what's expired, and surfaces re-credentialing windows before they lapse. Roster reconciliation, NPI verification, and run-by-run credentialing reports all live here.",
      library: "The shared corpus \u2014 payer manuals, state Medicaid handbooks, federal regs, public CMS guidance. Anything anyone uploads as a public source becomes searchable across every chat (with source citation). Mobius retrieves from this library automatically when you ask a payer / policy / regulatory question.",
      vault: "Your private workspace \u2014 recent chats, liked answers, open tasks, and uploaded documents. Use the My Vault block in the sidebar to browse, or click '\u2922 Open' to launch the full Vault panel."
    };
    function renderSkillsModal() {
      if (!modalBody)
        return;
      const html = [
        // Universal capabilities \u2014 baked into every chat
        '<div class="skills-section">',
        '<div class="skills-section-head">',
        '<span class="skills-section-eyebrow">Always on \u2014 baked into every chat</span>',
        '<span class="skills-section-hint">These five capabilities run in every turn. Mobius picks the right ones automatically based on your question.</span>',
        "</div>",
        '<div class="skills-themes-grid">',
        ...CHAT_THEMES.map(
          (t) => `<article class="skills-theme"><header class="skills-theme-head"><h3 class="skills-theme-title">${escapeHtml5(t.title)}</h3><p class="skills-theme-tagline">${escapeHtml5(t.tagline)}</p></header><p class="skills-theme-desc">${escapeHtml5(t.description)}</p><p class="skills-theme-example"><span class="skills-theme-example-label">Try:</span> \u201C${escapeHtml5(t.examplePrompt)}\u201D</p></article>`
        ),
        "</div>",
        "</div>",
        // Mobius modules \u2014 open-in-tab today, with descriptions
        '<div class="skills-section">',
        '<div class="skills-section-head">',
        '<span class="skills-section-eyebrow">Mobius modules</span>',
        '<span class="skills-section-hint">Standalone workspaces that complement chat. Open in a new tab today; deeper chat integration on the roadmap.</span>',
        "</div>",
        '<div class="skills-standalone-grid">',
        ...SUITE_TILES.map(
          (t) => `<article class="skills-standalone skills-standalone--${t.accent}${t.comingSoon ? " skills-standalone--coming-soon" : ""}"><h3 class="skills-standalone-title">${escapeHtml5(t.label)}</h3><p class="skills-standalone-tagline">${escapeHtml5(t.tagline)}</p>` + (SUITE_LONG_DESC[t.key] ? `<p class="skills-standalone-desc">${escapeHtml5(SUITE_LONG_DESC[t.key])}</p>` : "") + (t.comingSoon ? '<span class="skills-standalone-badge">Coming soon</span>' : `<button type="button" class="skills-standalone-open" data-suite-key="${escapeHtml5(t.key)}">Open ${escapeHtml5(t.label)} \u2197</button>`) + "</article>"
        ),
        "</div>",
        "</div>",
        // Coming soon
        '<div class="skills-section">',
        '<div class="skills-section-head">',
        '<span class="skills-section-eyebrow">Coming soon</span>',
        "</div>",
        '<div class="skills-coming-grid">',
        ...COMING_SOON.map(
          (c) => `<article class="skills-coming"><h3 class="skills-coming-title">${escapeHtml5(c.title)}</h3><p class="skills-coming-tagline">${escapeHtml5(c.tagline)}</p><p class="skills-coming-desc">${escapeHtml5(c.description)}</p></article>`
        ),
        "</div>",
        "</div>",
        // Trust footer
        '<div class="skills-trust">',
        '<span class="skills-trust-eyebrow">How Mobius protects you</span>',
        '<ul class="skills-trust-list">',
        "<li>Cached answers for repeated lookups \u2014 fast when it matters</li>",
        "<li>Hard refuse on questions about specific patients</li>",
        "<li>Every claim cited to its source</li>",
        "</ul>",
        "</div>"
      ].join("");
      modalBody.innerHTML = html;
      modalBody.querySelectorAll("[data-suite-key]").forEach((btn) => {
        btn.addEventListener("click", () => {
          const key = btn.getAttribute("data-suite-key") || "";
          const tile = SUITE_TILES.find((t) => t.key === key);
          if (!tile)
            return;
          closeSkillsModal();
          const url = tileUrl(tile);
          window.open(url, "_blank", "noopener");
        });
      });
    }
    function openSkillsModal() {
      overlay?.removeAttribute("hidden");
      modal2?.removeAttribute("hidden");
    }
    function closeSkillsModal() {
      overlay?.setAttribute("hidden", "");
      modal2?.setAttribute("hidden", "");
    }
    renderSidebarSuiteTiles();
    renderSkillsModal();
    learnMoreBtn?.addEventListener("click", openSkillsModal);
    document.getElementById("skillsModalClose")?.addEventListener("click", closeSkillsModal);
    overlay?.addEventListener("click", closeSkillsModal);
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && !modal2?.hasAttribute("hidden"))
        closeSkillsModal();
    });
    function _wireLegacySuiteButton(btnId, tileKey) {
      const el2 = document.getElementById(btnId);
      if (!el2)
        return;
      const t = SUITE_TILES.find((x) => x.key === tileKey);
      if (t?.comingSoon) {
        el2.disabled = true;
        el2.classList.add("skill-sidebar-item--coming-soon");
        el2.title = "Coming soon";
        el2.setAttribute("aria-disabled", "true");
        if (!el2.querySelector(".skill-sidebar-coming-soon")) {
          const badge = document.createElement("span");
          badge.className = "skill-sidebar-coming-soon";
          badge.textContent = "Coming soon";
          el2.appendChild(badge);
        }
        return;
      }
      el2.addEventListener("click", () => {
        if (!t)
          return;
        closeSkillsModal();
        const url = tileUrl(t);
        window.open(url, "_blank", "noopener");
      });
    }
    _wireLegacySuiteButton("btnOpenSkillPipeline", "roster");
    _wireLegacySuiteButton("btnOpenFinancialStrategy", "strategy");
    _wireLegacySuiteButton("btnOpenRoster", "roster");
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
    const rosterUrl = base + "/roster" + (lastOrg ? "?org=" + encodeURIComponent(lastOrg) : "");
    openRosterPanel(rosterUrl);
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
