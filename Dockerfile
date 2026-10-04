# =========================================================================
# Dice Automation Backend - Dockerfile for Production Deployment
# Cloud-Agnostic: Deployable to Render, AWS ECS, GCP Cloud Run, Azure, Docker
# =========================================================================
FROM python:3.11-slim

# Set environment variables
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8000 \
    BROWSER_MODE=server \
    HEADLESS=true \
    BROWSER_DATA_DIR=/app/data/browser_profiles \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

# 1. Install system utilities and SSL certificates
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# 2. Copy and install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 3. Install Playwright Chromium and required OS shared libraries during BUILD TIME
RUN python -m playwright install --with-deps chromium

# 4. Copy application source code (.dockerignore excludes .env, local data/, .git)
COPY . .

# 5. Ensure runtime directories exist with permissive write permissions for container user
RUN mkdir -p /app/resumes /app/data/browser_profiles /app/data/browser_profile \
    && chmod -R 777 /app/data /app/resumes /ms-playwright

# Expose default port
EXPOSE 8000

# 6. Start FastAPI using dynamic $PORT assigned by Render or container orchestrator
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
