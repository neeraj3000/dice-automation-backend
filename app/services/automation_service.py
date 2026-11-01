"""
Timed automations: a saved set of search profiles, an apply target and a timer.

Starting an automation creates an `automation_runs` document and executes it as a background
task in this server process, so it keeps going after the user closes the browser tab. A run:
  1. searches each selected profile on Dice and applies to the best-matching new jobs,
  2. then works through older, never-applied jobs from those same profiles (backlog),
and ends at whichever comes first: the apply target (submitted jobs only), the timer, the user
pressing Stop, a lost Dice login, or running out of jobs.

Jobs that need a person (screener questions, external portal, unfinished wizard) are skipped and
land in the Review Queue. Every application a run makes is tagged with `automation_run_id`.

A watchdog task enforces the timer and stop requests (via the DB flag, so a Stop received by
another worker process still lands) and keeps the run's heartbeat fresh. An application that is
mid-submit when the run ends gets a short grace period so its outcome is never left ambiguous.
"""
import asyncio
import logging
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

from bson import ObjectId
from fastapi import HTTPException
from pymongo.errors import DuplicateKeyError

from app.database import get_database
from app.schemas.application import ApplicationState
from app.schemas.automation import (
    AutomationCreate,
    AutomationResponse,
    AutomationRunPage,
    AutomationRunResponse,
    AutomationRunStatus,
    AutomationUpdate,
)
from app.services.matching_service import user_resume_filter
from app.services.billing import PlanLimitError, entitlement_service
from app.services.realtime import AUTOMATION_UPDATED, notify_user

logger = logging.getLogger(__name__)

MAX_CONCURRENT_RUNS = int(os.environ.get("AUTOMATION_MAX_CONCURRENT_RUNS", "2"))
# Pause between applications, matching the Jobs page batch apply, so Dice isn't hammered
COOLDOWN_SECONDS = float(os.environ.get("AUTOMATION_COOLDOWN_SECONDS", "10"))
SUBMIT_GRACE_SECONDS = 30
# Steps an end request waits for (up to the grace period): submitting, and saving the outcome of
# an application that already finished, so a job Dice accepted is never left unrecorded
GRACE_STEPS = {"SUBMITTING", "SUBMITTED", "RECORDING"}
# A paused run still holds the user's single run slot, so it can't stay paused forever
MAX_PAUSE_SECONDS = 60 * 60
WATCH_INTERVAL_SECONDS = 2.0
HEARTBEAT_INTERVAL_SECONDS = 10.0
HEARTBEAT_STALE_SECONDS = 90
# Full activity log is kept for history; live polling only sends the latest entries
MAX_EVENTS = 1000
LIVE_EVENTS = 50
MAX_CANDIDATE_JOBS = 200
# Results fetched per profile per search round; independent of the user's manual-search setting
MIN_SCRAPE_PER_PROFILE = 20
MAX_SCRAPE_PER_PROFILE = 200
# The first round stays shallow so applying starts within minutes; later rounds go deeper.
# Sizing it from the target alone (70 -> 140 per profile) spent 20+ minutes on one search.
FIRST_ROUND_MAX_SCRAPE = 40
# Submits in a row that Dice clicked through but never showed as Applied: Dice is refusing
# applications (throttling / account limit), so stop rather than burn every job until the timer
MAX_UNCONFIRMED_STREAK = 3
# How long to wait for a search before applying to already-found jobs in the meantime
SEARCH_HEAD_START_SECONDS = 1.0

# Jobs that were found/matched but never queued, applied, failed or sent for review
ELIGIBLE_JOB_STATUSES = ["DISCOVERED", "ANALYZED", "MATCHED"]
SUBMITTED_OUTCOMES = {ApplicationState.SUBMITTED, ApplicationState.APPLIED}
REVIEW_OUTCOMES = {ApplicationState.REVIEW, ApplicationState.EXTERNAL_PORTAL}
LOGIN_OUTCOMES = {ApplicationState.LOGIN_REQUIRED, ApplicationState.SESSION_EXPIRED}

