import re
import logging
from typing import Dict, Any, List
from app.config import settings
from app.schemas.job import JDStructuredData
from app.services.llm_service import llm_service
from app.services.parser_service import COMMON_ROLES, SKILL_KEYWORDS

logger = logging.getLogger(__name__)

class JDService:
    async def analyze_job_description(self, jd_text: str, title_hint: str = "") -> JDStructuredData:
        client = llm_service.get_client()
        if client:
            prompt = f"""
You are an expert technical recruiter analyzing a job description.
Extract structured requirements from this Job Description.
Title hint: {title_hint}

Job Description:
\"\"\"{jd_text[:6000]}\"\"\"

CRITICAL: Extract accurately without inventing qualifications not stated in the JD.
Return JSON matching the schema.
"""
            try:
                response = await client.beta.chat.completions.parse(
                    model=settings.OPENAI_MODEL,
                    messages=[
                        {"role": "system", "content": "You extract structured requirements from technical job descriptions."},
                        {"role": "user", "content": prompt}
                    ],
                    response_format=JDStructuredData,
                    temperature=0.1
                )
                parsed = response.choices[0].message.parsed
                if not parsed.role:
                    parsed.role = title_hint or "Technical Specialist"
                return parsed
            except Exception as e:
                logger.warning(f"LLM JD analysis failed: {e}. Falling back to heuristic extractor.")

        # Fallback Heuristic Analysis
        detected_role = title_hint.strip() if title_hint else ""
        if not detected_role:
            for r in COMMON_ROLES:
                if re.search(r"\b" + re.escape(r) + r"\b", jd_text, re.IGNORECASE):
                    detected_role = r
                    break
        if not detected_role:
            first_line = jd_text.strip().split('\n')[0].strip() if jd_text else ""
            detected_role = first_line[:60] if (first_line and len(first_line) < 60 and not any(c in first_line for c in ["{", "}", "<", ">"])) else "Technical Role"

        detected_skills: List[str] = []
        for skill in SKILL_KEYWORDS:
            pattern = r"(?<!\w)" + re.escape(skill) + r"(?!\w)"
            if re.search(pattern, jd_text, re.IGNORECASE):
                detected_skills.append(skill)

        detected_exp = "3+ years"
        exp_m = re.findall(r"(\d{1,2}\+?)\s*(?:years|yrs)\s*(?:of\s*)?(?:experience)?", jd_text, re.IGNORECASE)
        if exp_m:
            detected_exp = f"{exp_m[0]} years"

        # Split skills into required vs preferred
        req_skills = detected_skills[:6] if len(detected_skills) >= 6 else detected_skills
        pref_skills = detected_skills[6:] if len(detected_skills) > 6 else []

        cloud_keywords = ["AWS", "Azure", "GCP", "Google Cloud", "Snowflake", "Databricks"]
        cloud_found = [c for c in cloud_keywords if c in detected_skills]

        # Extract lines that look like bullet points for responsibilities
        resp_lines = [
            line.strip().lstrip("-•* ")
            for line in jd_text.split("\n")
            if len(line.strip()) > 25 and any(line.strip().startswith(bullet) for bullet in ["-", "•", "*"])
        ][:5]

        return JDStructuredData(
            role=detected_role,
            required_skills=req_skills,
            preferred_skills=pref_skills,
            experience=detected_exp,
            responsibilities=resp_lines or ["Design and implement scalable software systems."],
            cloud=cloud_found,
            tools=[s for s in detected_skills if s in ["Docker", "Kubernetes", "Git", "Terraform", "CI/CD"]],
        )

jd_service = JDService()
