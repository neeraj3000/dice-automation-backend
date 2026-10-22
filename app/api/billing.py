import logging
from dataclasses import asdict
from typing import List

from fastapi import APIRouter, Depends, Header, Request

from app.core.deps import get_current_user, rate_limit
from app.schemas.billing import (
    BillingStatusOut,
    CheckoutConfirmIn,
    CheckoutIn,
    InvoiceOut,
    PlanOut,
    RedirectOut,
    SubscriptionOut,
    UsageOut,
)
from app.services.billing import entitlement_service, plan_catalog, subscription_service
from app.services.billing.entitlement_service import as_utc
from app.services.billing.stripe_gateway import construct_event
from app.services.billing.webhook_service import handle_event

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/billing", tags=["Billing"])
limiter = Depends(rate_limit(20, 60))


@router.get("/plans", response_model=List[PlanOut])
async def list_plans():
    return [PlanOut(**asdict(p)) for p in await plan_catalog.all_plans()]


@router.get("/status", response_model=BillingStatusOut)
async def billing_status(user: dict = Depends(get_current_user)):
    user_id = str(user["_id"])
    ent = await entitlement_service.get(user_id)
    sub = ent.account.get("subscription")
    return BillingStatusOut(
        source=ent.source,
        plan=PlanOut(**asdict(ent.plan)) if ent.plan else None,
        subscription=SubscriptionOut(**{k: sub.get(k) for k in SubscriptionOut.model_fields}) if sub else None,
        trial_ends_at=as_utc(ent.account.get("trial_ends_at")),
        has_billing_account=bool(ent.account.get("stripe_customer_id")),
        applications=UsageOut(
            used=await entitlement_service.applications_used(user_id, ent),
            limit=ent.application_limit,
            period={"subscription": "day", "trial": "trial"}.get(ent.source),
            resets_at=ent.window_end if ent.source == "subscription" else None,
        ),
        search_profiles=UsageOut(
            used=await entitlement_service.search_profiles_used(user_id),
            limit=ent.search_profile_limit,
            period=None,
            resets_at=None,
        ),
    )


@router.get("/invoices", response_model=List[InvoiceOut], dependencies=[limiter])
async def list_invoices(user: dict = Depends(get_current_user)):
    return [InvoiceOut(**inv) for inv in await subscription_service.list_invoices(user)]


@router.post("/checkout-session", response_model=RedirectOut, dependencies=[limiter])
async def create_checkout_session(body: CheckoutIn, user: dict = Depends(get_current_user)):
    return RedirectOut(url=await subscription_service.create_checkout_session(user, body.price_id))


@router.post("/checkout-session/confirm", status_code=204, dependencies=[limiter])
async def confirm_checkout_session(body: CheckoutConfirmIn, user: dict = Depends(get_current_user)):
    await subscription_service.confirm_checkout_session(user, body.session_id)


@router.post("/portal-session", response_model=RedirectOut, dependencies=[limiter])
async def create_portal_session(user: dict = Depends(get_current_user)):
    return RedirectOut(url=await subscription_service.create_portal_session(user))


@router.post("/webhook", include_in_schema=False)
async def stripe_webhook(request: Request, stripe_signature: str | None = Header(default=None)):
    event = construct_event(await request.body(), stripe_signature)
    await handle_event(event)
    return {"received": True}
