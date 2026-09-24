from typing import List
from fastapi import APIRouter, HTTPException
from app.schemas.application import ApplicationCreate, ApplicationResponse
from app.services.application_service import application_service

router = APIRouter(prefix="/applications", tags=["Applications"])

@router.post("/prepare", response_model=ApplicationResponse)
async def prepare_application(req: ApplicationCreate):
    return await application_service.prepare_application(req)

@router.post("/apply", response_model=ApplicationResponse)
async def apply_to_job(req: ApplicationCreate):
    req.mode = "APPLY"
    return await application_service.prepare_application(req)

@router.get("", response_model=List[ApplicationResponse])
async def list_applications():
    return await application_service.get_applications()

@router.get("/{app_id}", response_model=ApplicationResponse)
async def get_application(app_id: str):
    res = await application_service.get_application_by_id(app_id)
    if not res:
        raise HTTPException(status_code=404, detail="Application not found")
    return res

@router.post("/{app_id}/submit", response_model=ApplicationResponse)
async def submit_application(app_id: str):
    return await application_service.submit_application(app_id)
