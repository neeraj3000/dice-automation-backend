import asyncio
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from bson import ObjectId
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.database import connect_db, close_db, get_database
from app.services.resume_service import resume_service
from app.services.application_queue import application_queue_manager, ApplicationQueueManager
from app.schemas.application import ApplicationState

@pytest.fixture
def anyio_backend():
    return "asyncio"

@pytest.fixture
async def test_db():
    await connect_db()
    db = get_database()
    assert "test" in db.name, f"Safety check: refusing to run against {db.name}"
    yield db
    await close_db()


@pytest.mark.anyio
async def test_fix1_default_resume_authoritative_pointer(test_db):
    """
    Verify:
    1. Setting default updates users.default_resume_id atomically.
    2. Denormalized is_default on resumes collection is synchronized.
    3. Read paths format is_default from authoritative user pointer.
    """
    db = test_db
    user_id = str(ObjectId())
    await db.users.insert_one({"_id": ObjectId(user_id), "email": f"test_{user_id}@example.com"})

    # Create two resumes for this user
    r1 = await db.resumes.insert_one({
        "user_id": user_id,
        "file_name": "resume1.pdf",
        "display_name": "Resume 1",
        "is_default": False,
        "created_at": datetime.now(timezone.utc)
    })
    r2 = await db.resumes.insert_one({
        "user_id": user_id,
        "file_name": "resume2.pdf",
        "display_name": "Resume 2",
        "is_default": False,
        "created_at": datetime.now(timezone.utc)
    })

    r1_id = str(r1.inserted_id)
    r2_id = str(r2.inserted_id)

    # Set r1 as default
    res1 = await resume_service.set_default_resume(r1_id, user_id=user_id)
    assert res1 is not None
    assert res1.is_default is True

    user_doc = await db.users.find_one({"_id": ObjectId(user_id)})
    assert user_doc.get("default_resume_id") == r1_id

    # Switch to r2 as default
    res2 = await resume_service.set_default_resume(r2_id, user_id=user_id)
    assert res2 is not None
    assert res2.is_default is True

    user_doc = await db.users.find_one({"_id": ObjectId(user_id)})
    assert user_doc.get("default_resume_id") == r2_id

    # Verify r1 is no longer default in DB and in read paths
    resumes = await resume_service.get_resumes(user_id=user_id)
    defaults = [r for r in resumes if r.is_default]
    assert len(defaults) == 1
    assert defaults[0].id == r2_id

    # Clean up
    await db.users.delete_one({"_id": ObjectId(user_id)})
    await db.resumes.delete_many({"user_id": user_id})


@pytest.mark.anyio
async def test_fix1_cross_user_selection_rejected(test_db):
    """
    Verify cross-user default resume selection is strictly forbidden.
    """
    db = test_db
    user_a = str(ObjectId())
    user_b = str(ObjectId())

    await db.users.insert_one({"_id": ObjectId(user_a), "email": f"a_{user_a}@example.com"})
    await db.users.insert_one({"_id": ObjectId(user_b), "email": f"b_{user_b}@example.com"})

    # Resume belongs to user B
    r_b = await db.resumes.insert_one({
        "user_id": user_b,
        "file_name": "resume_b.pdf",
        "display_name": "Resume B",
        "is_default": False,
        "created_at": datetime.now(timezone.utc)
    })
    rb_id = str(r_b.inserted_id)

    # User A tries to set user B's resume as their default
    result = await resume_service.set_default_resume(rb_id, user_id=user_a)
    assert result is None, "Cross-user default selection must return None"

    # Verify user A's document was NOT modified
    doc_a = await db.users.find_one({"_id": ObjectId(user_a)})
    assert doc_a.get("default_resume_id") is None

    # Clean up
    await db.users.delete_many({"_id": {"$in": [ObjectId(user_a), ObjectId(user_b)]}})
    await db.resumes.delete_one({"_id": ObjectId(rb_id)})


