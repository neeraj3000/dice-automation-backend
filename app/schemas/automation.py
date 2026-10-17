from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator

DEFAULT_APPLY_LIMIT = 10
DEFAULT_DURATION_MINUTES = 30
MIN_DURATION_MINUTES = 5
MAX_DURATION_MINUTES = 240
MAX_APPLY_LIMIT = 100
MAX_PROFILES = 20


class AutomationRunStatus:
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"            # apply target reached, or no jobs left
    TIME_UP = "TIME_UP"                # timer ran out
    STOPPED = "STOPPED"                # stopped by the user
    LOGIN_REQUIRED = "LOGIN_REQUIRED"  # Dice session lost mid-run
    FAILED = "FAILED"                  # unexpected error
    INTERRUPTED = "INTERRUPTED"        # server restarted / worker died mid-run


def _clean_profile_ids(v):
    if isinstance(v, list):
        # Keep order, drop duplicates and blanks
        return list(dict.fromkeys(str(x).strip() for x in v if str(x).strip()))
    return v


class AutomationCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    search_profile_ids: List[str] = Field(..., min_length=1, max_length=MAX_PROFILES)
    apply_limit: int = Field(DEFAULT_APPLY_LIMIT, ge=1, le=MAX_APPLY_LIMIT)
    duration_minutes: int = Field(DEFAULT_DURATION_MINUTES, ge=MIN_DURATION_MINUTES, le=MAX_DURATION_MINUTES)

    @field_validator("name", mode="before")
    @classmethod
    def strip_name(cls, v):
        return v.strip() if isinstance(v, str) else v

    @field_validator("search_profile_ids", mode="before")
    @classmethod
    def clean_profile_ids(cls, v):
        return _clean_profile_ids(v)


class AutomationUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=100)
    search_profile_ids: Optional[List[str]] = Field(None, min_length=1, max_length=MAX_PROFILES)
    apply_limit: Optional[int] = Field(None, ge=1, le=MAX_APPLY_LIMIT)
    duration_minutes: Optional[int] = Field(None, ge=MIN_DURATION_MINUTES, le=MAX_DURATION_MINUTES)

    @field_validator("name", mode="before")
    @classmethod
    def strip_name(cls, v):
        return v.strip() if isinstance(v, str) else v

    @field_validator("search_profile_ids", mode="before")
    @classmethod
    def clean_profile_ids(cls, v):
        return _clean_profile_ids(v)


class AutomationRunCounters(BaseModel):
    scraped: int = 0      # jobs found by this run's searches
    submitted: int = 0    # applications submitted to Dice
    review: int = 0       # skipped and sent to the Review Queue
    failed: int = 0       # skipped because they failed (CAPTCHA, errors, ...)


class AutomationRunCurrent(BaseModel):
    profile_name: Optional[str] = None
    job_id: Optional[str] = None
    application_id: Optional[str] = None
    job_title: Optional[str] = None
    company: Optional[str] = None
    step: Optional[str] = None


class AutomationRunEvent(BaseModel):
    at: Optional[str] = None
    type: str = "info"
    message: str = ""


class AutomationRunResponse(BaseModel):
    id: str
    automation_id: str
    automation_name: str = ""
    status: str
    phase: Optional[str] = None
    is_active: bool = False
    started_at: Optional[str] = None
    ends_at: Optional[str] = None
    finished_at: Optional[str] = None
    end_reason: Optional[str] = None
    apply_limit: int
    duration_minutes: int
    profile_names: List[str] = []
    counters: AutomationRunCounters = AutomationRunCounters()
    current: Optional[AutomationRunCurrent] = None
    events: List[AutomationRunEvent] = []
    # Server-computed so the client countdown never depends on the client clock or timezone
    remaining_seconds: int = 0
    server_now: str
    paused: bool = False            # paused right now (timer frozen)
    pause_requested: bool = False   # user pressed Pause; true from then until Resume
    paused_at: Optional[str] = None
    paused_seconds: int = 0         # total time spent paused (not counted against the timer)


class AutomationRunPage(BaseModel):
    items: List[AutomationRunResponse]
    total: int


class AutomationResponse(BaseModel):
    id: str
    name: str
    search_profile_ids: List[str]
    apply_limit: int
    duration_minutes: int
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    last_run: Optional[Dict[str, Any]] = None
