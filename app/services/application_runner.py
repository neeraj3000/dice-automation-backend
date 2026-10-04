import asyncio
import logging
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional, Callable
from bson import ObjectId

from app.database import get_database
from app.config import settings
from app.schemas.application import ApplicationState
from app.browser.playwright_manager import playwright_manager
from app.browser.application_browser import application_browser
from app.services.session_store import get_session_store, _is_expired
from app.services.dice_session_restorer import (
    restore_dice_session,
    verify_dice_session,
    has_required_auth_tokens,
    SessionState
)
from app.services.settings_service import settings_service
from app.services.matching_service import matching_service
from app.services.jd_service import jd_service

logger = logging.getLogger(__name__)


def is_retryable_error(error: Exception, status: Optional[str] = None) -> bool:
    """
    Determines whether a failure is transient and eligible for bounded retries.
    Retry only:
    - browser startup failure
    - temporary network failure
    - page timeout
    - browser crash

    Do NOT retry blindly:
    - CAPTCHA / CAPTCHA_REQUIRED
    - login required / LOGIN_REQUIRED
    - session expired / SESSION_EXPIRED
    - external portal / EXTERNAL_PORTAL
    - account blocked
    - access denied / 401 / 403
    - failed application submission / validation errors
    """
    if status in (
        ApplicationState.CAPTCHA_REQUIRED,
        ApplicationState.LOGIN_REQUIRED,
        ApplicationState.SESSION_EXPIRED,
        ApplicationState.EXTERNAL_PORTAL,
        ApplicationState.SUBMITTED,
        ApplicationState.READY,
        ApplicationState.REVIEW
    ):
        return False

    err_str = str(error).lower()

    # Explicit non-retryable triggers
    non_retryable_terms = (
        "captcha",
        "turnstile",
        "login required",
        "session expired",
        "account blocked",
        "access denied",
        "403 forbidden",
        "401 unauthorized",
        "validation error",
        "required field",
        "already applied"
    )
    if any(term in err_str for term in non_retryable_terms):
        return False

    # Check for Timeout types
    if isinstance(error, (asyncio.TimeoutError, TimeoutError)):
        return True

    try:
        from playwright.async_api import TimeoutError as PlaywrightTimeoutError
        if isinstance(error, PlaywrightTimeoutError):
            return True
    except ImportError:
        pass

    # Explicit retryable transient terms
    retryable_terms = (
        "timeout",
        "timed out",
        "target closed",
        "browser has been closed",
        "browser closed",
        "browser crash",
        "connection closed",
        "connection refused",
        "connection reset",
        "net::err_",
        "browser startup failure",
        "failed to launch browser",
        "browser disconnected",
        "page crashed"
    )
    return any(term in err_str for term in retryable_terms)


