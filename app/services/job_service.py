import re
import hashlib
from datetime import datetime, timezone
from typing import List, Optional, Dict, Any
from bson import ObjectId
from fastapi import HTTPException

from app.database import get_database
from app.schemas.job import JobResponse, JobUpdate, DirectMatchRequest, JobMatchResult
from app.services.jd_service import jd_service
from app.services.matching_service import matching_service
from app.browser.dice_browser import dice_browser

def format_job_doc(doc: Dict[str, Any]) -> JobResponse:
    data = dict(doc)
    data["id"] = str(data.pop("_id"))
    if not data.get("title"):
        data["title"] = "Untitled Position"
    if not data.get("company"):
        data["company"] = "Company Not Specified"
    if data.get("search_profile_id"):
        data["search_profile_id"] = str(data["search_profile_id"])
    return JobResponse(**data)

class JobService:
    @property
    def jobs_col(self):
        return get_database().jobs

    async def get_jobs(
        self,
        search: Optional[str] = None,
        company: Optional[str] = None,
        status: Optional[str] = None,
        location: Optional[str] = None,
        search_profile_id: Optional[str] = None,
        exclude_applied: bool = False
    ) -> List[JobResponse]:
        query = {}
        if search:
            regex = {"$regex": re.escape(search), "$options": "i"}
            query["$or"] = [
                {"title": regex},
                {"company": regex},
                {"description_raw": regex},
                {"location": regex}
            ]
        if company:
            query["company"] = {"$regex": re.escape(company), "$options": "i"}
        if exclude_applied:
            query["status"] = {"$ne": "APPLIED"}
        elif status:
            query["status"] = status
        if location:
            query["location"] = {"$regex": re.escape(location), "$options": "i"}
        if search_profile_id and ObjectId.is_valid(search_profile_id):
            query["search_profile_id"] = ObjectId(search_profile_id)

        cursor = self.jobs_col.find(query).sort("created_at", -1)
        jobs = []
        async for doc in cursor:
            jobs.append(format_job_doc(doc))
        return jobs

    async def clear_all_jobs(self) -> Dict[str, Any]:
        res = await self.jobs_col.delete_many({})
        return {"message": f"Successfully deleted {res.deleted_count} jobs", "count": res.deleted_count}

    async def get_job_by_id(self, job_id: str) -> Optional[JobResponse]:
        if not ObjectId.is_valid(job_id):
            return None
        doc = await self.jobs_col.find_one({"_id": ObjectId(job_id)})
        if not doc:
            return None
        # Fallback check if failure_reason exists on associated application
        if not doc.get("failure_reason"):
            app_doc = await get_database().applications.find_one({"job_id": ObjectId(job_id)})
            if app_doc and app_doc.get("failure_reason"):
                doc["failure_reason"] = app_doc["failure_reason"]
                if doc.get("status") not in ["APPLIED", "FAILED"] and app_doc.get("status") in ["FAILED", "REVIEW"]:
                    doc["status"] = app_doc["status"]
        return format_job_doc(doc)

    async def analyze_job(self, job_id: str) -> JobResponse:
        if not ObjectId.is_valid(job_id):
            raise HTTPException(status_code=400, detail="Invalid job ID")
        doc = await self.jobs_col.find_one({"_id": ObjectId(job_id)})
        if not doc:
            raise HTTPException(status_code=404, detail="Job not found")

        raw_desc = doc.get("description_raw", "")
        # If JD is short or missing, attempt to fetch full JD via Playwright
        if len(raw_desc) < 200 and doc.get("job_url"):
            fetched_desc = await dice_browser.fetch_full_job_description(doc["job_url"])
            if fetched_desc:
                raw_desc = fetched_desc

        jd_structured = await jd_service.analyze_job_description(raw_desc, doc.get("title", ""))
        
        updated = await self.jobs_col.find_one_and_update(
            {"_id": ObjectId(job_id)},
            {
                "$set": {
                    "description_raw": raw_desc,
                    "description_structured": jd_structured.model_dump(),
                    "status": "ANALYZED" if doc.get("status") == "DISCOVERED" else doc.get("status"),
                    "updated_at": datetime.now(timezone.utc)
                }
            },
            return_document=True
        )
        return format_job_doc(updated)

    async def match_job(self, job_id: str) -> JobResponse:
        if not ObjectId.is_valid(job_id):
            raise HTTPException(status_code=400, detail="Invalid job ID")
        doc = await self.jobs_col.find_one({"_id": ObjectId(job_id)})
        if not doc:
            raise HTTPException(status_code=404, detail="Job not found")

        # Ensure structured JD exists
        if not doc.get("description_structured"):
            await self.analyze_job(job_id)
            doc = await self.jobs_col.find_one({"_id": ObjectId(job_id)})

        from app.schemas.job import JDStructuredData
        jd_data = JDStructuredData(**doc["description_structured"])
        match_result = await matching_service.match_job_against_all_resumes(jd_data, job_id=job_id)

        updated = await self.jobs_col.find_one({"_id": ObjectId(job_id)})
        return format_job_doc(updated)

    async def match_direct(self, req: DirectMatchRequest) -> Dict[str, Any]:
        """Directly analyzes and matches a pasted Job Description against the 40 resumes."""
        jd_structured = await jd_service.analyze_job_description(req.description_raw, req.title or "")
        
        # Save temporary or direct job
        now = datetime.now(timezone.utc)
        ext_id = hashlib.sha256(f"{req.company}_{req.title}_{req.description_raw[:100]}".encode()).hexdigest()[:16]
        
        job_doc = {
            "external_job_id": ext_id,
            "title": req.title or jd_structured.role or "Target Role",
            "company": req.company or "Direct Job",
            "location": "Remote",
            "salary": "",
            "employment_type": "Full-time",
            "posted_date": "Now",
            "job_url": "",
            "application_url": "",
            "description_raw": req.description_raw,
            "description_structured": jd_structured.model_dump(),
            "status": "MATCHED",
            "created_at": now,
            "updated_at": now
        }
        res = await self.jobs_col.update_one(
            {"external_job_id": ext_id},
            {"$set": job_doc},
            upsert=True
        )
        saved = await self.jobs_col.find_one({"external_job_id": ext_id})
        job_id = str(saved["_id"])

        match_result = await matching_service.match_job_against_all_resumes(jd_structured, job_id=job_id)
        
        return {
            "job_id": job_id,
            "title": job_doc["title"],
            "company": job_doc["company"],
            "description_structured": jd_structured.model_dump(),
            "match_result": match_result.model_dump()
        }

job_service = JobService()
