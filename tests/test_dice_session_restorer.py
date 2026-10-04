import sys
import time
import json
import base64
from typing import Optional, Dict, Any, List
from pathlib import Path
from datetime import datetime, timezone, timedelta

# Add backend directory to sys.path
backend_dir = Path(__file__).resolve().parent.parent
if str(backend_dir) not in sys.path:
    sys.path.insert(0, str(backend_dir))

import pytest
from httpx import AsyncClient, ASGITransport

from app.main import app
from app.models.session import DiceSession
from app.services.session_store import get_session_store
from app.services.dice_session_restorer import (
    SessionState,
    restore_dice_session,
    verify_dice_session,
    is_auth_cookie,
    filter_cognito_local_storage,
    has_required_auth_tokens,
    is_jwt_token_expired
)
from app.database import connect_db, close_db, get_database

TEST_ENCRYPTION_KEY = "test-session-restorer-encryption-key-12345"

# --- Mock Playwright Context & Page Helpers ---

class MockLocator:
    def __init__(self, visible: bool = False, count: int = 0):
        self._visible = visible
        self._count = count

    @property
    def first(self):
        return self

    async def count(self) -> int:
        return self._count

    async def is_visible(self) -> bool:
        return self._visible


class MockPage:
    def __init__(self, url: str = "https://www.dice.com/dashboard", content: str = "<html>Dashboard Content</html>", has_login_wall: bool = False, redirect_to: Optional[str] = None):
        self.url = url
        self.redirect_to = redirect_to
        self._content = content
        self._has_login_wall = has_login_wall
        self._closed = False

    async def goto(self, url: str, timeout: int = 20000, wait_until: str = "domcontentloaded"):
        if self.redirect_to is not None:
            self.url = self.redirect_to
        else:
            self.url = url
        return None

    async def content(self) -> str:
        return self._content

    def locator(self, selector: str):
        if self._has_login_wall:
            return MockLocator(visible=True, count=1)
        return MockLocator(visible=False, count=0)

    def is_closed(self) -> bool:
        return self._closed

    async def close(self):
        self._closed = True


class MockBrowserContext:
    def __init__(self, page: Optional[MockPage] = None):
        self.added_cookies = []
        self.added_init_scripts = []
        self._page = page or MockPage()
        self._closed = False

    async def add_cookies(self, cookies: list):
        self.added_cookies.extend(cookies)

    async def add_init_script(self, script: str):
        self.added_init_scripts.append(script)

    async def new_page(self):
        return self._page

    def is_closed(self) -> bool:
        return self._closed

    async def close(self):
        self._closed = True


def make_jwt(exp_timestamp: int, email: str = "test@candidate.com") -> str:
    header = base64.urlsafe_b64encode(json.dumps({"alg": "HS256", "typ": "JWT"}).encode()).decode().rstrip("=")
    payload = base64.urlsafe_b64encode(json.dumps({
        "sub": "candidate_123",
        "email": email,
        "exp": exp_timestamp
    }).encode()).decode().rstrip("=")
    signature = "mock_sig_12345"
    return f"{header}.{payload}.{signature}"


@pytest.fixture(autouse=True)
async def setup_env():
    import os
    os.environ["SESSION_ENCRYPTION_KEY"] = TEST_ENCRYPTION_KEY
    await connect_db()
    db = get_database()
    if db is not None:
        try:
            await db.dice_sessions.delete_many({"user_id": {"$regex": "^test_restorer_"}})
        except Exception:
            pass
    yield
    if db is not None:
        try:
            await db.dice_sessions.delete_many({"user_id": {"$regex": "^test_restorer_"}})
        except Exception:
            pass
    await close_db()


