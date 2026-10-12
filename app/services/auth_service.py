import asyncio
from datetime import datetime, timedelta, timezone

import jwt
from bson import ObjectId
from fastapi import HTTPException, Response, status
from google.auth.transport import requests as g_requests
from google.oauth2 import id_token

from app.config import settings
from app.core.security import (
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_jti,
    hash_password,
    verify_password,
)
from app.database import get_database

REFRESH_COOKIE = "a2h_refresh"
REFRESH_REUSE_GRACE_SECONDS = 30


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def user_out(u: dict) -> dict:
    return {
        "id": str(u["_id"]),
        "email": u["email"],
        "name": u.get("name", ""),
        "avatar": u.get("avatar"),
    }


async def _issue(user: dict, response: Response) -> dict:
    uid = str(user["_id"])
    refresh, jti, exp = create_refresh_token(uid)
    db = get_database()
    await db.refresh_tokens.insert_one({
        "user_id": user["_id"],
        "jti_hash": hash_jti(jti),
        "expires_at": exp,
    })
    response.set_cookie(
        REFRESH_COOKIE,
        refresh,
        max_age=settings.refresh_token_days * 86400,
        httponly=True,
        secure=settings.cookie_secure,
        samesite=settings.cookie_samesite,
        path="/",
    )
    return {
        "access_token": create_access_token(uid),
        "token_type": "bearer",
        "user": user_out(user),
    }


async def register(name: str, email: str, password: str, response: Response) -> dict:
    db = get_database()
    email_clean = email.strip().lower()
    if await db.users.find_one({"email": email_clean}):
        raise HTTPException(status.HTTP_409_CONFLICT, "An account with this email already exists")
    user = {
        "email": email_clean,
        "name": name.strip(),
        "password_hash": await asyncio.to_thread(hash_password, password),
        "created_at": utcnow(),
    }
    insert_res = await db.users.insert_one(user)
    user["_id"] = insert_res.inserted_id
    return await _issue(user, response)


async def login(email: str, password: str, response: Response) -> dict:
    db = get_database()
    email_clean = email.strip().lower()
    user = await db.users.find_one({"email": email_clean})
    ok = await asyncio.to_thread(verify_password, password, user.get("password_hash") if user else None)
    if not user or not ok:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Incorrect email or password")
    return await _issue(user, response)


async def google_login(credential: str, response: Response) -> dict:
    if not settings.google_client_id:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Google sign-in is not configured")
    try:
        info = await asyncio.to_thread(
            id_token.verify_oauth2_token,
            credential,
            g_requests.Request(),
            settings.google_client_id,
        )
    except Exception:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid Google credential")
    if not info.get("email_verified"):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Google email is not verified")
    db = get_database()
    email = info["email"].lower()
    user = await db.users.find_one({"email": email})
    if user:
        await db.users.update_one(
            {"_id": user["_id"]},
            # Keep a photo the user uploaded themselves; only refresh the Google picture otherwise.
            {"$set": {
                "google_sub": info["sub"],
                "avatar": user.get("avatar") if user.get("avatar_public_id") else (info.get("picture") or user.get("avatar")),
            }},
        )
        user = await db.users.find_one({"_id": user["_id"]})
    else:
        user = {
            "email": email,
            "name": info.get("name") or email.split("@")[0],
            "avatar": info.get("picture"),
            "google_sub": info["sub"],
            "password_hash": None,
            "created_at": utcnow(),
        }
        user["_id"] = (await db.users.insert_one(user)).inserted_id
    return await _issue(user, response)


async def refresh(token: str | None, response: Response) -> dict:
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "No session")
    try:
        payload = decode_token(token, "refresh")
    except jwt.PyJWTError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Session expired")
    db = get_database()
    jti_hash = hash_jti(payload["jti"])
    now = utcnow()
    # Rotate: mark the token as used rather than deleting it, so concurrent refreshes
    # (React StrictMode double effects, several tabs) presenting the same cookie within
    # a short grace window still succeed instead of logging the user out.
    doc = await db.refresh_tokens.find_one_and_update(
        {"jti_hash": jti_hash, "rotated_at": None},
        {"$set": {
            "rotated_at": now,
            "expires_at": now + timedelta(seconds=REFRESH_REUSE_GRACE_SECONDS),
        }},
    )
    if not doc:
        doc = await db.refresh_tokens.find_one({
            "jti_hash": jti_hash,
            "rotated_at": {"$gte": now - timedelta(seconds=REFRESH_REUSE_GRACE_SECONDS)},
        })
    if not doc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Session revoked")
    sub = doc["user_id"]
    query = {"_id": ObjectId(sub)} if ObjectId.is_valid(sub) else {"_id": sub}
    user = await db.users.find_one(query)
    if not user:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "User not found")
    return await _issue(user, response)


async def logout(token: str | None, response: Response) -> None:
    if token:
        try:
            payload = decode_token(token, "refresh")
            db = get_database()
            await db.refresh_tokens.delete_one({"jti_hash": hash_jti(payload["jti"])})
        except Exception:
            pass
    response.delete_cookie(REFRESH_COOKIE, path="/")
