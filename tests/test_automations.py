"""
Timed automations: the runner applies until the target or the timer, skips jobs that need a
person into the Review Queue, stops on a lost Dice login, and never leaves an application
in flight after the run ends. Dice (search + browser) is faked; MongoDB is real (test DB).
"""
import asyncio
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

from bson import ObjectId

backend_dir = Path(__file__).resolve().parent.parent
if str(backend_dir) not in sys.path:
    sys.path.insert(0, str(backend_dir))

import pytest
from httpx import ASGITransport, AsyncClient

import app.services.automation_service as automation_module
from app.main import app
from app.database import close_db, connect_db, get_database
from app.services.automation_service import automation_service
from app.services.billing import entitlement_service

USER = "test_automation_user"
OTHER_USER = "test_automation_other"


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def _cleanup(db):
    users = {"$in": [USER, OTHER_USER]}
    app_ids = await db.applications.distinct("_id", {"user_id": users})
    await db.application_answers.delete_many({"application_id": {"$in": app_ids}})
    for col in ("automations", "automation_runs", "search_profiles", "jobs", "applications", "resumes"):
        await db[col].delete_many({"user_id": users})
    await db.users.delete_many({"_id": users})


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
    monkeypatch.setattr(automation_module, "COOLDOWN_SECONDS", 0)
    monkeypatch.setattr(automation_module, "WATCH_INTERVAL_SECONDS", 0.05)
    monkeypatch.setattr(automation_service, "_check_dice_session", AsyncMock())
    # Billing has its own tests; a stored trial expiring must not fail these
    monkeypatch.setattr(entitlement_service, "assert_can_apply", AsyncMock())
    monkeypatch.setattr("app.browser.playwright_manager.playwright_manager.close_context", AsyncMock())
    yield
    await _wait_for_runs()
    await _cleanup(db)
    await close_db()


async def _wait_for_runs(timeout: float = 15):
    if automation_service._tasks:
        await asyncio.wait(set(automation_service._tasks), timeout=timeout)


async def _seed(job_specs, apply_limit=10):
    """Creates a resume, a search profile, its jobs (keyed by external id) and an automation."""
    db = get_database()
    resume = await db.resumes.insert_one({"user_id": USER, "file_name": "test_resume.pdf", "display_name": "Test Resume"})
    profile = await db.search_profiles.insert_one({"user_id": USER, "name": "Test Profile", "keywords": ["python"]})
    job_ids = []
    for i, ext_id in enumerate(job_specs):
        res = await db.jobs.insert_one({
            "user_id": USER,
            "external_job_id": ext_id,
            "title": f"Job {ext_id}",
            "company": "Acme",
            "application_url": f"https://www.dice.com/job-detail/{ext_id}",
            "search_profile_id": profile.inserted_id,
            "status": "MATCHED",
            # Higher score first, so jobs are tried in the listed order
            "match_result": {"recommended_resume_id": str(resume.inserted_id), "match_percentage": 100 - i},
            "created_at": datetime.now(timezone.utc),
        })
        job_ids.append(str(res.inserted_id))
    automation = await db.automations.insert_one({
        "user_id": USER,
        "name": "Test automation",
        "search_profile_ids": [profile.inserted_id],
        "apply_limit": apply_limit,
        "duration_minutes": 30,
        "created_at": datetime.now(timezone.utc),
    })
    search_result = {"total_found": len(job_ids), "new_jobs_added": len(job_ids), "new_job_ids": job_ids}
    return str(automation.inserted_id), search_result


def _browser(outcomes, hold=None):
    """Fake application_browser.prepare_application: outcome per external job id."""
    async def fake(**kw):
        ext_id = kw["application_url"].rsplit("/", 1)[-1]
        cb = kw.get("status_callback")
        await cb("OPENING_JOB")
        if hold:
            await hold(ext_id, cb)
        status = outcomes[ext_id]
        res = {"success": status == "SUBMITTED", "status": status, "progress_steps": [f"fake {status}"], "unanswered_questions": []}
        if status == "REVIEW":
            res["unanswered_questions"] = [{"question_text": "Years of Rust?", "field_name": "rust", "options": []}]
        if status == "LOGIN_REQUIRED":
            res["unanswered_questions"] = [{"question_text": "Please sign in", "field_name": "login_required", "options": [], "is_login_prompt": True}]
        if status == "FAILED":
            res["failure_reason"] = "Something broke"
        if status == "UNCONFIRMED":
            # Submit clicked, but Dice never showed the job as Applied
            res.update(status="FAILED", failure_reason="Dice did not confirm", submit_unconfirmed=True)
        return res
    return fake


