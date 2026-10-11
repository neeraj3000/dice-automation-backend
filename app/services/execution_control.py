"""
Cancellation control for in-flight application automations.

A stop request is recorded in MongoDB (`cancel_requested` + CANCELLED status), which is
the source of truth across processes. Each process additionally keeps a registry of the
asyncio tasks it is running so a stop issued to the same process cancels immediately;
a lightweight watcher polls the DB flag so a stop issued to a *different* worker/replica
still reaches the run within CANCEL_POLL_INTERVAL seconds.
"""
import asyncio
import logging
from typing import Any, Awaitable, Dict, Optional, Tuple, TypeVar

from bson import ObjectId

from app.database import get_database

logger = logging.getLogger(__name__)

T = TypeVar("T")

CANCEL_POLL_INTERVAL = 2.0

# Mongo filter fragment: only write runner progress while no stop has been requested,
# so a late status update can never overwrite a CANCELLED record.
NOT_CANCELLED = {"cancel_requested": {"$ne": True}}


class ExecutionControl:
    def __init__(self):
        self._tasks: Dict[str, asyncio.Task] = {}

    def is_running_locally(self, app_id: str) -> bool:
        task = self._tasks.get(app_id)
        return task is not None and not task.done()

    def cancel_local(self, app_id: str) -> bool:
        """Cancels the run for app_id if this process owns it. Returns True if a task was cancelled."""
        task = self._tasks.get(app_id)
        if task is None or task.done():
            return False
        task.cancel()
        logger.info(f"Cancellation signalled to running application {app_id}")
        return True

    async def _is_cancel_requested(self, app_id: str) -> bool:
        doc = await get_database().applications.find_one(
            {"_id": ObjectId(app_id)}, {"cancel_requested": 1}
        )
        return bool(doc and doc.get("cancel_requested"))

    async def _watch_db_flag(self, app_id: str, task: asyncio.Task):
        """Cancels `task` once the DB flag is set (covers stops received by another process)."""
        while not task.done():
            await asyncio.sleep(CANCEL_POLL_INTERVAL)
            try:
                if await self._is_cancel_requested(app_id):
                    task.cancel()
                    return
            except Exception as e:
                logger.debug(f"Cancel watcher poll failed for {app_id}: {e}")

    async def run_cancellable(self, app_id: str, coro: Awaitable[T]) -> Tuple[bool, Optional[T]]:
        """
        Runs `coro` as a child task that can be stopped via cancel_local() or the DB flag.
        Returns (cancelled, result). Cancellation of the *caller* (e.g. shutdown) still
        propagates normally and tears the child down.
        """
        task = asyncio.ensure_future(coro)
        self._tasks[app_id] = task
        watcher = asyncio.create_task(self._watch_db_flag(app_id, task)) if ObjectId.is_valid(app_id) else None
        try:
            await asyncio.wait({task})
        except asyncio.CancelledError:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise
        finally:
            if watcher:
                watcher.cancel()
            if self._tasks.get(app_id) is task:
                self._tasks.pop(app_id, None)

        if task.cancelled():
            logger.info(f"Application {app_id} execution stopped.")
            return True, None
        return False, task.result()


execution_control = ExecutionControl()
