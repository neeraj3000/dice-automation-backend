# Apply2Hire – Dice Session Sync — Chrome Extension

A lightweight, secure **Manifest V3** companion extension for the **Dice Job Application Automation** system.

---

## 🎯 Purpose & Architecture

- **Extension Role**: The extension is **strictly** responsible for capturing authenticated Dice session credentials (cookies and AWS Cognito localStorage tokens) from your active Chrome browser and synchronizing them with your backend.
- **Automation Role**: The extension does **NOT** run any scraping or job application automation. All automation remains executed by **Playwright + Chromium** running headlessly on your backend server.
- **Privacy & Security**:
  - **Zero Passwords**: The extension never collects, requests, or reads passwords.
  - **No Token Exposure**: Authentication tokens are never displayed in the extension UI.
  - **Minimal Data**: Only genuine `.dice.com` authentication tokens are captured; advertising, tracking, and unrelated browser data are ignored.
  - **Encryption**: The backend encrypts all sensitive tokens at rest using AES-128-CBC / Fernet (`SESSION_ENCRYPTION_KEY`).

---

## 🚀 Setup & Installation (Step-by-Step)

### Step 1: Load the Unpacked Extension in Chrome

1. Open Google Chrome.
2. Navigate to `chrome://extensions/` in your address bar.
3. In the top-right corner, toggle **Developer mode** to **ON**.
4. Click the **Load unpacked** button in the top-left toolbar.
5. In the file picker, select the `dice-chrome-extension` directory:
   ```
   <the folder you unzipped>/dice-chrome-extension
   ```
6. The **Apply2Hire – Dice Session Sync** extension icon (green "A") will appear in your Chrome toolbar.
   *(Tip: Click the puzzle piece icon in Chrome and click the Pin icon next to "Apply2Hire – Dice Session Sync" to keep it visible).*

---

### Step 2: Open Apply2Hire and Sign In

1. Open your browser and navigate to your Apply2Hire web dashboard (e.g., `http://localhost:5173` or `https://app.apply2hire.com`).
2. Sign in to your Apply2Hire account.
3. The extension automatically detects your authenticated Apply2Hire session in the background via the browser cookie bridge — **no pairing codes or manual tokens needed**.

---

### Step 3: Open Dice and Sign In

1. Open a regular Chrome tab and navigate to [https://www.dice.com/dashboard/login](https://www.dice.com/dashboard/login).
2. Sign in to your Dice account using your preferred method (Email, Google, 2FA, CAPTCHA).
3. Confirm you have landed on your Dice Dashboard (`https://www.dice.com/dashboard`).

---

### Step 4: Click Sync in Extension

1. Click the **Apply2Hire** extension icon in your Chrome toolbar.
2. In the popup:
   - Your Apply2Hire user profile will be automatically detected and displayed as **ACTIVE**.
   - Click the primary button: **Sync Dice Account**.
3. The extension captures your authenticated Dice tokens and securely registers them with your Apply2Hire account.

---

### Step 4: Verify in Dashboard

1. Open your Dice Automation Web Dashboard (e.g., `http://localhost:5173` or your production frontend).
2. Look at the top navigation bar or settings card.
3. The status indicator will display:
   ```
   Dice Connected (Candidate: your.email@example.com)
   ```
4. You are now ready to perform automated job searches, resume matching, and job applications!

---

## 🔒 Security & Data Collection Policy

| Data Category | Handled by Extension | Destination |
| :--- | :--- | :--- |
| **Passwords** | ❌ **Never** captured or requested | None |
| **Browsing History** | ❌ **Never** accessed | None |
| **Third-Party Cookies** | ❌ Filtered out and ignored | None |
| **Analytics/Marketing Cookies** | ❌ Filtered out (`_ga`, `_gid`, `_uetsid`, etc.) | None |
| **Dice Auth Cookies** | ✅ Captured (`identity`, `refreshToken`, `cognito`) | Backend API (encrypted at rest) |
| **Cognito localStorage** | ✅ Captured (`idToken`, `accessToken`, `lastAuthUser`) | Backend API (encrypted at rest) |
