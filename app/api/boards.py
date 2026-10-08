from fastapi import APIRouter, Depends, HTTPException, Body
from typing import Optional

from app.boards.registry import BOARDS, get_board
from app.core.deps import get_current_user_optional
from app.services.session_store import session_store
from app.services.dice_session_manager import dice_session_manager

router = APIRouter(prefix="/boards", tags=["boards"])


@router.get("")
async def list_boards(user: Optional[dict] = Depends(get_current_user_optional)):
    user_id = str(user["_id"]) if user else "default"
    out = []
    sess = await session_store.get_session(user_id=user_id)
    if not sess and user_id != "default":
        sess = await session_store.get_session(user_id="default")

    for b in BOARDS.values():
        is_conn = bool(sess and sess.status == "connected")
        out.append({
            "key": b.key,
            "name": b.name,
            "status": "CONNECTED" if is_conn else "DISCONNECTED",
            "connected_at": sess.last_verified_at.isoformat() if (sess and sess.last_verified_at) else None,
            "cookies_count": len(sess.cookies) if sess else 0,
        })
    return out


@router.get("/{key}/status")
async def board_status(key: str, user: Optional[dict] = Depends(get_current_user_optional)):
    b = get_board(key)
    user_id = str(user["_id"]) if user else "default"
    sess = await session_store.get_session(user_id=user_id)
    if not sess and user_id != "default":
        sess = await session_store.get_session(user_id="default")

    is_conn = bool(sess and sess.status == "connected")
    return {
        "key": b.key,
        "name": b.name,
        "connected": is_conn,
        "status": "CONNECTED" if is_conn else "DISCONNECTED",
        "last_verified_at": sess.last_verified_at.isoformat() if (sess and sess.last_verified_at) else None,
        "cookies_count": len(sess.cookies) if sess else 0,
    }


@router.post("/{key}/connect", status_code=202)
async def connect_board(key: str, user: Optional[dict] = Depends(get_current_user_optional)):
    get_board(key)
    user_id = str(user["_id"]) if user else "default"
    # Launch interactive login flow
    res = await dice_session_manager.start_interactive_login(user_id=user_id)
    return {
        "status": "CONNECTING",
        "key": key,
        "message": res.get("message", f"Connecting to {key}..."),
        **res
    }


@router.post("/{key}/verify")
async def verify_board(key: str, user: Optional[dict] = Depends(get_current_user_optional)):
    get_board(key)
    user_id = str(user["_id"]) if user else "default"
    from app.services.settings_service import settings_service
    status_res = await settings_service.get_dice_status(check_live=False)
    is_conn = bool(status_res.get("is_connected", False))
    return {
        "key": key,
        "status": "CONNECTED" if is_conn else "DISCONNECTED",
        "connected": is_conn,
        "username": status_res.get("username", ""),
        "last_verified_at": status_res.get("last_verified")
    }


@router.post("/{key}/cookies")
async def import_board_cookies(
    key: str,
    cookies: list[dict] = Body(..., max_length=500),
    user: Optional[dict] = Depends(get_current_user_optional)
):
    get_board(key)
    user_id = str(user["_id"]) if user else "default"
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
    return {"status": "success", "message": f"Disconnected from {key}"}
