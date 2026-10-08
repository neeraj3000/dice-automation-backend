import time
from collections import defaultdict, deque

import jwt
from bson import ObjectId
from fastapi import Depends, HTTPException, Request, status
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
