import sys
import os
import asyncio
from contextlib import asynccontextmanager

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.database import connect_db, close_db, get_database
from app.api import resumes, search, jobs, applications, review, settings as settings_api


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: Connect to MongoDB and set up indexes
    try:
        await connect_db()
        print("[Dice-Automation] Connected to MongoDB.")
    except Exception as e:
        print(f"[Dice-Automation] Error connecting to MongoDB: {e}")

    # Startup: Verify Dice session and display current status
    try:
        from app.services.settings_service import settings_service
        await settings_service.verify_session_on_startup()
    except Exception as e:
        print(f"[Dice-Automation] Notice: Could not verify Dice session on startup: {e}")

    yield

    # Shutdown: Close browser context, cancel active login supervisors, and close MongoDB
    try:
        from app.services.dice_session_manager import dice_session_manager
        await dice_session_manager.cleanup()
    except Exception as e:
        print(f"[Dice-Automation] Error cleaning up session manager: {e}")

    try:
        from app.browser.playwright_manager import playwright_manager
        await playwright_manager.close()
        print("[Dice-Automation] Browser context closed.")
    except Exception as e:
        print(f"[Dice-Automation] Error closing browser context: {e}")

    await close_db()
    print("[Dice-Automation] Disconnected from MongoDB.")

app = FastAPI(
    title="Dice Job Application Automation API",
    description="Automate job search, resume matching, and application preparation on Dice.com",
    version="1.0.0",
    lifespan=lifespan
)

# CORS configuration
cors_origins_env = os.environ.get("CORS_ORIGINS", "")
allowed_origins = [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://localhost:3000",
    "http://127.0.0.1:3000"
]
if cors_origins_env and cors_origins_env != "*":
    for origin in cors_origins_env.split(","):
        if origin.strip():
            allowed_origins.append(origin.strip())

cors_kwargs = {
    "allow_methods": ["*"],
    "allow_headers": ["*"],
    "allow_credentials": True,
}
if cors_origins_env == "*" or not cors_origins_env:
    # Allow all HTTP/HTTPS origins safely with regex while preserving credentials
    cors_kwargs["allow_origin_regex"] = r"^https?://.*"
else:
    cors_kwargs["allow_origins"] = allowed_origins

app.add_middleware(CORSMiddleware, **cors_kwargs)

# Include Routers (available directly at root http://localhost:8000)
app.include_router(resumes.router)
app.include_router(search.router)
app.include_router(jobs.router)
app.include_router(applications.router)
app.include_router(review.router)
app.include_router(settings_api.router)

# Also support /api prefix for backwards compatibility
app.include_router(resumes.router, prefix="/api", include_in_schema=False)
app.include_router(search.router, prefix="/api", include_in_schema=False)
app.include_router(jobs.router, prefix="/api", include_in_schema=False)
app.include_router(applications.router, prefix="/api", include_in_schema=False)
app.include_router(review.router, prefix="/api", include_in_schema=False)
app.include_router(settings_api.router, prefix="/api", include_in_schema=False)

@app.get("/")
async def root():
    from app.services.settings_service import settings_service
    dice_status = await settings_service.get_dice_status(check_live=False)
    return {
        "status": "healthy",
        "message": "Dice Job Application Automation API is running",
        "dice_session_connected": dice_status.get("is_connected", False),
        "dice_username": dice_status.get("username", ""),
        "docs": "/docs",
        "health": "/health"
    }

@app.get("/health")
@app.get("/api/health", include_in_schema=False)
async def health_check():
    db = get_database()
    db_status = "connected" if db is not None else "disconnected"
    from app.services.settings_service import settings_service
    dice_status = await settings_service.get_dice_status(check_live=False)
    is_connected = dice_status.get("is_connected", False)
    return {
        "status": "healthy",
        "database": db_status,
        "dice_session": {
            "is_connected": is_connected,
            "status": "Connected" if is_connected else "Disconnected",
            "username": dice_status.get("username", ""),
            "cookies_count": dice_status.get("cookies_count", 0),
            "last_verified": dice_status.get("last_verified")
        },
        "mode": "Full MVP Engine"
    }

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    loop_param = "asyncio:ProactorEventLoop" if sys.platform == "win32" else "auto"
    uvicorn.run("app.main:app", host="0.0.0.0", port=port, reload=False, loop=loop_param)