# 1. Successful Restoration
@pytest.mark.anyio
async def test_successful_restoration():
    valid_jwt = make_jwt(int(time.time()) + 3600)
    session = DiceSession(
        user_id="test_restorer_success",
        cookies=[
            {"name": "identity", "value": valid_jwt, "domain": ".dice.com", "path": "/"},
            {"name": "refreshToken", "value": "rt_secure_123", "domain": ".dice.com", "path": "/"},
            # Tracking cookie that should NOT be injected
            {"name": "_ga", "value": "GA1.2.12345.67890", "domain": ".dice.com"},
            # Third-party cookie that should NOT be injected
            {"name": "third_party", "value": "bad_domain", "domain": ".google.com"}
        ],
        local_storage={
            "CognitoIdentityServiceProvider.app.user.idToken": valid_jwt,
            "CognitoIdentityServiceProvider.app.user.refreshToken": "rt_secure_123",
            "CognitoIdentityServiceProvider.app.LastAuthUser": "user@test.com",
            # Tracking localStorage key that should NOT be injected
            "ajs_anonymous_id": "anon_999",
            "theme_preference": "dark"
        },
        identity=valid_jwt
    )

    mock_context = MockBrowserContext()
    restored = await restore_dice_session(mock_context, session)
    assert restored is True

    # Verify injected cookies: ONLY required auth cookies
    cookie_names = [c["name"] for c in mock_context.added_cookies]
    assert "identity" in cookie_names
    assert "refreshToken" in cookie_names
    assert "_ga" not in cookie_names
    assert "third_party" not in cookie_names

    # Verify injected localStorage: ONLY required Cognito auth keys
    assert len(mock_context.added_init_scripts) == 1
    script = mock_context.added_init_scripts[0]
    assert "CognitoIdentityServiceProvider.app.user.idToken" in script
    assert "ajs_anonymous_id" not in script
    assert "theme_preference" not in script


# 2. Failed Restoration
@pytest.mark.anyio
async def test_failed_restoration():
    mock_context = MockBrowserContext()

    # Session with empty auth data
    empty_session = DiceSession(user_id="test_restorer_empty", cookies=[], local_storage={})
    res_empty = await restore_dice_session(mock_context, empty_session)
    assert res_empty is False

    # Session with only marketing/tracking cookies
    tracking_only_session = DiceSession(
        user_id="test_restorer_tracking",
        cookies=[
            {"name": "_ga", "value": "GA1.2.3.4", "domain": ".dice.com"},
            {"name": "_gid", "value": "GID123", "domain": ".dice.com"}
        ],
        local_storage={"analytics_id": "12345"}
    )
    res_tracking = await restore_dice_session(mock_context, tracking_only_session)
    assert res_tracking is False


# 3. Missing Authentication Tokens
@pytest.mark.anyio
async def test_missing_authentication_tokens():
    mock_page = MockPage()
    mock_context = MockBrowserContext(page=mock_page)

    session = DiceSession(
        user_id="test_restorer_missing",
        status="DISCONNECTED",
        cookies=[],
        local_storage={}
    )

    result = await verify_dice_session(mock_context, session, page=mock_page, user_id=session.user_id)
    assert result["connected"] is False
    assert result["state"] == SessionState.LOGIN_REQUIRED
    assert result["status"] == SessionState.LOGIN_REQUIRED
    assert "Missing authentication tokens" in result["reason"]


# 4. Expired Session
@pytest.mark.anyio
async def test_expired_session():
    mock_page = MockPage()
    mock_context = MockBrowserContext(page=mock_page)

    # A: expires_at in the past
    past_time = datetime.now(timezone.utc) - timedelta(hours=2)
    session_past_exp = DiceSession(
        user_id="test_restorer_exp_dt",
        status="CONNECTED",
        is_connected=True,
        expires_at=past_time,
        cookies=[{"name": "identity", "value": "sample_token", "domain": ".dice.com"}]
    )
    res_a = await verify_dice_session(mock_context, session_past_exp, page=mock_page, user_id=session_past_exp.user_id)
    assert res_a["connected"] is False
    assert res_a["state"] == SessionState.SESSION_EXPIRED
    assert res_a["status"] == SessionState.SESSION_EXPIRED

    # B: JWT expired claim in the past
    expired_jwt = make_jwt(int(time.time()) - 3600)
    session_expired_jwt = DiceSession(
        user_id="test_restorer_exp_jwt",
        status="CONNECTED",
        is_connected=True,
        cookies=[{"name": "identity", "value": expired_jwt, "domain": ".dice.com"}],
        identity=expired_jwt
    )
    res_b = await verify_dice_session(mock_context, session_expired_jwt, page=mock_page, user_id=session_expired_jwt.user_id)
    assert res_b["connected"] is False
    assert res_b["state"] == SessionState.SESSION_EXPIRED
    assert res_b["status"] == SessionState.SESSION_EXPIRED


