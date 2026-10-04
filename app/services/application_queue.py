import asyncio
import logging
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional
from bson import ObjectId

from app.database import get_database
from app.schemas.application import ApplicationState
from app.services.application_runner import application_runner
from app.services.settings_service import settings_service
from app.services.matching_service import matching_service
from app.services.jd_service import jd_service

logger = logging.getLogger(__name__)


class ApplicationQueueManager:
    def __init__(self):
        self._queue: asyncio.Queue = asyncio.Queue()
        self._worker_task: Optional[asyncio.Task] = None
        self._active_application_id: Optional[str] = None
        self._is_running: bool = False

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

                self._active_application_id = app_id
                logger.info(f"Processing application from queue: {app_id} (user={user_id})")

                try:
                    await application_runner.run_application(
                        application_id=app_id,
                        user_id=user_id,
                        mode=mode,
                        max_retries=max_retries
                    )
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

        # Mark QUEUED in MongoDB
        await self.apps_col.update_one(
            {"_id": app_oid},
            {"$set": {
                "status": ApplicationState.QUEUED,
                "mode": mode,
                "max_retries": max_retries,
                "retry_count": 0,
                "failure_reason": "",
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

        # Resolve resume
        resolved_resume_id = resume_id
        if not resolved_resume_id:
            if job.get("match_result") and job["match_result"].get("recommended_resume_id"):
                resolved_resume_id = job["match_result"]["recommended_resume_id"]

        resume = None
        if resolved_resume_id and ObjectId.is_valid(resolved_resume_id):
            resume = await self.resumes_col.find_one({"_id": ObjectId(resolved_resume_id)})

        if not resume:
            # Match against resume library
            jd_structured = job.get("description_structured")
            if not jd_structured:
                jd_data = await jd_service.analyze_job_description(job.get("description_raw", ""), job.get("title", ""))
                jd_structured = jd_data.model_dump()
                await self.jobs_col.update_one({"_id": job["_id"]}, {"$set": {"description_structured": jd_structured}})

            match_res = await matching_service.match_job_against_all_resumes(
                jd_data if 'jd_data' in locals() else await jd_service.analyze_job_description(job.get("description_raw", "")),
                job_id=str(job["_id"])
            )
            if match_res.recommended_resume_id and ObjectId.is_valid(match_res.recommended_resume_id):
                resume = await self.resumes_col.find_one({"_id": ObjectId(match_res.recommended_resume_id)})

        if not resume:
            resume = await self.resumes_col.find_one({})
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
            ApplicationState.TIMEOUT
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
        """Scans MongoDB for interrupted jobs in QUEUED or STARTING state and re-enqueues them."""
        try:
            cursor = self.apps_col.find({"status": {"$in": [ApplicationState.QUEUED, ApplicationState.STARTING]}})
            recovered_count = 0
            async for doc in cursor:
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
                recovered_count += 1
            if recovered_count > 0:
                logger.info(f"ApplicationQueueManager recovered {recovered_count} pending applications on startup.")
                self.start_worker()
        except Exception as e:
            logger.warning(f"Notice during queue startup recovery: {e}")


application_queue_manager = ApplicationQueueManager()
