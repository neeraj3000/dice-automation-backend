from datetime import datetime, timezone
from typing import List, Optional, Dict, Any
from bson import ObjectId
from fastapi import HTTPException

from app.database import get_database
from app.schemas.search_profile import (
    SearchProfileCreate, SearchProfileUpdate, SearchProfileResponse
)
from app.browser.dice_browser import dice_browser
from app.services.settings_service import settings_service

def format_profile_doc(doc: Dict[str, Any]) -> SearchProfileResponse:
    data = dict(doc)
    data["id"] = str(data.pop("_id"))
    return SearchProfileResponse(**data)

class SearchService:
    @property
    def profiles_col(self):
        return get_database().search_profiles

    @property
    def jobs_col(self):
        return get_database().jobs

    async def create_profile(self, profile: SearchProfileCreate) -> SearchProfileResponse:
        doc = profile.model_dump()
        now = datetime.now(timezone.utc)
        doc["created_at"] = now
        doc["updated_at"] = now
        res = await self.profiles_col.insert_one(doc)
        doc["_id"] = res.inserted_id
        return format_profile_doc(doc)

    async def get_profiles(self) -> List[SearchProfileResponse]:
        cursor = self.profiles_col.find({}).sort("created_at", -1)
        profiles = []
        async for doc in cursor:
            profiles.append(format_profile_doc(doc))
        return profiles

    async def get_profile_by_id(self, profile_id: str) -> Optional[SearchProfileResponse]:
        if not ObjectId.is_valid(profile_id):
            return None
        doc = await self.profiles_col.find_one({"_id": ObjectId(profile_id)})
        return format_profile_doc(doc) if doc else None

    async def update_profile(self, profile_id: str, update: SearchProfileUpdate) -> Optional[SearchProfileResponse]:
        if not ObjectId.is_valid(profile_id):
            return None
        data = update.model_dump(exclude_unset=True)
        if not data:
            return await self.get_profile_by_id(profile_id)
        data["updated_at"] = datetime.now(timezone.utc)
        res = await self.profiles_col.find_one_and_update(
            {"_id": ObjectId(profile_id)},
            {"$set": data},
            return_document=True
        )
        return format_profile_doc(res) if res else None

    async def delete_profile(self, profile_id: str) -> bool:
        if not ObjectId.is_valid(profile_id):
            return False
        res = await self.profiles_col.delete_one({"_id": ObjectId(profile_id)})
        return res.deleted_count > 0

    async def run_search(self, profile_id: str) -> Dict[str, Any]:
        if not ObjectId.is_valid(profile_id):
            raise HTTPException(status_code=400, detail="Invalid search profile ID")
        profile = await self.profiles_col.find_one({"_id": ObjectId(profile_id)})
        if not profile:
            raise HTTPException(status_code=404, detail="Search profile not found")

        app_settings = await settings_service.get_settings()
        max_jobs = app_settings.max_jobs_per_search or 15

        easy_apply_only = profile.get("easy_apply_only")
        if easy_apply_only is None:
            easy_apply_only = True

        scraped_jobs = await dice_browser.search_jobs(
            keywords=profile.get("keywords", []),
            location=profile.get("location", "United States"),
            is_remote=profile.get("is_remote", True),
            work_settings=profile.get("work_settings"),
            employment_types=profile.get("employment_types"),
            easy_apply_only=easy_apply_only,
            posted_within=profile.get("posted_within", "ONE"),
            radius=profile.get("radius", 30),
            experience_levels=profile.get("experience_levels"),
            max_results=max_jobs
        )

        new_jobs_count = 0
        duplicate_count = 0

        now = datetime.now(timezone.utc)
        for j in scraped_jobs:
            # Skip if already applied on Dice
            if j.get("is_applied"):
                duplicate_count += 1
                continue

            ext_id = j["external_job_id"]
            existing = await self.jobs_col.find_one({"external_job_id": ext_id})
            if existing:
                # Skip if already applied in database
                if existing.get("status") == "APPLIED":
                    duplicate_count += 1
                    continue

                # If existing job has a placeholder title or dummy company, update it with real scraped details
                is_placeholder = (
                    not existing.get("title")
                    or existing.get("title") == "Software Engineer"
                    or existing.get("company") in ["Employer", "Dice Employer", ""]
                )
                if is_placeholder and j.get("title") and j.get("title") != "Software Engineer":
                    await self.jobs_col.update_one(
                        {"_id": existing["_id"]},
                        {"$set": {
                            "title": j["title"],
                            "company": j["company"],
                            "location": j["location"],
                            "work_setting": j["work_setting"],
                            "salary": j["salary"],
                            "employment_type": j["employment_type"],
                            "posted_date": j["posted_date"],
                            "is_easy_apply": j["is_easy_apply"],
                            "job_url": j["job_url"],
                            "application_url": j["application_url"],
                            "application_wizard_url": j["application_wizard_url"],
                            "description_raw": j["description_raw"],
                            "search_profile_id": profile["_id"],
                            "updated_at": now
                        }}
                    )
                    new_jobs_count += 1
                else:
                    duplicate_count += 1
            else:
                j_doc = dict(j)
                j_doc["search_profile_id"] = profile["_id"]
                j_doc["status"] = "DISCOVERED"
                j_doc["created_at"] = now
                j_doc["updated_at"] = now
                await self.jobs_col.insert_one(j_doc)
                new_jobs_count += 1

        return {
            "profile_name": profile.get("name"),
            "total_found": len(scraped_jobs),
            "new_jobs_added": new_jobs_count,
            "duplicates_skipped": duplicate_count
        }

search_service = SearchService()
