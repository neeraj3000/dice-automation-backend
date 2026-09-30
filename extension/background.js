// Dice Auto-Apply Assistant - Background Service Worker (Manifest V3)

const DEFAULT_API_BASE = "http://localhost:8000";

async function getApiBaseUrl() {
  const result = await chrome.storage.local.get(["apiBaseUrl"]);
  return result.apiBaseUrl || DEFAULT_API_BASE;
}

// Global message handler
chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  const { action, payload } = message;

  handleMessage(action, payload, sender)
    .then(response => sendResponse({ success: true, data: response }))
    .catch(error => sendResponse({ success: false, error: error.message }));

  return true; // Keep message channel open for async response
});

async function handleMessage(action, payload, sender) {
  const apiBase = await getApiBaseUrl();

  switch (action) {
    case "GET_STATUS": {
      const res = await fetch(`${apiBase}/api/extension/status`);
      if (!res.ok) throw new Error(`Backend returned HTTP ${res.status}`);
      return await res.json();
    }

    case "GET_PROFILE": {
      const res = await fetch(`${apiBase}/api/extension/profile`);
      if (!res.ok) throw new Error(`Backend returned HTTP ${res.status}`);
      return await res.json();
    }

    case "GET_ACTIVE_RESUME": {
      const res = await fetch(`${apiBase}/api/extension/resume/active`);
      if (!res.ok) throw new Error(`Backend returned HTTP ${res.status}`);
      return await res.json();
    }

    case "FETCH_RESUME_BLOB": {
      const { resumeId } = payload;
      const res = await fetch(`${apiBase}/api/extension/resume/file/${resumeId}`);
      if (!res.ok) throw new Error(`Could not download resume: ${res.status}`);
      const blob = await res.blob();
      
      // Convert Blob to Base64 so it can pass through message port to content script
      return new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onloadend = () => {
          resolve({
            base64: reader.result,
            mimeType: blob.type || "application/pdf"
          });
        };
        reader.onerror = reject;
        reader.readAsDataURL(blob);
      });
    }

    case "ANSWER_QUESTION": {
      const res = await fetch(`${apiBase}/api/extension/answer-question`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload)
      });
      if (!res.ok) throw new Error(`Answer service failed: ${res.status}`);
      return await res.json();
    }

    case "RECORD_APPLICATION": {
      const res = await fetch(`${apiBase}/api/extension/record-application`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload)
      });
      if (!res.ok) throw new Error(`Record application failed: ${res.status}`);
      return await res.json();
    }

    case "OPEN_JOB_TAB": {
      const { jobUrl, autoApply } = payload;
      const tab = await chrome.tabs.create({ url: jobUrl });
      if (autoApply) {
        // Store pending auto-apply instruction for this tab ID
        await chrome.storage.local.set({ [`auto_apply_${tab.id}`]: true });
      }
      return { tabId: tab.id };
    }

    case "SET_API_BASE": {
      const { url } = payload;
      await chrome.storage.local.set({ apiBaseUrl: url.replace(/\/+$/, "") });
      return { apiBaseUrl: url };
    }

    default:
      throw new Error(`Unknown action: ${action}`);
  }
}

// Update icon badge when on a Dice job page
chrome.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
  if (changeInfo.status === "complete" && tab.url && tab.url.includes("dice.com")) {
    if (tab.url.includes("/job-detail/") || tab.url.includes("/apply")) {
      chrome.action.setBadgeText({ text: "DICE", tabId });
      chrome.action.setBadgeBackgroundColor({ color: "#2563eb", tabId });
    } else {
      chrome.action.setBadgeText({ text: "", tabId });
    }
  }
});
