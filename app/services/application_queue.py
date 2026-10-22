import asyncio
import logging
import os
import uuid
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, List, Optional
from bson import ObjectId

from app.database import get_database
from app.schemas.application import ApplicationState
from app.services.application_runner import application_runner
from app.services.settings_service import settings_service
from app.services.matching_service import matching_service, user_resume_filter
from app.services.jd_service import jd_service
from app.services.execution_control import execution_control
from app.services.billing import entitlement_service

logger = logging.getLogger(__name__)


class ApplicationQueueManager:
    def __init__(self):
        self._queue: asyncio.Queue = asyncio.Queue()
        self._worker_task: Optional[asyncio.Task] = None
        self._active_application_id: Optional[str] = None
        self._is_running: bool = False
        self._worker_instance_id: str = f"worker_{os.getpid()}_{uuid.uuid4().hex[:8]}"
        self.lease_timeout_seconds: int = 180

    @property
    def apps_col(self):
        return get_database().applications

    @property
    def jobs_col(self):
        return get_database().jobs

    @property
    def resumes_col(self):
        return get_database().resumes

    def start_worker(self):
        """Starts the background sequential execution worker if not already running."""
        if self._worker_task is None or self._worker_task.done():
            self._is_running = True
            self._worker_task = asyncio.create_task(self._worker_loop())
            logger.info("ApplicationQueueManager worker started.")

    async def stop_worker(self):
        """Gracefully halts the background queue worker."""
        self._is_running = False
        if self._worker_task and not self._worker_task.done():
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
            logger.info("ApplicationQueueManager worker stopped.")

    async def _worker_loop(self):
        """Continuously pulls applications from the queue and executes them sequentially."""
        logger.info("ApplicationQueue worker loop entered.")
        while self._is_running:
            try:
                item = await self._queue.get()
                app_id = item["application_id"]
                user_id = item.get("user_id", "default")
                mode = item.get("mode", "APPLY")
                max_retries = item.get("max_retries", 2)

                try:
                    now = datetime.now(timezone.utc)
                    cutoff = now - timedelta(seconds=self.lease_timeout_seconds)

                    # Atomically claim the pending application
                    claim_filter = {
                        "_id": ObjectId(app_id),
                        "status": {"$in": [ApplicationState.QUEUED, ApplicationState.STARTING]},
                        "cancel_requested": {"$ne": True},
                        "$or": [
                            {"claimed_by": {"$exists": False}},
                            {"claimed_by": self._worker_instance_id},
                            {"heartbeat_at": {"$exists": False}},
                            {"heartbeat_at": {"$lt": cutoff}}
                        ]
                    }
                    claim_update = {
                        "$set": {
                            "status": ApplicationState.STARTING,
                            "claimed_by": self._worker_instance_id,
                            "claimed_at": now,
                            "heartbeat_at": now,
                            "updated_at": now
                        }
                    }
                    claimed = await self.apps_col.find_one_and_update(claim_filter, claim_update, return_document=True)
                    if not claimed:
                        logger.info(f"Skipping application {app_id}: no longer pending or actively claimed by another worker.")
                        continue

                    self._active_application_id = app_id
                    logger.info(f"Processing application from queue: {app_id} (claimed by {self._worker_instance_id})")

                    # Start periodic background heartbeat task for long running operations
                    async def _heartbeat_worker():
                        while self._active_application_id == app_id:
                            try:
                                await asyncio.sleep(20)
                                if self._active_application_id == app_id:
                                    await self.apps_col.update_one(
                                        {"_id": ObjectId(app_id), "claimed_by": self._worker_instance_id},
                                        {"$set": {"heartbeat_at": datetime.now(timezone.utc)}}
                                    )
                            except asyncio.CancelledError:
                                break
                            except Exception:
                                pass

                    hb_task = asyncio.create_task(_heartbeat_worker())
                    try:
                        await execution_control.run_cancellable(
                            app_id,
                            application_runner.run_application(
                                application_id=app_id,
                                user_id=user_id,
                                mode=mode,
                                max_retries=max_retries
                            ),
                        )
                    finally:
                        hb_task.cancel()
                        try:
                            await hb_task
                        except asyncio.CancelledError:
                            pass
                except Exception as ex:
                    logger.error(f"Error running application {app_id} in queue: {ex}", exc_info=True)
                finally:
                    self._active_application_id = None
                    self._queue.task_done()

            except asyncio.CancelledError:
                logger.info("Queue worker received cancellation.")
                break
            except Exception as e:
                logger.error(f"Unexpected error in queue worker loop: {e}", exc_info=True)
                await asyncio.sleep(1)

    async def enqueue_application(
        self,
        application_id: str,
        user_id: str = "default",
        mode: str = "APPLY",
        max_retries: int = 2,
        start_worker: bool = True
    ) -> Dict[str, Any]:
        """
        Puts an existing application into the execution queue and marks it QUEUED in MongoDB.
        """
        if not ObjectId.is_valid(application_id):
            raise ValueError(f"Invalid application ID: {application_id}")

        app_oid = ObjectId(application_id)
        now = datetime.now(timezone.utc)

        query: Dict[str, Any] = {"_id": app_oid}
        if user_id and user_id != "default":
            u_oids = [ObjectId(user_id)] if ObjectId.is_valid(user_id) else []
            query["user_id"] = {"$in": u_oids + [str(user_id)]}

        existing = await self.apps_col.find_one(query)
        if not existing:
            raise ValueError(f"Application not found or unauthorized: {application_id}")

        cur_status = existing.get("status")
        if cur_status == ApplicationState.APPLYING:
            raise ValueError(f"Application is currently in-flight (status: {cur_status})")

        # Mark QUEUED in MongoDB
        await self.apps_col.update_one(
            query,
            {"$set": {
                "status": ApplicationState.QUEUED,
                "mode": mode,
                "max_retries": max_retries,
                "retry_count": 0,
                "failure_reason": "",
                "cancel_requested": False,
                "updated_at": now
            }}
        )

        if start_worker:
            # Add to execution queue
            await self._queue.put({
                "application_id": application_id,
                "user_id": user_id,
                "mode": mode,
                "max_retries": max_retries
            })

            # Ensure worker is alive
            self.start_worker()

        logger.info(f"Application {application_id} enqueued successfully. Queue size: {self._queue.qsize()}")
        return {
            "application_id": application_id,
            "status": ApplicationState.QUEUED,
            "queue_position": self._queue.qsize()
        }

    async def enqueue_job(
        self,
        job_id: str,
        resume_id: Optional[str] = None,
        user_id: str = "default",
        mode: str = "APPLY",
        max_retries: int = 2,
        start_worker: bool = True
    ) -> Dict[str, Any]:
        """
        Creates or updates an application record for a job, selects the best resume,
        and pushes it onto the execution queue.
        """
        if not ObjectId.is_valid(job_id):
            raise ValueError(f"Invalid job ID: {job_id}")

        job = await self.jobs_col.find_one({"_id": ObjectId(job_id)})
        if not job:
            raise ValueError(f"Job not found: {job_id}")

        # Check if already applied
        if job.get("status") in ("APPLIED", ApplicationState.SUBMITTED):
            return {
                "job_id": job_id,
                "status": ApplicationState.SUBMITTED,
                "message": "Job has already been submitted."
            }

        if user_id and user_id != "default":
            await entitlement_service.assert_can_apply(user_id)

        # Resolve resume
        resolved_resume_id = resume_id
        if not resolved_resume_id:
            if job.get("match_result") and job["match_result"].get("recommended_resume_id"):
                resolved_resume_id = job["match_result"]["recommended_resume_id"]

        # Resumes may only come from the applying user's own library
        owner_filter = user_resume_filter(user_id if user_id != "default" else job.get("user_id")) or {"_id": None}
        resume = None
        if resolved_resume_id and ObjectId.is_valid(resolved_resume_id):
            resume = await self.resumes_col.find_one({"_id": ObjectId(resolved_resume_id), **owner_filter})

        if not resume:
            # Match against resume library
            jd_structured = job.get("description_structured")
            if not jd_structured:
                jd_data = await jd_service.analyze_job_description(job.get("description_raw", ""), job.get("title", ""))
                jd_structured = jd_data.model_dump()
                await self.jobs_col.update_one({"_id": job["_id"]}, {"$set": {"description_structured": jd_structured}})

            match_res = await matching_service.match_job_against_all_resumes(
                jd_data if 'jd_data' in locals() else await jd_service.analyze_job_description(job.get("description_raw", "")),
                job_id=str(job["_id"]),
                user_id=user_id if user_id != "default" else job.get("user_id"),
            )
            if match_res.recommended_resume_id and ObjectId.is_valid(match_res.recommended_resume_id):
                resume = await self.resumes_col.find_one({"_id": ObjectId(match_res.recommended_resume_id), **owner_filter})

        if not resume:
            resume = await self.resumes_col.find_one(owner_filter)
            if not resume:
                raise ValueError("No resumes found in database to apply with.")

        now = datetime.now(timezone.utc)
        resume_name = resume.get("display_name") or resume.get("file_name", "Resume")

        existing_app = await self.apps_col.find_one({"job_id": job["_id"]})
        app_doc_data = {
            "job_id": job["_id"],
            "resume_id": resume["_id"],
            "company": job.get("company", "Company"),
            "job_title": job.get("title", "Job Title"),
            "application_url": job.get("application_url") or job.get("job_url", ""),
            "resume_name": resume_name,
            "status": ApplicationState.QUEUED,
            "mode": mode,
            "user_id": user_id,
            "progress_steps": ["Enqueued for automated submission", f"Selected resume: {resume_name}"],
            "failure_reason": "",
            "retry_count": 0,
            "max_retries": max_retries,
            "cancel_requested": False,
            "updated_at": now
        }

        if existing_app:
            await self.apps_col.update_one({"_id": existing_app["_id"]}, {"$set": app_doc_data})
            app_id = str(existing_app["_id"])
        else:
            app_doc_data["created_at"] = now
            ins = await self.apps_col.insert_one(app_doc_data)
            app_id = str(ins.inserted_id)

        # Update Job doc status
        await self.jobs_col.update_one(
            {"_id": job["_id"]},
            {"$set": {"status": "QUEUED", "updated_at": now}}
        )

        return await self.enqueue_application(
            application_id=app_id,
            user_id=user_id,
            mode=mode,
            max_retries=max_retries,
            start_worker=start_worker
        )

    async def enqueue_jobs(
        self,
        job_ids: List[str],
        user_id: str = "default",
        mode: str = "APPLY",
        max_retries: int = 2
    ) -> List[Dict[str, Any]]:
        """Batch enqueues multiple job IDs."""
        results = []
        for jid in job_ids:
            try:
                res = await self.enqueue_job(
                    job_id=jid,
                    user_id=user_id,
                    mode=mode,
                    max_retries=max_retries
                )
                results.append(res)
            except Exception as e:
                logger.error(f"Failed to enqueue job {jid}: {e}")
                results.append({"job_id": jid, "error": str(e)})
        return results

    async def cancel_application(self, application_id: str, user_id: Optional[str] = None, reason: str = "Stopped by user") -> Optional[Dict[str, Any]]:
        """
        Stops a queued or running application. The status flip is atomic and conditional on
        the application still being active, so it cannot clobber a run that already finished.
        Returns None if the application does not exist (or is not owned by user_id).
        Raises ValueError if it is not in a stoppable state.
        """
        if not ObjectId.is_valid(application_id):
            raise ValueError(f"Invalid application ID: {application_id}")

        query: Dict[str, Any] = {"_id": ObjectId(application_id)}
        if user_id:
            u_oids = [ObjectId(user_id)] if ObjectId.is_valid(user_id) else []
            query["user_id"] = {"$in": u_oids + [str(user_id)]}

        now = datetime.now(timezone.utc)
        doc = await self.apps_col.find_one_and_update(
            {**query, "status": {"$in": list(ApplicationState.ACTIVE)}},
            {
                "$set": {
                    "status": ApplicationState.CANCELLED,
                    "cancel_requested": True,
                    "failure_reason": reason,
                    "message": reason,
                    "updated_at": now,
                },
                "$push": {"progress_steps": f"Automation stopped ({reason}) at {now.strftime('%H:%M:%S')} UTC"},
            },
            projection={"job_id": 1},
        )

        if not doc:
            existing = await self.apps_col.find_one(query, {"status": 1})
            if not existing:
                return None
            if existing.get("status") == ApplicationState.CANCELLED:
                return {"application_id": application_id, "status": ApplicationState.CANCELLED, "stopped": False}
            raise ValueError(f"Application is not running (status: {existing.get('status')}).")

        # Immediate stop if this process owns the run; other processes pick up the DB flag.
        was_running_here = execution_control.cancel_local(application_id)

        # Release the job so it can be applied to again
        if doc.get("job_id"):
            await self.jobs_col.update_one(
                {"_id": doc["job_id"], "status": {"$nin": ["APPLIED"]}},
                {"$set": {"status": "MATCHED", "failure_reason": None, "updated_at": now}},
            )

        logger.info(f"Application {application_id} stopped: {reason} (running_here={was_running_here}).")
        return {"application_id": application_id, "status": ApplicationState.CANCELLED, "stopped": True}

    async def get_queue_status(self) -> Dict[str, Any]:
        """Returns current operational status of the queue and aggregated counts from MongoDB."""
        counts = {}
        all_states = [
            ApplicationState.QUEUED,
            ApplicationState.STARTING,
            ApplicationState.OPENING_JOB,
            ApplicationState.FILLING_APPLICATION,
            ApplicationState.UPLOADING_RESUME,
            ApplicationState.SUBMITTING,
            ApplicationState.SUBMITTED,
            ApplicationState.FAILED,
            ApplicationState.SESSION_EXPIRED,
            ApplicationState.LOGIN_REQUIRED,
            ApplicationState.EXTERNAL_PORTAL,
            ApplicationState.CAPTCHA_REQUIRED,
            ApplicationState.TIMEOUT,
            ApplicationState.CANCELLED
        ]
        for s in all_states:
            try:
                c = await self.apps_col.count_documents({"status": s})
                counts[s] = c
            except Exception:
                counts[s] = 0

        return {
            "is_worker_running": self._worker_task is not None and not self._worker_task.done(),
            "queue_size": self._queue.qsize(),
            "active_application_id": self._active_application_id,
            "counts_by_status": counts
        }

    async def get_application_status(self, app_id: str) -> Optional[Dict[str, Any]]:
        """Fetches the real-time application status document from MongoDB."""
        if not ObjectId.is_valid(app_id):
            return None
        doc = await self.apps_col.find_one({"_id": ObjectId(app_id)})
        if not doc:
            return None

        return {
            "id": str(doc["_id"]),
            "job_id": str(doc["job_id"]),
            "resume_id": str(doc.get("resume_id", "")),
            "company": doc.get("company", ""),
            "job_title": doc.get("job_title", ""),
            "status": doc.get("status", ApplicationState.QUEUED),
            "mode": doc.get("mode", "APPLY"),
            "progress_steps": doc.get("progress_steps", []),
            "failure_reason": doc.get("failure_reason", ""),
            "retry_count": doc.get("retry_count", 0),
            "max_retries": doc.get("max_retries", 2),
            "applied_at": doc.get("applied_at"),
            "created_at": doc.get("created_at"),
            "updated_at": doc.get("updated_at")
        }

    async def recover_pending_on_startup(self):
        """
        Scans MongoDB for interrupted jobs in QUEUED, STARTING, in-flight pre-submission,
        or SUBMITTING states, distinguishing active work from abandoned crashes via heartbeats.
        """
        try:
            now = datetime.now(timezone.utc)
            cutoff = now - timedelta(seconds=self.lease_timeout_seconds)

            # 1. Recover abandoned pre-submission in-flight states:
            # [APPLYING, STARTING, OPENING_JOB, FILLING_APPLICATION, UPLOADING_RESUME]
            # APPLYING is a direct (non-queued) run; a retry detects an existing Dice application.
            pre_submission_states = [
                ApplicationState.APPLYING,
                ApplicationState.STARTING,
                ApplicationState.OPENING_JOB,
                ApplicationState.FILLING_APPLICATION,
                ApplicationState.UPLOADING_RESUME,
            ]
            stale_pre_submission_query = {
                "status": {"$in": pre_submission_states},
                "$or": [
                    {"heartbeat_at": {"$lt": cutoff}},
                    {"heartbeat_at": {"$exists": False}},
                    {"heartbeat_at": None}
                ]
            }

            cursor = self.apps_col.find(stale_pre_submission_query)
            abandoned_pre_count = 0
            async for doc in cursor:
                app_oid = doc["_id"]
                current_st = doc.get("status")
                recovery_reason = f"Worker crash detected during {current_st} (heartbeat lost). Mark as retryable failure."
                recovered = await self.apps_col.find_one_and_update(
                    {
                        "_id": app_oid,
                        "status": current_st,
                        "$or": [
                            {"heartbeat_at": {"$lt": cutoff}},
                            {"heartbeat_at": {"$exists": False}},
                            {"heartbeat_at": None}
                        ]
                    },
                    {
                        "$set": {
                            "status": ApplicationState.FAILED,
                            "failure_reason": recovery_reason,
                            "is_retryable": True,
                            "recovered_at": now,
                            "recovered_by": self._worker_instance_id,
                            "updated_at": now,
                        },
                        "$push": {
                            "progress_steps": f"Recovery monitor recovered stale in-flight application ({current_st}) at {now.strftime('%H:%M:%S')} UTC"
                        }
                    }
                )
                if recovered:
                    abandoned_pre_count += 1
                    if recovered.get("job_id"):
                        await self.jobs_col.update_one(
                            {"_id": recovered["job_id"], "status": {"$nin": ["APPLIED"]}},
                            {"$set": {"status": "FAILED", "failure_reason": recovery_reason, "updated_at": now}}
                        )

            # 2. Recover interrupted SUBMITTING operations:
            # Treated as ambiguous outcome requiring manual verification to prevent duplicate submission
            stale_submitting_query = {
                "status": ApplicationState.SUBMITTING,
                "$or": [
                    {"heartbeat_at": {"$lt": cutoff}},
                    {"heartbeat_at": {"$exists": False}},
                    {"heartbeat_at": None}
                ]
            }
            submitting_cursor = self.apps_col.find(stale_submitting_query)
            abandoned_submitting_count = 0
            async for doc in submitting_cursor:
                app_oid = doc["_id"]
                submitting_reason = "Application interrupted during SUBMITTING. Outcome ambiguous — manual verification required to avoid duplicate submission."
                recovered = await self.apps_col.find_one_and_update(
                    {
                        "_id": app_oid,
                        "status": ApplicationState.SUBMITTING,
                        "$or": [
                            {"heartbeat_at": {"$lt": cutoff}},
                            {"heartbeat_at": {"$exists": False}},
                            {"heartbeat_at": None}
                        ]
                    },
                    {
                        "$set": {
                            "status": ApplicationState.FAILED,
                            "failure_reason": submitting_reason,
                            "is_retryable": False,
                            "ambiguous_submission": True,
                            "recovered_at": now,
                            "recovered_by": self._worker_instance_id,
                            "updated_at": now,
                        },
                        "$push": {
                            "progress_steps": f"Recovery monitor intercepted ambiguous SUBMITTING state at {now.strftime('%H:%M:%S')} UTC (requires verification)"
                        }
                    }
                )
                if recovered:
                    abandoned_submitting_count += 1
                    if recovered.get("job_id"):
                        await self.jobs_col.update_one(
                            {"_id": recovered["job_id"], "status": {"$nin": ["APPLIED"]}},
                            {"$set": {"status": "FAILED", "failure_reason": submitting_reason, "updated_at": now}}
                        )

            # 3. Recover QUEUED items that are waiting to be processed
            queued_cursor = self.apps_col.find({"status": ApplicationState.QUEUED})
            recovered_queued_count = 0
            async for doc in queued_cursor:
                app_id = str(doc["_id"])
                user_id = doc.get("user_id", "default")
                mode = doc.get("mode", "APPLY")
                max_retries = doc.get("max_retries", 2)
                await self._queue.put({
                    "application_id": app_id,
                    "user_id": user_id,
                    "mode": mode,
                    "max_retries": max_retries
                })
                recovered_queued_count += 1

            if any([abandoned_pre_count, abandoned_submitting_count, recovered_queued_count]):
                logger.info(
                    f"ApplicationQueueManager recovery completed: "
                    f"{abandoned_pre_count} pre-submission abandoned, "
                    f"{abandoned_submitting_count} ambiguous submitting, "
                    f"{recovered_queued_count} queued."
                )
            if recovered_queued_count > 0:
                self.start_worker()

            return {
                "recovered_pre_submission": abandoned_pre_count,
                "recovered_ambiguous_submitting": abandoned_submitting_count,
                "recovered_queued": recovered_queued_count,
            }
        except Exception as e:
            logger.warning(f"Notice during queue startup recovery: {e}")
            return {"error": str(e)}


application_queue_manager = ApplicationQueueManager()