async def _run_doc(run_id):
    return await get_database().automation_runs.find_one({"_id": ObjectId(run_id)})


@pytest.mark.anyio
async def test_applies_until_target_and_routes_human_jobs_to_review_queue():
    db = get_database()
    automation_id, search = await _seed(["a1", "a2", "a3", "a4", "a5"], apply_limit=2)
    outcomes = {"a1": "SUBMITTED", "a2": "REVIEW", "a3": "READY", "a4": "SUBMITTED", "a5": "SUBMITTED"}

    with patch("app.services.search_service.search_service.run_search", AsyncMock(return_value=search)), \
         patch("app.services.application_service.application_browser.prepare_application", side_effect=_browser(outcomes)):
        run = await automation_service.start_run(USER, automation_id)
        await _wait_for_runs()

    doc = await _run_doc(run.id)
    assert doc["status"] == "COMPLETED"
    assert doc["counters"] == {"scraped": 5, "submitted": 2, "review": 2, "failed": 0}

    apps = await db.applications.find({"automation_run_id": ObjectId(run.id)}).to_list(10)
    by_title = {a["job_title"]: a["status"] for a in apps}
    assert by_title == {"Job a1": "SUBMITTED", "Job a2": "REVIEW", "Job a3": "READY", "Job a4": "SUBMITTED"}

    # Target reached before a5 was touched
    assert (await db.jobs.find_one({"external_job_id": "a5", "user_id": USER}))["status"] == "MATCHED"

    # Both human-intervention jobs show up in the Review Queue
    from app.services.application_service import application_service
    queue = await application_service.get_review_queue(user_id=USER)
    assert sorted(q.job_title for q in queue) == ["Job a2", "Job a3"]


@pytest.mark.anyio
async def test_timer_stops_run_and_cancels_inflight_application():
    db = get_database()
    automation_id, search = await _seed(["t1", "t2"])
    reached = asyncio.Event()

    async def hold(ext_id, cb):
        await cb("FILLING_APPLICATION")
        reached.set()
        await asyncio.sleep(60)

    with patch("app.services.search_service.search_service.run_search", AsyncMock(return_value=search)), \
         patch("app.services.application_service.application_browser.prepare_application",
               side_effect=_browser({"t1": "SUBMITTED", "t2": "SUBMITTED"}, hold=hold)):
        run = await automation_service.start_run(USER, automation_id)
        await asyncio.wait_for(reached.wait(), 10)
        automation_service._runs[run.id].ends_at = datetime.now(timezone.utc)  # time's up
        await _wait_for_runs()

    doc = await _run_doc(run.id)
    assert doc["status"] == "TIME_UP"
    app = await db.applications.find_one({"automation_run_id": ObjectId(run.id)})
    assert app["status"] == "CANCELLED"
    # The interrupted job is free to be picked up by the next run
    assert (await db.jobs.find_one({"external_job_id": "t1", "user_id": USER}))["status"] == "MATCHED"
    assert (await db.jobs.find_one({"external_job_id": "t2", "user_id": USER}))["status"] == "MATCHED"


@pytest.mark.anyio
async def test_application_mid_submit_gets_grace_to_finish():
    db = get_database()
    automation_id, search = await _seed(["g1", "g2"])
    submitting = asyncio.Event()

    async def hold(ext_id, cb):
        await cb("SUBMITTING")
        submitting.set()
        await asyncio.sleep(0.5)

    with patch("app.services.search_service.search_service.run_search", AsyncMock(return_value=search)), \
         patch("app.services.application_service.application_browser.prepare_application",
               side_effect=_browser({"g1": "SUBMITTED", "g2": "SUBMITTED"}, hold=hold)):
        run = await automation_service.start_run(USER, automation_id)
        await asyncio.wait_for(submitting.wait(), 10)
        automation_service._runs[run.id].ends_at = datetime.now(timezone.utc)
        await _wait_for_runs()

    doc = await _run_doc(run.id)
    assert doc["status"] == "TIME_UP"
    assert doc["counters"]["submitted"] == 1
    statuses = [a["status"] for a in await db.applications.find({"automation_run_id": ObjectId(run.id)}).to_list(5)]
    assert statuses == ["SUBMITTED"]  # g2 was never started