@pytest.mark.anyio
async def test_fix1_concurrent_default_selections(test_db):
    """
    Simulate high-concurrency race condition: 10 concurrent requests
    setting defaults across multiple resumes for the same user.
    Verify: exactly 1 authoritative pointer remains, pointing to a valid owned resume.
    """
    db = test_db
    user_id = str(ObjectId())
    await db.users.insert_one({"_id": ObjectId(user_id), "email": f"conc_{user_id}@example.com"})

    resume_ids = []
    for i in range(5):
        ins = await db.resumes.insert_one({
            "user_id": user_id,
            "file_name": f"conc_{i}.pdf",
            "display_name": f"Resume {i}",
            "is_default": False,
            "created_at": datetime.now(timezone.utc)
        })
        resume_ids.append(str(ins.inserted_id))

    # Run 10 concurrent selections alternating across the resumes
    tasks = [
        resume_service.set_default_resume(resume_ids[i % len(resume_ids)], user_id=user_id)
        for i in range(10)
    ]
    results = await asyncio.gather(*tasks)

    # All should return a valid ResumeResponse
    assert all(r is not None for r in results)

    # Check database state
    user_doc = await db.users.find_one({"_id": ObjectId(user_id)})
    final_default_id = user_doc.get("default_resume_id")
    assert final_default_id in resume_ids, "Final pointer must be one of user's resumes"

    # Verify read path
    resumes = await resume_service.get_resumes(user_id=user_id)
    defaults = [r for r in resumes if r.is_default]
    assert len(defaults) == 1, "Exactly one resume must be marked default"
    assert defaults[0].id == final_default_id

    # Clean up
    await db.users.delete_one({"_id": ObjectId(user_id)})
    await db.resumes.delete_many({"user_id": user_id})


@pytest.mark.anyio
async def test_fix1_deletion_pointer_cleanup_and_fallback(test_db):
    """
    Verify:
    1. When default resume is deleted, user pointer falls back to newest remaining resume.
    2. When the last remaining resume is deleted, default_resume_id is unset.
    """
    db = test_db
    user_id = str(ObjectId())
    await db.users.insert_one({"_id": ObjectId(user_id), "email": f"del_{user_id}@example.com"})

    r1 = await db.resumes.insert_one({
        "user_id": user_id,
        "file_name": "r1.pdf",
        "created_at": datetime.now(timezone.utc) - timedelta(minutes=10)
    })
    r2 = await db.resumes.insert_one({
        "user_id": user_id,
        "file_name": "r2.pdf",
        "created_at": datetime.now(timezone.utc)
    })

    r1_id = str(r1.inserted_id)
    r2_id = str(r2.inserted_id)

    # Set r2 as default
    await resume_service.set_default_resume(r2_id, user_id=user_id)

    # Delete r2
    del_ok = await resume_service.delete_resume(r2_id, user_id=user_id)
    assert del_ok is True

    # User doc should now fallback to r1_id
    user_doc = await db.users.find_one({"_id": ObjectId(user_id)})
    assert user_doc.get("default_resume_id") == r1_id

    # Now delete r1
    del_ok2 = await resume_service.delete_resume(r1_id, user_id=user_id)
    assert del_ok2 is True

    # User doc should have default_resume_id removed
    user_doc = await db.users.find_one({"_id": ObjectId(user_id)})
    assert "default_resume_id" not in user_doc or user_doc.get("default_resume_id") is None

    # Clean up
    await db.users.delete_one({"_id": ObjectId(user_id)})


@pytest.mark.anyio
async def test_fix2_stale_in_flight_pre_submission_recovery(test_db):
    """
    Verify:
    1. In-flight pre-submission records (STARTING, OPENING_JOB, FILLING_APPLICATION, UPLOADING_RESUME)
       with expired heartbeats are recovered to FAILED (is_retryable=True).
    2. Failure reason clearly explains recovery without sensitive leaks.
    """
    db = test_db
    stale_time = datetime.now(timezone.utc) - timedelta(seconds=300)

    # Create stale in-flight records
    inserted_ids = []
    for st in [
        ApplicationState.STARTING,
        ApplicationState.OPENING_JOB,
        ApplicationState.FILLING_APPLICATION,
        ApplicationState.UPLOADING_RESUME,
    ]:
        ins = await db.applications.insert_one({
            "job_id": ObjectId(),
            "user_id": "test_user",
            "status": st,
            "heartbeat_at": stale_time,
            "created_at": stale_time,
            "updated_at": stale_time
        })
        inserted_ids.append(ins.inserted_id)

    # Run recovery
    result = await application_queue_manager.recover_pending_on_startup()
    assert result.get("recovered_pre_submission", 0) >= 4

    # Verify records in DB
    for oid in inserted_ids:
        doc = await db.applications.find_one({"_id": oid})
        assert doc["status"] == ApplicationState.FAILED
        assert doc.get("is_retryable") is True
        assert "heartbeat lost" in doc.get("failure_reason", "")
        assert "recovered_at" in doc
        assert "recovered_by" in doc

    # Clean up
    await db.applications.delete_many({"_id": {"$in": inserted_ids}})


