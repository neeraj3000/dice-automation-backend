// Apply2Hire - Content Bridge Script
// Injected into Apply2Hire pages (localhost / 127.0.0.1 / apply2hire.com)
// Continuously bridges authentication status to extension storage & background service

(function () {
  function isAuthPage() {
    const path = (window.location.pathname || "").toLowerCase();
    return (
      path.includes("/login") ||
      path.includes("/signin") ||
      path.includes("/register") ||
      path.includes("/signup")
    );
  }

  function getLocalAuth() {
    // If the browser is on the login/signup page, the user is explicitly NOT authenticated
    if (isAuthPage()) {
      return { user: null, token: null, isLoggedOut: true };
    }

    try {
      let user = null;
      let token = null;

      const rawUser = localStorage.getItem("a2h_user");
      if (rawUser) {
        try {
          user = JSON.parse(rawUser);
        } catch (_) {}
      }

      token = localStorage.getItem("a2h_token");

      // Check user in sessionStorage
      if (!user) {
        const rawSsUser = sessionStorage.getItem("a2h_user");
        if (rawSsUser) {
          try {
            user = JSON.parse(rawSsUser);
          } catch (_) {}
        }
      }
      if (!token) {
        token = sessionStorage.getItem("a2h_token");
      }

      // Check Redux or generic auth keys in localStorage
      if (!token || !user) {
        for (let i = 0; i < localStorage.length; i++) {
          const k = localStorage.key(i);
          if (!k) continue;
          const val = localStorage.getItem(k);
          if (typeof val !== "string") continue;
          if (!token && val.startsWith("eyJ") && val.split(".").length === 3) {
            token = val;
          }
          if (!user && (k.toLowerCase().includes("user") || k.toLowerCase().includes("auth"))) {
            try {
              const parsed = JSON.parse(val);
              if (parsed && (parsed.email || parsed.name || (parsed.user && parsed.user.email))) {
                user = parsed.user || parsed;
              }
            } catch (_) {}
          }
        }
      }

      // Never infer login from "Welcome back" on the DOM
      return { user, token, isLoggedOut: false };
    } catch (e) {
      return { user: null, token: null, isLoggedOut: false };
    }
  }

  function broadcastAuthToExtension() {
    if (isAuthPage()) {
      // Actively clear cached auth when visiting /login
      if (typeof chrome !== "undefined" && chrome.runtime && chrome.runtime.sendMessage) {
        try {
          chrome.runtime.sendMessage({ action: "CLEAR_APP_AUTH" }, () => {
            if (chrome.runtime.lastError) {}
          });
        } catch (_) {}
      }
      return;
    }

    const auth = getLocalAuth();
    if (auth.user && (auth.user.email || auth.user.name)) {
      if (typeof chrome !== "undefined" && chrome.runtime && chrome.runtime.sendMessage) {
        try {
          chrome.runtime.sendMessage({
            action: "SYNC_APP_AUTH",
            user: auth.user,
            token: auth.token || "session_token"
          }, () => {
            if (chrome.runtime.lastError) {}
          });
        } catch (_) {}
      }
    } else {
      // Non-auth page with no credentials present in storage -> clear
      if (typeof chrome !== "undefined" && chrome.runtime && chrome.runtime.sendMessage) {
        try {
          chrome.runtime.sendMessage({ action: "CLEAR_APP_AUTH" }, () => {
            if (chrome.runtime.lastError) {}
          });
        } catch (_) {}
      }
    }
  }

  // 1. Initial sync on page load / script injection
  broadcastAuthToExtension();

  // 2. Listen to custom window events from React frontend with strict origin & payload validation
  const TRUSTED_ORIGINS = new Set([
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "https://apply2hire.com",
    "https://www.apply2hire.com",
    "https://app.apply2hire.com",
    window.location.origin
  ]);

  window.addEventListener("message", (event) => {
    // 1. Verify message source is the local window
    if (event.source !== window) return;

    // 2. Verify origin matches trusted application allowlist
    if (!event.origin || !TRUSTED_ORIGINS.has(event.origin)) {
      return;
    }

    // 3. Verify event data structure and avoid oversized payloads
    if (!event.data || typeof event.data !== "object") return;

    const { type, user, token } = event.data;

    if (type === "APPLY2HIRE_AUTH_SYNC") {
      // Validate user object schema and token
      if (!user || typeof user !== "object") return;
      const email = typeof user.email === "string" ? user.email.trim() : "";
      const id = typeof user.id === "string" ? user.id.trim() : (typeof user._id === "string" ? user._id.trim() : "");
      if (!email && !id) return;

      const safeUser = {
        id: id || undefined,
        email: email || undefined,
        name: typeof user.name === "string" ? user.name.slice(0, 100) : ""
      };

      const safeToken = typeof token === "string" && token.length < 4096 ? token : "session_token";

      if (typeof chrome !== "undefined" && chrome.runtime && chrome.runtime.sendMessage) {
        try {
          chrome.runtime.sendMessage({
            action: "SYNC_APP_AUTH",
            user: safeUser,
            token: safeToken
          });
        } catch (_) {}
      }
    } else if (type === "APPLY2HIRE_AUTH_LOGOUT" || type === "LOGOUT") {
      if (typeof chrome !== "undefined" && chrome.runtime && chrome.runtime.sendMessage) {
        try {
          chrome.runtime.sendMessage({ action: "CLEAR_APP_AUTH" });
        } catch (_) {}
      }
    }
  });

  // 3. Listen for direct messages from extension popup or background
  if (typeof chrome !== "undefined" && chrome.runtime && chrome.runtime.onMessage) {
    chrome.runtime.onMessage.addListener((request, sender, sendResponse) => {
      if (request && request.action === "EXTRACT_PAGE_AUTH") {
        sendResponse(getLocalAuth());
        return true;
      }
    });
  }

  // 4. Periodic sync on focus & storage change
  window.addEventListener("focus", broadcastAuthToExtension);
  window.addEventListener("storage", broadcastAuthToExtension);
  setInterval(broadcastAuthToExtension, 10000);
})();
