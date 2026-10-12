"""
Avatar API – upload / delete profile pictures via Cloudinary.
Mounted at  /avatar  (see app/main.py prefix).
"""
from __future__ import annotations

import asyncio
import io
import uuid
import base64
import re
from typing import Optional

import cloudinary
import cloudinary.uploader

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from app.config import settings
from app.core.deps import get_current_user_optional
from app.database import get_database
from app.services.cloudinary_service import is_cloudinary_configured, _configure

router = APIRouter(prefix="/avatar", tags=["Avatar"])


# ── helpers ──────────────────────────────────────────────────────────────────

def _cloud_configured() -> None:
    if not is_cloudinary_configured():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Cloudinary is not configured on this server.",
        )
    _configure()


async def _destroy(public_id: str) -> None:
    """Delete an image from Cloudinary (silently ignores errors)."""
    try:
        await asyncio.to_thread(
            cloudinary.uploader.destroy, public_id, resource_type="image"
        )
    except Exception:
        pass


def _data_url_to_bytes(data_url: str) -> tuple[bytes, str]:
    """Parse a data-URL and return (raw_bytes, extension)."""
    match = re.match(r"data:(image/[a-zA-Z+]+);base64,(.+)", data_url, re.DOTALL)
    if not match:
        raise HTTPException(status_code=400, detail="Invalid image data-URL.")
    mime = match.group(1)          # e.g. image/jpeg
    raw = base64.b64decode(match.group(2))
    ext = mime.split("/")[-1].replace("jpeg", "jpg")
    return raw, ext


# ── schemas ───────────────────────────────────────────────────────────────────

class AvatarUploadIn(BaseModel):
    image_data: str          # base-64 data-URL  (data:image/jpeg;base64,…)
    old_public_id: Optional[str] = None  # previous Cloudinary public_id to delete


class AvatarOut(BaseModel):
    avatar_url: str
    public_id: str


class AvatarDeleteIn(BaseModel):
    public_id: str           # Cloudinary public_id to destroy


# ── routes ────────────────────────────────────────────────────────────────────

@router.post("", response_model=AvatarOut, status_code=201)
async def upload_avatar(
    body: AvatarUploadIn,
    user: Optional[dict] = Depends(get_current_user_optional),
):
    """Upload or replace a profile avatar. Stores in Cloudinary under avatars/<user_id>/."""
    _cloud_configured()
    uid = str(user["_id"]) if user else "default"

    raw, ext = _data_url_to_bytes(body.image_data)
    if len(raw) > 8 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Image must be under 8 MB.")

    public_id = f"avatars/{uid}/{uuid.uuid4().hex[:12]}.{ext}"

    def _up():
        return cloudinary.uploader.upload(
            io.BytesIO(raw),
            resource_type="image",
            public_id=public_id,
            overwrite=True,
            folder=None,  # public_id already contains the folder path
            transformation=[
                {"width": 256, "height": 256, "crop": "fill", "gravity": "face"}
            ],
        )

    try:
        result = await asyncio.to_thread(_up)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Cloudinary upload failed: {exc}")

    avatar_url = result["secure_url"]
    cld_public_id = result["public_id"]

    # Delete the old image if requested
    if body.old_public_id and body.old_public_id != cld_public_id:
        await _destroy(body.old_public_id)

    # Persist avatar to the user's DB record
    db = get_database()
    if db is not None and user:
        await db.users.update_one(
            {"_id": user["_id"]},
            {"$set": {"avatar": avatar_url, "avatar_public_id": cld_public_id}},
        )
        # Also persist to the candidate profile collection
        await db.profiles.update_one(
            {"user_id": uid},
            {"$set": {"avatar": avatar_url, "avatar_url": avatar_url, "avatar_public_id": cld_public_id}},
            upsert=True,
        )

    return AvatarOut(avatar_url=avatar_url, public_id=cld_public_id)


@router.delete("", status_code=204)
async def delete_avatar(
    body: AvatarDeleteIn,
    user: Optional[dict] = Depends(get_current_user_optional),
):
    """Remove a profile avatar from Cloudinary and clear DB references."""
    _cloud_configured()

    await _destroy(body.public_id)

    db = get_database()
    uid = str(user["_id"]) if user else "default"
    if db is not None and user:
        await db.users.update_one(
            {"_id": user["_id"]},
            {"$unset": {"avatar": "", "avatar_public_id": ""}},
        )
        await db.profiles.update_one(
            {"user_id": uid},
            {"$unset": {"avatar": "", "avatar_url": "", "avatar_public_id": ""}},
        )