@pytest.mark.anyio
async def test_lost_dice_login_stops_run_without_review_item():
    db = get_database()
    automation_id, search = await _seed(["l1", "l2"])

    with patch("app.services.search_service.search_service.run_search", AsyncMock(return_value=search)), \
         patch("app.services.application_service.application_browser.prepare_application",
               side_effect=_browser({"l1": "LOGIN_REQUIRED", "l2": "SUBMITTED"})):
        run = await automation_service.start_run(USER, automation_id)
        await _wait_for_runs()

    doc = await _run_doc(run.id)
    assert doc["status"] == "LOGIN_REQUIRED"
    app = await db.applications.find_one({"automation_run_id": ObjectId(run.id)})
    assert await db.application_answers.count_documents({"application_id": app["_id"]}) == 0
    assert (await db.jobs.find_one({"external_job_id": "l1", "user_id": USER}))["status"] == "MATCHED"
    assert await db.applications.count_documents({"automation_run_id": ObjectId(run.id)}) == 1


@pytest.mark.anyio
async def test_api_crud_start_stop_and_single_run_guard():
    db = get_database()
    automation_id, search = await _seed(["s1", "s2"])
    profile = await db.search_profiles.find_one({"user_id": USER})
    other_profile = await db.search_profiles.insert_one({"user_id": OTHER_USER, "name": "Not mine", "keywords": ["x"]})
    await db.users.update_one({"_id": USER}, {"$set": {"email": "automation_test@example.com"}}, upsert=True)

    from app.core.security import create_access_token
    headers = {"Authorization": f"Bearer {create_access_token(USER)}"}
    started = asyncio.Event()

    async def hold(ext_id, cb):
        started.set()
        await asyncio.sleep(60)

    transport = ASGITransport(app=app)
    with patch("app.services.search_service.search_service.run_search", AsyncMock(return_value=search)), \
         patch("app.services.application_service.application_browser.prepare_application",
               side_effect=_browser({"s1": "SUBMITTED", "s2": "SUBMITTED"}, hold=hold)):
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # Another user's profile is rejected
            r = await client.post("/api/automations", headers=headers, json={"name": "Bad", "search_profile_ids": [str(other_profile.inserted_id)]})
            assert r.status_code == 400
            # Timer is capped at 4 hours
            r = await client.post("/api/automations", headers=headers, json={"name": "Long", "search_profile_ids": [str(profile["_id"])], "duration_minutes": 300})
            assert r.status_code == 422

            r = await client.post("/api/automations", headers=headers, json={"name": "Mine", "search_profile_ids": [str(profile["_id"])]})
            assert r.status_code == 200
            assert r.json()["apply_limit"] == 10 and r.json()["duration_minutes"] == 30

            r = await client.post(f"/api/automations/{automation_id}/start", headers=headers)
            assert r.status_code == 202
            run = r.json()
            assert run["is_active"] and 1790 <= run["remaining_seconds"] <= 1800
            assert run["ends_at"].endswith("Z")

            # One running automation per user
            r = await client.post(f"/api/automations/{automation_id}/start", headers=headers)
            assert r.status_code == 409
            r = await client.delete(f"/api/automations/{automation_id}", headers=headers)
            assert r.status_code == 409

            await asyncio.wait_for(started.wait(), 10)
            r = await client.get("/api/automation-runs/active", headers=headers)
            assert r.json()["current"]["job_title"] == "Job s1"

            r = await client.post(f"/api/automation-runs/{run['id']}/stop", headers=headers)
            assert r.status_code == 200
            await _wait_for_runs()

            r = await client.get(f"/api/automation-runs/{run['id']}", headers=headers)
            assert r.json()["status"] == "STOPPED" and not r.json()["is_active"]
            r = await client.get("/api/automation-runs/active", headers=headers)
            assert r.json() is None

            r = await client.get(f"/api/applications?automation_run_id={run['id']}", headers=headers)
            assert [a["status"] for a in r.json()] == ["CANCELLED"]

            r = await client.get("/api/automations", headers=headers)
            listed = {a["id"]: a for a in r.json()}
            assert listed[automation_id]["last_run"]["status"] == "STOPPED"


