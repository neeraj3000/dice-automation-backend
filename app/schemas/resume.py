from pydantic import BaseModel, ConfigDict, Field
from typing import List, Optional, Any, Dict
from datetime import datetime

class ResumeBase(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)
    display_name: str
    target_role: Optional[str] = ""
    skills: List[str] = []
    experience_years: Optional[str] = ""
    summary: Optional[str] = ""
    custom_fields: Optional[Dict[str, Any]] = {}

class ResumeCreate(ResumeBase):
    pass

class ResumeUpdate(BaseModel):
    model_config = ConfigDict(extra="allow")
    display_name: Optional[str] = None
    target_role: Optional[str] = None
    skills: Optional[List[str]] = None
    experience_years: Optional[str] = None
    summary: Optional[str] = None
    custom_fields: Optional[Dict[str, Any]] = None

class ResumeResponse(ResumeBase):
    id: str
    file_name: str
    file_type: str
    file_size: int
    file_path: str
    raw_text: str
    created_at: datetime
    updated_at: datetime

class ResumeBrief(BaseModel):
    id: str
    display_name: str
    target_role: str
    skills: List[str]
    experience_years: str
    summary: str

class ResumeParsedMetadata(BaseModel):
    model_config = ConfigDict(extra="ignore")
    display_name: Optional[str] = None
    target_role: str
    skills: List[str]
    experience_years: str
    summary: str
