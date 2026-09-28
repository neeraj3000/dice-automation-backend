# =========================================================================
# Dice Automation Backend - Dockerfile for Render Deployment
# =========================================================================
FROM python:3.11-slim

# Set environment variables
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8000 \
    HEADLESS_BROWSER=true \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

# Install curl, ca-certificates, and build tools
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements and install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright browser binary and OS shared libraries
RUN playwright install --with-deps chromium

# Copy application source code
COPY . .

# Ensure storage directories exist
RUN mkdir -p /app/resumes /app/data/browser_profile

# Expose default port
EXPOSE 8000

# Start Uvicorn bound to 0.0.0.0 reading dynamic $PORT provided by Render
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