# 5. Valid Session
@pytest.mark.anyio
async def test_valid_session():
    valid_jwt = make_jwt(int(time.time()) + 7200)
    mock_page = MockPage(
        url="https://www.dice.com/dashboard",
        content="<html><body><div id='dashboard'>Welcome Candidate</div></body></html>",
        has_login_wall=False
    )
    mock_context = MockBrowserContext(page=mock_page)

    session = DiceSession(
        user_id="test_restorer_valid",
        status="CONNECTED",
        is_connected=True,
        cookies=[{"name": "identity", "value": valid_jwt, "domain": ".dice.com"}],
        identity=valid_jwt
    )

    result = await verify_dice_session(mock_context, session, page=mock_page, user_id=session.user_id)
    assert result["connected"] is True
    assert result["state"] == SessionState.CONNECTED
    assert result["status"] == "valid"
    assert "verified successfully" in result["reason"].lower()


# 6. Invalid Session
@pytest.mark.anyio
async def test_invalid_session():
    valid_jwt = make_jwt(int(time.time()) + 7200)
    mock_page = MockPage(
        url="https://unknown-domain.com/error",
        redirect_to="https://unknown-domain.com/error",
        content="<html><body>Error</body></html>",
        has_login_wall=False
    )
    mock_context = MockBrowserContext(page=mock_page)

    session = DiceSession(
        user_id="test_restorer_invalid",
        status="CONNECTED",
        cookies=[{"name": "identity", "value": valid_jwt, "domain": ".dice.com"}],
        identity=valid_jwt
    )

    result = await verify_dice_session(mock_context, session, page=mock_page, user_id=session.user_id)
    assert result["connected"] is False
    assert result["state"] == SessionState.UNKNOWN


# 7. Login Redirect
@pytest.mark.anyio
async def test_login_redirect():
    valid_jwt = make_jwt(int(time.time()) + 7200)
    # Page redirected to login screen
    mock_page = MockPage(
        url="https://www.dice.com/dashboard/login?redirectUrl=%2Fdashboard",
        redirect_to="https://www.dice.com/dashboard/login?redirectUrl=%2Fdashboard",
        content="<html><body>Please log in</body></html>",
        has_login_wall=False
    )
    mock_context = MockBrowserContext(page=mock_page)

    session = DiceSession(
        user_id="test_restorer_login_redir",
        status="CONNECTED",
        cookies=[{"name": "identity", "value": valid_jwt, "domain": ".dice.com"}],
        identity=valid_jwt
    )

    result = await verify_dice_session(mock_context, session, page=mock_page, user_id=session.user_id)
    assert result["connected"] is False
    assert result["state"] == SessionState.LOGIN_REQUIRED
    assert result["status"] == SessionState.LOGIN_REQUIRED
    assert "redirected to login" in result["reason"].lower()


# 8. Login-Wall Detection
@pytest.mark.anyio
async def test_login_wall_detection():
    valid_jwt = make_jwt(int(time.time()) + 7200)
    # Page URL stayed on dashboard, but login wall appeared with email prompt
    mock_page = MockPage(
        url="https://www.dice.com/dashboard",
        content="<html><body><h1>Create an account or Sign In</h1><input type='email' /></body></html>",
        has_login_wall=True
    )
    mock_context = MockBrowserContext(page=mock_page)

    session = DiceSession(
        user_id="test_restorer_login_wall",
        status="CONNECTED",
        cookies=[{"name": "identity", "value": valid_jwt, "domain": ".dice.com"}],
        identity=valid_jwt
    )

    result = await verify_dice_session(mock_context, session, page=mock_page, user_id=session.user_id)
    assert result["connected"] is False
    assert result["state"] == SessionState.LOGIN_REQUIRED
    assert result["status"] == SessionState.LOGIN_REQUIRED
    assert "login wall detected" in result["reason"].lower()


