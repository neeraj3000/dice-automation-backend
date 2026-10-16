import sys
from pathlib import Path
from bson import ObjectId

backend_dir = Path(__file__).resolve().parent.parent
if str(backend_dir) not in sys.path:
    sys.path.insert(0, str(backend_dir))

import pytest
from httpx import AsyncClient, ASGITransport

from app.main import app
from app.database import get_database, connect_db, close_db
from app.core.security import create_access_token
from app.schemas.linkedin import StatusProvenance
from app.boards.registry import get_board
from app.boards.base import BoardError


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
async def setup_test_users():
    await connect_db()
    db = get_database()
    if db is not None:
        try:
            # Clean up test user records
            await db.users.delete_many({"email": {"$regex": "^test_linkedin_"}})
            await db.jobs.delete_many({"user_id": {"$regex": "^test_linkedin_"}})
            await db.applications.delete_many({"user_id": {"$regex": "^test_linkedin_"}})
        except Exception:
            pass
    yield
    if db is not None:
        try:
            await db.users.delete_many({"email": {"$regex": "^test_linkedin_"}})
            await db.jobs.delete_many({"user_id": {"$regex": "^test_linkedin_"}})
            await db.applications.delete_many({"user_id": {"$regex": "^test_linkedin_"}})
        except Exception:
            pass


async def create_user(db, email: str, name: str) -> tuple[str, str]:
    user_id = str(ObjectId())
    await db.users.insert_one({
        "_id": ObjectId(user_id),
        "email": email,
        "name": name,
        "is_active": True
    })
    token = create_access_token(user_id)
    return user_id, token


# 1. Authenticated Creation of User-Confirmed LinkedIn Application
@pytest.mark.anyio
async def test_create_user_confirmed_linkedin_application():
    db = get_database()
    user_id, token = await create_user(db, "test_linkedin_user1@example.com", "Alice Engineer")
    headers = {"Authorization": f"Bearer {token}"}

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        payload = {
            "job_url": "https://www.linkedin.com/jobs/view/4159827361/?trackingId=abc123xyz&refId=987",
            "job_title": "Lead Staff Architect",
            "company": "Stripe",
            "location": "Remote - US",
            "status": "APPLIED",
            "notes": "Submitted application via LinkedIn Easy Apply directly in browser"
        }

        resp = await client.post("/api/applications/external", json=payload, headers=headers)
        assert resp.status_code == 200, resp.text
        data = resp.json()

        assert data["job_title"] == "Lead Staff Architect"
        assert data["company"] == "Stripe"
        assert data["board"] == "linkedin"
        assert data["source"] == "linkedin"
        assert data["status"] == "APPLIED"
        # Status provenance must strictly be user_confirmed
        assert data["status_provenance"] == StatusProvenance.USER_CONFIRMED
        assert data["status_provenance"] != StatusProvenance.OFFICIALLY_VERIFIED
        # Canonical URL cleaned
        assert data["application_url"] == "https://www.linkedin.com/jobs/view/4159827361/"
        assert data["external_job_id"] == "4159827361"
        assert data["user_id"] == user_id
        assert data["notes"] == "Submitted application via LinkedIn Easy Apply directly in browser"
        assert len(data.get("status_history", [])) >= 1
        assert data["status_history"][0]["status"] == "APPLIED"
        assert data["status_history"][0]["status_provenance"] == StatusProvenance.USER_CONFIRMED


# 2. Rejection of Unauthenticated Requests
@pytest.mark.anyio
async def test_rejection_of_unauthenticated_requests():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        payload = {
            "job_url": "https://www.linkedin.com/jobs/view/4159827361/",
            "job_title": "Backend Dev",
            "company": "Acme"
        }
        resp = await client.post("/api/applications/external", json=payload)
        assert resp.status_code in (401, 403)

        list_resp = await client.get("/api/applications/external")
        assert list_resp.status_code in (401, 403)


# 3. Cross-User Isolation and Tenant Boundaries
@pytest.mark.anyio
async def test_cross_user_tenant_isolation():
    db = get_database()
    user1_id, token1 = await create_user(db, "test_linkedin_user_a@example.com", "User A")
    user2_id, token2 = await create_user(db, "test_linkedin_user_b@example.com", "User B")
    headers1 = {"Authorization": f"Bearer {token1}"}
    headers2 = {"Authorization": f"Bearer {token2}"}

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # User A creates application
        create_resp = await client.post("/api/applications/external", json={
            "job_url": "https://www.linkedin.com/jobs/view/999888777/",
            "job_title": "Security Architect",
            "company": "SecureCorp",
            "status": "APPLIED"
        }, headers=headers1)
        assert create_resp.status_code == 200
        app_id = create_resp.json()["id"]

        # User B attempts to read User A's application -> 404
        get_b = await client.get(f"/api/applications/external/{app_id}", headers=headers2)
        assert get_b.status_code == 404

        # User B attempts to update User A's application -> 404
        patch_b = await client.patch(f"/api/applications/external/{app_id}", json={
            "status": "INTERVIEWING"
        }, headers=headers2)
        assert patch_b.status_code == 404

        # User B attempts to view User A's status history -> 404
        hist_b = await client.get(f"/api/applications/external/{app_id}/history", headers=headers2)
        assert hist_b.status_code == 404

        # User B's list endpoint does NOT return User A's application
        list_b = await client.get("/api/applications/external", headers=headers2)
        assert list_b.status_code == 200
        b_ids = [item["id"] for item in list_b.json()]
        assert app_id not in b_ids


