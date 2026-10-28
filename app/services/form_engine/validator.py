"""
Layer D: Form Answer Validator & Safety Guardrails.
Deterministically audits every proposed answer against candidate facts, control types,
allowed select/radio options, and strict application safety policies.
"""

import re
import logging
from typing import Dict, Optional, Tuple
from app.config import settings
from app.services.form_engine.schemas import (
    FormField,
    CandidateFacts,
    ProposedFormAnswer,
    ValidationResult,
)

logger = logging.getLogger(__name__)

# Patterns for sensitive/consequential questions that must never be auto-filled without explicit user configuration
CONSEQUENTIAL_PATTERNS = [
    r"\b(disabilit(y|ies)|handicap)\b",
    r"\b(veteran|military\s*service)\b",
    r"\b(gender|sex(ual)?|race|ethnicity|hispanic|latino)\b",
    r"\b(criminal|felon(y)?|misdemeanor|convict(ed|ion)?)\b",
    r"\b(salary|compensation|pay|rate|remuneration)\b",
    r"\b(non-?compete|restrictive\s*covenant)\b",
    r"\b(sign(ature)?|electronic\s*signature|attest|certif(y|ication))\b",
    r"\b(security\s*clearance)\b",
]


class FormAnswerValidator:
    """Validates proposed LLM answers against verified candidate facts and safety policies."""

    def __init__(self):
        self.confidence_threshold: float = getattr(
            settings, "FORM_INTERPRETATION_CONFIDENCE_THRESHOLD", 0.8
        )

    def is_consequential_question(self, question_text: str) -> bool:
        """Checks if question touches legal, demographic, compensation, or sensitive topics."""
        q_lower = question_text.lower()
        return any(re.search(pat, q_lower) for pat in CONSEQUENTIAL_PATTERNS)

    def find_matching_option(self, proposed_answer: str, options: list[str]) -> Optional[str]:
        """
        Finds the matching option from a field's options list.
        Returns the exact option string as rendered on the form, or None.
        """
        if not options or not proposed_answer:
            return None

        clean_proposed = proposed_answer.strip().lower()

        # 1. Exact case-insensitive match
        for opt in options:
            if opt.strip().lower() == clean_proposed:
                return opt.strip()

        # 2. Strict normalized substring or prefix match (e.g. 'Yes' matching 'Yes, I am')
        for opt in options:
            clean_opt = opt.strip().lower()
            if clean_opt.startswith(clean_proposed) or clean_proposed.startswith(clean_opt):
                return opt.strip()

        return None

    def validate_answer(
        self,
        field: FormField,
        plan: ProposedFormAnswer,
        facts: CandidateFacts,
    ) -> ValidationResult:
        """
        Runs comprehensive deterministic checks on a proposed answer.
        Returns a ValidationResult indicating whether the answer can be safely filled.
        """
        # 1. Null / unsupported check
        if not plan.proposed_answer or plan.confidence <= 0.0:
            return ValidationResult(
                field_id=field.field_id,
                is_valid=False,
                rejection_reason="No answer proposed by planner.",
                needs_review=field.required or plan.needs_user_review
            )

        # 2. Confidence threshold
        if plan.confidence < self.confidence_threshold:
            return ValidationResult(
                field_id=field.field_id,
                is_valid=False,
                rejection_reason=f"Confidence {plan.confidence:.2f} below threshold {self.confidence_threshold:.2f}.",
                needs_review=True
            )

        # 3. Consequential / Protected Class Guardrail
        if self.is_consequential_question(field.question_text):
            # Check if user provided an explicit custom answer in profile
            explicit_user_ans = None
            if facts.custom_answers:
                for k, v in facts.custom_answers.items():
                    if k.lower() in field.question_text.lower() or field.question_text.lower() in k.lower():
                        explicit_user_ans = v
                        break

            if not explicit_user_ans:
                return ValidationResult(
                    field_id=field.field_id,
                    is_valid=False,
                    rejection_reason="Consequential question requires explicit user consent / answer bank match.",
                    needs_review=True
                )

        # 4. Control type specific validation
        ans_str = plan.proposed_answer.strip()

        # A. Number controls
        if field.control_type == "number":
            digits_only = re.sub(r"[^\d.]", "", ans_str)
            if not digits_only:
                return ValidationResult(
                    field_id=field.field_id,
                    is_valid=False,
                    rejection_reason=f"Value '{ans_str}' is not numeric for number input.",
                    needs_review=True
                )
            ans_str = digits_only

        # B. Select and Radio controls (must match an existing option)
        if field.control_type in ("select", "radio") and field.options:
            matched_option = self.find_matching_option(ans_str, field.options)
            if not matched_option:
                return ValidationResult(
                    field_id=field.field_id,
                    is_valid=False,
                    rejection_reason=f"Answer '{ans_str}' does not match available options: {field.options}.",
                    needs_review=True
                )
            ans_str = matched_option

        # C. Checkbox controls (Yes/No or True/False)
        if field.control_type == "checkbox":
            if ans_str.lower() not in ("yes", "true", "no", "false", "1", "0"):
                return ValidationResult(
                    field_id=field.field_id,
                    is_valid=False,
                    rejection_reason=f"Answer '{ans_str}' cannot map to checkbox boolean.",
                    needs_review=True
                )

        # 5. Skill & Fact Ground Truth check
        # If the category is skill_experience, check whether the skill claimed is supported by candidate skills
        if plan.question_category == "skill_experience":
            if ans_str.lower() in ("yes", "true"):
                # Ensure at least one skill keyword from the question is present in verified skills
                q_words = set(re.findall(r"\b[A-Za-z0-9+#\.]+\b", field.question_text.lower()))
                cand_skills = {s.lower() for s in facts.skills}
                # Check for overlap
                overlap = q_words & cand_skills
                if not overlap and facts.skills:
                    # Check substring match (e.g. 'react.js' vs 'react')
                    matched = any(s in field.question_text.lower() for s in cand_skills)
                    if not matched:
                        return ValidationResult(
                            field_id=field.field_id,
                            is_valid=False,
                            rejection_reason="Question asks about skills not found in candidate's verified skills list.",
                            needs_review=True
                        )

        return ValidationResult(
            field_id=field.field_id,
            is_valid=True,
            validated_answer=ans_str,
            needs_review=plan.needs_user_review
        )


form_answer_validator = FormAnswerValidator()
