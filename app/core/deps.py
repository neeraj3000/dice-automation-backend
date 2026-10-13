import time
from collections import defaultdict, deque

import jwt
from bson import ObjectId
from fastapi import Depends, HTTPException, Request, Cookie, Query, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.security import decode_token
from app.database import get_database

_bearer = HTTPBearer(auto_error=False)


async def get_current_user(creds: HTTPAuthorizationCredentials | None = Depends(_bearer)) -> dict:
    if not creds:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated")
    try:
        payload = decode_token(creds.credentials, "access")
    except jwt.PyJWTError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired token")

    sub = payload.get("sub")
    query = {"_id": ObjectId(sub)} if ObjectId.is_valid(sub) else {"_id": sub}
    db = get_database()
    user = await db.users.find_one(query)
    if not user:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "User not found")
    return user


async def get_current_user_optional(creds: HTTPAuthorizationCredentials | None = Depends(_bearer)) -> dict | None:
    if not creds:
        return None
    try:
        payload = decode_token(creds.credentials, "access")
        sub = payload.get("sub")
        query = {"_id": ObjectId(sub)} if ObjectId.is_valid(sub) else {"_id": sub}
        db = get_database()
        return await db.users.find_one(query)
    except Exception:
        return None


async def get_current_user_sse(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
    a2h_refresh: str | None = Cookie(default=None),
    a2h_access: str | None = Cookie(default=None),
    ticket: str | None = Query(default=None)
) -> dict:
    token = creds.credentials if isinstance(creds, HTTPAuthorizationCredentials) else None
    if not token and isinstance(a2h_access, str):
        token = a2h_access

    if token:
        try:
            payload = decode_token(token, "access")
            sub = payload.get("sub")
            query = {"_id": ObjectId(sub)} if ObjectId.is_valid(sub) else {"_id": sub}
            db = get_database()
            user = await db.users.find_one(query)
            if user:
                return user
        except Exception:
            pass

    if isinstance(a2h_refresh, str):
        try:
            payload = decode_token(a2h_refresh, "refresh")
            sub = payload.get("sub")
            query = {"_id": ObjectId(sub)} if ObjectId.is_valid(sub) else {"_id": sub}
            db = get_database()
            user = await db.users.find_one(query)
            if user:
                return user
        except Exception:
            pass

    if isinstance(ticket, str):
        try:
            payload = decode_token(ticket, "access")
            sub = payload.get("sub")
            query = {"_id": ObjectId(sub)} if ObjectId.is_valid(sub) else {"_id": sub}
            db = get_database()
            user = await db.users.find_one(query)
            if user:
                return user
        except Exception:
            pass

    raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated for SSE stream")


def rate_limit(max_calls: int, per_seconds: int):
    """Sliding-window in-memory rate limiter."""
    hits: dict[str, deque] = defaultdict(deque)

    async def _dep(request: Request) -> None:
        key = request.client.host if request.client else "unknown"
        now = time.monotonic()
        q = hits[key]
        while q and now - q[0] > per_seconds:
            q.popleft()
        if len(q) >= max_calls:
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many attempts. Please wait a moment.")
        q.append(now)

    return _dep
