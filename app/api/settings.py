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

@router.put("/settings", response_model=AppSettingsSchema)
async def update_app_settings(settings: AppSettingsSchema):
    return await settings_service.update_settings(settings)

@router.post("/settings/open-dice-login")
async def open_dice_login():
    """Opens a visible browser page to the Dice login screen (local) or provides instructions (cloud)."""
    from app.browser.playwright_manager import playwright_manager
    from fastapi import HTTPException

    headless_env = os.environ.get("HEADLESS_BROWSER")
    is_headless = headless_env.lower() in ("true", "1", "yes") if headless_env else (sys.platform != "win32")

    if is_headless:
        # In cloud/headless mode, the server cannot open a desktop window on the client's screen.
        logger.info("open-dice-login called in headless cloud mode.")
        try:
            status = await settings_service.get_dice_status(check_live=True)
            if status.get("is_connected"):
                return {
                    "status": "success",
                    "message": "Dice session is already active in the cloud!",
                    "is_connected": True
                }
        except Exception:
            pass

        return {
            "status": "warning",
            "message": "Backend is running on a cloud server without a display. A browser window cannot open on your computer from the server. Please import your Dice session cookies via the Session Importer.",
            "is_connected": False,
            "cloud_mode": True
        }

    try:
        page = await playwright_manager.get_new_page()
        await page.goto("https://www.dice.com/dashboard/login", wait_until="domcontentloaded", timeout=15000)
        return {"status": "success", "message": "Browser opened to Dice login page"}
    except Exception as e:
        logger.error(f"Error opening Dice login page: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Could not open browser: {str(e)}"
        )

@router.post("/settings/import-dice-session")
async def import_dice_session(payload: SessionImportPayload):
    """Imports Dice session cookies into MongoDB and browser context for cloud deployments."""
    return await settings_service.import_dice_session(
        cookies=payload.cookies,
        cookie_string=payload.cookie_string,
        username=payload.username
    )

@router.get("/settings/dice-status")
async def get_dice_status(check_live: bool = False):
    """Returns the current Dice account connection status."""
    return await settings_service.get_dice_status(check_live=check_live)

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
