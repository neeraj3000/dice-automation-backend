import sys
import os
from pathlib import Path
import asyncio
from contextlib import asynccontextmanager

# Ensure virtual environment site-packages is in sys.path even when spawned via Windows multiprocessing
backend_dir = Path(__file__).resolve().parent.parent
venv_site_packages = backend_dir / ".venv" / "Lib" / "site-packages"
if venv_site_packages.exists() and str(venv_site_packages) not in sys.path:
    sys.path.insert(0, str(venv_site_packages))

if sys.platform == "win32":
    try:
        current_policy = asyncio.get_event_loop_policy()
        if not isinstance(current_policy, asyncio.WindowsProactorEventLoopPolicy):
            asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    except Exception:
        pass

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.database import connect_db, close_db, get_database
from app.api import resumes, search, jobs, applications, review, settings as settings_api, dice_session


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: Connect to MongoDB and set up indexes
    try:
        await connect_db()
        print("[Dice-Automation] Connected to MongoDB.")
        # One-time migration: Purge legacy shared session cookies from MongoDB
        db = get_database()
        if db is not None:
            await db.app_settings.update_many(
                {},
                {"$unset": {
                    "saved_cookies": "",
                    "dice_session_connected": "",
                    "dice_username": "",
                    "dice_cookies_count": "",
                    "dice_last_verified": ""
                }}
            )
            print("[Dice-Automation] Purged legacy shared session cookies from MongoDB.")
    except Exception as e:
        print(f"[Dice-Automation] Error connecting to MongoDB: {e}")

    # Startup: Verify Dice session and display current status
    try:
        from app.services.settings_service import settings_service
        await settings_service.verify_session_on_startup()
    except Exception as e:
        print(f"[Dice-Automation] Notice: Could not verify Dice session on startup: {e}")

    # Startup: Verify Playwright browser availability without downloading at runtime
    try:
        from app.browser.playwright_manager import playwright_manager
        is_ready = await playwright_manager.ensure_browser_installed()
        if is_ready:
            print("[Dice-Automation] Playwright Chromium binary verified (installed during build phase).")
        else:
            print("[Dice-Automation] Warning: Playwright Chromium binary not found. Ensure build step ran 'python -m playwright install --with-deps chromium'.")
    except Exception as e:
        print(f"[Dice-Automation] Notice during startup browser verification: {e}")

    # Startup: Start Application Execution Queue & recover pending applications
    try:
        from app.services.application_queue import application_queue_manager
        application_queue_manager.start_worker()
        await application_queue_manager.recover_pending_on_startup()
        print("[Dice-Automation] Application execution queue worker started.")
    except Exception as e:
        print(f"[Dice-Automation] Notice during application queue startup: {e}")

    yield

    # Shutdown: Stop application queue worker
    try:
        from app.services.application_queue import application_queue_manager
        await application_queue_manager.stop_worker()
        print("[Dice-Automation] Application execution queue worker stopped.")
    except Exception as e:
        print(f"[Dice-Automation] Error stopping application queue worker: {e}")

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
    "http://127.0.0.1:3000",
    "https://www.dice.com",
    "https://dice.com",
    "https://profile.dice.com",
    "https://customer.dice.com",
    "https://login.dice.com",
    "https://dashboard.dice.com",
]
if cors_origins_env and cors_origins_env != "*":
    for origin in cors_origins_env.split(","):
        if origin.strip() and origin.strip() not in allowed_origins:
            allowed_origins.append(origin.strip())

cors_kwargs = {
    "allow_methods": ["*"],
    "allow_headers": ["*"],
    "allow_credentials": True,
}
if cors_origins_env == "*" or not cors_origins_env:
    # Allow all HTTP/HTTPS origins and Chrome Extensions safely with regex while preserving credentials
    cors_kwargs["allow_origin_regex"] = r"^(https?://.*|chrome-extension://.*)"
else:
    cors_kwargs["allow_origins"] = allowed_origins
    cors_kwargs["allow_origin_regex"] = r"^((https?://([a-zA-Z0-9-]+\.)*dice\.com(:[0-9]+)?)|chrome-extension://.*)"

app.add_middleware(CORSMiddleware, **cors_kwargs)

# Include Routers (available directly at root http://localhost:8000)
app.include_router(resumes.router)
app.include_router(search.router)
app.include_router(jobs.router)
app.include_router(applications.router)
app.include_router(review.router)
app.include_router(settings_api.router)
app.include_router(dice_session.router)

# Also support /api prefix for backwards compatibility
app.include_router(resumes.router, prefix="/api", include_in_schema=False)
app.include_router(search.router, prefix="/api", include_in_schema=False)
app.include_router(jobs.router, prefix="/api", include_in_schema=False)
app.include_router(applications.router, prefix="/api", include_in_schema=False)
app.include_router(review.router, prefix="/api", include_in_schema=False)
app.include_router(settings_api.router, prefix="/api", include_in_schema=False)
app.include_router(dice_session.router, prefix="/api", include_in_schema=False)

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

