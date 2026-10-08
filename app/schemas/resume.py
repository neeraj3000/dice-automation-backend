from pydantic import BaseModel, ConfigDict, Field
from typing import List, Optional, Any, Dict
from datetime import datetime

class ResumeBase(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)
    display_name: Optional[str] = ""
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
    file_type: Optional[str] = "pdf"
    file_size: Optional[int] = 0
    file_path: Optional[str] = ""
    raw_text: Optional[str] = ""
    cloudinary_url: Optional[str] = None
    cloudinary_public_id: Optional[str] = None
    user_id: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

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
