import os
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any
from bson import ObjectId
from pydantic import BaseModel, Field
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse

from app.database import get_database
from app.config import settings
from app.services.settings_service import settings_service
from app.services.resume_service import resume_service
from app.services.llm_service import llm_service
from app.schemas.user_profile import UserProfileSchema

router = APIRouter(prefix="/extension", tags=["Chrome Extension"])

class AnswerQuestionRequest(BaseModel):
    question_text: str
    job_title: Optional[str] = ""
    company: Optional[str] = ""
    options: Optional[List[str]] = Field(default_factory=list)

class RecordApplicationRequest(BaseModel):
    job_id: Optional[str] = None
    job_url: Optional[str] = None
    job_title: str
    company: str
    status: str = "APPLIED"  # APPLIED, REVIEW, FAILED
    mode: str = "APPLY"
    resume_name: Optional[str] = None
    questions_answered: Optional[List[Dict[str, Any]]] = Field(default_factory=list)
    failure_reason: Optional[str] = None
    notes: Optional[str] = None

@router.get("/status")
async def get_extension_status():
    """
    Returns connection status, candidate profile, and resume count for the Chrome extension popup.
    """
    db = get_database()
    profile = await settings_service.get_profile()
    resumes_count = await db.resumes.count_documents({}) if db is not None else 0
    
    # Get active or primary resume
    active_resume = None
    if db is not None:
        doc = await db.resumes.find_one({})
        if doc:
            active_resume = {
                "id": str(doc["_id"]),
                "name": doc.get("display_name", doc.get("file_name", "Resume")),
                "file_name": doc.get("file_name", ""),
                "target_role": doc.get("target_role", "Software Engineer"),
            }

    candidate_name = f"{profile.first_name} {profile.last_name}".strip()
    if not candidate_name:
        candidate_name = profile.email or "Candidate"

    return {
        "status": "online",
        "backend": "Dice Automation API",
        "version": "1.0.0",
        "candidate_name": candidate_name,
        "email": profile.email,
        "phone": profile.phone,
        "resumes_count": resumes_count,
        "active_resume": active_resume,
        "timestamp": datetime.now(timezone.utc).isoformat()
    }

@router.get("/profile")
async def get_extension_profile():
    """
    Returns the comprehensive candidate profile for autofilling forms on Dice.com.
    """
    profile = await settings_service.get_profile()
    return profile.model_dump()

@router.get("/resume/active")
async def get_active_resume():
    """
    Returns metadata and download link for the primary candidate resume.
    """
    db = get_database()
    if db is None:
        raise HTTPException(status_code=503, detail="Database unavailable")
    
    doc = await db.resumes.find_one({})
    if not doc:
        raise HTTPException(status_code=404, detail="No resume uploaded in backend")
    
    return {
        "id": str(doc["_id"]),
        "file_name": doc.get("file_name"),
        "display_name": doc.get("display_name", doc.get("file_name")),
        "target_role": doc.get("target_role", ""),
        "skills": doc.get("skills_top", []),
        "download_url": f"/api/extension/resume/file/{str(doc['_id'])}"
    }

@router.get("/resume/file/{resume_id}")
async def download_resume_file(resume_id: str):
    """
    Streams the raw resume file (PDF or DOCX) so the Chrome Extension can attach it to file inputs.
    """
    if not ObjectId.is_valid(resume_id):
        raise HTTPException(status_code=400, detail="Invalid resume ID")
    
    db = get_database()
    if db is None:
        raise HTTPException(status_code=503, detail="Database unavailable")
    
    doc = await db.resumes.find_one({"_id": ObjectId(resume_id)})
    if not doc:
        raise HTTPException(status_code=404, detail="Resume not found")
    
    file_path = doc.get("file_path")
    if not file_path or not Path(file_path).exists():
        # Fallback to checking RESUMES_DIR
        file_path = settings.RESUMES_DIR / doc.get("file_name", "")
        if not file_path.exists():
            raise HTTPException(status_code=404, detail="Resume file missing from storage")

    filename = doc.get("file_name", "resume.pdf")
    media_type = "application/pdf" if filename.lower().endswith(".pdf") else "application/octet-stream"
    
    return FileResponse(
        path=str(file_path),
        filename=filename,
        media_type=media_type
    )

