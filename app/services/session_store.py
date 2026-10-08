import os
import re
import json
import base64
import hashlib
import logging
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Dict, Any, List

from cryptography.fernet import Fernet, InvalidToken
from app.config import settings
from app.database import get_database
from app.models.session import DiceSession

logger = logging.getLogger(__name__)

class InvalidEncryptionKeyError(Exception):
    """Raised when an encryption key is invalid, missing, or decryption fails."""
    pass

def _is_expired(expires_at: Optional[Any]) -> bool:
    """Safely compare naive, timezone-aware, or ISO string expiration timestamps against current UTC time."""
    if not expires_at:
        return False
    if isinstance(expires_at, str):
        try:
            expires_at = datetime.fromisoformat(expires_at)
        except Exception:
            return False
    if not isinstance(expires_at, datetime):
        return False
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return expires_at < datetime.now(timezone.utc)

class SessionEncryption:
    """
    Application-level encryption helper for sensitive candidate credentials.
    Uses authenticated Fernet encryption derived from SESSION_ENCRYPTION_KEY.
    """

    @staticmethod
    def derive_key(key: str) -> bytes:
        if not key or not isinstance(key, str) or len(key.strip()) == 0:
            raise InvalidEncryptionKeyError("Encryption key cannot be empty.")
        clean = key.strip()
        try:
            raw = base64.urlsafe_b64decode(clean.encode("utf-8"))
            if len(raw) == 32:
                return clean.encode("utf-8")
        except Exception:
            pass
        # Derive 32-byte key via SHA-256
        digest = hashlib.sha256(clean.encode("utf-8")).digest()
        return base64.urlsafe_b64encode(digest)

    @classmethod
    def encrypt(cls, data: Dict[str, Any], key: str) -> str:
        """Encrypts dictionary payload into an authenticated ciphertext string."""
        fernet_key = cls.derive_key(key)
        f = Fernet(fernet_key)
        payload = json.dumps(data).encode("utf-8")
        return f.encrypt(payload).decode("utf-8")

    @classmethod
    def decrypt(cls, ciphertext: str, key: str) -> Dict[str, Any]:
        """Decrypts ciphertext string back into dictionary payload."""
        fernet_key = cls.derive_key(key)
        f = Fernet(fernet_key)
        try:
            decrypted = f.decrypt(ciphertext.encode("utf-8"))
            return json.loads(decrypted.decode("utf-8"))
        except InvalidToken:
            raise InvalidEncryptionKeyError("Failed to decrypt session data: invalid encryption key or corrupted ciphertext.")
        except Exception as e:
            raise InvalidEncryptionKeyError(f"Decryption error: {e}")

class SessionStore(ABC):
    """Abstract base class for Dice session persistence."""

    @abstractmethod
    async def save_session(self, user_id: str, session: DiceSession) -> DiceSession:
        pass

    @abstractmethod
    async def get_session(self, user_id: str) -> Optional[DiceSession]:
        pass

    @abstractmethod
    async def update_session(self, user_id: str, update_data: Dict[str, Any]) -> Optional[DiceSession]:
        pass

    @abstractmethod
    async def delete_session(self, user_id: str) -> bool:
        pass

    @abstractmethod
    async def get_session_status(self, user_id: str) -> Dict[str, Any]:
        pass

