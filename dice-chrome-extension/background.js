// Dice Session Sync - Background Service Worker (Manifest V3)
// Automatic Apply2Hire Cookie & LocalStorage Bridge + Dice Session Sync

try {
  importScripts("config.js");
} catch (_) {}

const DEFAULT_CONFIG = (typeof APP_CONFIG !== "undefined")
  ? APP_CONFIG.getDefaults()
  : { backendUrl: "http://localhost:8000", webAppUrl: "http://localhost:5173" };

const DEFAULT_BACKEND_URL = DEFAULT_CONFIG.backendUrl;
const DEFAULT_WEB_APP_URL = DEFAULT_CONFIG.webAppUrl;

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

// Helper to retrieve configured web app URL
async function getWebAppUrl() {
  return new Promise((resolve) => {
    chrome.storage.local.get(["webAppUrl"], (result) => {
      let url = result.webAppUrl || DEFAULT_WEB_APP_URL;
      url = url.trim().replace(/\/+$/, "");
      resolve(url);
    });
  });
}

// ---------------------------------------------------------------------------
// Apply2Hire Authentication Detection (Storage & Cookie Bridge)
// ---------------------------------------------------------------------------

function extractStorageFromPage() {
  try {
    const path = (window.location.pathname || "").toLowerCase();
    if (path.includes("/login") || path.includes("/signin") || path.includes("/register") || path.includes("/signup")) {
      return { isLoggedOutPage: true, ls: {}, ss: {} };
    }

    const ls = {};
    for (let i = 0; i < window.localStorage.length; i++) {
      const k = window.localStorage.key(i);
      if (k) ls[k] = window.localStorage.getItem(k);
    }
    const ss = {};
    for (let i = 0; i < window.sessionStorage.length; i++) {
      const k = window.sessionStorage.key(i);
      if (k) ss[k] = window.sessionStorage.getItem(k);
    }

    // Direct check for custom session bridge keys
    let bridgeUser = null;
    let bridgeToken = null;
    if (ls["a2h_user"]) {
      try { bridgeUser = JSON.parse(ls["a2h_user"]); } catch (_) {}
    }
    if (ls["a2h_token"]) {
      bridgeToken = ls["a2h_token"];
    }

    return { ls, ss, bridgeUser, bridgeToken, href: window.location.href };
  } catch (e) {
    return { error: e.message };
  }
}

function parseAuthFromStorage(storageData) {
  if (!storageData || storageData.isLoggedOutPage) return { token: null, user: null };

  if (storageData.bridgeUser) {
    return {
      token: storageData.bridgeToken || null,
      user: storageData.bridgeUser
    };
  }

  const combined = { ...(storageData.ss || {}), ...(storageData.ls || {}) };
  let foundToken = null;
  let foundUser = null;

  for (const [key, raw] of Object.entries(combined)) {
    if (typeof raw !== "string" || !raw) continue;

    // 1. Direct JWT check
    if (raw.startsWith("eyJ") && raw.split(".").length === 3) {
      if (!key.toLowerCase().includes("refresh")) {
        foundToken = raw;
      }
    }

    // 2. Token key names
    if (/^(token|access_token|accessToken|authToken|auth_token|a2h_token)$/i.test(key)) {
      if (raw.length > 10) foundToken = raw;
    }

    // 3. JSON values (auth object, user profile, etc.)
    if (raw.startsWith("{") && raw.endsWith("}")) {
      try {
        const obj = JSON.parse(raw);
        if (obj.token || obj.access_token || obj.accessToken) {
          foundToken = obj.token || obj.access_token || obj.accessToken;
        }
        if (obj.user && typeof obj.user === "object") {
          foundUser = obj.user;
        }
        if (obj.email && (obj.name || obj.id || obj._id)) {
          foundUser = obj;
        }
      } catch (_) {}
    }

    // 4. User profile key names
    if (/^(user|currentUser|current_user|profile|authUser|auth_user|a2h_user)$/i.test(key)) {
      try {
        const obj = JSON.parse(raw);
        if (obj && (obj.email || obj.name || obj.id || obj._id)) {
          foundUser = obj;
        }
      } catch (_) {}
    }
  }

  return { token: foundToken, user: foundUser };
}

