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
from app.config import settings
from app.database import connect_db, close_db, get_database
from app.api import resumes, search, jobs, applications, review, settings as settings_api, dice_session, auth, boards, avatar as avatar_api, automations, billing


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup validation for production security
    if settings.is_prod:
        known_defaults = {
            "dice-auto-apply-default-jwt-secret-key-32bytes-min",
            "secret", "change-me", "changeme", "default", "your-secret-key", "jwt-secret"
        }
        if not settings.JWT_SECRET or settings.JWT_SECRET in known_defaults or len(settings.JWT_SECRET.strip()) < 32:
            msg = "CRITICAL: JWT_SECRET must be configured with a secure, random secret (min 32 characters) in production."
            print(f"[Dice-Automation] STARTUP HALTED: {msg}", file=sys.stderr, flush=True)
            raise RuntimeError(msg)

        if not settings.SESSION_ENCRYPTION_KEY or not settings.SESSION_ENCRYPTION_KEY.strip():
            msg = "CRITICAL: SESSION_ENCRYPTION_KEY must be configured with a valid encryption key in production."
            print(f"[Dice-Automation] STARTUP HALTED: {msg}", file=sys.stderr, flush=True)
            raise RuntimeError(msg)

        from app.services.session_store import SessionEncryption
        try:
            SessionEncryption.derive_key(settings.SESSION_ENCRYPTION_KEY)
        except Exception as e:
            msg = f"CRITICAL: SESSION_ENCRYPTION_KEY is invalid in production: {e}"
            print(f"[Dice-Automation] STARTUP HALTED: {msg}", file=sys.stderr, flush=True)
            raise RuntimeError(msg)

        cors_env = (settings.CORS_ORIGINS or "").strip()
        if not cors_env or cors_env == "*":
            msg = "CRITICAL: In production, CORS_ORIGINS must be set to explicit trusted origins and cannot be '*' or empty when credentials are enabled."
            print(f"[Dice-Automation] STARTUP HALTED: {msg}", file=sys.stderr, flush=True)
            raise RuntimeError(msg)

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

    # Startup: Mark automation runs abandoned by a previous process as interrupted
    try:
        from app.services.automation_service import automation_service
        await automation_service.recover_on_startup()
    except Exception as e:
        print(f"[Dice-Automation] Notice during automation recovery: {e}")

    yield

    # Shutdown: End running automations cleanly (records them as interrupted)
    try:
        from app.services.automation_service import automation_service
        await automation_service.shutdown()
    except Exception as e:
        print(f"[Dice-Automation] Error stopping automations: {e}")

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

# CORS configuration: strict allowlist
cors_origins_env = (settings.CORS_ORIGINS or os.environ.get("CORS_ORIGINS", "")).strip()
chrome_ext_id = (os.environ.get("CHROME_EXTENSION_ID") or "").strip()

if settings.is_prod:
    # In production, require explicit configured origins only (never arbitrary regex or wildcards)
    explicit_origins = [o.strip() for o in cors_origins_env.split(",") if o.strip() and o.strip() != "*"]
    if chrome_ext_id:
        explicit_origins.append(f"chrome-extension://{chrome_ext_id}")
    cors_kwargs = {
        "allow_origins": explicit_origins,
        "allow_methods": ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        "allow_headers": ["Authorization", "Content-Type", "Accept", "X-Refresh-Token", "X-Timezone"],
        "allow_credentials": True,
    }
else:
    # Development: allow approved local development origins and configured origins
    dev_origins = [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    ]
    for o in [x.strip() for x in cors_origins_env.split(",") if x.strip() and x.strip() != "*"]:
        if o not in dev_origins:
            dev_origins.append(o)
    if chrome_ext_id:
        dev_origins.append(f"chrome-extension://{chrome_ext_id}")
    cors_kwargs = {
        "allow_origins": dev_origins,
        "allow_methods": ["*"],
        "allow_headers": ["*"],
        "allow_credentials": True,
    }

app.add_middleware(CORSMiddleware, **cors_kwargs)

# Routers list
routers = [
    resumes.router,
    search.router,
    search.search_router,
    jobs.router,
    applications.router,
    review.router,
    settings_api.router,
    dice_session.router,
    auth.router,
    boards.router,
    avatar_api.router,
    automations.router,
    automations.runs_router,
    billing.router,
]

# Include Routers at root (available directly at http://localhost:8000/)
for r in routers:
    app.include_router(r)

# Support /api prefix
for r in routers:
    app.include_router(r, prefix="/api", include_in_schema=False)

# Support /api/v1 prefix (used by default bench-sales client baseQuery)
for r in routers:
    app.include_router(r, prefix="/api/v1", include_in_schema=False)


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
@app.get("/api/v1/health", include_in_schema=False)
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

