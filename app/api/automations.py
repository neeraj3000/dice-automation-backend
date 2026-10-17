from typing import List, Optional

from fastapi import APIRouter, Depends, Query

from app.core.deps import get_current_user
from app.schemas.automation import (
    AutomationCreate,
    AutomationResponse,
    AutomationRunPage,
    AutomationRunResponse,
    AutomationUpdate,
)
from app.services.automation_service import automation_service

router = APIRouter(prefix="/automations", tags=["Automations"])
runs_router = APIRouter(prefix="/automation-runs", tags=["Automations"])


@router.post("", response_model=AutomationResponse)
async def create_automation(body: AutomationCreate, user: dict = Depends(get_current_user)):
    return await automation_service.create_automation(str(user["_id"]), body)


@router.get("", response_model=List[AutomationResponse])
async def list_automations(user: dict = Depends(get_current_user)):
    return await automation_service.list_automations(str(user["_id"]))


@router.get("/{automation_id}", response_model=AutomationResponse)
async def get_automation(automation_id: str, user: dict = Depends(get_current_user)):
    return await automation_service.get_automation(str(user["_id"]), automation_id)


@router.put("/{automation_id}", response_model=AutomationResponse)
async def update_automation(automation_id: str, body: AutomationUpdate, user: dict = Depends(get_current_user)):
    return await automation_service.update_automation(str(user["_id"]), automation_id, body)


@router.delete("/{automation_id}")
async def delete_automation(automation_id: str, user: dict = Depends(get_current_user)):
    await automation_service.delete_automation(str(user["_id"]), automation_id)
    return {"success": True, "message": "Automation deleted"}


@router.post("/{automation_id}/start", response_model=AutomationRunResponse, status_code=202)
async def start_automation(automation_id: str, user: dict = Depends(get_current_user)):
    """Starts a run in the background; it keeps going after the browser tab is closed."""
    return await automation_service.start_run(str(user["_id"]), automation_id)


@runs_router.get("", response_model=AutomationRunPage)
async def list_run_history(
    automation_id: Optional[str] = Query(None, description="Only runs of this automation"),
    limit: int = Query(20, ge=1, le=100),
    skip: int = Query(0, ge=0),
    user: dict = Depends(get_current_user),
):
    """Run history, newest first. Runs of deleted automations are kept."""
    return await automation_service.list_run_history(str(user["_id"]), automation_id, limit, skip)


@runs_router.get("/active", response_model=Optional[AutomationRunResponse])
async def get_active_run(user: dict = Depends(get_current_user)):
    return await automation_service.get_active_run(str(user["_id"]))


@runs_router.get("/{run_id}", response_model=AutomationRunResponse)
async def get_run(run_id: str, user: dict = Depends(get_current_user)):
    return await automation_service.get_run(str(user["_id"]), run_id)


@runs_router.post("/{run_id}/pause", response_model=AutomationRunResponse)
async def pause_run(run_id: str, user: dict = Depends(get_current_user)):
    """Pauses after the current job finishes; the timer is frozen while paused."""
    return await automation_service.set_paused(str(user["_id"]), run_id, True)


@runs_router.post("/{run_id}/resume", response_model=AutomationRunResponse)
async def resume_run(run_id: str, user: dict = Depends(get_current_user)):
    return await automation_service.set_paused(str(user["_id"]), run_id, False)


@runs_router.post("/{run_id}/stop", response_model=AutomationRunResponse)
async def stop_run(run_id: str, user: dict = Depends(get_current_user)):
    return await automation_service.stop_run(str(user["_id"]), run_id)
