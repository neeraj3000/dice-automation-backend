"""
Uploading a resume the user already has: the same content is recognised under any file name,
before any LLM call or storage. Cloudinary, the LLM and text extraction are faked; MongoDB is real
(test DB).
"""
import io
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException, UploadFile

from app.api.resumes import upload_resumes
from app.database import close_db, connect_db, get_database
from app.services.resume_service import DuplicateResumeError, resume_content_hash, resume_service

USER = "test_duplicate_user"
OTHER_USER = "test_duplicate_other"
TEXT = "Veera Sekhar\nSenior AI Developer\nPython, LLMs, Azure"


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
async def env(monkeypatch):
    await connect_db()
    db = get_database()
    await db.resumes.delete_many({"user_id": {"$in": [USER, OTHER_USER]}})

    # The "file" is its text, so tests choose exactly what gets extracted
    async def extract(self, content, file_name, ext):
        return content.decode()

    metadata = SimpleNamespace(
        display_name="Veera Sekhar", target_role="Senior AI Developer", skills=[], experience_years="", summary=""
    )
    parse = AsyncMock(return_value=metadata)
    store = AsyncMock(side_effect=lambda *a: {"cloudinary_url": "https://files.test/r.pdf", "cloudinary_public_id": "r"})
    monkeypatch.setattr(type(resume_service), "_extract_text_from_bytes", extract)
    monkeypatch.setattr(type(resume_service), "_store_in_cloudinary", lambda self, *a: store(*a))
    monkeypatch.setattr("app.services.resume_service.llm_service.parse_resume_metadata", parse)
    yield SimpleNamespace(parse=parse, store=store)
    await db.resumes.delete_many({"user_id": {"$in": [USER, OTHER_USER]}})
    await close_db()


def _file(name, text=TEXT):
    return UploadFile(filename=name, file=io.BytesIO(text.encode()))


async def _upload(name, text=TEXT, user=USER, reject=True):
    return await resume_service.save_uploaded_resume(_file(name, text), user_id=user, reject_duplicates=reject)


@pytest.mark.anyio
async def test_same_content_under_another_name_is_refused_before_any_costly_step(env):
    first = await _upload("veera.pdf")
    env.parse.reset_mock()
    env.store.reset_mock()

    with pytest.raises(DuplicateResumeError) as err:
        await _upload("Veera Sekhar (1) final.pdf")

    assert err.value.status_code == 409
    assert err.value.detail["resume"]["id"] == first.id
    assert err.value.detail["resume"]["target_role"] == "Senior AI Developer"
    env.parse.assert_not_awaited()
    env.store.assert_not_awaited()
    assert await get_database().resumes.count_documents({"user_id": USER}) == 1


@pytest.mark.anyio
async def test_resaved_copy_with_different_spacing_and_case_is_a_duplicate():
    await _upload("veera.pdf")
    with pytest.raises(DuplicateResumeError):
        await _upload("copy.pdf", "  VEERA SEKHAR   senior ai developer\n\npython, llms,   azure ")


@pytest.mark.anyio
async def test_different_content_uploads_normally():
    await _upload("veera.pdf")
    await _upload("veera.pdf", TEXT + "\nNew project at Acme")
    assert await get_database().resumes.count_documents({"user_id": USER}) == 2


@pytest.mark.anyio
async def test_another_users_identical_resume_does_not_count():
    await _upload("veera.pdf", user=OTHER_USER)
    await _upload("veera.pdf", user=USER)
    assert await get_database().resumes.count_documents({"user_id": USER}) == 1


@pytest.mark.anyio
async def test_resumes_from_before_hashes_are_matched_and_backfilled():
    db = get_database()
    old = await db.resumes.insert_one({"user_id": USER, "file_name": "old.pdf", "display_name": "Old", "raw_text": TEXT})
    other = await db.resumes.insert_one({"user_id": USER, "file_name": "b.pdf", "display_name": "B", "raw_text": "Other"})

    with pytest.raises(DuplicateResumeError) as err:
        await _upload("renamed.pdf")

    assert err.value.detail["resume"]["id"] == str(old.inserted_id)
    assert (await db.resumes.find_one({"_id": old.inserted_id}))["content_hash"] == resume_content_hash(TEXT)
    assert (await db.resumes.find_one({"_id": other.inserted_id}))["content_hash"] == resume_content_hash("Other")


@pytest.mark.anyio
async def test_scanned_files_without_text_compare_by_bytes():
    assert resume_content_hash("", b"scan one") == resume_content_hash("  ", b"scan one")
    assert resume_content_hash("", b"scan one") != resume_content_hash("", b"scan two")


@pytest.mark.anyio
async def test_replacing_a_file_moves_its_fingerprint():
    first = await _upload("veera.pdf")
    await resume_service.replace_resume_file(first.id, _file("new.pdf", "Completely rewritten"), user_id=USER)

    await _upload("veera-again.pdf")  # the old content is free again
    with pytest.raises(DuplicateResumeError):
        await _upload("x.pdf", "Completely rewritten")


@pytest.mark.anyio
async def test_uploads_without_the_flag_keep_working():
    await _upload("veera.pdf", reject=False)
    await _upload("veera.pdf", reject=False)
    assert await get_database().resumes.count_documents({"user_id": USER}) == 2


@pytest.mark.anyio
async def test_endpoint_answers_409_rather_than_folding_it_into_a_generic_error():
    await _upload("veera.pdf")
    with pytest.raises(HTTPException) as err:
        await upload_resumes(files=None, file=_file("copy.pdf"), reject_duplicates=True, user={"_id": USER})
    assert err.value.status_code == 409
    assert err.value.detail["message"] == "This resume already exists in your library."
