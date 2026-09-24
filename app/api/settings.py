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

@router.put("/settings", response_model=AppSettingsSchema)
async def update_app_settings(settings: AppSettingsSchema):
    return await settings_service.update_settings(settings)

@router.post("/settings/open-dice-login")
async def open_dice_login():
    """Opens a visible browser page to the Dice login screen using the persistent profile."""
    from app.browser.playwright_manager import playwright_manager
    page = await playwright_manager.get_new_page()
    await page.goto("https://www.dice.com/dashboard/login", wait_until="domcontentloaded")
    return {"status": "success", "message": "Browser opened to Dice login page"}

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