async function checkApply2HireAuth() {
  const backendUrl = await getBackendUrl();
  const webAppUrl = await getWebAppUrl();

  // 1. First inspect open browser tabs matching Apply2Hire to detect live state
  let relevantTabs = [];
  try {
    const allTabs = await chrome.tabs.query({});
    relevantTabs = allTabs.filter(t => t.url && (
      t.url.includes("localhost:5173") ||
      t.url.includes("localhost:3000") ||
      t.url.includes("127.0.0.1:5173") ||
      t.url.includes("apply2hire") ||
      (webAppUrl && t.url.includes(new URL(webAppUrl).hostname))
    ));

    // Check if the user is on an active login or signin tab
    const activeRelevantTab = relevantTabs.find(t => t.active);
    if (activeRelevantTab && activeRelevantTab.url) {
      const activeUrl = activeRelevantTab.url.toLowerCase();
      if (
        activeUrl.includes("/login") ||
        activeUrl.includes("/signin") ||
        activeUrl.includes("/register") ||
        activeUrl.includes("/signup")
      ) {
        // Active tab is explicitly on the auth/login page: clear cached auth AND dice session!
        await chrome.storage.local.remove(["a2h_token", "a2h_user", "dice_session"]);
        return {
          authenticated: false,
          message: "Sign in to Apply2Hire in this browser, then reopen this popup to sync."
        };
      }
    }
  } catch (err) {
    console.debug("[Apply2Hire Sync] Tab query error:", err);
  }

  // 2. Validate cached user profile in chrome.storage.local
  try {
    const stored = await new Promise((r) => chrome.storage.local.get(["a2h_token", "a2h_user"], r));
    if (stored && stored.a2h_user && (stored.a2h_user.name || stored.a2h_user.email)) {
      if (stored.a2h_token && stored.a2h_token.startsWith("eyJ")) {
        try {
          const meResp = await fetch(`${backendUrl}/auth/me`, {
            headers: {
              "Accept": "application/json",
              "Authorization": `Bearer ${stored.a2h_token}`
            }
          });
          if (meResp.ok) {
            const freshUser = await meResp.json();
            await chrome.storage.local.set({ a2h_user: freshUser });
            return { authenticated: true, user: freshUser, token: stored.a2h_token };
          } else if (meResp.status === 401 || meResp.status === 403) {
            // Token is invalid/expired! Wipe storage
            await chrome.storage.local.remove(["a2h_token", "a2h_user"]);
          }
        } catch (_) {}
      } else {
        // Mock/session token stored: check if at least one tab is actually logged in
        let hasOpenAuthTab = false;
        for (const tab of relevantTabs) {
          if (!tab.url) continue;
          const u = tab.url.toLowerCase();
          if (!u.includes("/login") && !u.includes("/signin") && !u.includes("/register") && !u.includes("/signup")) {
            hasOpenAuthTab = true;
            break;
          }
        }
        if (hasOpenAuthTab) {
          return {
            authenticated: true,
            user: stored.a2h_user,
            token: stored.a2h_token || "session_token"
          };
        } else if (relevantTabs.length > 0) {
          await chrome.storage.local.remove(["a2h_token", "a2h_user"]);
        }
      }
    }
  } catch (_) {}

  // 3. Check open tabs matching Apply2Hire
  try {
    relevantTabs.sort((a, b) => (b.active ? 1 : 0) - (a.active ? 1 : 0));

    for (const tab of relevantTabs) {
      if (!tab.id || !tab.url) continue;
      const tabUrl = tab.url.toLowerCase();
      if (tabUrl.includes("/login") || tabUrl.includes("/signin") || tabUrl.includes("/register") || tabUrl.includes("/signup")) {
        continue;
      }

      // Try communicating with content_bridge.js first
      try {
        const tabAuth = await new Promise((resolve) => {
          chrome.tabs.sendMessage(tab.id, { action: "EXTRACT_PAGE_AUTH" }, (resp) => {
            if (chrome.runtime.lastError || !resp) {
              resolve(null);
            } else {
              resolve(resp);
            }
          });
        });

        if (tabAuth && !tabAuth.isLoggedOut && tabAuth.user && (tabAuth.user.name || tabAuth.user.email)) {
          await chrome.storage.local.set({
            a2h_user: tabAuth.user,
            a2h_token: tabAuth.token || "session_token"
          });
          return {
            authenticated: true,
            user: tabAuth.user,
            token: tabAuth.token || "session_token"
          };
        }
      } catch (_) {}

      // Fallback: In-page script execution
      try {
        const scriptRes = await chrome.scripting.executeScript({
          target: { tabId: tab.id },
          func: extractStorageFromPage,
        });

        if (scriptRes && scriptRes[0] && scriptRes[0].result) {
          const parsed = parseAuthFromStorage(scriptRes[0].result);

          if (!parsed.isLoggedOutPage && parsed.user && (parsed.user.email || parsed.user.name)) {
            const userObj = {
              id: parsed.user.id || parsed.user._id || "local_user",
              name: parsed.user.name || "Candidate",
              email: parsed.user.email || ""
            };
            await chrome.storage.local.set({
              a2h_token: parsed.token || "session_token",
              a2h_user: userObj
            });
            return {
              authenticated: true,
              user: userObj,
              token: parsed.token || "session_token"
            };
          }
        }
      } catch (tabErr) {
        console.debug("[Apply2Hire Sync] Script execution notice on tab:", tabErr);
      }
    }
  } catch (err) {
    console.debug("[Apply2Hire Sync] Tab query error:", err);
  }

  // 3. Check cookies across all localhost and backend domains
  try {
    const cookieQueries = [
      chrome.cookies.getAll({ name: "a2h_refresh" }),
      chrome.cookies.getAll({ domain: "localhost" }),
      chrome.cookies.getAll({ url: "http://localhost/" }),
      chrome.cookies.getAll({ url: "http://localhost:8000/" }),
      chrome.cookies.getAll({ url: "http://localhost:5173/" }),
      chrome.cookies.getAll({ url: `${backendUrl}/` })
    ];

    const results = await Promise.allSettled(cookieQueries);
    const candidateCookies = [];
    for (const res of results) {
      if (res.status === "fulfilled" && Array.isArray(res.value)) {
        candidateCookies.push(...res.value);
      }
    }

    const refreshCookie = candidateCookies.find(c => c.name === "a2h_refresh");
    if (refreshCookie && refreshCookie.value) {
      try {
        const resp = await fetch(`${backendUrl}/auth/refresh`, {
          method: "POST",
          headers: {
            "Accept": "application/json",
            "X-Refresh-Token": refreshCookie.value
          },
          credentials: "include"
        });

        if (resp.ok) {
          const data = await resp.json();
          if (data && data.access_token && data.user) {
            await chrome.storage.local.set({
              a2h_token: data.access_token,
              a2h_user: data.user
            });
            return {
              authenticated: true,
              user: data.user,
              token: data.access_token
            };
          }
        }
      } catch (e) {
        console.debug("[Apply2Hire Sync] Cookie refresh attempt notice:", e);
      }
    }
  } catch (err) {
    console.debug("[Apply2Hire Sync] Cookie search notice:", err);
  }

  return {
    authenticated: false,
    message: "Sign in to Apply2Hire in this browser, then reopen this popup to sync."
  };
}

