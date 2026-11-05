"""
Resumes selected on a search profile: the selection is validated against the user's library,
matching only considers it, and an automation run never applies with a resume outside it.
Dice (search + browser) is faked; MongoDB is real (test DB).
"""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from bson import ObjectId
from fastapi import HTTPException

import app.services.automation_service as automation_module
import app.services.matching_service as matching_module
from app.database import close_db, connect_db, get_database
from app.schemas.job import JDStructuredData
from app.schemas.search_profile import SearchProfileCreate, SearchProfileUpdate
from app.services.application_service import application_service
from app.services.automation_service import automation_service
from app.services.billing import entitlement_service
from app.services.matching_service import matching_service, resume_pool_filter
from app.services.search_service import search_service

USER = "test_profile_resumes_user"
OTHER_USER = "test_profile_resumes_other"
JD = JDStructuredData(role="Data Engineer", required_skills=["Python", "Spark", "SQL"])


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def _cleanup(db):
    users = {"$in": [USER, OTHER_USER]}
    for col in ("automations", "automation_runs", "search_profiles", "jobs", "applications", "resumes"):
        await db[col].delete_many({"user_id": users})


@pytest.fixture(autouse=True)
async def setup_env(monkeypatch, tmp_path):
    await connect_db()
    db = get_database()
    await _cleanup(db)

    @asynccontextmanager
    async def fake_resume_file_for(resume):
        path = tmp_path / "resume.pdf"
        path.write_bytes(b"%PDF-1.4 test resume")
        yield path

    monkeypatch.setattr("app.services.application_service.resume_file_for", fake_resume_file_for)
    monkeypatch.setattr(matching_module.llm_service, "get_client", lambda: None)
    monkeypatch.setattr(automation_module, "COOLDOWN_SECONDS", 0)
    monkeypatch.setattr(automation_module, "WATCH_INTERVAL_SECONDS", 0.05)
    monkeypatch.setattr(automation_service, "_check_dice_session", AsyncMock())
    monkeypatch.setattr(entitlement_service, "assert_can_apply", AsyncMock())
    monkeypatch.setattr("app.browser.playwright_manager.playwright_manager.close_context", AsyncMock())
    yield db
    if automation_service._tasks:
        await asyncio.wait(set(automation_service._tasks), timeout=15)
    await _cleanup(db)
    await close_db()


async def _resume(name, skills=(), user=USER, role="Data Engineer"):
    res = await get_database().resumes.insert_one({
        "user_id": user, "file_name": f"{name}.pdf", "display_name": name, "skills": list(skills),
        "target_role": role, "created_at": datetime.now(timezone.utc),
    })
    return res.inserted_id


async def _profile(name, resume_ids=None):
    now = datetime.now(timezone.utc)
    doc = {"user_id": USER, "name": name, "keywords": ["python"], "created_at": now, "updated_at": now}
    if resume_ids is not None:
        doc["resume_ids"] = resume_ids
    return (await get_database().search_profiles.insert_one(doc)).inserted_id


async def _job(ext_id, profile_id, recommended=None, score=90):
    doc = {
        "user_id": USER, "external_job_id": ext_id, "title": f"Job {ext_id}", "company": "Acme",
        "application_url": f"https://www.dice.com/job-detail/{ext_id}",
        "search_profile_id": profile_id, "status": "MATCHED",
        "description_structured": JD.model_dump(), "created_at": datetime.now(timezone.utc),
    }
    if recommended:
        doc["match_result"] = {"recommended_resume_id": str(recommended), "match_percentage": score}
    return (await get_database().jobs.insert_one(doc)).inserted_id


async def _automation(profile_ids, apply_limit=10):
    res = await get_database().automations.insert_one({
        "user_id": USER, "name": "Run", "search_profile_ids": profile_ids,
        "apply_limit": apply_limit, "duration_minutes": 30, "created_at": datetime.now(timezone.utc),
    })
    return str(res.inserted_id)


async def _submit(**kw):
    await kw["status_callback"]("OPENING_JOB")
    return {"success": True, "status": "SUBMITTED", "progress_steps": ["fake"], "unanswered_questions": []}


async def _run(automation_id):
    search = AsyncMock(return_value={"total_found": 0, "new_jobs_added": 0, "new_job_ids": []})
    with patch("app.services.search_service.search_service.run_search", search), \
         patch("app.services.application_service.application_browser.prepare_application", side_effect=_submit):
        run = await automation_service.start_run(USER, automation_id)
        await asyncio.wait(set(automation_service._tasks), timeout=15)
    db = get_database()
    apps = await db.applications.find({"automation_run_id": ObjectId(run.id)}).to_list(50)
    return await db.automation_runs.find_one({"_id": ObjectId(run.id)}), {a["job_title"]: a["resume_id"] for a in apps}


