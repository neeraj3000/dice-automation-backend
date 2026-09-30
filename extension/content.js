// Dice Auto-Apply Assistant - Content Script (In-Browser Automation)

(function () {
  let profile = null;
  let backendStatus = null;
  let hudElement = null;

  // Initialize on page load
  window.addEventListener("load", () => {
    setTimeout(initAssistant, 1200);
  });

  async function initAssistant() {
    try {
      const statusRes = await sendMessageAsync("GET_STATUS");
      if (statusRes && statusRes.status === "online") {
        backendStatus = statusRes;
        const profRes = await sendMessageAsync("GET_PROFILE");
        profile = profRes;
        injectHud();
        checkAutoApplyTrigger();
      }
    } catch (e) {
      console.debug("[Dice Assistant] Backend not reachable:", e.message);
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

  // Inject floating HUD
  function injectHud() {
    if (document.getElementById("dice-assistant-hud")) return;

    hudElement = document.createElement("div");
    hudElement.id = "dice-assistant-hud";

    const candidateName = backendStatus?.candidate_name || "Connected Candidate";
    const candidateEmail = backendStatus?.email || "Synced with Backend";
    const resumeName = backendStatus?.active_resume?.name || "Active Resume";

    hudElement.innerHTML = `
      <div class="hud-header">
        <div class="hud-brand">
          <div class="hud-brand-icon">⚡</div>
          <span>Dice Auto-Apply</span>
        </div>
        <div class="hud-controls">
          <button class="hud-btn-icon" id="hud-minimize-btn" title="Minimize">−</button>
        </div>
      </div>
      <div class="hud-body">
        <div class="hud-status-badge">
          <span class="hud-status-dot"></span>
          <span>Online & Ready</span>
        </div>
        <div class="hud-candidate-info">
          <div class="hud-candidate-name">${escapeHtml(candidateName)}</div>
          <div class="hud-candidate-email">${escapeHtml(candidateEmail)} • ${escapeHtml(resumeName)}</div>
        </div>
        <button class="hud-action-btn" id="hud-apply-btn">
          <span>⚡ Auto-Fill Application</span>
        </button>
        <div class="hud-progress-log" id="hud-log">Ready to assist application</div>
      </div>
      <div class="hud-footer">
        <span>Dice Automation</span>
        <a class="hud-link" id="hud-dashboard-link" href="http://localhost:5173" target="_blank">Open Dashboard →</a>
      </div>
    `;

    document.body.appendChild(hudElement);

    // Event listeners
    document.getElementById("hud-minimize-btn").addEventListener("click", () => {
      hudElement.classList.toggle("minimized");
      const btn = document.getElementById("hud-minimize-btn");
      btn.innerText = hudElement.classList.contains("minimized") ? "+" : "−";
    });

    document.getElementById("hud-apply-btn").addEventListener("click", () => {
      runAutoFill();
    });
  }

  function setLog(message, isDone = false) {
    const logEl = document.getElementById("hud-log");
    if (logEl) {
      logEl.innerText = message;
      if (isDone) {
        logEl.style.color = "#10b981";
        logEl.style.borderLeftColor = "#10b981";
      } else {
        logEl.style.color = "#38bdf8";
        logEl.style.borderLeftColor = "#38bdf8";
      }
    }
  }

  // Check if this tab was opened with an auto-apply flag from the web app
  async function checkAutoApplyTrigger() {
    chrome.storage.local.get(null, items => {
      for (const k in items) {
        if (k.startsWith("auto_apply_") && items[k]) {
          chrome.storage.local.remove(k);
          setTimeout(runAutoFill, 1500);
          break;
        }
      }
    });
  }

  // Core in-browser autofill runner
  async function runAutoFill() {
    const applyBtn = document.getElementById("hud-apply-btn");
    if (applyBtn) applyBtn.disabled = true;

    try {
      setLog("Checking application status...");

      // 1. Look for Easy Apply button on page if form is not already visible
      let modal = findApplicationContainer();
      if (!modal) {
        const triggerBtn = findApplyButton();
        if (triggerBtn) {
          setLog("Clicking Easy Apply...");
          triggerBtn.click();
          await sleep(2000);
          modal = findApplicationContainer();
        }
      }

      // 2. Autofill Personal Information
      setLog("Filling candidate information...");
      const filledFields = fillPersonalInfo(modal || document);

      // 3. Handle Work Authorization & Experience
      setLog("Selecting work authorization...");
      fillWorkAuthorization(modal || document);

      // 4. Attach or Select Resume
      setLog("Checking resume upload...");
      await handleResumeAttachment(modal || document);

      // 5. Answer Screener Questions
      setLog("Evaluating screener questions...");
      await answerQuestions(modal || document);

      setLog("✅ Form ready! Review & Submit.", true);

      // 6. Record Application to backend
      const jobTitle = extractJobTitle();
      const company = extractCompany();
      await sendMessageAsync("RECORD_APPLICATION", {
        job_title: jobTitle,
        company: company,
        job_url: window.location.href,
        status: "REVIEW",
        notes: `Autofilled fields: ${filledFields.join(", ")}`
      });

    } catch (err) {
      console.error("[Dice Assistant] Error during autofill:", err);
      setLog(`Error: ${err.message}`);
    } finally {
      if (applyBtn) applyBtn.disabled = false;
    }
  }

  function findApplyButton() {
    const selectors = [
      'button[data-cy="apply-button"]',
      'button:has-text("Apply Now")',
      'button:has-text("Easy Apply")',
      'apply-button-wc',
      '.apply-button',
      '[aria-label*="Apply"]'
    ];
    for (const s of selectors) {
      try {
        const el = document.querySelector(s);
        if (el && isVisible(el)) return el;
      } catch (e) {}
    }
    // Search by text content
    const buttons = Array.from(document.querySelectorAll("button, a"));
    return buttons.find(b => {
      const t = (b.innerText || "").toLowerCase();
      return (t.includes("apply now") || t.includes("easy apply")) && isVisible(b);
    });
  }

  function findApplicationContainer() {
    const selectors = [
      'div[role="dialog"]',
      '.modal-content',
      '#apply-modal',
      '.application-container',
      'form[name*="apply"]'
    ];
    for (const s of selectors) {
      const el = document.querySelector(s);
      if (el && isVisible(el)) return el;
    }
    return null;
  }

  function fillPersonalInfo(root) {
    if (!profile) return [];
    const filled = [];

    const fieldMap = [
      { keys: ["first_name", "firstname", "first name", "fname"], val: profile.first_name },
      { keys: ["last_name", "lastname", "last name", "lname"], val: profile.last_name },
      { keys: ["email", "e-mail", "email address"], val: profile.email },
      { keys: ["phone", "mobile", "cell", "telephone"], val: profile.phone },
      { keys: ["city", "town"], val: profile.city },
      { keys: ["state", "province", "region"], val: profile.state },
      { keys: ["zip", "postal", "zipcode"], val: profile.zip_code },
      { keys: ["linkedin", "linkedin_url"], val: profile.linkedin_url },
      { keys: ["github", "github_url"], val: profile.github_url },
      { keys: ["portfolio", "website"], val: profile.portfolio_url }
    ];

    const inputs = Array.from(root.querySelectorAll("input, textarea"));

    for (const input of inputs) {
      if (input.type === "hidden" || input.type === "submit" || input.type === "file") continue;
      
      const id = (input.id || "").toLowerCase();
      const name = (input.name || "").toLowerCase();
      const placeholder = (input.placeholder || "").toLowerCase();
      const aria = (input.getAttribute("aria-label") || "").toLowerCase();
      const label = findLabelText(input).toLowerCase();

      const combined = `${id} ${name} ${placeholder} ${aria} ${label}`;

      for (const item of fieldMap) {
        if (!item.val) continue;
        const matches = item.keys.some(k => combined.includes(k));
        if (matches) {
          // If field already contains valid email, don't overwrite
          if (input.value && input.value.trim().length > 0) {
            filled.push(item.keys[0]);
            break;
          }
          setInputValue(input, item.val);
          filled.push(item.keys[0]);
          break;
        }
      }
    }

    return filled;
  }

  function fillWorkAuthorization(root) {
    if (!profile) return;
    const authText = (profile.work_authorization || "US Citizen").toLowerCase();

    // Check dropdowns
    const selects = Array.from(root.querySelectorAll("select"));
    for (const select of selects) {
      const label = findLabelText(select).toLowerCase();
      if (label.includes("authorized") || label.includes("work authorization") || label.includes("sponsorship")) {
        for (const opt of Array.from(select.options)) {
          const optText = opt.text.toLowerCase();
          if (authText.includes("citizen") && optText.includes("citizen")) {
            select.value = opt.value;
            dispatchChange(select);
            break;
          } else if (authText.includes("green") && (optText.includes("green card") || optText.includes("permanent"))) {
            select.value = opt.value;
            dispatchChange(select);
            break;
          }
        }
      }
    }

    // Check radio buttons
    const radios = Array.from(root.querySelectorAll('input[type="radio"]'));
    for (const radio of radios) {
      const rLabel = findLabelText(radio).toLowerCase();
      if (authText.includes("citizen") && (rLabel.includes("yes") || rLabel.includes("authorized") || rLabel.includes("citizen"))) {
        radio.checked = true;
        dispatchChange(radio);
      }
    }
  }

  async function handleResumeAttachment(root) {
    const fileInput = root.querySelector('input[type="file"]');
    if (!fileInput) return;

    try {
      const activeRes = await sendMessageAsync("GET_ACTIVE_RESUME");
      if (!activeRes || !activeRes.id) return;

      const fileData = await sendMessageAsync("FETCH_RESUME_BLOB", { resumeId: activeRes.id });
      if (!fileData || !fileData.base64) return;

      // Create File from Base64
      const resFile = base64ToFile(fileData.base64, activeRes.file_name || "Resume.pdf", fileData.mimeType);

      // Attach via DataTransfer API
      const dt = new DataTransfer();
      dt.items.add(resFile);
      fileInput.files = dt.files;
      dispatchChange(fileInput);
      setLog(`Attached: ${resFile.name}`);
    } catch (e) {
      console.debug("[Dice Assistant] Resume upload notice:", e);
    }
  }

  async function answerQuestions(root) {
    const questionContainers = Array.from(root.querySelectorAll('.form-group, .question-wrapper, [data-cy*="question"]'));
    const jobTitle = extractJobTitle();
    const company = extractCompany();

    for (const container of questionContainers) {
      const label = container.querySelector("label, .label, legend");
      if (!label) continue;
      const qText = label.innerText.trim();
      if (!qText || qText.length < 5) continue;

      const input = container.querySelector("input:not([type='file']):not([type='hidden']), textarea, select");
      if (!input || (input.value && input.value.trim().length > 0)) continue;

      try {
        const resp = await sendMessageAsync("ANSWER_QUESTION", {
          question_text: qText,
          job_title: jobTitle,
          company: company
        });
        if (resp && resp.answer) {
          setInputValue(input, resp.answer);
        }
      } catch (e) {
        console.debug("Could not answer question:", qText, e);
      }
    }
  }

  function setInputValue(input, value) {
    input.focus();
    input.value = value;
    dispatchChange(input);
  }

  function dispatchChange(element) {
    element.dispatchEvent(new Event("input", { bubbles: true }));
    element.dispatchEvent(new Event("change", { bubbles: true }));
  }

  function findLabelText(el) {
    if (el.labels && el.labels.length) return el.labels[0].innerText;
    let parent = el.parentElement;
    for (let i = 0; i < 3 && parent; i++) {
      const lbl = parent.querySelector("label");
      if (lbl) return lbl.innerText;
      parent = parent.parentElement;
    }
    return "";
  }

  function extractJobTitle() {
    const el = document.querySelector('h1[data-cy="jobTitle"], .job-header h1, h1');
    return el ? el.innerText.trim() : "Job Position";
  }

  function extractCompany() {
    const el = document.querySelector('a[data-cy="companyName"], .company-name, [data-cy="company"]');
    return el ? el.innerText.trim() : "Company";
  }

  function base64ToFile(dataUrl, filename, mimeType) {
    const arr = dataUrl.split(",");
    const bstr = atob(arr[1]);
    let n = bstr.length;
    const u8arr = new Uint8Array(n);
    while (n--) {
      u8arr[n] = bstr.charCodeAt(n);
    }
    return new File([u8arr], filename, { type: mimeType });
  }

  function isVisible(elem) {
    return !!(elem.offsetWidth || elem.offsetHeight || elem.getClientRects().length);
  }

  function sleep(ms) {
    return new Promise(resolve => setTimeout(resolve, ms));
  }

  function escapeHtml(text) {
    const div = document.createElement("div");
    div.innerText = text;
    return div.innerHTML;
  }
})();
