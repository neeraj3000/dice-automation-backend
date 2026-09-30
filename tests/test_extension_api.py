import pytest
from httpx import AsyncClient, ASGITransport
from app.main import app
from app.database import connect_db, close_db

@pytest.fixture(autouse=True)
async def setup_db():
    await connect_db()
    yield
    await close_db()

@pytest.mark.anyio
async def test_extension_status_and_profile():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. Test status
        res = await client.get("/api/extension/status")
        assert res.status_code == 200
        data = res.json()
        assert data["status"] == "online"
        assert "candidate_name" in data
        assert "email" in data
        assert "resumes_count" in data

        # 2. Test profile
        prof_res = await client.get("/api/extension/profile")
        assert prof_res.status_code == 200
        prof_data = prof_res.json()
        assert "first_name" in prof_data
        assert "work_authorization" in prof_data

@pytest.mark.anyio
async def test_extension_answer_question():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Heuristic test for work authorization
        res = await client.post("/api/extension/answer-question", json={
            "question_text": "Are you legally authorized to work in the United States?",
            "job_title": "Software Engineer",
            "company": "Test Company"
        })
        assert res.status_code == 200
        data = res.json()
        assert "answer" in data
        assert data["answer"] in ["Yes", "US Citizen", "No"]

@pytest.mark.anyio
async def test_extension_record_application():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.post("/api/extension/record-application", json={
            "job_title": "Full Stack Developer",
            "company": "Acme Innovations",
            "job_url": "https://www.dice.com/job-detail/test123",
            "status": "APPLIED",
            "notes": "Submitted via Chrome Extension"
        })
        assert res.status_code == 200
        data = res.json()
        assert data["success"] is True
        assert "application_id" in data