@pytest.mark.anyio
async def test_fix2_interrupted_submitting_ambiguous_outcome(test_db):
    """
    Verify:
    1. SUBMITTING record with expired heartbeat is marked FAILED with ambiguous_submission=True.
    2. is_retryable is False to prevent duplicate external submissions.
    """
    db = test_db
    stale_time = datetime.now(timezone.utc) - timedelta(seconds=400)

    ins = await db.applications.insert_one({
        "job_id": ObjectId(),
        "user_id": "test_user",
        "status": ApplicationState.SUBMITTING,
        "heartbeat_at": stale_time,
        "created_at": stale_time,
        "updated_at": stale_time
    })
    app_oid = ins.inserted_id

    # Run recovery
    result = await application_queue_manager.recover_pending_on_startup()
    assert result.get("recovered_ambiguous_submitting", 0) >= 1

    doc = await db.applications.find_one({"_id": app_oid})
    assert doc["status"] == ApplicationState.FAILED
    assert doc.get("ambiguous_submission") is True
    assert doc.get("is_retryable") is False
    assert "manual verification required" in doc.get("failure_reason", "")

    # Clean up
    await db.applications.delete_one({"_id": app_oid})


@pytest.mark.anyio
async def test_fix2_active_worker_protection(test_db):
    """
    Verify:
    An in-flight application whose worker heartbeat is FRESH (< 180s old)
    is NOT touched by recovery.
    """
    db = test_db
    fresh_time = datetime.now(timezone.utc) - timedelta(seconds=10)

    ins = await db.applications.insert_one({
        "job_id": ObjectId(),
        "user_id": "test_user",
        "status": ApplicationState.FILLING_APPLICATION,
        "heartbeat_at": fresh_time,
        "created_at": fresh_time,
        "updated_at": fresh_time
    })
    app_oid = ins.inserted_id

    await application_queue_manager.recover_pending_on_startup()

    doc = await db.applications.find_one({"_id": app_oid})
    assert doc["status"] == ApplicationState.FILLING_APPLICATION, "Active worker record must not be disrupted"

    # Clean up
    await db.applications.delete_one({"_id": app_oid})


@pytest.mark.anyio
async def test_fix2_multi_worker_atomic_claiming(test_db):
    """
    Verify:
    When two workers attempt to claim the same QUEUED application simultaneously,
    exactly one worker succeeds and claims the lease.
    """
    db = test_db
    now = datetime.now(timezone.utc)
    ins = await db.applications.insert_one({
        "job_id": ObjectId(),
        "user_id": "test_user",
        "status": ApplicationState.QUEUED,
        "created_at": now,
        "updated_at": now
    })
    app_oid = ins.inserted_id

    w1 = ApplicationQueueManager()
    w2 = ApplicationQueueManager()

    async def attempt_claim(worker):
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=worker.lease_timeout_seconds)
        claim_filter = {
            "_id": app_oid,
            "status": {"$in": [ApplicationState.QUEUED, ApplicationState.STARTING]},
            "cancel_requested": {"$ne": True},
            "$or": [
                {"claimed_by": {"$exists": False}},
                {"claimed_by": worker._worker_instance_id},
                {"heartbeat_at": {"$exists": False}},
                {"heartbeat_at": {"$lt": cutoff}}
            ]
        }
        claim_update = {
            "$set": {
                "status": ApplicationState.STARTING,
                "claimed_by": worker._worker_instance_id,
                "claimed_at": datetime.now(timezone.utc),
                "heartbeat_at": datetime.now(timezone.utc),
                "updated_at": datetime.now(timezone.utc)
            }
        }
        return await db.applications.find_one_and_update(claim_filter, claim_update, return_document=True)

    r1, r2 = await asyncio.gather(attempt_claim(w1), attempt_claim(w2))

    successes = [r for r in [r1, r2] if r is not None]
    assert len(successes) == 1, "Exactly one worker must successfully claim the lease"

    # Clean up
    await db.applications.delete_one({"_id": app_oid})


