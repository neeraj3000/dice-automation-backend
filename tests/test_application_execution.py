import sys
import os
import json
import asyncio
from pathlib import Path
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, patch, MagicMock
from bson import ObjectId

backend_dir = Path(__file__).resolve().parent.parent
if str(backend_dir) not in sys.path:
    sys.path.insert(0, str(backend_dir))

import pytest
from httpx import AsyncClient, ASGITransport

from app.main import app
from app.database import connect_db, close_db, get_database
from app.models.session import DiceSession
from app.schemas.application import ApplicationState
from app.services.session_store import get_session_store
from app.services.application_runner import application_runner, is_retryable_error
from app.services.application_queue import application_queue_manager
from app.browser.application_browser import application_browser

TEST_ENCRYPTION_KEY = "test-app-execution-encryption-key-12345"


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
async def setup_env():
    os.environ["SESSION_ENCRYPTION_KEY"] = TEST_ENCRYPTION_KEY
    await application_queue_manager.stop_worker()
    await connect_db()
    db = get_database()
    if db is not None:
        try:
            await db.dice_sessions.delete_many({"user_id": {"$regex": "^test_"}})
            await db.jobs.delete_many({"external_job_id": {"$regex": "^test_"}})
            await db.applications.delete_many({"user_id": {"$regex": "^test_"}})
            await db.resumes.delete_many({"file_name": {"$regex": "^test_"}})
        except Exception:
            pass
    yield
    await application_queue_manager.stop_worker()
    if db is not None:
        try:
            await db.dice_sessions.delete_many({"user_id": {"$regex": "^test_"}})
            await db.jobs.delete_many({"external_job_id": {"$regex": "^test_"}})
            await db.applications.delete_many({"user_id": {"$regex": "^test_"}})
            await db.resumes.delete_many({"file_name": {"$regex": "^test_"}})
        except Exception:
            pass
    await close_db()


def create_valid_session(user_id: str) -> DiceSession:
    future = datetime.now(timezone.utc) + timedelta(days=7)
    return DiceSession(
        user_id=user_id,
        platform="dice",
        status="CONNECTED",
        cookies=[
            {"name": "identity", "value": "test_auth_jwt_token_sample_1234567890", "domain": ".dice.com", "path": "/"},
            {"name": "dice_member_id", "value": "test-candidate-id-999", "domain": ".dice.com", "path": "/"}
        ],
        local_storage={
            "CognitoIdentityServiceProvider.lastAuthUser": "test_user",
            "CognitoIdentityServiceProvider.accessToken": "test_access_token_token"
        },
        expires_at=future,
        last_verified_at=datetime.now(timezone.utc)
    )


# --- 1. Successful Application Test ---

@pytest.mark.anyio
async def test_successful_application():
    db = get_database()
    store = get_session_store()
    user_id = "test_user_success"

    # Save session
    sess = create_valid_session(user_id)
    await store.save_session(user_id, sess)

    # Insert test resume
    res_ins = await db.resumes.insert_one({
        "file_name": "test_resume.pdf",
        "display_name": "Test Candidate Resume",
        "file_path": "test_resume.pdf",
        "target_role": "Python Backend Engineer",
        "skills": ["Python", "FastAPI", "MongoDB"]
    })
    resume_id = res_ins.inserted_id

    # Insert test job
    job_ins = await db.jobs.insert_one({
        "external_job_id": "test_job_1",
        "title": "Senior Python Engineer",
        "company": "Tech Innovations LLC",
        "application_url": "https://www.dice.com/jobs/detail/test_job_1",
        "status": "DISCOVERED"
    })
    job_id = job_ins.inserted_id

    # Insert test application in QUEUED status
    app_ins = await db.applications.insert_one({
        "job_id": job_id,
        "resume_id": resume_id,
        "user_id": user_id,
        "company": "Tech Innovations LLC",
        "job_title": "Senior Python Engineer",
        "application_url": "https://www.dice.com/jobs/detail/test_job_1",
        "status": ApplicationState.QUEUED,
        "progress_steps": [],
        "retry_count": 0,
        "max_retries": 2
    })
    app_id = str(app_ins.inserted_id)

    # Mock Playwright & Browser behavior
    mock_page = AsyncMock()
    mock_context = AsyncMock()
    mock_context.new_page.return_value = mock_page

    with patch("app.browser.playwright_manager.playwright_manager.get_context", return_value=mock_context), \
         patch("app.services.application_runner.verify_dice_session", return_value={"state": "CONNECTED"}), \
         patch("app.browser.application_browser.application_browser.prepare_application", return_value={
             "success": True,
             "status": ApplicationState.SUBMITTED,
             "progress_steps": ["Navigated to job", "Contact info filled", "Resume selected", "Application submitted successfully"],
             "failure_reason": None
         }):

        result = await application_runner.run_application(
            application_id=app_id,
            user_id=user_id,
            mode="APPLY"
        )

        assert result["success"] is True
        assert result["status"] == ApplicationState.SUBMITTED

        # Verify MongoDB application status
        app_doc = await db.applications.find_one({"_id": ObjectId(app_id)})
        assert app_doc["status"] == ApplicationState.SUBMITTED
        assert app_doc["applied_at"] is not None

        # Verify MongoDB job status updated to APPLIED
        job_doc = await db.jobs.find_one({"_id": job_id})
        assert job_doc["status"] == "APPLIED"

        # Verify browser cleanup happened
        assert mock_page.close.called


