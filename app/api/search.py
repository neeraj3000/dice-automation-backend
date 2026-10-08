from typing import List, Optional, Dict, Any
from fastapi import APIRouter, HTTPException
from app.schemas.search_profile import SearchProfileCreate, SearchProfileUpdate, SearchProfileResponse
from app.services.search_service import search_service

router = APIRouter(prefix="/search-profiles", tags=["Search Profiles"])

@router.post("", response_model=SearchProfileResponse)
async def create_profile(profile: SearchProfileCreate):
    return await search_service.create_profile(profile)

@router.get("", response_model=List[SearchProfileResponse])
async def list_profiles():
    return await search_service.get_profiles()

@router.get("/{profile_id}", response_model=SearchProfileResponse)
async def get_profile(profile_id: str):
    res = await search_service.get_profile_by_id(profile_id)
    if not res:
        raise HTTPException(status_code=404, detail="Search profile not found")
    return res

@router.put("/{profile_id}", response_model=SearchProfileResponse)
async def update_profile(profile_id: str, update: SearchProfileUpdate):
    res = await search_service.update_profile(profile_id, update)
    if not res:
        raise HTTPException(status_code=404, detail="Search profile not found")
    return res

@router.delete("/{profile_id}")
async def delete_profile(profile_id: str):
    success = await search_service.delete_profile(profile_id)
    if not success:
        raise HTTPException(status_code=404, detail="Search profile not found")
    return {"success": True, "message": "Search profile deleted"}

@router.post("/{profile_id}/run")
async def run_search(profile_id: str):
    return await search_service.run_search(profile_id)

import asyncio
from datetime import datetime, timezone
from bson import ObjectId
from fastapi import Body, Depends
from app.database import get_database
from app.core.deps import get_current_user_optional

search_router = APIRouter(prefix="/search", tags=["Search Execution"])

@search_router.post("/run", status_code=202)
async def run_search_job(body: dict = Body(...), user: Optional[dict] = Depends(get_current_user_optional)):
    db = get_database()
    user_id = str(user["_id"]) if user else "default"

    existing_running = await db.searches.find_one({"user_id": user_id, "status": "RUNNING"})
    if existing_running:
        raise HTTPException(status_code=409, detail="A search is already running")

    board = body.get("board", "dice")
    doc = {
        "user_id": user_id,
        "board": board,
        "criteria": body,
        "status": "RUNNING",
        "found": 0,
        "saved": 0,
        "error": None,
        "created_at": datetime.now(timezone.utc)
    }
    insert_res = await db.searches.insert_one(doc)
    doc["_id"] = insert_res.inserted_id
    search_id = str(insert_res.inserted_id)

    async def _execute_search():
        try:
            from app.browser.dice_browser import dice_browser
            keywords = body.get("keywords", "")
            if isinstance(keywords, str):
                keywords = [k.strip() for k in keywords.split(",") if k.strip()]
            location = body.get("location", "United States")
            is_remote = "Remote" in body.get("work_settings", []) or body.get("is_remote", False)
            work_settings = body.get("work_settings", [])
            employment_types = body.get("employment_types", [])
            easy_apply = body.get("easy_apply_only", True)
            posted = body.get("posted_within", "3d")
            max_results = body.get("max_results", 20)

            posted_map = {"today": "ONE", "3d": "THREE", "7d": "SEVEN", "any": None}
            dice_posted = posted_map.get(posted, posted)

            jobs = await dice_browser.search_jobs(
                keywords=keywords,
                location=location,
                is_remote=is_remote,
                work_settings=work_settings,
                employment_types=employment_types,
                easy_apply_only=easy_apply,
                posted_within=dice_posted,
                max_results=max_results
            )

            await db.searches.update_one({"_id": ObjectId(search_id)}, {"$set": {"found": len(jobs)}})

            saved_count = 0
            for j in jobs:
                ext_id = j.get("external_job_id")
                if not ext_id:
                    continue
                j["user_id"] = user_id
                j["board"] = board
                j["search_id"] = search_id
                j["created_at"] = datetime.now(timezone.utc)
                j["updated_at"] = datetime.now(timezone.utc)
                up_res = await db.jobs.update_one(
                    {"external_job_id": ext_id},
                    {"$setOnInsert": j},
                    upsert=True
                )
                if up_res.upserted_id:
                    saved_count += 1

            await db.searches.update_one(
                {"_id": ObjectId(search_id)},
                {"$set": {"status": "COMPLETED", "saved": saved_count, "finished_at": datetime.now(timezone.utc)}}
            )
        except Exception as e:
            await db.searches.update_one(
                {"_id": ObjectId(search_id)},
                {"$set": {"status": "FAILED", "error": str(e), "finished_at": datetime.now(timezone.utc)}}
            )

    asyncio.create_task(_execute_search())

    from app.database import sanitize_object_ids
    doc = sanitize_object_ids(doc)
    doc["id"] = str(doc.pop("_id", doc.get("id", "")))
    return doc

@search_router.get("/latest")
async def get_latest_search(user: Optional[dict] = Depends(get_current_user_optional)):
    db = get_database()
    user_id = str(user["_id"]) if user else "default"
    doc = await db.searches.find_one({"user_id": user_id}, sort=[("created_at", -1)])
    if not doc and user_id != "default":
        doc = await db.searches.find_one(sort=[("created_at", -1)])
    if not doc:
        return None
    from app.database import sanitize_object_ids
    doc = sanitize_object_ids(doc)
    doc["id"] = str(doc.pop("_id", doc.get("id", "")))
    return doc

@search_router.get("/{search_id}")
async def get_search_by_id(search_id: str, user: Optional[dict] = Depends(get_current_user_optional)):
    if not ObjectId.is_valid(search_id):
        raise HTTPException(status_code=400, detail="Invalid search ID")
    db = get_database()
    doc = await db.searches.find_one({"_id": ObjectId(search_id)})
    if not doc:
        raise HTTPException(status_code=404, detail="Search not found")
    from app.database import sanitize_object_ids
    doc = sanitize_object_ids(doc)
    doc["id"] = str(doc.pop("_id", doc.get("id", "")))
    return doc

