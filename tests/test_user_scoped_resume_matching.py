"""
Recommended resumes must come only from the owning user's resume library.
Runs against the isolated test database configured in conftest.py.
"""
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from bson import ObjectId
from httpx import AsyncClient, ASGITransport

from app.config import settings
from app.core.security import create_access_token
from app.database import connect_db, close_db, get_database
from app.main import app
from app.schemas.job import JDStructuredData, JobMatchResult
from app.services import matching_service as matching_module
from app.services.matching_service import matching_service

JD = JDStructuredData(role="Data Engineer", required_skills=["Python", "Spark", "SQL"])


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
async def test_db(monkeypatch):
    assert "test" in settings.DATABASE_NAME
    await connect_db()
    db = get_database()
    await db.client.drop_database(settings.DATABASE_NAME)
    # Deterministic offline matching unless a test installs its own LLM stub
    monkeypatch.setattr(matching_module.llm_service, "get_client", lambda: None)
    yield db
    await db.client.drop_database(settings.DATABASE_NAME)
    await close_db()


async def make_user(db, email: str) -> ObjectId:
    res = await db.users.insert_one({"email": email, "name": email.split("@")[0], "created_at": datetime.now(timezone.utc)})
    return res.inserted_id


async def make_resume(db, user_id, name: str, skills, as_str: bool = False) -> str:
    res = await db.resumes.insert_one({
        "user_id": str(user_id) if as_str else user_id,
        "file_name": f"{name}.pdf",
        "display_name": name,
        "target_role": "Data Engineer",
        "skills": skills,
        "created_at": datetime.now(timezone.utc),
    })
    return str(res.inserted_id)


async def make_job(db, user_id, ext_id: str = "ext-1") -> str:
    res = await db.jobs.insert_one({
        "user_id": str(user_id),
        "external_job_id": ext_id,
        "title": "Data Engineer",
        "company": "Acme",
        "description_raw": "Python Spark SQL",
        "description_structured": JD.model_dump(),
        "status": "ANALYZED",
        "created_at": datetime.now(timezone.utc),
    })
    return str(res.inserted_id)


def auth(user_id) -> dict:
    return {"Authorization": f"Bearer {create_access_token(str(user_id))}"}


@pytest.mark.anyio
async def test_match_uses_only_own_resumes(test_db):
    alice, bob = await make_user(test_db, "alice@test.io"), await make_user(test_db, "bob@test.io")
    alice_resume = await make_resume(test_db, alice, "Alice Weak", ["Python"])
    # Bob's resume is a far better fit, but must never be recommended to Alice
    await make_resume(test_db, bob, "Bob Perfect", ["Python", "Spark", "SQL"])

    result = await matching_service.match_job_against_all_resumes(JD, user_id=str(alice))

    assert result.recommended_resume_id == alice_resume
    assert all(a.resume_id == alice_resume for a in result.alternatives)


@pytest.mark.anyio
async def test_string_and_objectid_owner_ids_both_match(test_db):
    alice = await make_user(test_db, "alice@test.io")
    r1 = await make_resume(test_db, alice, "Alice OID", ["Python"])
    r2 = await make_resume(test_db, alice, "Alice STR", ["Python", "Spark", "SQL"], as_str=True)

    result = await matching_service.match_job_against_all_resumes(JD, user_id=str(alice))

    assert result.recommended_resume_id == r2
    assert {result.recommended_resume_id, *(a.resume_id for a in result.alternatives)} == {r1, r2}


@pytest.mark.anyio
async def test_user_without_resumes_gets_none_not_someone_elses(test_db):
    alice, bob = await make_user(test_db, "alice@test.io"), await make_user(test_db, "bob@test.io")
    await make_resume(test_db, bob, "Bob Perfect", ["Python", "Spark", "SQL"])

    result = await matching_service.match_job_against_all_resumes(JD, user_id=str(alice))

    assert result.recommended_resume_id == ""
    assert result.recommended_resume_name == "No resumes available"


@pytest.mark.anyio
@pytest.mark.parametrize("user_id", [None, "", "default"])
async def test_anonymous_matching_sees_no_resumes(test_db, user_id):
    bob = await make_user(test_db, "bob@test.io")
    await make_resume(test_db, bob, "Bob Perfect", ["Python", "Spark", "SQL"])

    result = await matching_service.match_job_against_all_resumes(JD, user_id=user_id)

    assert result.recommended_resume_id == ""


