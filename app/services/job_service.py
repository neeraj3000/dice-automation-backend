import re
import hashlib
import logging
from datetime import datetime, timezone, timedelta
from typing import List, Optional, Dict, Any
from bson import ObjectId
from fastapi import HTTPException

from app.database import get_database, sanitize_object_ids
from app.schemas.job import JobResponse, JobUpdate, DirectMatchRequest, JobMatchResult
from app.services.jd_service import jd_service
from app.services.matching_service import matching_service
from app.browser.dice_browser import dice_browser

logger = logging.getLogger(__name__)

# Statuses that mean the job has never been successfully applied to and is safe to expire.
# APPLIED, SKIPPED, and QUEUED are intentionally excluded — they represent completed or
# in-progress work that must never be silently deleted.
STALE_ELIGIBLE_STATUSES = ["DISCOVERED", "ANALYZED", "MATCHED", "LOW_MATCH", "FAILED", "REVIEW"]

# Default expiry window for fetched/scraped jobs that have not progressed to APPLIED.
JOB_STALE_HOURS = 72

# Minimum resume match percentage required to attempt automated application.
# Jobs that score below this threshold are marked LOW_MATCH after matching and
# are never picked up by the automation engine.
MIN_MATCH_SCORE = 70

def format_job_doc(doc: Dict[str, Any]) -> JobResponse:
    data = sanitize_object_ids(dict(doc))
    data["id"] = str(data.pop("_id", data.get("id", "")))
    if "user_id" in data and data["user_id"] is not None:
        data["user_id"] = str(data["user_id"])
    if not data.get("title"):
        data["title"] = "Untitled Position"
    if not data.get("company"):
        data["company"] = "Company Not Specified"
    if not data.get("job_url"):
        data["job_url"] = data.get("url") or (f"https://www.dice.com/job-detail/{data.get('external_job_id')}" if data.get("external_job_id") else "")
    if not data.get("url"):
        data["url"] = data.get("job_url") or ""
    if not data.get("application_url"):
        data["application_url"] = data.get("apply_url") or ""
    if not data.get("description_raw"):
        data["description_raw"] = data.get("description") or ""
    if "is_easy_apply" not in data:
        data["is_easy_apply"] = bool(data.get("easy_apply", False))
    if "board" not in data:
        data["board"] = "dice"
    if data.get("created_at"):
        if isinstance(data["created_at"], datetime) and data["created_at"].tzinfo is None:
            data["created_at"] = data["created_at"].replace(tzinfo=timezone.utc)
    else:
        data["created_at"] = datetime.now(timezone.utc)

    if data.get("updated_at"):
        if isinstance(data["updated_at"], datetime) and data["updated_at"].tzinfo is None:
            data["updated_at"] = data["updated_at"].replace(tzinfo=timezone.utc)
    else:
        data["updated_at"] = data.get("created_at") or datetime.now(timezone.utc)
    if data.get("search_profile_id"):
        data["search_profile_id"] = str(data["search_profile_id"])

    # Harmonize match_result and match
    if not data.get("match") and data.get("match_result"):
        mr = data["match_result"]
        data["match"] = {
            "score": mr.get("match_percentage", 0),
            "matched": mr.get("matched_skills", []),
            "missing": mr.get("missing_skills", []),
            "resume_id": mr.get("recommended_resume_id", ""),
            "resume_name": mr.get("recommended_resume_file_name", ""),
            "alternatives": mr.get("alternatives", []),
        }
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
        exclude_applied: bool = False,
        board: Optional[str] = None,
        q: Optional[str] = None,
        easy_apply: Optional[bool] = None,
        page: Optional[int] = None,
        page_size: Optional[int] = None,
        user_id: Optional[str] = None
    ) -> Any:
        query = {}
        search_term = q or search
        if search_term:
            regex = {"$regex": re.escape(search_term.strip()), "$options": "i"}
            query["$or"] = [
                {"title": regex},
                {"company": regex},
                {"description_raw": regex},
                {"location": regex}
            ]
        if company:
            query["company"] = {"$regex": re.escape(company.strip()), "$options": "i"}
        if board:
            query["board"] = board
        if easy_apply is not None:
            query["is_easy_apply"] = easy_apply
        if exclude_applied:
            query["status"] = {"$ne": "APPLIED"}
        elif status:
            if status == "NEW":
                query["status"] = {"$in": ["DISCOVERED", "ANALYZED", "MATCHED", "NEW", "READY"]}
            elif status == "LOW_MATCH":
                query["$or"] = [
                    {"status": "LOW_MATCH"},
                    {"match_result.match_percentage": {"$lt": MIN_MATCH_SCORE, "$ne": None}},
                ]
            elif status == "MATCHED":
                query["status"] = "MATCHED"
                query["match_result.match_percentage"] = {"$gte": MIN_MATCH_SCORE}
            else:
                query["status"] = status
        if location:
            query["location"] = {"$regex": re.escape(location.strip()), "$options": "i"}
        if search_profile_id and ObjectId.is_valid(search_profile_id):
            query["search_profile_id"] = ObjectId(search_profile_id)
        if user_id:
            u_query = [ObjectId(user_id)] if ObjectId.is_valid(user_id) else []
            u_query.append(str(user_id))
            query["user_id"] = {"$in": u_query}
        else:
            if page is not None and page_size is not None:
                return {"items": [], "total": 0, "page": page, "page_size": page_size}
            return []

        # Do not show low-match (< MIN_MATCH_SCORE) jobs in Job Explorer
        if status != "LOW_MATCH":
            if "status" in query and isinstance(query["status"], dict) and "$in" in query["status"]:
                query["status"]["$in"] = [s for s in query["status"]["$in"] if s != "LOW_MATCH"]
            elif "status" not in query:
                query["status"] = {"$ne": "LOW_MATCH"}
            score_gate = {
                "$or": [
                    {"match_result.match_percentage": {"$gte": MIN_MATCH_SCORE}},
                    {"match_result": {"$exists": False}},
                    {"match_result": None},
                ]
            }
            if "$or" in query:
                existing_or = query.pop("$or")
                query["$and"] = [{"$or": existing_or}, score_gate]
            elif "$and" in query:
                query["$and"].append(score_gate)
            else:
                query["$or"] = score_gate["$or"]

        total = await self.jobs_col.count_documents(query)

        # Pagination handling
        cur = self.jobs_col.find(query).sort("created_at", -1)
        if page is not None and page_size is not None:
            cur = cur.skip((page - 1) * page_size).limit(page_size)
            items = [format_job_doc(doc) async for doc in cur]
            return {"items": items, "total": total, "page": page, "page_size": page_size}

        jobs = [format_job_doc(doc) async for doc in cur]
        return jobs

    async def delete_job(self, job_id: str, user_id: Optional[str] = None) -> bool:
        if not ObjectId.is_valid(job_id) or not user_id:
            return False
        u_query = [ObjectId(user_id)] if ObjectId.is_valid(user_id) else []
        u_query.append(str(user_id))
        q = {"_id": ObjectId(job_id), "user_id": {"$in": u_query}}
        res = await self.jobs_col.delete_one(q)
        return res.deleted_count > 0

    async def purge_low_match_jobs(self, user_id: Optional[str] = None) -> int:
        """Permanently deletes unapplied jobs that scored below MIN_MATCH_SCORE."""
        q: Dict[str, Any] = {
            "status": {"$nin": ["APPLIED", "REVIEW", "QUEUED"]},
            "$or": [
                {"status": "LOW_MATCH"},
                {"match_result.match_percentage": {"$lt": MIN_MATCH_SCORE, "$ne": None}}
            ]
        }
        if user_id:
            u_query = [ObjectId(user_id)] if ObjectId.is_valid(user_id) else []
            u_query.append(str(user_id))
            q["user_id"] = {"$in": u_query}
        res = await self.jobs_col.delete_many(q)
        return res.deleted_count

    async def purge_stale_jobs(
        self,
        user_id: Optional[str] = None,
        older_than_hours: int = JOB_STALE_HOURS,
    ) -> Dict[str, Any]:
        """
        Deletes jobs that were scraped/fetched more than `older_than_hours` ago and have
        never been successfully applied to.  Jobs in terminal success states (APPLIED,
        SKIPPED, QUEUED) are always preserved.

        Called automatically before each automation search round and also exposed as an
        explicit API endpoint so the user can trigger it from the UI.
        """
        if not user_id:
            return {"message": "User required", "deleted": 0, "cutoff_hours": older_than_hours}

        cutoff = datetime.now(timezone.utc) - timedelta(hours=older_than_hours)
        u_query = [ObjectId(user_id)] if ObjectId.is_valid(user_id) else []
        u_query.append(str(user_id))

        query = {
            "user_id": {"$in": u_query},
            "status": {"$in": STALE_ELIGIBLE_STATUSES},
            "created_at": {"$lt": cutoff},
        }

        # Count first for the response log — deleteMany has no returned_count in the doc
        count = await self.jobs_col.count_documents(query)
        if count == 0:
            logger.info(f"purge_stale_jobs: no stale jobs found for user {user_id} (cutoff {older_than_hours}h)")
            return {
                "message": f"No stale jobs found (older than {older_than_hours} hours).",
                "deleted": 0,
                "cutoff_hours": older_than_hours,
            }

        res = await self.jobs_col.delete_many(query)
        logger.info(
            f"purge_stale_jobs: deleted {res.deleted_count} stale job(s) "
            f"(>{older_than_hours}h old, user={user_id})"
        )
        return {
            "message": f"Removed {res.deleted_count} stale job(s) older than {older_than_hours} hours.",
            "deleted": res.deleted_count,
            "cutoff_hours": older_than_hours,
        }

    async def clear_all_jobs(self, user_id: Optional[str] = None) -> Dict[str, Any]:
        query = {}
        if user_id:
            u_query = [ObjectId(user_id)] if ObjectId.is_valid(user_id) else []
            u_query.append(str(user_id))
            query["user_id"] = {"$in": u_query}
        else:
            return {"message": "User required", "count": 0}
        res = await self.jobs_col.delete_many(query)
        return {"message": f"Successfully deleted {res.deleted_count} jobs", "count": res.deleted_count}

    async def get_job_by_id(self, job_id: str, user_id: Optional[str] = None) -> Optional[JobResponse]:
        if not ObjectId.is_valid(job_id) or not user_id:
            return None
        u_query = [ObjectId(user_id)] if ObjectId.is_valid(user_id) else []
        u_query.append(str(user_id))
        query = {"_id": ObjectId(job_id), "user_id": {"$in": u_query}}
        doc = await self.jobs_col.find_one(query)
        if not doc:
            return None
        # Fallback check if failure_reason exists on associated application
        if not doc.get("failure_reason"):
            app_doc = await get_database().applications.find_one({"job_id": ObjectId(job_id), "user_id": {"$in": u_query}})
            if app_doc and app_doc.get("failure_reason"):
                doc["failure_reason"] = app_doc["failure_reason"]
                if doc.get("status") not in ["APPLIED", "FAILED"] and app_doc.get("status") in ["FAILED", "REVIEW"]:
                    doc["status"] = app_doc["status"]
        return format_job_doc(doc)

    async def analyze_job(self, job_id: str, user_id: Optional[str] = None) -> JobResponse:
        if not ObjectId.is_valid(job_id) or not user_id:
            raise HTTPException(status_code=400, detail="Invalid job ID or missing user")
        u_query = [ObjectId(user_id)] if ObjectId.is_valid(user_id) else []
        u_query.append(str(user_id))
        doc = await self.jobs_col.find_one({"_id": ObjectId(job_id), "user_id": {"$in": u_query}})
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
            {"_id": ObjectId(job_id), "user_id": {"$in": u_query}},
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

    async def match_job(self, job_id: str, user_id: Optional[str] = None) -> JobResponse:
        if not ObjectId.is_valid(job_id) or not user_id:
            raise HTTPException(status_code=400, detail="Invalid job ID or missing user")
        u_query = [ObjectId(user_id)] if ObjectId.is_valid(user_id) else []
        u_query.append(str(user_id))
        query = {"_id": ObjectId(job_id), "user_id": {"$in": u_query}}
        doc = await self.jobs_col.find_one(query)
        if not doc:
            raise HTTPException(status_code=404, detail="Job not found")

        # Ensure structured JD exists
        if not doc.get("description_structured"):
            await self.analyze_job(job_id)
            doc = await self.jobs_col.find_one({"_id": ObjectId(job_id)})

        from app.schemas.job import JDStructuredData
        jd_data = JDStructuredData(**doc["description_structured"])
        match_result = await matching_service.match_job_against_all_resumes(
            jd_data, job_id=job_id, user_id=user_id or doc.get("user_id")
        )

        updated = await self.jobs_col.find_one({"_id": ObjectId(job_id)})
        return format_job_doc(updated)

    async def match_direct(self, req: DirectMatchRequest, user_id: Optional[str] = None) -> Dict[str, Any]:
        """Directly analyzes and matches a pasted Job Description against the user's resume library."""
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
            "user_id": user_id,
            "created_at": now,
            "updated_at": now
        }
        owner_q = {"external_job_id": ext_id, "user_id": user_id}
        await self.jobs_col.update_one(owner_q, {"$set": job_doc}, upsert=True)
        saved = await self.jobs_col.find_one(owner_q)
        job_id = str(saved["_id"])

        match_result = await matching_service.match_job_against_all_resumes(jd_structured, job_id=job_id, user_id=user_id)
        
        return {
            "job_id": job_id,
            "title": job_doc["title"],
            "company": job_doc["company"],
            "description_structured": jd_structured.model_dump(),
            "match_result": match_result.model_dump()
        }

job_service = JobService()
purge_stale_jobs = job_service.purge_stale_jobs
