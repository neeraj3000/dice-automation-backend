from typing import List, Dict, Any, Optional
from fastapi import APIRouter, HTTPException, Query, Depends
from app.schemas.application import (
    ApplicationCreate,
    ApplicationResponse,
    AnswersBatchSubmit,
    QueueApplicationRequest,
    QueueBatchRequest,
    QueueStatusResponse,
    ApplicationStatusResponse,
    ExternalApplicationTrackRequest,
    ExternalApplicationUpdateRequest,
)
from app.services.application_service import application_service
from app.services.application_queue import application_queue_manager
from app.services.linkedin_service import linkedin_service
from app.core.deps import get_current_user

router = APIRouter(prefix="/applications", tags=["Applications"])

@router.post("", response_model=ApplicationResponse)
async def start_application(req: ApplicationCreate, user: dict = Depends(get_current_user)):
    req.user_id = str(user["_id"])
    return await application_service.prepare_application(req)

@router.post("/prepare", response_model=ApplicationResponse)
async def prepare_application(req: ApplicationCreate, user: dict = Depends(get_current_user)):
    req.user_id = str(user["_id"])
    return await application_service.prepare_application(req)

@router.post("/apply", response_model=ApplicationResponse)
async def apply_to_job(req: ApplicationCreate, user: dict = Depends(get_current_user)):
    req.user_id = str(user["_id"])
    req.mode = "APPLY"
    return await application_service.prepare_application(req)

@router.post("/queue")
async def queue_application(req: QueueApplicationRequest, user: dict = Depends(get_current_user)):
    """Queues a job for automated background application execution."""
    uid = str(user["_id"])
    try:
        return await application_queue_manager.enqueue_job(
            job_id=req.job_id,
            resume_id=req.resume_id,
            user_id=uid,
            mode=req.mode or "APPLY"
        )
    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/queue/batch")
async def queue_batch_applications(req: QueueBatchRequest, user: dict = Depends(get_current_user)):
    """Batch queues multiple jobs for automated sequential execution."""
    uid = str(user["_id"])
    return await application_queue_manager.enqueue_jobs(
        job_ids=req.job_ids,
        user_id=uid,
        mode=req.mode or "APPLY"
    )

@router.get("/queue/status", response_model=QueueStatusResponse)
async def get_queue_status(user: dict = Depends(get_current_user)):
    """Retrieves current queue activity and application status metrics."""
    return await application_queue_manager.get_queue_status()

@router.get("", response_model=List[ApplicationResponse])
async def list_applications(
    status: Optional[str] = Query(None, description="Filter by status"),
    board: Optional[str] = Query(None, description="Filter by board (dice, linkedin)"),
    source: Optional[str] = Query(None, description="Filter by source (dice, linkedin)"),
    user: dict = Depends(get_current_user)
):
    user_id = user["_id"]
    target_board = (board or source or "").strip().lower()
    apps = await application_service.get_applications(user_id=user_id)
    if target_board and target_board != "all":
        apps = [
            a for a in apps
            if (getattr(a, "board", "") or "").lower() == target_board
            or (getattr(a, "source", "") or "").lower() == target_board
        ]
    if status and status != "ALL":
        apps = [a for a in apps if a.status == status]
    return apps

@router.post("/external", response_model=ApplicationResponse)
@router.post("/linkedin", response_model=ApplicationResponse, include_in_schema=False)
async def track_external_application(
    req: ExternalApplicationTrackRequest,
    user: dict = Depends(get_current_user)
):
    """Creates or idempotently updates a user-confirmed external (LinkedIn) application record."""
    user_id = str(user["_id"])
    return await linkedin_service.track_application(req, user_id=user_id)

@router.get("/external", response_model=List[ApplicationResponse])
@router.get("/linkedin", response_model=List[ApplicationResponse], include_in_schema=False)
async def list_external_applications(
    source: Optional[str] = Query("linkedin", description="Filter by external board source"),
    user: dict = Depends(get_current_user)
):
    """Retrieves all external user-confirmed applications for the authenticated user."""
    user_id = str(user["_id"])
    return await linkedin_service.get_user_external_applications(user_id=user_id, source=source)

