import asyncio
import io
import re
import shutil
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import cloudinary
import cloudinary.uploader
import httpx

from app.config import settings

_configured = False


def is_cloudinary_configured() -> bool:
    return bool(
        (settings.cloudinary_cloud_name
         and settings.cloudinary_api_key
         and settings.cloudinary_api_secret)
        or settings.cloudinary_url
    )


def _configure() -> None:
    global _configured
    if not _configured and is_cloudinary_configured():
        if settings.cloudinary_url:
            cloudinary.config(cloudinary_url=settings.cloudinary_url, secure=True)
        else:
            cloudinary.config(
                cloud_name=settings.cloudinary_cloud_name,
                api_key=settings.cloudinary_api_key,
                api_secret=settings.cloudinary_api_secret,
                secure=True,
            )
        _configured = True


def safe_filename(name: str) -> str:
    name = Path(name).name
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name)[:120] or "resume"


async def upload_resume(data: bytes, user_id: str = "default", file_name: str = "resume.pdf") -> dict:
    if not is_cloudinary_configured():
        return {}
    _configure()
    public_id = f"{uuid.uuid4().hex[:12]}_{safe_filename(file_name)}"

    def _up() -> dict:
        return cloudinary.uploader.upload(
            io.BytesIO(data),
            resource_type="raw",
            folder=f"resumes/{user_id}",
            public_id=public_id,
            overwrite=False,
        )

    res = await asyncio.to_thread(_up)
    return {
        "cloudinary_url": res.get("secure_url"),
        "cloudinary_public_id": res.get("public_id"),
    }


async def delete_resume(public_id: str) -> None:
    if not is_cloudinary_configured() or not public_id:
        return
    _configure()
    try:
        await asyncio.to_thread(cloudinary.uploader.destroy, public_id, resource_type="raw")
    except Exception:
        pass


@asynccontextmanager
async def temp_resume_file(url_or_path: str, file_name: str = "resume.pdf"):
    """
    Yields a local Path to the resume file.
    If url_or_path is an existing local file, yields it directly without overhead.
    If it is a remote URL (Cloudinary), downloads to a unique temp directory and cleans up afterwards.
    """
    local_p = Path(url_or_path)
    if local_p.exists() and local_p.is_file():
        yield local_p
        return

    if str(url_or_path).startswith("http://") or str(url_or_path).startswith("https://"):
        folder = settings.temp_resumes_dir / uuid.uuid4().hex
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / safe_filename(file_name)
        try:
            async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
                async with client.stream("GET", str(url_or_path)) as r:
                    if r.status_code != 200:
                        raise RuntimeError(
                            f"Could not download resume from Cloudinary (HTTP {r.status_code})."
                        )
                    with path.open("wb") as f:
                        async for chunk in r.aiter_bytes():
                            f.write(chunk)
            yield path
        finally:
            shutil.rmtree(folder, ignore_errors=True)
    else:
        yield local_p
