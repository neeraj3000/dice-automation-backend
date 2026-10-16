from fastapi import APIRouter, Depends, HTTPException, Body
from typing import Optional

from app.boards.registry import BOARDS, get_board
from app.core.deps import get_current_user, get_current_user_optional
from app.services.session_store import session_store
from app.services.dice_session_manager import dice_session_manager

router = APIRouter(prefix="/boards", tags=["boards"])


async def _resolve_dice_session(user: Optional[dict]):
    """Helper to resolve active Dice session strictly for the authenticated user."""
    if not user:
        return {
            "is_connected": False,
            "username": "",
            "email": "",
            "last_verified_at": None,
            "cookies_count": 0,
        }

    from app.database import get_database
    from bson import ObjectId
    db = get_database()

    uid = str(user["_id"])
    u_oids = [ObjectId(uid)] if ObjectId.is_valid(uid) else []
    u_strs = [uid]
    if user.get("email"):
        u_strs.append(user["email"].strip().lower())

    is_conn = False
    username = ""
    email = ""
    last_verified = None
    cookies_count = 0

    # 1. Check board_connections in DB for this user
    bc = None
    if db is not None:
        bc = await db.board_connections.find_one({
            "user_id": {"$in": u_oids + u_strs},
            "$or": [{"board": "dice"}, {"board_key": "dice"}]
        })

    if bc:
        status = str(bc.get("status", "")).upper()
        if status == "CONNECTED" or bc.get("is_connected") is True:
            is_conn = True
            username = bc.get("username", "") or bc.get("account_name", "")
            email = bc.get("account_email", "") or bc.get("email", "")
            if bc.get("last_verified_at"):
                last_verified = bc["last_verified_at"].isoformat() if hasattr(bc["last_verified_at"], "isoformat") else str(bc["last_verified_at"])
            elif bc.get("connected_at"):
                last_verified = bc["connected_at"].isoformat() if hasattr(bc["connected_at"], "isoformat") else str(bc["connected_at"])
            cookies_count = bc.get("cookies_count", 0)

    # 2. Check user-scoped session in session_store (strictly for this user)
    if not is_conn:
        sess = await session_store.get_session(user_id=uid)
        if not sess and user.get("email"):
            sess = await session_store.get_session(user_id=user["email"].strip().lower())

        if sess:
            sess_conn = bool(
                getattr(sess, "is_connected", False) is True
                or str(getattr(sess, "status", "")).upper() in ("CONNECTED", "VALID")
            )
            if sess_conn:
                is_conn = True
                username = getattr(sess, "username", "") or username
                email = getattr(sess, "email", "") or email
                if sess.last_verified_at:
                    last_verified = sess.last_verified_at.isoformat()
                cookies_count = len(sess.cookies)

    return {
        "is_connected": is_conn,
        "username": username,
        "email": email,
        "last_verified_at": last_verified,
        "cookies_count": cookies_count,
    }


@router.get("")
async def list_boards(user: Optional[dict] = Depends(get_current_user_optional)):
    dice_info = await _resolve_dice_session(user)
    out = []
    for b in BOARDS.values():
        is_conn = dice_info["is_connected"] if b.key == "dice" else False
        out.append({
            "key": b.key,
            "name": b.name,
            "status": "CONNECTED" if is_conn else "DISCONNECTED",
            "connected": is_conn,
            "connected_at": dice_info["last_verified_at"] if b.key == "dice" else None,
            "last_verified_at": dice_info["last_verified_at"] if b.key == "dice" else None,
            "cookies_count": dice_info["cookies_count"] if b.key == "dice" else 0,
            "username": dice_info["username"] if b.key == "dice" else "",
            "email": dice_info["email"] if b.key == "dice" else "",
        })
    return out


@router.get("/{key}/status")
async def board_status(key: str, user: Optional[dict] = Depends(get_current_user_optional)):
    b = get_board(key)
    dice_info = await _resolve_dice_session(user)
    is_conn = dice_info["is_connected"] if key == "dice" else False
    return {
        "key": b.key,
        "name": b.name,
        "connected": is_conn,
        "status": "CONNECTED" if is_conn else "DISCONNECTED",
        "last_verified_at": dice_info["last_verified_at"] if key == "dice" else None,
        "cookies_count": dice_info["cookies_count"] if key == "dice" else 0,
        "username": dice_info["username"] if key == "dice" else "",
        "email": dice_info["email"] if key == "dice" else "",
    }


