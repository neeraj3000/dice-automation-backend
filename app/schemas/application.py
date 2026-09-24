from pydantic import BaseModel, ConfigDict
from typing import List, Optional, Any, Dict
from datetime import datetime

class ApplicationAnswerSchema(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: Optional[str] = None
    question_text: str
    field_name: Optional[str] = ""
    options: List[str] = []
    answer_text: Optional[str] = ""
    is_answered: bool = False
    created_at: Optional[datetime] = None
    answered_at: Optional[datetime] = None

class ApplicationBase(BaseModel):
    model_config = ConfigDict(extra="allow")
    job_id: str
    resume_id: str
    company: str
    job_title: str
    application_url: str
    status: str = "READY" # DISCOVERED, MATCHED, READY, REVIEW, APPLYING, APPLIED, SKIPPED, FAILED
    mode: str = "PREPARE" # ANALYZE, PREPARE, APPLY
    progress_steps: List[str] = []
    failure_reason: Optional[str] = ""
    applied_at: Optional[datetime] = None

class ApplicationCreate(BaseModel):
    job_id: str
    resume_id: Optional[str] = None
    mode: Optional[str] = "PREPARE"

class ApplicationResponse(ApplicationBase):
    id: str
    resume_name: Optional[str] = ""
    match_percentage: Optional[int] = None
    pending_questions: List[ApplicationAnswerSchema] = []
    created_at: datetime
    updated_at: datetime

class ReviewItem(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: str
    application_id: str
    company: str
    job_title: str
    question_text: str
    field_name: Optional[str] = ""
    options: List[str] = []
    answer_text: Optional[str] = ""
    is_answered: bool = False
    created_at: datetime

class ReviewAnswerSubmit(BaseModel):
    answer_text: str
