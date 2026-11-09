"""
What a user's plan allows and how much of it they've used.

Paid plans: N applications per day, resetting at midnight in the user's own timezone
(sent by the browser, stored on the billing account; BILLING_TIMEZONE until we know it).
Trial: N applications in total until the trial ends.
An application counts once it is submitted, or while it is queued / in flight, so a large
batch can't overshoot the allowance before any of it finishes.
"""
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from bson import ObjectId
from fastapi import HTTPException, status

from app.config import settings
from app.database import get_database
from app.services.billing.billing_repository import billing_repository
from app.services.billing.plan_catalog import Plan, plan_catalog, trial_plan
from app.services.billing.subscription_service import is_entitled

logger = logging.getLogger(__name__)

SUBMITTED_STATUSES = ["APPLIED", "SUBMITTED"]
IN_FLIGHT_STATUSES = [
    "QUEUED", "APPLYING", "STARTING", "OPENING_JOB", "FILLING_APPLICATION", "UPLOADING_RESUME", "SUBMITTING",
]


class PlanLimitError(HTTPException):
    """402 Payment Required: the frontend shows an upgrade prompt for this status."""

    def __init__(self, message: str):
        super().__init__(status.HTTP_402_PAYMENT_REQUIRED, message)


@dataclass
class Entitlement:
    plan: Optional[Plan]
    source: str  # "subscription" | "trial" | "none"
    account: Dict[str, Any]
    window_start: Optional[datetime]
    window_end: Optional[datetime]

    @property
    def application_limit(self) -> int:
        if not self.plan:
            return 0
        return self.plan.total_application_limit if self.plan.is_trial else self.plan.daily_application_limit

    @property
    def search_profile_limit(self) -> int:
        return self.plan.search_profile_limit if self.plan else 0


def as_utc(dt: Optional[datetime]) -> Optional[datetime]:
    return dt.replace(tzinfo=timezone.utc) if dt and dt.tzinfo is None else dt


def _user_filter(user_id: str) -> Dict[str, Any]:
    ids: list = [user_id]
    if ObjectId.is_valid(user_id):
        ids.append(ObjectId(user_id))
    return {"user_id": {"$in": ids}}


def valid_timezone(name: Optional[str]) -> Optional[str]:
    """Returns `name` if it is a real IANA timezone (e.g. "Asia/Kolkata"), else None."""
    if not name or len(name) > 64:
        return None
    try:
        ZoneInfo(name)
        return name
    except (ZoneInfoNotFoundError, ValueError):
        return None


def _today_window(now: datetime, tz_name: Optional[str] = None) -> tuple[datetime, datetime]:
    tz = ZoneInfo(valid_timezone(tz_name) or settings.BILLING_TIMEZONE)
    start = now.astimezone(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    # Add the day in local time, then re-resolve the offset so DST-change days stay midnight to midnight
    end = (start.replace(tzinfo=None) + timedelta(days=1)).replace(tzinfo=tz)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


async def _paid_plan(subscription: Dict[str, Any]) -> Optional[Plan]:
    try:
        plan = await plan_catalog.resolve_product(subscription.get("product_id"))
        if plan:
            return plan
    except Exception as e:  # Stripe unreachable: fall back to the stored snapshot
        logger.warning("Plan catalog unavailable, using stored limits: %s", e)
    if subscription.get("daily_application_limit") is None:
        return None
    return Plan(
        key=subscription.get("plan_key") or "paid",
        name=subscription.get("plan_name") or "Paid plan",
        description="",
        product_id=subscription.get("product_id"),
        price_id=subscription.get("price_id"),
        unit_amount=0,
        currency="usd",
        interval="month",
        daily_application_limit=subscription["daily_application_limit"],
        total_application_limit=None,
        search_profile_limit=subscription.get("search_profile_limit") or 1,
    )


class EntitlementService:
    async def get(self, user_id: str) -> Entitlement:
        account = await billing_repository.get_or_create(user_id)
        now = datetime.now(timezone.utc)
        subscription = account.get("subscription")

        if is_entitled(subscription):
            plan = await _paid_plan(subscription)
            if plan:
                return Entitlement(plan, "subscription", account, *_today_window(now, account.get("timezone")))

        trial_start, trial_end = as_utc(account.get("trial_started_at")), as_utc(account.get("trial_ends_at"))
        if trial_end and now < trial_end:
            return Entitlement(trial_plan(), "trial", account, trial_start, trial_end)

        return Entitlement(None, "none", account, None, None)

    async def applications_used(self, user_id: str, ent: Entitlement) -> int:
        if not ent.window_start:
            return 0
        return await get_database().applications.count_documents({
            **_user_filter(user_id),
            "$or": [
                {"status": {"$in": IN_FLIGHT_STATUSES}},
                {"status": {"$in": SUBMITTED_STATUSES}, "applied_at": {"$gte": ent.window_start}},
                {"status": {"$in": SUBMITTED_STATUSES}, "applied_at": None, "updated_at": {"$gte": ent.window_start}},
            ],
        })

    async def search_profiles_used(self, user_id: str) -> int:
        return await get_database().search_profiles.count_documents(_user_filter(user_id))

    async def assert_can_apply(self, user_id: str) -> None:
        ent = await self.get(user_id)
        if ent.source == "none":
            raise PlanLimitError("Your free trial has ended. Choose a plan to keep applying.")
        used = await self.applications_used(user_id, ent)
        if used >= ent.application_limit:
            if ent.source == "trial":
                raise PlanLimitError(f"You've used all {ent.application_limit} trial applications. Upgrade to keep applying.")
            raise PlanLimitError(
                f"You've reached today's limit of {ent.application_limit} applications on the {ent.plan.name} plan. "
                "It resets at midnight, or upgrade for a higher limit."
            )

    async def assert_can_create_search_profile(self, user_id: str) -> None:
        ent = await self.get(user_id)
        if ent.source == "none":
            raise PlanLimitError("Your free trial has ended. Choose a plan to create search profiles.")
        if await self.search_profiles_used(user_id) >= ent.search_profile_limit:
            raise PlanLimitError(
                f"Your {ent.plan.name} plan includes {ent.search_profile_limit} search profile(s). "
                "Delete one or upgrade to add more."
            )


entitlement_service = EntitlementService()
