import json
import asyncio
import logging
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional
from bson import ObjectId

from app.database import get_database
from app.config import settings
from app.schemas.job import JDStructuredData, JobMatchResult, AlternativeMatch
from app.services.llm_service import llm_service

logger = logging.getLogger(__name__)

class MatchingService:
    @property
    def resumes_col(self):
        return get_database().resumes

    @property
    def matches_col(self):
        return get_database().job_matches

    async def match_job_against_all_resumes(
        self, jd: JDStructuredData, job_id: Optional[str] = None
    ) -> JobMatchResult:
        # 1. Fetch all resumes from MongoDB
        cursor = self.resumes_col.find({})
        resumes: List[Dict[str, Any]] = []
        async for doc in cursor:
            file_name = doc.get("file_name", "")
            raw_display = doc.get("display_name", "") or file_name
            # Provide full descriptive identifier so user knows exactly which profile was picked
            if file_name and file_name not in raw_display:
                full_name = f"{raw_display} ({file_name})"
            else:
                full_name = raw_display

            resumes.append({
                "id": str(doc["_id"]),
                "display_name": full_name,
                "file_name": file_name,
                "target_role": doc.get("target_role", ""),
                "skills": doc.get("skills", []),
                "experience_years": doc.get("experience_years", ""),
                "summary": doc.get("summary", ""),
            })

        if not resumes:
            return JobMatchResult(
                recommended_resume_id="",
                recommended_resume_name="No resumes available",
                recommended_resume_file_name="",
                match_percentage=0,
                matched_skills=[],
                partial_matches=[],
                missing_skills=jd.required_skills,
                reason="No resumes have been uploaded to the library yet. Please upload resumes first.",
                alternatives=[]
            )

        client = llm_service.get_client()
        if client:
            resumes_json = json.dumps(resumes, indent=1)
            jd_json = jd.model_dump_json(indent=1)

            prompt = f"""
You are an expert technical recruiter matching a Job Description against a library of candidate resumes.
Your task is to select the single best matching resume from the candidates provided.

TARGET JOB REQUIREMENTS:
{jd_json}

CANDIDATE RESUMES LIBRARY (~40 profiles):
{resumes_json}

CRITICAL RULES:
1. Do NOT fabricate or hallucinate skills that are not explicitly present in the candidate's resume.
2. Select the single best matching resume ID (`recommended_resume_id`).
3. Set `recommended_resume_name` to the exact candidate's `display_name` from the library (e.g. "Veera AzureDevOpsEngineer (Veera-AzureDevOpsEngineer.docx)"). Do NOT just output the personal name "Veera Sekhar".
4. Calculate a realistic match percentage (0 to 100) based on required skills coverage and role alignment.
5. List matched_skills (explicitly present in both JD and selected resume).
6. List partial_matches (related skills or preferred skills present).
7. List missing_skills (required by JD but missing from resume).
8. Provide a concise, professional explanation (`reason`) for why this resume is the strongest fit.
9. Include the top 2 alternative candidates (`alternatives`) with their `resume_id`, `display_name`, and `match_percentage`.
"""
            try:
                response = await asyncio.wait_for(
                    client.beta.chat.completions.parse(
                        model=settings.OPENAI_MODEL,
                        messages=[
                            {"role": "system", "content": "You are a precision technical matching engine. Always return structured JSON adhering to the schema."},
                            {"role": "user", "content": prompt}
                        ],
                        response_format=JobMatchResult,
                        temperature=0.1
                    ),
                    timeout=18.0
                )
                result = response.choices[0].message.parsed
                # Strictly enforce complete resume name and filename from database
                matched_resume = next((r for r in resumes if r["id"] == result.recommended_resume_id), None)
                if matched_resume:
                    result.recommended_resume_name = matched_resume["display_name"]
                    result.recommended_resume_file_name = matched_resume.get("file_name", "")

                for alt in result.alternatives:
                    alt_resume = next((r for r in resumes if r["id"] == alt.resume_id), None)
                    if alt_resume:
                        alt.display_name = alt_resume["display_name"]
                        alt.file_name = alt_resume.get("file_name", "")

                result.created_at = datetime.now(timezone.utc)
                if job_id:
                    await self._save_match_record(job_id, result)
                return result
            except Exception as e:
                logger.warning(f"LLM matching failed: {e}. Falling back to algorithmic ranking.")

        # Fallback Algorithmic Scoring (Guaranteed offline matching)
        return await self._algorithmic_match(jd, resumes, job_id)

    async def _algorithmic_match(
        self, jd: JDStructuredData, resumes: List[Dict[str, Any]], job_id: Optional[str]
    ) -> JobMatchResult:
        scored_candidates = []
        req_set = {s.lower() for s in jd.required_skills}
        pref_set = {s.lower() for s in jd.preferred_skills}

        for r in resumes:
            r_skills_lower = {s.lower() for s in r["skills"]}
            r_skills_original = r["skills"]

            # Matched required skills
            matched_req = [s for s in jd.required_skills if s.lower() in r_skills_lower]
            # Matched preferred skills
            matched_pref = [s for s in jd.preferred_skills if s.lower() in r_skills_lower]
            # Missing required skills
            missing_req = [s for s in jd.required_skills if s.lower() not in r_skills_lower]

            # Role bonus
            role_bonus = 0
            if r["target_role"] and jd.role:
                r_words = set(r["target_role"].lower().split())
                jd_words = set(jd.role.lower().split())
                if r_words & jd_words:
                    role_bonus = 20

            # Calculate score
            total_req = max(1, len(jd.required_skills))
            req_score = (len(matched_req) / total_req) * 60
            pref_score = (len(matched_pref) / max(1, len(jd.preferred_skills))) * 20 if jd.preferred_skills else 10
            total_score = min(98, int(req_score + pref_score + role_bonus))

            scored_candidates.append({
                "resume": r,
                "score": total_score,
                "matched_skills": matched_req,
                "partial_matches": matched_pref,
                "missing_skills": missing_req,
            })

        # Sort descending by score
        scored_candidates.sort(key=lambda x: x["score"], reverse=True)
        top = scored_candidates[0]
        top_resume = top["resume"]

        alternatives = []
        for alt in scored_candidates[1:3]:
            alternatives.append(AlternativeMatch(
                resume_id=alt["resume"]["id"],
                display_name=alt["resume"]["display_name"],
                file_name=alt["resume"].get("file_name", ""),
                match_percentage=alt["score"]
            ))

        reason = (
            f"This resume demonstrates the strongest alignment ({top['score']}%) for the {jd.role} role. "
            f"It covers key required skills including {', '.join(top['matched_skills'][:4]) or 'core technical competencies'}."
        )

        result = JobMatchResult(
            recommended_resume_id=top_resume["id"],
            recommended_resume_name=top_resume["display_name"],
            recommended_resume_file_name=top_resume.get("file_name", ""),
            match_percentage=top["score"],
            matched_skills=top["matched_skills"],
            partial_matches=top["partial_matches"],
            missing_skills=top["missing_skills"],
            reason=reason,
            alternatives=alternatives,
            created_at=datetime.now(timezone.utc)
        )

        if job_id:
            await self._save_match_record(job_id, result)
        return result

    async def _save_match_record(self, job_id: str, result: JobMatchResult):
        match_doc = result.model_dump()
        match_doc["job_id"] = job_id
        await self.matches_col.update_one(
            {"job_id": job_id},
            {"$set": match_doc},
            upsert=True
        )
        # Update job document with match result and status
        if ObjectId.is_valid(job_id):
            await get_database().jobs.update_one(
                {"_id": ObjectId(job_id)},
                {"$set": {
                    "match_result": match_doc,
                    "status": "MATCHED",
                    "updated_at": datetime.now(timezone.utc)
                }}
            )

matching_service = MatchingService()
