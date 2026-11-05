from pydantic import BaseModel, ConfigDict, Field, field_validator
from typing import List, Optional, Union
from datetime import datetime

MAX_PROFILE_RESUMES = 100


def _clean_resume_ids(v):
    if isinstance(v, list):
        # Keep order, drop duplicates and blanks
        return list(dict.fromkeys(str(x).strip() for x in v if str(x).strip()))
    return v


class SearchProfileBase(BaseModel):
    model_config = ConfigDict(extra="allow")
    name: str
    keywords: List[str]
    location: str = "United States"
    radius: int = 30 # 10, 30, 50, 75 miles
    user_id: Optional[str] = None
    
    # Dice "Work settings"
    work_settings: List[str] = ["Remote"] # Remote, Hybrid, On-Site
    
    # Dice "Employment type"
    employment_types: List[str] = ["FULLTIME", "CONTRACTS"] # FULLTIME, CONTRACTS, THIRD_PARTY, PARTTIME
    
    # Dice "Job post features"
    easy_apply_only: bool = True # Dice Easy Apply wizard
    
    # Dice "Posted date"
    posted_within: str = "ONE" # ONE (Today), THREE (3 days), SEVEN (7 days), ANY (No preference)
    
    # Dice "Experience level"
    experience_levels: List[str] = [] # ENTRY_LEVEL, SENIOR, PRINCIPAL
    
    # Resumes this profile may apply with; empty means the user's whole library
    resume_ids: List[str] = Field(default_factory=list, max_length=MAX_PROFILE_RESUMES)

    # Legacy compatibility
    is_remote: bool = True
    job_type: str = "Full-time"
    is_active: bool = True

    @field_validator("resume_ids", mode="before")
    @classmethod
    def clean_resume_ids(cls, v):
        return [] if v is None else _clean_resume_ids(v)

    @field_validator("keywords", mode="before")
    @classmethod
    def parse_keywords(cls, v):
        if isinstance(v, str):
            return [k.strip() for k in v.split(",") if k.strip()]
        if isinstance(v, list):
            return [str(k).strip() for k in v if str(k).strip()]
        return v

    @field_validator("radius", mode="before")
    @classmethod
    def parse_radius(cls, v):
        if isinstance(v, str):
            digits = "".join(c for c in v if c.isdigit())
            return int(digits) if digits else 30
        return v

    @field_validator("posted_within", mode="before")
    @classmethod
    def parse_posted_within(cls, v):
        if isinstance(v, str):
            clean = v.strip().upper()
            if "3" in clean or "THREE" in clean:
                return "THREE"
            if "7" in clean or "SEVEN" in clean:
                return "SEVEN"
            if "1" in clean or "ONE" in clean or "TODAY" in clean:
                return "ONE"
            if "ANY" in clean or "ALL" in clean:
                return "ANY"
        return v

class SearchProfileCreate(SearchProfileBase):
    pass

class SearchProfileUpdate(BaseModel):
    model_config = ConfigDict(extra="allow")
    name: Optional[str] = None
    keywords: Optional[List[str]] = None
    location: Optional[str] = None
    radius: Optional[int] = None
    user_id: Optional[str] = None
    work_settings: Optional[List[str]] = None
    employment_types: Optional[List[str]] = None
    easy_apply_only: Optional[bool] = None
    posted_within: Optional[str] = None
    experience_levels: Optional[List[str]] = None
    resume_ids: Optional[List[str]] = Field(None, max_length=MAX_PROFILE_RESUMES)
    is_remote: Optional[bool] = None
    job_type: Optional[str] = None
    is_active: Optional[bool] = None

    @field_validator("resume_ids", mode="before")
    @classmethod
    def clean_resume_ids(cls, v):
        return _clean_resume_ids(v)

    @field_validator("keywords", mode="before")
    @classmethod
    def parse_keywords(cls, v):
        if isinstance(v, str):
            return [k.strip() for k in v.split(",") if k.strip()]
        if isinstance(v, list):
            return [str(k).strip() for k in v if str(k).strip()]
        return v

    @field_validator("radius", mode="before")
    @classmethod
    def parse_radius(cls, v):
        if isinstance(v, str):
            digits = "".join(c for c in v if c.isdigit())
            return int(digits) if digits else 30
        return v

    @field_validator("posted_within", mode="before")
    @classmethod
    def parse_posted_within(cls, v):
        if isinstance(v, str):
            clean = v.strip().upper()
            if "3" in clean or "THREE" in clean:
                return "THREE"
            if "7" in clean or "SEVEN" in clean:
                return "SEVEN"
            if "1" in clean or "ONE" in clean or "TODAY" in clean:
                return "ONE"
            if "ANY" in clean or "ALL" in clean:
                return "ANY"
        return v

class SearchProfileResponse(SearchProfileBase):
    id: str
    created_at: datetime
    updated_at: datetime
