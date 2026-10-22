"""MongoDB access for billing: one `billing_accounts` doc per user, plus processed webhook events."""
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from app.config import settings
from app.database import get_database

# Webhook event ids are kept long enough to cover Stripe's 3-day retry window
EVENT_RETENTION = timedelta(days=30)


def _now() -> datetime:
    return datetime.now(timezone.utc)


class BillingRepository:
    @property
    def accounts(self):
        return get_database().billing_accounts

    @property
    def events(self):
        return get_database().stripe_events

    async def ensure_indexes(self):
        await self.accounts.create_index([("user_id", 1)], unique=True)
        await self.accounts.create_index([("stripe_customer_id", 1)], sparse=True)
        await self.accounts.create_index([("subscription.id", 1)], sparse=True)
        await self.events.create_index([("event_id", 1)], unique=True)
        await self.events.create_index([("expires_at", 1)], expireAfterSeconds=0)

    async def get_or_create(self, user_id: str) -> Dict[str, Any]:
        """Returns the user's billing account, starting their free trial on first access."""
        now = _now()
        return await self.accounts.find_one_and_update(
            {"user_id": user_id},
            {"$setOnInsert": {
                "user_id": user_id,
                "trial_started_at": now,
                "trial_ends_at": now + timedelta(days=settings.TRIAL_DAYS),
                "subscription": None,
                "created_at": now,
                "updated_at": now,
            }},
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )

    async def find_by_customer(self, customer_id: str) -> Optional[Dict[str, Any]]:
        return await self.accounts.find_one({"stripe_customer_id": customer_id})

    async def set_customer_if_missing(self, user_id: str, customer_id: str) -> str:
        """Stores the Stripe customer id unless one is already set; returns the winning id."""
        doc = await self.accounts.find_one_and_update(
            {"user_id": user_id, "stripe_customer_id": {"$exists": False}},
            {"$set": {"stripe_customer_id": customer_id, "updated_at": _now()}},
            return_document=ReturnDocument.AFTER,
        )
        if doc:
            return customer_id
        existing = await self.accounts.find_one({"user_id": user_id}, {"stripe_customer_id": 1})
        return existing["stripe_customer_id"]

    async def replace_subscription(
        self, user_id: str, customer_id: str, subscription: Dict[str, Any], expected_id: Optional[str]
    ) -> bool:
        """Compare-and-set: stores the snapshot only if the stored subscription is still `expected_id`."""
        res = await self.accounts.update_one(
            {"user_id": user_id, "subscription.id" if expected_id else "subscription": expected_id},
            {"$set": {"stripe_customer_id": customer_id, "subscription": subscription, "updated_at": _now()}},
        )
        return res.modified_count == 1

    async def mark_event_processed(self, event_id: str, event_type: str) -> bool:
        """Records a webhook event; False when it was already handled (Stripe retries)."""
        now = _now()
        try:
            await self.events.insert_one({
                "event_id": event_id, "type": event_type, "processed_at": now, "expires_at": now + EVENT_RETENTION,
            })
            return True
        except DuplicateKeyError:
            return False

    async def unmark_event(self, event_id: str):
        """Lets Stripe's retry reprocess an event whose handler failed."""
        await self.events.delete_one({"event_id": event_id})


billing_repository = BillingRepository()