// ---------------------------------------------------------------------------
// Dice Credential Filtering & Extraction
// ---------------------------------------------------------------------------

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

function parseJwtPayload(tokenStr) {
  if (!tokenStr || typeof tokenStr !== "string") return null;
  const parts = tokenStr.split(".");
  if (parts.length < 2) return null;
  try {
    const base64Url = parts[1];
    const base64 = base64Url.replace(/-/g, "+").replace(/_/g, "/");
    const jsonStr = decodeURIComponent(
      atob(base64)
        .split("")
        .map((c) => "%" + ("00" + c.charCodeAt(0).toString(16)).slice(-2))
        .join("")
    );
    return JSON.parse(jsonStr);
  } catch (_) {
    try {
      return JSON.parse(atob(parts[1]));
    } catch (_) {
      return null;
    }
  }
}

function extractDiceCandidateDetails(cookies, localStorageObj) {
  let diceName = "";
  let diceEmail = "";
  let candidateId = "";

  // 1. Check identity cookie JWT from dice.com
  if (Array.isArray(cookies)) {
    const identityCookie = cookies.find((c) => (c.name || "").toLowerCase() === "identity");
    if (identityCookie && identityCookie.value) {
      const payload = parseJwtPayload(identityCookie.value);
      if (payload) {
        const first = (payload.given_name || payload.first_name || "").trim();
        const last = (payload.family_name || payload.last_name || "").trim();
        const full = (payload.name || `${first} ${last}`).trim();
        if (full) diceName = full;
        if (payload.email) diceEmail = String(payload.email).trim();
        if (payload.candidate_id || payload.sub) {
          candidateId = String(payload.candidate_id || payload.sub).trim();
        }
      }
    }
  }

  // 2. Check Cognito idToken and userData in localStorage
  if (localStorageObj && typeof localStorageObj === "object") {
    for (const [k, v] of Object.entries(localStorageObj)) {
      if (typeof v !== "string") continue;
      const kLower = k.toLowerCase();
      if (kLower.includes("idtoken") && v.includes(".")) {
        const payload = parseJwtPayload(v);
        if (payload) {
          if (!diceEmail && payload.email) diceEmail = String(payload.email).trim();
          if (!diceName) {
            const first = (payload.given_name || payload.first_name || "").trim();
            const last = (payload.family_name || payload.last_name || "").trim();
            const full = (payload.name || `${first} ${last}`).trim();
            if (full) diceName = full;
          }
          if (!candidateId && (payload.sub || payload["cognito:username"])) {
            candidateId = String(payload.sub || payload["cognito:username"]).trim();
          }
        }
      }
      if (kLower.includes("userdata")) {
        try {
          const ud = JSON.parse(v);
          const attrs = ud.UserAttributes || [];
          for (const attr of attrs) {
            if (attr.Name === "email" && !diceEmail && attr.Value) {
              diceEmail = String(attr.Value).trim();
            }
            if ((attr.Name === "name" || attr.Name === "given_name") && !diceName && attr.Value) {
              diceName = String(attr.Value).trim();
            }
          }
        } catch (_) {}
      }
    }
  }

  return {
    username: diceName || "",
    email: diceEmail || "",
    candidate_id: candidateId || ""
  };
}

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

