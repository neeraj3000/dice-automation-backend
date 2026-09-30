// Dice Auto-Apply Assistant - Web App Bridge Script
// Injected into the React frontend dashboard (localhost:5173 or production domain)
// Listens for "Apply" events triggered by the user in Job Explorer and opens a browser tab

(function () {
  // Announce to the web app that the Dice Extension is active
  window.addEventListener("DOMContentLoaded", () => {
    window.postMessage({ type: "DICE_EXTENSION_READY", version: "1.0.0" }, "*");
  });

  // Listen for Apply commands from Job Explorer
  window.addEventListener("message", async (event) => {
    if (!event.data || typeof event.data !== "object") return;

    if (event.data.type === "DICE_AUTOMATION_APPLY") {
      const { jobUrl, jobId, jobTitle, company } = event.data;
      if (!jobUrl) return;

      console.log("[Dice Extension Bridge] Received apply trigger for:", jobTitle || jobUrl);

      // Tell background service worker to open the tab and trigger auto-apply
      chrome.runtime.sendMessage({
        action: "OPEN_JOB_TAB",
        payload: {
          jobUrl: jobUrl,
          jobId: jobId,
          autoApply: true
        }
      }, (response) => {
        if (response && response.success) {
          window.postMessage({
            type: "DICE_APPLICATION_TAB_OPENED",
            jobId: jobId,
            tabId: response.data?.tabId
          }, "*");
        }
      });
    }
  });
})();
