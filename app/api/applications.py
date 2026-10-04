from typing import List, Dict, Any
from fastapi import APIRouter, HTTPException
from app.schemas.application import (
    ApplicationCreate,
    ApplicationResponse,
    QueueApplicationRequest,
    QueueBatchRequest,
    QueueStatusResponse,
    ApplicationStatusResponse
)
from app.services.application_service import application_service
from app.services.application_queue import application_queue_manager

router = APIRouter(prefix="/applications", tags=["Applications"])

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
async def list_applications():
    return await application_service.get_applications()

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
async def get_application(app_id: str):
    res = await application_service.get_application_by_id(app_id)
    if not res:
        raise HTTPException(status_code=404, detail="Application not found")
    return res

@router.post("/{app_id}/submit", response_model=ApplicationResponse)
async def submit_application(app_id: str):
    return await application_service.submit_application(app_id)

