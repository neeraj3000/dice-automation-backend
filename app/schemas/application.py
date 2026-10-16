from pydantic import BaseModel, ConfigDict
from typing import List, Optional, Any, Dict
from datetime import datetime

class ApplicationState:
    # Execution states
    QUEUED = "QUEUED"
    STARTING = "STARTING"
    OPENING_JOB = "OPENING_JOB"
    FILLING_APPLICATION = "FILLING_APPLICATION"
    UPLOADING_RESUME = "UPLOADING_RESUME"
    SUBMITTING = "SUBMITTING"
    SUBMITTED = "SUBMITTED"

    # Failure states
    FAILED = "FAILED"
    SESSION_EXPIRED = "SESSION_EXPIRED"
    LOGIN_REQUIRED = "LOGIN_REQUIRED"
    EXTERNAL_PORTAL = "EXTERNAL_PORTAL"
    CAPTCHA_REQUIRED = "CAPTCHA_REQUIRED"
    TIMEOUT = "TIMEOUT"

    # Stopped by the user
    CANCELLED = "CANCELLED"

    # Legacy compatibility states
    DISCOVERED = "DISCOVERED"
    MATCHED = "MATCHED"
    READY = "READY"
    REVIEW = "REVIEW"
    APPLYING = "APPLYING"
    APPLIED = "APPLIED"
    SKIPPED = "SKIPPED"

    # States in which an automation is queued or executing and can be stopped
    ACTIVE = (
        QUEUED, STARTING, OPENING_JOB, FILLING_APPLICATION,
        UPLOADING_RESUME, SUBMITTING, APPLYING, "PREPARING", "PREPARE",
    )


class ApplicationAnswerSchema(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: Optional[str] = None
    question_text: str
    field_name: Optional[str] = ""
    options: Optional[List[str]] = []
    answer_text: Optional[str] = ""
    is_answered: bool = False
    created_at: Optional[datetime] = None
    answered_at: Optional[datetime] = None

class ApplicationBase(BaseModel):
    model_config = ConfigDict(extra="allow")
    job_id: str
    resume_id: Optional[str] = ""
    company: Optional[str] = ""
    job_title: Optional[str] = ""
    application_url: Optional[str] = ""
    job_url: Optional[str] = ""
    user_id: Optional[str] = "default"
    board: Optional[str] = "dice"
    source: Optional[str] = "dice" # dice | linkedin | external
    status: str = "READY" # QUEUED, STARTING, OPENING_JOB, FILLING_APPLICATION, UPLOADING_RESUME, SUBMITTING, SUBMITTED, FAILED, APPLIED, etc.
    status_provenance: Optional[str] = "unknown" # user_confirmed | officially_verified | unknown
    external_job_id: Optional[str] = ""
    external_job_url: Optional[str] = ""
    notes: Optional[str] = ""
    location: Optional[str] = ""
    confirmed_at: Optional[datetime] = None
    status_history: List[Dict[str, Any]] = []
    mode: str = "PREPARE" # ANALYZE, PREPARE, APPLY
    progress_steps: List[str] = []
    failure_reason: Optional[str] = ""
    message: Optional[str] = ""
    external_url: Optional[str] = ""
    applied_at: Optional[datetime] = None

class ApplicationCreate(BaseModel):
    job_id: str
    resume_id: Optional[str] = None
    mode: Optional[str] = "PREPARE"
    user_id: Optional[str] = "default"

class ApplicationResponse(ApplicationBase):
    id: str
    resume_name: Optional[str] = ""
    match_percentage: Optional[int] = None
    pending_questions: List[ApplicationAnswerSchema] = []
    unanswered_questions: List[Dict[str, Any]] = []
    steps: List[Dict[str, Any]] = []
    retry_count: Optional[int] = 0
    max_retries: Optional[int] = 2
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

class AnswersBatchSubmit(BaseModel):
    model_config = ConfigDict(extra="allow")
    answers: List[Dict[str, Any]]


class ReviewItem(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: str
    application_id: str
    company: Optional[str] = ""
    job_title: Optional[str] = ""
    question_text: str
    field_name: Optional[str] = ""
    options: Optional[List[str]] = []
    answer_text: Optional[str] = ""
    is_answered: bool = False
    created_at: Optional[datetime] = None

class ReviewAnswerSubmit(BaseModel):
    answer_text: str

class QueueApplicationRequest(BaseModel):
    model_config = ConfigDict(extra="allow")
    job_id: str
    resume_id: Optional[str] = None
    user_id: Optional[str] = "default"
    mode: Optional[str] = "APPLY"

class QueueBatchRequest(BaseModel):
    model_config = ConfigDict(extra="allow")
    job_ids: List[str]
    user_id: Optional[str] = "default"
    mode: Optional[str] = "APPLY"

class QueueStatusResponse(BaseModel):
    model_config = ConfigDict(extra="allow")
    is_worker_running: bool
    queue_size: int
    active_application_id: Optional[str] = None
    counts_by_status: Dict[str, int] = {}

class ApplicationStatusResponse(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: str
    job_id: str
    resume_id: Optional[str] = None
    company: Optional[str] = ""
    job_title: Optional[str] = ""
    status: str
    mode: Optional[str] = "APPLY"
    progress_steps: List[str] = []
    failure_reason: Optional[str] = ""
    retry_count: int = 0
    max_retries: int = 2
    applied_at: Optional[datetime] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

from app.schemas.linkedin import (
    StatusProvenance,
    ExternalApplicationTrackRequest,
    ExternalApplicationUpdateRequest,
    StatusHistoryEntry,
    PERMITTED_EXTERNAL_STATUSES,
)

