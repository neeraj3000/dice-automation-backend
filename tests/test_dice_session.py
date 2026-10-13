import sys
import json
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
    settings_service.clear_local_session()
    # Cleanup MongoDB legacy session fields
    try:
        db = get_database()
        if db is not None:
            await db.app_settings.update_one(
                {},
                {"$unset": {
                    "saved_cookies": "",
                    "dice_session_connected": "",
                    "dice_username": "",
                    "dice_cookies_count": "",
                    "dice_last_verified": ""
                }}
            )
    except Exception:
        pass

    yield

    # Cleanup test mutations after test execution
    settings_service.clear_local_session()
    try:
        db = get_database()
        if db is not None:
            await db.app_settings.update_one(
                {},
                {"$unset": {
                    "saved_cookies": "",
                    "dice_session_connected": "",
                    "dice_username": "",
                    "dice_cookies_count": "",
                    "dice_last_verified": ""
                }}
            )
    except Exception:
        pass
    await close_db()

@pytest.fixture
async def auth_headers():
    from app.core.security import create_access_token
    from bson import ObjectId
    user_id = str(ObjectId())
    db = get_database()
    if db is not None:
        await db.users.insert_one({"_id": ObjectId(user_id), "email": "dicesession@example.com", "name": "Dice Session Tester"})
    token = create_access_token(user_id)
    headers = {"Authorization": f"Bearer {token}"}
    yield headers
    if db is not None:
        await db.users.delete_one({"_id": ObjectId(user_id)})

@pytest.mark.anyio
async def test_dice_status_generic_no_hardcoding(auth_headers):
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

        # Check /settings/dice-status (requires auth)
        status_res = await client.get("/settings/dice-status", headers=auth_headers)
        assert status_res.status_code == 200
        status_data = status_res.json()
        assert "is_connected" in status_data
        assert "username" in status_data
        assert status_data["username"] != "VS (Veera Sekhar)"

@pytest.mark.anyio
async def test_session_import_generic_username(auth_headers):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Import with no username -> should resolve dynamically, not hardcoded
        cookies = [{"name": "test_auth_token", "value": "xyz123", "domain": ".dice.com", "path": "/"}]
        res = await client.post("/settings/import-dice-session", headers=auth_headers, json={"cookies": cookies})
        assert res.status_code == 200
        import_data = res.json()
        assert import_data["status"] == "success"
        assert import_data["is_connected"] is True
        assert import_data["username"] != "VS (Veera Sekhar)"
        assert len(import_data["username"]) > 0

        # CRITICAL TEST: Verify cookies are saved LOCALLY, NEVER in MongoDB
        assert settings_service.SESSION_FILE.exists()
        local_sess = settings_service.get_local_session()
        assert len(local_sess.get("cookies", [])) > 0

        db = get_database()
        settings_doc = await db.app_settings.find_one({}) or {}
        assert "saved_cookies" not in settings_doc, "Cookies MUST NOT be saved to MongoDB!"

        # Import with custom generic username
        res2 = await client.post(
            "/settings/import-dice-session",
            headers=auth_headers,
            json={"cookies": cookies, "username": "Alex Johnson"}
        )
        assert res2.status_code == 200
        assert res2.json()["username"] == "Alex Johnson"

@pytest.mark.anyio
async def test_disconnect_dice(auth_headers):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Import first
        cookies = [{"name": "test_auth_token", "value": "xyz123", "domain": ".dice.com", "path": "/"}]
        await client.post("/settings/import-dice-session", headers=auth_headers, json={"cookies": cookies, "username": "Test User"})
        assert settings_service.SESSION_FILE.exists()

        # Disconnect
        disc_res = await client.post("/settings/disconnect-dice", headers=auth_headers)
        assert disc_res.status_code == 200
        assert disc_res.json()["is_connected"] is False
        assert not settings_service.SESSION_FILE.exists()

        status_res = await client.get("/settings/dice-status", headers=auth_headers)
        assert status_res.json()["is_connected"] is False

