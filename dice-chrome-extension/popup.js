// Apply2Hire - Session Sync Popup Script (Manifest V3)

document.addEventListener("DOMContentLoaded", async () => {
  // Navigation Tabs
  const tabConnectBtn = document.getElementById("tabConnectBtn");
  const tabSettingsBtn = document.getElementById("tabSettingsBtn");
  const viewConnect = document.getElementById("viewConnect");
  const viewSettings = document.getElementById("viewSettings");

  // Alert Box
  const alertBox = document.getElementById("alertBox");
  const alertIcon = document.getElementById("alertIcon");
  const alertMessage = document.getElementById("alertMessage");

  // Apply2Hire Auth Elements
  const notLoggedInBox = document.getElementById("notLoggedInBox");
  const loggedInBox = document.getElementById("loggedInBox");
  const openAppBtn = document.getElementById("openAppBtn");
  const authUrlCaption = document.getElementById("authUrlCaption");
  const appUserAvatar = document.getElementById("appUserAvatar");
  const appUserName = document.getElementById("appUserName");
  const appUserEmail = document.getElementById("appUserEmail");

  // Dice Session Elements
  const diceDot = document.getElementById("diceDot");
  const diceBadge = document.getElementById("diceBadge");
  const diceBadgeText = document.getElementById("diceBadgeText");
  const diceDetails = document.getElementById("diceDetails");
  const diceCandidateName = document.getElementById("diceCandidateName");
  const diceCandidateEmail = document.getElementById("diceCandidateEmail");
  const diceVerifiedAt = document.getElementById("diceVerifiedAt");
  const diceEmptyNote = document.getElementById("diceEmptyNote");

  // Buttons
  const syncBtn = document.getElementById("syncBtn");
  const btnSpinner = document.getElementById("btnSpinner");
  const btnIcon = document.getElementById("btnIcon");
  const btnText = document.getElementById("btnText");
  const openDiceBtn = document.getElementById("openDiceBtn");
  const disconnectBtn = document.getElementById("disconnectBtn");

  // Settings Inputs
  const backendUrlInput = document.getElementById("backendUrlInput");
  const webAppUrlInput = document.getElementById("webAppUrlInput");
  const saveSettingsBtn = document.getElementById("saveSettingsBtn");

  const hasChromeStorage = typeof chrome !== "undefined" && chrome.storage && chrome.storage.local;
  const hasChromeRuntime = typeof chrome !== "undefined" && chrome.runtime && chrome.runtime.sendMessage;

  const defaultConfig = (typeof APP_CONFIG !== "undefined")
    ? APP_CONFIG.getDefaults()
    : { backendUrl: "http://localhost:8000", webAppUrl: "http://localhost:5173" };

  let currentWebAppUrl = defaultConfig.webAppUrl;
  let currentBackendUrl = defaultConfig.backendUrl;

  // Tab switching
  tabConnectBtn.addEventListener("click", () => {
    tabConnectBtn.classList.add("active");
    tabSettingsBtn.classList.remove("active");
    viewConnect.style.display = "flex";
    viewSettings.style.display = "none";
  });

  tabSettingsBtn.addEventListener("click", () => {
    tabSettingsBtn.classList.add("active");
    tabConnectBtn.classList.remove("active");
    viewSettings.style.display = "flex";
    viewConnect.style.display = "none";
  });

  // Load configured URLs
  if (hasChromeStorage) {
    chrome.storage.local.get(["backendUrl", "webAppUrl"], (result) => {
      currentBackendUrl = result.backendUrl || defaultConfig.backendUrl;
      currentWebAppUrl = result.webAppUrl || defaultConfig.webAppUrl;
      backendUrlInput.value = currentBackendUrl;
      webAppUrlInput.value = currentWebAppUrl;
      if (authUrlCaption) authUrlCaption.textContent = currentWebAppUrl;
    });
  } else {
    backendUrlInput.value = currentBackendUrl;
    webAppUrlInput.value = currentWebAppUrl;
  }

  // Preset buttons
  document.querySelectorAll("[data-backend]").forEach((btn) => {
    btn.addEventListener("click", () => {
      backendUrlInput.value = btn.getAttribute("data-backend");
    });
  });

  document.querySelectorAll("[data-web]").forEach((btn) => {
    btn.addEventListener("click", () => {
      webAppUrlInput.value = btn.getAttribute("data-web");
    });
  });

  // Save Settings
  saveSettingsBtn.addEventListener("click", () => {
    let bUrl = backendUrlInput.value.trim().replace(/\/+$/, "") || defaultConfig.backendUrl;
    let wUrl = webAppUrlInput.value.trim().replace(/\/+$/, "") || defaultConfig.webAppUrl;
    if (hasChromeStorage) {
      chrome.storage.local.set({ backendUrl: bUrl, webAppUrl: wUrl }, () => {
        currentBackendUrl = bUrl;
        currentWebAppUrl = wUrl;
        if (authUrlCaption) authUrlCaption.textContent = currentWebAppUrl;
        showAlert("success", "Configuration saved. Refreshing session...");
        tabConnectBtn.click();
        loadSessionStatus();
      });
    }
  });

  // "Open Apply2Hire" button
  openAppBtn.addEventListener("click", (e) => {
    e.preventDefault();
    const targetUrl = currentWebAppUrl || "http://localhost:5173";
    if (typeof chrome !== "undefined" && chrome.tabs && chrome.tabs.create) {
      chrome.tabs.create({ url: targetUrl });
    } else {
      window.open(targetUrl, "_blank");
    }
  });

  // "Open Dice.com" link
  openDiceBtn.addEventListener("click", (e) => {
    e.preventDefault();
    if (typeof chrome !== "undefined" && chrome.tabs && chrome.tabs.create) {
      chrome.tabs.create({ url: "https://www.dice.com/dashboard" });
    } else {
      window.open("https://www.dice.com/dashboard", "_blank");
    }
  });

  // "Sync Dice Account" click
  syncBtn.addEventListener("click", async () => {
    setLoading(true);
    hideAlert();

    if (!hasChromeRuntime) {
      setTimeout(() => {
        setLoading(false);
        showAlert("info", "Preview mode: Run as an installed Chrome extension to capture real session cookies.");
      }, 700);
      return;
    }

    try {
      chrome.runtime.sendMessage({ action: "SYNC_DICE_SESSION" }, (response) => {
        setLoading(false);

        if (chrome.runtime.lastError) {
          showAlert("error", `Extension connection error: ${chrome.runtime.lastError.message}`);
          return;
        }

        if (response && response.success) {
          const d = response.data || {};
          const candidateStr = d.username || d.email || "Candidate";
          showAlert("success", `Sync successful! Dice account '${candidateStr}' linked to Apply2Hire.`);
          showDiceConnected(d);
          if (hasChromeStorage) {
            chrome.storage.local.set({ dice_session: d });
          }
          loadSessionStatus();
        } else {
          const err = (response && response.error) || "Failed to sync Dice session.";
          showAlert("error", err);
          if (response && response.require_app_auth) {
            showApply2HireNotLoggedIn();
          }
        }
      });
    } catch (err) {
      setLoading(false);
      showAlert("error", `Unexpected error: ${err.message}`);
    }
  });

  // "Disconnect" click
  disconnectBtn.addEventListener("click", () => {
    if (!hasChromeRuntime) return;
    disconnectBtn.disabled = true;
    chrome.runtime.sendMessage({ action: "DISCONNECT_DICE" }, (resp) => {
      disconnectBtn.disabled = false;
      if (resp && resp.success) {
        showAlert("info", "Dice session disconnected.");
        loadSessionStatus();
      } else {
        showAlert("error", (resp && resp.error) || "Failed to disconnect.");
      }
    });
  });

  // In-page extractor to run on the active tab if Apply2Hire is open
  function extractActiveTabSession() {
    try {
      let user = null;
      let token = null;

      const rawUser = localStorage.getItem("a2h_user");
      if (rawUser) {
        try { user = JSON.parse(rawUser); } catch (_) {}
      }
      token = localStorage.getItem("a2h_token");

      if (!user) {
        const rawSs = sessionStorage.getItem("a2h_user");
        if (rawSs) {
          try { user = JSON.parse(rawSs); } catch (_) {}
        }
      }
      if (!token) token = sessionStorage.getItem("a2h_token");

      // DOM inspection fallback
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

  // Initial load: Fast checks
  if (hasChromeStorage) {
    chrome.storage.local.get(["a2h_user", "a2h_token", "dice_session"], (stored) => {
      if (stored && stored.a2h_user && (stored.a2h_user.name || stored.a2h_user.email)) {
        showApply2HireLoggedIn(stored.a2h_user);
      }
      if (stored && stored.dice_session && stored.dice_session.connected) {
        showDiceConnected(stored.dice_session);
      }
    });
  }

  // Check active tab directly (Apply2Hire website)
  try {
    if (typeof chrome !== "undefined" && chrome.tabs && chrome.tabs.query) {
      const tabs = await chrome.tabs.query({ active: true, currentWindow: true });
      const activeTab = tabs && tabs[0];
      if (activeTab && activeTab.id && activeTab.url && (
        activeTab.url.includes("localhost:5173") ||
        activeTab.url.includes("localhost:3000") ||
        activeTab.url.includes("127.0.0.1:5173") ||
        activeTab.url.includes("apply2hire")
      )) {
        try {
          const results = await chrome.scripting.executeScript({
            target: { tabId: activeTab.id },
            func: extractActiveTabSession,
          });
          if (results && results[0] && results[0].result && results[0].result.user) {
            const { user, token } = results[0].result;
            showApply2HireLoggedIn(user);
            if (hasChromeStorage) {
              chrome.storage.local.set({ a2h_user: user, a2h_token: token || "session_token" });
            }
            if (hasChromeRuntime) {
              chrome.runtime.sendMessage({ action: "SYNC_APP_AUTH", user, token: token || "session_token" });
            }
          }
        } catch (tabScanErr) {
          console.debug("Active tab script extraction notice:", tabScanErr);
        }
      }
    }
  } catch (err) {
    console.debug("Active tab inspection notice:", err);
  }

  await loadSessionStatus();

  // Helper to load combined session status from background service worker
  async function loadSessionStatus() {
    if (!hasChromeRuntime) return;

    chrome.runtime.sendMessage({ action: "GET_SESSION_STATUS" }, (response) => {
      if (chrome.runtime.lastError) {
        showAlert("error", "Cannot connect to extension background service.");
        return;
      }

      if (response && response.success && response.data) {
        const { app_auth, dice } = response.data;

        // 1. Render Apply2Hire state
        if (app_auth && app_auth.authenticated && app_auth.user) {
          showApply2HireLoggedIn(app_auth.user);
        } else {
          // If we already detected the user from storage/active tab, keep it
          if (!loggedInBox || (loggedInBox.style.display !== "flex" && loggedInBox.style.display !== "block")) {
            showApply2HireNotLoggedIn();
          }
        }

        // 2. Render Dice state
        if (dice && dice.connected) {
          showDiceConnected(dice);
        } else {
          if (hasChromeStorage) {
            chrome.storage.local.get(["dice_session"], (stored) => {
              if (stored && stored.dice_session && stored.dice_session.connected) {
                showDiceConnected(stored.dice_session);
              } else {
                showDiceDisconnected();
              }
            });
          } else {
            showDiceDisconnected();
          }
        }
      }
    });
  }

  function showApply2HireLoggedIn(user) {
    notLoggedInBox.style.display = "none";
    loggedInBox.style.display = "block";
    const firstName = user.name ? user.name.trim().split(/\s+/)[0] : "there";
    const heroName = document.getElementById("heroWelcomeName");
    if (heroName) heroName.textContent = firstName;
    const heroEmail = document.getElementById("heroConnectedEmail");
    if (heroEmail) heroEmail.textContent = user.email ? `Connected: ${user.email}` : "Connected";
    if (appUserName) appUserName.textContent = user.name || "Candidate User";
    if (appUserEmail) appUserEmail.textContent = user.email || "-";
    if (appUserAvatar) appUserAvatar.textContent = getInitials(user.name, user.email);
    syncBtn.disabled = false;
  }

  function showApply2HireNotLoggedIn() {
    loggedInBox.style.display = "none";
    notLoggedInBox.style.display = "flex";
    if (authUrlCaption) authUrlCaption.textContent = currentWebAppUrl;
  }

  function showDiceConnected(dice) {
    if (!dice) return;
    diceDot.className = "status-dot connected";
    diceBadge.className = "status-badge badge-success";
    diceBadgeText.textContent = "CONNECTED";
    const rawName = (dice.username || dice.name || "").trim();
    diceCandidateName.textContent = rawName && rawName !== "default" ? rawName : "Active Candidate";
    const rawEmail = (dice.email || "").trim();
    diceCandidateEmail.textContent = rawEmail || "Session linked";
    diceVerifiedAt.textContent = dice.last_verified_at ? formatDate(dice.last_verified_at) : "Active";
    diceDetails.style.display = "flex";
    diceEmptyNote.style.display = "none";
    disconnectBtn.style.display = "inline-flex";
    btnText.textContent = "Sync Dice Again";
  }

  function showDiceDisconnected() {
    diceDot.className = "status-dot disconnected";
    diceBadge.className = "status-badge badge-neutral";
    diceBadgeText.textContent = "Not Connected";
    diceDetails.style.display = "none";
    diceEmptyNote.style.display = "block";
    disconnectBtn.style.display = "none";
    btnText.textContent = "Sync Dice Account";
  }

  function setLoading(loading) {
    syncBtn.disabled = loading;
    btnSpinner.style.display = loading ? "inline-block" : "none";
    btnIcon.style.display = loading ? "none" : "inline-block";
    btnText.textContent = loading ? "Syncing with Server..." : "Sync Dice Account";
    if (loading) {
      diceDot.className = "status-dot syncing";
    }
  }

  function showAlert(type, text) {
    alertBox.className = `alert-banner ${type}`;
    let svgIcon = "";
    if (type === "success") {
      svgIcon = `<svg viewBox="0 0 20 20" fill="currentColor"><path fill-rule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zm3.707-9.293a1 1 0 00-1.414-1.414L9 10.586 7.707 9.293a1 1 0 00-1.414 1.414l2 2a1 1 0 001.414 0l4-4z" clip-rule="evenodd" /></svg>`;
    } else if (type === "error") {
      svgIcon = `<svg viewBox="0 0 20 20" fill="currentColor"><path fill-rule="evenodd" d="M18 10a8 8 0 11-16 0 8 8 0 0116 0zm-7 4a1 1 0 11-2 0 1 1 0 012 0zm-1-9a1 1 0 00-1 1v4a1 1 0 102 0V6a1 1 0 00-1-1z" clip-rule="evenodd" /></svg>`;
    } else {
      svgIcon = `<svg viewBox="0 0 20 20" fill="currentColor"><path fill-rule="evenodd" d="M18 10a8 8 0 11-16 0 8 8 0 0116 0zm-7-4a1 1 0 11-2 0 1 1 0 012 0zM9 9a1 1 0 000 2v3a1 1 0 001 1h1a1 1 0 100-2v-3a1 1 0 00-1-1H9z" clip-rule="evenodd" /></svg>`;
    }
    alertIcon.innerHTML = svgIcon;
    alertMessage.textContent = text;
    alertBox.style.display = "flex";
  }

  function hideAlert() {
    alertBox.style.display = "none";
  }

  function getInitials(name, email) {
    if (name && name.trim()) {
      const parts = name.trim().split(/\s+/);
      if (parts.length >= 2) {
        return (parts[0][0] + parts[1][0]).toUpperCase();
      }
      return name.slice(0, 2).toUpperCase();
    }
    if (email && email.includes("@")) {
      return email.slice(0, 2).toUpperCase();
    }
    return "AH";
  }

  function formatDate(isoStr) {
    try {
      const dt = new Date(isoStr);
      if (isNaN(dt.getTime())) return isoStr;
      const now = new Date();
      const diffSec = Math.floor((now - dt) / 1000);
      if (diffSec < 60) return "Just now";
      if (diffSec < 3600) return `${Math.floor(diffSec / 60)}m ago`;
      if (diffSec < 86400) return `${Math.floor(diffSec / 3600)}h ago`;
      return dt.toLocaleDateString([], { month: "short", day: "numeric" }) + " " +
             dt.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    } catch (_) {
      return isoStr;
    }
  }
});
