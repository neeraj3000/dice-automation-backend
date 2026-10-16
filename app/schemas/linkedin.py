from datetime import datetime, timezone
from typing import Optional, List, Dict, Any
from pydantic import BaseModel, ConfigDict, Field, field_validator


class StatusProvenance:
    USER_CONFIRMED = "user_confirmed"
    OFFICIALLY_VERIFIED = "officially_verified"
    UNKNOWN = "unknown"


PERMITTED_EXTERNAL_STATUSES = {
    "APPLIED",
    "SUBMITTED",
    "READY",
    "REVIEW",
    "DISCOVERED",
    "INTERVIEWING",
    "OFFER",
    "REJECTED",
    "CANCELLED",
}


class StatusHistoryEntry(BaseModel):
    model_config = ConfigDict(extra="allow")
    status: str
    status_provenance: str = StatusProvenance.USER_CONFIRMED
    at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    notes: Optional[str] = ""
    actor: str = "user"


class ExternalApplicationTrackRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    job_url: str = Field(..., description="External job posting URL (e.g., LinkedIn job view URL)")
    job_title: str = Field(..., min_length=1, max_length=255, description="Position or job title")
    company: str = Field(..., min_length=1, max_length=255, description="Employer or company name")
    location: Optional[str] = Field("", max_length=255, description="Location if known")
    status: Optional[str] = Field("APPLIED", description="Current status of the application")
    notes: Optional[str] = Field("", max_length=2000, description="User notes or follow-up details")
    resume_id: Optional[str] = Field(None, description="Optional ID of matched resume used")
    confirmed_at: Optional[datetime] = Field(None, description="User confirmation timestamp")
    source: Optional[str] = Field("linkedin", description="Source board identifier")

    @field_validator("job_title", "company")
    @classmethod
    def validate_non_empty_strings(cls, v: str) -> str:
        s = v.strip()
        if not s:
            raise ValueError("Field cannot be empty or only whitespace")
        return s

    @field_validator("status")
    @classmethod
    def validate_permitted_status(cls, v: Optional[str]) -> str:
        if not v:
            return "APPLIED"
        val = v.strip().upper()
        if val not in PERMITTED_EXTERNAL_STATUSES:
            raise ValueError(f"Status '{v}' is not permitted for user-confirmed applications. Allowed: {sorted(list(PERMITTED_EXTERNAL_STATUSES))}")
        return val


class ExternalApplicationUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    status: Optional[str] = None
    notes: Optional[str] = Field(None, max_length=2000)
    job_title: Optional[str] = Field(None, min_length=1, max_length=255)
    company: Optional[str] = Field(None, min_length=1, max_length=255)
    location: Optional[str] = Field(None, max_length=255)

    @field_validator("status")
    @classmethod
    def validate_update_status(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        val = v.strip().upper()
        if val not in PERMITTED_EXTERNAL_STATUSES:
            raise ValueError(f"Status '{v}' is not permitted. Allowed: {sorted(list(PERMITTED_EXTERNAL_STATUSES))}")
        return val

    @field_validator("job_title", "company")
    @classmethod
    def validate_optional_non_empty(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        s = v.strip()
        if not s:
            raise ValueError("Field cannot be empty or only whitespace")
        return s
