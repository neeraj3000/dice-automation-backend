"""
Master Security Remediation Regression Test Suite
Covers SEC-001 through SEC-007, API-001 through API-005, and OPS-001/TEST-003.
Runs in isolated test environment with synthetic users, disposable DB, and mocked external services.
"""
import pytest
from bson import ObjectId
from httpx import AsyncClient, ASGITransport

from app.config import settings
from app.core.security import create_access_token
from app.database import connect_db, close_db, get_database
from app.main import app
from app.services import cloudinary_service

@pytest.fixture(autouse=True)
async def setup_test_env(monkeypatch):
    await connect_db()
    # Mock Cloudinary so no real external calls are made
    monkeypatch.setattr(cloudinary_service, "is_cloudinary_configured", lambda: True)
    monkeypatch.setattr(cloudinary_service, "upload_resume", lambda data, user_id="default", file_name="r.pdf": {
        "cloudinary_url": f"https://mock.cloudinary/{user_id}/{file_name}",
        "cloudinary_public_id": f"resumes/{user_id}/{file_name}"
    })
    monkeypatch.setattr(cloudinary_service, "delete_resume", lambda public_id: None)
    yield
    await close_db()

import uuid

async def create_user(db, email: str) -> tuple[str, str, dict]:
    uid = str(ObjectId())
    unique_email = f"{uuid.uuid4().hex[:8]}_{email}"
    await db.users.insert_one({"_id": ObjectId(uid), "email": unique_email, "name": email.split("@")[0]})
    token = create_access_token(uid)
    headers = {"Authorization": f"Bearer {token}"}
    return uid, token, headers

# ---------------------------------------------------------------------------
# SEC-001: Resumes Security & Tenant Isolation
# ---------------------------------------------------------------------------
@pytest.mark.anyio
async def test_sec001_unauthenticated_resume_endpoints_rejected():
    transport = ASGITransport(app=app)
    fake_id = str(ObjectId())
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Delete unauthenticated -> 401
        r_del = await client.delete(f"/api/resumes/{fake_id}")
        assert r_del.status_code == 401

        # List unauthenticated -> 401
        r_list = await client.get("/api/resumes")
        assert r_list.status_code == 401

        # Get by id unauthenticated -> 401
        r_get = await client.get(f"/api/resumes/{fake_id}")
        assert r_get.status_code == 401

        # Put unauthenticated -> 401
        r_put = await client.put(f"/api/resumes/{fake_id}", json={"display_name": "New"})
        assert r_put.status_code == 401

        # Set default unauthenticated -> 401
        r_def = await client.post(f"/api/resumes/{fake_id}/set-default")
        assert r_def.status_code == 401

@pytest.mark.anyio
async def test_sec001_cross_user_resume_isolation():
    db = get_database()
    transport = ASGITransport(app=app)

    # Create User A and User B
    uid_a, _, headers_a = await create_user(db, "userA@test.com")
    uid_b, _, headers_b = await create_user(db, "userB@test.com")

    # Seed resume for User A
    resume_a_id = ObjectId()
    await db.resumes.insert_one({
        "_id": resume_a_id,
        "user_id": str(uid_a),
        "file_name": "alice_resume.pdf",
        "display_name": "Alice Senior Resume",
        "target_role": "Data Engineer",
        "skills": ["Python", "Spark"],
        "raw_text": "Alice Data Engineer Python",
        "is_default": False
    })

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # User B attempts to read User A's resume -> 404
        r_get = await client.get(f"/api/resumes/{resume_a_id}", headers=headers_b)
        assert r_get.status_code == 404

        # User B attempts to update User A's resume -> 404
        r_put = await client.put(f"/api/resumes/{resume_a_id}", json={"display_name": "Hacked"}, headers=headers_b)
        assert r_put.status_code == 404
        doc = await db.resumes.find_one({"_id": resume_a_id})
        assert doc["display_name"] == "Alice Senior Resume"  # Unchanged

        # User B attempts to set User A's resume as default -> 404
        r_def = await client.post(f"/api/resumes/{resume_a_id}/set-default", headers=headers_b)
        assert r_def.status_code == 404

        # User B attempts to delete User A's resume -> 404
        r_del = await client.delete(f"/api/resumes/{resume_a_id}", headers=headers_b)
        assert r_del.status_code == 404
        assert await db.resumes.find_one({"_id": resume_a_id}) is not None  # Not deleted

        # User A can legitimately read and delete their own resume
        r_owner_get = await client.get(f"/api/resumes/{resume_a_id}", headers=headers_a)
        assert r_owner_get.status_code == 200
        r_owner_del = await client.delete(f"/api/resumes/{resume_a_id}", headers=headers_a)
        assert r_owner_del.status_code == 200
        assert await db.resumes.find_one({"_id": resume_a_id}) is None