@router.get("/external/{app_id}", response_model=ApplicationResponse)
@router.get("/linkedin/{app_id}", response_model=ApplicationResponse, include_in_schema=False)
async def get_external_application(
    app_id: str,
    user: dict = Depends(get_current_user)
):
    """Retrieves a single external application, enforcing strict tenant ownership."""
    user_id = str(user["_id"])
    app = await linkedin_service.get_external_application(app_id=app_id, user_id=user_id)
    if not app:
        raise HTTPException(status_code=404, detail="Application not found")
    return app

@router.patch("/external/{app_id}", response_model=ApplicationResponse)
@router.patch("/linkedin/{app_id}", response_model=ApplicationResponse, include_in_schema=False)
async def update_external_application(
    app_id: str,
    req: ExternalApplicationUpdateRequest,
    user: dict = Depends(get_current_user)
):
    """Updates permitted status or notes for a user-confirmed application with full audit history."""
    user_id = str(user["_id"])
    return await linkedin_service.update_external_application(app_id=app_id, user_id=user_id, req=req)

@router.get("/external/{app_id}/history", response_model=List[Dict[str, Any]])
@router.get("/linkedin/{app_id}/history", response_model=List[Dict[str, Any]], include_in_schema=False)
async def get_external_application_history(
    app_id: str,
    user: dict = Depends(get_current_user)
):
    """Retrieves audit trail and status transition history for the specified application."""
    user_id = str(user["_id"])
    return await linkedin_service.get_application_history(app_id=app_id, user_id=user_id)

@router.get("/{app_id}/status", response_model=ApplicationStatusResponse)
async def get_application_status(app_id: str, user: dict = Depends(get_current_user)):
    """Allows the frontend to poll and retrieve real-time execution status."""
    user_id = str(user["_id"])
    res = await application_queue_manager.get_application_status(app_id)
    if not res:
        raise HTTPException(status_code=404, detail="Application status not found")
    # Verify ownership
    app_doc = await application_service.get_application_by_id(app_id, user_id=user_id)
    if not app_doc:
        raise HTTPException(status_code=404, detail="Application status not found")
    return res

@router.post("/{app_id}/retry")
async def retry_application(app_id: str, user: dict = Depends(get_current_user)):
    """Retries a failed or timed out application through the execution queue."""
    user_id = str(user["_id"])
    try:
        return await application_queue_manager.enqueue_application(
            application_id=app_id,
            user_id=user_id,
            mode="APPLY"
        )
    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve))

@router.post("/{app_id}/stop")
async def stop_application(app_id: str, user: dict = Depends(get_current_user)):
    """Stops a queued or running application and halts its browser automation."""
    user_id = str(user["_id"])
    try:
        res = await application_queue_manager.cancel_application(
            application_id=app_id,
            user_id=user_id
        )
    except ValueError as ve:
        raise HTTPException(status_code=409, detail=str(ve))
    if not res:
        raise HTTPException(status_code=404, detail="Application not found")
    return res

@router.get("/{app_id}", response_model=ApplicationResponse)
async def get_application(app_id: str, user: dict = Depends(get_current_user)):
    user_id = user["_id"]
    res = await application_service.get_application_by_id(app_id, user_id=user_id)
    if not res:
        raise HTTPException(status_code=404, detail="Application not found")
    return res

@router.post("/{app_id}/answers", response_model=ApplicationResponse)
async def answer_application(app_id: str, payload: AnswersBatchSubmit, user: dict = Depends(get_current_user)):
    user_id = str(user["_id"])
    res = await application_service.answer_application_batch(app_id, payload.answers, user_id=user_id)
    if not res:
        raise HTTPException(status_code=404, detail="Application not found")
    return res

@router.post("/{app_id}/submit", response_model=ApplicationResponse)
async def submit_application(app_id: str, user: dict = Depends(get_current_user)):
    user_id = str(user["_id"])
    return await application_service.submit_application(app_id, user_id=user_id)

