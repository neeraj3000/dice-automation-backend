import os
import sys
from pathlib import Path
from unittest.mock import patch

backend_dir = Path(__file__).resolve().parent.parent
if str(backend_dir) not in sys.path:
    sys.path.insert(0, str(backend_dir))

import pytest
from httpx import AsyncClient, ASGITransport

from app.main import app
from app.config import settings
from app.browser.playwright_manager import playwright_manager

@pytest.fixture
def anyio_backend():
    return "asyncio"

# 1. Playwright Chromium Availability & No Runtime Auto-Install
@pytest.mark.anyio
async def test_chromium_availability_and_no_runtime_install():
    """Verifies that Chromium binary exists and ensure_browser_installed() performs checks without downloading."""
    is_installed = await playwright_manager.ensure_browser_installed()
    assert is_installed is True, "Playwright Chromium should be verified as installed."

    status = await playwright_manager.get_browser_status()
    assert status["installed"] is True
    assert status["executable_path"] is not None

# 2. FastAPI Starts and /health and /api/health Work
@pytest.mark.anyio
async def test_health_check_endpoint():
    """Verifies that /health and /api/health return HTTP 200 with status healthy."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Root health check
        res_root = await client.get("/health")
        assert res_root.status_code == 200
        data_root = res_root.json()
        assert data_root["status"] == "healthy"
        assert "database" in data_root

        # /api/health alias check
        res_api = await client.get("/api/health")
        assert res_api.status_code == 200
        data_api = res_api.json()
        assert data_api["status"] == "healthy"

# 3. Headless Browser Launch
@pytest.mark.anyio
async def test_headless_browser_launch():
    """Verifies headless browser launches and operates cleanly in server mode."""
    with patch.dict(os.environ, {"BROWSER_MODE": "server", "HEADLESS": "true"}):
        page = await playwright_manager.get_new_page(profile_id="prod_test_profile", headless=True)
        assert page is not None
        assert not page.is_closed()

        await page.goto("about:blank")
        content = await page.content()
        assert "<html" in content.lower()

        await page.close()
        await playwright_manager.close_context("prod_test_profile")

# 4. Profile Directory Creation Under BROWSER_DATA_DIR
@pytest.mark.anyio
async def test_browser_data_dir_resolution(tmp_path):
    """Verifies BROWSER_DATA_DIR environment variable is respected and creates isolated directories."""
    custom_dir = tmp_path / "custom_browser_data"
    with patch.dict(os.environ, {"BROWSER_DATA_DIR": str(custom_dir)}):
        resolved_base = playwright_manager.base_data_dir
        assert resolved_base == custom_dir
        assert custom_dir.exists()

        profile_dir = playwright_manager.get_profile_dir("user_prod_1")
        assert profile_dir.exists()
        assert profile_dir.parent == custom_dir

# 5. Linux-Compatible Paths (No Windows Drive Letters Hardcoded)
def test_no_hardcoded_windows_paths():
    """Verifies that Dockerfile, render.yaml, and settings do not contain hardcoded Windows drive paths (e.g. C:\\)."""
    dockerfile_content = Path("Dockerfile").read_text(encoding="utf-8")
    assert "C:\\" not in dockerfile_content
    assert "c:/" not in dockerfile_content.lower()

    render_yaml_content = Path("render.yaml").read_text(encoding="utf-8")
    assert "C:\\" not in render_yaml_content
    assert "c:/" not in render_yaml_content.lower()

    env_example_content = Path(".env.example").read_text(encoding="utf-8")
    assert "C:\\" not in env_example_content
    assert "c:/" not in env_example_content.lower()

# 6. Production Mode Configuration
def test_production_mode_configuration():
    """Verifies production defaults: BROWSER_MODE=server, HEADLESS=true."""
    with patch.dict(os.environ, {"BROWSER_MODE": "server", "HEADLESS": "true"}):
        assert playwright_manager.browser_mode == "server"
        assert playwright_manager.headless is True
        assert playwright_manager.is_cloud_mode is True