# 4. URL Validation, SSRF Protection and Canonicalization
@pytest.mark.anyio
async def test_url_validation_and_ssrf_protection():
    db = get_database()
    user_id, token = await create_user(db, "test_linkedin_url@example.com", "URL Tester")
    headers = {"Authorization": f"Bearer {token}"}

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Rejection of non-http/https protocols
        bad_schemes = [
            "javascript:alert(1)",
            "file:///etc/passwd",
            "ftp://files.example.com/job",
            "data:text/html,<h1>Malicious</h1>"
        ]
        for bad_url in bad_schemes:
            resp = await client.post("/api/applications/external", json={
                "job_url": bad_url,
                "job_title": "Dev",
                "company": "Corp"
            }, headers=headers)
            assert resp.status_code == 400, f"Expected 400 for {bad_url}"

        # Rejection of loopback and private SSRF targets
        ssrf_targets = [
            "http://localhost:8000/api/secret",
            "http://127.0.0.1:8000/admin",
            "http://169.254.169.254/latest/meta-data/",
            "http://0.0.0.0/test"
        ]
        for ssrf in ssrf_targets:
            resp = await client.post("/api/applications/external", json={
                "job_url": ssrf,
                "job_title": "Dev",
                "company": "Corp"
            }, headers=headers)
            assert resp.status_code == 400, f"Expected 400 for SSRF: {ssrf}"

        # Canonicalization of currentJobId format
        current_job_id_url = "https://www.linkedin.com/jobs/collections/recommended/?currentJobId=555666777"
        resp_canonical = await client.post("/api/applications/external", json={
            "job_url": current_job_id_url,
            "job_title": "Site Reliability Engineer",
            "company": "CloudTech"
        }, headers=headers)
        assert resp_canonical.status_code == 200
        canon_data = resp_canonical.json()
        assert canon_data["application_url"] == "https://www.linkedin.com/jobs/view/555666777/"
        assert canon_data["external_job_id"] == "555666777"


# 5. Payload Validation
@pytest.mark.anyio
async def test_payload_validation():
    db = get_database()
    user_id, token = await create_user(db, "test_linkedin_val@example.com", "Val Tester")
    headers = {"Authorization": f"Bearer {token}"}

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Empty title
        resp1 = await client.post("/api/applications/external", json={
            "job_url": "https://www.linkedin.com/jobs/view/111222333/",
            "job_title": "   ",
            "company": "Stripe"
        }, headers=headers)
        assert resp1.status_code in (400, 422)

        # Empty company
        resp2 = await client.post("/api/applications/external", json={
            "job_url": "https://www.linkedin.com/jobs/view/111222333/",
            "job_title": "Valid Title",
            "company": "  "
        }, headers=headers)
        assert resp2.status_code in (400, 422)

        # Invalid internal status injection (e.g. STARTING or SUBMITTING)
        resp3 = await client.post("/api/applications/external", json={
            "job_url": "https://www.linkedin.com/jobs/view/111222333/",
            "job_title": "Valid Title",
            "company": "Stripe",
            "status": "STARTING"
        }, headers=headers)
        assert resp3.status_code in (400, 422)


# 6. Deduplication and Idempotent Retries
@pytest.mark.anyio
async def test_deduplication_and_idempotent_creation():
    db = get_database()
    user_id, token = await create_user(db, "test_linkedin_dedupe@example.com", "Dedupe Tester")
    headers = {"Authorization": f"Bearer {token}"}

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # First creation
        resp1 = await client.post("/api/applications/external", json={
            "job_url": "https://www.linkedin.com/jobs/view/888111222/?ref=search",
            "job_title": "Full Stack Engineer",
            "company": "Airbnb",
            "status": "APPLIED",
            "notes": "First submission attempt"
        }, headers=headers)
        assert resp1.status_code == 200
        app1 = resp1.json()
        app_id_first = app1["id"]

        # Repeated creation with slightly different query param or updated notes
        resp2 = await client.post("/api/applications/external", json={
            "job_url": "https://www.linkedin.com/jobs/view/888111222/?utm_source=feed",
            "job_title": "Full Stack Engineer",
            "company": "Airbnb",
            "status": "INTERVIEWING",
            "notes": "Recruiter reached out for screen"
        }, headers=headers)
        assert resp2.status_code == 200
        app2 = resp2.json()

        # MUST be idempotent: exact same application ID, not duplicated
        assert app2["id"] == app_id_first
        assert app2["status"] == "INTERVIEWING"
        assert app2["notes"] == "Recruiter reached out for screen"

        # Verify database only has 1 record for this job
        count = await db.applications.count_documents({
            "user_id": user_id,
            "external_job_id": "888111222"
        })
        assert count == 1


