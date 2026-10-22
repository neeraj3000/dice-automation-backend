"""
Data schemas for the LLM-assisted Dynamic Form Interpretation Engine.
Defines extracted form field structures, candidate fact representations,
LLM structured output models, and execution tracking metrics.
"""

from typing import List, Optional, Dict, Any
from pydantic import BaseModel, Field, ConfigDict


class FormField(BaseModel):
    """Represents an extracted interactive form field on an application page."""
    model_config = ConfigDict(extra="ignore")

    field_id: str = Field(..., description="Stable, unique DOM/descriptor identifier")
    question_text: str = Field(..., description="Visible question label, legend, or prompt text")
    control_type: str = Field(
        ...,
        description="HTML/ARIA control type: 'text', 'textarea', 'number', 'select', 'radio', 'checkbox'"
    )
    required: bool = Field(default=False, description="Whether the field is flagged as required")
    options: List[str] = Field(default_factory=list, description="Available options for select or radio groups")
    current_value: Optional[str] = Field(default=None, description="Pre-existing value in the field, if any")
    field_name: Optional[str] = Field(default="", description="HTML name attribute if available")
    element_id: Optional[str] = Field(default="", description="HTML id attribute if available")


class CandidateFacts(BaseModel):
    """
    Sanitized, minimal candidate facts for form interpretation.
    Never transmits raw credentials, session tokens, or unneeded private data.
    """
    model_config = ConfigDict(extra="ignore")

    first_name: str = ""
    last_name: str = ""
    email: str = ""
    phone: str = ""
    city: str = ""
    state: str = ""
    zip_code: str = ""
    years_of_experience: Optional[str] = None
    work_authorization: str = ""
    requires_sponsorship: bool = False
    willing_to_relocate: bool = False
    skills: List[str] = Field(default_factory=list)
    target_role: Optional[str] = None
    custom_answers: Dict[str, str] = Field(default_factory=dict)


class ProposedFormAnswer(BaseModel):
    """Structured plan for an individual unknown form field."""
    model_config = ConfigDict(extra="ignore")

    field_id: str = Field(..., description="Target field identifier matching the input")
    question_category: str = Field(
        ...,
        description="Category: 'skill_experience', 'education', 'work_authorization', 'relocation', 'compensation', 'demographic_or_legal', 'unsupported'"
    )
    proposed_answer: Optional[str] = Field(
        default=None,
        description="Proposed answer value. Must be chosen from options if options were provided. Null if unsupported."
    )
    confidence: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="Confidence score from 0.0 to 1.0"
    )
    source_fact: Optional[str] = Field(
        default=None,
        description="Exact profile field or skill that supports this answer (e.g. 'skills: Python')"
    )
    needs_user_review: bool = Field(
        default=True,
        description="Flag indicating manual human review is required before submission"
    )
    explanation: str = Field(
        default="",
        description="Brief explanation of the reasoning or why the question cannot be answered"
    )


class FormAnswerPlan(BaseModel):
    """Container for batch structured answer plans returned by the LLM."""
    model_config = ConfigDict(extra="ignore")

    answers: List[ProposedFormAnswer] = Field(default_factory=list)


class ValidationResult(BaseModel):
    """Result of deterministic validation on a proposed answer."""
    model_config = ConfigDict(extra="ignore")

    field_id: str
    is_valid: bool
    validated_answer: Optional[str] = None
    rejection_reason: Optional[str] = None
    needs_review: bool = False


class FormFillResult(BaseModel):
    """Result of Playwright browser execution for a single field."""
    model_config = ConfigDict(extra="ignore")

    field_id: str
    question_text: str
    control_type: str
    success: bool
    applied_value: Optional[str] = None
    error: Optional[str] = None


class FormEngineMetrics(BaseModel):
    """Telemetry and cost tracking metrics for form interpretation."""
    model_config = ConfigDict(extra="ignore")

    total_fields_extracted: int = 0
    deterministic_filled: int = 0
    llm_calls_made: int = 0
    llm_fields_planned: int = 0
    llm_fields_validated: int = 0
    llm_fields_filled: int = 0
    cache_hits: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    estimated_cost_usd: float = 0.0
    unanswered_for_review: int = 0
    latency_ms: float = 0.0
