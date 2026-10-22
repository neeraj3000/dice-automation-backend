"""
Pushes small "something changed" events to a user's open tabs over the existing SSE stream
(/settings/dice-session/stream), so the frontend refetches only when data actually changes
instead of polling on a timer.

Events carry no data beyond their type; the frontend refetches the matching endpoint:
    AUTOMATION_UPDATED  -> /automation-runs/active
    BILLING_UPDATED     -> /billing/status
"""
import logging

logger = logging.getLogger(__name__)

AUTOMATION_UPDATED = "AUTOMATION_UPDATED"
BILLING_UPDATED = "BILLING_UPDATED"


async def notify_user(user_id, event_type: str) -> None:
    """Best effort: a missed event is covered by the frontend's refetch on focus/reconnect."""
    if not user_id:
        return
    try:
        # Imported lazily: the session manager pulls in browser/settings modules
        from app.services.dice_session_manager import dice_session_manager
        await dice_session_manager.notify_user(str(user_id), event_type, {})
    except Exception as e:
        logger.debug("Realtime notify failed for %s (%s): %s", user_id, event_type, e)
