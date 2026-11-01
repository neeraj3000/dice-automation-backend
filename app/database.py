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
    
    # Create essential indexes safely
    try:
        resumes_col = db_instance.db.resumes
        await resumes_col.create_index([("target_role", 1)])
        await resumes_col.create_index([("created_at", -1)])
        await resumes_col.create_index([("file_name", 1)])
    except Exception:
        pass

    try:
        jobs_col = db_instance.db.jobs
        await jobs_col.create_index([("user_id", 1), ("external_job_id", 1)])
        await jobs_col.create_index([("status", 1)])
    except Exception:
        pass

    try:
        apps_col = db_instance.db.applications
        await apps_col.create_index([("status", 1)])
    except Exception:
        pass

    # application_answers holds pending-question docs (application_id, no user_id/key).
    # A legacy unique index on (user_id, key) made every such doc collide on {null, null},
    # so restrict it to docs that actually carry a key.
    try:
        answers_col = db_instance.db.application_answers
        indexes = await answers_col.index_information()
        legacy = indexes.get("user_id_1_key_1")
        if legacy and legacy.get("unique") and not legacy.get("partialFilterExpression"):
            await answers_col.drop_index("user_id_1_key_1")
        await answers_col.create_index(
            [("user_id", 1), ("key", 1)],
            name="user_id_1_key_1",
            unique=True,
            partialFilterExpression={"key": {"$exists": True}},
        )
        await answers_col.create_index([("application_id", 1), ("is_answered", 1)])
    except Exception as e:
        print(f"[DB] Failed to ensure application_answers indexes: {e}")

    # Multi-tenant Auth indexes
    try:
        users_col = db_instance.db.users
        await users_col.create_index([("email", 1)], unique=True)

        refresh_col = db_instance.db.refresh_tokens
        await refresh_col.create_index([("jti_hash", 1)], unique=True)
        await refresh_col.create_index([("user_id", 1)])
        await refresh_col.create_index([("expires_at", 1)], expireAfterSeconds=0)
    except Exception:
        pass

    # Search profiles & Job matches performance indexes
    try:
        search_col = db_instance.db.search_profiles
        await search_col.create_index([("user_id", 1), ("created_at", -1)])
    except Exception:
        pass

    try:
        matches_col = db_instance.db.job_matches
        await matches_col.create_index([("job_id", 1)])
    except Exception:
        pass

    # Timed automations: at most one RUNNING run per user, enforced by the unique partial index
    try:
        await db_instance.db.automations.create_index([("user_id", 1), ("created_at", -1)])
        runs_col = db_instance.db.automation_runs
        await runs_col.create_index([("automation_id", 1), ("started_at", -1)])
        await runs_col.create_index([("status", 1), ("heartbeat_at", 1)])
        await runs_col.create_index(
            [("user_id", 1)],
            name="one_running_automation_per_user",
            unique=True,
            partialFilterExpression={"status": "RUNNING"},
        )
        await db_instance.db.applications.create_index([("automation_run_id", 1)])
    except Exception as e:
        print(f"[DB] Failed to ensure automation indexes: {e}")

    # Billing: one account per user, webhook idempotency, and per-user usage counts
    try:
        from app.services.billing import billing_repository
        await billing_repository.ensure_indexes()
        await db_instance.db.applications.create_index([("user_id", 1), ("status", 1), ("applied_at", -1)])
    except Exception as e:
        print(f"[DB] Failed to ensure billing indexes: {e}")

async def ensure_indexes():
    """Create production-critical indexes. Safe to call at startup (idempotent)."""
    db = get_database()
    import asyncio
    await asyncio.gather(
        db.applications.create_index([("user_id", 1), ("created_at", -1)], name="app_user_created"),
        db.applications.create_index([("user_id", 1), ("status", 1)], name="app_user_status"),
        db.application_answers.create_index(
            [("application_id", 1), ("is_answered", 1)], name="ans_app_answered"
        ),
    )

async def close_db():
    if db_instance.client:
        db_instance.client.close()

def utcnow():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc)

def sanitize_object_ids(val):
    from bson import ObjectId
    if isinstance(val, ObjectId):
        return str(val)
    if isinstance(val, dict):
        return {k: sanitize_object_ids(v) for k, v in val.items()}
    if isinstance(val, list):
        return [sanitize_object_ids(v) for v in val]
    return val