class LocalSessionStore(SessionStore):
    """
    Local file-based session store for development and testing.
    Preserves data/dice_session.json for backward compatibility.
    """

    def __init__(self, data_dir: Optional[Path] = None):
        self.data_dir = data_dir or settings.DATA_DIR

    def _get_session_path(self, user_id: str) -> Path:
        if user_id == "default":
            return self.data_dir / "dice_session.json"
        clean = re.sub(r'[^a-zA-Z0-9_\-\.]', '_', user_id)
        return self.data_dir / f"dice_session_{clean}.json"

    async def save_session(self, user_id: str, session: DiceSession) -> DiceSession:
        session.user_id = user_id
        session.updated_at = datetime.now(timezone.utc)
        path = self._get_session_path(user_id)
        path.parent.mkdir(parents=True, exist_ok=True)

        payload = session.model_dump(mode="json")
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
            logger.info(f"Saved session locally for user '{user_id}'. Safe status: {session.status}")
        except Exception as e:
            logger.error(f"Error saving local session file for user '{user_id}': {e}")
        return session

    async def get_session(self, user_id: str) -> Optional[DiceSession]:
        path = self._get_session_path(user_id)
        if not path.exists():
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)

            session = DiceSession(**data)
            # Check expiration
            if _is_expired(session.expires_at):
                session.status = "EXPIRED"
                session.is_connected = False
                session.disconnect_reason = "Session expired"

            return session
        except Exception as e:
            logger.warning(f"Error loading local session file for user '{user_id}': {e}")
            return None

    async def update_session(self, user_id: str, update_data: Dict[str, Any]) -> Optional[DiceSession]:
        existing = await self.get_session(user_id)
        if not existing:
            return None
        data = existing.model_dump()
        data.update(update_data)
        data["updated_at"] = datetime.now(timezone.utc)
        updated = DiceSession(**data)
        return await self.save_session(user_id, updated)

    async def delete_session(self, user_id: str) -> bool:
        path = self._get_session_path(user_id)
        deleted = False
        try:
            if path.exists():
                path.unlink(missing_ok=True)
                deleted = True
        except Exception as e:
            logger.debug(f"Error deleting local session file: {e}")

        # Secure cleanup: close isolated browser context
        try:
            from app.browser.playwright_manager import playwright_manager
            await playwright_manager.close_context(user_id)
        except Exception as be:
            logger.debug(f"Error closing browser context for user '{user_id}': {be}")

        logger.info(f"Deleted local session for user '{user_id}'")
        return deleted

    async def get_session_status(self, user_id: str) -> Dict[str, Any]:
        session = await self.get_session(user_id)
        if not session:
            return {
                "user_id": user_id,
                "is_connected": False,
                "status": "DISCONNECTED",
                "username": "",
                "email": "",
                "cookies_count": 0,
                "last_verified_at": None,
            }
        return session.safe_metadata()

