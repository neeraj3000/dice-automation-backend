// Dice Session Sync - Background Service Worker (Manifest V3)

const DEFAULT_BACKEND_URL = "http://localhost:8000";

const TRACKING_COOKIE_PREFIXES = [
  "_ga", "_gid", "_gat", "_gcl", "_uet", "_mkto", "_gd_",
  "intercom", "optimizely", "amplitude", "hotjar", "li_",
  "bscookie", "bcookie", "hubspot", "ajs_", "mp_"
];

const AUTH_COOKIE_KEYWORDS = [
  "identity", "refreshtoken", "candidate_id", "peopleid",
  "dice_member_id", "dice_session", "dice-user-id", "_oauth2_proxy",
  "cognito", "test_auth_token", "auth", "token", "session"
];

const COGNITO_LS_KEYWORDS = [
  "idtoken", "accesstoken", "refreshtoken", "lastauthuser",
  "userdata", "cognito", "clockdrift", "dice_auth"
];

// Helper to retrieve configured backend URL
async function getBackendUrl() {
  return new Promise((resolve) => {
    chrome.storage.local.get(["backendUrl"], (result) => {
      let url = result.backendUrl || DEFAULT_BACKEND_URL;
      url = url.trim().replace(/\/+$/, "");
      resolve(url);
    });
  });
}

// Filter authentic Dice session cookies and exclude tracking cookies
function filterDiceAuthCookies(cookies) {
  if (!Array.isArray(cookies)) return [];

  return cookies.filter((cookie) => {
    const name = (cookie.name || "").toLowerCase().trim();
    const domain = (cookie.domain || "").toLowerCase().trim();
    const val = (cookie.value || "").trim();

    if (!val) return false;
    if (domain && !domain.includes("dice.com")) return false;

    // Exclude analytics and tracking cookies
    if (TRACKING_COOKIE_PREFIXES.some((prefix) => name.startsWith(prefix))) return false;
    if (["dli", "cms_cookie", "_fbp", "session_id", "crumb"].includes(name)) return false;

    // Retain candidate authentication cookies
    if (name === "identity" && val.length > 20) return true;
    if (AUTH_COOKIE_KEYWORDS.some((kw) => name.includes(kw))) return true;

    return domain.includes("dice.com");
  });
}

// Injected into dice.com page to extract only Cognito authentication localStorage
function extractCognitoLocalStorage() {
  const authItems = {};
  const keywords = ["idtoken", "accesstoken", "refreshtoken", "lastauthuser", "userdata", "cognito", "dice_auth"];

  try {
    for (let i = 0; i < window.localStorage.length; i++) {
      const key = window.localStorage.key(i);
      if (!key) continue;
      const keyLower = key.toLowerCase();
      if (keywords.some((kw) => keyLower.includes(kw))) {
        const val = window.localStorage.getItem(key);
        if (typeof val === "string" && val.length > 0) {
          authItems[key] = val;
        }
      }
    }
  } catch (err) {
    console.debug("[Dice Sync] Notice reading localStorage:", err);
  }

  return authItems;
}

// Capture authentic Dice cookies and localStorage
async function captureDiceSession() {
  // 1. Capture cookies for dice.com
  const allCookies = await chrome.cookies.getAll({ domain: "dice.com" });
  const authCookies = filterDiceAuthCookies(allCookies);

  // 2. Query for active dice.com tab to extract Cognito localStorage
  let authLocalStorage = {};
  try {
    const tabs = await chrome.tabs.query({ url: "*://*.dice.com/*" });
    if (tabs && tabs.length > 0) {
      const targetTab = tabs.find((t) => t.active) || tabs[0];
      if (targetTab && targetTab.id) {
        const results = await chrome.scripting.executeScript({
          target: { tabId: targetTab.id },
          func: extractCognitoLocalStorage,
        });
        if (results && results[0] && results[0].result) {
          authLocalStorage = results[0].result;
        }
      }
    }
  } catch (err) {
    console.debug("[Dice Sync] Notice querying active tab for localStorage:", err);
  }

  return {
    cookies: authCookies,
    local_storage: authLocalStorage,
  };
}

// Main message listener
chrome.runtime.onMessage.addListener((request, sender, sendResponse) => {
  if (request.action === "GET_SESSION_STATUS") {
    (async () => {
      try {
        const backendUrl = await getBackendUrl();
        const resp = await fetch(`${backendUrl}/api/dice/session/status`, {
          method: "GET",
          headers: { "Accept": "application/json" },
        });

        if (resp.ok) {
          const data = await resp.json();
          sendResponse({ success: true, data });
        } else {
          sendResponse({
            success: false,
            error: `Backend returned HTTP ${resp.status}`,
          });
        }
      } catch (err) {
        sendResponse({
          success: false,
          error: `Could not connect to backend: ${err.message}`,
        });
      }
    })();
    return true;
  }

  if (request.action === "SYNC_DICE_SESSION") {
    (async () => {
      try {
        const backendUrl = await getBackendUrl();

        // 1. Capture authenticated Dice session data
        const sessionData = await captureDiceSession();

        const hasIdentityCookie = sessionData.cookies.some((c) => c.name === "identity");
        const hasCognitoTokens = Object.keys(sessionData.local_storage).length > 0;

        if (!hasIdentityCookie && !hasCognitoTokens && sessionData.cookies.length === 0) {
          sendResponse({
            success: false,
            error: "No active Dice.com session found. Please log in to Dice.com in Chrome first, then click Sync.",
          });
          return;
        }

        // 2. Post session package to backend sync endpoint
        const syncUrl = `${backendUrl}/api/dice/session/sync`;
        const resp = await fetch(syncUrl, {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "Accept": "application/json",
          },
          body: JSON.stringify({
            cookies: sessionData.cookies,
            local_storage: sessionData.local_storage,
            user_id: "default",
          }),
        });

        if (resp.ok) {
          const result = await resp.json();
          sendResponse({ success: result.connected, data: result });
        } else {
          let errText = `HTTP ${resp.status}`;
          try {
            const errJson = await resp.json();
            if (errJson.detail || errJson.message) {
              errText = errJson.detail || errJson.message;
            }
          } catch (_) {}
          sendResponse({
            success: false,
            error: `Sync failed: ${errText}`,
          });
        }
      } catch (err) {
        sendResponse({
          success: false,
          error: `Network error connecting to backend: ${err.message}`,
        });
      }
    })();
    return true;
  }
});
