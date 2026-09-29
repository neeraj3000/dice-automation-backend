import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from httpx import AsyncClient, ASGITransport
from app.main import app
from app.database import connect_db, close_db, get_database
from app.services.settings_service import settings_service

@pytest.fixture(autouse=True)
async def setup_db():
    await connect_db()
    yield
    # Cleanup test mutations after test execution
    try:
        db = get_database()
        if db is not None:
            await db.app_settings.update_one(
                {},
                {"$set": {"dice_session_connected": False, "dice_username": "", "dice_cookies_count": 0, "saved_cookies": []}}
            )
    except Exception:
        pass
    await close_db()

@pytest.mark.anyio
async def test_dice_status_generic_no_hardcoding():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Check health endpoint includes dice_session
        res = await client.get("/health")
        assert res.status_code == 200
        data = res.json()
        assert "dice_session" in data
        dice_sess = data["dice_session"]
        assert "is_connected" in dice_sess
        assert "status" in dice_sess
        assert "username" in dice_sess
        # Verify no obsolete hardcoded "VS (Veera Sekhar)"
        assert dice_sess["username"] != "VS (Veera Sekhar)"

        # Check /settings/dice-status
        status_res = await client.get("/settings/dice-status")
        assert status_res.status_code == 200
        status_data = status_res.json()
        assert "is_connected" in status_data
        assert "username" in status_data
        assert status_data["username"] != "VS (Veera Sekhar)"

@pytest.mark.anyio
async def test_session_import_generic_username():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Import with no username -> should resolve dynamically, not hardcoded
        cookies = [{"name": "test_auth_token", "value": "xyz123", "domain": ".dice.com", "path": "/"}]
        res = await client.post("/settings/import-dice-session", json={"cookies": cookies})
        assert res.status_code == 200
        import_data = res.json()
        assert import_data["status"] == "success"
        assert import_data["is_connected"] is True
        assert import_data["username"] != "VS (Veera Sekhar)"
        assert len(import_data["username"]) > 0

        # Import with custom generic username
        res2 = await client.post(
            "/settings/import-dice-session",
            json={"cookies": cookies, "username": "Alex Johnson"}
        )
        assert res2.status_code == 200
        assert res2.json()["username"] == "Alex Johnson"

@pytest.mark.anyio
async def test_verify_session_on_startup():
    # Verify startup function executes and prints banner without error
    status = await settings_service.verify_session_on_startup()
    assert isinstance(status, dict)
    assert "is_connected" in status
    assert status["username"] != "VS (Veera Sekhar)"

@pytest.mark.anyio
async def test_browser_status_and_verification():
    from app.browser.playwright_manager import playwright_manager
    # Test ensure_browser_installed returns True (since Chromium is installed locally)
    is_installed = await playwright_manager.ensure_browser_installed()
    assert is_installed is True

    # Test /settings/browser-status endpoint
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.get("/settings/browser-status")
        assert res.status_code == 200
        data = res.json()
        assert "installed" in data
        assert data["installed"] is True
        assert "executable_path" in data
        assert "headless" in data
        assert "is_cloud_mode" in data