class ApplicationRunner:
    @property
    def apps_col(self):
        return get_database().applications

    @property
    def jobs_col(self):
        return get_database().jobs

    @property
    def resumes_col(self):
        return get_database().resumes

    def is_retryable_error(self, error: Exception, status: Optional[str] = None) -> bool:
        return is_retryable_error(error, status)

    async def _notify(self, callback: Optional[Callable], state: str, app_id: str):
        if callback:
            try:
                res = callback(state, {"application_id": app_id})
                if asyncio.iscoroutine(res):
                    await res
            except Exception as e:
                logger.debug(f"Runner status notification failed: {e}")

    async def _update_application_status_only(self, app_id: str, status: str):
        try:
            if ObjectId.is_valid(app_id):
                await self.apps_col.update_one(
                    {"_id": ObjectId(app_id)},
                    {"$set": {"status": status, "updated_at": datetime.now(timezone.utc)}}
                )
        except Exception as e:
            logger.debug(f"Failed to update status for {app_id}: {e}")

    async def _update_application_doc(
        self,
        app_id: str,
        job_id: Any,
        status: str,
        progress_steps: List[str],
        failure_reason: Optional[str] = None,
        applied_at: Optional[datetime] = None,
        retry_count: int = 0,
        max_retries: int = 2
    ):
        now = datetime.now(timezone.utc)
        update_set: Dict[str, Any] = {
            "status": status,
            "progress_steps": progress_steps,
            "failure_reason": failure_reason or "",
            "retry_count": retry_count,
            "max_retries": max_retries,
            "updated_at": now
        }
        if applied_at:
            update_set["applied_at"] = applied_at

        if ObjectId.is_valid(app_id):
            await self.apps_col.update_one({"_id": ObjectId(app_id)}, {"$set": update_set})

        # Update Job doc
        if job_id and ObjectId.is_valid(str(job_id)):
            job_oid = ObjectId(str(job_id))
            if status == ApplicationState.SUBMITTED:
                await self.jobs_col.update_one(
                    {"_id": job_oid},
                    {"$set": {"status": "APPLIED", "applied_at": applied_at or now, "failure_reason": None, "updated_at": now}}
                )
            elif status in (
                ApplicationState.FAILED,
                ApplicationState.SESSION_EXPIRED,
                ApplicationState.LOGIN_REQUIRED,
                ApplicationState.EXTERNAL_PORTAL,
                ApplicationState.CAPTCHA_REQUIRED,
                ApplicationState.TIMEOUT
            ):
                await self.jobs_col.update_one(
                    {"_id": job_oid},
                    {"$set": {
                        "status": "FAILED",
                        "failure_reason": failure_reason or f"Application halted ({status})",
                        "updated_at": now
                    }}
                )

    async def run_application(
        self,
        application_id: str,
        user_id: str = "default",
        mode: str = "APPLY",
        max_retries: int = 2,
        status_callback: Optional[Callable[[str, Optional[Dict[str, Any]]], Any]] = None
    ) -> Dict[str, Any]:
        """
        Executes a job application adhering to the 11 runner requirements with bounded retries:
        1. Retrieve user's Dice session.
        2. Create/use isolated browser context.
        3. Restore Dice session.
        4. Verify authentication.
        5. Open the selected job.
        6. Run the existing application workflow.
        7. Upload resume.
        8. Handle existing screener logic.
        9. Detect result.
        10. Store final status.
        11. Clean up browser resources.
        """
        if not ObjectId.is_valid(application_id):
            return {
                "success": False,
                "status": ApplicationState.FAILED,
                "failure_reason": f"Invalid application ID: {application_id}"
            }

        app_doc = await self.apps_col.find_one({"_id": ObjectId(application_id)})
        if not app_doc:
            return {
                "success": False,
                "status": ApplicationState.FAILED,
                "failure_reason": f"Application document not found: {application_id}"
            }

        job = await self.jobs_col.find_one({"_id": app_doc["job_id"]})
        if not job:
            return {
                "success": False,
                "status": ApplicationState.FAILED,
                "failure_reason": f"Job document not found: {app_doc['job_id']}"
            }

        # Resolve associated resume
        resume_id = app_doc.get("resume_id")
        resume = await self.resumes_col.find_one({"_id": resume_id}) if resume_id else None
        if not resume:
            resume = await self.resumes_col.find_one({})
            if not resume:
                return {
                    "success": False,
                    "status": ApplicationState.FAILED,
                    "failure_reason": "No resume available in database."
                }

        user_profile = await settings_service.get_profile()

        attempt = 0
        retry_count = 0
        final_status = ApplicationState.FAILED
        progress_steps = list(app_doc.get("progress_steps", []))

        while attempt <= max_retries:
            context = None
            page = None
            try:
                attempt += 1
                if attempt > 1:
                    retry_count = attempt - 1
                    logger.info(f"Retrying application {application_id} (Attempt {attempt}/{max_retries + 1})...")

                # Step 1: Retrieve user's Dice session
                session_store = get_session_store()
                dice_session = await session_store.get_session(user_id)

                if not dice_session or not has_required_auth_tokens(dice_session):
                    final_status = ApplicationState.LOGIN_REQUIRED
                    progress_steps.append("No active Dice session credentials found. Login required.")
                    await self._update_application_doc(
                        application_id, job["_id"],
                        status=final_status,
                        progress_steps=progress_steps,
                        failure_reason="Dice login required. Please connect your Dice account.",
                        retry_count=retry_count,
                        max_retries=max_retries
                    )
                    return {
                        "success": False,
                        "status": final_status,
                        "progress_steps": progress_steps,
                        "failure_reason": "Dice login required."
                    }

                # Check session expiration
                exp_ts = getattr(dice_session, "expires_at", None) or (dice_session.get("expires_at") if isinstance(dice_session, dict) else None)
                if _is_expired(exp_ts):
                    final_status = ApplicationState.SESSION_EXPIRED
                    progress_steps.append("Dice session timestamp is expired.")
                    await self._update_application_doc(
                        application_id, job["_id"],
                        status=final_status,
                        progress_steps=progress_steps,
                        failure_reason="Dice session expired. Please resync your Dice account.",
                        retry_count=retry_count,
                        max_retries=max_retries
                    )
                    return {
                        "success": False,
                        "status": final_status,
                        "progress_steps": progress_steps,
                        "failure_reason": "Dice session expired."
                    }

                # Notify STARTING
                await self._notify(status_callback, ApplicationState.STARTING, application_id)
                await self._update_application_status_only(application_id, ApplicationState.STARTING)

                # Step 2: Create/use isolated browser context
                context = await playwright_manager.get_context(profile_id=user_id)

                # Step 3: Restore Dice session
                restored = await restore_dice_session(context, dice_session)
                if not restored:
                    logger.warning(f"Could not restore full session credentials for profile: {user_id}")

                # Step 4: Verify authentication
                page = await context.new_page()
                auth_res = await verify_dice_session(
                    browser_context=context,
                    session=dice_session,
                    page=page,
                    user_id=user_id,
                    timeout_ms=15000
                )

                if auth_res.get("state") == SessionState.SESSION_EXPIRED:
                    final_status = ApplicationState.SESSION_EXPIRED
                    progress_steps.append("Session verification failed: session expired.")
                    await self._update_application_doc(
                        application_id, job["_id"],
                        status=final_status,
                        progress_steps=progress_steps,
                        failure_reason="Dice session expired.",
                        retry_count=retry_count,
                        max_retries=max_retries
                    )
                    return {
                        "success": False,
                        "status": final_status,
                        "progress_steps": progress_steps,
                        "failure_reason": "Dice session expired."
                    }
                elif auth_res.get("state") == SessionState.LOGIN_REQUIRED:
                    final_status = ApplicationState.LOGIN_REQUIRED
                    progress_steps.append("Session verification failed: login required.")
                    await self._update_application_doc(
                        application_id, job["_id"],
                        status=final_status,
                        progress_steps=progress_steps,
                        failure_reason="Dice login required.",
                        retry_count=retry_count,
                        max_retries=max_retries
                    )
                    return {
                        "success": False,
                        "status": final_status,
                        "progress_steps": progress_steps,
                        "failure_reason": "Dice login required."
                    }

                # Step 5 - 9: Open selected job, run workflow, upload resume, screener QA, detect result
                target_url = job.get("application_url") or job.get("job_url", "")
                resume_file_path = resume.get("file_path") or resume.get("file_name", "")

                async def browser_status_cb(st: str):
                    await self._notify(status_callback, st, application_id)
                    await self._update_application_status_only(application_id, st)

                res = await application_browser.prepare_application(
                    application_url=target_url,
                    resume_file_path=resume_file_path,
                    user_profile=user_profile,
                    mode=mode,
                    page=page,
                    profile_id=user_id,
                    status_callback=browser_status_cb,
                    should_close_page=False
                )

                final_status = res.get("status", ApplicationState.FAILED)
                progress_steps.extend(res.get("progress_steps", []))

                # Step 10: Store final status
                if final_status in (ApplicationState.SUBMITTED, "APPLIED"):
                    final_status = ApplicationState.SUBMITTED
                    now_ts = datetime.now(timezone.utc)
                    await self._update_application_doc(
                        application_id, job["_id"],
                        status=ApplicationState.SUBMITTED,
                        progress_steps=progress_steps,
                        failure_reason=None,
                        applied_at=now_ts,
                        retry_count=retry_count,
                        max_retries=max_retries
                    )
                    return {
                        "success": True,
                        "status": ApplicationState.SUBMITTED,
                        "progress_steps": progress_steps,
                        "failure_reason": None
                    }

                elif final_status in (
                    ApplicationState.CAPTCHA_REQUIRED,
                    ApplicationState.LOGIN_REQUIRED,
                    ApplicationState.SESSION_EXPIRED,
                    ApplicationState.EXTERNAL_PORTAL,
                    ApplicationState.READY,
                    ApplicationState.REVIEW
                ):
                    # Explicit non-retryable states
                    await self._update_application_doc(
                        application_id, job["_id"],
                        status=final_status,
                        progress_steps=progress_steps,
                        failure_reason=res.get("failure_reason") or f"Application paused: {final_status}",
                        retry_count=retry_count,
                        max_retries=max_retries
                    )
                    return {
                        "success": final_status in (ApplicationState.READY, ApplicationState.REVIEW),
                        "status": final_status,
                        "progress_steps": progress_steps,
                        "failure_reason": res.get("failure_reason"),
                        "unanswered_questions": res.get("unanswered_questions", [])
                    }

                elif final_status == ApplicationState.TIMEOUT:
                    if attempt <= max_retries:
                        progress_steps.append(f"Timeout encountered. Retrying (attempt {attempt}/{max_retries})...")
                        await asyncio.sleep(2)
                        continue
                    else:
                        await self._update_application_doc(
                            application_id, job["_id"],
                            status=ApplicationState.TIMEOUT,
                            progress_steps=progress_steps,
                            failure_reason="Operation timed out after retries.",
                            retry_count=retry_count,
                            max_retries=max_retries
                        )
                        return {
                            "success": False,
                            "status": ApplicationState.TIMEOUT,
                            "progress_steps": progress_steps,
                            "failure_reason": "Operation timed out after retries."
                        }

                else:
                    # Failed state
                    fail_reason = res.get("failure_reason", "Application automation failed.")
                    if self.is_retryable_error(Exception(fail_reason), final_status) and attempt <= max_retries:
                        progress_steps.append(f"Transient error ({fail_reason}). Retrying (attempt {attempt}/{max_retries})...")
                        await asyncio.sleep(2)
                        continue
                    else:
                        await self._update_application_doc(
                            application_id, job["_id"],
                            status=ApplicationState.FAILED,
                            progress_steps=progress_steps,
                            failure_reason=fail_reason,
                            retry_count=retry_count,
                            max_retries=max_retries
                        )
                        return {
                            "success": False,
                            "status": ApplicationState.FAILED,
                            "progress_steps": progress_steps,
                            "failure_reason": fail_reason
                        }

            except Exception as exc:
                logger.error(f"ApplicationRunner attempt {attempt} failed: {exc}", exc_info=True)
                if self.is_retryable_error(exc, final_status) and attempt <= max_retries:
                    progress_steps.append(f"Transient error encountered: {exc}. Retrying...")
                    await asyncio.sleep(2)
                    continue
                else:
                    err_msg = str(exc)
                    final_status = ApplicationState.TIMEOUT if ("timeout" in err_msg.lower() or "timed out" in err_msg.lower()) else ApplicationState.FAILED
                    progress_steps.append(f"Fatal error: {err_msg}")
                    await self._update_application_doc(
                        application_id, job["_id"],
                        status=final_status,
                        progress_steps=progress_steps,
                        failure_reason=err_msg,
                        retry_count=retry_count,
                        max_retries=max_retries
                    )
                    return {
                        "success": False,
                        "status": final_status,
                        "progress_steps": progress_steps,
                        "failure_reason": err_msg
                    }

            finally:
                # Step 11: Clean up browser resources
                if page:
                    try:
                        await page.close()
                    except Exception:
                        pass


application_runner = ApplicationRunner()
