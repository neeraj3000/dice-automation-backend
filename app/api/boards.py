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


@router.post("/{key}/disconnect")
async def disconnect_board(key: str, user: Optional[dict] = Depends(get_current_user_optional)):
    get_board(key)
    user_id = str(user["_id"]) if user else "default"
    await dice_session_manager.disconnect(user_id=user_id)
    return {"status": "success", "message": f"Disconnected from {key}"}