# ---------------------------------------------------------------------------
# SEC-002: Job Deletion Tenant Isolation
# ---------------------------------------------------------------------------
@pytest.mark.anyio
async def test_sec002_job_deletion_cross_user_isolation():
    db = get_database()
    transport = ASGITransport(app=app)

    uid_a, _, headers_a = await create_user(db, "jobA@test.com")
    uid_b, _, headers_b = await create_user(db, "jobB@test.com")

    # Seed job owned by User A
    job_a_id = ObjectId()
    await db.jobs.insert_one({
        "_id": job_a_id,
        "user_id": str(uid_a),
        "title": "Lead Software Engineer",
        "company": "Tech Corp",
        "status": "DISCOVERED"
    })

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Unauthenticated deletion attempt -> 401
        unauth_del = await client.delete(f"/api/jobs/{job_a_id}")
        assert unauth_del.status_code == 401

        # User B attempts to delete User A's job -> 404
        cross_del = await client.delete(f"/api/jobs/{job_a_id}", headers=headers_b)
        assert cross_del.status_code == 404
        assert await db.jobs.find_one({"_id": job_a_id}) is not None  # Intact

        # User B attempts clear_all_jobs -> does not clear User A's job
        clear_res = await client.delete("/api/jobs/clear", headers=headers_b)
        assert clear_res.status_code == 200
        assert await db.jobs.find_one({"_id": job_a_id}) is not None  # Still intact

        # User A deletes their own job -> 204
        owner_del = await client.delete(f"/api/jobs/{job_a_id}", headers=headers_a)
        assert owner_del.status_code == 204
        assert await db.jobs.find_one({"_id": job_a_id}) is None

# ---------------------------------------------------------------------------
# SEC-003: Restrict Credentialed CORS
# ---------------------------------------------------------------------------
@pytest.mark.anyio
async def test_sec003_cors_rejects_arbitrary_origins():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Malicious third-party origin
        res = await client.options(
            "/api/health",
            headers={
                "Origin": "https://malicious-attacker-website.com",
                "Access-Control-Request-Method": "POST",
            }
        )
        # Must NOT reflect the malicious origin in Access-Control-Allow-Origin
        acao = res.headers.get("access-control-allow-origin")
        assert acao != "https://malicious-attacker-website.com"

        # Approved dev origin
        res_ok = await client.options(
            "/api/health",
            headers={
                "Origin": "http://localhost:5173",
                "Access-Control-Request-Method": "GET",
            }
        )
        assert res_ok.headers.get("access-control-allow-origin") == "http://localhost:5173"

# ---------------------------------------------------------------------------
# SEC-004 & OPS-001: Production Startup Lifespan Validation
# ---------------------------------------------------------------------------
@pytest.mark.anyio
async def test_sec004_production_startup_rejects_insecure_secrets(monkeypatch):
    from app.main import lifespan

    # Test 1: Production with default JWT secret must fail
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    monkeypatch.setattr(settings, "JWT_SECRET", "dice-auto-apply-default-jwt-secret-key-32bytes-min")
    monkeypatch.setattr(settings, "SESSION_ENCRYPTION_KEY", "ValidKey32BytesLongSecretPaddingA=")
    monkeypatch.setattr(settings, "CORS_ORIGINS", "https://apply2hire.com")

    with pytest.raises(RuntimeError, match="JWT_SECRET"):
        async with lifespan(app):
            pass

    # Test 2: Production with missing SESSION_ENCRYPTION_KEY must fail
    monkeypatch.setattr(settings, "JWT_SECRET", "a-very-strong-unique-production-secret-key-32chars")
    monkeypatch.setattr(settings, "SESSION_ENCRYPTION_KEY", "")
    with pytest.raises(RuntimeError, match="SESSION_ENCRYPTION_KEY"):
        async with lifespan(app):
            pass

    # Test 3: Production with wildcard CORS must fail
    monkeypatch.setattr(settings, "SESSION_ENCRYPTION_KEY", "ValidKey32BytesLongSecretPaddingA=")
    monkeypatch.setattr(settings, "CORS_ORIGINS", "*")
    with pytest.raises(RuntimeError, match="CORS_ORIGINS"):
        async with lifespan(app):
            pass

