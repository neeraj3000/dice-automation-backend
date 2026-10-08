from typing import List, Optional, Union, Dict, Any
from fastapi import APIRouter, HTTPException, Query, Depends
from app.schemas.job import JobResponse, JobListResponse, DirectMatchRequest
from app.services.job_service import job_service
from app.core.deps import get_current_user_optional

router = APIRouter(prefix="/jobs", tags=["Jobs"])

@router.get("", response_model=Union[JobListResponse, List[JobResponse]])
async def list_jobs(
    search: Optional[str] = Query(None, description="Search across title, company, description"),
    q: Optional[str] = Query(None, description="Search keyword"),
    board: Optional[str] = Query(None, description="Filter by job board"),
    easy_apply: Optional[bool] = Query(None, description="Filter by easy apply"),
    company: Optional[str] = Query(None, description="Filter by company"),
    status: Optional[str] = Query(None, description="Filter by job status"),
    location: Optional[str] = Query(None, description="Filter by location"),
    search_profile_id: Optional[str] = Query(None, description="Filter by search profile ID"),
    exclude_applied: bool = Query(False, description="Exclude already applied jobs"),
    page: Optional[int] = Query(None, ge=1, description="Page number"),
    page_size: Optional[int] = Query(None, ge=1, le=100, description="Page size"),
    user: Optional[dict] = Depends(get_current_user_optional)
):
    user_id = str(user["_id"]) if user else None
    return await job_service.get_jobs(
        search=search,
        q=q,
        board=board,
        easy_apply=easy_apply,
        company=company,
        status=status,
        location=location,
        search_profile_id=search_profile_id,
        exclude_applied=exclude_applied,
        page=page,
        page_size=page_size,
        user_id=user_id
    )

@router.delete("/clear")
async def clear_all_jobs(user: Optional[dict] = Depends(get_current_user_optional)):
    user_id = str(user["_id"]) if user else None
    return await job_service.clear_all_jobs(user_id=user_id)

@router.delete("/{job_id}", status_code=204)
async def delete_job(job_id: str, user: Optional[dict] = Depends(get_current_user_optional)):
    user_id = str(user["_id"]) if user else None
    deleted = await job_service.delete_job(job_id, user_id=user_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Job not found")
    return None

@router.get("/debug-dice")
async def debug_dice():
    from app.browser.dice_browser import dice_browser
    return await dice_browser.debug_dom()

@router.get("/{job_id}", response_model=JobResponse)
async def get_job(job_id: str, user: Optional[dict] = Depends(get_current_user_optional)):
    user_id = str(user["_id"]) if user else None
    res = await job_service.get_job_by_id(job_id, user_id=user_id)
    if not res:
        raise HTTPException(status_code=404, detail="Job not found")
    return res

@router.post("/{job_id}/analyze", response_model=JobResponse)
async def analyze_job(job_id: str):
    return await job_service.analyze_job(job_id)

@router.post("/{job_id}/match", response_model=JobResponse)
async def match_job(job_id: str, user: Optional[dict] = Depends(get_current_user_optional)):
    user_id = str(user["_id"]) if user else None
    return await job_service.match_job(job_id, user_id=user_id)

@router.post("/match-direct")
async def match_direct(req: DirectMatchRequest, user: Optional[dict] = Depends(get_current_user_optional)):
    user_id = str(user["_id"]) if user else None
    return await job_service.match_direct(req, user_id=user_id)


