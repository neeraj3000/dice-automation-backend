from typing import List, Dict, Any, Optional
from fastapi import APIRouter, HTTPException, Query, Depends
from app.schemas.application import (
    ApplicationCreate,
    ApplicationResponse,
    AnswersBatchSubmit,
    QueueApplicationRequest,
    QueueBatchRequest,
    QueueStatusResponse,
    ApplicationStatusResponse
)
from app.services.application_service import application_service
from app.services.application_queue import application_queue_manager
from app.core.deps import get_current_user_optional

router = APIRouter(prefix="/applications", tags=["Applications"])

@router.post("", response_model=ApplicationResponse)
async def start_application(req: ApplicationCreate, user: Optional[dict] = Depends(get_current_user_optional)):
    if user:
        req.user_id = str(user["_id"])
    return await application_service.prepare_application(req)

@router.post("/prepare", response_model=ApplicationResponse)
async def prepare_application(req: ApplicationCreate):
    return await application_service.prepare_application(req)

@router.post("/apply", response_model=ApplicationResponse)
async def apply_to_job(req: ApplicationCreate):
    req.mode = "APPLY"
    return await application_service.prepare_application(req)

@router.post("/queue")
async def queue_application(req: QueueApplicationRequest):
    """Queues a job for automated background application execution."""
    try:
        return await application_queue_manager.enqueue_job(
            job_id=req.job_id,
            resume_id=req.resume_id,
            user_id=req.user_id or "default",
            mode=req.mode or "APPLY"
        )
    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/queue/batch")
async def queue_batch_applications(req: QueueBatchRequest):
    """Batch queues multiple jobs for automated sequential execution."""
    return await application_queue_manager.enqueue_jobs(
        job_ids=req.job_ids,
        user_id=req.user_id or "default",
        mode=req.mode or "APPLY"
    )

@router.get("/queue/status", response_model=QueueStatusResponse)
async def get_queue_status():
    """Retrieves current queue activity and application status metrics."""
    return await application_queue_manager.get_queue_status()

@router.get("", response_model=List[ApplicationResponse])
async def list_applications(
    status: Optional[str] = Query(None, description="Filter by status"),
    user: Optional[dict] = Depends(get_current_user_optional)
):
    user_id = user["_id"] if user else None
    apps = await application_service.get_applications(user_id=user_id)
    if status and status != "ALL":
        apps = [a for a in apps if a.status == status]
    return apps

@router.get("/{app_id}/status", response_model=ApplicationStatusResponse)
async def get_application_status(app_id: str):
    """Allows the frontend to poll and retrieve real-time execution status."""
    res = await application_queue_manager.get_application_status(app_id)
    if not res:
        raise HTTPException(status_code=404, detail="Application status not found")
    return res

@router.post("/{app_id}/retry")
async def retry_application(app_id: str):
    """Retries a failed or timed out application through the execution queue."""
    try:
        return await application_queue_manager.enqueue_application(
            application_id=app_id,
            mode="APPLY"
        )
    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve))

@router.get("/{app_id}", response_model=ApplicationResponse)
async def get_application(app_id: str, user: Optional[dict] = Depends(get_current_user_optional)):
    user_id = user["_id"] if user else None
    res = await application_service.get_application_by_id(app_id, user_id=user_id)
    if not res:
        raise HTTPException(status_code=404, detail="Application not found")
    return res

@router.post("/{app_id}/answers", response_model=ApplicationResponse)
async def answer_application(app_id: str, payload: AnswersBatchSubmit):
    res = await application_service.answer_application_batch(app_id, payload.answers)
    if not res:
        raise HTTPException(status_code=404, detail="Application not found")
    return res

@router.post("/{app_id}/submit", response_model=ApplicationResponse)
async def submit_application(app_id: str):
    return await application_service.submit_application(app_id)