@router.post("/answer-question")
async def answer_screening_question(req: AnswerQuestionRequest):
    """
    Intelligently answers a screener question asked on a job application.
    Prioritizes profile custom_answers, then candidate profile defaults, then AI generation.
    """
    profile = await settings_service.get_profile()
    q_lower = req.question_text.lower().strip()
    
    # 1. Direct match in custom answers
    custom_ans = profile.custom_answers or {}
    for k, v in custom_ans.items():
        if k.lower() in q_lower or q_lower in k.lower():
            return {"answer": str(v), "source": "custom_answers"}

    # 2. Heuristic matches for common questions
    if any(term in q_lower for term in ["legally authorized", "work authorization", "authorized to work"]):
        return {"answer": "Yes" if "citizen" in profile.work_authorization.lower() or "green" in profile.work_authorization.lower() else profile.work_authorization, "source": "profile_heuristic"}

    if any(term in q_lower for term in ["sponsorship", "require sponsorship", "future sponsorship"]):
        sponsorship = "No" if "citizen" in profile.work_authorization.lower() or "green" in profile.work_authorization.lower() else "Yes"
        return {"answer": sponsorship, "source": "profile_heuristic"}

    if "years of experience" in q_lower or "how many years" in q_lower:
        return {"answer": str(profile.years_of_experience or "5"), "source": "profile_heuristic"}

    if any(term in q_lower for term in ["relocate", "willing to relocate"]):
        return {"answer": "Yes" if profile.willing_to_relocate else "No", "source": "profile_heuristic"}

    if any(term in q_lower for term in ["salary", "compensation", "desired salary", "hourly rate"]):
        return {"answer": "Open for discussion", "source": "profile_heuristic"}

    # 3. AI-assisted answer generation
    try:
        prompt = (
            f"You are helping a candidate answer a job application screening question.\n"
            f"Candidate Name: {profile.first_name} {profile.last_name}\n"
            f"Work Authorization: {profile.work_authorization}\n"
            f"Years of Experience: {profile.years_of_experience}\n"
            f"Job Title: {req.job_title}\n"
            f"Company: {req.company}\n"
            f"Question: {req.question_text}\n"
            f"Options (if multiple choice): {', '.join(req.options) if req.options else 'Free text'}\n\n"
            f"Provide a concise, direct, professional answer. If options are provided, select the best matching option exactly."
        )
        ai_resp = await llm_service.generate_text(prompt)
        cleaned = ai_resp.strip().strip('"').strip("'")
        return {"answer": cleaned, "source": "ai_generated"}
    except Exception:
        # Fallback
        return {"answer": "Yes", "source": "default_fallback"}

@router.post("/record-application")
async def record_extension_application(req: RecordApplicationRequest):
    """
    Records an application submission performed via the Chrome Extension into MongoDB.
    Updates dashboard metrics and application audit trail in real-time.
    """
    db = get_database()
    if db is None:
        raise HTTPException(status_code=503, detail="Database unavailable")

    now = datetime.now(timezone.utc)
    app_doc = {
        "job_title": req.job_title,
        "company": req.company,
        "status": req.status,
        "mode": req.mode,
        "application_url": req.job_url or "",
        "resume_name": req.resume_name or "Primary Resume",
        "questions_answered": req.questions_answered,
        "failure_reason": req.failure_reason,
        "notes": req.notes,
        "applied_via": "chrome_extension",
        "created_at": now,
        "updated_at": now
    }
    
    if req.status == "APPLIED":
        app_doc["applied_at"] = now

    # If job_id exists and valid, link it
    if req.job_id and ObjectId.is_valid(req.job_id):
        app_doc["job_id"] = ObjectId(req.job_id)
        # Also update status in jobs collection
        await db.jobs.update_one(
            {"_id": ObjectId(req.job_id)},
            {"$set": {"status": req.status, "applied_at": now if req.status == "APPLIED" else None}}
        )

    res = await db.applications.insert_one(app_doc)
    return {
        "success": True,
        "application_id": str(res.inserted_id),
        "status": req.status,
        "message": f"Application for '{req.job_title}' at '{req.company}' successfully recorded!"
    }

@router.get("/jobs")
async def get_extension_jobs(limit: int = 15):
    """
    Returns pending/matched jobs for quick application from the extension.
    """
    db = get_database()
    if db is None:
        return []
    
    cursor = db.jobs.find(
        {"status": {"$in": ["MATCHED", "NEW", "READY"]}}
    ).sort("created_at", -1).limit(limit)

    jobs = []
    async for doc in cursor:
        jobs.append({
            "id": str(doc["_id"]),
            "title": doc.get("title", ""),
            "company": doc.get("company", ""),
            "location": doc.get("location", ""),
            "url": doc.get("url") or doc.get("apply_url") or "",
            "match_score": doc.get("match_result", {}).get("score", 0),
            "status": doc.get("status", "MATCHED")
        })
    return jobs
