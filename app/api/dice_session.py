import logging
from typing import Optional
from datetime import datetime, timezone, timedelta
from fastapi import APIRouter, Query, Depends
from pydantic import BaseModel

from app.core.deps import get_current_user_optional

from app.services.session_store import get_session_store, _is_expired
from app.services.dice_session_restorer import (
    SessionState,
    has_required_auth_tokens,
    verify_dice_session
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Dice Session Health"])

class DiceSessionStatusResponse(BaseModel):
    connected: bool
    status: str
    last_verified_at: Optional[str] = None
    expires_at: Optional[str] = None

@router.get("/dice/session/status", response_model=DiceSessionStatusResponse)
@router.get("/api/dice/session/status", response_model=DiceSessionStatusResponse, include_in_schema=False)
async def get_dice_session_status(
    check_live: bool = Query(False, description="Verify live via Playwright browser navigation"),
    user_id: str = Query("default", description="User ID for isolated session storage")
) -> DiceSessionStatusResponse:
    """
    Returns current Dice session health status.
    
    Security:
    - Never returns tokens, cookies, credentials, or localStorage values.
    - If authentication fails or session is expired, connected is always false.
    """
    store = get_session_store()
    session = await store.get_session(user_id)

    if not session:
        return DiceSessionStatusResponse(
            connected=False,
            status=SessionState.LOGIN_REQUIRED,
            last_verified_at=None,
            expires_at=None
        )

    # If check_live is requested, verify via Playwright browser context
    if check_live:
        from app.browser.playwright_manager import playwright_manager
        try:
            context = await playwright_manager.get_context(profile_id=user_id, restore_session=False)
            res = await verify_dice_session(context, session=session, user_id=user_id)
            return DiceSessionStatusResponse(
                connected=res["connected"],
                status=res["status"],
                last_verified_at=res.get("last_verified_at"),
                expires_at=res.get("expires_at")
            )
        except Exception as e:
            logger.error(f"Live verification failed with browser error: {e}")
            return DiceSessionStatusResponse(
                connected=False,
                status=SessionState.BROWSER_ERROR,
                last_verified_at=session.last_verified_at.isoformat() if session.last_verified_at else None,
                expires_at=session.expires_at.isoformat() if session.expires_at else None
            )

    # Fast path: check expiration & stored status
    if _is_expired(session.expires_at):
        return DiceSessionStatusResponse(
            connected=False,
            status=SessionState.SESSION_EXPIRED,
            last_verified_at=session.last_verified_at.isoformat() if session.last_verified_at else None,
            expires_at=session.expires_at.isoformat() if session.expires_at else None
        )

    if not has_required_auth_tokens(session):
        return DiceSessionStatusResponse(
            connected=False,
            status=SessionState.LOGIN_REQUIRED,
            last_verified_at=session.last_verified_at.isoformat() if session.last_verified_at else None,
            expires_at=session.expires_at.isoformat() if session.expires_at else None
        )

    is_conn = bool(session.is_connected and session.status == SessionState.CONNECTED)
    status_str = "valid" if is_conn else (session.status or SessionState.LOGIN_REQUIRED)

    return DiceSessionStatusResponse(
        connected=is_conn,
        status=status_str,
        last_verified_at=session.last_verified_at.isoformat() if session.last_verified_at else None,
        expires_at=session.expires_at.isoformat() if session.expires_at else None
    )


from typing import List, Dict, Any

class DiceSessionSyncPayload(BaseModel):
    cookies: Optional[List[Dict[str, Any]]] = None
    local_storage: Optional[Dict[str, Any]] = None
    username: Optional[str] = None
    user_id: Optional[str] = "default"

class DiceSessionSyncResponse(BaseModel):
    success: bool
    connected: bool
    status: str
    message: str
    username: Optional[str] = None
    email: Optional[str] = None
    cookies_count: int = 0
    last_verified_at: Optional[str] = None
    expires_at: Optional[str] = None

@router.post("/dice/session/sync", response_model=DiceSessionSyncResponse)
@router.post("/api/dice/session/sync", response_model=DiceSessionSyncResponse, include_in_schema=False)
async def sync_dice_session(payload: DiceSessionSyncPayload) -> DiceSessionSyncResponse:
    """
    Synchronizes authenticated Dice session tokens/cookies captured by the Chrome Extension.
    
    Security & Flow:
    1. Validates that genuine candidate authentication data is present.
    2. Encrypts sensitive values via application-level encryption (Fernet) in SessionStore.
    3. Verifies session authenticity and updates connection state.
    4. Broadcasts connection event (DICE_CONNECTED) to the dashboard frontend.
    5. Returns ONLY safe metadata (no tokens, cookies, or secrets).
    """
    user_id = payload.user_id or "default"

    # 1. Validate presence of required authentication credentials
    has_tokens = has_required_auth_tokens({
        "cookies": payload.cookies or [],
        "local_storage": payload.local_storage or {}
    })
    if not has_tokens:
        logger.warning(f"Sync attempt for user '{user_id}' contained no valid authentication tokens.")
        return DiceSessionSyncResponse(
            success=False,
            connected=False,
            status=SessionState.LOGIN_REQUIRED,
            message="No valid Dice authentication tokens found in the synchronized session package. Please make sure you are logged into Dice.com first.",
            cookies_count=0
        )

    # 2. Process, encrypt, and store via settings_service
    from app.services.settings_service import settings_service
    import_result = await settings_service.import_dice_session(
        cookies=payload.cookies,
        username=payload.username,
        local_storage=payload.local_storage
    )

    is_connected = bool(import_result.get("is_connected", False))
    status_str = "valid" if is_connected else SessionState.LOGIN_REQUIRED
    msg = import_result.get("message", "Session synchronized.")

    store = get_session_store()
    now_utc = datetime.now(timezone.utc)
    last_verified = now_utc.isoformat()
    expires_at = (now_utc + timedelta(days=30)).isoformat()

    # 3. Save under requested user_id in SessionStore with encryption
    if is_connected:
        from app.models.session import DiceSession
        from app.database import get_database
        identity_val = next((c.get("value", "") for c in (payload.cookies or []) if c.get("name") == "identity"), "")
        sess_obj = DiceSession(
            user_id=user_id,
            platform="dice",
            status="CONNECTED",
            is_connected=True,
            username=import_result.get("username") or "",
            email=import_result.get("email") or "",
            candidate_id=import_result.get("candidate_id") or "",
            cookies_count=import_result.get("cookies_count", 0),
            cookies=payload.cookies or [],
            local_storage=payload.local_storage or {},
            identity=identity_val,
            last_verified_at=now_utc,
            expires_at=now_utc + timedelta(days=30)
        )
        await store.save_session(user_id, sess_obj)

        # Multi-tenant link: If an email was detected, also link to the registered user ID
        cand_email = (import_result.get("email") or "").strip().lower()
        if cand_email:
            try:
                db = get_database()
                if db is not None:
                    matched_user = await db.users.find_one({"email": cand_email})
                    if matched_user:
                        matched_id = str(matched_user["_id"])
                        if matched_id != user_id:
                            sess_copy = sess_obj.model_copy(update={"user_id": matched_id})
                            await store.save_session(matched_id, sess_copy)
                            logger.info(f"Automatically linked Dice session to user '{matched_id}' ({cand_email})")
            except Exception as le:
                logger.debug(f"Notice auto-linking user session: {le}")
    else:
        last_verified = None
        expires_at = None

    return DiceSessionSyncResponse(
        success=is_connected,
        connected=is_connected,
        status=status_str,
        message=msg,
        username=import_result.get("username") or "",
        email=import_result.get("email") or "",
        cookies_count=import_result.get("cookies_count", 0),
        last_verified_at=last_verified,
        expires_at=expires_at
    )

@router.post("/dice/session/disconnect")
@router.post("/api/dice/session/disconnect", include_in_schema=False)
async def disconnect_dice_session(
    user_id: Optional[str] = Query(None),
    user: Optional[dict] = Depends(get_current_user_optional)
):
    """Disconnects the active Dice session, clears cookies/tokens, and notifies listeners."""
    target_uid = user_id or (str(user["_id"]) if user else "default")
    from app.services.settings_service import settings_service
    return await settings_service.disconnect_dice(user_id=target_uid)

