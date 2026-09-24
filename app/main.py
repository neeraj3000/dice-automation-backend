import sys
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
    yield
    # Shutdown
    await close_db()
    print("[Dice-Automation] Disconnected from MongoDB.")

app = FastAPI(
    title="Dice Job Application Automation API",
    description="Automate job search, resume matching, and application preparation on Dice.com",
    version="1.0.0",
    lifespan=lifespan
)

# CORS configuration
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:3000",
        "http://127.0.0.1:3000"
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include Routers
app.include_router(resumes.router, prefix="/api")
app.include_router(search.router, prefix="/api")
app.include_router(jobs.router, prefix="/api")
app.include_router(applications.router, prefix="/api")
app.include_router(review.router, prefix="/api")
app.include_router(settings_api.router, prefix="/api")

@app.get("/api/health")
async def health_check():
    db = get_database()
    db_status = "connected" if db is not None else "disconnected"
    return {
        "status": "healthy",
        "database": db_status,
        "mode": "Full MVP Engine"
    }

if __name__ == "__main__":
    import uvicorn
    loop_param = "asyncio:ProactorEventLoop" if sys.platform == "win32" else "auto"
    uvicorn.run("app.main:app", host="127.0.0.1", port=8000, reload=True, loop=loop_param)