@pytest.mark.anyio
async def test_verify_session_on_startup():
    # Verify startup function executes and prints banner without error
    status = await settings_service.verify_session_on_startup()
    assert isinstance(status, dict)
    assert "is_connected" in status
    assert status["username"] != "VS (Veera Sekhar)"

@pytest.mark.anyio
async def test_browser_status_and_verification(auth_headers):
    from app.browser.playwright_manager import playwright_manager
    is_installed = await playwright_manager.ensure_browser_installed()
    assert isinstance(is_installed, bool)

    # Test /settings/browser-status endpoint
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.get("/settings/browser-status", headers=auth_headers)
        assert res.status_code == 200
        data = res.json()
        assert "installed" in data
        assert data["installed"] == is_installed
        assert "executable_path" in data
        assert "headless" in data
        assert "is_cloud_mode" in data

@pytest.mark.anyio
async def test_marketing_cookies_rejected_as_unauthenticated(auth_headers):
    """Verify that marketing/tracking cookies alone are never treated as an active session."""
    marketing_cookies = [
        {"name": "_ga", "value": "GA1.1.123456", "domain": ".dice.com", "path": "/"},
        {"name": "_uetsid", "value": "abc123456", "domain": ".dice.com", "path": "/"},
        {"name": "_mkto_trk", "value": "id:123&token:456", "domain": ".dice.com", "path": "/"},
        {"name": "_gcl_au", "value": "1.1.987654", "domain": ".dice.com", "path": "/"},
    ]
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.post("/settings/import-dice-session", headers=auth_headers, json={"cookies": marketing_cookies})
        assert res.status_code == 200
        data = res.json()
        assert data["is_connected"] is False
        assert "No candidate authentication tokens found" in data["message"]

        # Ensure dice-status also reflects not connected
        status_res = await client.get("/settings/dice-status", headers=auth_headers)
        assert status_res.status_code == 200
        status_data = status_res.json()
        assert status_data["is_connected"] is False

@pytest.mark.anyio
async def test_mark_session_disconnected():
    """Verify soft-disconnection updates local session without deleting cookies."""
    # First save a valid dummy session
    cookies = [{"name": "test_auth_token", "value": "xyz123", "domain": ".dice.com", "path": "/"}]
    await settings_service.import_dice_session(cookies=cookies, username="Test Candidate")
    assert settings_service.get_local_session().get("is_connected") is True

    # Mark disconnected
    res = await settings_service.mark_session_disconnected("Login wall encountered")
    assert res["is_connected"] is False
    sess = settings_service.get_local_session()
    assert sess.get("is_connected") is False
    assert sess.get("disconnect_reason") == "Login wall encountered"
    # Cookies are preserved for re-authentication rather than abruptly wiped
    assert len(sess.get("cookies", [])) > 0

@pytest.mark.anyio
async def test_headless_chromium_launch():
    """Verify that Playwright can launch and close headless Chromium when browser is installed."""
    from app.browser.playwright_manager import playwright_manager
    if not await playwright_manager.ensure_browser_installed():
        pytest.skip("Playwright Chromium browser binary not installed in test environment")
    page = await playwright_manager.get_new_page(headless=True)
    assert page is not None
    await page.goto("about:blank")
    content = await page.content()
    assert "<html" in content.lower()
    await page.close()
    await playwright_manager.close()


