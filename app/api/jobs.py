from typing import List, Optional
from fastapi import APIRouter, HTTPException, Query
from app.schemas.job import JobResponse, DirectMatchRequest
from app.services.job_service import job_service

router = APIRouter(prefix="/jobs", tags=["Jobs"])

@router.get("", response_model=List[JobResponse])
async def list_jobs(
    search: Optional[str] = Query(None, description="Search across title, company, description"),
    company: Optional[str] = Query(None, description="Filter by company"),
    status: Optional[str] = Query(None, description="Filter by job status"),
    location: Optional[str] = Query(None, description="Filter by location"),
    search_profile_id: Optional[str] = Query(None, description="Filter by search profile ID"),
    exclude_applied: bool = Query(False, description="Exclude already applied jobs")
):
    return await job_service.get_jobs(
        search=search,
        company=company,
        status=status,
        location=location,
        search_profile_id=search_profile_id,
        exclude_applied=exclude_applied
    )

@router.delete("/clear")
async def clear_all_jobs():
    return await job_service.clear_all_jobs()

@router.get("/debug-dice")
async def debug_dice():
    from app.browser.dice_browser import dice_browser
    return await dice_browser.debug_dom()

@router.get("/{job_id}", response_model=JobResponse)
async def get_job(job_id: str):
    res = await job_service.get_job_by_id(job_id)
    if not res:
        raise HTTPException(status_code=404, detail="Job not found")
    return res

@router.post("/{job_id}/analyze", response_model=JobResponse)
async def analyze_job(job_id: str):
    return await job_service.analyze_job(job_id)

@router.post("/{job_id}/match", response_model=JobResponse)
async def match_job(job_id: str):
    return await job_service.match_job(job_id)

@router.post("/match-direct")
async def match_direct(req: DirectMatchRequest):
    return await job_service.match_direct(req)


