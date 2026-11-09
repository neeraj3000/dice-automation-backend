"""
Plan catalog, read from Stripe.

A Stripe Product is an Apply2Hire plan when its metadata has `plan_key`. Its limits come
from metadata too, and its price from the product's default price, so plans are created
and changed in the Stripe dashboard without a code change:

    plan_key                 starter | pro | premium   (any unique slug)
    daily_application_limit  10     (whole number >= 1)
    search_profile_limit     1      (whole number >= 1; "Resume Profiles" in marketing copy)
    highlight                true   (optional: marks the "most popular" card)

Archiving a plan's Product hides it from the plan list, but people already subscribed to
it keep its limits: `resolve_product` still reads an archived Product's metadata.
"""
import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import stripe

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


def _positive_int_meta(meta: Dict[str, str], key: str) -> Optional[int]:
    try:
        value = int(str(meta.get(key, "")).strip())
    except ValueError:
        return None
    return value if value >= 1 else None


def _plan_from_product(product: Dict, require_active_price: bool = True) -> Optional[Plan]:
    """Builds a Plan from a Stripe Product, or None if it isn't a valid plan.

    `require_active_price=False` is for products that are already subscribed to: an archived
    product (or one whose default price was replaced) still defines its subscribers' limits.
    """
    meta = product.get("metadata") or {}
    plan_key = str(meta.get("plan_key") or "").strip().lower()
    if not plan_key:
        return None
    if plan_key == TRIAL_PLAN_KEY:
        logger.warning("Stripe product %s uses reserved plan_key %r; skipped.", product.get("id"), plan_key)
        return None

    price = product.get("default_price")
    price = price if isinstance(price, dict) else {}
    if require_active_price and (not price.get("active") or price.get("type") != "recurring"):
        logger.warning("Stripe product %s has no active recurring default price; skipped.", product.get("id"))
        return None

    daily = _positive_int_meta(meta, "daily_application_limit")
    profiles = _positive_int_meta(meta, "search_profile_limit")
    if daily is None or profiles is None:
        logger.warning(
            "Stripe product %s needs whole-number daily_application_limit and search_profile_limit "
            "metadata (>= 1); skipped.", product.get("id"),
        )
        return None

    return Plan(
        key=plan_key,
        name=product.get("name") or plan_key,
        description=product.get("description") or "",
        product_id=product["id"],
        price_id=price.get("id"),
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
        # Archived products that still have subscribers: product_id -> (Plan | None, loaded_at)
        self._archived: Dict[str, tuple[Optional[Plan], float]] = {}

    def invalidate(self):
        self._loaded_at = 0.0
        self._archived.clear()

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
            plans.sort(key=lambda p: (p.unit_amount, p.daily_application_limit or 0))
            # plan_key identifies a plan to the frontend; two live products with one key is a dashboard mistake
            unique: Dict[str, Plan] = {}
            for plan in plans:
                if plan.key in unique:
                    logger.error(
                        "Stripe products %s and %s share plan_key %r; only the first is offered.",
                        unique[plan.key].product_id, plan.product_id, plan.key,
                    )
                    continue
                unique[plan.key] = plan
            self._plans, self._loaded_at = list(unique.values()), time.monotonic()
            return self._plans

    async def all_plans(self) -> List[Plan]:
        return [trial_plan(), *await self.paid_plans()]

    async def by_price_id(self, price_id: str) -> Optional[Plan]:
        return next((p for p in await self.paid_plans() if p.price_id == price_id), None)

    async def by_product_id(self, product_id: str) -> Optional[Plan]:
        return next((p for p in await self.paid_plans() if p.product_id == product_id), None)

    async def resolve_product(self, product_id: Optional[str]) -> Optional[Plan]:
        """The plan a subscription's product grants, including archived (no longer sold) plans."""
        if not product_id:
            return None
        plan = await self.by_product_id(product_id)
        if plan:
            return plan
        cached = self._archived.get(product_id)
        if cached and time.monotonic() - cached[1] < settings.STRIPE_PLAN_CACHE_SECONDS:
            return cached[0]
        try:
            product = as_dict(await get_stripe().v1.products.retrieve_async(product_id, {"expand": ["default_price"]}))
        except stripe.InvalidRequestError:  # deleted product: nothing to read limits from
            logger.warning("Stripe product %s not found while resolving a subscription's plan.", product_id)
            product = {}
        plan = _plan_from_product(product, require_active_price=False) if product else None
        self._archived[product_id] = (plan, time.monotonic())
        return plan


plan_catalog = PlanCatalog()