@pytest.mark.anyio
async def test_searches_deeper_until_nothing_new_then_completes():
    """A shallow search that only returns known jobs must not end the run early."""
    db = get_database()
    automation_id, _ = await _seed([], apply_limit=10)
    profile = await db.search_profiles.find_one({"user_id": USER})
    resume = await db.resumes.find_one({"user_id": USER})
    sizes = []

    async def fake_search(profile_id, auto_match=True, auto_queue=None, user_id="default", browser_profile_id="default", max_results=None, **kw):
        sizes.append(max_results)
        # Rounds 1 and 2 each surface one new job; round 3 finds nothing new
        if len(sizes) > 2:
            return {"total_found": max_results, "new_jobs_added": 0, "new_job_ids": []}
        ext_id = f"d{len(sizes)}"
        res = await db.jobs.insert_one({
            "user_id": USER, "external_job_id": ext_id, "title": f"Job {ext_id}", "company": "Acme",
            "application_url": f"https://www.dice.com/job-detail/{ext_id}", "search_profile_id": profile["_id"],
            "status": "MATCHED", "match_result": {"recommended_resume_id": str(resume["_id"]), "match_percentage": 90},
        })
        return {"total_found": max_results, "new_jobs_added": 1, "new_job_ids": [str(res.inserted_id)]}

    with patch("app.services.search_service.search_service.run_search", side_effect=fake_search), \
         patch("app.services.application_service.application_browser.prepare_application",
               side_effect=_browser({"d1": "SUBMITTED", "d2": "SUBMITTED"})):
        run = await automation_service.start_run(USER, automation_id)
        await _wait_for_runs()

    doc = await _run_doc(run.id)
    assert doc["status"] == "COMPLETED"
    assert doc["counters"]["submitted"] == 2 and doc["counters"]["scraped"] == 2
    assert sizes == [20, 40, 80]  # independent of the user's max_jobs_per_search; doubles each round
    assert any("Searching deeper" in e["message"] for e in doc["events"])


@pytest.mark.anyio
async def test_history_lists_all_runs_and_survives_automation_delete():
    db = get_database()
    automation_id, search = await _seed(["h1"], apply_limit=1)
    await db.users.update_one({"_id": USER}, {"$set": {"email": "automation_test@example.com"}}, upsert=True)
    from app.core.security import create_access_token
    headers = {"Authorization": f"Bearer {create_access_token(USER)}"}

    with patch("app.services.search_service.search_service.run_search", AsyncMock(return_value=search)), \
         patch("app.services.application_service.application_browser.prepare_application",
               side_effect=_browser({"h1": "SUBMITTED"})):
        first = await automation_service.start_run(USER, automation_id)
        await _wait_for_runs()
        await db.jobs.update_many({"user_id": USER}, {"$set": {"status": "MATCHED"}})
        second = await automation_service.start_run(USER, automation_id)
        await _wait_for_runs()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/api/automation-runs?limit=1", headers=headers)
        page = r.json()
        assert page["total"] == 2 and [i["id"] for i in page["items"]] == [second.id]
        assert page["items"][0]["events"] == []  # list stays light
        r = await client.get("/api/automation-runs?limit=1&skip=1", headers=headers)
        assert [i["id"] for i in r.json()["items"]] == [first.id]

        r = await client.get(f"/api/automation-runs/{first.id}", headers=headers)
        assert r.json()["counters"]["submitted"] == 1 and len(r.json()["events"]) >= 3

        r = await client.delete(f"/api/automations/{automation_id}", headers=headers)
        assert r.status_code == 200
        r = await client.get("/api/automation-runs", headers=headers)
        assert r.json()["total"] == 2
        assert r.json()["items"][0]["automation_name"] == "Test automation"


async def _wait_until(predicate, timeout=10):
    deadline = asyncio.get_running_loop().time() + timeout
    while not await predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.05)