@pytest.mark.anyio
async def test_fix3_index_definitions(test_db):
    """
    Verify required performance indexes are defined and active:
    1. search_profiles: (user_id, created_at DESC)
    2. job_matches: (job_id)
    3. jobs: (user_id, external_job_id) is retained
    """
    db = test_db

    # Check search_profiles index
    sp_indexes = await db.search_profiles.index_information()
    has_sp_idx = any(
        idx.get("key") == [("user_id", 1), ("created_at", -1)]
        for idx in sp_indexes.values()
    )
    assert has_sp_idx, f"search_profiles index (user_id, created_at -1) missing: {sp_indexes}"

    # Check job_matches index
    jm_indexes = await db.job_matches.index_information()
    has_jm_idx = any(
        idx.get("key") == [("job_id", 1)]
        for idx in jm_indexes.values()
    )
    assert has_jm_idx, f"job_matches index (job_id 1) missing: {jm_indexes}"

    # Check jobs retention
    jobs_indexes = await db.jobs.index_information()
    has_jobs_idx = any(
        idx.get("key") == [("user_id", 1), ("external_job_id", 1)]
        for idx in jobs_indexes.values()
    )
    assert has_jobs_idx, f"jobs index (user_id 1, external_job_id 1) must be retained: {jobs_indexes}"


@pytest.mark.anyio
async def test_fix4_mixed_bson_ownership_compatibility(test_db):
    """
    Verify:
    Queries safely resolve records regardless of whether user_id is stored
    as a BSON ObjectId or a BSON string.
    """
    db = test_db
    oid_uid = ObjectId()
    str_uid = str(oid_uid)

    await db.users.insert_one({"_id": oid_uid, "email": f"bson_{str_uid}@example.com"})

    # Insert one resume with str user_id and one with ObjectId user_id
    r_str = await db.resumes.insert_one({
        "user_id": str_uid,
        "file_name": "str_owner.pdf",
        "created_at": datetime.now(timezone.utc)
    })
    r_oid = await db.resumes.insert_one({
        "user_id": oid_uid,
        "file_name": "oid_owner.pdf",
        "created_at": datetime.now(timezone.utc)
    })

    # Query using str_uid via resume_service
    resumes = await resume_service.get_resumes(user_id=str_uid)
    res_ids = {r.id for r in resumes}
    assert str(r_str.inserted_id) in res_ids
    assert str(r_oid.inserted_id) in res_ids

    # Query using oid_uid
    resumes_oid = await resume_service.get_resumes(user_id=oid_uid)
    res_ids_oid = {r.id for r in resumes_oid}
    assert str(r_str.inserted_id) in res_ids_oid
    assert str(r_oid.inserted_id) in res_ids_oid

    # Clean up
    await db.users.delete_one({"_id": oid_uid})
    await db.resumes.delete_many({"_id": {"$in": [r_str.inserted_id, r_oid.inserted_id]}})


@pytest.mark.anyio
async def test_fix4_legacy_default_records_isolation(test_db):
    """
    Verify:
    Records owned by 'default' are strictly inaccessible to normal tenant users.
    """
    db = test_db
    tenant_id = str(ObjectId())

    # Insert legacy default resume
    r_def = await db.resumes.insert_one({
        "user_id": "default",
        "file_name": "legacy_system.pdf",
        "created_at": datetime.now(timezone.utc)
    })

    # Tenant user queries their resumes
    resumes = await resume_service.get_resumes(user_id=tenant_id)
    assert not any(r.id == str(r_def.inserted_id) for r in resumes), "Tenant must not see legacy default resume"

    # Tenant tries to get legacy resume by id
    res = await resume_service.get_resume_by_id(str(r_def.inserted_id), user_id=tenant_id)
    assert res is None, "Tenant must not be able to read legacy default resume by ID"

    # Clean up
    await db.resumes.delete_one({"_id": r_def.inserted_id})