class PersistentSessionStore(SessionStore):
    """
    Production persistent session store backed by MongoDB and application-level encryption.
    Stores sessions per-user without cross-contamination.
    """

    def __init__(self, encryption_key: Optional[str] = None):
        self._encryption_key = encryption_key

    @property
    def encryption_key(self) -> str:
        key = self._encryption_key or os.environ.get("SESSION_ENCRYPTION_KEY") or settings.SESSION_ENCRYPTION_KEY
        if not key:
            raise InvalidEncryptionKeyError(
                "SESSION_ENCRYPTION_KEY environment variable is required for persistent session storage."
            )
        return key

    @property
    def col(self):
        db = get_database()
        if db is None:
            raise RuntimeError("MongoDB connection is not established.")
        return db.dice_sessions

    async def _ensure_index(self):
        try:
            await self.col.create_index([("user_id", 1)], unique=True)
        except Exception:
            pass

    async def save_session(self, user_id: str, session: DiceSession) -> DiceSession:
        await self._ensure_index()
        session.user_id = user_id
        session.updated_at = datetime.now(timezone.utc)

        # Encrypt sensitive authentication fields
        sensitive_data = {
            "cookies": session.cookies,
            "local_storage": session.local_storage,
            "identity": session.identity,
            "auth_state": session.auth_state,
        }
        encrypted_blob = SessionEncryption.encrypt(sensitive_data, self.encryption_key)

        # Build document with only safe metadata and encrypted payload
        doc = {
            "user_id": user_id,
            "platform": session.platform,
            "status": session.status,
            "is_connected": session.is_connected,
            "username": session.username,
            "email": session.email,
            "candidate_id": session.candidate_id,
            "cookies_count": session.cookies_count,
            "disconnect_reason": session.disconnect_reason,
            "created_at": session.created_at,
            "updated_at": session.updated_at,
            "last_verified_at": session.last_verified_at,
            "expires_at": session.expires_at,
            "encrypted_data": encrypted_blob,
        }

        await self.col.update_one({"user_id": user_id}, {"$set": doc}, upsert=True)
        logger.info(f"Persisted encrypted session for user '{user_id}'. Safe status: {session.status}")
        return session

    async def get_session(self, user_id: str) -> Optional[DiceSession]:
        await self._ensure_index()
        doc = await self.col.find_one({"user_id": user_id})
        if not doc:
            return None

        # Decrypt sensitive credentials
        encrypted_blob = doc.get("encrypted_data", "")
        sensitive = {}
        if encrypted_blob:
            sensitive = SessionEncryption.decrypt(encrypted_blob, self.encryption_key)

        # Reconstruct session
        data = dict(doc)
        data.pop("_id", None)
        data.pop("encrypted_data", None)
        data["cookies"] = sensitive.get("cookies", [])
        data["local_storage"] = sensitive.get("local_storage", {})
        data["identity"] = sensitive.get("identity", "")
        data["auth_state"] = sensitive.get("auth_state", {})

        session = DiceSession(**data)

        # Check expiration
        if _is_expired(session.expires_at):
            session.status = "EXPIRED"
            session.is_connected = False
            session.disconnect_reason = "Session expired"

        return session

    async def update_session(self, user_id: str, update_data: Dict[str, Any]) -> Optional[DiceSession]:
        session = await self.get_session(user_id)
        if not session:
            return None

        data = session.model_dump()
        data.update(update_data)
        data["updated_at"] = datetime.now(timezone.utc)
        updated = DiceSession(**data)
        return await self.save_session(user_id, updated)

    async def delete_session(self, user_id: str) -> bool:
        res = await self.col.delete_one({"user_id": user_id})
        deleted = res.deleted_count > 0

        # Terminate associated browser context
        try:
            from app.browser.playwright_manager import playwright_manager
            await playwright_manager.close_context(user_id)
        except Exception as e:
            logger.debug(f"Error terminating browser context on session deletion: {e}")

        logger.info(f"Securely deleted persistent session for user '{user_id}'")
        return deleted

    async def get_session_status(self, user_id: str) -> Dict[str, Any]:
        doc = await self.col.find_one({"user_id": user_id})
        if not doc:
            return {
                "user_id": user_id,
                "is_connected": False,
                "status": "DISCONNECTED",
                "username": "",
                "email": "",
                "cookies_count": 0,
                "last_verified_at": None,
            }

        last_ver = doc.get("last_verified_at")
        exp = doc.get("expires_at")
        is_conn = bool(doc.get("is_connected", False))
        status = doc.get("status", "DISCONNECTED")

        if _is_expired(exp):
            is_conn = False
            status = "EXPIRED"

        return {
            "user_id": user_id,
            "platform": doc.get("platform", "dice"),
            "status": status,
            "is_connected": is_conn,
            "username": doc.get("username", ""),
            "email": doc.get("email", ""),
            "candidate_id": doc.get("candidate_id", ""),
            "cookies_count": doc.get("cookies_count", 0),
            "created_at": doc.get("created_at").isoformat() if doc.get("created_at") else None,
            "updated_at": doc.get("updated_at").isoformat() if doc.get("updated_at") else None,
            "last_verified_at": last_ver.isoformat() if last_ver else None,
            "expires_at": exp.isoformat() if exp else None,
            "disconnect_reason": doc.get("disconnect_reason"),
        }

def get_session_store(store_type: Optional[str] = None) -> SessionStore:
    """
    Factory function returning the appropriate SessionStore based on environment:
    - 'local': LocalSessionStore (data/dice_session.json)
    - 'persistent': PersistentSessionStore (MongoDB + encryption)
    - None: Defaults to PersistentSessionStore in server mode, LocalSessionStore in local mode.
    """
    explicit = store_type or os.environ.get("SESSION_STORE", "").lower().strip()
    if explicit == "persistent":
        return PersistentSessionStore()
    if explicit == "local":
        return LocalSessionStore()

    if getattr(settings, "BROWSER_MODE", "local") == "server" or os.environ.get("RENDER"):
        # If encryption key is present, default to PersistentSessionStore
        key = os.environ.get("SESSION_ENCRYPTION_KEY") or settings.SESSION_ENCRYPTION_KEY
        if key:
            return PersistentSessionStore()

    return LocalSessionStore()

session_store = get_session_store()
