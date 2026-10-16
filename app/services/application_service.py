import asyncio
import logging
from datetime import datetime, timezone
from typing import List, Optional, Dict, Any, Awaitable, Callable
from bson import ObjectId
from fastapi import HTTPException

from app.database import get_database, sanitize_object_ids
from app.schemas.application import (
    ApplicationResponse, ApplicationAnswerSchema, ReviewItem, ApplicationCreate
)
from app.services.settings_service import settings_service
from app.services.matching_service import matching_service, user_resume_filter
from app.services.cloudinary_service import resume_file_for
from app.services.jd_service import jd_service
from app.browser.application_browser import application_browser
from app.services.execution_control import execution_control, NOT_CANCELLED

logger = logging.getLogger(__name__)

HEARTBEAT_INTERVAL = 20
FAILURE_STATES = ("FAILED", "EXTERNAL_PORTAL", "CAPTCHA_REQUIRED", "LOGIN_REQUIRED", "SESSION_EXPIRED", "TIMEOUT")

def format_app_doc(doc: Dict[str, Any], questions: List[Dict[str, Any]] = None) -> ApplicationResponse:
    data = sanitize_object_ids(dict(doc))
    data["id"] = str(data.pop("_id", data.get("id", "")))
    data["job_id"] = str(data.get("job_id", ""))
    data["resume_id"] = str(data.get("resume_id", "")) if data.get("resume_id") else ""
    if "user_id" in data and data["user_id"] is not None:
        data["user_id"] = str(data["user_id"])

    # Harmonize application_url and job_url
    if not data.get("application_url"):
        data["application_url"] = data.get("job_url", "")
    if not data.get("job_url"):
        data["job_url"] = data.get("application_url", "")

    # Harmonize failure_reason and message
    if not data.get("failure_reason") and data.get("message"):
        data["failure_reason"] = data.get("message", "")

    # Harmonize steps and progress_steps
    if not data.get("progress_steps") and data.get("steps"):
        steps = data.get("steps")
        if isinstance(steps, list):
            data["progress_steps"] = [s.get("step", str(s)) if isinstance(s, dict) else str(s) for s in steps]

    # Harmonize updated_at and finished_at
    if "updated_at" not in data or data["updated_at"] is None:
        data["updated_at"] = data.get("finished_at") or data.get("created_at")

    q_schemas = []
    if questions:
        for q in questions:
            qd = dict(q)
            qd["id"] = str(qd.pop("_id"))
            qd["application_id"] = str(qd.get("application_id", ""))
            q_schemas.append(ApplicationAnswerSchema(**qd))
    elif data.get("unanswered_questions"):
        for q in data["unanswered_questions"]:
            if isinstance(q, dict):
                qd = dict(q)
                q_text = qd.get("question") or qd.get("question_text", "")
                q_schemas.append(ApplicationAnswerSchema(
                    id=str(qd.get("_id", qd.get("id", ""))),
                    question_text=q_text,
                    options=qd.get("options") or [],
                    answer_text=qd.get("answer_text") or "",
                    is_answered=bool(qd.get("is_answered", False))
                ))
    data["pending_questions"] = q_schemas

    # Populate unanswered_questions list if not already present
    if not data.get("unanswered_questions"):
        unans = []
        for q in q_schemas:
            if not q.is_answered:
                unans.append({"question": q.question_text, "options": q.options or []})
        data["unanswered_questions"] = unans

    # Populate steps if missing
    if not data.get("steps"):
        step_time = data.get("created_at") or datetime.now(timezone.utc)
        data["steps"] = [{"at": step_time, "message": s} for s in data.get("progress_steps", [])]

    # Populate message and external_url
    if not data.get("message"):
        data["message"] = data.get("failure_reason") or ("Application submitted." if data.get("status") in ("APPLIED", "SUBMITTED") else "")
    if not data.get("external_url"):
        data["external_url"] = None

    # Harmonize board, source and provenance
    if not data.get("source"):
        data["source"] = data.get("board") or "dice"
    if not data.get("board"):
        data["board"] = data.get("source") or "dice"
    if not data.get("status_provenance"):
        data["status_provenance"] = "user_confirmed" if data.get("source") == "linkedin" else "unknown"

    return ApplicationResponse(**data)

