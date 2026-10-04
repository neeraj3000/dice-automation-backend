import re
import json
import logging
import asyncio
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List
from playwright.async_api import BrowserContext, Page, Error as PlaywrightError

from app.models.session import DiceSession
from app.services.session_store import get_session_store, _is_expired

logger = logging.getLogger(__name__)

class SessionState:
    CONNECTED = "CONNECTED"
    SESSION_EXPIRED = "SESSION_EXPIRED"
    LOGIN_REQUIRED = "LOGIN_REQUIRED"
    BROWSER_ERROR = "BROWSER_ERROR"
    UNKNOWN = "UNKNOWN"

TRACKING_COOKIE_PREFIXES = (
    "_ga", "_gid", "_gat", "_gcl", "_uet", "_mkto", "_gd_",
    "intercom", "optimizely", "amplitude", "hotjar", "li_",
    "bscookie", "bcookie", "hubspot", "ajs_", "mp_"
)

AUTH_COOKIE_KEYWORDS = (
    "identity", "refreshtoken", "candidate_id", "peopleid",
    "dice_member_id", "dice_session", "dice-user-id", "_oauth2_proxy",
    "cognito", "test_auth_token", "auth", "token", "session"
)

COGNITO_LS_KEYWORDS = (
    "idtoken", "accesstoken", "refreshtoken", "lastauthuser",
    "userdata", "cognito", "clockdrift", "dice_auth"
)

LOGIN_REDIRECT_MARKERS = (
    "/dashboard/login",
    "/signin",
    "login.dice.com",
    "redirecturl=",
    "auth0",
    "authorize",
    "accounts.dice.com"
)

LOGIN_WALL_PHRASES = (
    "create an account or sign in",
    "continue with email",
    "sign in to continue",
    "sign in to apply",
    "sign in to dice",
    "sign in with email",
    "log in to dice",
)


def is_auth_cookie(cookie: Dict[str, Any]) -> bool:
    """
    Returns True only if the cookie is an authentic session/auth cookie for Dice.com.
    Explicitly filters out marketing, analytics, and third-party tracking cookies.
    """
    if not isinstance(cookie, dict):
        return False
    name = (cookie.get("name") or "").strip().lower()
    domain = (cookie.get("domain") or "").strip().lower()
    val = str(cookie.get("value") or "").strip()

    if not val:
        return False

    # Must be on a dice.com domain
    if domain and "dice.com" not in domain:
        return False

    # Explicitly filter tracking & marketing cookies
    if any(name.startswith(p) for p in TRACKING_COOKIE_PREFIXES):
        return False
    if name in ("dli", "cms_cookie", "_fbp", "session_id", "crumb"):
        return False

    # Match genuine candidate auth cookie keys
    if name == "identity" and len(val) > 20:
        return True
    if any(kw in name for kw in AUTH_COOKIE_KEYWORDS):
        return True

    # Keep general non-tracking cookies on .dice.com
    return "dice.com" in domain


def filter_cognito_local_storage(local_storage: Dict[str, Any]) -> Dict[str, str]:
    """
    Extracts only the authentication state actually required by Dice/AWS Cognito.
    Filters out analytics, tracking, UI state, and unrelated keys.
    """
    if not local_storage or not isinstance(local_storage, dict):
        return {}

    filtered = {}
    for k, v in local_storage.items():
        if not isinstance(v, str):
            continue
        k_lower = k.lower()
        if any(kw in k_lower for kw in COGNITO_LS_KEYWORDS):
            filtered[k] = v
    return filtered


def has_required_auth_tokens(session: Optional[Any]) -> bool:
    """
    Determines if the session contains genuine candidate authentication tokens
    (identity cookie/JWT, refresh token, or Cognito localStorage keys).
    """
    if not session:
        return False

    cookies = []
    if hasattr(session, "cookies") and session.cookies:
        cookies = session.cookies
    elif isinstance(session, dict):
        cookies = session.get("cookies", [])

    for c in cookies:
        if is_auth_cookie(c):
            return True

    ls = {}
    if hasattr(session, "local_storage") and session.local_storage:
        ls = session.local_storage
    elif isinstance(session, dict):
        ls = session.get("local_storage", {})

    filtered_ls = filter_cognito_local_storage(ls)
    if filtered_ls:
        return True

    identity = getattr(session, "identity", None) or (session.get("identity") if isinstance(session, dict) else None)
    if identity and isinstance(identity, str) and len(identity) > 20:
        return True

    return False