@router.post("/{key}/connect", status_code=202)
async def connect_board(key: str, user: dict = Depends(get_current_user)):
    b = get_board(key)
    if key == "linkedin":
        return {
            "status": "USER_TRACKING_ACTIVE",
            "key": "linkedin",
            "name": "LinkedIn",
            "login_url": "https://www.linkedin.com/login",
            "message": "LinkedIn uses safe user-confirmed status tracking without scraping or credential extraction.",
            "instructions": [
                "1. Open LinkedIn in your browser and sign in normally.",
                "2. Browse jobs and submit your application directly on LinkedIn.",
                "3. Record your confirmed application outcome in Apply2Hire to track progress safely."
            ]
        }
    return {
        "status": "WAITING_FOR_SYNC",
        "key": b.key,
        "name": b.name,
        "login_url": "https://www.dice.com/dashboard/login",
        "message": f"Log in to {b.name} in your Chrome browser and click 'Sync Dice Account' in the extension.",
        "instructions": [
            f"1. Open {b.name} in your browser and sign in normally.",
            "2. Click the Dice Sync Extension icon in your Chrome toolbar.",
            "3. Click 'Sync Dice Account' to save your authenticated session."
        ]
    }


@router.post("/{key}/verify")
async def verify_board(key: str, user: dict = Depends(get_current_user)):
    get_board(key)
    if key == "linkedin":
        return {
            "key": "linkedin",
            "status": "TRACKING_ACTIVE",
            "connected": True,
            "username": user.get("name") or user.get("email") or "",
            "email": user.get("email") or "",
            "last_verified_at": None,
            "cookies_count": 0,
            "workflow": "user_confirmed"
        }
    dice_info = await _resolve_dice_session(user)
    is_conn = dice_info["is_connected"] if key == "dice" else False
    return {
        "key": key,
        "status": "CONNECTED" if is_conn else "DISCONNECTED",
        "connected": is_conn,
        "username": dice_info["username"] if key == "dice" else "",
        "email": dice_info["email"] if key == "dice" else "",
        "last_verified_at": dice_info["last_verified_at"] if key == "dice" else None,
        "cookies_count": dice_info["cookies_count"] if key == "dice" else 0,
    }


@router.post("/{key}/cookies")
async def import_board_cookies(
    key: str,
    cookies: list[dict] = Body(..., max_length=500),
    user: dict = Depends(get_current_user)
):
    get_board(key)
    if key == "linkedin":
        raise HTTPException(
            status_code=400,
            detail="Cookie extraction or import is prohibited for LinkedIn. Apply2Hire uses safe, user-confirmed application status tracking."
        )
    user_id = str(user["_id"])
    from app.services.settings_service import settings_service
    res = await settings_service.import_dice_session(cookies=cookies, user_id=user_id)
    is_connected = bool(res.get("is_connected", False))
    return {
        "status": "CONNECTED" if is_connected else "DISCONNECTED",
        "key": key,
        "message": res.get("message", "Cookies imported successfully."),
        "cookies_count": res.get("cookies_count", len(cookies))
    }


@router.post("/{key}/disconnect")
async def disconnect_board(key: str, user: Optional[dict] = Depends(get_current_user_optional)):
    get_board(key)
    user_id = str(user["_id"]) if user else "default"
    await dice_session_manager.disconnect(user_id=user_id)
    if key == "dice":
        from app.services.settings_service import settings_service
        await settings_service.disconnect_dice(user_id=user_id)

    from app.database import get_database
    db = get_database()
    if db is not None:
        from bson import ObjectId
        uid = user["_id"] if user else "default"
        u_oids = [ObjectId(uid)] if ObjectId.is_valid(uid) else []
        await db.board_connections.update_many(
            {"user_id": {"$in": u_oids + [str(uid)]}, "$or": [{"board": key}, {"board_key": key}]},
            {"$set": {"status": "DISCONNECTED", "is_connected": False, "cookies_count": 0}}
        )
    return {"status": "success", "message": f"Disconnected from {key}"}