# ---------------------------------------------------------------------------
# SEC-005 & API-001: Applications Operations Authorization
# ---------------------------------------------------------------------------
@pytest.mark.anyio
async def test_sec005_application_operations_require_auth_and_ownership():
    db = get_database()
    transport = ASGITransport(app=app)

    uid_a, _, headers_a = await create_user(db, "appA@test.com")
    uid_b, _, headers_b = await create_user(db, "appB@test.com")

    app_a_id = ObjectId()
    await db.applications.insert_one({
        "_id": app_a_id,
        "user_id": str(uid_a),
        "job_id": ObjectId(),
        "resume_id": ObjectId(),
        "status": "FAILED",
        "company": "Enterprise Co",
        "job_title": "Lead Architect",
        "progress_steps": []
    })

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Unauthenticated retry -> 401
        r_unauth_retry = await client.post(f"/api/applications/{app_a_id}/retry")
        assert r_unauth_retry.status_code == 401

        # Unauthenticated submit -> 401
        r_unauth_submit = await client.post(f"/api/applications/{app_a_id}/submit")
        assert r_unauth_submit.status_code == 401

        # Unauthenticated answer -> 401
        r_unauth_ans = await client.post(f"/api/applications/{app_a_id}/answers", json={"answers": []})
        assert r_unauth_ans.status_code == 401

        # User B cross-user retry User A's application -> 400 (application not found/unauthorized)
        r_cross_retry = await client.post(f"/api/applications/{app_a_id}/retry", headers=headers_b)
        assert r_cross_retry.status_code == 400

        # User B cross-user get User A's application detail -> 404
        r_cross_get = await client.get(f"/api/applications/{app_a_id}", headers=headers_b)
        assert r_cross_get.status_code == 404

        # User B cross-user submit User A's application -> 404
        r_cross_submit = await client.post(f"/api/applications/{app_a_id}/submit", headers=headers_b)
        assert r_cross_submit.status_code == 404

# ---------------------------------------------------------------------------
# SEC-006 & API-002, API-004: Dice Settings & Diagnostics Security
# ---------------------------------------------------------------------------
@pytest.mark.anyio
async def test_sec006_dice_settings_endpoints_protected():
    db = get_database()
    transport = ASGITransport(app=app)
    uid_a, _, headers_a = await create_user(db, "dice_owner@test.com")

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. POST /settings/import-dice-session unauthenticated -> 401
        r_import_unauth = await client.post("/api/settings/import-dice-session", json={"cookies": []})
        assert r_import_unauth.status_code == 401

        # 2. GET /settings/dice-session/stream unauthenticated -> 401
        r_stream_unauth = await client.get("/api/settings/dice-session/stream")
        assert r_stream_unauth.status_code == 401

        # 3. Test get_current_user_sse dependency directly with valid & invalid tokens
        from app.core.deps import get_current_user_sse
        from fastapi.security import HTTPAuthorizationCredentials
        auth_user = await get_current_user_sse(creds=HTTPAuthorizationCredentials(scheme="Bearer", credentials=headers_a["Authorization"].split()[1]))
        assert str(auth_user["_id"]) == str(uid_a)

        # 4. Stream ticket endpoint generates valid short-lived ticket
        r_ticket = await client.post("/api/settings/dice-session/stream-ticket", headers=headers_a)
        assert r_ticket.status_code == 200
        ticket_val = r_ticket.json()["ticket"]

        # 5. Verify ticket authenticates successfully
        ticket_user = await get_current_user_sse(ticket=ticket_val)
        assert str(ticket_user["_id"]) == str(uid_a)

        # 6. Verify invalid ticket rejected
        with pytest.raises(Exception):
            await get_current_user_sse(ticket="invalid.ticket.token")

        # 7. Verify session manager subscription lifecycle and connection cleanup
        from app.services.dice_session_manager import dice_session_manager
        q = await dice_session_manager.subscribe(user_id=str(uid_a))
        assert q in dice_session_manager._subscribers
        init_msg = await q.get()
        assert "INIT_STATE" in init_msg
        dice_session_manager.unsubscribe(q)
        assert q not in dice_session_manager._subscribers

        # 6. POST /settings/open-dice-login unauthenticated -> 401
        r_login_unauth = await client.post("/api/settings/open-dice-login")
        assert r_login_unauth.status_code == 401

        # 7. GET /jobs/debug-dice unauthenticated -> 401
        r_dbg_unauth = await client.get("/api/jobs/debug-dice")
        assert r_dbg_unauth.status_code == 401

