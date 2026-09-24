import json
import logging
from typing import Optional
from openai import AsyncOpenAI
from app.config import settings
from app.schemas.resume import ResumeParsedMetadata
from app.services.parser_service import extract_heuristic_metadata

logger = logging.getLogger(__name__)

class LLMService:
    def __init__(self):
        self._client: Optional[AsyncOpenAI] = None

    def get_client(self) -> Optional[AsyncOpenAI]:
        if not settings.OPENAI_API_KEY:
            return None
        if not self._client:
            self._client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
        return self._client

    async def parse_resume_metadata(self, text: str, file_name: str) -> ResumeParsedMetadata:
        client = self.get_client()
        heuristic_data = extract_heuristic_metadata(text, file_name)

        if not client:
            return ResumeParsedMetadata(
                display_name=file_name.rsplit(".", 1)[0].replace("_", " ").replace("-", " "),
                target_role=heuristic_data["target_role"],
                skills=heuristic_data["skills"],
                experience_years=heuristic_data["experience_years"],
                summary=heuristic_data["summary"]
            )

        prompt = f"""
You are an expert technical recruiter and resume analyzer.
Analyze the following resume text extracted from file '{file_name}' and produce a concise structured summary.
Extract:
1. target_role: The primary professional title or role (e.g., 'Databricks Consultant', 'AI Engineer', 'Senior Data Engineer').
2. skills: A list of key technical skills, frameworks, tools, and platforms mentioned in the resume.
3. experience_years: Total years of professional experience (e.g., '10+ years', '5 years').
4. summary: A clean 2-sentence summary of the candidate's core expertise and background.

CRITICAL RULE: Never fabricate or hallucinate skills or qualifications not explicitly supported by the resume text.

Resume Text (first 4,000 characters):
\"\"\"{text[:4000]}\"\"\"
"""
        try:
            response = await client.beta.chat.completions.parse(
                model=settings.OPENAI_MODEL,
                messages=[
                    {"role": "system", "content": "You extract structured metadata from resumes. Respond strictly with the requested schema."},
                    {"role": "user", "content": prompt}
                ],
                response_format=ResumeParsedMetadata,
                temperature=0.1
            )
            parsed = response.choices[0].message.parsed
            if not parsed.display_name:
                parsed.display_name = file_name.rsplit(".", 1)[0].replace("_", " ").replace("-", " ")
            return parsed
        except Exception as e:
            logger.warning(f"LLM extraction failed or timed out: {e}. Falling back to heuristic parser.")
            return ResumeParsedMetadata(
                display_name=file_name.rsplit(".", 1)[0].replace("_", " ").replace("-", " "),
                target_role=heuristic_data["target_role"],
                skills=heuristic_data["skills"],
                experience_years=heuristic_data["experience_years"],
                summary=heuristic_data["summary"]
            )

llm_service = LLMService()