# ----- Saving a selection ------------------------------------------------------------------------

@pytest.mark.anyio
async def test_profile_saves_selection_in_order_without_duplicates():
    a, b = await _resume("A"), await _resume("B")
    created = await search_service.create_profile(
        SearchProfileCreate(name="P", keywords=["python"], resume_ids=[str(b), str(a), str(b), " "]), user_id=USER
    )
    assert created.resume_ids == [str(b), str(a)]

    stored = await get_database().search_profiles.find_one({"_id": ObjectId(created.id)})
    assert stored["resume_ids"] == [b, a]
    assert (await search_service.get_profile_by_id(created.id, user_id=USER)).resume_ids == [str(b), str(a)]


@pytest.mark.anyio
async def test_legacy_profile_without_field_reads_as_whole_library():
    profile_id = await _profile("Legacy")
    assert (await search_service.get_profile_by_id(str(profile_id), user_id=USER)).resume_ids == []


@pytest.mark.anyio
@pytest.mark.parametrize("bad", ["foreign", "missing", "malformed"])
async def test_selection_rejects_resumes_outside_the_library(bad):
    mine = await _resume("Mine")
    ids = {
        "foreign": str(await _resume("Theirs", user=OTHER_USER)),
        "missing": str(ObjectId()),
        "malformed": "not-an-object-id",
    }
    payload = SearchProfileCreate(name="P", keywords=["python"], resume_ids=[str(mine), ids[bad]])
    with pytest.raises(HTTPException) as err:
        await search_service.create_profile(payload, user_id=USER)
    assert err.value.status_code == 400
    assert await get_database().search_profiles.count_documents({"user_id": USER}) == 0


@pytest.mark.anyio
async def test_update_changes_clears_and_preserves_selection():
    a, b = await _resume("A"), await _resume("B")
    pid = str(await _profile("P", [a]))

    res = await search_service.update_profile(pid, SearchProfileUpdate(resume_ids=[str(b)]), user_id=USER)
    assert res.resume_ids == [str(b)]
    # An update that doesn't mention resumes, or sends null, leaves them alone
    res = await search_service.update_profile(pid, SearchProfileUpdate(name="Renamed"), user_id=USER)
    assert res.resume_ids == [str(b)]
    res = await search_service.update_profile(pid, SearchProfileUpdate(resume_ids=None), user_id=USER)
    assert res.resume_ids == [str(b)]
    res = await search_service.update_profile(pid, SearchProfileUpdate(resume_ids=[]), user_id=USER)
    assert res.resume_ids == []

    with pytest.raises(HTTPException):
        await search_service.update_profile(pid, SearchProfileUpdate(resume_ids=[str(ObjectId())]), user_id=USER)


# ----- Matching ----------------------------------------------------------------------------------

def test_pool_filter_never_widens_a_selection_of_bad_ids():
    assert resume_pool_filter(USER, ["bad"])["_id"] == {"$in": []}
    assert "_id" not in resume_pool_filter(USER, [])
    assert resume_pool_filter("default", [str(ObjectId())]) is None


@pytest.mark.anyio
async def test_matching_only_considers_the_selection():
    weak = await _resume("Weak", ["Python"])
    await _resume("Perfect", ["Python", "Spark", "SQL"])

    result = await matching_service.match_job_against_all_resumes(JD, user_id=USER, resume_ids=[str(weak)])
    assert result.recommended_resume_id == str(weak)
    assert result.alternatives == []

    unrestricted = await matching_service.match_job_against_all_resumes(JD, user_id=USER)
    assert unrestricted.recommended_resume_name.startswith("Perfect")


@pytest.mark.anyio
async def test_manual_search_matches_new_jobs_within_the_selection():
    weak = await _resume("Weak", ["Python"])
    await _resume("Perfect", ["Python", "Spark", "SQL"])
    profile_id = await _profile("P", [weak])
    scraped = [{
        "external_job_id": "s1", "title": "Data Engineer", "company": "Acme", "location": "Remote",
        "work_setting": "Remote", "salary": "", "employment_type": "", "posted_date": "", "is_easy_apply": True,
        "job_url": "https://www.dice.com/job-detail/s1", "application_url": "https://www.dice.com/job-detail/s1",
        "application_wizard_url": "", "description_raw": "Python Spark SQL",
    }]
    with patch("app.services.search_service.dice_browser.search_jobs", AsyncMock(return_value=scraped)), \
         patch("app.services.search_service.jd_service.analyze_job_description", AsyncMock(return_value=JD)):
        await search_service.run_search(str(profile_id), auto_queue=False, user_id=USER)

    job = await get_database().jobs.find_one({"external_job_id": "s1", "user_id": USER})
    assert job["match_result"]["recommended_resume_id"] == str(weak)


