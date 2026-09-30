// Dice Auto-Apply Extension - Popup Controller

document.addEventListener("DOMContentLoaded", async () => {
  initPopup();
});

async function initPopup() {
  const statusBadge = document.getElementById("status-badge");
  const statusText = document.getElementById("status-text");
  const candidateName = document.getElementById("candidate-name");
  const candidateEmail = document.getElementById("candidate-email");
  const avatarInitials = document.getElementById("avatar-initials");
  const resumesCount = document.getElementById("resumes-count");
  const activeResumeName = document.getElementById("active-resume-name");
  const apiUrlInput = document.getElementById("api-url-input");

  // Load saved API endpoint
  chrome.storage.local.get(["apiBaseUrl"], items => {
    apiUrlInput.value = items.apiBaseUrl || "http://localhost:8000";
  });

  // Fetch backend status
  try {
    const res = await sendMessageAsync("GET_STATUS");
    if (res && res.status === "online") {
      statusBadge.classList.remove("offline");
      statusText.innerText = "Connected";

      candidateName.innerText = res.candidate_name || "Candidate";
      candidateEmail.innerText = res.email || "Synced";
      resumesCount.innerText = `${res.resumes_count || 0} Resumes`;

      // Initials
      const parts = (res.candidate_name || "Candidate").split(" ");
      avatarInitials.innerText = parts.map(p => p[0]).slice(0, 2).join("").toUpperCase();

      if (res.active_resume) {
        activeResumeName.innerText = res.active_resume.name || res.active_resume.file_name;
      } else {
        activeResumeName.innerText = "No resume uploaded";
      }

      loadReadyJobs();
    } else {
      markOffline();
    }
  } catch (e) {
    console.error("Could not reach backend:", e);
    markOffline();
  }

  // Setup Event Listeners
  document.getElementById("save-api-btn").addEventListener("click", async () => {
    const val = apiUrlInput.value.trim();
    if (val) {
      await sendMessageAsync("SET_API_BASE", { url: val });
      initPopup();
    }
  });

  document.getElementById("open-dashboard-btn").addEventListener("click", () => {
    chrome.tabs.create({ url: "http://localhost:5173" });
  });

  document.getElementById("autofill-btn").addEventListener("click", async () => {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (tab && tab.url && tab.url.includes("dice.com")) {
      // Trigger autofill on active Dice tab
      chrome.scripting.executeScript({
        target: { tabId: tab.id },
        func: () => {
          const btn = document.getElementById("hud-apply-btn");
          if (btn) btn.click();
        }
      });
    } else {
      // Open Dice dashboard
      chrome.tabs.create({ url: "https://www.dice.com/dashboard/jobs" });
    }
  });

  document.getElementById("refresh-jobs-btn").addEventListener("click", () => {
    loadReadyJobs();
  });
}

function markOffline() {
  const statusBadge = document.getElementById("status-badge");
  const statusText = document.getElementById("status-text");
  statusBadge.classList.add("offline");
  statusText.innerText = "Offline";
  document.getElementById("candidate-name").innerText = "Backend Offline";
  document.getElementById("candidate-email").innerText = "Ensure API is running on localhost:8000";
}

async function loadReadyJobs() {
  const container = document.getElementById("jobs-container");
  container.innerHTML = '<div class="loading-state">Fetching jobs...</div>';

  try {
    const apiBase = (await chrome.storage.local.get(["apiBaseUrl"])).apiBaseUrl || "http://localhost:8000";
    const res = await fetch(`${apiBase}/api/extension/jobs?limit=5`);
    if (!res.ok) throw new Error("Failed to load jobs");
    const jobs = await res.json();

    if (!jobs || jobs.length === 0) {
      container.innerHTML = '<div class="loading-state">No pending jobs found.</div>';
      return;
    }

    container.innerHTML = "";
    for (const job of jobs) {
      const item = document.createElement("div");
      item.className = "job-item";
      item.innerHTML = `
        <div>
          <div class="job-title" title="${escapeHtml(job.title)}">${escapeHtml(job.title)}</div>
          <div class="job-company">${escapeHtml(job.company)}</div>
        </div>
        <div class="job-score">${job.match_score ? `${job.match_score}%` : "Ready"}</div>
      `;

      item.addEventListener("click", () => {
        if (job.url) {
          sendMessageAsync("OPEN_JOB_TAB", { jobUrl: job.url, autoApply: true });
        }
      });
      container.appendChild(item);
    }
  } catch (e) {
    container.innerHTML = '<div class="loading-state">Could not load jobs</div>';
  }
}

function sendMessageAsync(action, payload = {}) {
  return new Promise((resolve, reject) => {
    chrome.runtime.sendMessage({ action, payload }, response => {
      if (chrome.runtime.lastError) {
        reject(chrome.runtime.lastError);
      } else if (response && response.success) {
        resolve(response.data);
      } else {
        reject(new Error(response?.error || "Unknown error"));
      }
    });
  });
}

function escapeHtml(text) {
  const div = document.createElement("div");
  div.innerText = text || "";
  return div.innerHTML;
}