@pytest.mark.anyio
async def test_llm_picking_foreign_resume_is_rejected(test_db, monkeypatch):
    alice, bob = await make_user(test_db, "alice@test.io"), await make_user(test_db, "bob@test.io")
    alice_resume = await make_resume(test_db, alice, "Alice", ["Python"])
    bob_resume = await make_resume(test_db, bob, "Bob", ["Python", "Spark", "SQL"])

    async def fake_parse(**_kwargs):
        parsed = JobMatchResult(recommended_resume_id=bob_resume, match_percentage=99, reason="x")
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(parsed=parsed))])

    client = SimpleNamespace(beta=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(parse=fake_parse))))
    monkeypatch.setattr(matching_module.llm_service, "get_client", lambda: client)

    result = await matching_service.match_job_against_all_resumes(JD, user_id=str(alice))

    assert result.recommended_resume_id == alice_resume


@pytest.mark.anyio
async def test_match_endpoint_saves_owner_resume_and_blocks_other_users(test_db):
    alice, bob = await make_user(test_db, "alice@test.io"), await make_user(test_db, "bob@test.io")
    alice_resume = await make_resume(test_db, alice, "Alice", ["Python", "Spark"])
    await make_resume(test_db, bob, "Bob", ["Python", "Spark", "SQL"])
    job_id = await make_job(test_db, alice)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        res = await ac.post(f"/api/v1/jobs/{job_id}/match", headers=auth(alice))
        assert res.status_code == 200
        assert res.json()["match_result"]["recommended_resume_id"] == alice_resume

        # Bob cannot re-match (and overwrite) Alice's job with his library
        res = await ac.post(f"/api/v1/jobs/{job_id}/match", headers=auth(bob))
        assert res.status_code == 404

        # Jobs Explorer listing for Alice shows only her resume
        res = await ac.get("/api/v1/jobs", headers=auth(alice))
        items = res.json() if isinstance(res.json(), list) else res.json()["items"]
        assert [j["match_result"]["recommended_resume_id"] for j in items] == [alice_resume]


@pytest.mark.anyio
async def test_match_direct_endpoint_is_user_scoped(test_db, monkeypatch):
    alice, bob = await make_user(test_db, "alice@test.io"), await make_user(test_db, "bob@test.io")
    alice_resume = await make_resume(test_db, alice, "Alice", ["Python"])
    await make_resume(test_db, bob, "Bob", ["Python", "Spark", "SQL"])

    async def fake_analyze(*_a, **_k):
        return JD
    monkeypatch.setattr("app.services.job_service.jd_service.analyze_job_description", fake_analyze)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        res = await ac.post("/api/v1/jobs/match-direct", headers=auth(alice),
                            json={"description_raw": "We need Python, Spark and SQL engineers."})
    assert res.status_code == 200
    assert res.json()["match_result"]["recommended_resume_id"] == alice_resume
    job = await test_db.jobs.find_one({"_id": ObjectId(res.json()["job_id"])})
    assert job["user_id"] == str(alice)


