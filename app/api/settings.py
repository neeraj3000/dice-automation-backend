import asyncio
from fastapi import APIRouter
from app.database import get_database
from app.schemas.user_profile import UserProfileSchema, AppSettingsSchema
from app.services.settings_service import settings_service

router = APIRouter(tags=["Settings & Profile"])

@router.get("/profile", response_model=UserProfileSchema)
async def get_user_profile():
    return await settings_service.get_profile()

@router.put("/profile", response_model=UserProfileSchema)
async def update_user_profile(profile: UserProfileSchema):
    return await settings_service.update_profile(profile)

@router.get("/settings", response_model=AppSettingsSchema)
async def get_app_settings():
    return await settings_service.get_settings()

import os
import sys
import logging
from pydantic import BaseModel
from typing import Optional, List, Dict, Any

logger = logging.getLogger(__name__)

class SessionImportPayload(BaseModel):
    cookies: Optional[List[Dict[str, Any]]] = None
    cookie_string: Optional[str] = None
    username: Optional[str] = None
    local_storage: Optional[Dict[str, Any]] = None

@router.put("/settings", response_model=AppSettingsSchema)
async def update_app_settings(settings: AppSettingsSchema):
    return await settings_service.update_settings(settings)

from fastapi.responses import StreamingResponse
from app.services.dice_session_manager import dice_session_manager

@router.post("/settings/open-dice-login")
async def open_dice_login():
    """Opens a visible browser page to the Dice login screen (local) or provides instructions (cloud)."""
    return await dice_session_manager.start_interactive_login()

@router.get("/settings/dice-session/stream")
async def stream_dice_session():
    """Real-time Server-Sent Events (SSE) stream for Dice session connection status."""
    return StreamingResponse(
        dice_session_manager.event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"
        }
    )

@router.post("/settings/import-dice-session")
async def import_dice_session(payload: SessionImportPayload):
    """Imports Dice session cookies into MongoDB and browser context for cloud deployments."""
    return await settings_service.import_dice_session(
        cookies=payload.cookies,
        cookie_string=payload.cookie_string,
        username=payload.username,
        local_storage=payload.local_storage
    )

@router.get("/settings/dice-status")
async def get_dice_status(check_live: bool = False):
    """Returns the current Dice account connection status."""
    return await settings_service.get_dice_status(check_live=check_live)

@router.post("/settings/disconnect-dice")
async def disconnect_dice():
    """Disconnects the local Dice session, removes local cookies, and resets state."""
    return await settings_service.disconnect_dice()

@router.get("/settings/browser-status")
async def get_browser_status():
    """Returns Playwright browser binary presence and environment status."""
    from app.browser.playwright_manager import playwright_manager
    return await playwright_manager.get_browser_status()

@router.post("/settings/install-browser")
async def install_browser():
    """Verifies presence of Playwright Chromium browser binary."""
    from app.browser.playwright_manager import playwright_manager
    installed = await playwright_manager.ensure_browser_installed()
    status = await playwright_manager.get_browser_status()
    return {
        "success": installed,
        "message": "Playwright Chromium browser binary verified." if installed else "Playwright Chromium browser binary not found. Ensure it was installed during build phase via 'python -m playwright install --with-deps chromium'.",
        **status
    }

@router.get("/dashboard/stats")
async def get_dashboard_stats():
    db = get_database()
    resumes_count = await db.resumes.count_documents({})
    profiles_count = await db.search_profiles.count_documents({})
    jobs_count = await db.jobs.count_documents({})
    jobs_matched = await db.jobs.count_documents({"status": "MATCHED"})
    apps_count = await db.applications.count_documents({})
    apps_ready = await db.applications.count_documents({"status": "READY"})
    apps_applied = await db.applications.count_documents({"status": "APPLIED"})
    review_count = await db.application_answers.count_documents({"is_answered": False})

    # Get 5 recent applications
    recent_apps = []
    cursor = db.applications.find({}).sort("updated_at", -1).limit(5)
    async for doc in cursor:
        recent_apps.append({
            "id": str(doc["_id"]),
            "company": doc.get("company", ""),
            "job_title": doc.get("job_title", ""),
            "status": doc.get("status", ""),
            "resume_name": doc.get("resume_name", ""),
            "updated_at": doc.get("updated_at")
        })

    return {
        "resumes_count": resumes_count,
        "profiles_count": profiles_count,
        "jobs_count": jobs_count,
        "jobs_matched": jobs_matched,
        "apps_count": apps_count,
        "apps_ready": apps_ready,
        "apps_applied": apps_applied,
        "review_count": review_count,
        "recent_applications": recent_apps
    }