@pytest.mark.anyio
async def test_pause_waits_for_current_job_freezes_timer_and_resumes():
    db = get_database()
    automation_id, search = await _seed(["p1", "p2"])
    started = []

    async def hold(ext_id, cb):
        started.append(ext_id)
        await asyncio.sleep(0.3)

    with patch("app.services.search_service.search_service.run_search", AsyncMock(return_value=search)), \
         patch("app.services.application_service.application_browser.prepare_application",
               side_effect=_browser({"p1": "SUBMITTED", "p2": "SUBMITTED"}, hold=hold)):
        run = await automation_service.start_run(USER, automation_id)
        async def has_started():
            return bool(started)
        await _wait_until(has_started)
        await automation_service.set_paused(USER, run.id, True)  # mid-job: p1 finishes first

        async def is_paused():
            return bool((await _run_doc(run.id)).get("paused"))
        await _wait_until(is_paused)
        ends_before = automation_service._runs[run.id].ends_at
        frozen = (await automation_service.get_active_run(USER)).remaining_seconds
        await asyncio.sleep(1.2)
        active = await automation_service.get_active_run(USER)
        assert active.paused and active.remaining_seconds == frozen  # timer frozen
        assert started == ["p1"]  # p2 not started while paused
        assert (await db.applications.find_one({"job_title": "Job p1"}))["status"] == "SUBMITTED"

        await automation_service.set_paused(USER, run.id, False)
        await _wait_for_runs()

    doc = await _run_doc(run.id)
    assert doc["status"] == "COMPLETED" and doc["counters"]["submitted"] == 2
    assert doc["paused_seconds"] >= 1
    assert _aware_dt(doc["ends_at"]) - ends_before >= timedelta(seconds=1)  # deadline pushed out by the pause


def _aware_dt(dt):
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


@pytest.mark.anyio
async def test_timer_cannot_expire_while_paused_and_stop_works_when_paused():
    automation_id, search = await _seed(["q1", "q2"])
    with patch("app.services.search_service.search_service.run_search", AsyncMock(return_value=search)), \
         patch("app.services.application_service.application_browser.prepare_application",
               side_effect=_browser({"q1": "SUBMITTED", "q2": "SUBMITTED"})):
        run = await automation_service.start_run(USER, automation_id)
        await automation_service.set_paused(USER, run.id, True)

        async def is_paused():
            return bool((await _run_doc(run.id)).get("paused"))
        await _wait_until(is_paused)
        automation_service._runs[run.id].ends_at = datetime.now(timezone.utc)  # timer would be up
        await asyncio.sleep(0.5)
        assert (await _run_doc(run.id))["status"] == "RUNNING"

        await automation_service.stop_run(USER, run.id)
        await _wait_for_runs()

    doc = await _run_doc(run.id)
    assert doc["status"] == "STOPPED" and doc["paused"] is False


@pytest.mark.anyio
async def test_long_pause_stops_the_run(monkeypatch):
    monkeypatch.setattr(automation_module, "MAX_PAUSE_SECONDS", 0.3)
    automation_id, search = await _seed(["m1"])
    with patch("app.services.search_service.search_service.run_search", AsyncMock(return_value=search)), \
         patch("app.services.application_service.application_browser.prepare_application",
               side_effect=_browser({"m1": "SUBMITTED"})):
        run = await automation_service.start_run(USER, automation_id)
        await automation_service.set_paused(USER, run.id, True)
        await _wait_for_runs()
    doc = await _run_doc(run.id)
    assert doc["status"] == "STOPPED" and "Paused for more than" in doc["end_reason"]