# --- 2. Failed Application Test ---

@pytest.mark.anyio
async def test_failed_application():
    db = get_database()
    store = get_session_store()
    user_id = "test_user_failed"

    sess = create_valid_session(user_id)
    await store.save_session(user_id, sess)

    res_ins = await db.resumes.insert_one({"file_name": "test_resume.pdf", "display_name": "Test Candidate"})
    job_ins = await db.jobs.insert_one({
        "external_job_id": "test_job_fail",
        "title": "Software Engineer",
        "company": "Failing Corp",
        "application_url": "https://www.dice.com/jobs/detail/test_job_fail",
        "status": "DISCOVERED"
    })

    app_ins = await db.applications.insert_one({
        "job_id": job_ins.inserted_id,
        "resume_id": res_ins.inserted_id,
        "user_id": user_id,
        "company": "Failing Corp",
        "job_title": "Software Engineer",
        "status": ApplicationState.QUEUED
    })
    app_id = str(app_ins.inserted_id)

    mock_page = AsyncMock()
    mock_context = AsyncMock()
    mock_context.new_page.return_value = mock_page

    with patch("app.browser.playwright_manager.playwright_manager.get_context", return_value=mock_context), \
         patch("app.services.application_runner.verify_dice_session", return_value={"state": "CONNECTED"}), \
         patch("app.browser.application_browser.application_browser.prepare_application", return_value={
             "success": False,
             "status": ApplicationState.FAILED,
             "progress_steps": ["Navigated to job", "Form validation error on required questions"],
             "failure_reason": "Form validation error on required fields."
         }):

        result = await application_runner.run_application(
            application_id=app_id,
            user_id=user_id,
            mode="APPLY",
            max_retries=2
        )

        assert result["success"] is False
        assert result["status"] == ApplicationState.FAILED

        # Verify stored in MongoDB as FAILED
        app_doc = await db.applications.find_one({"_id": ObjectId(app_id)})
        assert app_doc["status"] == ApplicationState.FAILED
        assert "Form validation" in app_doc["failure_reason"]

        # Ensure non-retryable validation error did not retry 2 times
        assert app_doc.get("retry_count", 0) == 0


# --- 3. External Portal Detection Test ---