READY_REVIEW_TEXT = (
    "The automation couldn't reach Dice's final Submit step for this job. "
    "Open it from Applications and finish the application manually."
)
LOGIN_LOST_REASON = "Dice login expired. Reconnect your Dice account, then start the automation again."
UNCONFIRMED_STREAK_REASON = (
    "Dice stopped accepting applications: the last {n} submits went through the form but Dice never "
    "marked the jobs as Applied. Dice may be limiting applications on your account for now. "
    "Those jobs were kept for the next run. Try again later, and check one of them on Dice."
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    """MongoDB hands back naive datetimes (stored as UTC); make them comparable with aware ones."""
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _iso(dt: Optional[datetime]) -> Optional[str]:
    dt = _aware(dt)
    return dt.isoformat().replace("+00:00", "Z") if dt else None


def _user_filter(user_id: str) -> Dict[str, Any]:
    u_oids = [ObjectId(user_id)] if ObjectId.is_valid(user_id) else []
    return {"user_id": {"$in": u_oids + [str(user_id)]}}


def _oid(value: str, what: str) -> ObjectId:
    if not ObjectId.is_valid(value):
        raise HTTPException(status_code=404, detail=f"{what} not found")
    return ObjectId(value)


@dataclass
class _RunContext:
    run_id: ObjectId
    user_id: str
    automation_id: ObjectId
    profiles: List[Dict[str, Any]]
    apply_limit: int
    ends_at: datetime
    main_task: Optional[asyncio.Task] = None
    # (status, reason) once the run has been asked to end
    end: Optional[Tuple[str, str]] = None
    end_requested_at: Optional[datetime] = None
    cancel_sent: bool = False
    step: Optional[str] = None
    submitted: int = 0
    attempts: int = 0
    attempted: Set[ObjectId] = field(default_factory=set)
    # Jobs of the current run of consecutive unconfirmed submits
    unconfirmed_streak: List[ObjectId] = field(default_factory=list)
    # Pause: requested by the user, takes effect between jobs; the timer is frozen while paused
    pause_requested: bool = False
    paused_at: Optional[datetime] = None


class AutomationService:
    def __init__(self):
        self._runs: Dict[str, _RunContext] = {}
        self._tasks: Set[asyncio.Task] = set()
        self._start_lock: Optional[asyncio.Lock] = None
        self.worker_id = f"automation_{os.getpid()}_{uuid.uuid4().hex[:8]}"

    @property
    def automations_col(self):
        return get_database().automations

    @property
    def runs_col(self):
        return get_database().automation_runs

    # ----- Automations CRUD ----------------------------------------------------

    async def _owned_profile_ids(self, user_id: str, profile_ids: List[str]) -> List[ObjectId]:
        oids = [ObjectId(p) for p in profile_ids if ObjectId.is_valid(p)]
        found = set()
        if oids:
            found = set(await get_database().search_profiles.distinct("_id", {"_id": {"$in": oids}, **_user_filter(user_id)}))
        if len(oids) != len(profile_ids) or len(found) != len(oids):
            raise HTTPException(status_code=400, detail="One or more selected search profiles no longer exist.")
        return oids

    async def _get_owned(self, user_id: str, automation_id: str) -> Dict[str, Any]:
        doc = await self.automations_col.find_one({"_id": _oid(automation_id, "Automation"), **_user_filter(user_id)})
        if not doc:
            raise HTTPException(status_code=404, detail="Automation not found")
        return doc

    async def _last_runs(self, automation_ids: List[ObjectId]) -> Dict[ObjectId, Dict[str, Any]]:
        out: Dict[ObjectId, Dict[str, Any]] = {}
        if not automation_ids:
            return out
        pipeline = [
            {"$match": {"automation_id": {"$in": automation_ids}}},
            {"$sort": {"started_at": -1}},
            {"$group": {"_id": "$automation_id", "run": {"$first": "$$ROOT"}}},
        ]
        async for row in self.runs_col.aggregate(pipeline):
            run = row["run"]
            out[row["_id"]] = {
                "id": str(run["_id"]),
                "status": run.get("status"),
                "started_at": _iso(run.get("started_at")),
                "finished_at": _iso(run.get("finished_at")),
                "end_reason": run.get("end_reason"),
                "counters": run.get("counters") or {},
            }
        return out

    def _automation_out(self, doc: Dict[str, Any], last_run: Optional[Dict[str, Any]] = None) -> AutomationResponse:
        return AutomationResponse(
            id=str(doc["_id"]),
            name=doc.get("name", ""),
            search_profile_ids=[str(p) for p in doc.get("search_profile_ids", [])],
            apply_limit=doc.get("apply_limit", 10),
            duration_minutes=doc.get("duration_minutes", 30),
            created_at=_aware(doc.get("created_at")),
            updated_at=_aware(doc.get("updated_at")),
            last_run=last_run,
        )

    async def create_automation(self, user_id: str, data: AutomationCreate) -> AutomationResponse:
        profile_oids = await self._owned_profile_ids(user_id, data.search_profile_ids)
        now = _utcnow()
        doc = {
            "user_id": str(user_id),
            "name": data.name,
            "search_profile_ids": profile_oids,
            "apply_limit": data.apply_limit,
            "duration_minutes": data.duration_minutes,
            "created_at": now,
            "updated_at": now,
        }
        res = await self.automations_col.insert_one(doc)
        doc["_id"] = res.inserted_id
        return self._automation_out(doc)

    async def list_automations(self, user_id: str) -> List[AutomationResponse]:
        docs = await self.automations_col.find(_user_filter(user_id)).sort("created_at", -1).to_list(200)
        last = await self._last_runs([d["_id"] for d in docs])
        return [self._automation_out(d, last.get(d["_id"])) for d in docs]

    async def get_automation(self, user_id: str, automation_id: str) -> AutomationResponse:
        doc = await self._get_owned(user_id, automation_id)
        last = await self._last_runs([doc["_id"]])
        return self._automation_out(doc, last.get(doc["_id"]))

    async def update_automation(self, user_id: str, automation_id: str, data: AutomationUpdate) -> AutomationResponse:
        doc = await self._get_owned(user_id, automation_id)
        changes = data.model_dump(exclude_unset=True, exclude_none=True)
        if "search_profile_ids" in changes:
            changes["search_profile_ids"] = await self._owned_profile_ids(user_id, changes["search_profile_ids"])
        if changes:
            changes["updated_at"] = _utcnow()
            await self.automations_col.update_one({"_id": doc["_id"]}, {"$set": changes})
        return await self.get_automation(user_id, automation_id)

    async def delete_automation(self, user_id: str, automation_id: str):
        doc = await self._get_owned(user_id, automation_id)
        if await self.runs_col.find_one({"automation_id": doc["_id"], "status": AutomationRunStatus.RUNNING}):
            raise HTTPException(status_code=409, detail="Stop the running automation before deleting it.")
        # Runs are kept: they carry the automation's name, so history stays complete
        await self.automations_col.delete_one({"_id": doc["_id"]})

    # ----- Runs: queries -------------------------------------------------------

    def _run_out(self, doc: Dict[str, Any], events_limit: Optional[int] = LIVE_EVENTS) -> AutomationRunResponse:
        """events_limit: newest N events (None = the full log, 0 = none)."""
        now = _utcnow()
        is_active = doc.get("status") == AutomationRunStatus.RUNNING
        ends_at = _aware(doc.get("ends_at"))
        paused_at = _aware(doc.get("paused_at")) if doc.get("paused") else None
        # While paused the countdown is frozen at the moment the run paused
        clock = paused_at or now
        remaining = max(0, int((ends_at - clock).total_seconds())) if (is_active and ends_at) else 0
        events = [
            {"at": _iso(e.get("at")), "type": e.get("type", "info"), "message": e.get("message", "")}
            for e in (doc.get("events") or [])[-events_limit if events_limit else None:]
        ] if events_limit != 0 else []
        current = doc.get("current") if is_active else None
        return AutomationRunResponse(
            id=str(doc["_id"]),
            automation_id=str(doc.get("automation_id", "")),
            automation_name=doc.get("automation_name", ""),
            status=doc.get("status", AutomationRunStatus.FAILED),
            phase=doc.get("phase") if is_active else None,
            is_active=is_active,
            started_at=_iso(doc.get("started_at")),
            ends_at=_iso(ends_at),
            finished_at=_iso(doc.get("finished_at")),
            end_reason=doc.get("end_reason"),
            apply_limit=doc.get("apply_limit", 0),
            duration_minutes=doc.get("duration_minutes", 0),
            profile_names=doc.get("profile_names", []),
            counters=doc.get("counters") or {},
            current=current,
            events=events,
            remaining_seconds=remaining,
            server_now=_iso(now),
            paused=bool(paused_at) and is_active,
            pause_requested=bool(doc.get("pause_requested")) and is_active,
            paused_at=_iso(paused_at),
            paused_seconds=int(doc.get("paused_seconds") or 0),
        )

    async def _expire_stale_runs(self, extra_filter: Optional[Dict[str, Any]] = None) -> int:
        """Marks RUNNING runs whose worker stopped heartbeating (crash / restart) as INTERRUPTED."""
        now = _utcnow()
        cutoff = now - timedelta(seconds=HEARTBEAT_STALE_SECONDS)
        query = {
            "status": AutomationRunStatus.RUNNING,
            "$or": [{"heartbeat_at": {"$lt": cutoff}}, {"heartbeat_at": None}],
            **(extra_filter or {}),
        }
        stale = await self.runs_col.find(query, {"_id": 1, "user_id": 1}).to_list(500)
        for doc in stale:
            res = await self.runs_col.update_one(
                {"_id": doc["_id"], "status": AutomationRunStatus.RUNNING},
                {
                    "$set": {
                        "status": AutomationRunStatus.INTERRUPTED,
                        "end_reason": "The server restarted while this automation was running.",
                        "finished_at": now,
                        "phase": None,
                        "current": None,
                        "updated_at": now,
                    },
                    "$push": {"events": {"$each": [{"at": now, "type": "error", "message": "Automation interrupted by a server restart."}], "$slice": -MAX_EVENTS}},
                },
            )
            if res.modified_count:
                await self._release_active_applications(doc["_id"], str(doc.get("user_id")), "Automation interrupted")
                await notify_user(doc.get("user_id"), AUTOMATION_UPDATED)
        return len(stale)

    async def get_active_run(self, user_id: str) -> Optional[AutomationRunResponse]:
        await self._expire_stale_runs(_user_filter(user_id))
        doc = await self.runs_col.find_one({**_user_filter(user_id), "status": AutomationRunStatus.RUNNING})
        return self._run_out(doc) if doc else None

    async def get_run(self, user_id: str, run_id: str) -> AutomationRunResponse:
        doc = await self.runs_col.find_one({"_id": _oid(run_id, "Automation run"), **_user_filter(user_id)})
        if not doc:
            raise HTTPException(status_code=404, detail="Automation run not found")
        return self._run_out(doc, events_limit=None)

    async def list_run_history(
        self, user_id: str, automation_id: Optional[str] = None, limit: int = 20, skip: int = 0
    ) -> AutomationRunPage:
        """All of the user's runs, newest first, including runs of deleted automations."""
        await self._expire_stale_runs(_user_filter(user_id))
        query: Dict[str, Any] = _user_filter(user_id)
        if automation_id:
            query["automation_id"] = _oid(automation_id, "Automation")
        total = await self.runs_col.count_documents(query)
        docs = await self.runs_col.find(query, {"events": 0}).sort("started_at", -1).skip(skip).limit(limit).to_list(limit)
        return AutomationRunPage(items=[self._run_out(d, events_limit=0) for d in docs], total=total)

    # ----- Runs: start / stop --------------------------------------------------

    async def _check_dice_session(self, user_id: str):
        # Pre-flight check before any automation run.
        # Validates both token presence/expiry AND performs a lightweight HTTP live-check
        # so we catch Dice sessions that are structurally present but server-side invalidated
        # BEFORE burning 50+ application attempts (which trigger browser-nav false positives).
        from app.services.session_store import get_session_store, _is_expired
        from app.services.dice_session_restorer import has_required_auth_tokens
        from app.services.settings_service import settings_service

        store = get_session_store()
        sess = await store.get_session(user_id) or await store.get_session("default")
        ok = has_required_auth_tokens(sess) and not _is_expired(getattr(sess, "expires_at", None))
        if not ok:
            local_sess = settings_service.get_local_session()
            ok = has_required_auth_tokens(local_sess)
            sess = local_sess if ok else sess
        if not ok:
            raise HTTPException(status_code=400, detail="Connect your Dice account before starting an automation.")

        # Safety: also do an HTTP live-check so a structurally-valid-but-server-expired
        # session is caught here rather than mid-run. Log a warning but don't abort ?
        # the session may still work; this just surfaces the risk early.
        try:
            live = await settings_service._verify_cookies_via_http(getattr(sess, "cookies", None) or [])
            if not live.get("is_connected"):
                logger.warning(
                    f"Pre-flight HTTP session check for user {user_id} returned not-connected. "
                    "The session cookies may be stale. Consider re-syncing via the extension."
                )
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "Your Dice session appears to have expired. "
                        "Open the Dice Session & Board settings, click 'Check Status', "
                        "and re-sync your session via the Chrome extension before starting the automation."
                    )
                )
        except HTTPException:
            raise
        except Exception as e:
            # Network / unexpected error during live check ? log and allow the run to proceed
            logger.warning(f"Pre-flight HTTP session live-check could not complete for user {user_id}: {e}")

    async def start_run(self, user_id: str, automation_id: str) -> AutomationRunResponse:
        automation = await self._get_owned(user_id, automation_id)
        profile_oids = automation.get("search_profile_ids", [])
        profiles_by_id = {
            p["_id"]: p
            async for p in get_database().search_profiles.find({"_id": {"$in": profile_oids}, **_user_filter(user_id)})
        }
        profiles = [profiles_by_id[p] for p in profile_oids if p in profiles_by_id]
        if not profiles:
            raise HTTPException(status_code=400, detail="This automation's search profiles were deleted. Edit it and pick at least one profile.")

        if not await get_database().resumes.find_one(user_resume_filter(user_id) or {"_id": None}):
            raise HTTPException(status_code=400, detail="Upload a resume before starting an automation.")
        await entitlement_service.assert_can_apply(user_id)
        await self._check_dice_session(user_id)

        if self._start_lock is None:
            self._start_lock = asyncio.Lock()
        async with self._start_lock:
            await self._expire_stale_runs()
            if await self.runs_col.find_one({**_user_filter(user_id), "status": AutomationRunStatus.RUNNING}):
                raise HTTPException(status_code=409, detail="An automation is already running. Stop it before starting another.")
            running = await self.runs_col.count_documents({"status": AutomationRunStatus.RUNNING})
            if running >= MAX_CONCURRENT_RUNS:
                raise HTTPException(status_code=409, detail="The automation server is busy right now. Please try again in a few minutes.")

            now = _utcnow()
            duration = int(automation.get("duration_minutes", 30))
            apply_limit = int(automation.get("apply_limit", 10))
            ends_at = now + timedelta(minutes=duration)
            run_doc = {
                "automation_id": automation["_id"],
                "automation_name": automation.get("name", ""),
                "user_id": str(user_id),
                "status": AutomationRunStatus.RUNNING,
                "phase": "STARTING",
                "apply_limit": apply_limit,
                "duration_minutes": duration,
                "profile_names": [p.get("name", "") for p in profiles],
                "counters": {"scraped": 0, "submitted": 0, "review": 0, "failed": 0},
                "current": None,
                "events": [],
                "stop_requested": False,
                "pause_requested": False,
                "paused": False,
                "paused_at": None,
                "paused_seconds": 0,
                "worker_id": self.worker_id,
                "started_at": now,
                "ends_at": ends_at,
                "heartbeat_at": now,
                "updated_at": now,
            }
            try:
                res = await self.runs_col.insert_one(run_doc)
            except DuplicateKeyError:
                # Unique partial index on (user_id, status=RUNNING) caught a concurrent start
                raise HTTPException(status_code=409, detail="An automation is already running. Stop it before starting another.")

        run_doc["_id"] = res.inserted_id
        await self.automations_col.update_one({"_id": automation["_id"]}, {"$set": {"last_run_at": now}})

        ctx = _RunContext(
            run_id=res.inserted_id,
            user_id=str(user_id),
            automation_id=automation["_id"],
            profiles=profiles,
            apply_limit=apply_limit,
            ends_at=ends_at,
        )
        self._runs[str(ctx.run_id)] = ctx
        task = asyncio.create_task(self._execute(ctx))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        await notify_user(user_id, AUTOMATION_UPDATED)
        return self._run_out(run_doc)

    async def stop_run(self, user_id: str, run_id: str) -> AutomationRunResponse:
        run_oid = _oid(run_id, "Automation run")
        doc = await self.runs_col.find_one_and_update(
            {"_id": run_oid, **_user_filter(user_id), "status": AutomationRunStatus.RUNNING},
            {"$set": {"stop_requested": True, "updated_at": _utcnow()}},
            return_document=True,
        )
        if not doc:
            existing = await self.runs_col.find_one({"_id": run_oid, **_user_filter(user_id)})
            if not existing:
                raise HTTPException(status_code=404, detail="Automation run not found")
            return self._run_out(existing)  # already finished
        ctx = self._runs.get(run_id)
        if ctx:
            self._request_end(ctx, AutomationRunStatus.STOPPED, "Stopped by you.")
        # Otherwise another worker owns the run; its watchdog picks up the DB flag
        await notify_user(user_id, AUTOMATION_UPDATED)
        return self._run_out(doc)

    async def set_paused(self, user_id: str, run_id: str, paused: bool) -> AutomationRunResponse:
        """Pause takes effect after the current job (or search) finishes; Resume continues at once."""
        run_oid = _oid(run_id, "Automation run")
        doc = await self.runs_col.find_one_and_update(
            {"_id": run_oid, **_user_filter(user_id), "status": AutomationRunStatus.RUNNING},
            {"$set": {"pause_requested": paused, "updated_at": _utcnow()}},
            return_document=True,
        )
        if not doc:
            existing = await self.runs_col.find_one({"_id": run_oid, **_user_filter(user_id)})
            if not existing:
                raise HTTPException(status_code=404, detail="Automation run not found")
            raise HTTPException(status_code=409, detail="This automation has already finished.")
        ctx = self._runs.get(run_id)
        if ctx:
            ctx.pause_requested = paused
        if paused and not doc.get("paused"):
            await self.runs_col.update_one(
                {"_id": run_oid},
                {"$push": {"events": {"$each": [{"at": _utcnow(), "type": "info", "message": "Pausing after the current step finishes…"}], "$slice": -MAX_EVENTS}}},
            )
        await notify_user(user_id, AUTOMATION_UPDATED)
        return self._run_out(doc)

    # ----- Lifecycle -----------------------------------------------------------

    async def recover_on_startup(self):
        try:
            count = await self._expire_stale_runs()
            if count:
                logger.info(f"Marked {count} abandoned automation run(s) as interrupted.")
        except Exception as e:
            logger.warning(f"Automation startup recovery failed: {e}")

    async def shutdown(self):
        for ctx in list(self._runs.values()):
            self._request_end(ctx, AutomationRunStatus.INTERRUPTED, "The server restarted while this automation was running.", immediate=True)
        if self._tasks:
            await asyncio.wait(set(self._tasks), timeout=15)

    # ----- Execution -----------------------------------------------------------

    def _request_end(self, ctx: _RunContext, status: str, reason: str, immediate: bool = False):
        if ctx.end is None:
            ctx.end = (status, reason)
            ctx.end_requested_at = _utcnow()
        if immediate and ctx.main_task and not ctx.main_task.done():
            ctx.cancel_sent = True
            ctx.main_task.cancel()

    async def _event(self, ctx: _RunContext, message: str, type_: str = "info", **set_fields):
        now = _utcnow()
        update: Dict[str, Any] = {
            "$push": {"events": {"$each": [{"at": now, "type": type_, "message": message}], "$slice": -MAX_EVENTS}},
            "$set": {"updated_at": now, **set_fields},
        }
        try:
            await self.runs_col.update_one({"_id": ctx.run_id}, update)
        except Exception as e:
            logger.debug(f"Could not record automation event: {e}")
        await notify_user(ctx.user_id, AUTOMATION_UPDATED)

    async def _set(self, ctx: _RunContext, inc: Optional[Dict[str, int]] = None, **set_fields):
        update: Dict[str, Any] = {"$set": {"updated_at": _utcnow(), **set_fields}}
        if inc:
            update["$inc"] = {f"counters.{k}": v for k, v in inc.items()}
        try:
            await self.runs_col.update_one({"_id": ctx.run_id}, update)
        except Exception as e:
            logger.debug(f"Could not update automation run: {e}")
        await notify_user(ctx.user_id, AUTOMATION_UPDATED)

    async def _watchdog(self, ctx: _RunContext):
        """Enforces the timer and stop requests, and keeps the run's heartbeat fresh."""
        last_heartbeat = _utcnow()
        while ctx.main_task and not ctx.main_task.done():
            await asyncio.sleep(WATCH_INTERVAL_SECONDS)
            now = _utcnow()
            try:
                if ctx.end is None:
                    # DB flags carry Stop / Pause / Resume received by any worker process
                    doc = await self.runs_col.find_one({"_id": ctx.run_id}, {"stop_requested": 1, "pause_requested": 1})
                    if doc:
                        ctx.pause_requested = bool(doc.get("pause_requested"))
                    if doc and doc.get("stop_requested"):
                        self._request_end(ctx, AutomationRunStatus.STOPPED, "Stopped by you.")
                    elif ctx.paused_at:
                        if (now - ctx.paused_at).total_seconds() >= MAX_PAUSE_SECONDS:
                            self._request_end(
                                ctx, AutomationRunStatus.STOPPED,
                                f"Paused for more than {MAX_PAUSE_SECONDS // 60} minutes, so the automation was stopped.",
                            )
                    elif now >= ctx.ends_at:
                        self._request_end(ctx, AutomationRunStatus.TIME_UP, "Time limit reached.")

                if ctx.end and not ctx.cancel_sent:
                    in_grace = (
                        ctx.step in GRACE_STEPS
                        and (now - ctx.end_requested_at).total_seconds() < SUBMIT_GRACE_SECONDS
                    )
                    if not in_grace:
                        ctx.cancel_sent = True
                        ctx.main_task.cancel()

                if (now - last_heartbeat).total_seconds() >= HEARTBEAT_INTERVAL_SECONDS:
                    last_heartbeat = now
                    await self.runs_col.update_one({"_id": ctx.run_id}, {"$set": {"heartbeat_at": now}})
            except Exception as e:
                logger.debug(f"Automation watchdog notice for {ctx.run_id}: {e}")

    async def _execute(self, ctx: _RunContext):
        ctx.main_task = asyncio.create_task(self._run_main(ctx))
        watchdog = asyncio.create_task(self._watchdog(ctx))
        status, reason = AutomationRunStatus.INTERRUPTED, "The automation stopped unexpectedly."
        try:
            try:
                status, reason = await ctx.main_task
            except asyncio.CancelledError:
                if ctx.end is None:
                    raise
            except Exception as e:
                logger.error(f"Automation run {ctx.run_id} failed: {e}", exc_info=True)
                status, reason = AutomationRunStatus.FAILED, f"Automation error: {e}"
            # An end request (timer / stop / shutdown) wins over whatever the loop returned
            if ctx.end:
                status, reason = ctx.end
        finally:
            watchdog.cancel()
            await self._finalize(ctx, status, reason)
            self._runs.pop(str(ctx.run_id), None)

    async def _finalize(self, ctx: _RunContext, status: str, reason: str):
        now = _utcnow()
        try:
            await self._release_active_applications(ctx.run_id, ctx.user_id, f"Automation ended: {reason}")
        except Exception as e:
            logger.warning(f"Could not release in-flight applications for run {ctx.run_id}: {e}")
        try:
            from app.browser.playwright_manager import playwright_manager
            await playwright_manager.close_context(ctx.user_id)
        except Exception as e:
            logger.debug(f"Could not close automation browser for {ctx.user_id}: {e}")
        type_ = {
            AutomationRunStatus.COMPLETED: "success",
            AutomationRunStatus.TIME_UP: "info",
            AutomationRunStatus.STOPPED: "info",
        }.get(status, "error")
        # Ended while paused (e.g. Stop pressed during a pause): count that pause too
        paused_inc = int((now - ctx.paused_at).total_seconds()) if ctx.paused_at else 0
        try:
            await self.runs_col.update_one(
                {"_id": ctx.run_id, "status": AutomationRunStatus.RUNNING},
                {
                    "$inc": {"paused_seconds": paused_inc},
                    "$set": {
                        "paused": False,
                        "pause_requested": False,
                        "status": status,
                        "end_reason": reason,
                        "finished_at": now,
                        "phase": None,
                        "current": None,
                        "updated_at": now,
                        "heartbeat_at": now,
                    },
                    "$push": {"events": {"$each": [{"at": now, "type": type_, "message": reason}], "$slice": -MAX_EVENTS}},
                },
            )
        except Exception as e:
            logger.error(f"Could not finalize automation run {ctx.run_id}: {e}")
        logger.info(f"Automation run {ctx.run_id} finished: {status} ({reason})")
        await notify_user(ctx.user_id, AUTOMATION_UPDATED)

    async def _release_active_applications(self, run_id: ObjectId, user_id: str, reason: str):
        """Cancels any application of this run that is still in flight and frees its job."""
        from app.services.application_queue import application_queue_manager

        active = await get_database().applications.find(
            {"automation_run_id": run_id, "status": {"$in": list(ApplicationState.ACTIVE)}}, {"_id": 1}
        ).to_list(50)
        for app in active:
            try:
                await application_queue_manager.cancel_application(str(app["_id"]), user_id=user_id, reason=reason)
            except ValueError:
                pass  # finished in the meantime

    async def _candidate_jobs(
        self, ctx: _RunContext, job_ids: Optional[List[str]] = None, limit: int = MAX_CANDIDATE_JOBS
    ) -> List[Dict[str, Any]]:
        """Never-applied jobs, best match first: the given ids, or the backlog of the run's profiles."""
        query: Dict[str, Any] = {**_user_filter(ctx.user_id), "status": {"$in": ELIGIBLE_JOB_STATUSES}}
        id_filter: Dict[str, Any] = {"$nin": list(ctx.attempted)}
        if job_ids is not None:
            id_filter["$in"] = [ObjectId(j) for j in job_ids if ObjectId.is_valid(j)]
        else:
            profile_oids = [p["_id"] for p in ctx.profiles]
            query["search_profile_id"] = {"$in": profile_oids + [str(p) for p in profile_oids]}
        query["_id"] = id_filter
        cursor = get_database().jobs.find(query).sort([("match_result.match_percentage", -1), ("created_at", -1)])
        return await cursor.to_list(limit)

    def _start_search(self, ctx: _RunContext, profile: Dict[str, Any], size: int) -> asyncio.Task:
        """Searches one profile in its own browser tab; the task's result is the search result,
        or None if the search failed (already reported in the activity log)."""
        from app.services.search_service import search_service

        name = profile.get("name", "Search profile")

        async def on_progress(message: str):
            await self._event(ctx, f"“{name}”: {message}")

        async def run() -> Optional[Dict[str, Any]]:
            await self._event(ctx, f"Searching Dice for “{name}”…")
            try:
                return await search_service.run_search(
                    str(profile["_id"]), auto_match=True, auto_queue=False,
                    user_id=ctx.user_id, browser_profile_id=ctx.user_id, max_results=size,
                    on_progress=on_progress, fast=True,
                )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                detail = getattr(e, "detail", None) or str(e) or e.__class__.__name__
                await self._event(ctx, f"Search for “{name}” failed: {detail}", "warning")
                return None

        return asyncio.create_task(run())

    async def _await_search(self, ctx: _RunContext, task: asyncio.Task, name: str) -> Tuple[Optional[Dict[str, Any]], bool, Optional[Tuple[str, str]]]:
        """Waits for a search, applying to jobs found earlier in the meantime so the applying
        side never idles on Dice's search. Returns (search result, applied anything, end)."""
        applied_any = False
        await asyncio.wait({task}, timeout=SEARCH_HEAD_START_SECONDS)
        while not task.done():
            await self._pause_point(ctx)
            if ctx.end:
                return None, applied_any, ctx.end
            if ctx.submitted >= ctx.apply_limit:
                return None, applied_any, (AutomationRunStatus.COMPLETED, f"Target reached: applied to {ctx.submitted} job(s).")
            # One at a time: the search's fresh results take over as soon as they are in
            backlog = await self._candidate_jobs(ctx, limit=1)
            if not backlog:
                ctx.step = "SEARCHING"
                await self._set(ctx, phase="SCRAPING", current={"profile_name": name, "step": "SEARCHING"})
                await asyncio.wait({task})
                break
            applied_any = True
            ended = await self._apply_jobs(ctx, backlog, "Earlier jobs")
            if ended:
                return None, applied_any, ended
        return task.result(), applied_any, None

    async def _run_main(self, ctx: _RunContext) -> Tuple[str, str]:
        names = ", ".join(p.get("name", "") for p in ctx.profiles)
        await self._event(ctx, f"Automation started: {len(ctx.profiles)} profile(s) ({names}), target {ctx.apply_limit} application(s).")

        # Dice lists the newest jobs first, so a shallow search keeps returning jobs the user
        # already applied to. While target and time remain, search each profile again, deeper
        # each round, until a whole round turns up nothing new to apply to.
        # Spread the target over the profiles: each one only needs its share of candidates
        per_profile = -(-ctx.apply_limit * 2 // max(1, len(ctx.profiles)))
        scrape_size = min(FIRST_ROUND_MAX_SCRAPE, max(MIN_SCRAPE_PER_PROFILE, per_profile))
        round_no = 0
        search: Optional[asyncio.Task] = None
        try:
            while True:
                round_no += 1
                if round_no > 1:
                    await self._event(ctx, f"Searching deeper: up to {scrape_size} results per profile.")
                found_new = False

                await self._pause_point(ctx)
                if ctx.end:
                    return ctx.end
                search = self._start_search(ctx, ctx.profiles[0], scrape_size)
                for i, profile in enumerate(ctx.profiles):
                    name = profile.get("name", "Search profile")
                    res, applied_any, ended = await self._await_search(ctx, search, name)
                    found_new = found_new or applied_any
                    if ended:
                        return ended
                    # Pipeline: the next profile is searched in another tab while this one's jobs are applied to
                    search = (
                        self._start_search(ctx, ctx.profiles[i + 1], scrape_size)
                        if i + 1 < len(ctx.profiles) else None
                    )
                    if res is None:
                        continue

                    found = int(res.get("total_found", 0))
                    new = int(res.get("new_jobs_added", 0))
                    await self._set(ctx, inc={"scraped": new})
                    await self._event(ctx, f"“{name}”: {found} result(s), {new} new.")
                    jobs = await self._candidate_jobs(ctx, job_ids=res.get("new_job_ids") or [])
                    found_new = found_new or bool(jobs)
                    ended = await self._apply_jobs(ctx, jobs, name)
                    if ended:
                        return ended
                    await self._pause_point(ctx)
                    if ctx.end:
                        return ctx.end
                    if ctx.submitted >= ctx.apply_limit:
                        return AutomationRunStatus.COMPLETED, f"Target reached: applied to {ctx.submitted} job(s)."

                backlog = await self._candidate_jobs(ctx)
                if backlog:
                    found_new = True
                    await self._event(ctx, f"Working through {len(backlog)} earlier job(s) from these profiles.")
                    ended = await self._apply_jobs(ctx, backlog, "Earlier jobs")
                    if ended:
                        return ended

                if ctx.submitted >= ctx.apply_limit:
                    return AutomationRunStatus.COMPLETED, f"Target reached: applied to {ctx.submitted} job(s)."
                if not found_new or scrape_size >= MAX_SCRAPE_PER_PROFILE:
                    break
                scrape_size = min(MAX_SCRAPE_PER_PROFILE, scrape_size * 2)
        finally:
            if search and not search.done():
                search.cancel()

        return AutomationRunStatus.COMPLETED, (
            f"No more new jobs on Dice for these search profiles. Applied to {ctx.submitted} job(s)."
        )

    async def _pause_point(self, ctx: _RunContext):
        """Blocks here while the user has the run paused. Called between jobs/searches, so
        whatever was in progress finishes first. The time spent paused is added to the timer."""
        if not ctx.pause_requested or ctx.end:
            return
        paused_at = _utcnow()
        ctx.step = "PAUSED"
        await self._event(
            ctx, "Paused. The timer is frozen until you resume.",
            phase="PAUSED", current=None, paused=True, paused_at=paused_at,
        )
        # The watchdog stops checking the timer once paused_at is set
        ctx.paused_at = paused_at
        while ctx.pause_requested and not ctx.end:
            await asyncio.sleep(0.5)
        paused_for = _utcnow() - paused_at
        # Extend the deadline before un-pausing so the watchdog never sees a stale ends_at
        ctx.ends_at = ctx.ends_at + paused_for
        ctx.paused_at = None
        ctx.step = None
        await self.runs_col.update_one({"_id": ctx.run_id}, {"$inc": {"paused_seconds": int(paused_for.total_seconds())}})
        if ctx.end:
            await self._set(ctx, paused=False, paused_at=None, ends_at=ctx.ends_at)
            return
        await self._event(
            ctx, f"Resumed after {int(paused_for.total_seconds() // 60)} min {int(paused_for.total_seconds() % 60)} s.",
            phase="RESUMING", paused=False, paused_at=None, ends_at=ctx.ends_at,
        )

    async def _cooldown(self, ctx: _RunContext):
        deadline = asyncio.get_running_loop().time() + COOLDOWN_SECONDS
        while not ctx.end and not ctx.pause_requested and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(min(1.0, COOLDOWN_SECONDS))

    async def _apply_jobs(self, ctx: _RunContext, jobs: List[Dict[str, Any]], profile_name: str) -> Optional[Tuple[str, str]]:
        """Applies to jobs in order. Returns (status, reason) when the run should end, else None."""
        from app.services.application_service import application_service

        jobs_col = get_database().jobs
        for job in jobs:
            ctx.step = None
            if ctx.end:
                return ctx.end
            if ctx.submitted >= ctx.apply_limit:
                return AutomationRunStatus.COMPLETED, f"Target reached: applied to {ctx.submitted} job(s)."
            if job["_id"] in ctx.attempted:
                continue
            ctx.attempted.add(job["_id"])

            await self._pause_point(ctx)
            if ctx.end:
                return ctx.end

            try:
                await entitlement_service.assert_can_apply(ctx.user_id)
            except PlanLimitError as limit:
                return AutomationRunStatus.COMPLETED, f"Plan limit reached: {limit.detail}"

            # The job may have been applied to (or queued) elsewhere since it was listed
            job = await jobs_col.find_one({"_id": job["_id"], "status": {"$in": ELIGIBLE_JOB_STATUSES}})
            if not job:
                continue

            if ctx.attempts:
                ctx.step = "COOLDOWN"
                await self._set(ctx, **{"current.step": "COOLDOWN"})
                await self._cooldown(ctx)
                if ctx.end:
                    return ctx.end
            ctx.attempts += 1

            title = job.get("title") or "Untitled job"
            company = job.get("company") or "Company"
            ctx.step = "STARTING"
            await self._event(
                ctx, f"Applying: {title} at {company}",
                phase="APPLYING",
                current={
                    "profile_name": profile_name,
                    "job_id": str(job["_id"]),
                    "application_id": None,
                    "job_title": title,
                    "company": company,
                    "step": "STARTING",
                },
            )

            async def on_step(state: str):
                ctx.step = state
                await self._set(ctx, **{"current.step": state})

            started = asyncio.get_running_loop().time()
            try:
                app_id, res = await application_service.run_automated_application(job, ctx.user_id, ctx.run_id, on_step)
            except asyncio.CancelledError:
                raise
            except HTTPException as he:
                if he.status_code == 400 and "resume" in str(he.detail).lower():
                    return AutomationRunStatus.FAILED, str(he.detail)
                await self._set(ctx, inc={"failed": 1})
                await self._event(ctx, f"Skipped {title}: {he.detail}", "warning")
                continue
            except Exception as e:
                logger.error(f"Automation {ctx.run_id} could not apply to job {job['_id']}: {e}", exc_info=True)
                await self._set(ctx, inc={"failed": 1})
                await self._event(ctx, f"Skipped {title}: {e}", "warning")
                continue
            finally:
                ctx.step = "RECORDING"

            outcome = (res or {}).get("status")
            if (res or {}).get("submit_unconfirmed"):
                ctx.unconfirmed_streak.append(job["_id"])
            elif res is not None:
                ctx.unconfirmed_streak.clear()
            if res is None:
                await self._event(ctx, f"Skipped {title}: the application was stopped.", "warning")
            elif outcome in SUBMITTED_OUTCOMES:
                ctx.submitted += 1
                await self._set(ctx, inc={"submitted": 1})
                took = int(asyncio.get_running_loop().time() - started)
                await self._event(ctx, f"Applied to {title} at {company} ({ctx.submitted}/{ctx.apply_limit}) in {took}s.", "success")
            elif outcome in REVIEW_OUTCOMES:
                await self._set(ctx, inc={"review": 1})
                await self._event(ctx, f"{title} needs your input. Skipped and added to the Review Queue.", "warning")
            elif outcome == ApplicationState.READY:
                await application_service.add_review_item(ObjectId(app_id), READY_REVIEW_TEXT, "manual_finish", ["Done"])
                await self._set(ctx, inc={"review": 1})
                await self._event(ctx, f"{title} couldn't be finished automatically. Added to the Review Queue.", "warning")
            elif outcome == ApplicationState.SKIPPED:
                await self._set(ctx, inc={"failed": 1})
                await self._event(ctx, f"Skipped {title}: Dice already shows it as applied, so nothing was submitted.", "warning")
            elif outcome in LOGIN_OUTCOMES:
                # The job itself didn't fail; leave it eligible for the next run
                await jobs_col.update_one(
                    {"_id": job["_id"], "status": "FAILED"},
                    {"$set": {"status": "MATCHED", "failure_reason": None, "updated_at": _utcnow()}},
                )
                return AutomationRunStatus.LOGIN_REQUIRED, LOGIN_LOST_REASON
            else:
                await self._set(ctx, inc={"failed": 1})
                reason = (res or {}).get("failure_reason") or outcome or "Application failed"
                await self._event(ctx, f"Skipped {title}: {reason}", "warning")
                if len(ctx.unconfirmed_streak) >= MAX_UNCONFIRMED_STREAK:
                    return await self._end_unconfirmed_streak(ctx)
        ctx.step = None
        return None

    async def _end_unconfirmed_streak(self, ctx: _RunContext) -> Tuple[str, str]:
        """Dice refused several submits in a row. Those jobs most likely weren't applied to, so put
        them back in the eligible pool instead of leaving them FAILED (never retried)."""
        await get_database().jobs.update_many(
            {"_id": {"$in": ctx.unconfirmed_streak}, "status": "FAILED"},
            {"$set": {"status": "MATCHED", "failure_reason": None, "updated_at": _utcnow()}},
        )
        return AutomationRunStatus.FAILED, UNCONFIRMED_STREAK_REASON.format(n=len(ctx.unconfirmed_streak))


automation_service = AutomationService()
