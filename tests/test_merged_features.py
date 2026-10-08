import pytest
from pathlib import Path
from app.core.security import (
    hash_password,
    verify_password,
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_jti,
)
from app.services.skills_vocab import find_skills
from app.services.cloudinary_service import safe_filename, temp_resume_file, is_cloudinary_configured
from app.boards.registry import get_board, list_boards
from app.boards.dice.rules import resolve_answer, normalize


def test_password_hashing():
    pwd = "SecretPassword123!"
    hashed = hash_password(pwd)
    assert hashed != pwd
    assert verify_password(pwd, hashed) is True
    assert verify_password("WrongPassword", hashed) is False
    assert verify_password(pwd, None) is False


def test_jwt_access_and_refresh_tokens():
    user_id = "test_user_456"
    access_token = create_access_token(user_id)
    assert isinstance(access_token, str)

    payload = decode_token(access_token, "access")
    assert payload["sub"] == user_id
    assert payload["type"] == "access"

    refresh_token, jti, exp = create_refresh_token(user_id)
    assert isinstance(refresh_token, str)
    assert len(jti) == 32
    hashed_jti = hash_jti(jti)
    assert len(hashed_jti) == 64

    refresh_payload = decode_token(refresh_token, "refresh")
    assert refresh_payload["sub"] == user_id
    assert refresh_payload["jti"] == jti


def test_skills_vocabulary():
    text = "We are seeking a Senior Data Engineer with strong experience in Python, AWS, Spark, Kubernetes, and Databricks."
    skills = find_skills(text)
    assert "python" in skills
    assert "aws" in skills
    assert "spark" in skills
    assert "kubernetes" in skills
    assert "databricks" in skills

    # Multi-word skills
    multi_text = "Proficient in Spring Boot, React Native, and Google Cloud."
    multi_skills = find_skills(multi_text)
    assert "spring boot" in multi_skills
    assert "react native" in multi_skills
    assert "google cloud" in multi_skills


def test_board_registry():
    boards = list_boards()
    assert len(boards) >= 1
    assert any(b["key"] == "dice" for b in boards)

    dice = get_board("dice")
    assert dice.key == "dice"
    assert dice.name == "Dice"
    assert "dice.com" in dice.base_url


def test_rules_resolve_answer():
    profile = {
        "first_name": "John",
        "last_name": "Doe",
        "email": "john@example.com",
        "phone": "555-123-4567",
        "city": "Dallas",
        "state": "TX",
        "requires_sponsorship": False,
        "authorized_to_work": True,
        "years_experience": 5,
    }
    saved = {"custom_question": "42"}

    assert resolve_answer("What is your first name?", profile, saved) == "John"
    assert resolve_answer("Are you authorized to work in the US?", profile, saved) == "Yes"
    assert resolve_answer("Will you now or in the future require sponsorship?", profile, saved) == "No"
    assert resolve_answer("What is your current location?", profile, saved) == "Dallas, TX"
    assert resolve_answer("custom_question", profile, saved) == "42"


def test_safe_filename():
    assert safe_filename("My Resume (2026) #1.pdf") == "My_Resume_2026_1.pdf"
    assert safe_filename("") == "resume"


@pytest.mark.anyio
async def test_temp_resume_file_local(tmp_path):
    local_file = tmp_path / "test_resume.pdf"
    local_file.write_bytes(b"%PDF-1.4 test")

    async with temp_resume_file(str(local_file), "test_resume.pdf") as path:
        assert path.exists()
        assert path.read_bytes() == b"%PDF-1.4 test"


@pytest.mark.anyio
async def test_boards_api_endpoints():
    from httpx import AsyncClient, ASGITransport
    from app.main import app
    from app.database import connect_db, close_db

    await connect_db()
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            res = await client.get("/api/boards")
            assert res.status_code == 200
            data = res.json()
            assert isinstance(data, list)
            assert any(b["key"] == "dice" for b in data)

            res_v1 = await client.get("/api/v1/boards")
            assert res_v1.status_code == 200

            res_status = await client.get("/api/boards/dice/status")
            assert res_status.status_code == 200
            status_data = res_status.json()
            assert status_data["key"] == "dice"
            assert "status" in status_data
    finally:
        await close_db()


@pytest.mark.anyio
async def test_auth_api_lifecycle():
    import uuid
    from httpx import AsyncClient, ASGITransport
    from app.main import app
    from app.database import connect_db, close_db, get_database

    await connect_db()
    db = get_database()
    test_email = f"test_{uuid.uuid4().hex[:8]}@example.com"
    test_password = "SecurePassword123!"

    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # 1. Register
            reg_resp = await client.post("/api/auth/register", json={
                "name": "Jane Developer",
                "email": test_email,
                "password": test_password,
            })
            assert reg_resp.status_code == 201, reg_resp.text
            reg_data = reg_resp.json()
            assert "access_token" in reg_data
            assert reg_data["user"]["email"] == test_email
            access_token = reg_data["access_token"]

            # 2. Authenticated /me
            me_resp = await client.get("/api/auth/me", headers={"Authorization": f"Bearer {access_token}"})
            assert me_resp.status_code == 200
            assert me_resp.json()["email"] == test_email

            # 3. Login
            login_resp = await client.post("/api/auth/login", json={
                "email": test_email,
                "password": test_password,
            })
            assert login_resp.status_code == 200
            new_token = login_resp.json()["access_token"]
            assert isinstance(new_token, str)

            # 4. Refresh token rotation
            refresh_cookie = login_resp.cookies.get("a2h_refresh")
            assert refresh_cookie is not None

            refresh_resp = await client.post("/api/auth/refresh", cookies={"a2h_refresh": refresh_cookie})
            assert refresh_resp.status_code == 200
            refreshed_data = refresh_resp.json()
            assert "access_token" in refreshed_data

            # 5. Logout
            logout_resp = await client.post("/api/auth/logout", cookies={"a2h_refresh": refresh_cookie})
            assert logout_resp.status_code == 204
    finally:
        # Cleanup test user
        if db is not None:
            await db.users.delete_many({"email": test_email})
        await close_db()

