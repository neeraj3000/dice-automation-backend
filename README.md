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
Copy .env.example to .env and fill in your connection details:
`ash
cp .env.example .env
`

### 3. Run with Virtual Environment
`powershell
# Activate virtual environment
.\.venv\Scripts\Activate.ps1

# Install dependencies (if not already installed)
pip install -r requirements.txt

# Start FastAPI server (Port 8000)
uvicorn app.main:app --reload --port 8000
`

### 4. Interactive API Docs
Visit: http://localhost:8000/docs

---

## Directory Structure
- pp/ - FastAPI routes, models, schemas, and services
  - pi/ - REST API endpoints (esumes, jobs, pplications, search, settings)
  - rowser/ - Playwright automation and dice session manager
  - services/ - Business logic (matching, resume parsing, application runner)
- esumes/ - Stored uploaded candidate resumes (.pdf, .docx)
- sample_resumes/ - Sample candidate resumes for testing
- data/ - Persistent storage for Playwright browser profiles
- scripts/ - Utility scripts (e.g. login_dice.py)
- 	ests/ - Backend test suites