async function captureDiceSession() {
  const allCookies = await chrome.cookies.getAll({ domain: "dice.com" });
  const authCookies = filterDiceAuthCookies(allCookies);

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

// ---------------------------------------------------------------------------
// Runtime Messages Listener
// ---------------------------------------------------------------------------

chrome.runtime.onMessage.addListener((request, sender, sendResponse) => {
  if (request.action === "CHECK_APPLY2HIRE_AUTH") {
    (async () => {
      try {
        const auth = await checkApply2HireAuth();
        sendResponse({ success: true, data: auth });
      } catch (err) {
        sendResponse({ success: false, error: err.message });
      }
    })();
    return true;
  }

  if (request.action === "STORE_AUTH_SESSION" || request.action === "SYNC_APP_AUTH") {
    (async () => {
      try {
        if (request.token || request.user) {
          await chrome.storage.local.set({
            a2h_token: request.token || "session_token",
            a2h_user: request.user || null
          });
        }
        sendResponse({ success: true });
      } catch (err) {
        sendResponse({ success: false, error: err.message });
      }
    })();
    return true;
  }

  if (request.action === "CLEAR_APP_AUTH") {
    (async () => {
      try {
        await chrome.storage.local.remove(["a2h_token", "a2h_user", "dice_session"]);
        sendResponse({ success: true });
      } catch (err) {
        sendResponse({ success: false, error: err.message });
      }
    })();
    return true;
  }

  if (request.action === "GET_SESSION_STATUS") {
    (async () => {
      try {
        const backendUrl = await getBackendUrl();
        const webAppUrl = await getWebAppUrl();
        const auth = await checkApply2HireAuth();

        if (!auth.authenticated) {
          sendResponse({
            success: true,
            data: {
              app_auth: auth,
              dice: { connected: false, status: "login_required" },
              backend_url: backendUrl,
              web_app_url: webAppUrl
            }
          });
          return;
        }

        const headers = { "Accept": "application/json" };
        if (auth.authenticated && auth.token && auth.token !== "session_token") {
          headers["Authorization"] = `Bearer ${auth.token}`;
        }

        const queryUid = auth.authenticated && auth.user && auth.user.id ? `?user_id=${auth.user.id}` : "";
        const liveSep = queryUid ? "&" : "?";
        const resp = await fetch(`${backendUrl}/api/dice/session/status${queryUid}${liveSep}check_live=true`, {
          method: "GET",
          headers,
        });

        let diceData = null;
        if (resp.ok) {
          diceData = await resp.json();
        }

        // Only cache dice_session if actively connected on backend; otherwise ensure storage is clear
        if (diceData && diceData.connected) {
          try {
            await chrome.storage.local.set({ dice_session: diceData });
          } catch (_) {}
        } else {
          try {
            await chrome.storage.local.remove(["dice_session"]);
          } catch (_) {}
        }

        sendResponse({
          success: true,
          data: {
            app_auth: auth,
            dice: diceData || { connected: false, status: "unknown" },
            backend_url: backendUrl,
            web_app_url: webAppUrl
          }
        });
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

        // 1. Verify user is authenticated in Apply2Hire
        const auth = await checkApply2HireAuth();
        if (!auth.authenticated) {
          sendResponse({
            success: false,
            error: "Not logged in to Apply2Hire. Sign in on the website in Chrome first, then click Sync.",
            require_app_auth: true
          });
          return;
        }

        // 2. Capture authenticated Dice session data
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

        // 3. Post session package to backend sync endpoint with user token
        const syncUrl = `${backendUrl}/api/dice/session/sync`;
        const headers = {
          "Content-Type": "application/json",
          "Accept": "application/json",
        };
        if (auth.token && auth.token !== "session_token") {
          headers["Authorization"] = `Bearer ${auth.token}`;
        }

        const diceDetails = extractDiceCandidateDetails(sessionData.cookies, sessionData.local_storage);
        const targetUid = (auth.user && auth.user.id) || "default";
        const resp = await fetch(syncUrl, {
          method: "POST",
          headers,
          body: JSON.stringify({
            cookies: sessionData.cookies,
            local_storage: sessionData.local_storage,
            user_id: targetUid,
            username: diceDetails.username || undefined,
            email: diceDetails.email || undefined
          }),
        });

        if (resp.ok) {
          const result = await resp.json();
          try {
            await chrome.storage.local.set({
              dice_session: {
                connected: Boolean(result.connected),
                status: result.status || "valid",
                username: result.username || diceDetails.username || "Dice Candidate",
                email: result.email || diceDetails.email || "",
                last_verified_at: result.last_verified_at || new Date().toISOString()
              }
            });
          } catch (_) {}

          sendResponse({
            success: result.connected,
            data: result,
            app_user: auth.user
          });
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

  if (request.action === "DISCONNECT_DICE") {
    (async () => {
      try {
        const backendUrl = await getBackendUrl();
        const auth = await checkApply2HireAuth();
        const headers = { "Accept": "application/json" };
        if (auth.authenticated && auth.token && auth.token !== "session_token") {
          headers["Authorization"] = `Bearer ${auth.token}`;
        }
        const resp = await fetch(`${backendUrl}/api/dice/session/disconnect`, {
          method: "POST",
          headers
        });
        await chrome.storage.local.remove(["dice_session"]);
        if (resp.ok) {
          sendResponse({ success: true });
        } else {
          sendResponse({ success: false, error: `Disconnect returned HTTP ${resp.status}` });
        }
      } catch (err) {
        sendResponse({ success: false, error: err.message });
      }
    })();
    return true;
  }
});