@pytest.mark.anyio
async def test_headless_follows_each_users_own_setting(monkeypatch):
    from app.browser.playwright_manager import playwright_manager
    db = get_database()
    await db.settings.delete_many({"user_id": {"$in": [USER, OTHER_USER]}})
    await db.settings.insert_many([
        {"user_id": USER, "headless_browser": True},
        {"user_id": OTHER_USER, "headless_browser": False},
    ])
    for var in ("HEADLESS", "HEADLESS_BROWSER", "BROWSER_MODE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr("app.config.settings.HEADLESS", None)
    monkeypatch.setattr("app.config.settings.HEADLESS_BROWSER", None)
    monkeypatch.setattr("app.config.settings.BROWSER_MODE", "local")
    try:
        assert await playwright_manager.resolve_headless("default", user_id=USER) is True
        assert await playwright_manager.resolve_headless("default", user_id=OTHER_USER) is False
        assert await playwright_manager.resolve_headless(USER) is True  # user's own profile
        assert await playwright_manager.resolve_headless("default", headless=False, user_id=USER) is False

        # Server mode forces headless whatever the user chose
        monkeypatch.setenv("BROWSER_MODE", "server")
        assert playwright_manager.headless_locked
        assert await playwright_manager.resolve_headless("default", user_id=OTHER_USER) is True
    finally:
        await db.settings.delete_many({"user_id": {"$in": [USER, OTHER_USER]}})


@pytest.mark.anyio
async def test_dice_refusing_submits_ends_run_and_keeps_jobs_for_next_run():
    """Three submits in a row that Dice never marks Applied: stop instead of burning every job."""
    db = get_database()
    automation_id, search = await _seed(["u1", "u2", "u3", "u4", "u5"], apply_limit=5)
    outcomes = {"u1": "SUBMITTED", "u2": "UNCONFIRMED", "u3": "UNCONFIRMED", "u4": "UNCONFIRMED", "u5": "SUBMITTED"}

    with patch("app.services.search_service.search_service.run_search", AsyncMock(return_value=search)), \
         patch("app.services.application_service.application_browser.prepare_application", side_effect=_browser(outcomes)):
        run = await automation_service.start_run(USER, automation_id)
        await _wait_for_runs()

    doc = await _run_doc(run.id)
    assert doc["status"] == "FAILED"
    assert "Dice stopped accepting applications" in doc["end_reason"]
    assert doc["counters"]["submitted"] == 1 and doc["counters"]["failed"] == 3
    # u5 was never tried
    assert await db.applications.count_documents({"automation_run_id": ObjectId(run.id)}) == 4
    jobs = {j["external_job_id"]: j["status"] async for j in db.jobs.find({"user_id": USER})}
    assert jobs == {"u1": "APPLIED", "u2": "MATCHED", "u3": "MATCHED", "u4": "MATCHED", "u5": "MATCHED"}


@pytest.mark.anyio
async def test_unconfirmed_streak_resets_after_a_confirmed_submit():
    automation_id, search = await _seed(["r1", "r2", "r3", "r4", "r5"], apply_limit=2)
    outcomes = {"r1": "UNCONFIRMED", "r2": "UNCONFIRMED", "r3": "SUBMITTED", "r4": "UNCONFIRMED", "r5": "SUBMITTED"}

    with patch("app.services.search_service.search_service.run_search", AsyncMock(return_value=search)), \
         patch("app.services.application_service.application_browser.prepare_application", side_effect=_browser(outcomes)):
        run = await automation_service.start_run(USER, automation_id)
        await _wait_for_runs()

    doc = await _run_doc(run.id)
    assert doc["status"] == "COMPLETED"
    assert doc["counters"]["submitted"] == 2 and doc["counters"]["failed"] == 3


@pytest.mark.anyio
@pytest.mark.parametrize("apply_limit, n_profiles, first_size", [(70, 1, 40), (70, 7, 20), (5, 1, 20), (30, 2, 30)])
async def test_first_search_round_stays_shallow(apply_limit, n_profiles, first_size):
    """A big target must not make the first search scrape (and match) 140 jobs before applying."""
    db = get_database()
    automation_id, _ = await _seed([], apply_limit=apply_limit)
    extra = [
        (await db.search_profiles.insert_one({"user_id": USER, "name": f"P{i}", "keywords": ["x"]})).inserted_id
        for i in range(n_profiles - 1)
    ]
    if extra:
        await db.automations.update_one({"_id": ObjectId(automation_id)}, {"$push": {"search_profile_ids": {"$each": extra}}})
    sizes = []

    async def fake_search(profile_id, max_results=None, on_progress=None, **kw):
        sizes.append(max_results)
        await on_progress("Dice returned 0 result(s).")
        return {"total_found": 0, "new_jobs_added": 0, "new_job_ids": []}

    with patch("app.services.search_service.search_service.run_search", side_effect=fake_search):
        run = await automation_service.start_run(USER, automation_id)
        await _wait_for_runs()

    assert sizes[:n_profiles] == [first_size] * n_profiles
    doc = await _run_doc(run.id)
    assert any(e["message"] == "“Test Profile”: Dice returned 0 result(s)." for e in doc["events"])


async def _add_profile_with_job(automation_id, name, ext_id, resume_id):
    db = get_database()
    profile = await db.search_profiles.insert_one({"user_id": USER, "name": name, "keywords": ["x"]})
    job = await db.jobs.insert_one({
        "user_id": USER, "external_job_id": ext_id, "title": f"Job {ext_id}", "company": "Acme",
        "application_url": f"https://www.dice.com/job-detail/{ext_id}", "search_profile_id": profile.inserted_id,
        "status": "DISCOVERED", "match_result": {"recommended_resume_id": str(resume_id), "match_percentage": 50},
    })
    await db.automations.update_one({"_id": ObjectId(automation_id)}, {"$push": {"search_profile_ids": profile.inserted_id}})
    return str(profile.inserted_id), str(job.inserted_id)


@pytest.mark.anyio
async def test_next_profile_is_searched_while_current_jobs_are_applied():
    """Searching and applying overlap, so the applying side never waits on Dice's search."""
    db = get_database()
    automation_id, search = await _seed(["p1"], apply_limit=5)
    resume = await db.resumes.find_one({"user_id": USER})
    p2_id, p2_job = await _add_profile_with_job(automation_id, "Second", "p2", resume["_id"])
    timeline = []
    second_search_started = asyncio.Event()

    async def fake_search(profile_id, **kw):
        if profile_id == p2_id:
            timeline.append("search2 start")
            second_search_started.set()
            await asyncio.sleep(0.3)
            await db.jobs.update_one({"_id": ObjectId(p2_job)}, {"$set": {"status": "MATCHED"}})
            return {"total_found": 1, "new_jobs_added": 1, "new_job_ids": [p2_job]}
        return search

    async def hold(ext_id, cb):
        if ext_id == "p1":
            timeline.append("apply1 start")
            # Profile 2's search must already be running while p1 is being applied to
            await asyncio.wait_for(second_search_started.wait(), 5)
            timeline.append("apply1 end")

    with patch("app.services.search_service.search_service.run_search", side_effect=fake_search), \
         patch("app.services.application_service.application_browser.prepare_application",
               side_effect=_browser({"p1": "SUBMITTED", "p2": "SUBMITTED"}, hold=hold)):
        run = await automation_service.start_run(USER, automation_id)
        await _wait_for_runs()

    doc = await _run_doc(run.id)
    assert doc["counters"]["submitted"] == 2
    assert timeline.index("search2 start") < timeline.index("apply1 end")
    assert any(e["message"].startswith("Applied to Job p1 at Acme (1/5) in ") for e in doc["events"])


@pytest.mark.anyio
async def test_jobs_found_earlier_are_applied_while_the_first_search_runs():
    db = get_database()
    automation_id, _ = await _seed(["b1"], apply_limit=1)  # b1: found by an earlier search
    search_done = asyncio.Event()

    async def slow_search(profile_id, **kw):
        await asyncio.sleep(1.5)
        search_done.set()
        return {"total_found": 0, "new_jobs_added": 0, "new_job_ids": []}

    applied_during_search = []

    async def hold(ext_id, cb):
        applied_during_search.append(not search_done.is_set())

    with patch("app.services.search_service.search_service.run_search", side_effect=slow_search), \
         patch("app.services.application_service.application_browser.prepare_application",
               side_effect=_browser({"b1": "SUBMITTED"}, hold=hold)):
        run = await automation_service.start_run(USER, automation_id)
        await _wait_for_runs()

    doc = await _run_doc(run.id)
    assert doc["status"] == "COMPLETED" and doc["counters"]["submitted"] == 1
    assert applied_during_search == [True]


@pytest.mark.anyio
async def test_application_finished_on_dice_is_recorded_even_if_timer_ends_right_then():
    """Timer runs out after Dice accepted the job but before the outcome is saved: still counted."""
    db = get_database()
    automation_id, search = await _seed(["t1", "t2"])
    submitted = asyncio.Event()

    async def hold(ext_id, cb):
        await cb("SUBMITTED")
        submitted.set()
        await asyncio.sleep(0.3)

    with patch("app.services.search_service.search_service.run_search", AsyncMock(return_value=search)), \
         patch("app.services.application_service.application_browser.prepare_application",
               side_effect=_browser({"t1": "SUBMITTED", "t2": "SUBMITTED"}, hold=hold)):
        run = await automation_service.start_run(USER, automation_id)
        await asyncio.wait_for(submitted.wait(), 10)
        automation_service._runs[run.id].ends_at = datetime.now(timezone.utc)
        await _wait_for_runs()

    doc = await _run_doc(run.id)
    assert doc["status"] == "TIME_UP" and doc["counters"]["submitted"] == 1
    statuses = [a["status"] for a in await db.applications.find({"automation_run_id": ObjectId(run.id)}).to_list(5)]
    assert statuses == ["SUBMITTED"]
