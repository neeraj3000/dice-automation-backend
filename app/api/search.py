from typing import List
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
