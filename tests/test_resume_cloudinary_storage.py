"""
Resume files live only in Cloudinary: uploads keep nothing on local disk, and the
apply flow downloads a temp copy that is deleted afterwards.
Runs against the isolated test database configured in conftest.py; Cloudinary is mocked.
"""
import io
from datetime import datetime, timezone

import docx
import httpx
import pytest
from bson import ObjectId
from httpx import AsyncClient, ASGITransport

from app.config import settings
from app.core.security import create_access_token
from app.database import connect_db, close_db, get_database
from app.main import app
from app.services import cloudinary_service, resume_service as resume_module

FAKE_URL = "https://res.cloudinary.test/raw/upload/resumes/{user}/{name}"


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def cloud(monkeypatch):
    """In-memory stand-in for Cloudinary: url -> bytes."""
    store, deleted = {}, []

    async def fake_upload(data, user_id="default", file_name="resume.pdf"):
        url = FAKE_URL.format(user=user_id, name=file_name)
        store[url] = data
        return {"cloudinary_url": url, "cloudinary_public_id": f"resumes/{user_id}/{file_name}"}

    async def fake_delete(public_id):
        deleted.append(public_id)

    monkeypatch.setattr(cloudinary_service, "is_cloudinary_configured", lambda: True)
    monkeypatch.setattr(cloudinary_service, "upload_resume", fake_upload)
    monkeypatch.setattr(cloudinary_service, "delete_resume", fake_delete)
    monkeypatch.setattr(resume_module.llm_service, "get_client", lambda: None)

    real_client = httpx.AsyncClient

    def client_factory(*a, **k):
        if "transport" in k:  # the test's own ASGI client
            return real_client(*a, **k)
        transport = httpx.MockTransport(
            lambda req: httpx.Response(200, content=store[str(req.url)]) if str(req.url) in store else httpx.Response(404)
        )
        return real_client(transport=transport, follow_redirects=True)

    monkeypatch.setattr(cloudinary_service.httpx, "AsyncClient", client_factory)
    return {"store": store, "deleted": deleted}


@pytest.fixture(autouse=True)
async def test_db():
    assert "test" in settings.DATABASE_NAME
    await connect_db()
    db = get_database()
    await db.client.drop_database(settings.DATABASE_NAME)
    yield db
    await db.client.drop_database(settings.DATABASE_NAME)
    await close_db()


def docx_bytes(text: str) -> bytes:
    d = docx.Document()
    d.add_paragraph(text)
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def temp_dir_entries():
    return sorted(p.name for p in settings.temp_resumes_dir.iterdir()) if settings.temp_resumes_dir.exists() else []


@pytest.mark.anyio
async def test_upload_stores_in_cloudinary_only_and_extracts_metadata(test_db, cloud):
    user = (await test_db.users.insert_one({"email": "u@test.io", "name": "u"})).inserted_id
    before = temp_dir_entries()
    payload = docx_bytes("Jane Smith - AI Engineer. Python, FastAPI, Docker, Kubernetes. 5 years experience.")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        res = await ac.post(
            "/api/v1/resumes/upload",
            headers={"Authorization": f"Bearer {create_access_token(str(user))}"},
            files=[("files", ("Jane_AI.docx", payload, "application/octet-stream"))],
        )
    assert res.status_code == 200, res.text
    body = res.json()[0]

    doc = await test_db.resumes.find_one({"_id": ObjectId(body["id"])})
    assert doc["cloudinary_url"] == FAKE_URL.format(user=str(user), name="Jane_AI.docx")
    assert cloud["store"][doc["cloudinary_url"]] == payload
    assert "file_path" not in doc
    assert "Python" in doc["raw_text"] and doc["skills"]
    assert temp_dir_entries() == before  # temp extraction copy removed


@pytest.mark.anyio
async def test_upload_fails_cleanly_without_cloudinary(test_db, cloud, monkeypatch):
    monkeypatch.setattr(cloudinary_service, "is_cloudinary_configured", lambda: False)
    user = (await test_db.users.insert_one({"email": "u@test.io", "name": "u"})).inserted_id

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        res = await ac.post(
            "/api/v1/resumes/upload",
            headers={"Authorization": f"Bearer {create_access_token(str(user))}"},
            files=[("files", ("Jane_AI.docx", docx_bytes("Python engineer"), "application/octet-stream"))],
        )
    assert res.status_code == 400
    assert "Cloudinary" in res.text
    assert await test_db.resumes.count_documents({}) == 0


@pytest.mark.anyio
async def test_apply_downloads_temp_copy_and_deletes_it(cloud):
    url = FAKE_URL.format(user="u1", name="cv.pdf")
    cloud["store"][url] = b"%PDF-1.4 resume bytes"
    before = temp_dir_entries()

    async with cloudinary_service.resume_file_for({"cloudinary_url": url, "file_name": "cv.pdf"}) as path:
        assert path.read_bytes() == b"%PDF-1.4 resume bytes"
        assert path.parent.parent == settings.temp_resumes_dir
        seen = path

    assert not seen.exists() and not seen.parent.exists()
    assert temp_dir_entries() == before


@pytest.mark.anyio
async def test_apply_refuses_resume_without_cloudinary_copy():
    with pytest.raises(RuntimeError, match="not stored in Cloudinary"):
        async with cloudinary_service.resume_file_for({"file_name": "old.pdf", "file_path": "resumes/old.pdf"}):
            pass


@pytest.mark.anyio
async def test_migration_moves_local_files_to_cloudinary(test_db, cloud, tmp_path, monkeypatch):
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "migrate", Path(__file__).resolve().parent.parent / "scripts" / "migrate_local_resumes_to_cloudinary.py"
    )
    mig = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mig)
    monkeypatch.setattr(mig, "upload_resume", cloudinary_service.upload_resume)
    monkeypatch.setattr(mig, "is_cloudinary_configured", lambda: True)

    legacy = tmp_path / "resumes"
    legacy.mkdir()
    (legacy / "old_cv.pdf").write_bytes(b"old resume bytes")
    monkeypatch.setattr(mig, "backend_dir", tmp_path)
    monkeypatch.setattr(mig, "LEGACY_RESUMES_DIR", legacy)

    now = datetime.now(timezone.utc)
    local_id = (await test_db.resumes.insert_one({"user_id": "u1", "file_name": "old_cv.pdf", "file_path": "resumes/old_cv.pdf", "created_at": now})).inserted_id
    await test_db.resumes.insert_one({"user_id": "u2", "file_name": "gone.pdf", "file_path": "resumes/gone.pdf", "created_at": now})
    await test_db.resumes.insert_one({"user_id": "u3", "file_name": "c.pdf", "cloudinary_url": "https://x/c.pdf", "file_path": "resumes/c.pdf", "created_at": now})

    dry = await mig.migrate(apply=False)
    assert dry == {"total": 3, "already_on_cloudinary": 1, "migrated": 0, "missing_file": 1}
    assert "cloudinary_url" not in await test_db.resumes.find_one({"_id": local_id})

    res = await mig.migrate(apply=True)
    assert res["migrated"] == 1
    doc = await test_db.resumes.find_one({"_id": local_id})
    assert cloud["store"][doc["cloudinary_url"]] == b"old resume bytes"
    assert "/u1/" in doc["cloudinary_url"] and "file_path" not in doc
    assert await test_db.resumes.count_documents({"file_path": {"$exists": True}, "cloudinary_url": {"$exists": True}}) == 0