@pytest.mark.anyio
async def test_authenticated_dice_session_sync():
    """Verify that syncing with an authenticated Bearer token saves the session directly under the user's ID."""
    from app.core.security import create_access_token
    from bson import ObjectId
    from app.services.session_store import get_session_store

    test_uid = str(ObjectId())
    db = get_database()
    if db is not None:
        await db.users.insert_one({"_id": ObjectId(test_uid), "email": "testuser@example.com", "name": "Test User"})

    token = create_access_token(test_uid)
    transport = ASGITransport(app=app)
    cookies = [{"name": "test_auth_token", "value": "xyz_user_token", "domain": ".dice.com", "path": "/"}]

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Sync with Authorization header
        sync_res = await client.post(
            "/api/dice/session/sync",
            headers={"Authorization": f"Bearer {token}"},
            json={"cookies": cookies}
        )
        assert sync_res.status_code == 200
        sync_data = sync_res.json()
        assert sync_data["success"] is True
        assert sync_data["connected"] is True

        # Check status with the same user token
        status_res = await client.get(
            "/api/dice/session/status",
            headers={"Authorization": f"Bearer {token}"}
        )
        assert status_res.status_code == 200
        status_data = status_res.json()
        assert status_data["connected"] is True

        # Verify session was stored under test_uid in session store
        store = get_session_store()
        sess = await store.get_session(test_uid)
        assert sess is not None
        assert sess.is_connected is True

    if db is not None:
        await db.users.delete_one({"_id": ObjectId(test_uid)})
        await store.delete_session(test_uid)




@pytest.mark.anyio
async def test_dice_account_details_independent_from_apply2hire():
    """Verify that Dice account details never fall back to Apply2Hire details, and never mutate Apply2Hire profile."""
    from app.core.security import create_access_token
    from bson import ObjectId
    from app.services.session_store import get_session_store
    import base64
    import json

    test_uid = str(ObjectId())
    db = get_database()
    if db is not None:
        await db.users.insert_one({
            "_id": ObjectId(test_uid),
            "email": "a2h_candidate@example.com",
            "name": "Apply2Hire Real User"
        })

    token = create_access_token(test_uid)
    transport = ASGITransport(app=app)

    try:
        # Case 1: Generic auth tokens with NO identity payload - MUST NOT fall back to Apply2Hire user name/email
        cookies_no_identity = [{"name": "test_auth_token", "value": "auth_val_1", "domain": ".dice.com", "path": "/"}]
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            res1 = await client.post(
                "/api/dice/session/sync",
                headers={"Authorization": f"Bearer {token}"},
                json={"cookies": cookies_no_identity}
            )
            assert res1.status_code == 200
            data1 = res1.json()
            assert data1["username"] != "Apply2Hire Real User"
            assert data1["email"] != "a2h_candidate@example.com"
            assert data1["username"] == "Dice Candidate"

            # Case 2: Dice session with genuine Dice candidate identity JWT
            dice_payload = {
                "given_name": "DiceCandidate",
                "family_name": "Specialist",
                "email": "dice_candidate_acc@example.com",
                "sub": "dice-sub-7788"
            }
            jwt_payload_b64 = base64.urlsafe_b64encode(json.dumps(dice_payload).encode()).decode().rstrip("=")
            mock_identity_cookie = f"header.{jwt_payload_b64}.signature"
            cookies_with_dice_id = [
                {"name": "identity", "value": mock_identity_cookie, "domain": ".dice.com", "path": "/"}
            ]

            res2 = await client.post(
                "/api/dice/session/sync",
                headers={"Authorization": f"Bearer {token}"},
                json={"cookies": cookies_with_dice_id}
            )
            assert res2.status_code == 200
            data2 = res2.json()
            assert data2["username"] == "DiceCandidate Specialist"
            assert data2["email"] == "dice_candidate_acc@example.com"

            # Verify Apply2Hire user was NOT overwritten or mutated
            if db is not None:
                user_doc = await db.users.find_one({"_id": ObjectId(test_uid)})
                assert user_doc["name"] == "Apply2Hire Real User"
                assert user_doc["email"] == "a2h_candidate@example.com"

            # Verify status endpoint returns Dice details, not Apply2Hire details
            status_res = await client.get(
                "/api/dice/session/status",
                headers={"Authorization": f"Bearer {token}"}
            )
            assert status_res.status_code == 200
            status_data = status_res.json()
            assert status_data["username"] == "DiceCandidate Specialist"
            assert status_data["email"] == "dice_candidate_acc@example.com"
            assert status_data["username"] != "Apply2Hire Real User"

    finally:
        if db is not None:
            await db.users.delete_one({"_id": ObjectId(test_uid)})
        store = get_session_store()
        await store.delete_session(test_uid)