# 9. API Endpoint: GET /api/dice/session/status
@pytest.mark.anyio
async def test_api_get_dice_session_status():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # A: When no session exists
        res = await client.get("/api/dice/session/status?user_id=test_restorer_nonexistent")
        assert res.status_code == 200
        data = res.json()
        assert data["connected"] is False
        assert data["status"] == "LOGIN_REQUIRED"
        # SECURITY AUDIT: Never return tokens or cookies
        assert "cookies" not in data
        assert "identity" not in data
        assert "local_storage" not in data

        # B: Connected valid session
        store = get_session_store()
        valid_jwt = make_jwt(int(time.time()) + 7200)
        valid_session = DiceSession(
            user_id="test_restorer_api_valid",
            status="CONNECTED",
            is_connected=True,
            cookies=[{"name": "identity", "value": valid_jwt, "domain": ".dice.com"}],
            identity=valid_jwt,
            last_verified_at=datetime.now(timezone.utc)
        )
        await store.save_session(valid_session.user_id, valid_session)

        res_valid = await client.get(f"/api/dice/session/status?user_id={valid_session.user_id}")
        assert res_valid.status_code == 200
        data_valid = res_valid.json()
        assert data_valid["connected"] is True
        assert data_valid["status"] == "valid"
        assert data_valid["last_verified_at"] is not None
        # Security audit
        assert "cookies" not in data_valid
        assert "tokens" not in data_valid
        assert "identity" not in data_valid

        # C: Expired session
        expired_session = DiceSession(
            user_id="test_restorer_api_expired",
            status="CONNECTED",
            is_connected=True,
            expires_at=datetime.now(timezone.utc) - timedelta(hours=1),
            cookies=[{"name": "identity", "value": valid_jwt, "domain": ".dice.com"}]
        )
        await store.save_session(expired_session.user_id, expired_session)

        res_exp = await client.get(f"/api/dice/session/status?user_id={expired_session.user_id}")
        assert res_exp.status_code == 200
        data_exp = res_exp.json()
        assert data_exp["connected"] is False
        assert data_exp["status"] == "SESSION_EXPIRED"

# 10. API Endpoint: POST /api/dice/session/sync (Extension Sync Flow)
@pytest.mark.anyio
async def test_api_post_dice_session_sync():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # A: Empty / invalid sync payload rejected
        empty_payload = {
            "cookies": [{"name": "_ga", "value": "tracking_only", "domain": ".dice.com"}],
            "local_storage": {}
        }
        res_empty = await client.post("/api/dice/session/sync", json=empty_payload)
        assert res_empty.status_code == 200
        data_empty = res_empty.json()
        assert data_empty["success"] is False
        assert data_empty["connected"] is False
        assert data_empty["status"] == "LOGIN_REQUIRED"

        # B: Authentic Dice session synced
        valid_jwt = make_jwt(int(time.time()) + 7200, email="extension_user@dice.com")
        sync_payload = {
            "cookies": [
                {"name": "identity", "value": valid_jwt, "domain": ".dice.com", "path": "/"},
                {"name": "refreshToken", "value": "rt_sync_token_456", "domain": ".dice.com", "path": "/"},
                # Tracking cookie that should be filtered out
                {"name": "_ga", "value": "GA_track_id", "domain": ".dice.com"}
            ],
            "local_storage": {
                "CognitoIdentityServiceProvider.app.user.idToken": valid_jwt,
                "CognitoIdentityServiceProvider.app.user.refreshToken": "rt_sync_token_456",
                "CognitoIdentityServiceProvider.app.LastAuthUser": "extension_user@dice.com"
            },
            "user_id": "test_restorer_sync_user"
        }

        res_sync = await client.post("/api/dice/session/sync", json=sync_payload)
        assert res_sync.status_code == 200
        data_sync = res_sync.json()
        assert data_sync["success"] is True
        assert data_sync["connected"] is True
        assert data_sync["status"] == "valid"
        assert "extension_user@dice.com" in (data_sync["email"] or data_sync["username"])
        # SECURITY AUDIT: Response must never return tokens or cookies
        assert "cookies" not in data_sync
        assert "identity" not in data_sync
        assert "local_storage" not in data_sync
        assert "tokens" not in data_sync

        # C: Query session status endpoint to verify state is CONNECTED
        res_stat = await client.get("/api/dice/session/status?user_id=test_restorer_sync_user")
        assert res_stat.status_code == 200
        data_stat = res_stat.json()
        assert data_stat["connected"] is True
        assert data_stat["status"] == "valid"

# 11. API Endpoint: POST /api/dice/session/disconnect
@pytest.mark.anyio
async def test_api_post_dice_session_disconnect():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.post("/api/dice/session/disconnect?user_id=test_user_disc")
        assert res.status_code == 200
        data = res.json()
        assert data["is_connected"] is False

