import asyncio
from typing import Optional, List, Dict, Any
from fastapi import APIRouter, Depends, Body
from app.core.deps import get_current_user_optional
from app.database import get_database
from app.schemas.user_profile import UserProfileSchema, AppSettingsSchema
from app.services.settings_service import settings_service

router = APIRouter(tags=["Settings & Profile"])

@router.get("/profile", response_model=UserProfileSchema)
async def get_user_profile(user: Optional[dict] = Depends(get_current_user_optional)):
    db = get_database()
    if user and db is not None:
        uid = user["_id"]
        user_sett = await db.settings.find_one({"user_id": {"$in": [uid, str(uid)]}})
        if user_sett and user_sett.get("profile"):
            p_data = dict(user_sett["profile"])
            return UserProfileSchema(**{**UserProfileSchema().model_dump(), **p_data})
        name_parts = (user.get("name") or "").split()
        return UserProfileSchema(
            first_name=name_parts[0] if name_parts else "",
            last_name=" ".join(name_parts[1:]) if len(name_parts) > 1 else "",
            email=user.get("email", "")
        )
    return await settings_service.get_profile()

@router.put("/profile", response_model=UserProfileSchema)
async def update_user_profile(profile: UserProfileSchema, user: Optional[dict] = Depends(get_current_user_optional)):
    db = get_database()
    if user and db is not None:
        from datetime import datetime, timezone
        uid = user["_id"]
        await db.settings.update_one(
            {"user_id": uid},
            {"$set": {
                "user_id": uid,
                "profile": profile.model_dump(),
                "updated_at": datetime.now(timezone.utc)
            }},
            upsert=True
        )
        return profile
    return await settings_service.update_profile(profile)

@router.get("/settings")
async def get_app_settings(user: Optional[dict] = Depends(get_current_user_optional)):
    app_cfg = await settings_service.get_settings()
    db = get_database()
    profile_dict = {}

    if user and db is not None:
        uid = user["_id"]
        user_sett = await db.settings.find_one({"user_id": {"$in": [uid, str(uid)]}})
        if user_sett and user_sett.get("profile"):
            profile_dict = dict(user_sett["profile"])

    if not profile_dict:
        if user:
            name_parts = (user.get("name") or "").split()
            profile_dict = {
                "first_name": name_parts[0] if name_parts else "",
                "last_name": " ".join(name_parts[1:]) if len(name_parts) > 1 else "",
                "email": user.get("email", ""),
                "phone": user.get("phone", ""),
                "city": "",
                "state": "",
                "zip_code": "",
                "linkedin_url": "",
                "github_url": "",
                "portfolio_url": "",
                "years_of_experience": "",
                "work_authorization": "US Citizen",
                "willing_to_relocate": False,
                "custom_answers": {}
            }
        else:
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

@router.put("/settings")
async def update_app_settings(body: Dict[str, Any] = Body(...), user: Optional[dict] = Depends(get_current_user_optional)):
    db = get_database()
    if user and db is not None:
        uid = user["_id"]
        await db.settings.update_one(
            {"user_id": uid},
            {"$set": {
                "user_id": uid,
                "profile": body.get("profile", {}),
                "headless": bool(body.get("headless", False)),
                "updated_at": datetime.now(timezone.utc)
            }},
            upsert=True
        )

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
    return await get_app_settings(user)


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
async def disconnect_dice(user: Optional[dict] = Depends(get_current_user_optional)):
    """Disconnects the local Dice session, removes local cookies, and resets state."""
    user_id = str(user["_id"]) if user else "default"
    return await settings_service.disconnect_dice(user_id=user_id)

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
    if not user:
        return {
            "jobs": 0, "resumes": 0, "applications": {}, "connected_boards": 0,
            "resumes_count": 0, "profiles_count": 0, "jobs_count": 0, "jobs_matched": 0,
            "apps_count": 0, "apps_ready": 0, "apps_applied": 0, "review_count": 0,
            "recent_applications": []
        }

    uid = user["_id"]
    from bson import ObjectId
    u_oids = [ObjectId(uid)] if ObjectId.is_valid(uid) else []
    u_match = {"user_id": {"$in": u_oids + [str(uid)]}}

    resumes_count = await db.resumes.count_documents(u_match)
    profiles_count = await db.search_profiles.count_documents(u_match)
    jobs_count = await db.jobs.count_documents(u_match)
    jobs_matched = await db.jobs.count_documents({**u_match, "status": "MATCHED"})
    apps_count = await db.applications.count_documents(u_match)
    apps_ready = await db.applications.count_documents({**u_match, "status": "READY"})
    apps_applied = await db.applications.count_documents({**u_match, "status": "APPLIED"})

    # Applications belonging to user
    user_app_ids = await db.applications.distinct("_id", u_match)
    review_count = 0
    if user_app_ids:
        review_count = await db.application_answers.count_documents({"application_id": {"$in": user_app_ids}, "is_answered": False})

    # Group applications by status for bench-sales-frontend
    apps_by_status = {}
    async for d in db.applications.aggregate([{"$match": u_match}, {"$group": {"_id": "$status", "n": {"$sum": 1}}}]):
        if d.get("_id"):
            apps_by_status[d["_id"]] = d.get("n", 0)

    # Check board connection status strictly for user
    conn_count = await db.board_connections.count_documents({
        "user_id": {"$in": u_oids + [str(uid)]},
        "$or": [{"is_connected": True}, {"status": "CONNECTED"}]
    })
    if conn_count == 0:
        from app.services.session_store import session_store
        sess = await session_store.get_session(str(uid))
        if not sess and user.get("email"):
            sess = await session_store.get_session(user["email"].strip().lower())
        is_conn = bool(sess and (getattr(sess, "is_connected", False) or str(getattr(sess, "status", "")).upper() in ("CONNECTED", "VALID")))
        conn_count = 1 if is_conn else 0

    # Get 5 recent applications
    recent_apps = []
    cursor = db.applications.find(u_match).sort("updated_at", -1).limit(5)
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

