from datetime import datetime, timezone
from typing import List, Optional, Dict, Any
from bson import ObjectId
from fastapi import HTTPException

from app.database import get_database
from app.schemas.application import (
    ApplicationResponse, ApplicationAnswerSchema, ReviewItem, ApplicationCreate
)
from app.services.settings_service import settings_service
from app.services.matching_service import matching_service
from app.services.jd_service import jd_service
from app.browser.application_browser import application_browser

def format_app_doc(doc: Dict[str, Any], questions: List[Dict[str, Any]] = None) -> ApplicationResponse:
    data = dict(doc)
    data["id"] = str(data.pop("_id"))
    data["job_id"] = str(data["job_id"])
    data["resume_id"] = str(data["resume_id"])
    
    q_schemas = []
    if questions:
        for q in questions:
            qd = dict(q)
            qd["id"] = str(qd.pop("_id"))
            qd["application_id"] = str(qd.get("application_id"))
            q_schemas.append(ApplicationAnswerSchema(**qd))
    data["pending_questions"] = q_schemas
    return ApplicationResponse(**data)

class ApplicationService:
    @property
    def apps_col(self):
        return get_database().applications

    @property
    def jobs_col(self):
        return get_database().jobs

    @property
    def resumes_col(self):
        return get_database().resumes

    @property
    def answers_col(self):
        return get_database().application_answers

    async def prepare_application(self, req: ApplicationCreate) -> ApplicationResponse:
        if not ObjectId.is_valid(req.job_id):
            raise HTTPException(status_code=400, detail="Invalid job ID")

        job = await self.jobs_col.find_one({"_id": ObjectId(req.job_id)})
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")

        # Select resume
        resume_id = req.resume_id
        if not resume_id:
            # Check if job already has recommended resume
            if job.get("match_result") and job["match_result"].get("recommended_resume_id"):
                resume_id = job["match_result"]["recommended_resume_id"]

        resume = None
        if resume_id and ObjectId.is_valid(resume_id):
            resume = await self.resumes_col.find_one({"_id": ObjectId(resume_id)})

        # If no resume specified or the referenced resume was deleted/replaced, re-match against current active library
        if not resume:
            # Run matching now against active resumes
            jd_structured = job.get("description_structured")
            if not jd_structured:
                jd_data = await jd_service.analyze_job_description(job.get("description_raw", ""), job.get("title", ""))
                jd_structured = jd_data.model_dump()
                await self.jobs_col.update_one(
                    {"_id": ObjectId(req.job_id)},
                    {"$set": {"description_structured": jd_structured}}
                )
            match_res = await matching_service.match_job_against_all_resumes(
                jd_data if 'jd_data' in locals() else await jd_service.analyze_job_description(job.get("description_raw", "")),
                job_id=str(job["_id"])
            )
            resume_id = match_res.recommended_resume_id
            if resume_id and ObjectId.is_valid(resume_id):
                resume = await self.resumes_col.find_one({"_id": ObjectId(resume_id)})

        # If matching still couldn't resolve, fallback to any valid resume in the library
        if not resume:
            resume = await self.resumes_col.find_one({})
            if not resume:
                raise HTTPException(status_code=400, detail="No matching resume found in library. Please upload a resume first.")
            resume_id = str(resume["_id"])

        now = datetime.now(timezone.utc)
        # Create or update Application doc
        existing_app = await self.apps_col.find_one({"job_id": ObjectId(req.job_id)})
        app_data = {
            "job_id": ObjectId(req.job_id),
            "resume_id": ObjectId(resume_id),
            "company": job.get("company", "Company"),
            "job_title": job.get("title", "Job Title"),
            "application_url": job.get("application_url") or job.get("job_url", ""),
            "resume_name": resume.get("display_name") if (resume.get("file_name") and resume.get("file_name") in str(resume.get("display_name"))) else f"{resume.get('display_name', '')} ({resume.get('file_name', '')})".strip(),
            "status": "APPLYING",
            "mode": req.mode or "PREPARE",
            "progress_steps": ["Application preparation started", f"Selected resume: {resume.get('display_name')}"],
            "failure_reason": "",
            "updated_at": now
        }

        if existing_app:
            await self.apps_col.update_one({"_id": existing_app["_id"]}, {"$set": app_data})
            app_id = existing_app["_id"]
        else:
            app_data["created_at"] = now
            res = await self.apps_col.insert_one(app_data)
            app_id = res.inserted_id

        # Run Browser automation
        user_profile = await settings_service.get_profile()
        browser_res = await application_browser.prepare_application(
            application_url=app_data["application_url"],
            resume_file_path=resume.get("file_path", resume.get("file_name")),
            user_profile=user_profile,
            mode=app_data["mode"]
        )

        final_steps = app_data["progress_steps"] + browser_res["progress_steps"]
        update_fields = {
            "status": browser_res["status"],
            "progress_steps": final_steps,
            "failure_reason": browser_res.get("failure_reason") or "",
            "updated_at": datetime.now(timezone.utc)
        }

        if browser_res["status"] == "APPLIED":
            now_ts = datetime.now(timezone.utc)
            update_fields["applied_at"] = now_ts
            await self.jobs_col.update_one(
                {"_id": job["_id"]},
                {"$set": {"status": "APPLIED", "applied_at": now_ts, "failure_reason": None}}
            )
        elif browser_res["status"] == "FAILED":
            await self.jobs_col.update_one(
                {"_id": job["_id"]},
                {"$set": {
                    "status": "FAILED",
                    "failure_reason": browser_res.get("failure_reason") or "Application submission failed.",
                    "updated_at": datetime.now(timezone.utc)
                }}
            )
        elif browser_res["status"] == "REVIEW":
            await self.jobs_col.update_one(
                {"_id": job["_id"]},
                {"$set": {
                    "status": "REVIEW",
                    "updated_at": datetime.now(timezone.utc)
                }}
            )

        # Store any questions into application_answers collection
        pending_qs = []
        if browser_res.get("unanswered_questions"):
            for q in browser_res["unanswered_questions"]:
                q_doc = {
                    "application_id": app_id,
                    "question_text": q["question_text"],
                    "field_name": q.get("field_name", ""),
                    "options": q.get("options", []),
                    "answer_text": "",
                    "is_answered": False,
                    "created_at": datetime.now(timezone.utc)
                }
                insert_q = await self.answers_col.insert_one(q_doc)
                q_doc["_id"] = insert_q.inserted_id
                pending_qs.append(q_doc)

        await self.apps_col.update_one({"_id": app_id}, {"$set": update_fields})
        saved_app = await self.apps_col.find_one({"_id": app_id})
        return format_app_doc(saved_app, pending_qs)

    async def get_applications(self) -> List[ApplicationResponse]:
        cursor = self.apps_col.find({}).sort("created_at", -1)
        apps = []
        async for doc in cursor:
            # fetch pending questions
            q_cursor = self.answers_col.find({"application_id": doc["_id"], "is_answered": False})
            qs = await q_cursor.to_list(10)
            apps.append(format_app_doc(doc, qs))
        return apps

    async def get_application_by_id(self, app_id: str) -> Optional[ApplicationResponse]:
        if not ObjectId.is_valid(app_id):
            return None
        doc = await self.apps_col.find_one({"_id": ObjectId(app_id)})
        if not doc:
            return None
        q_cursor = self.answers_col.find({"application_id": ObjectId(app_id)})
        qs = await q_cursor.to_list(20)
        return format_app_doc(doc, qs)

    async def submit_application(self, app_id: str) -> ApplicationResponse:
        if not ObjectId.is_valid(app_id):
            raise HTTPException(status_code=400, detail="Invalid application ID")
        app_doc = await self.apps_col.find_one({"_id": ObjectId(app_id)})
        if not app_doc:
            raise HTTPException(status_code=404, detail="Application not found")

        # Submission protection: allow submitting if READY, REVIEW, PREPARE, FAILED, or re-submitting APPLIED
        if app_doc["status"] not in ["READY", "REVIEW", "PREPARE", "FAILED", "APPLIED", "APPLYING"]:
            raise HTTPException(status_code=400, detail=f"Cannot submit application in '{app_doc['status']}' status")

        # Fetch associated resume and user profile
        resume_id = app_doc.get("resume_id")
        resume = await self.resumes_col.find_one({"_id": resume_id}) if resume_id else None
        if not resume:
            # Fallback to any available resume in library
            resume = await self.resumes_col.find_one({})
            if not resume:
                raise HTTPException(status_code=400, detail="Associated resume was deleted and no resumes exist in library. Please upload a resume.")
            await self.apps_col.update_one(
                {"_id": ObjectId(app_id)},
                {"$set": {"resume_id": resume["_id"], "resume_name": resume.get("display_name", "Resume")}}
            )

        user_profile = await settings_service.get_profile()
        now = datetime.now(timezone.utc)
        steps = app_doc.get("progress_steps", [])
        steps.append(f"Submitting application to Dice at {now.strftime('%H:%M:%S')}...")

        # Update to APPLYING while in-flight
        await self.apps_col.update_one(
            {"_id": ObjectId(app_id)},
            {"$set": {"status": "APPLYING", "progress_steps": steps, "updated_at": now}}
        )

        # Run real browser submission
        browser_res = await application_browser.submit_application_to_dice(
            application_url=app_doc["application_url"],
            resume_file_path=resume.get("file_path", resume.get("file_name")),
            user_profile=user_profile
        )

        final_steps = steps + browser_res.get("progress_steps", [])
        final_status = browser_res["status"]
        failure_reason = browser_res.get("failure_reason") or ""

        update_fields = {
            "status": final_status,
            "progress_steps": final_steps,
            "failure_reason": failure_reason,
            "updated_at": datetime.now(timezone.utc)
        }

        if final_status == "APPLIED":
            update_fields["applied_at"] = datetime.now(timezone.utc)
            # Update job status as well
            if ObjectId.is_valid(app_doc.get("job_id")):
                await self.jobs_col.update_one(
                    {"_id": ObjectId(app_doc["job_id"])},
                    {"$set": {"status": "APPLIED", "applied_at": datetime.now(timezone.utc), "failure_reason": None, "updated_at": datetime.now(timezone.utc)}}
                )
        elif final_status == "FAILED" and ObjectId.is_valid(app_doc.get("job_id")):
            await self.jobs_col.update_one(
                {"_id": ObjectId(app_doc["job_id"])},
                {"$set": {"status": "FAILED", "failure_reason": failure_reason, "updated_at": datetime.now(timezone.utc)}}
            )
        elif final_status == "REVIEW" and ObjectId.is_valid(app_doc.get("job_id")):
            await self.jobs_col.update_one(
                {"_id": ObjectId(app_doc["job_id"])},
                {"$set": {"status": "REVIEW", "updated_at": datetime.now(timezone.utc)}}
            )

        # Record any newly discovered questions if stopped in REVIEW
        pending_qs = []
        if browser_res.get("unanswered_questions"):
            for q in browser_res["unanswered_questions"]:
                q_doc = {
                    "application_id": ObjectId(app_id),
                    "question_text": q["question_text"],
                    "field_name": q.get("field_name", ""),
                    "options": q.get("options", []),
                    "answer_text": "",
                    "is_answered": False,
                    "created_at": datetime.now(timezone.utc)
                }
                insert_q = await self.answers_col.insert_one(q_doc)
                q_doc["_id"] = insert_q.inserted_id
                pending_qs.append(q_doc)

        updated = await self.apps_col.find_one_and_update(
            {"_id": ObjectId(app_id)},
            {"$set": update_fields},
            return_document=True
        )
        return format_app_doc(updated, pending_qs)

    async def get_review_queue(self) -> List[ReviewItem]:
        cursor = self.answers_col.find({"is_answered": False}).sort("created_at", -1)
        items = []
        async for doc in cursor:
            app_id = doc.get("application_id")
            app = await self.apps_col.find_one({"_id": app_id}) if app_id else None
            items.append(ReviewItem(
                id=str(doc["_id"]),
                application_id=str(app_id) if app_id else "",
                company=app.get("company", "Unknown") if app else "Unknown",
                job_title=app.get("job_title", "Unknown") if app else "Unknown",
                question_text=doc.get("question_text", ""),
                field_name=doc.get("field_name", ""),
                options=doc.get("options", []),
                answer_text=doc.get("answer_text", ""),
                is_answered=doc.get("is_answered", False),
                created_at=doc.get("created_at", datetime.now(timezone.utc))
            ))
        return items

    async def answer_review_question(self, question_id: str, answer_text: str) -> bool:
        if not ObjectId.is_valid(question_id):
            return False
        q_doc = await self.answers_col.find_one_and_update(
            {"_id": ObjectId(question_id)},
            {
                "$set": {
                    "answer_text": answer_text,
                    "is_answered": True,
                    "answered_at": datetime.now(timezone.utc)
                }
            },
            return_document=True
        )
        if not q_doc:
            return False

        # If no more pending questions for this application, move status to READY
        app_id = q_doc.get("application_id")
        if app_id:
            pending_count = await self.answers_col.count_documents({
                "application_id": app_id,
                "is_answered": False
            })
            if pending_count == 0:
                await self.apps_col.update_one(
                    {"_id": app_id},
                    {
                        "$set": {
                            "status": "READY",
                            "updated_at": datetime.now(timezone.utc)
                        },
                        "$push": {
                            "progress_steps": f"Question answered: '{q_doc['question_text'][:40]}...'. Application ready."
                        }
                    }
                )
        return True

application_service = ApplicationService()