# ---------------------------------------------------------------------------
# Parser Fallback Test
# ---------------------------------------------------------------------------
@pytest.mark.anyio
async def test_parser_fallback_handles_pdf(tmp_path):
    from app.services.parser_service import extract_text, extract_text_from_pdf
    # Create sample PDF using clean byte builder
    from tests.test_resumes import create_sample_pdf_bytes
    pdf_bytes = create_sample_pdf_bytes()
    pdf_file = tmp_path / "test_resume.pdf"
    pdf_file.write_bytes(pdf_bytes)

    extracted = extract_text_from_pdf(pdf_file)
    assert "Databricks" in extracted
    assert "PySpark" in extracted

# ---------------------------------------------------------------------------
# SEC-008: Avatar Authentication & Asset Destruction Protection
# ---------------------------------------------------------------------------
@pytest.mark.anyio
async def test_sec008_avatar_authorization_and_asset_isolation(monkeypatch):
    db = get_database()
    uid_a, token_a, h_a = await create_user(db, "avatar_a@example.com")
    uid_b, token_b, h_b = await create_user(db, "avatar_b@example.com")

    destroyed_ids = []
    import cloudinary.uploader
    monkeypatch.setattr(cloudinary.uploader, "destroy", lambda pid, **kw: destroyed_ids.append(pid))
    monkeypatch.setattr("app.api.avatar.is_cloudinary_configured", lambda: True)
    monkeypatch.setattr("app.api.avatar._configure", lambda: None)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. Unauthenticated requests rejected
        r_del_unauth = await client.request("DELETE", "/api/avatar", json={"public_id": "avatars/any/img.jpg"})
        assert r_del_unauth.status_code == 401

        r_up_unauth = await client.post("/api/avatar", json={"image_data": "data:image/jpeg;base64,ZmFrZQ=="})
        assert r_up_unauth.status_code == 401

        # 2. User B attempting to destroy User A's public_id is rejected / ignored
        foreign_pid = f"avatars/{uid_a}/photo.jpg"
        r_del_foreign = await client.request("DELETE", "/api/avatar", headers=h_b, json={"public_id": foreign_pid})
        assert r_del_foreign.status_code == 204
        assert foreign_pid not in destroyed_ids, "User B must not be able to trigger Cloudinary destruction of User A's avatar!"

