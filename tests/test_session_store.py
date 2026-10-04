import os
import sys
import logging
import pytest
from pathlib import Path
from datetime import datetime, timezone, timedelta

# Add backend directory to sys.path
backend_dir = Path(__file__).resolve().parent.parent
if str(backend_dir) not in sys.path:
    sys.path.insert(0, str(backend_dir))

from app.models.session import DiceSession
from app.services.session_store import (
    SessionEncryption,
    InvalidEncryptionKeyError,
    LocalSessionStore,
    PersistentSessionStore,
    get_session_store
)
from app.database import connect_db, close_db, get_database

TEST_ENCRYPTION_KEY = "test-session-secret-key-1234567890-secure"

@pytest.fixture(autouse=True)
async def setup_test_env():
    os.environ["SESSION_ENCRYPTION_KEY"] = TEST_ENCRYPTION_KEY
    await connect_db()
    db = get_database()
    if db is not None:
        try:
            await db.dice_sessions.delete_many({"user_id": {"$regex": "^test_"}})
        except Exception:
            pass
    yield
    if db is not None:
        try:
            await db.dice_sessions.delete_many({"user_id": {"$regex": "^test_"}})
        except Exception:
            pass
    await close_db()

# 1. Encryption & Decryption
@pytest.mark.anyio
async def test_encryption_and_decryption():
    data = {
        "access_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.secret_payload",
        "refresh_token": "rt_987654321_secret",
        "cookies": [{"name": "identity", "value": "id_jwt_val"}],
        "local_storage": {"CognitoUser": "candidate_123"}
    }
    ciphertext = SessionEncryption.encrypt(data, TEST_ENCRYPTION_KEY)
    assert isinstance(ciphertext, str)
    assert len(ciphertext) > 0

    # Ensure sensitive plaintext does NOT appear in raw ciphertext
    assert "secret_payload" not in ciphertext
    assert "rt_987654321_secret" not in ciphertext

    decrypted = SessionEncryption.decrypt(ciphertext, TEST_ENCRYPTION_KEY)
    assert decrypted == data

# 2. Invalid Encryption Key
@pytest.mark.anyio
async def test_invalid_encryption_key():
    data = {"token": "secret_token_123"}
    ciphertext = SessionEncryption.encrypt(data, TEST_ENCRYPTION_KEY)

    # Empty key raises InvalidEncryptionKeyError
    with pytest.raises(InvalidEncryptionKeyError):
        SessionEncryption.encrypt(data, "")

    with pytest.raises(InvalidEncryptionKeyError):
        SessionEncryption.decrypt(ciphertext, "")

    # Wrong key raises InvalidEncryptionKeyError on decryption
    wrong_key = "different-encryption-key-0987654321"
    with pytest.raises(InvalidEncryptionKeyError):
        SessionEncryption.decrypt(ciphertext, wrong_key)

    # Corrupted ciphertext raises InvalidEncryptionKeyError
    with pytest.raises(InvalidEncryptionKeyError):
        SessionEncryption.decrypt("corrupted_invalid_base64_string", TEST_ENCRYPTION_KEY)

# 3. Save & Retrieve Session (Local)
@pytest.mark.anyio
async def test_save_and_retrieve_session_local(tmp_path):
    store = LocalSessionStore(data_dir=tmp_path)
    user_id = "test_user_local_1"

    session = DiceSession(
        user_id=user_id,
        status="CONNECTED",
        is_connected=True,
        username="Local Candidate",
        email="local@candidate.com",
        cookies_count=3,
        cookies=[{"name": "identity", "value": "id_local_123"}],
        local_storage={"token": "ls_local_val"},
        identity="id_local_123"
    )

    saved = await store.save_session(user_id, session)
    assert saved.user_id == user_id

    retrieved = await store.get_session(user_id)
    assert retrieved is not None
    assert retrieved.user_id == user_id
    assert retrieved.is_connected is True
    assert retrieved.username == "Local Candidate"
    assert len(retrieved.cookies) == 1
    assert retrieved.cookies[0]["name"] == "identity"

