// Dice Session Sync - Production Grade Popup Script

document.addEventListener("DOMContentLoaded", async () => {
  // Status Elements
  const statusDot = document.getElementById("statusDot");
  const statusText = document.getElementById("statusText");
  const statusBadge = document.getElementById("statusBadge");
  const badgeText = document.getElementById("badgeText");
  const disconnectedNotice = document.getElementById("disconnectedNotice");
  
  // Account Details Elements
  const accountDetails = document.getElementById("accountDetails");
  const userAvatar = document.getElementById("userAvatar");
  const detailUsername = document.getElementById("detailUsername");
  const detailEmail = document.getElementById("detailEmail");
  const detailVerified = document.getElementById("detailVerified");

  // Alert Box Elements
  const alertBox = document.getElementById("alertBox");
  const alertIcon = document.getElementById("alertIcon");
  const alertMessage = document.getElementById("alertMessage");

  // Buttons & Controls
  const syncBtn = document.getElementById("syncBtn");
  const btnSpinner = document.getElementById("btnSpinner");
  const btnIcon = document.getElementById("btnIcon");
  const btnText = document.getElementById("btnText");
  const openDiceBtn = document.getElementById("openDiceBtn");

  // Settings Panel Elements
  const settingsToggle = document.getElementById("settingsToggle");
  const settingsPanel = document.getElementById("settingsPanel");
  const settingsArrow = document.getElementById("settingsArrow");
  const backendUrlInput = document.getElementById("backendUrlInput");
  const saveSettingsBtn = document.getElementById("saveSettingsBtn");
  const presetTags = document.querySelectorAll(".preset-tag");

  // Safe chrome APIs check
  const hasChromeStorage = typeof chrome !== "undefined" && chrome.storage && chrome.storage.local;
  const hasChromeRuntime = typeof chrome !== "undefined" && chrome.runtime && chrome.runtime.sendMessage;

  // Load configured backend URL from storage
  if (hasChromeStorage) {
    chrome.storage.local.get(["backendUrl"], (result) => {
      backendUrlInput.value = result.backendUrl || "http://localhost:8000";
    });
  } else {
    backendUrlInput.value = "http://localhost:8000";
  }

  // Preset button clicks
  presetTags.forEach((btn) => {
    btn.addEventListener("click", () => {
      const url = btn.getAttribute("data-url");
      if (url) {
        backendUrlInput.value = url;
      }
    });
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

  // Expand / collapse backend settings drawer
  settingsToggle.addEventListener("click", () => {
    const isHidden = settingsPanel.style.display === "none" || !settingsPanel.style.display;
    settingsPanel.style.display = isHidden ? "flex" : "none";
    if (settingsArrow) {
      if (isHidden) {
        settingsArrow.classList.add("expanded");
      } else {
        settingsArrow.classList.remove("expanded");
      }
    }
  });

  // Save backend URL
  saveSettingsBtn.addEventListener("click", () => {
    let url = backendUrlInput.value.trim().replace(/\/+$/, "");
    if (!url) url = "http://localhost:8000";
    if (hasChromeStorage) {
      chrome.storage.local.set({ backendUrl: url }, () => {
        showAlert("success", "Backend endpoint saved. Verifying connection...");
        checkSessionStatus();
      });
    } else {
      showAlert("success", "Backend endpoint saved (preview mode).");
    }
  });

  // "Sync Dice Account" click handler
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
          setStatus("error", "Offline", "Error");
          return;
        }

        if (response && response.success) {
          const d = response.data || {};
          const userStr = d.username || d.email || "Candidate";
          showAlert("success", `Sync successful! Account '${userStr}' is authenticated and live on your server.`);
          setStatus("connected", "Connected", "Active");
          showAccountDetails(d.username, d.email, d.last_verified_at || new Date().toISOString());
        } else {
          const err = (response && (response.error || (response.data && response.data.message))) || "Failed to capture active session.";
          showAlert("error", err);
          setStatus("disconnected", "Not Connected", "Offline");
        }
      });
    } catch (err) {
      setLoading(false);
      showAlert("error", `Unexpected error: ${err.message}`);
      setStatus("error", "Error", "Error");
    }
  });

  // Check current backend session status on load
  await checkSessionStatus();

  // Query backend for session health
  async function checkSessionStatus() {
    setStatus("syncing", "Checking...", "Checking");
    if (!hasChromeRuntime) {
      setStatus("disconnected", "Ready", "Ready");
      return;
    }

    chrome.runtime.sendMessage({ action: "GET_SESSION_STATUS" }, (response) => {
      if (chrome.runtime.lastError) {
        setStatus("error", "Offline", "Server Offline");
        showAlert("info", "Backend server unreachable. Verify that backend is running on the configured URL.");
        return;
      }

      if (response && response.success && response.data) {
        const data = response.data;
        if (data.connected && data.status === "valid") {
          setStatus("connected", "Connected", "Active");
          showAccountDetails(data.username, data.email, data.last_verified_at);
        } else {
          setStatus("disconnected", "Not Connected", "Disconnected");
          hideAccountDetails();
        }
      } else {
        setStatus("disconnected", "Not Connected", "Disconnected");
        hideAccountDetails();
      }
    });
  }

  function setStatus(type, headerLabel, badgeLabel) {
    if (statusDot) {
      statusDot.className = `status-dot ${type}`;
    }
    if (statusText) {
      statusText.textContent = headerLabel;
    }
    if (statusBadge) {
      statusBadge.className = `status-badge ${type}`;
    }
    if (badgeText) {
      badgeText.textContent = badgeLabel || headerLabel;
    }
  }

  function setLoading(loading) {
    syncBtn.disabled = loading;
    btnSpinner.style.display = loading ? "inline-block" : "none";
    btnIcon.style.display = loading ? "none" : "inline-block";
    btnText.textContent = loading ? "Syncing with Server..." : "Sync Dice Account";
    if (loading) {
      setStatus("syncing", "Syncing...", "Syncing");
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
    return "DC";
  }

  function showAccountDetails(username, email, lastVerified) {
    detailUsername.textContent = username || "(Candidate)";
    detailEmail.textContent = email || "-";
    detailVerified.textContent = lastVerified ? formatDate(lastVerified) : "Just now";
    
    if (userAvatar) {
      userAvatar.textContent = getInitials(username, email);
    }
    
    if (disconnectedNotice) {
      disconnectedNotice.style.display = "none";
    }
    accountDetails.style.display = "flex";
  }

  function hideAccountDetails() {
    accountDetails.style.display = "none";
    if (disconnectedNotice) {
      disconnectedNotice.style.display = "flex";
    }
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