def is_jwt_token_expired(jwt_token: str) -> Optional[bool]:
    """
    Decodes the JWT unverified payload to check if the 'exp' claim is in the past.
    Returns True if expired, False if valid, None if not a valid JWT.
    """
    if not jwt_token or not isinstance(jwt_token, str) or "." not in jwt_token:
        return None
    parts = jwt_token.split(".")
    if len(parts) < 2:
        return None
    try:
        import base64, time
        payload_b64 = parts[1] + "=" * ((4 - len(parts[1]) % 4) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        exp = payload.get("exp")
        if exp and isinstance(exp, (int, float)):
            return exp < time.time()
    except Exception:
        pass
    return None


async def restore_dice_session(
    browser_context: Any,
    session: Any
) -> bool:
    """
    Restores only the authentication state actually required by the existing Dice implementation:
    - Auth cookies (sanitized for Playwright, excluding unrelated tracking cookies)
    - Required Cognito localStorage state (idToken, accessToken, refreshToken, lastAuthUser, etc.)
    Do NOT blindly inject unrelated browser data.
    """
    if not session or not browser_context:
        return False

    cookies = []
    if hasattr(session, "cookies") and session.cookies:
        cookies = session.cookies
    elif isinstance(session, dict):
        cookies = session.get("cookies", [])

    ls = {}
    if hasattr(session, "local_storage") and session.local_storage:
        ls = session.local_storage
    elif isinstance(session, dict):
        ls = session.get("local_storage", {})

    # 1. Filter and sanitize authentication cookies
    from app.services.settings_service import sanitize_cookie_for_playwright
    auth_cookies = []
    seen_keys = set()
    for c in cookies:
        if is_auth_cookie(c):
            clean = sanitize_cookie_for_playwright(c)
            if clean:
                dedup_key = (clean["name"].lower(), clean.get("domain", "").lower(), clean.get("path", "/"))
                if dedup_key not in seen_keys:
                    seen_keys.add(dedup_key)
                    auth_cookies.append(clean)

    # 2. Filter Cognito / auth localStorage
    auth_ls = filter_cognito_local_storage(ls)

    # 3. Check fallback identity field
    identity = getattr(session, "identity", None) or (session.get("identity") if isinstance(session, dict) else None)
    if identity and isinstance(identity, str) and len(identity) > 20:
        if not any(c["name"] == "identity" for c in auth_cookies):
            auth_cookies.append({
                "name": "identity",
                "value": identity,
                "domain": ".dice.com",
                "path": "/"
            })

    # Validate that we have actual authentication credentials to inject
    if not auth_cookies and not auth_ls:
        logger.warning("restore_dice_session: No required authentication cookies or localStorage tokens found.")
        return False

    restored_something = False

    # Inject cookies into browser context
    if auth_cookies:
        try:
            await browser_context.add_cookies(auth_cookies)
            restored_something = True
            logger.info(f"Restored {len(auth_cookies)} required Dice auth cookies into browser context.")
        except Exception as bulk_err:
            logger.warning(f"Bulk add_cookies failed ({bulk_err}). Retrying per-cookie...")
            for sc in auth_cookies:
                try:
                    await browser_context.add_cookies([sc])
                    restored_something = True
                except Exception as single_err:
                    logger.debug(f"Skipping cookie '{sc.get('name')}': {single_err}")

    # Inject localStorage via context.add_init_script for dice.com
    if auth_ls:
        try:
            escaped_json = json.dumps(auth_ls)
            script = f"""
            (() => {{
                try {{
                    if (window.location.hostname.includes("dice.com")) {{
                        const items = {escaped_json};
                        for (const [k, v] of Object.entries(items)) {{
                            window.localStorage.setItem(k, v);
                        }}
                    }}
                }} catch (e) {{}}
            }})();
            """
            await browser_context.add_init_script(script)
            restored_something = True
            logger.info(f"Injected {len(auth_ls)} required Cognito localStorage items into browser context.")
        except Exception as ls_err:
            logger.warning(f"Failed to register localStorage init_script: {ls_err}")

    return restored_something


async def verify_dice_session(
    browser_context: Any,
    session: Optional[Any] = None,
    page: Optional[Any] = None,
    user_id: str = "default",
    timeout_ms: int = 20000
) -> Dict[str, Any]:
    """
    Verifies Dice session health by:
    1. Detecting missing authentication tokens
    2. Detecting expired authentication (via expires_at or JWT exp)
    3. Restoring required authentication state via restore_dice_session()
    4. Opening Dice (https://www.dice.com/dashboard)
    5. Checking final URL & detecting /login redirects
    6. Detecting login walls (DOM markers & visible sign-in forms)
    7. Verifying authentication (valid dashboard presence)
    8. Updating session status in SessionStore
    """
    now_utc = datetime.now(timezone.utc)
    now_iso = now_utc.isoformat()

    # 1. Detect missing authentication tokens
    if session is None or not has_required_auth_tokens(session):
        state = SessionState.LOGIN_REQUIRED
        reason = "Missing authentication tokens. Please sign in to Dice."
        await _update_session_record(user_id, state=state, is_connected=False, reason=reason, now_utc=now_utc)
        return {
            "state": state,
            "connected": False,
            "status": state,
            "reason": reason,
            "last_verified_at": now_iso,
            "expires_at": None
        }

    # 2. Detect expired authentication
    expires_at = getattr(session, "expires_at", None) or (session.get("expires_at") if isinstance(session, dict) else None)
    if _is_expired(expires_at):
        state = SessionState.SESSION_EXPIRED
        reason = "Session expired: expiration timestamp has passed."
        await _update_session_record(user_id, state=state, is_connected=False, reason=reason, now_utc=now_utc)
        return {
            "state": state,
            "connected": False,
            "status": state,
            "reason": reason,
            "last_verified_at": now_iso,
            "expires_at": expires_at.isoformat() if isinstance(expires_at, datetime) else str(expires_at)
        }

    # Check JWT expiry inside cookies or identity field
    jwt_val = getattr(session, "identity", None) or (session.get("identity") if isinstance(session, dict) else None)
    if not jwt_val:
        cookies = getattr(session, "cookies", []) or (session.get("cookies", []) if isinstance(session, dict) else [])
        for c in cookies:
            if c.get("name") == "identity":
                jwt_val = c.get("value")
                break

    if jwt_val and is_jwt_token_expired(jwt_val) is True:
        state = SessionState.SESSION_EXPIRED
        reason = "Session expired: candidate JWT token has expired."
        await _update_session_record(user_id, state=state, is_connected=False, reason=reason, now_utc=now_utc)
        return {
            "state": state,
            "connected": False,
            "status": state,
            "reason": reason,
            "last_verified_at": now_iso,
            "expires_at": expires_at.isoformat() if isinstance(expires_at, datetime) else None
        }

    # 3. Restore authentication state into browser context
    try:
        restored = await restore_dice_session(browser_context, session)
        if not restored:
            state = SessionState.LOGIN_REQUIRED
            reason = "Session restoration failed: no required authentication state to restore."
            await _update_session_record(user_id, state=state, is_connected=False, reason=reason, now_utc=now_utc)
            return {
                "state": state,
                "connected": False,
                "status": state,
                "reason": reason,
                "last_verified_at": now_iso,
                "expires_at": expires_at.isoformat() if isinstance(expires_at, datetime) else None
            }
    except Exception as restore_err:
        logger.error(f"Browser error during session restoration: {restore_err}")
        state = SessionState.BROWSER_ERROR
        reason = f"Browser error restoring session: {restore_err}"
        await _update_session_record(user_id, state=state, is_connected=False, reason=reason, now_utc=now_utc)
        return {
            "state": state,
            "connected": False,
            "status": state,
            "reason": reason,
            "last_verified_at": now_iso,
            "expires_at": expires_at.isoformat() if isinstance(expires_at, datetime) else None
        }

    # 4. Open Dice
    created_page = False
    active_page = page
    if active_page is None:
        try:
            active_page = await browser_context.new_page()
            created_page = True
        except Exception as e:
            logger.error(f"Failed to create new browser page: {e}")
            state = SessionState.BROWSER_ERROR
            reason = f"Browser error creating page: {e}"
            await _update_session_record(user_id, state=state, is_connected=False, reason=reason, now_utc=now_utc)
            return {
                "state": state,
                "connected": False,
                "status": state,
                "reason": reason,
                "last_verified_at": now_iso,
                "expires_at": expires_at.isoformat() if isinstance(expires_at, datetime) else None
            }

    state = SessionState.UNKNOWN
    reason = "Verification underway"

    try:
        logger.info("Opening Dice dashboard for authentication verification...")
        await active_page.goto("https://www.dice.com/dashboard", timeout=timeout_ms, wait_until="domcontentloaded")

        # 5. Check final URL & detect /login redirects
        final_url = (active_page.url or "").lower()
        logger.info(f"Dice dashboard navigation reached: {final_url}")

        if any(marker in final_url for marker in LOGIN_REDIRECT_MARKERS):
            state = SessionState.LOGIN_REQUIRED
            reason = f"Session redirected to login page: {active_page.url}"
        else:
            # 6. Detect login walls
            has_login_wall = False
            try:
                content = (await active_page.content()).lower()
                if any(phrase in content for phrase in LOGIN_WALL_PHRASES):
                    # Verify if email/password input or sign in button is present and visible
                    indicators = active_page.locator(
                        'input[type="email"], input[name="email"], input[type="password"], '
                        'button:has-text("Continue with email"), button:has-text("Sign In")'
                    ).first
                    if await indicators.count() > 0 and await indicators.is_visible():
                        has_login_wall = True
            except Exception as wall_err:
                logger.debug(f"Notice inspecting login wall elements: {wall_err}")

            if has_login_wall:
                state = SessionState.LOGIN_REQUIRED
                reason = "Login wall detected on Dice page. Sign-in required."
            elif "dice.com" in final_url:
                # 7. Verify authentication
                state = SessionState.CONNECTED
                reason = "Dice session authenticated and verified successfully."
            else:
                state = SessionState.UNKNOWN
                reason = f"Unexpected URL destination: {active_page.url}"

    except Exception as nav_err:
        logger.warning(f"Browser navigation error verifying Dice session: {nav_err}")
        state = SessionState.BROWSER_ERROR
        reason = f"Browser navigation error: {nav_err}"
    finally:
        if created_page and active_page:
            try:
                if not (hasattr(active_page, "is_closed") and active_page.is_closed()):
                    await active_page.close()
            except Exception:
                pass

    # 8. Update session status
    is_connected = (state == SessionState.CONNECTED)
    await _update_session_record(user_id, state=state, is_connected=is_connected, reason=reason, now_utc=now_utc)

    return {
        "state": state,
        "connected": is_connected,
        "status": "valid" if is_connected else state,
        "reason": reason,
        "last_verified_at": now_iso,
        "expires_at": expires_at.isoformat() if isinstance(expires_at, datetime) else (str(expires_at) if expires_at else None)
    }


async def _update_session_record(
    user_id: str,
    state: str,
    is_connected: bool,
    reason: str,
    now_utc: datetime
):
    """Updates session status in SessionStore and syncs local session file if user_id == 'default'."""
    try:
        store = get_session_store()
        update_data = {
            "status": state,
            "is_connected": is_connected,
            "last_verified_at": now_utc,
            "disconnect_reason": reason if not is_connected else None
        }
        await store.update_session(user_id, update_data)
    except Exception as e:
        logger.debug(f"Notice updating session in store: {e}")

    if user_id == "default":
        try:
            from app.services.settings_service import settings_service
            local_sess = settings_service.get_local_session()
            local_sess["is_connected"] = is_connected
            local_sess["status"] = state
            local_sess["last_verified"] = now_utc.isoformat()
            if not is_connected:
                local_sess["disconnect_reason"] = reason
            settings_service.save_local_session(local_sess)
        except Exception:
            pass
