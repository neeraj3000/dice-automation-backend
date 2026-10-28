"""
Hybrid Form Engine Coordinator.
Orchestrates Deterministic Matching (Layer A), Dynamic Extraction (Layer B),
LLM Planning (Layer C), Validation (Layer D), and Playwright Execution (Layer E).
Enforces feature flags, call budgets, telemetry, and safe preview modes.
"""

import re
import logging
from typing import List, Dict, Any, Optional, Tuple

from app.config import settings
from app.schemas.user_profile import UserProfileSchema
from app.boards.dice.rules import resolve_answer as deterministic_resolve
from app.services.form_engine.schemas import (
    FormField,
    CandidateFacts,
    ProposedFormAnswer,
    ValidationResult,
    FormFillResult,
    FormEngineMetrics,
)
from app.services.form_engine.extractor import dynamic_field_extractor
from app.services.form_engine.planner import llm_answer_planner
from app.services.form_engine.validator import form_answer_validator
from app.services.form_engine.executor import playwright_form_executor
from app.services.form_engine.cache import form_engine_cache

logger = logging.getLogger(__name__)

# Pricing per million tokens for gpt-4o-mini
COST_PER_M_INPUT = 0.15
COST_PER_M_OUTPUT = 0.60


def sanitize_log_text(text: str) -> str:
    """Redacts potential emails and phone numbers from logs."""
    no_email = re.sub(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+", "[REDACTED_EMAIL]", text)
    no_phone = re.sub(r"\b(?:\+?1[-.]?)?\(?[0-9]{3}\)?[-. ]?[0-9]{3}[-. ]?[0-9]{4}\b", "[REDACTED_PHONE]", no_email)
    return no_phone


class FormEngineService:
    """End-to-end coordinator for the hybrid dynamic application form engine."""

    def __init__(self):
        self.call_budget: int = getattr(settings, "FORM_INTERPRETATION_BUDGET_PER_APP", 3)

    @property
    def is_enabled(self) -> bool:
        """Checks whether the LLM interpretation feature flag is active."""
        return getattr(settings, "FORM_INTERPRETATION_LLM_ENABLED", False)

    def extract_candidate_facts(
        self,
        user_profile: UserProfileSchema,
        resume_skills: Optional[List[str]] = None,
        target_role: Optional[str] = None
    ) -> CandidateFacts:
        """Builds sanitized candidate facts from profile and selected resume."""
        work_auth = (user_profile.work_authorization or "US Citizen").strip()
        is_citizen_or_gc = any(k in work_auth.lower() for k in ["citizen", "green card", "permanent"])
        requires_sponsor = not is_citizen_or_gc

        skills = list(resume_skills or [])
        profile_dict = user_profile.model_dump()
        custom = profile_dict.get("custom_answers") or {}

        return CandidateFacts(
            first_name=user_profile.first_name,
            last_name=user_profile.last_name,
            email=user_profile.email,
            phone=user_profile.phone,
            city=user_profile.city,
            state=user_profile.state,
            zip_code=user_profile.zip_code,
            years_of_experience=user_profile.years_of_experience,
            work_authorization=work_auth,
            requires_sponsorship=requires_sponsor,
            willing_to_relocate=bool(user_profile.willing_to_relocate),
            skills=skills,
            target_role=target_role,
            custom_answers=custom,
        )

    async def process_form_step(
        self,
        page: Any,
        user_profile: UserProfileSchema,
        resume_skills: Optional[List[str]] = None,
        target_role: Optional[str] = None,
        user_id: str = "default",
        preview_mode: bool = False,
        metrics: Optional[FormEngineMetrics] = None,
    ) -> Dict[str, Any]:
        """
        Executes the 5-layer form evaluation and filling process on the current form view.
        Returns a dictionary containing:
        - fields_processed: int
        - filled_count: int
        - unanswered_questions: list of question dicts for user review
        - preview_report: list of field plans (if preview_mode is True)
        - metrics: FormEngineMetrics telemetry
        """
        if metrics is None:
            metrics = FormEngineMetrics()

        candidate_facts = self.extract_candidate_facts(user_profile, resume_skills, target_role)
        profile_dict = user_profile.model_dump()
        saved_answers = candidate_facts.custom_answers

        # 1. Layer B: Extract visible fields on the page
        fields = await dynamic_field_extractor.extract_fields(page)
        metrics.total_fields_extracted += len(fields)

        unanswered_fields: List[FormField] = []
        answers_to_fill: List[Tuple[FormField, str]] = []
        preview_report: List[Dict[str, Any]] = []

        # 2. Layer A: Deterministic Matching first
        for field in fields:
            # Check if field already has a valid value
            if field.current_value and field.control_type not in ("radio", "checkbox"):
                continue

            # Attempt deterministic resolution (regex & profile rules)
            det_ans = deterministic_resolve(field.question_text, profile_dict, saved_answers)
            if det_ans:
                # Validate option matching if select or radio
                if field.control_type in ("select", "radio") and field.options:
                    matched = form_answer_validator.find_matching_option(det_ans, field.options)
                    if matched:
                        answers_to_fill.append((field, matched))
                        metrics.deterministic_filled += 1
                        preview_report.append({
                            "field_id": field.field_id,
                            "question": field.question_text,
                            "proposed_answer": matched,
                            "source": "deterministic_rules",
                            "confidence": 1.0,
                            "needs_user_review": False
                        })
                        continue
                else:
                    answers_to_fill.append((field, det_ans))
                    metrics.deterministic_filled += 1
                    preview_report.append({
                        "field_id": field.field_id,
                        "question": field.question_text,
                        "proposed_answer": det_ans,
                        "source": "deterministic_rules",
                        "confidence": 1.0,
                        "needs_user_review": False
                    })
                    continue

            # Field remains unknown
            unanswered_fields.append(field)

        # 3. Layer C: LLM Fallback (if feature flag is enabled and budget remains)
        if self.is_enabled and unanswered_fields and metrics.llm_calls_made < self.call_budget:
            # Check user-scoped cache first
            cached_plans: List[Tuple[FormField, ProposedFormAnswer]] = []
            uncached_fields: List[FormField] = []

            for field in unanswered_fields:
                cached = form_engine_cache.get(user_id, field.question_text, field.options)
                if cached:
                    cached_plans.append((field, cached))
                    metrics.cache_hits += 1
                else:
                    uncached_fields.append(field)

            # Call LLM planner for uncached fields
            if uncached_fields:
                metrics.llm_calls_made += 1
                plans, in_toks, out_toks, lat = await llm_answer_planner.plan_answers(
                    uncached_fields, candidate_facts
                )
                metrics.prompt_tokens += in_toks
                metrics.completion_tokens += out_toks
                metrics.latency_ms += lat * 1000.0
                metrics.estimated_cost_usd += (in_toks * COST_PER_M_INPUT / 1e6) + (out_toks * COST_PER_M_OUTPUT / 1e6)

                plan_map = {p.field_id: p for p in plans}
                for f in uncached_fields:
                    p = plan_map.get(f.field_id)
                    if p:
                        form_engine_cache.set(user_id, f.question_text, p, f.options)
                        cached_plans.append((f, p))

            # 4. Layer D: Validate all LLM proposed answers
            for field, plan in cached_plans:
                metrics.llm_fields_planned += 1
                val_res = form_answer_validator.validate_answer(field, plan, candidate_facts)
                preview_report.append({
                    "field_id": field.field_id,
                    "question": field.question_text,
                    "proposed_answer": plan.proposed_answer,
                    "validated_answer": val_res.validated_answer,
                    "source": plan.source_fact or "llm_interpretation",
                    "confidence": plan.confidence,
                    "is_valid": val_res.is_valid,
                    "needs_user_review": val_res.needs_review,
                    "explanation": plan.explanation or val_res.rejection_reason
                })

                if val_res.is_valid and val_res.validated_answer:
                    metrics.llm_fields_validated += 1
                    answers_to_fill.append((field, val_res.validated_answer))

        # 5. Layer E: Execution (unless preview_mode is enabled)
        filled_count = 0
        if not preview_mode:
            for field, ans in answers_to_fill:
                fill_res = await playwright_form_executor.fill_field(page, field, ans)
                if fill_res.success:
                    filled_count += 1
                    logger.info(
                        sanitize_log_text(f"Filled form field '{field.question_text}': [SUCCESS]")
                    )
                else:
                    logger.warning(
                        sanitize_log_text(f"Could not fill field '{field.question_text}': {fill_res.error}")
                    )

        # 6. Identify remaining required questions needing human review
        filled_ids = {f.field_id for f, _ in answers_to_fill}
        review_questions = []
        for f in fields:
            if f.required and f.field_id not in filled_ids and not f.current_value:
                metrics.unanswered_for_review += 1
                review_questions.append({
                    "question_text": f.question_text,
                    "field_name": f.field_name or f.element_id,
                    "options": f.options or []
                })

        return {
            "fields_processed": len(fields),
            "filled_count": filled_count if not preview_mode else len(answers_to_fill),
            "unanswered_questions": review_questions,
            "preview_report": preview_report,
            "metrics": metrics
        }


form_engine_service = FormEngineService()
