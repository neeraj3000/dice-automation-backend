"""Stripe Checkout, Customer Portal, and syncing Stripe subscriptions into MongoDB."""
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import HTTPException, status

from app.config import settings
from app.services.billing.billing_repository import billing_repository
from app.services.billing.plan_catalog import plan_catalog
from app.services.billing.stripe_gateway import as_dict, get_stripe

logger = logging.getLogger(__name__)

# Statuses that keep plan access. past_due stays entitled while Stripe retries the payment.
ENTITLED_STATUSES = {"active", "trialing", "past_due"}


def _ts(value: Optional[int]) -> Optional[datetime]:
    return datetime.fromtimestamp(value, tz=timezone.utc) if value else None


def is_entitled(subscription: Optional[Dict[str, Any]]) -> bool:
    return bool(subscription) and subscription.get("status") in ENTITLED_STATUSES


def _billing_url(query: str = "") -> str:
    return f"{settings.FRONTEND_URL.rstrip('/')}/billing{query}"


class SubscriptionService:
    async def _snapshot(self, sub: Dict[str, Any]) -> Dict[str, Any]:
        item = ((sub.get("items") or {}).get("data") or [{}])[0]
        price = item.get("price") or {}
        product_id = price.get("product") if isinstance(price.get("product"), str) else (price.get("product") or {}).get("id")
        plan = await plan_catalog.by_product_id(product_id) if product_id else None
        # Newer API versions report billing periods per subscription item
        period_end = item.get("current_period_end") or sub.get("current_period_end")
        return {
            "id": sub["id"],
            "status": sub.get("status"),
            "price_id": price.get("id"),
            "product_id": product_id,
            "current_period_end": _ts(period_end),
            "cancel_at_period_end": bool(sub.get("cancel_at_period_end")),
            "cancel_at": _ts(sub.get("cancel_at")),
            # Limits are re-read from the catalog at check time; this copy is the fallback
            # when Stripe is unreachable.
            "plan_key": plan.key if plan else None,
            "plan_name": plan.name if plan else None,
            "daily_application_limit": plan.daily_application_limit if plan else None,
            "search_profile_limit": plan.search_profile_limit if plan else None,
            "synced_at": datetime.now(timezone.utc),
        }

    async def _user_id_for(self, sub: Dict[str, Any]) -> Optional[str]:
        user_id = (sub.get("metadata") or {}).get("user_id")
        if user_id:
            return user_id
        account = await billing_repository.find_by_customer(sub.get("customer"))
        return account["user_id"] if account else None

    async def sync_subscription(self, subscription_id: str) -> None:
        """Re-reads the subscription from Stripe (never trusting event payload order) and stores it."""
        sub = as_dict(await get_stripe().v1.subscriptions.retrieve_async(subscription_id))
        user_id = await self._user_id_for(sub)
        if not user_id:
            logger.warning("Stripe subscription %s has no matching user; ignored.", subscription_id)
            return
        snapshot = await self._snapshot(sub)

        for _ in range(3):  # compare-and-set retries against concurrent webhooks
            account = await billing_repository.get_or_create(user_id)
            stored = account.get("subscription")
            # A late event about an ended subscription must not replace the user's live one
            if stored and stored["id"] != snapshot["id"] and is_entitled(stored) and not is_entitled(snapshot):
                return
            if await billing_repository.replace_subscription(
                user_id, sub["customer"], snapshot, stored["id"] if stored else None
            ):
                return
        logger.error("Could not store subscription %s for user %s after retries.", subscription_id, user_id)

    async def _ensure_customer(self, user: Dict[str, Any], account: Dict[str, Any]) -> str:
        if account.get("stripe_customer_id"):
            return account["stripe_customer_id"]
        user_id = str(user["_id"])
        customer = as_dict(await get_stripe().v1.customers.create_async(
            {"email": user.get("email"), "name": user.get("name") or None, "metadata": {"user_id": user_id}},
            {"idempotency_key": f"a2h-customer-{user_id}"},
        ))
        return await billing_repository.set_customer_if_missing(user_id, customer["id"])

    async def create_checkout_session(self, user: Dict[str, Any], price_id: str) -> str:
        plan = await plan_catalog.by_price_id(price_id)
        if not plan:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "That plan is not available.")

        user_id = str(user["_id"])
        account = await billing_repository.get_or_create(user_id)
        if is_entitled(account.get("subscription")):
            # Plan changes go through the Customer Portal so Stripe handles proration
            raise HTTPException(status.HTTP_409_CONFLICT, "You already have an active subscription. Use Manage billing to change plans.")

        customer_id = await self._ensure_customer(user, account)
        session = as_dict(await get_stripe().v1.checkout.sessions.create_async({
            "mode": "subscription",
            "customer": customer_id,
            "client_reference_id": user_id,
            "line_items": [{"price": plan.price_id, "quantity": 1}],
            "subscription_data": {"metadata": {"user_id": user_id, "plan_key": plan.key}},
            "allow_promotion_codes": True,
            "success_url": _billing_url("?checkout=success&session_id={CHECKOUT_SESSION_ID}"),
            "cancel_url": _billing_url("?checkout=cancelled"),
        }))
        return session["url"]

    async def confirm_checkout_session(self, user: Dict[str, Any], session_id: str) -> None:
        """Called on the success redirect so the UI updates even if the webhook is still in flight."""
        session = as_dict(await get_stripe().v1.checkout.sessions.retrieve_async(session_id))
        if session.get("client_reference_id") != str(user["_id"]):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Checkout session not found")
        if session.get("subscription"):
            await self.sync_subscription(session["subscription"])

    async def list_invoices(self, user: Dict[str, Any], limit: int = 24) -> List[Dict[str, Any]]:
        account = await billing_repository.get_or_create(str(user["_id"]))
        customer_id = account.get("stripe_customer_id")
        if not customer_id:
            return []
        page = as_dict(await get_stripe().v1.invoices.list_async({"customer": customer_id, "limit": limit}))
        invoices = []
        for inv in page.get("data") or []:
            if inv.get("status") in ("draft", "void"):
                continue
            line = ((inv.get("lines") or {}).get("data") or [{}])[0]
            period = line.get("period") or {}
            # Newer API versions put the product under pricing.price_details; older ones under price
            product_id = ((line.get("pricing") or {}).get("price_details") or {}).get("product")
            if not product_id:
                product = (line.get("price") or {}).get("product")
                product_id = product if isinstance(product, str) else (product or {}).get("id")
            plan = await plan_catalog.by_product_id(product_id) if product_id else None
            invoices.append({
                "id": inv["id"],
                "number": inv.get("number"),
                "status": inv.get("status"),
                "plan_name": plan.name if plan else None,
                "description": line.get("description"),
                "amount": inv.get("amount_paid") if inv.get("status") == "paid" else inv.get("amount_due") or 0,
                "currency": inv.get("currency") or "usd",
                "created": _ts(inv.get("created")),
                "period_start": _ts(period.get("start")),
                "period_end": _ts(period.get("end")),
                "hosted_invoice_url": inv.get("hosted_invoice_url"),
                "invoice_pdf": inv.get("invoice_pdf"),
            })
        return invoices

    async def create_portal_session(self, user: Dict[str, Any]) -> str:
        account = await billing_repository.get_or_create(str(user["_id"]))
        if not account.get("stripe_customer_id"):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "No billing account yet. Choose a plan first.")
        session = as_dict(await get_stripe().v1.billing_portal.sessions.create_async({
            "customer": account["stripe_customer_id"],
            "return_url": _billing_url(),
        }))
        return session["url"]


subscription_service = SubscriptionService()