class ApplicationService:
    _bg_tasks: set = set()

    @property
    def apps_col(self):
        return get_database().applications

    @property
    def jobs_col(self):
        return get_database().jobs

    @property
    def resumes_col(self):
        return get_database().resumes

    @property
    def answers_col(self):
        return get_database().application_answers

    async def prepare_application(self, req: ApplicationCreate) -> ApplicationResponse:
        if not ObjectId.is_valid(req.job_id):
            raise HTTPException(status_code=400, detail="Invalid job ID")

        job = await self.jobs_col.find_one({"_id": ObjectId(req.job_id)})
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")

        # Select resume (only ever from the requesting user's own library)
        owner_filter = user_resume_filter(getattr(req, "user_id", None) or job.get("user_id")) or {"_id": None}
        resume_id = req.resume_id
        if not resume_id:
            # Check if job already has recommended resume
            if job.get("match_result") and job["match_result"].get("recommended_resume_id"):
                resume_id = job["match_result"]["recommended_resume_id"]

        resume = None
        if resume_id and ObjectId.is_valid(resume_id):
            resume = await self.resumes_col.find_one({"_id": ObjectId(resume_id), **owner_filter})

        # If no resume specified or the referenced resume was deleted/replaced, re-match against current active library
        if not resume:
            # Run matching now against active resumes
            jd_structured = job.get("description_structured")
            if not jd_structured:
                jd_data = await jd_service.analyze_job_description(job.get("description_raw", ""), job.get("title", ""))
                jd_structured = jd_data.model_dump()
                await self.jobs_col.update_one(
                    {"_id": ObjectId(req.job_id)},
                    {"$set": {"description_structured": jd_structured}}
                )
            match_res = await matching_service.match_job_against_all_resumes(
                jd_data if 'jd_data' in locals() else await jd_service.analyze_job_description(job.get("description_raw", "")),
                job_id=str(job["_id"]),
                user_id=getattr(req, "user_id", None) or job.get("user_id"),
            )
            resume_id = match_res.recommended_resume_id
            if resume_id and ObjectId.is_valid(resume_id):
                resume = await self.resumes_col.find_one({"_id": ObjectId(resume_id), **owner_filter})

        # If matching still couldn't resolve, fallback to any resume in the user's own library
        if not resume:
            resume = await self.resumes_col.find_one(owner_filter)
            if not resume:
                raise HTTPException(status_code=400, detail="No matching resume found in library. Please upload a resume first.")
            resume_id = str(resume["_id"])

        now = datetime.now(timezone.utc)
        # Create or update Application doc
        existing_app = await self.apps_col.find_one({"job_id": ObjectId(req.job_id)})
        if existing_app and execution_control.is_running_locally(str(existing_app["_id"])):
            raise HTTPException(status_code=409, detail="An application for this job is already in progress.")
        target_uid = str(getattr(req, "user_id", None) or "default")
        app_data = {
            "job_id": ObjectId(req.job_id),
            "user_id": target_uid,
            "resume_id": ObjectId(resume_id),
            "company": job.get("company", "Company"),
            "job_title": job.get("title", "Job Title"),
            "application_url": job.get("application_url") or job.get("job_url", ""),
            "resume_name": resume.get("display_name") if (resume.get("file_name") and resume.get("file_name") in str(resume.get("display_name"))) else f"{resume.get('display_name', '')} ({resume.get('file_name', '')})".strip(),
            "status": "APPLYING",
            "mode": req.mode or "PREPARE",
            "progress_steps": ["Application preparation started", f"Selected resume: {resume.get('display_name')}"],
            "failure_reason": "",
            "cancel_requested": False,
            "updated_at": now
        }

        if existing_app:
            await self.apps_col.update_one({"_id": existing_app["_id"]}, {"$set": app_data})
            app_id = existing_app["_id"]
        else:
            app_data["created_at"] = now
            res = await self.apps_col.insert_one(app_data)
            app_id = res.inserted_id
        # Questions from an earlier attempt no longer apply to this run
        await self.answers_col.delete_many({"application_id": app_id, "is_answered": False})

        # Pre-check: Validate that candidate has active session tokens before launching browser
        from app.services.session_store import get_session_store
        from app.services.dice_session_restorer import has_required_auth_tokens
        store = get_session_store()
        active_sess = await store.get_session(target_uid) or await store.get_session("default")
        has_tokens = has_required_auth_tokens(active_sess)
        if not has_tokens:
            local_sess = settings_service.get_local_session()
            has_tokens = has_required_auth_tokens(local_sess)

        if not has_tokens:
            fail_msg = "Dice login required. Please connect your Dice account using the Dice Sync Extension."
            await self.apps_col.update_one(
                {"_id": app_id, **NOT_CANCELLED},
                {"$set": {
                    "status": "LOGIN_REQUIRED",
                    "failure_reason": fail_msg,
                    "progress_steps": app_data["progress_steps"] + [fail_msg],
                    "updated_at": datetime.now(timezone.utc)
                }}
            )
            await self.jobs_col.update_one(
                {"_id": job["_id"]},
                {"$set": {
                    "status": "FAILED",
                    "failure_reason": fail_msg,
                    "updated_at": datetime.now(timezone.utc)
                }}
            )
            saved_app = await self.apps_col.find_one({"_id": app_id})
            return format_app_doc(saved_app, [])

        # Run browser automation in the background: a full Dice run takes minutes, longer than
        # proxies keep a request open. The client follows progress by polling the application.
        user_profile = await settings_service.get_profile(user_id=target_uid)
        self._start_run(
            app_id, job["_id"], resume, app_data["progress_steps"],
            lambda resume_path: application_browser.prepare_application(
                application_url=app_data["application_url"],
                resume_file_path=resume_path,
                user_profile=user_profile,
                mode=app_data["mode"],
                user_id=target_uid
            ),
        )
        saved_app = await self.apps_col.find_one({"_id": app_id})
        return format_app_doc(saved_app, [])

    def _start_run(self, app_id: ObjectId, job_id: Any, resume: Dict[str, Any], base_steps: List[str], make_run: Callable[[str], Awaitable[Dict[str, Any]]]):
        task = asyncio.create_task(self._run_in_background(app_id, job_id, resume, base_steps, make_run))
        # Keep a reference so the task isn't garbage-collected mid-run
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    async def _heartbeat(self, app_id: ObjectId):
        # Lets startup recovery tell a live run from one abandoned by a crashed worker
        while True:
            try:
                await self.apps_col.update_one({"_id": app_id, **NOT_CANCELLED}, {"$set": {"heartbeat_at": datetime.now(timezone.utc)}})
            except Exception as e:
                logger.debug(f"Heartbeat failed for {app_id}: {e}")
            await asyncio.sleep(HEARTBEAT_INTERVAL)

    async def _run_in_background(self, app_id: ObjectId, job_id: Any, resume: Dict[str, Any], base_steps: List[str], make_run: Callable[[str], Awaitable[Dict[str, Any]]]):
        hb = asyncio.create_task(self._heartbeat(app_id))
        try:
            async with resume_file_for(resume) as resume_path:
                cancelled, browser_res = await execution_control.run_cancellable(str(app_id), make_run(str(resume_path)))
            if not cancelled:
                await self._finish_run(app_id, job_id, base_steps, browser_res)
        except Exception as e:
            logger.error(f"Application {app_id} run failed: {e}", exc_info=True)
            await self._finish_run(app_id, job_id, base_steps, {
                "status": "FAILED",
                "progress_steps": [f"Automation stopped: {e}"],
                "failure_reason": str(e) or "Application automation failed.",
            })
        finally:
            hb.cancel()

    async def _finish_run(self, app_id: ObjectId, job_id: Any, base_steps: List[str], browser_res: Dict[str, Any]):
        """Persists a run's outcome on the application, its job, and the review queue."""
        now = datetime.now(timezone.utc)
        status = browser_res.get("status") or "FAILED"
        failure_reason = browser_res.get("failure_reason") or ""
        applied = status in ("APPLIED", "SUBMITTED")
        update_fields = {
            "status": status,
            "progress_steps": base_steps + browser_res.get("progress_steps", []),
            "failure_reason": failure_reason,
            "updated_at": now
        }
        if applied:
            update_fields["applied_at"] = now

        # Persist the outcome unless the user stopped the run in the meantime
        res = await self.apps_col.update_one({"_id": app_id, **NOT_CANCELLED}, {"$set": update_fields})
        if res.matched_count == 0:
            return

        if applied:
            job_update = {"status": "APPLIED", "applied_at": now, "failure_reason": None}
            # Nothing left to answer once Dice has the application
            await self.answers_col.delete_many({"application_id": app_id, "is_answered": False})
        elif status in FAILURE_STATES:
            job_update = {"status": "FAILED", "failure_reason": failure_reason or f"Application halted: {status}"}
        elif status == "REVIEW":
            job_update = {"status": "REVIEW"}
        else:
            job_update = None
        if job_update and job_id and ObjectId.is_valid(str(job_id)):
            await self.jobs_col.update_one({"_id": ObjectId(str(job_id))}, {"$set": {**job_update, "updated_at": now}})

        for q in browser_res.get("unanswered_questions") or []:
            await self.answers_col.insert_one({
                "application_id": app_id,
                "question_text": q["question_text"],
                "field_name": q.get("field_name", ""),
                "options": q.get("options", []),
                "answer_text": "",
                "is_answered": False,
                "created_at": now
            })

    async def get_applications(self, user_id: Optional[Any] = None, board: Optional[str] = None) -> List[ApplicationResponse]:
        if not user_id:
            return []
        u_oids = [ObjectId(user_id)] if ObjectId.is_valid(user_id) else []
        query: Dict[str, Any] = {"user_id": {"$in": u_oids + [str(user_id)]}}
        if board and board.lower() != "all":
            query["$or"] = [{"board": board.lower()}, {"source": board.lower()}]
        cursor = self.apps_col.find(query).sort("created_at", -1)
        apps = []
        async for doc in cursor:
            # fetch pending questions
            q_cursor = self.answers_col.find({"application_id": doc["_id"], "is_answered": False})
            qs = await q_cursor.to_list(10)
            apps.append(format_app_doc(doc, qs))
        return apps

    async def get_application_by_id(self, app_id: str, user_id: Optional[Any] = None) -> Optional[ApplicationResponse]:
        if not ObjectId.is_valid(app_id):
            return None
        query = {"_id": ObjectId(app_id)}
        if user_id:
            u_oids = [ObjectId(user_id)] if ObjectId.is_valid(user_id) else []
            query["user_id"] = {"$in": u_oids + [str(user_id)]}
        doc = await self.apps_col.find_one(query)
        if not doc:
            return None
        q_cursor = self.answers_col.find({"application_id": ObjectId(app_id)})
        qs = await q_cursor.to_list(20)
        return format_app_doc(doc, qs)

    async def submit_application(self, app_id: str, user_id: Optional[str] = None) -> ApplicationResponse:
        if not ObjectId.is_valid(app_id):
            raise HTTPException(status_code=400, detail="Invalid application ID")
        query: Dict[str, Any] = {"_id": ObjectId(app_id)}
        if user_id:
            u_oids = [ObjectId(user_id)] if ObjectId.is_valid(user_id) else []
            query["user_id"] = {"$in": u_oids + [str(user_id)]}
        app_doc = await self.apps_col.find_one(query)
        if not app_doc:
            raise HTTPException(status_code=404, detail="Application not found")

        if execution_control.is_running_locally(app_id):
            raise HTTPException(status_code=409, detail="This application is already in progress.")
        # Submission protection: allow submitting if READY, REVIEW, PREPARE, FAILED, or re-submitting APPLIED
        if app_doc["status"] not in ["READY", "REVIEW", "PREPARE", "FAILED", "APPLIED", "APPLYING", "CANCELLED"]:
            raise HTTPException(status_code=400, detail=f"Cannot submit application in '{app_doc['status']}' status")

        # Fetch associated resume and user profile
        resume_id = app_doc.get("resume_id")
        resume = await self.resumes_col.find_one({"_id": resume_id}) if resume_id else None
        if not resume:
            # Fallback to any available resume in library
            resume = await self.resumes_col.find_one({})
            if not resume:
                raise HTTPException(status_code=400, detail="Associated resume was deleted and no resumes exist in library. Please upload a resume.")
            await self.apps_col.update_one(
                {"_id": ObjectId(app_id)},
                {"$set": {"resume_id": resume["_id"], "resume_name": resume.get("display_name", "Resume")}}
            )

        # Pre-check: Validate active session before browser launch
        from app.services.session_store import get_session_store
        from app.services.dice_session_restorer import has_required_auth_tokens
        store = get_session_store()
        session_uid = str(app_doc.get("user_id") or "default")
        active_sess = await store.get_session(session_uid) or await store.get_session("default")
        has_tokens = has_required_auth_tokens(active_sess)
        if not has_tokens:
            local_sess = settings_service.get_local_session()
            has_tokens = has_required_auth_tokens(local_sess)

        if not has_tokens:
            fail_msg = "Dice login required. Please connect your Dice account using the Dice Sync Extension."
            await self.apps_col.update_one(
                {"_id": ObjectId(app_id)},
                {"$set": {
                    "status": "LOGIN_REQUIRED",
                    "failure_reason": fail_msg,
                    "updated_at": datetime.now(timezone.utc)
                }}
            )
            updated = await self.apps_col.find_one({"_id": ObjectId(app_id)})
            return format_app_doc(updated, [])

        target_uid = str(app_doc.get("user_id") or "default")
        user_profile = await settings_service.get_profile(user_id=target_uid)
        now = datetime.now(timezone.utc)
        steps = app_doc.get("progress_steps", [])
        steps.append(f"Submitting application to Dice at {now.strftime('%H:%M:%S')}...")

        # Update to APPLYING while in-flight
        await self.apps_col.update_one(
            {"_id": ObjectId(app_id)},
            {"$set": {"status": "APPLYING", "progress_steps": steps, "failure_reason": "", "cancel_requested": False, "heartbeat_at": now, "updated_at": now}}
        )
        await self.answers_col.delete_many({"application_id": ObjectId(app_id), "is_answered": False})

        # Run real browser submission in the background (see prepare_application)
        self._start_run(
            ObjectId(app_id), app_doc.get("job_id"), resume, steps,
            lambda resume_path: application_browser.submit_application_to_dice(
                application_url=app_doc["application_url"],
                resume_file_path=resume_path,
                user_profile=user_profile,
                user_id=target_uid
            ),
        )
        return format_app_doc(await self.apps_col.find_one({"_id": ObjectId(app_id)}), [])

    async def get_review_queue(self, user_id: Optional[Any] = None) -> List[ReviewItem]:
        if not user_id:
            return []
        u_oids = [ObjectId(user_id)] if ObjectId.is_valid(user_id) else []
        # Questions only matter while their application is still waiting on the user
        user_app_ids = await self.apps_col.distinct("_id", {
            "user_id": {"$in": u_oids + [str(user_id)]},
            "status": {"$nin": ["SUBMITTED", "APPLIED", "CANCELLED", "APPLYING"]},
        })
        if not user_app_ids:
            return []
        cursor = self.answers_col.find({"application_id": {"$in": user_app_ids}, "is_answered": False}).sort("created_at", -1)
        items = []
        async for doc in cursor:
            app_id = doc.get("application_id")
            app = await self.apps_col.find_one({"_id": app_id}) if app_id else None
            items.append(ReviewItem(
                id=str(doc["_id"]),
                application_id=str(app_id) if app_id else "",
                company=app.get("company", "Unknown") if app else "Unknown",
                job_title=app.get("job_title", "Unknown") if app else "Unknown",
                question_text=doc.get("question_text", ""),
                field_name=doc.get("field_name", ""),
                options=doc.get("options", []),
                answer_text=doc.get("answer_text", ""),
                is_answered=doc.get("is_answered", False),
                created_at=doc.get("created_at", datetime.now(timezone.utc))
            ))
        return items

    async def answer_review_question(self, question_id: str, answer_text: str, user_id: Optional[Any] = None) -> bool:
        if not ObjectId.is_valid(question_id) or not user_id:
            return False
        q_doc = await self.answers_col.find_one({"_id": ObjectId(question_id)})
        if not q_doc:
            return False
        u_oids = [ObjectId(user_id)] if ObjectId.is_valid(user_id) else []
        app = await self.apps_col.find_one({"_id": q_doc.get("application_id"), "user_id": {"$in": u_oids + [str(user_id)]}})
        if not app:
            return False
        q_doc = await self.answers_col.find_one_and_update(
            {"_id": ObjectId(question_id)},
            {
                "$set": {
                    "answer_text": answer_text,
                    "is_answered": True,
                    "answered_at": datetime.now(timezone.utc)
                }
            },
            return_document=True
        )

        # If no more pending questions for this application, move status to READY
        app_id = q_doc.get("application_id")
        if app_id:
            pending_count = await self.answers_col.count_documents({
                "application_id": app_id,
                "is_answered": False
            })
            if pending_count == 0:
                await self.apps_col.update_one(
                    {"_id": app_id},
                    {
                        "$set": {
                            "status": "READY",
                            "updated_at": datetime.now(timezone.utc)
                        },
                        "$push": {
                            "progress_steps": f"Question answered: '{q_doc['question_text'][:40]}...'. Application ready."
                        }
                    }
                )
        return True

    async def answer_application_batch(self, app_id: str, answers: List[Dict[str, Any]], user_id: Optional[str] = None) -> Optional[ApplicationResponse]:
        if not ObjectId.is_valid(app_id):
            return None
        query: Dict[str, Any] = {"_id": ObjectId(app_id)}
        if user_id:
            u_oids = [ObjectId(user_id)] if ObjectId.is_valid(user_id) else []
            query["user_id"] = {"$in": u_oids + [str(user_id)]}
        app = await self.apps_col.find_one(query)
        if not app:
            return None

        now = datetime.now(timezone.utc)
        for a in answers:
            q_text = a.get("question") or a.get("question_text", "")
            ans_text = a.get("answer") or a.get("answer_text", "")
            await self.answers_col.update_many(
                {"application_id": ObjectId(app_id), "question_text": q_text},
                {"$set": {"answer_text": ans_text, "is_answered": True, "answered_at": now}}
            )

        updated = await self.apps_col.find_one_and_update(
            {"_id": ObjectId(app_id)},
            {
                "$set": {
                    "status": "READY",
                    "unanswered_questions": [],
                    "updated_at": now
                },
                "$push": {
                    "progress_steps": "Questions answered by user. Application marked READY."
                }
            },
            return_document=True
        )
        return format_app_doc(updated)

application_service = ApplicationService()