# 4. Save & Retrieve Session (Persistent / MongoDB + Encryption)
@pytest.mark.anyio
async def test_save_and_retrieve_session_persistent():
    store = PersistentSessionStore(encryption_key=TEST_ENCRYPTION_KEY)
    user_id = "test_user_persist_1"

    raw_jwt = "eyJhbGciOiJIUzI1NiJ9.super_secret_candidate_jwt.signature"
    raw_refresh = "refresh_secret_token_abc123"

    session = DiceSession(
        user_id=user_id,
        status="CONNECTED",
        is_connected=True,
        username="Persistent Candidate",
        email="persist@candidate.com",
        cookies_count=2,
        cookies=[{"name": "identity", "value": raw_jwt, "domain": ".dice.com"}],
        local_storage={"cognito_refresh": raw_refresh},
        identity=raw_jwt
    )

    await store.save_session(user_id, session)

    # DIRECT DATABASE AUDIT: verify plaintext secrets are NOT stored in MongoDB
    db = get_database()
    raw_doc = await db.dice_sessions.find_one({"user_id": user_id})
    assert raw_doc is not None
    assert "encrypted_data" in raw_doc
    assert isinstance(raw_doc["encrypted_data"], str)

    raw_doc_str = str(raw_doc)
    assert raw_jwt not in raw_doc_str, "Raw JWT MUST NOT appear plaintext in MongoDB!"
    assert raw_refresh not in raw_doc_str, "Raw refresh token MUST NOT appear plaintext in MongoDB!"

    # Decrypt and verify retrieval
    retrieved = await store.get_session(user_id)
    assert retrieved is not None
    assert retrieved.user_id == user_id
    assert retrieved.is_connected is True
    assert retrieved.identity == raw_jwt
    assert retrieved.local_storage.get("cognito_refresh") == raw_refresh

# 5. Update Session
@pytest.mark.anyio
async def test_update_session():
    store = PersistentSessionStore(encryption_key=TEST_ENCRYPTION_KEY)
    user_id = "test_user_update_1"

    session = DiceSession(
        user_id=user_id,
        status="CONNECTED",
        is_connected=True,
        username="Initial Name",
        email="initial@candidate.com",
        cookies_count=1
    )
    await store.save_session(user_id, session)

    # Update session status and username
    updated = await store.update_session(user_id, {
        "status": "DISCONNECTED",
        "is_connected": False,
        "username": "Updated Candidate Name",
        "disconnect_reason": "User signed out"
    })
    assert updated is not None
    assert updated.status == "DISCONNECTED"
    assert updated.is_connected is False
    assert updated.username == "Updated Candidate Name"

    # Verify persisted state matches update
    check = await store.get_session(user_id)
    assert check.status == "DISCONNECTED"
    assert check.disconnect_reason == "User signed out"

# 6. Delete Session & Secure Cleanup
@pytest.mark.anyio
async def test_delete_session():
    store = PersistentSessionStore(encryption_key=TEST_ENCRYPTION_KEY)
    user_id = "test_user_delete_1"

    session = DiceSession(user_id=user_id, status="CONNECTED", is_connected=True)
    await store.save_session(user_id, session)

    # Verify exists
    assert await store.get_session(user_id) is not None

    # Delete
    deleted = await store.delete_session(user_id)
    assert deleted is True

    # Verify no longer exists
    assert await store.get_session(user_id) is None

# 7. Missing Session
@pytest.mark.anyio
async def test_missing_session():
    store = PersistentSessionStore(encryption_key=TEST_ENCRYPTION_KEY)
    non_existent = "non_existent_user_9999"

    session = await store.get_session(non_existent)
    assert session is None

    status = await store.get_session_status(non_existent)
    assert status["is_connected"] is False
    assert status["status"] == "DISCONNECTED"
    assert status["username"] == ""
    assert status["cookies_count"] == 0

