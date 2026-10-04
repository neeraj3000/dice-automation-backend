# Dice Automation Backend

FastAPI backend service powering the Dice Job Application Automation platform. Handles candidate resume ingestion, intelligent LLM & algorithmic matching, MongoDB persistence, and Playwright browser automation for job applications.

---

## Tech Stack
- **Framework**: FastAPI (Async Python)
- **Database**: MongoDB (Local or MongoDB Atlas) via motor
- **Validation**: Pydantic v2
- **Document Processing**: PyMuPDF (pymupdf), python-docx
- **AI / LLM**: OpenAI API (GPT-4o-mini)
- **Browser Automation**: Playwright (persistent session profile)

---

## Quick Start

### 1. Prerequisites
- Python 3.10+
- MongoDB running locally on 127.0.0.1:27017 or MongoDB Atlas connection string

### 2. Environment Setup
Copy `.env.example` to `.env` and fill in your connection details:
```bash
cp .env.example .env
```

### 3. Run with Virtual Environment
```powershell
# Activate virtual environment
.\.venv\Scripts\Activate.ps1

# Install dependencies (if not already installed)
pip install -r requirements.txt

# Start FastAPI server (Port 8000)
uvicorn app.main:app --reload --port 8000
```

### 4. Interactive API Docs
Visit: `http://localhost:8000/docs`

---

## Directory Structure
- `app/` - FastAPI routes, models, schemas, and services
  - `api/` - REST API endpoints (resumes, jobs, applications, search, settings, dice_session)
  - `browser/` - Playwright automation and browser manager
  - `services/` - Business logic (matching, session store, session restorer)
- `dice-chrome-extension/` - Manifest V3 Chrome Extension for secure session syncing
- `resumes/` - Stored uploaded candidate resumes (.pdf, .docx)
- `sample_resumes/` - Sample candidate resumes for testing
- `data/` - Persistent storage for Playwright browser profiles
- `scripts/` - Utility scripts (e.g. login_dice.py)
- `tests/` - Backend test suites

---

## 🧩 Dice Chrome Extension (Session Synchronization)

A lightweight **Manifest V3** companion extension is provided in `dice-chrome-extension/` to synchronize your real Dice account session with the backend without sharing passwords.

### Quick Setup:
1. **Load Unpacked**: Open Google Chrome, go to `chrome://extensions/`, enable **Developer mode**, click **Load unpacked**, and select the `dice-chrome-extension/` directory.
2. **Log into Dice**: In normal Chrome, navigate to [https://www.dice.com/dashboard/login](https://www.dice.com/dashboard/login) and log in (Email/Password, Google login, or 2FA).
3. **Click Sync**: Click the **Dice Session Sync** icon (⚄) in your Chrome toolbar and press **Sync Dice Account**.
4. **Verify in Dashboard**: Open your Web Dashboard (`http://localhost:5173`). The status indicator will display **Dice Connected**.
