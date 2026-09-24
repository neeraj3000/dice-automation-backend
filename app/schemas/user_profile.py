from pydantic import BaseModel, ConfigDict
from typing import Optional, Dict, Any

class UserProfileSchema(BaseModel):
    model_config = ConfigDict(extra="allow")
    first_name: str = ""
    last_name: str = ""
    email: str = ""
    phone: str = ""
    city: str = ""
    state: str = ""
    zip_code: str = ""
    linkedin_url: str = ""
    github_url: str = ""
    portfolio_url: str = ""
    years_of_experience: str = ""
    work_authorization: str = "US Citizen" # US Citizen, Green Card, H1B, etc.
    willing_to_relocate: bool = False
    custom_answers: Dict[str, str] = {}

class AppSettingsSchema(BaseModel):
    model_config = ConfigDict(extra="allow")
    openai_api_key: Optional[str] = ""
    openai_model: str = "gpt-4o-mini"
    max_jobs_per_search: int = 25
    max_applications_per_run: int = 10
    default_mode: str = "PREPARE" # ANALYZE, PREPARE, APPLY
    headless_browser: bool = False # False lets user see browser & login
    dice_session_connected: bool = False
    dice_username: str = ""
    dice_last_verified: Optional[str] = None