# 8. Expired Session
@pytest.mark.anyio
async def test_expired_session():
    store = PersistentSessionStore(encryption_key=TEST_ENCRYPTION_KEY)
    user_id = "test_user_expired_1"

    # Set expiration in the past (1 hour ago)
    past_expiration = datetime.now(timezone.utc) - timedelta(hours=1)
    session = DiceSession(
        user_id=user_id,
        status="CONNECTED",
        is_connected=True,
        expires_at=past_expiration
    )
    await store.save_session(user_id, session)

    # Retrieval should detect expiration and update status
    retrieved = await store.get_session(user_id)
    assert retrieved is not None
    assert retrieved.status == "EXPIRED"
    assert retrieved.is_connected is False
    assert retrieved.disconnect_reason == "Session expired"

    status_dict = await store.get_session_status(user_id)
    assert status_dict["status"] == "EXPIRED"
    assert status_dict["is_connected"] is False

# 9. User Isolation
@pytest.mark.anyio
async def test_user_isolation():
    store = PersistentSessionStore(encryption_key=TEST_ENCRYPTION_KEY)
    user_a = "test_user_alpha"
    user_b = "test_user_beta"

    session_a = DiceSession(
        user_id=user_a,
        username="Alpha User",
        cookies=[{"name": "alpha_token", "value": "secret_alpha_value"}]
    )
    session_b = DiceSession(
        user_id=user_b,
        username="Beta User",
        cookies=[{"name": "beta_token", "value": "secret_beta_value"}]
    )

    await store.save_session(user_a, session_a)
    await store.save_session(user_b, session_b)

    # Retrieve and verify strict separation
    res_a = await store.get_session(user_a)
    res_b = await store.get_session(user_b)

    assert res_a.username == "Alpha User"
    assert res_a.cookies[0]["value"] == "secret_alpha_value"
    assert res_b.username == "Beta User"
    assert res_b.cookies[0]["value"] == "secret_beta_value"

    # Deleting user A must NOT affect user B
    await store.delete_session(user_a)
    assert await store.get_session(user_a) is None
    assert await store.get_session(user_b) is not None

# 10. Tokens Not Appearing in Logs
@pytest.mark.anyio
async def test_tokens_not_appearing_in_logs(caplog):
    secret_jwt = "eyJhbGciOiJIUzI1NiJ9.secret_jwt_body.sig"
    secret_refresh = "rt_super_secret_refresh_token_xyz"
    cookie_secret = "cookie_secret_auth_value"

    session = DiceSession(
        user_id="test_user_logs",
        status="CONNECTED",
        is_connected=True,
        username="Log Test Candidate",
        email="test@candidate.com",
        cookies_count=5,
        cookies=[{"name": "identity", "value": cookie_secret}],
        local_storage={"refresh": secret_refresh},
        identity=secret_jwt
    )

    # 1. repr(session) must be safe
    rep = repr(session)
    assert secret_jwt not in rep
    assert secret_refresh not in rep
    assert cookie_secret not in rep
    assert "user_id='test_user_logs'" in rep

    # 2. str(session) must be safe
    s = str(session)
    assert secret_jwt not in s
    assert secret_refresh not in s
    assert cookie_secret not in s

    # 3. safe_metadata() must NOT contain sensitive keys
    meta = session.safe_metadata()
    assert "cookies" not in meta
    assert "local_storage" not in meta
    assert "identity" not in meta
    assert "auth_state" not in meta

    # 4. Logging session object must NOT leak secrets to logger records
    logger = logging.getLogger("test_logger")
    with caplog.at_level(logging.INFO):
        logger.info(f"Processing session: {session}")
        logger.info(f"Session metadata: {session.safe_metadata()}")

    logged_text = caplog.text
    assert secret_jwt not in logged_text
    assert secret_refresh not in logged_text
    assert cookie_secret not in logged_text
    assert "test_user_logs" in logged_text
