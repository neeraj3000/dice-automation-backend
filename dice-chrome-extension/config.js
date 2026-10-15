// Apply2Hire Chrome Extension - Central Environment Configuration
// Toggle ENV between 'local' and 'production' for build releases,
// or change individual URLs dynamically via the Settings tab in the popup.

const APP_CONFIG = {
  // Active environment: 'local' | 'production'
  // ENV: "local",
  ENV: "production",

  environments: {
    local: {
      backendUrl: "http://localhost:8000",
      webAppUrl: "http://localhost:5173",
    },
    production: {
      backendUrl: "https://app-apply2hire-backend-cus-fvdbewb7gkczgecb.centralus-01.azurewebsites.net",
      webAppUrl: "http://localhost:5173",
    },
  },

  // Helper to get active default URLs
  getDefaults() {
    const active = this.environments[this.ENV] || this.environments.local;
    return {
      backendUrl: active.backendUrl,
      webAppUrl: active.webAppUrl,
    };
  },
};

// Export for ES modules/Node if needed, while keeping global for extension scripts
if (typeof module !== "undefined" && module.exports) {
  module.exports = APP_CONFIG;
}
