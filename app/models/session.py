from datetime import datetime, timezone
from typing import Optional, List, Dict, Any
from pydantic import BaseModel, ConfigDict, Field

class DiceSession(BaseModel):
    """
    Structured model for a Dice candidate session.
    Separates safe queryable metadata from sensitive authentication payloads.
    """
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    user_id: str = "default"
    platform: str = "dice"
    status: str = "DISCONNECTED"  # CONNECTED, DISCONNECTED, EXPIRED
    is_connected: bool = False

    # Safe display metadata
    username: Optional[str] = ""
    email: Optional[str] = ""
    candidate_id: Optional[str] = ""
    cookies_count: int = 0
    disconnect_reason: Optional[str] = None

    # Timestamps
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    last_verified_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None

    # Sensitive authentication information (stored encrypted in persistent storage)
    cookies: List[Dict[str, Any]] = Field(default_factory=list, repr=False)
    local_storage: Dict[str, Any] = Field(default_factory=dict, repr=False)
    identity: Optional[str] = Field(default="", repr=False)
    auth_state: Dict[str, Any] = Field(default_factory=dict, repr=False)

    def safe_metadata(self) -> Dict[str, Any]:
        """Returns non-sensitive metadata safe for logging and API responses."""
        return {
            "user_id": self.user_id,
            "platform": self.platform,
            "status": self.status,
            "is_connected": self.is_connected,
            "username": self.username,
            "email": self.email,
            "candidate_id": self.candidate_id,
            "cookies_count": self.cookies_count,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "last_verified_at": self.last_verified_at.isoformat() if self.last_verified_at else None,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "disconnect_reason": self.disconnect_reason,
        }

    def __repr__(self) -> str:
        """Safe string representation guaranteeing no tokens or cookies are leaked to logs."""
        return (
            f"<DiceSession user_id={self.user_id!r} platform={self.platform!r} "
            f"status={self.status!r} cookies_count={self.cookies_count} "
            f"last_verified_at={self.last_verified_at}>"
        )

    def __str__(self) -> str:
        return self.__repr__()