@pytest.mark.anyio
async def test_search_auto_match_and_dedupe_are_per_user(test_db, monkeypatch):
    from app.services.search_service import search_service

    alice, bob = await make_user(test_db, "alice@test.io"), await make_user(test_db, "bob@test.io")
    alice_resume = await make_resume(test_db, alice, "Alice", ["Python"])
    bob_resume = await make_resume(test_db, bob, "Bob", ["Python", "Spark", "SQL"])
    profile = await test_db.search_profiles.insert_one({"name": "DE", "keywords": ["data"], "user_id": str(alice)})
    bob_job_id = await make_job(test_db, bob, ext_id="dice-123")  # Bob already has this Dice job

    scraped = {
        "external_job_id": "dice-123", "title": "Data Engineer", "company": "Acme", "location": "Remote",
        "work_setting": "Remote", "salary": "", "employment_type": "Full-time", "posted_date": "Today",
        "is_easy_apply": True, "job_url": "https://dice.test/job", "application_url": "",
        "application_wizard_url": "", "description_raw": "Python Spark SQL", "is_applied": False,
    }

    async def fake_search(**_k):
        return [dict(scraped)]

    async def fake_analyze(*_a, **_k):
        return JD

    monkeypatch.setattr("app.services.search_service.dice_browser.search_jobs", fake_search)
    monkeypatch.setattr("app.services.search_service.jd_service.analyze_job_description", fake_analyze)

    result = await search_service.run_search(str(profile.inserted_id), auto_match=True, auto_queue=False, user_id=str(alice))

    assert result["new_jobs_added"] == 1
    alice_job = await test_db.jobs.find_one({"external_job_id": "dice-123", "user_id": str(alice)})
    assert alice_job["match_result"]["recommended_resume_id"] == alice_resume
    bob_job = await test_db.jobs.find_one({"_id": ObjectId(bob_job_id)})
    assert "match_result" not in bob_job and bob_job["user_id"] == str(bob)
    assert bob_resume not in str(alice_job["match_result"])


@pytest.mark.anyio
async def test_enqueue_never_uses_another_users_resume(test_db):
    from app.services.application_queue import application_queue_manager

    alice, bob = await make_user(test_db, "alice@test.io"), await make_user(test_db, "bob@test.io")
    alice_resume = await make_resume(test_db, alice, "Alice", ["Python"])
    bob_resume = await make_resume(test_db, bob, "Bob", ["Python", "Spark", "SQL"])
    job_id = await make_job(test_db, alice)

    await application_queue_manager.enqueue_job(
        job_id=job_id, resume_id=bob_resume, user_id=str(alice), mode="PREPARE", start_worker=False
    )

    app_doc = await test_db.applications.find_one({"job_id": ObjectId(job_id)})
    assert str(app_doc["resume_id"]) == alice_resume


@pytest.mark.anyio
async def test_repair_script_clears_cross_user_matches(test_db, tmp_path):
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "fix_matches", Path(__file__).resolve().parent.parent / "scripts" / "fix_cross_user_resume_matches.py"
    )
    fix = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fix)

    alice, bob = await make_user(test_db, "alice@test.io"), await make_user(test_db, "bob@test.io")
    alice_resume = await make_resume(test_db, alice, "Alice", ["Python"])
    bob_resume = await make_resume(test_db, bob, "Bob", ["Python", "Spark", "SQL"])
    good_job = await make_job(test_db, alice, ext_id="good")
    bad_job = await make_job(test_db, alice, ext_id="bad")
    orphan_job = await make_job(test_db, alice, ext_id="orphan")
    for jid, rid in [(good_job, alice_resume), (bad_job, bob_resume), (orphan_job, str(ObjectId()))]:
        await test_db.jobs.update_one({"_id": ObjectId(jid)}, {"$set": {
            "status": "MATCHED", "match_result": {"recommended_resume_id": rid, "alternatives": []}}})
        await test_db.job_matches.insert_one({"job_id": jid, "recommended_resume_id": rid})

    dry = await fix.repair(apply=False, rematch=False, backup_dir=tmp_path)
    assert dry["affected_jobs"] == 2
    assert (await test_db.jobs.find_one({"_id": ObjectId(bad_job)}))["match_result"]  # dry run changes nothing

    res = await fix.repair(apply=True, rematch=True, backup_dir=tmp_path)
    assert res == {"affected_jobs": 2, "foreign_applications": 0, "rematched": 2}

    # Verify backup artifact was written into tmp_path and NOT into scripts/backups/
    written_backups = list(tmp_path.glob("jobs_cross_user_matches_*.json"))
    assert len(written_backups) == 1

    for jid in (bad_job, orphan_job):
        job = await test_db.jobs.find_one({"_id": ObjectId(jid)})
        assert job["match_result"]["recommended_resume_id"] == alice_resume
    good = await test_db.jobs.find_one({"_id": ObjectId(good_job)})
    assert good["match_result"]["recommended_resume_id"] == alice_resume
    assert await test_db.job_matches.count_documents({"recommended_resume_id": bob_resume}) == 0
    assert (await fix.repair(apply=False, rematch=False, backup_dir=tmp_path))["affected_jobs"] == 0
