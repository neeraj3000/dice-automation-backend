# ⚡ Dice Auto-Apply Chrome Extension (Case 2 Architecture)

This Chrome Extension implements **Client-Side Automation (Case 2)** for Dice.com job applications.

Instead of running heavy, expensive headless browsers in the cloud, this extension runs directly inside the user's Google Chrome browser:
- **Zero Bot Detection / No Cloudflare IP bans**: Uses your natural home/office IP and existing logged-in Dice session.
- **Zero Cloud Server RAM**: Your browser executes the typing, form filling, and resume attachments.
- **1-Click Autofill**: Automatically fills candidate information, selects work authorization, uploads your matched resume, and answers screening questions.

---

## 🚀 How to Install in Google Chrome (Takes 30 Seconds)

1. Open **Google Chrome** on your computer.
2. Navigate to: `chrome://extensions/`
3. In the top-right corner, toggle **Developer mode** to **ON**.
4. In the top-left corner, click the **"Load unpacked"** button.
5. In the file dialog, select this folder:
   ```
   c:\Users\Kalki\Documents\code\dice-automation-backend\extension
   ```
6. The **Dice Auto-Apply Assistant** icon (⚡) will appear in your Chrome toolbar!
7. Pin the extension to your toolbar for instant access.

---

## 🎯 How to Use

1. Ensure the backend is running (`http://localhost:8000`).
2. Click the ⚡ extension icon in your Chrome toolbar:
   - You will see a green **"Connected"** badge.
   - Your candidate name and active resume from the backend will be displayed.
3. Open any job on **Dice.com**:
   - A floating **Dice Auto-Apply HUD** will appear in the bottom-right corner of the job page.
   - Click **"⚡ Auto-Fill Application"**.
   - The assistant will automatically click Easy Apply, fill all personal details, select your work authorization, attach your resume, and answer custom questions.
   - Once all fields are ready, you can review and submit!
