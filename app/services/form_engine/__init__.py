"""
Apply2Hire LLM-Assisted Dynamic Form Engine.
Combines deterministic Playwright automation with structured LLM answer planning
and conservative safety validation.
"""

from app.services.form_engine.schemas import (
    FormField,
    CandidateFacts,
    ProposedFormAnswer,
    FormAnswerPlan,
    ValidationResult,
    FormFillResult,
    FormEngineMetrics,
)
from app.services.form_engine.service import form_engine_service, FormEngineService
from app.services.form_engine.planner import llm_answer_planner, LLMAnswerPlanner
from app.services.form_engine.validator import form_answer_validator, FormAnswerValidator
from app.services.form_engine.extractor import dynamic_field_extractor, DynamicFieldExtractor
from app.services.form_engine.executor import playwright_form_executor, PlaywrightFormExecutor
from app.services.form_engine.cache import form_engine_cache, FormEngineCache

__all__ = [
    "FormField",
    "CandidateFacts",
    "ProposedFormAnswer",
    "FormAnswerPlan",
    "ValidationResult",
    "FormFillResult",
    "FormEngineMetrics",
    "form_engine_service",
    "FormEngineService",
    "llm_answer_planner",
    "LLMAnswerPlanner",
    "form_answer_validator",
    "FormAnswerValidator",
    "dynamic_field_extractor",
    "DynamicFieldExtractor",
    "playwright_form_executor",
    "PlaywrightFormExecutor",
    "form_engine_cache",
    "FormEngineCache",
]
