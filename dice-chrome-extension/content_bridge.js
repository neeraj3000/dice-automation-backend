// Apply2Hire - Content Bridge Script
// Injected into Apply2Hire pages (localhost / 127.0.0.1 / apply2hire.com)
// Continuously bridges authentication status to extension storage & background service

(function () {
  function getLocalAuth() {
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

      // Fallback: Check if user is present in sessionStorage
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

      // Fallback: DOM inspection for logged in greeting
      if (!user) {
        const bodyText = document.body ? document.body.innerText : "";
        const emailMatch = bodyText.match(/([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})/);
        const welcomeMatch = bodyText.match(/Welcome back,\s*([A-Za-z0-9_-]+)/i);
        if (emailMatch || welcomeMatch) {
          user = {
            id: "local_user",
            name: welcomeMatch ? welcomeMatch[1] : "Candidate",
            email: emailMatch ? emailMatch[1] : ""
          };
        }
      }

      return { user, token };
    } catch (e) {
      return { user: null, token: null };
    }
  }

  function broadcastAuthToExtension() {
    const auth = getLocalAuth();
    if (auth.user && (auth.user.email || auth.user.name)) {
      if (typeof chrome !== "undefined" && chrome.runtime && chrome.runtime.sendMessage) {
        try {
          chrome.runtime.sendMessage({
            action: "SYNC_APP_AUTH",
            user: auth.user,
            token: auth.token || "session_token"
          }, () => {
            if (chrome.runtime.lastError) {
              // Ignore background worker inactive errors
            }
          });
        } catch (_) {}
      }
    }
  }

  // 1. Initial sync on page load / script injection
  broadcastAuthToExtension();

  // 2. Listen to custom window events from React frontend
  window.addEventListener("message", (event) => {
    if (event.data && event.data.type === "APPLY2HIRE_AUTH_SYNC") {
      const { user, token } = event.data;
      if (user && typeof chrome !== "undefined" && chrome.runtime && chrome.runtime.sendMessage) {
        try {
          chrome.runtime.sendMessage({
            action: "SYNC_APP_AUTH",
            user,
            token: token || "session_token"
          });
        } catch (_) {}
      }
    } else if (event.data && event.data.type === "APPLY2HIRE_AUTH_LOGOUT") {
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

  // 4. Periodic sync on focus
  window.addEventListener("focus", broadcastAuthToExtension);
  setInterval(broadcastAuthToExtension, 15000);
})();
