#!/usr/bin/env bash
# =========================================================================
# Render Build Script for Dice Automation Backend
# Installs Python dependencies and Playwright Chromium binaries
# =========================================================================
set -o errexit

echo "===> Upgrading pip..."
pip install --upgrade pip

echo "===> Installing Python dependencies from requirements.txt..."
pip install -r requirements.txt

echo "===> Installing Playwright Chromium browser binaries..."
playwright install chromium

echo "===> Render build script completed successfully!"
