"""Handles Stripe webhook events. Each event is processed at most once (Stripe retries on failure)."""
import logging
from typing import Any, Dict

from app.services.billing.billing_repository import billing_repository
from app.services.billing.plan_catalog import plan_catalog
from app.services.billing.subscription_service import subscription_service

logger = logging.getLogger(__name__)

SUBSCRIPTION_EVENTS = {
    "customer.subscription.created",
    "customer.subscription.updated",
    "customer.subscription.deleted",
    "customer.subscription.paused",
    "customer.subscription.resumed",
}
INVOICE_EVENTS = {"invoice.paid", "invoice.payment_failed"}
CATALOG_EVENTS = {"product.created", "product.updated", "product.deleted", "price.created", "price.updated", "price.deleted"}


def _invoice_subscription_id(invoice: Dict[str, Any]) -> str | None:
    # Newer API versions nest it under parent.subscription_details
    details = (invoice.get("parent") or {}).get("subscription_details") or {}
    sub = details.get("subscription") or invoice.get("subscription")
    return sub if isinstance(sub, str) else (sub or {}).get("id")


async def handle_event(event: Dict[str, Any]) -> None:
    event_id, event_type = event["id"], event["type"]
    obj = event["data"]["object"]

    if not await billing_repository.mark_event_processed(event_id, event_type):
        logger.info("Stripe event %s already processed; skipped.", event_id)
        return

    try:
        if event_type in CATALOG_EVENTS:
            plan_catalog.invalidate()
        elif event_type == "checkout.session.completed" and obj.get("mode") == "subscription":
            await subscription_service.sync_subscription(obj["subscription"])
        elif event_type in SUBSCRIPTION_EVENTS:
            await subscription_service.sync_subscription(obj["id"])
        elif event_type in INVOICE_EVENTS:
            sub_id = _invoice_subscription_id(obj)
            if sub_id:
                await subscription_service.sync_subscription(sub_id)
    except Exception:
        await billing_repository.unmark_event(event_id)
        raise