@pytest.mark.anyio
async def test_external_portal_detection():
    db = get_database()
    store = get_session_store()
    user_id = "test_user_external"

    sess = create_valid_session(user_id)
    await store.save_session(user_id, sess)

    res_ins = await db.resumes.insert_one({"file_name": "test_resume.pdf", "display_name": "Test Candidate"})
    job_ins = await db.jobs.insert_one({
        "external_job_id": "test_job_ext",
        "title": "Enterprise Architect",
        "company": "MegaCorp",
        "application_url": "https://megacorp.myworkdayjobs.com/en-US/careers/job/12345",
        "status": "DISCOVERED"
    })

    app_ins = await db.applications.insert_one({
        "job_id": job_ins.inserted_id,
        "resume_id": res_ins.inserted_id,
        "user_id": user_id,
        "company": "MegaCorp",
        "job_title": "Enterprise Architect",
        "status": ApplicationState.QUEUED
    })
    app_id = str(app_ins.inserted_id)

    mock_page = AsyncMock()
    mock_context = AsyncMock()
    mock_context.new_page.return_value = mock_page

    with patch("app.browser.playwright_manager.playwright_manager.get_context", return_value=mock_context), \
         patch("app.services.application_runner.verify_dice_session", return_value={"state": "CONNECTED"}), \
         patch("app.browser.application_browser.application_browser.prepare_application", return_value={
             "success": False,
             "status": ApplicationState.EXTERNAL_PORTAL,
             "progress_steps": ["Navigated to job", "Redirected to myworkdayjobs.com external portal"],
             "failure_reason": "Job redirected to external employer portal (myworkdayjobs.com)."
         }):

        result = await application_runner.run_application(
            application_id=app_id,
            user_id=user_id,
            mode="APPLY"
        )

        assert result["success"] is False
        assert result["status"] == ApplicationState.EXTERNAL_PORTAL

        # Verify status stored in MongoDB
        app_doc = await db.applications.find_one({"_id": ObjectId(app_id)})
        assert app_doc["status"] == ApplicationState.EXTERNAL_PORTAL
        assert app_doc.get("retry_count", 0) == 0

    # Also test ApplicationBrowser._is_external_portal directly
    assert application_browser._is_external_portal("https://company.myworkdayjobs.com/apply") is True
    assert application_browser._is_external_portal("https://boards.greenhouse.io/corp/jobs/1") is True
    assert application_browser._is_external_portal("https://jobs.lever.co/startup/abc") is True
    assert application_browser._is_external_portal("https://www.dice.com/jobs/detail/123") is False


# --- 4. CAPTCHA Detection Test ---

@pytest.mark.anyio
async def test_captcha_detection():
    db = get_database()
    store = get_session_store()
    user_id = "test_user_captcha"

    sess = create_valid_session(user_id)
    await store.save_session(user_id, sess)

    res_ins = await db.resumes.insert_one({"file_name": "test_resume.pdf", "display_name": "Test Candidate"})
    job_ins = await db.jobs.insert_one({
        "external_job_id": "test_job_captcha",
        "title": "Cloud Architect",
        "company": "CloudCorp",
        "application_url": "https://www.dice.com/jobs/detail/test_job_captcha",
        "status": "DISCOVERED"
    })

    app_ins = await db.applications.insert_one({
        "job_id": job_ins.inserted_id,
        "resume_id": res_ins.inserted_id,
        "user_id": user_id,
        "company": "CloudCorp",
        "job_title": "Cloud Architect",
        "status": ApplicationState.QUEUED
    })
    app_id = str(app_ins.inserted_id)

    mock_page = AsyncMock()
    mock_context = AsyncMock()
    mock_context.new_page.return_value = mock_page

    with patch("app.browser.playwright_manager.playwright_manager.get_context", return_value=mock_context), \
         patch("app.services.application_runner.verify_dice_session", return_value={"state": "CONNECTED"}), \
         patch("app.browser.application_browser.application_browser.prepare_application", return_value={
             "success": False,
             "status": ApplicationState.CAPTCHA_REQUIRED,
             "progress_steps": ["Navigating to job", "Cloudflare Turnstile challenge detected"],
             "failure_reason": "CAPTCHA challenge detected on page. Manual verification required."
         }):

        result = await application_runner.run_application(
            application_id=app_id,
            user_id=user_id,
            mode="APPLY"
        )

        assert result["success"] is False
        assert result["status"] == ApplicationState.CAPTCHA_REQUIRED

        app_doc = await db.applications.find_one({"_id": ObjectId(app_id)})
        assert app_doc["status"] == ApplicationState.CAPTCHA_REQUIRED
        # CAPTCHA must NOT be retried blindly
        assert app_doc.get("retry_count", 0) == 0

    # Also test ApplicationBrowser._detect_captcha directly
    mock_captcha_page = AsyncMock()
    mock_captcha_page.url = "https://www.dice.com/jobs/challenge"
    mock_captcha_page.title.return_value = "Just a moment..."
    mock_captcha_page.content.return_value = "<html><body>Please verify you are human</body></html>"
    is_cap = await application_browser._detect_captcha(mock_captcha_page)
    assert is_cap is True


# --- 5. Session Expiration Test ---

