from pydantic import BaseModel, ConfigDict
from typing import List, Optional
from datetime import datetime

class SearchProfileBase(BaseModel):
    model_config = ConfigDict(extra="allow")
    name: str
    keywords: List[str]
    location: str = "United States"
    radius: int = 30 # 10, 30, 50, 75 miles
    
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
    
    # Legacy compatibility
    is_remote: bool = True
    job_type: str = "Full-time"
    is_active: bool = True

class SearchProfileCreate(SearchProfileBase):
    pass

class SearchProfileUpdate(BaseModel):
    model_config = ConfigDict(extra="allow")
    name: Optional[str] = None
    keywords: Optional[List[str]] = None
    location: Optional[str] = None
    radius: Optional[int] = None
    work_settings: Optional[List[str]] = None
    employment_types: Optional[List[str]] = None
    easy_apply_only: Optional[bool] = None
    posted_within: Optional[str] = None
    experience_levels: Optional[List[str]] = None
    is_remote: Optional[bool] = None
    job_type: Optional[str] = None
    is_active: Optional[bool] = None

class SearchProfileResponse(SearchProfileBase):
    id: str
    created_at: datetime
    updated_at: datetime
