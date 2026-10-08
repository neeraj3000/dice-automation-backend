import hashlib
import uuid
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt

from app.config import settings


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode()[:72], bcrypt.gensalt()).decode()


def verify_password(password: str, hashed: str | None) -> bool:
    if not hashed:
        return False
    try:
        return bcrypt.checkpw(password.encode()[:72], hashed.encode())
    except ValueError:
        return False


def _now() -> datetime:
    return datetime.now(timezone.utc)


def create_access_token(user_id: str) -> str:
    payload = {
        "sub": str(user_id),
        "type": "access",
        "exp": _now() + timedelta(minutes=settings.access_token_minutes),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def create_refresh_token(user_id: str) -> tuple[str, str, datetime]:
    jti = uuid.uuid4().hex
    exp = _now() + timedelta(days=settings.refresh_token_days)
    token = jwt.encode(
        {"sub": str(user_id), "type": "refresh", "jti": jti, "exp": exp},
        settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )
    return token, jti, exp


def decode_token(token: str, expected_type: str) -> dict:
    payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    if payload.get("type") != expected_type:
        raise jwt.InvalidTokenError("wrong token type")
    return payload


def hash_jti(jti: str) -> str:
    return hashlib.sha256(jti.encode()).hexdigest()
