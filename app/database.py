import certifi
from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase
from app.config import settings

def _create_client() -> AsyncIOMotorClient:
    url = settings.MONGODB_URL
    kwargs = {}
    if "mongodb.net" in url or "ssl=true" in url.lower() or "tls=true" in url.lower() or url.startswith("mongodb+srv://"):
        kwargs["tlsCAFile"] = certifi.where()
    return AsyncIOMotorClient(url, **kwargs)

class Database:
    client: AsyncIOMotorClient = None
    db: AsyncIOMotorDatabase = None

db_instance = Database()

def get_database() -> AsyncIOMotorDatabase:
    if db_instance.db is None:
        db_instance.client = _create_client()
        db_instance.db = db_instance.client[settings.DATABASE_NAME]
    return db_instance.db

async def connect_db():
    db_instance.client = _create_client()
    db_instance.db = db_instance.client[settings.DATABASE_NAME]
    
    # Create essential indexes
    resumes_col = db_instance.db.resumes
    await resumes_col.create_index([("target_role", 1)])
    await resumes_col.create_index([("created_at", -1)])
    await resumes_col.create_index([("file_name", 1)])

    jobs_col = db_instance.db.jobs
    await jobs_col.create_index([("external_job_id", 1)], unique=True)
    await jobs_col.create_index([("status", 1)])

    apps_col = db_instance.db.applications
    await apps_col.create_index([("status", 1)])

async def close_db():
    if db_instance.client:
        db_instance.client.close()
