"""
Plan catalog, read from Stripe.

A Stripe Product is an Apply2Hire plan when its metadata has `plan_key`. Its limits come
from metadata too, and its price from the product's default price, so plans are created
and changed in the Stripe dashboard without a code change:

    plan_key                 starter | pro | premium | ultimate   (any unique slug)
    daily_application_limit  10
    search_profile_limit     1
    highlight                true   (optional: marks the "most popular" card)
"""
import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from app.config import settings
from app.services.billing.stripe_gateway import as_dict, get_stripe

logger = logging.getLogger(__name__)

TRIAL_PLAN_KEY = "trial"


@dataclass(frozen=True)
class Plan:
    key: str
    name: str
    description: str
    product_id: Optional[str]
    price_id: Optional[str]
    unit_amount: int  # minor units (cents)
    currency: str
    interval: Optional[str]
    daily_application_limit: Optional[int]  # None for the trial (it has a total instead)
    total_application_limit: Optional[int]
    search_profile_limit: int
    features: List[str] = field(default_factory=list)
    highlight: bool = False
    is_trial: bool = False


def trial_plan() -> Plan:
    return Plan(
        key=TRIAL_PLAN_KEY,
        name="Basic",
        description=f"{settings.TRIAL_DAYS}-day free trial",
        product_id=None,
        price_id=None,
        unit_amount=0,
        currency="usd",
        interval=None,
        daily_application_limit=None,
        total_application_limit=settings.TRIAL_APPLICATION_LIMIT,
        search_profile_limit=settings.TRIAL_SEARCH_PROFILE_LIMIT,
        is_trial=True,
    )


def _int_meta(meta: Dict[str, str], key: str) -> Optional[int]:
    try:
        return int(str(meta.get(key, "")).strip())
    except ValueError:
        return None


def _plan_from_product(product: Dict) -> Optional[Plan]:
    meta = product.get("metadata") or {}
    price = product.get("default_price")
    if not meta.get("plan_key") or not isinstance(price, dict):
        return None
    if not price.get("active") or price.get("type") != "recurring":
        logger.warning("Stripe product %s has no active recurring default price; skipped.", product.get("id"))
        return None

    daily = _int_meta(meta, "daily_application_limit")
    profiles = _int_meta(meta, "search_profile_limit")
    if daily is None or profiles is None:
        logger.warning("Stripe product %s is missing limit metadata; skipped.", product.get("id"))
        return None

    return Plan(
        key=meta["plan_key"].strip().lower(),
        name=product.get("name") or meta["plan_key"],
        description=product.get("description") or "",
        product_id=product["id"],
        price_id=price["id"],
        unit_amount=price.get("unit_amount") or 0,
        currency=price.get("currency") or "usd",
        interval=(price.get("recurring") or {}).get("interval"),
        daily_application_limit=daily,
        total_application_limit=None,
        search_profile_limit=profiles,
        features=[f.get("name") for f in product.get("marketing_features") or [] if f.get("name")],
        highlight=str(meta.get("highlight", "")).lower() == "true",
    )


class PlanCatalog:
    """Paid plans from Stripe, cached in memory for STRIPE_PLAN_CACHE_SECONDS."""

    def __init__(self):
        self._plans: List[Plan] = []
        self._loaded_at = 0.0
        self._lock = asyncio.Lock()

    def invalidate(self):
        self._loaded_at = 0.0

    async def paid_plans(self) -> List[Plan]:
        if time.monotonic() - self._loaded_at < settings.STRIPE_PLAN_CACHE_SECONDS:
            return self._plans
        async with self._lock:
            if time.monotonic() - self._loaded_at < settings.STRIPE_PLAN_CACHE_SECONDS:
                return self._plans
            plans: List[Plan] = []
            stripe = get_stripe()
            params = {"active": True, "limit": 100, "expand": ["data.default_price"]}
            while True:
                page = as_dict(await stripe.v1.products.list_async(params))
                plans.extend(p for p in map(_plan_from_product, page.get("data", [])) if p)
                if not page.get("has_more"):
                    break
                params["starting_after"] = page["data"][-1]["id"]
            plans.sort(key=lambda p: p.unit_amount)
            self._plans, self._loaded_at = plans, time.monotonic()
            return plans

    async def all_plans(self) -> List[Plan]:
        return [trial_plan(), *await self.paid_plans()]

    async def by_price_id(self, price_id: str) -> Optional[Plan]:
        return next((p for p in await self.paid_plans() if p.price_id == price_id), None)

    async def by_product_id(self, product_id: str) -> Optional[Plan]:
        return next((p for p in await self.paid_plans() if p.product_id == product_id), None)


plan_catalog = PlanCatalog()