# 7. Status Update and Audit History
@pytest.mark.anyio
async def test_status_update_and_audit_history():
    db = get_database()
    user_id, token = await create_user(db, "test_linkedin_audit@example.com", "Audit Tester")
    headers = {"Authorization": f"Bearer {token}"}

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Create application
        create_resp = await client.post("/api/applications/external", json={
            "job_url": "https://www.linkedin.com/jobs/view/777888999/",
            "job_title": "Principal Architect",
            "company": "Netflix",
            "status": "APPLIED",
            "notes": "Applied on portal"
        }, headers=headers)
        assert create_resp.status_code == 200
        app_id = create_resp.json()["id"]

        # Update status to INTERVIEWING
        patch_resp = await client.patch(f"/api/applications/external/{app_id}", json={
            "status": "INTERVIEWING",
            "notes": "Technical screen scheduled"
        }, headers=headers)
        assert patch_resp.status_code == 200
        updated_data = patch_resp.json()
        assert updated_data["status"] == "INTERVIEWING"
        assert updated_data["notes"] == "Technical screen scheduled"

        # Check status history endpoint
        hist_resp = await client.get(f"/api/applications/external/{app_id}/history", headers=headers)
        assert hist_resp.status_code == 200
        history = hist_resp.json()
        assert len(history) >= 2
        assert history[0]["status"] == "APPLIED"
        assert history[0]["status_provenance"] == StatusProvenance.USER_CONFIRMED
        assert history[1]["status"] == "INTERVIEWING"
        assert history[1]["status_provenance"] == StatusProvenance.USER_CONFIRMED
        assert history[1]["notes"] == "Technical screen scheduled"


# 8. Board Filtering in Main Applications List
@pytest.mark.anyio
async def test_board_filtering_in_applications_list():
    db = get_database()
    user_id, token = await create_user(db, "test_linkedin_list@example.com", "List Filter Tester")
    headers = {"Authorization": f"Bearer {token}"}

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Create LinkedIn application
        await client.post("/api/applications/external", json={
            "job_url": "https://www.linkedin.com/jobs/view/121212121/",
            "job_title": "LinkedIn Role",
            "company": "Company A",
            "status": "APPLIED"
        }, headers=headers)

        # Create Dice application doc directly in DB
        await db.applications.insert_one({
            "user_id": user_id,
            "job_id": ObjectId(),
            "company": "Dice Corp",
            "job_title": "Dice Role",
            "board": "dice",
            "source": "dice",
            "status": "READY"
        })

        # Test GET /applications?board=linkedin
        li_resp = await client.get("/api/applications?board=linkedin", headers=headers)
        assert li_resp.status_code == 200
        li_items = li_resp.json()
        assert len(li_items) == 1
        assert li_items[0]["board"] == "linkedin"

        # Test GET /applications?board=dice
        dice_resp = await client.get("/api/applications?board=dice", headers=headers)
        assert dice_resp.status_code == 200
        dice_items = dice_resp.json()
        assert len(dice_items) == 1
        assert dice_items[0]["board"] == "dice"

        # Test GET /applications (all)
        all_resp = await client.get("/api/applications", headers=headers)
        assert all_resp.status_code == 200
        all_items = all_resp.json()
        assert len(all_items) >= 2


# 9. LinkedIn Board Safeguards (Cookie Rejection & Automated Bot Ban)
@pytest.mark.anyio
async def test_linkedin_board_safeguards():
    db = get_database()
    user_id, token = await create_user(db, "test_linkedin_safe@example.com", "Safety Tester")
    headers = {"Authorization": f"Bearer {token}"}

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Rejection of LinkedIn cookie harvesting / import
        cookie_resp = await client.post("/api/boards/linkedin/cookies", json=[
            {"name": "li_at", "value": "secret_cookie_token", "domain": ".linkedin.com"}
        ], headers=headers)
        assert cookie_resp.status_code == 400
        assert "Cookie extraction or import is prohibited for LinkedIn" in cookie_resp.text

        # Connection endpoint returns instructions rather than cookie sync
        conn_resp = await client.post("/api/boards/linkedin/connect", headers=headers)
        assert conn_resp.status_code == 202
        conn_data = conn_resp.json()
        assert conn_data["key"] == "linkedin"
        assert conn_data["status"] == "USER_TRACKING_ACTIVE"

    # Board plugin check: Automated scraping methods must raise BoardError
    li_board = get_board("linkedin")
    assert li_board.key == "linkedin"
    assert li_board.cookie_domain == "linkedin.com"

    with pytest.raises(BoardError) as exc_search:
        await li_board.search(None, None, None, False)
    assert "Automated scraping of LinkedIn is disabled" in str(exc_search.value)

    with pytest.raises(BoardError) as exc_desc:
        await li_board.fetch_description(None, {})
    assert "Automated scraping of LinkedIn job descriptions is disabled" in str(exc_desc.value)

    with pytest.raises(BoardError) as exc_apply:
        await li_board.apply(None, {}, None, {}, {}, "APPLY", None, False)
    assert "Automated bot submission to LinkedIn is prohibited" in str(exc_apply.value)
