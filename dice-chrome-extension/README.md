# Dice Account Session Sync — Chrome Extension

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
   c:\Users\Kalki\Documents\code\dice-automation-backend\dice-chrome-extension
   ```
6. The **Dice Account Session Sync** extension icon (⚄) will appear in your Chrome toolbar.
   *(Tip: Click the puzzle piece icon in Chrome and click the Pin icon next to "Dice Account Session Sync" to keep it visible).*

---

### Step 2: Open Dice and Log In

1. Open a regular Chrome tab and navigate to [https://www.dice.com/dashboard/login](https://www.dice.com/dashboard/login).
2. Sign in to your Dice account using your preferred method:
   - Email & Password
   - "Continue with Google"
   - Complete any CAPTCHA or Two-Factor Authentication (SMS / Authenticator app) if prompted.
3. Confirm you have landed on your Dice Dashboard (`https://www.dice.com/dashboard`) or candidate profile page.

---

### Step 3: Synchronize with Backend

1. Click the **Dice Session Sync** extension icon (⚄) in your Chrome toolbar.
2. In the popup:
   - The status badge will show the current backend connection status.
   - If using a remote cloud backend (e.g. Render), click **⚙ Backend Connection Settings** and enter your backend URL (e.g., `https://your-app.onrender.com`), then click **Save Backend URL**. (Defaults to `http://localhost:8000` for local development).
3. Click the primary button: **Sync Dice Account**.
4. The extension will:
   - Capture your authenticated `.dice.com` tokens.
   - Transmit them securely to your backend's `/api/dice/session/sync` endpoint.
   - Display a green confirmation banner: `Successfully synced! Dice account '<your_name>' is now connected.`
   - Display your verified candidate email and timestamp.

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
