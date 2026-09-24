from pydantic import BaseModel, ConfigDict
from typing import List, Optional, Any, Dict
from datetime import datetime

class JDStructuredData(BaseModel):
    model_config = ConfigDict(extra="ignore")
    role: str = ""
    required_skills: List[str] = []
    preferred_skills: List[str] = []
    experience: str = ""
    responsibilities: List[str] = []
    education: Optional[str] = None
    certifications: List[str] = []
    cloud: List[str] = []
    tools: List[str] = []
    domain: Optional[str] = None

class AlternativeMatch(BaseModel):
    model_config = ConfigDict(extra="ignore")
    resume_id: str
    display_name: str = ""
    file_name: Optional[str] = ""
    match_percentage: int

class JobMatchResult(BaseModel):
    model_config = ConfigDict(extra="ignore")
    job_id: Optional[str] = None
    recommended_resume_id: str
    recommended_resume_name: str = ""
    recommended_resume_file_name: Optional[str] = ""
    match_percentage: int
    matched_skills: List[str] = []
    partial_matches: List[str] = []
    missing_skills: List[str] = []
    reason: str = ""
    alternatives: List[AlternativeMatch] = []
    created_at: Optional[datetime] = None

class JobBase(BaseModel):
    model_config = ConfigDict(extra="allow")
    external_job_id: str
    title: str
    company: str
    location: str = ""
    work_setting: str = "Remote" # Remote, Hybrid, On-Site
    salary: Optional[str] = ""
    employment_type: Optional[str] = "" # Full-time, Contract, Third Party, Part-time
    posted_date: Optional[str] = ""
    is_easy_apply: bool = False
    job_url: str
    application_url: Optional[str] = ""
    application_wizard_url: Optional[str] = ""
    description_raw: str = ""
    status: str = "DISCOVERED" # DISCOVERED, ANALYZED, MATCHED, APPLIED, SKIPPED
    search_profile_id: Optional[str] = None
    failure_reason: Optional[str] = None

class JobCreate(JobBase):
    pass

class JobUpdate(BaseModel):
    model_config = ConfigDict(extra="allow")
    title: Optional[str] = None
    company: Optional[str] = None
    location: Optional[str] = None
    salary: Optional[str] = None
    employment_type: Optional[str] = None
    posted_date: Optional[str] = None
    application_url: Optional[str] = None
    description_raw: Optional[str] = None
    status: Optional[str] = None
    failure_reason: Optional[str] = None

class JobResponse(JobBase):
    id: str
    description_structured: Optional[JDStructuredData] = None
    match_result: Optional[JobMatchResult] = None
    created_at: datetime
    updated_at: datetime

class DirectMatchRequest(BaseModel):
    title: Optional[str] = "Pasted Job"
    company: Optional[str] = "Direct Input"
    description_raw: str
