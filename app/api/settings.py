import asyncio
from typing import Optional, List, Dict, Any
from fastapi import APIRouter, Depends, Body
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

@router.get("/settings")
async def get_app_settings():
    app_cfg = await settings_service.get_settings()
    profile = await settings_service.get_profile()
    profile_dict = profile.model_dump()
    profile_dict["linkedin"] = profile_dict.get("linkedin_url", "")
    profile_dict["github"] = profile_dict.get("github_url", "")
    profile_dict["portfolio"] = profile_dict.get("portfolio_url", "")
    try:
        profile_dict["years_experience"] = int(profile_dict.get("years_of_experience", 0)) if profile_dict.get("years_of_experience") else None
    except Exception:
        profile_dict["years_experience"] = None
    profile_dict["authorized_to_work"] = profile_dict.get("work_authorization") in ["US Citizen", "Green Card", "Authorized", "Yes", True]
    profile_dict["requires_sponsorship"] = not profile_dict["authorized_to_work"]

    data = app_cfg.model_dump()
    data["profile"] = profile_dict
    data["headless"] = data.get("headless_browser", False)
    return data

from fastapi import Body
from app.core.deps import get_current_user_optional

@router.put("/settings")
async def update_app_settings(body: Dict[str, Any] = Body(...)):
    if "profile" in body and isinstance(body["profile"], dict):
        p_raw = dict(body["profile"])
        if "linkedin" in p_raw: p_raw["linkedin_url"] = p_raw["linkedin"]
        if "github" in p_raw: p_raw["github_url"] = p_raw["github"]
        if "portfolio" in p_raw: p_raw["portfolio_url"] = p_raw["portfolio"]
        if "years_experience" in p_raw and p_raw["years_experience"] is not None:
            p_raw["years_of_experience"] = str(p_raw["years_experience"])
        if "authorized_to_work" in p_raw:
            p_raw["work_authorization"] = "US Citizen" if p_raw["authorized_to_work"] else "Requires Sponsorship"

        prof = UserProfileSchema(**p_raw)
        await settings_service.update_profile(prof)

    if "headless" in body:
        body["headless_browser"] = bool(body["headless"])

    cfg_data = {k: v for k, v in body.items() if k not in ["profile", "headless"]}
    current_cfg = await settings_service.get_settings()
    merged = current_cfg.model_dump()
    merged.update(cfg_data)
    if "headless_browser" in body:
        merged["headless_browser"] = body["headless_browser"]

    await settings_service.update_settings(AppSettingsSchema(**merged))
    return await get_app_settings()


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

from pydantic import BaseModel

class SessionImportPayload(BaseModel):
    cookies: Optional[List[Dict[str, Any]]] = None
    cookie_string: Optional[str] = None
    username: Optional[str] = None
    local_storage: Optional[Dict[str, Any]] = None

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

@router.get("/stats")
@router.get("/dashboard/stats")
async def get_dashboard_stats(user: Optional[dict] = Depends(get_current_user_optional)):
    db = get_database()
    resumes_count = await db.resumes.count_documents({})
    profiles_count = await db.search_profiles.count_documents({})
    jobs_count = await db.jobs.count_documents({})
    jobs_matched = await db.jobs.count_documents({"status": "MATCHED"})
    apps_count = await db.applications.count_documents({})
    apps_ready = await db.applications.count_documents({"status": "READY"})
    apps_applied = await db.applications.count_documents({"status": "APPLIED"})
    review_count = await db.application_answers.count_documents({"is_answered": False})

    # Group applications by status for bench-sales-frontend
    apps_by_status = {}
    async for d in db.applications.aggregate([{"$group": {"_id": "$status", "n": {"$sum": 1}}}]):
        if d.get("_id"):
            apps_by_status[d["_id"]] = d.get("n", 0)

    # Check board connection status
    from app.services.settings_service import settings_service
    d_stat = await settings_service.get_dice_status(check_live=False)
    conn_count = 1 if d_stat.get("is_connected") else 0

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
        # bench-sales keys
        "jobs": jobs_count,
        "resumes": resumes_count,
        "applications": apps_by_status,
        "connected_boards": conn_count,
        # dice-automation keys
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