# ---------------------------------------------------------------------------
# SEC-009: Review Queue Authorization & Question Isolation
# ---------------------------------------------------------------------------
@pytest.mark.anyio
async def test_sec009_review_queue_authorization():
    db = get_database()
    uid_a, token_a, h_a = await create_user(db, "review_a@example.com")
    uid_b, token_b, h_b = await create_user(db, "review_b@example.com")

    # Insert an application and review question for User A
    app_ins = await db.applications.insert_one({
        "user_id": uid_a,
        "company": "Review Corp",
        "job_title": "Review Engineer",
        "status": "REVIEW"
    })
    q_ins = await db.application_answers.insert_one({
        "application_id": app_ins.inserted_id,
        "question_text": "Do you require visa sponsorship?",
        "field_name": "sponsorship",
        "options": ["Yes", "No"],
        "answer_text": "",
        "is_answered": False
    })
    q_id = str(q_ins.inserted_id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. Unauthenticated access rejected
        r_list_unauth = await client.get("/api/review-queue")
        assert r_list_unauth.status_code == 401

        r_ans_unauth = await client.post(f"/api/review-queue/{q_id}/answer", json={"answer_text": "No"})
        assert r_ans_unauth.status_code == 401

        # 2. User A can view their question
        r_list_a = await client.get("/api/review-queue", headers=h_a)
        assert r_list_a.status_code == 200
        assert any(item["id"] == q_id for item in r_list_a.json())

        # 3. User B cannot see User A's review question
        r_list_b = await client.get("/api/review-queue", headers=h_b)
        assert r_list_b.status_code == 200
        assert not any(item["id"] == q_id for item in r_list_b.json())

        # 4. User B cannot answer User A's question
        r_ans_b = await client.post(f"/api/review-queue/{q_id}/answer", headers=h_b, json={"answer_text": "Yes"})
        assert r_ans_b.status_code == 404

        # 5. Question in DB remains unchanged
        q_doc = await db.application_answers.find_one({"_id": ObjectId(q_id)})
        assert q_doc["is_answered"] is False
        assert q_doc["answer_text"] == ""

# ---------------------------------------------------------------------------
# SEC-010: Search Profiles & Execution Isolation
# ---------------------------------------------------------------------------
@pytest.mark.anyio
async def test_sec010_search_profiles_isolation():
    db = get_database()
    uid_a, token_a, h_a = await create_user(db, "search_a@example.com")
    uid_b, token_b, h_b = await create_user(db, "search_b@example.com")

    # User A creates a search profile
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. Unauthenticated creation/listing rejected
        r_unauth_list = await client.get("/api/search-profiles")
        assert r_unauth_list.status_code == 401

        r_unauth_create = await client.post("/api/search-profiles", json={"name": "Secret Roles", "keywords": ["Python"]})
        assert r_unauth_create.status_code == 401

        # 2. User A creates profile
        r_create_a = await client.post("/api/search-profiles", headers=h_a, json={"name": "User A Profile", "keywords": ["Kubernetes"]})
        assert r_create_a.status_code == 200
        prof_a_id = r_create_a.json()["id"]

        # 3. User B cannot read User A's search profile
        r_get_b = await client.get(f"/api/search-profiles/{prof_a_id}", headers=h_b)
        assert r_get_b.status_code == 404

        # 4. User B cannot see it in their list
        r_list_b = await client.get("/api/search-profiles", headers=h_b)
        assert r_list_b.status_code == 200
        assert not any(p["id"] == prof_a_id for p in r_list_b.json())

        # 5. User B cannot update User A's profile
        r_put_b = await client.put(f"/api/search-profiles/{prof_a_id}", headers=h_b, json={"name": "Hacked Profile"})
        assert r_put_b.status_code == 404

        # 6. User B cannot delete User A's profile
        r_del_b = await client.delete(f"/api/search-profiles/{prof_a_id}", headers=h_b)
        assert r_del_b.status_code == 404

        # 7. User B cannot run search with User A's profile
        r_run_b = await client.post(f"/api/search-profiles/{prof_a_id}/run", headers=h_b)
        assert r_run_b.status_code == 404

        # Profile remains intact in DB
        prof_doc = await db.search_profiles.find_one({"_id": ObjectId(prof_a_id)})
        assert prof_doc["name"] == "User A Profile"

# ---------------------------------------------------------------------------
# SEC-011: Board Connections Isolation
# ---------------------------------------------------------------------------
@pytest.mark.anyio
async def test_sec011_board_connections_isolation():
    db = get_database()
    uid_a, token_a, h_a = await create_user(db, "board_a@example.com")
    uid_b, token_b, h_b = await create_user(db, "board_b@example.com")

    # Give User A a connected board in DB
    await db.board_connections.insert_one({
        "user_id": uid_a,
        "board": "dice",
        "board_key": "dice",
        "status": "CONNECTED",
        "is_connected": True,
        "username": "User A Dice Account",
        "email": "userA_dice@example.com"
    })

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. Unauthenticated cookie import rejected
        r_cookie_unauth = await client.post("/api/boards/dice/cookies", json=[{"name": "token", "value": "xyz"}])
        assert r_cookie_unauth.status_code == 401

        # 2. User A sees their connected status
        r_status_a = await client.get("/api/boards/dice/status", headers=h_a)
        assert r_status_a.status_code == 200
        assert r_status_a.json()["connected"] is True
        assert r_status_a.json()["username"] == "User A Dice Account"

        # 3. User B calling status does NOT see User A's connection
        r_status_b = await client.get("/api/boards/dice/status", headers=h_b)
        assert r_status_b.status_code == 200
        assert r_status_b.json()["connected"] is False
        assert r_status_b.json()["username"] != "User A Dice Account"

        # 4. User B calling disconnect does NOT disconnect User A's board
        r_disc_b = await client.post("/api/boards/dice/disconnect", headers=h_b)
        assert r_disc_b.status_code == 200
        doc_a = await db.board_connections.find_one({"user_id": uid_a, "board": "dice"})
        assert doc_a["status"] == "CONNECTED"

# ---------------------------------------------------------------------------
# SEC-012: Job Retrieval Object-Level Authorization
# ---------------------------------------------------------------------------
@pytest.mark.anyio
async def test_sec012_jobs_object_level_authorization():
    db = get_database()
    uid_a, token_a, h_a = await create_user(db, "job_sec_a@example.com")
    uid_b, token_b, h_b = await create_user(db, "job_sec_b@example.com")

    job_ins = await db.jobs.insert_one({
        "user_id": uid_a,
        "external_job_id": "ext_sec012_test_123",
        "title": "Principal Architect",
        "company": "Top Secret Tech",
        "status": "NEW"
    })
    job_id = str(job_ins.inserted_id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. Unauthenticated requests rejected
        r_list_unauth = await client.get("/api/jobs")
        assert r_list_unauth.status_code == 401

        r_get_unauth = await client.get(f"/api/jobs/{job_id}")
        assert r_get_unauth.status_code == 401

        # 2. User A can retrieve their job
        r_get_a = await client.get(f"/api/jobs/{job_id}", headers=h_a)
        assert r_get_a.status_code == 200
        assert r_get_a.json()["company"] == "Top Secret Tech"

        # 3. User B cannot retrieve User A's job
        r_get_b = await client.get(f"/api/jobs/{job_id}", headers=h_b)
        assert r_get_b.status_code == 404

        # 4. User B cannot match against User A's job
        r_match_b = await client.post(f"/api/jobs/{job_id}/match", headers=h_b)
        assert r_match_b.status_code == 404

# ---------------------------------------------------------------------------
# SEC-013: Dice Session Status Isolation
# ---------------------------------------------------------------------------
@pytest.mark.anyio
async def test_sec013_dice_session_status_isolation():
    db = get_database()
    uid_a, token_a, h_a = await create_user(db, "dicesess_a@example.com")
    uid_b, token_b, h_b = await create_user(db, "dicesess_b@example.com")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. Unauthenticated request returns safe disconnected status
        r_unauth = await client.get("/api/dice/session/status")
        assert r_unauth.status_code == 200
        assert r_unauth.json()["connected"] is False

        # 2. User B cannot pass ?user_id=uid_a to inspect User A's session
        r_b_probe = await client.get(f"/api/dice/session/status?user_id={uid_a}", headers=h_b)
        assert r_b_probe.status_code == 200
        # Should reflect User B's state (disconnected), not User A's
        assert r_b_probe.json()["connected"] is False

# ---------------------------------------------------------------------------
# SEC-014: Candidate Profile & Settings Isolation
# ---------------------------------------------------------------------------
@pytest.mark.anyio
async def test_sec014_profile_and_settings_isolation():
    db = get_database()
    uid_a, token_a, h_a = await create_user(db, "settings_a@example.com")
    uid_b, token_b, h_b = await create_user(db, "settings_b@example.com")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. Unauthenticated requests rejected
        assert (await client.get("/api/profile")).status_code == 401
        assert (await client.put("/api/profile", json={"first_name": "Test"})).status_code == 401
        assert (await client.get("/api/settings")).status_code == 401
        assert (await client.put("/api/settings", json={"headless": True})).status_code == 401

        # 2. User A updates profile
        r_put_a = await client.put("/api/profile", headers=h_a, json={
            "first_name": "Alice",
            "last_name": "Smith",
            "phone": "555-111-2222"
        })
        assert r_put_a.status_code == 200
        assert r_put_a.json()["first_name"] == "Alice"

        # 3. User B retrieves their profile -> does NOT see User A's data
        r_get_b = await client.get("/api/profile", headers=h_b)
        assert r_get_b.status_code == 200
        assert r_get_b.json().get("first_name") != "Alice"
        assert r_get_b.json().get("phone") != "555-111-2222"
