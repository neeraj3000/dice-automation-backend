"""
A job may only be marked applied after Apply2Hire itself submitted it and Dice then shows *this
job's own* apply button as "Applied". "Applied" badges on other jobs' cards (similar /
recommended jobs on the same page) and a pre-existing applied state must never count.
Runs the real detection code in headless Chromium against local pages (no Dice).
"""
import sys
from pathlib import Path

from bson import ObjectId

backend_dir = Path(__file__).resolve().parent.parent
if str(backend_dir) not in sys.path:
    sys.path.insert(0, str(backend_dir))

import pytest
from playwright.async_api import async_playwright

from app.browser.application_browser import ALREADY_APPLIED_REASON, application_browser
from app.database import close_db, connect_db, get_database
from app.schemas.user_profile import UserProfileSchema

JOB_URL = "https://www.dice.com/job-detail/own-job-123"

SIMILAR_JOBS = """
<section data-testid="similar-jobs">
  <a href="https://www.dice.com/job-detail/other-job-999"><h3>Other job</h3><button disabled>Applied</button></a>
  <div role="article"><span id="x">Another</span><button disabled>Applied</button></div>
</section>
"""


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def page():
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        pg = await browser.new_page()
        yield pg
        await browser.close()


async def _serve(page, body):
    async def handler(route):
        await route.fulfill(status=200, content_type="text/html", body=f"<html><body>{body}</body></html>")
    await page.route("https://www.dice.com/**", handler)
    await page.goto(JOB_URL)


@pytest.mark.anyio
async def test_other_jobs_applied_badges_are_ignored(page):
    await _serve(page, '<button data-testid="apply-button">Easy Apply</button>' + SIMILAR_JOBS)
    assert await application_browser._applied_signal(page) is None


@pytest.mark.anyio
async def test_own_applied_button_is_detected(page):
    await _serve(page, '<button data-testid="apply-button" disabled>Applied</button>' + SIMILAR_JOBS)
    signal = await application_browser._applied_signal(page)
    assert signal and "apply-button" in signal


@pytest.mark.anyio
async def test_job_already_applied_before_we_submit_is_skipped_not_submitted(page):
    await _serve(page, '<h1>Data Engineer</h1><button data-testid="apply-button" disabled>Applied</button>')
    res = await application_browser.prepare_application(
        application_url=JOB_URL, resume_file_path="resume.pdf",
        user_profile=UserProfileSchema(), mode="APPLY", page=page, user_id="test_verification_user",
    )
    assert res["status"] == "SKIPPED"
    assert res["failure_reason"] == ALREADY_APPLIED_REASON


@pytest.mark.anyio
async def test_missing_url_is_not_reported_as_submitted():
    res = await application_browser.prepare_application(
        application_url="", resume_file_path="resume.pdf", user_profile=UserProfileSchema(), mode="APPLY", page=object(),
    )
    assert res["status"] == "FAILED"


@pytest.mark.anyio
async def test_skipped_never_downgrades_a_job_we_applied_to():
    from app.services.application_service import application_service
    await connect_db()
    db = get_database()
    try:
        fresh = await db.jobs.insert_one({"user_id": "test_verification_user", "external_job_id": "test_v1", "status": "MATCHED"})
        ours = await db.jobs.insert_one({"user_id": "test_verification_user", "external_job_id": "test_v2", "status": "APPLIED"})
        for job_id in (fresh.inserted_id, ours.inserted_id):
            app = await db.applications.insert_one({"user_id": "test_verification_user", "job_id": job_id, "status": "APPLYING"})
            await application_service._finish_run(app.inserted_id, job_id, [], {
                "status": "SKIPPED", "progress_steps": [], "failure_reason": ALREADY_APPLIED_REASON,
            })
        assert (await db.jobs.find_one({"_id": fresh.inserted_id}))["status"] == "SKIPPED"
        assert (await db.jobs.find_one({"_id": ours.inserted_id}))["status"] == "APPLIED"
        statuses = {a["status"] async for a in db.applications.find({"user_id": "test_verification_user"})}
        assert statuses == {"SKIPPED"}
    finally:
        await db.jobs.delete_many({"user_id": "test_verification_user"})
        await db.applications.delete_many({"user_id": "test_verification_user"})
        await close_db()