@pytest.mark.anyio
async def test_session_expiration():
    db = get_database()
    store = get_session_store()
    user_id = "test_user_expired"

    # Create expired session
    past = datetime.now(timezone.utc) - timedelta(hours=2)
    sess = DiceSession(
        user_id=user_id,
        platform="dice",
        status="CONNECTED",
        cookies=[{"name": "identity", "value": "test_expired_token_1234567890", "domain": ".dice.com", "path": "/"}],
        local_storage={"CognitoIdentityServiceProvider.lastAuthUser": "test_user"},
        expires_at=past
    )
    await store.save_session(user_id, sess)

    res_ins = await db.resumes.insert_one({"file_name": "test_resume.pdf", "display_name": "Test Candidate"})
    job_ins = await db.jobs.insert_one({
        "external_job_id": "test_job_exp",
        "title": "DevOps Engineer",
        "company": "InfraCorp",
        "application_url": "https://www.dice.com/jobs/detail/test_job_exp",
        "status": "DISCOVERED"
    })

    app_ins = await db.applications.insert_one({
        "job_id": job_ins.inserted_id,
        "resume_id": res_ins.inserted_id,
        "user_id": user_id,
        "company": "InfraCorp",
        "job_title": "DevOps Engineer",
        "status": ApplicationState.QUEUED
    })
    app_id = str(app_ins.inserted_id)

    result = await application_runner.run_application(
        application_id=app_id,
        user_id=user_id,
        mode="APPLY"
    )

    assert result["success"] is False
    assert result["status"] == ApplicationState.SESSION_EXPIRED

    app_doc = await db.applications.find_one({"_id": ObjectId(app_id)})
    assert app_doc["status"] == ApplicationState.SESSION_EXPIRED
    assert "expired" in app_doc["failure_reason"].lower()


# --- 6. Queue Status Transitions Test ---

@pytest.mark.anyio
async def test_queue_status_transitions():
    db = get_database()
    store = get_session_store()
    user_id = "test_user_queue_trans"

    sess = create_valid_session(user_id)
    await store.save_session(user_id, sess)

    res_ins = await db.resumes.insert_one({"file_name": "test_resume.pdf", "display_name": "Test Candidate"})
    job_ins = await db.jobs.insert_one({
        "external_job_id": "test_job_trans",
        "title": "Fullstack Engineer",
        "company": "WebTech",
        "application_url": "https://www.dice.com/jobs/detail/test_job_trans",
        "status": "DISCOVERED"
    })

    # Step 1: Enqueue job without starting worker immediately
    enq_res = await application_queue_manager.enqueue_job(
        job_id=str(job_ins.inserted_id),
        resume_id=str(res_ins.inserted_id),
        user_id=user_id,
        mode="APPLY",
        start_worker=False
    )

    assert enq_res["status"] == ApplicationState.QUEUED
    app_id = enq_res["application_id"]

    # Verify initial QUEUED status
    status_doc = await application_queue_manager.get_application_status(app_id)
    assert status_doc["status"] == ApplicationState.QUEUED

    recorded_states = []

    def tracking_callback(state: str, details=None):
        recorded_states.append(state)

    mock_page = AsyncMock()
    mock_context = AsyncMock()
    mock_context.new_page.return_value = mock_page

    async def mock_prepare_with_transitions(**kwargs):
        cb = kwargs.get("status_callback")
        if cb:
            await cb(ApplicationState.OPENING_JOB)
            await cb(ApplicationState.FILLING_APPLICATION)
            await cb(ApplicationState.UPLOADING_RESUME)
            await cb(ApplicationState.SUBMITTING)
        return {
            "success": True,
            "status": ApplicationState.SUBMITTED,
            "progress_steps": ["Navigated", "Filled", "Uploaded", "Submitted"],
            "failure_reason": None
        }

    with patch("app.browser.playwright_manager.playwright_manager.get_context", return_value=mock_context), \
         patch("app.services.application_runner.verify_dice_session", return_value={"state": "CONNECTED"}), \
         patch("app.browser.application_browser.application_browser.prepare_application", side_effect=mock_prepare_with_transitions):

        res = await application_runner.run_application(
            application_id=app_id,
            user_id=user_id,
            mode="APPLY",
            status_callback=tracking_callback
        )

        assert res["success"] is True
        assert res["status"] == ApplicationState.SUBMITTED

        # Verify all lifecycle states were triggered in sequence
        assert ApplicationState.STARTING in recorded_states
        assert ApplicationState.OPENING_JOB in recorded_states
        assert ApplicationState.FILLING_APPLICATION in recorded_states
        assert ApplicationState.UPLOADING_RESUME in recorded_states
        assert ApplicationState.SUBMITTING in recorded_states

        # Verify final MongoDB state
        final_doc = await application_queue_manager.get_application_status(app_id)
        assert final_doc["status"] == ApplicationState.SUBMITTED


