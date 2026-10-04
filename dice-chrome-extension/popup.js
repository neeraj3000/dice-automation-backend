// Dice Session Sync - Popup Script

document.addEventListener("DOMContentLoaded", async () => {
  const statusDot = document.getElementById("statusDot");
  const statusText = document.getElementById("statusText");
  const accountDetails = document.getElementById("accountDetails");
  const detailUsername = document.getElementById("detailUsername");
  const detailEmail = document.getElementById("detailEmail");
  const detailVerified = document.getElementById("detailVerified");

  const alertBox = document.getElementById("alertBox");
  const alertIcon = document.getElementById("alertIcon");
  const alertMessage = document.getElementById("alertMessage");

  const syncBtn = document.getElementById("syncBtn");
  const btnSpinner = document.getElementById("btnSpinner");
  const btnIcon = document.getElementById("btnIcon");
  const btnText = document.getElementById("btnText");

  const openDiceBtn = document.getElementById("openDiceBtn");
  const settingsToggle = document.getElementById("settingsToggle");
  const settingsPanel = document.getElementById("settingsPanel");
  const settingsArrow = document.getElementById("settingsArrow");
  const backendUrlInput = document.getElementById("backendUrlInput");
  const saveSettingsBtn = document.getElementById("saveSettingsBtn");

  // Load configured backend URL
  chrome.storage.local.get(["backendUrl"], (result) => {
    backendUrlInput.value = result.backendUrl || "http://localhost:8000";
  });

  // Check current backend session status on load
  await checkSessionStatus();

  // "Open Dice.com" link
  openDiceBtn.addEventListener("click", (e) => {
    e.preventDefault();
    chrome.tabs.create({ url: "https://www.dice.com/dashboard" });
  });

  // Expand / collapse backend settings
  settingsToggle.addEventListener("click", () => {
    const isHidden = settingsPanel.style.display === "none";
    settingsPanel.style.display = isHidden ? "flex" : "none";
    settingsArrow.textContent = isHidden ? "▲" : "▼";
  });

  // Save backend URL
  saveSettingsBtn.addEventListener("click", () => {
    let url = backendUrlInput.value.trim().replace(/\/+$/, "");
    if (!url) url = "http://localhost:8000";
    chrome.storage.local.set({ backendUrl: url }, () => {
      showAlert("success", "Backend URL saved! Re-checking status...");
      checkSessionStatus();
    });
  });

  // "Sync Dice Account" click handler
  syncBtn.addEventListener("click", async () => {
    setLoading(true);
    hideAlert();

    try {
      chrome.runtime.sendMessage({ action: "SYNC_DICE_SESSION" }, (response) => {
        setLoading(false);

        if (chrome.runtime.lastError) {
          showAlert("error", `Extension communication error: ${chrome.runtime.lastError.message}`);
          setStatus("error", "Error");
          return;
        }

        if (response && response.success) {
          const d = response.data || {};
          const userStr = d.username || d.email || "Candidate";
          showAlert("success", `Successfully synced! Dice account '${userStr}' is now connected to your backend.`);
          setStatus("connected", "Connected");
          showAccountDetails(d.username, d.email, d.last_verified_at || new Date().toISOString());
        } else {
          const err = (response && (response.error || (response.data && response.data.message))) || "Failed to sync session.";
          showAlert("error", err);
          setStatus("disconnected", "Not Connected");
        }
      });
    } catch (err) {
      setLoading(false);
      showAlert("error", `Unexpected error: ${err.message}`);
      setStatus("error", "Error");
    }
  });

  // Query backend for session health
  async function checkSessionStatus() {
    setStatus("syncing", "Checking...");
    chrome.runtime.sendMessage({ action: "GET_SESSION_STATUS" }, (response) => {
      if (chrome.runtime.lastError) {
        setStatus("error", "Offline");
        showAlert("info", "Backend unreachable. Ensure backend server is running.");
        return;
      }

      if (response && response.success && response.data) {
        const data = response.data;
        if (data.connected && data.status === "valid") {
          setStatus("connected", "Connected");
          // If detailed user info is available
          showAccountDetails(data.username, data.email, data.last_verified_at);
        } else {
          setStatus("disconnected", "Not Connected");
          hideAccountDetails();
        }
      } else {
        setStatus("disconnected", "Not Connected");
        hideAccountDetails();
      }
    });
  }

  function setStatus(type, label) {
    statusDot.className = `status-dot ${type}`;
    statusText.textContent = label;
  }

  function setLoading(loading) {
    syncBtn.disabled = loading;
    btnSpinner.style.display = loading ? "inline-block" : "none";
    btnIcon.style.display = loading ? "none" : "inline-block";
    btnText.textContent = loading ? "Syncing with Server..." : "Sync Dice Account";
    if (loading) {
      setStatus("syncing", "Syncing...");
    }
  }

  function showAlert(type, text) {
    alertBox.className = `alert ${type}`;
    alertIcon.textContent = type === "success" ? "✓" : (type === "error" ? "⚠" : "ℹ");
    alertMessage.textContent = text;
    alertBox.style.display = "flex";
  }

  function hideAlert() {
    alertBox.style.display = "none";
  }

  function showAccountDetails(username, email, lastVerified) {
    detailUsername.textContent = username || "(Candidate)";
    detailEmail.textContent = email || "-";
    detailVerified.textContent = lastVerified ? formatDate(lastVerified) : "Just now";
    accountDetails.style.display = "flex";
  }

  function hideAccountDetails() {
    accountDetails.style.display = "none";
  }

  function formatDate(isoStr) {
    try {
      const dt = new Date(isoStr);
      return dt.toLocaleDateString() + " " + dt.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    } catch (_) {
      return isoStr;
    }
  }
});