# ----- Automation runs ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_run_applies_each_job_with_its_own_profiles_selection():
    a = await _resume("Same Name", ["Python"], role="Data Engineer")
    b = await _resume("Same Name", ["Python"], role="")
    p1, p2 = await _profile("One", [a]), await _profile("Two", [b])
    await _job("j1", p1, a)
    await _job("j2", p2, b)

    doc, used = await _run(await _automation([p1, p2]))

    assert doc["status"] == "COMPLETED"
    assert used == {"Job j1": a, "Job j2": b}
    messages = [e["message"] for e in doc["events"]]
    assert "“One” applies only with: Data Engineer." in messages
    assert "“Two” applies only with: Same Name." in messages


@pytest.mark.anyio
async def test_stale_match_outside_the_selection_is_rematched_within_it():
    selected = await _resume("Selected", ["Python"])
    other = await _resume("Other", ["Python", "Spark", "SQL"])
    profile_id = await _profile("P", [selected])
    job_id = await _job("j1", profile_id, other)

    _, used = await _run(await _automation([profile_id]))

    assert used == {"Job j1": selected}
    job = await get_database().jobs.find_one({"_id": job_id})
    assert job["match_result"]["recommended_resume_id"] == str(selected)


@pytest.mark.anyio
async def test_recommended_resume_inside_the_selection_skips_rematching():
    a = await _resume("A")
    b = await _resume("B")
    profile_id = await _profile("P", [a, b])
    await _job("j1", profile_id, b)

    with patch.object(matching_service, "match_job_against_all_resumes", AsyncMock()) as match:
        _, used = await _run(await _automation([profile_id]))

    assert used == {"Job j1": b}
    match.assert_not_awaited()


@pytest.mark.anyio
async def test_failed_matching_falls_back_inside_the_selection():
    selected = await _resume("Selected")
    other = await _resume("Other")
    profile_id = await _profile("P", [selected])
    await _job("j1", profile_id, other)

    empty = AsyncMock(return_value=type("Match", (), {"recommended_resume_id": ""})())
    with patch.object(matching_service, "match_job_against_all_resumes", empty):
        _, used = await _run(await _automation([profile_id]))

    assert used == {"Job j1": selected}


@pytest.mark.anyio
async def test_profile_without_selection_still_uses_the_whole_library():
    await _resume("Selected-elsewhere")
    best = await _resume("Best")
    profile_id = await _profile("Legacy")
    await _job("j1", profile_id, best)

    doc, used = await _run(await _automation([profile_id]))

    assert used == {"Job j1": best}
    assert not any("applies only with" in e["message"] for e in doc["events"])


@pytest.mark.anyio
async def test_partly_deleted_selection_keeps_the_rest():
    kept = await _resume("Kept", ["Python"])
    gone = ObjectId()
    await _resume("Unselected", ["Python", "Spark", "SQL"])
    profile_id = await _profile("P", [gone, kept])
    await _job("j1", profile_id, gone)

    _, used = await _run(await _automation([profile_id]))

    assert used == {"Job j1": kept}


@pytest.mark.anyio
async def test_start_is_refused_when_a_profiles_whole_selection_was_deleted():
    await _resume("Unselected")
    profile_id = await _profile("Orphaned", [ObjectId()])
    await _job("j1", profile_id)
    automation_id = await _automation([profile_id])

    with pytest.raises(HTTPException) as err:
        await automation_service.start_run(USER, automation_id)

    assert err.value.status_code == 400
    assert "Orphaned" in err.value.detail
    assert await get_database().automation_runs.count_documents({"user_id": USER}) == 0


@pytest.mark.anyio
async def test_selection_deleted_mid_run_never_falls_back_to_other_resumes():
    selected = await _resume("Selected")
    await _resume("Unselected")
    profile_id = await _profile("P", [selected])
    job_id = await _job("j1", profile_id, selected)

    async def delete_selection(*args, **kwargs):
        await get_database().resumes.delete_one({"_id": selected})
        return {"total_found": 0, "new_jobs_added": 0, "new_job_ids": []}

    with patch("app.services.search_service.search_service.run_search", AsyncMock(side_effect=delete_selection)), \
         patch("app.services.application_service.application_browser.prepare_application", side_effect=_submit) as browser:
        run = await automation_service.start_run(USER, await _automation([profile_id]))
        await asyncio.wait(set(automation_service._tasks), timeout=15)

    doc = await get_database().automation_runs.find_one({"_id": ObjectId(run.id)})
    assert doc["status"] == "FAILED"
    assert "no longer exist" in doc["end_reason"]
    browser.assert_not_called()
    # The job wasn't attempted, so it stays available for a later run
    assert (await get_database().jobs.find_one({"_id": job_id}))["status"] == "MATCHED"