# --- 7. Bounded Retries on Transient Timeout Test ---

@pytest.mark.anyio
async def test_bounded_retries_transient_failure():
    db = get_database()
    store = get_session_store()
    user_id = "test_user_timeout"

    sess = create_valid_session(user_id)
    await store.save_session(user_id, sess)

    res_ins = await db.resumes.insert_one({"file_name": "test_resume.pdf", "display_name": "Test Candidate"})
    job_ins = await db.jobs.insert_one({
        "external_job_id": "test_job_timeout",
        "title": "Site Reliability Engineer",
        "company": "NetworkCorp",
        "application_url": "https://www.dice.com/jobs/detail/test_job_timeout",
        "status": "DISCOVERED"
    })

    app_ins = await db.applications.insert_one({
        "job_id": job_ins.inserted_id,
        "resume_id": res_ins.inserted_id,
        "user_id": user_id,
        "company": "NetworkCorp",
        "job_title": "Site Reliability Engineer",
        "status": ApplicationState.QUEUED
    })
    app_id = str(app_ins.inserted_id)

    mock_page = AsyncMock()
    mock_context = AsyncMock()
    mock_context.new_page.return_value = mock_page

    call_count = 0

    async def mock_timeout_then_succeed(**kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # First attempt fails with timeout
            return {
                "success": False,
                "status": ApplicationState.TIMEOUT,
                "progress_steps": ["Navigating to page timed out"],
                "failure_reason": "Page load timed out."
            }
        # Second attempt succeeds
        return {
            "success": True,
            "status": ApplicationState.SUBMITTED,
            "progress_steps": ["Retry successful", "Application submitted"],
            "failure_reason": None
        }

    with patch("app.browser.playwright_manager.playwright_manager.get_context", return_value=mock_context), \
         patch("app.services.application_runner.verify_dice_session", return_value={"state": "CONNECTED"}), \
         patch("app.browser.application_browser.application_browser.prepare_application", side_effect=mock_timeout_then_succeed), \
         patch("asyncio.sleep", return_value=None):

        result = await application_runner.run_application(
            application_id=app_id,
            user_id=user_id,
            mode="APPLY",
            max_retries=2
        )

        assert result["success"] is True
        assert result["status"] == ApplicationState.SUBMITTED
        assert call_count == 2

        app_doc = await db.applications.find_one({"_id": ObjectId(app_id)})
        assert app_doc["status"] == ApplicationState.SUBMITTED
        assert app_doc["retry_count"] == 1


# --- 8. API Queue Endpoints Test ---

@pytest.mark.anyio
async def test_api_queue_and_status_endpoints():
    db = get_database()
    user_id = "test_user_api"

    res_ins = await db.resumes.insert_one({"file_name": "test_resume.pdf", "display_name": "Test Candidate"})
    job_ins = await db.jobs.insert_one({
        "external_job_id": "test_job_api",
        "title": "Backend API Engineer",
        "company": "API Dynamics",
        "application_url": "https://www.dice.com/jobs/detail/test_job_api",
        "status": "DISCOVERED"
    })
    job_id = str(job_ins.inserted_id)

    transport = ASGITransport(app=app)
    with patch("app.services.application_runner.application_runner.run_application"):
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # 1. Test POST /applications/queue
            q_res = await client.post("/api/applications/queue", json={
                "job_id": job_id,
                "user_id": user_id,
                "mode": "APPLY"
            })
            assert q_res.status_code == 200
            q_data = q_res.json()
            assert q_data["status"] == ApplicationState.QUEUED
            app_id = q_data["application_id"]

            # 2. Test GET /applications/queue/status
            qs_res = await client.get("/api/applications/queue/status")
            assert qs_res.status_code == 200
            qs_data = qs_res.json()
            assert "is_worker_running" in qs_data
            assert "counts_by_status" in qs_data
            assert qs_data["counts_by_status"].get(ApplicationState.QUEUED, 0) >= 1

            # 3. Test GET /applications/{app_id}/status
            astat_res = await client.get(f"/api/applications/{app_id}/status")
            assert astat_res.status_code == 200
            astat_data = astat_res.json()
            assert astat_data["id"] == app_id
            assert astat_data["status"] == ApplicationState.QUEUED
            assert astat_data["company"] == "API Dynamics"
