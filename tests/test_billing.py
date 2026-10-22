"""
Billing: free trial, plan limits on applications and search profiles, and Stripe subscription
sync from webhooks. Stripe is faked; MongoDB is real (test DB).
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

backend_dir = Path(__file__).resolve().parent.parent
if str(backend_dir) not in sys.path:
    sys.path.insert(0, str(backend_dir))

import pytest
from bson import ObjectId

from app.database import connect_db, get_database
from app.services.billing import PlanLimitError, entitlement_service, plan_catalog
import importlib
subscription_module = importlib.import_module("app.services.billing.subscription_service")
from app.services.billing.plan_catalog import Plan
from app.services.billing.subscription_service import subscription_service
from app.services.billing.webhook_service import handle_event

USER = str(ObjectId())
STARTER = Plan(
    key="starter", name="Starter", description="", product_id="prod_starter", price_id="price_starter",
    unit_amount=999, currency="usd", interval="month", daily_application_limit=10,
    total_application_limit=None, search_profile_limit=1,
)
PRO = Plan(
    key="pro", name="Pro", description="", product_id="prod_pro", price_id="price_pro",
    unit_amount=1999, currency="usd", interval="month", daily_application_limit=30,
    total_application_limit=None, search_profile_limit=3,
)


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _stripe_sub(sub_id: str, product: str, status: str = "active") -> dict:
    period_end = int((datetime.now(timezone.utc) + timedelta(days=30)).timestamp())
    return {
        "id": sub_id, "status": status, "customer": "cus_test", "metadata": {"user_id": USER},
        "cancel_at_period_end": False, "cancel_at": None,
        "items": {"data": [{"price": {"id": f"price_{product}", "product": f"prod_{product}"}, "current_period_end": period_end}]},
    }


class FakeStripe:
    """Just enough of StripeClient for subscription sync."""

    def __init__(self):
        self.subscriptions_by_id: dict = {}
        retrieve = AsyncMock(side_effect=lambda sub_id: SimpleNamespace(to_dict=lambda: self.subscriptions_by_id[sub_id]))
        self.v1 = SimpleNamespace(subscriptions=SimpleNamespace(retrieve_async=retrieve))


@pytest.fixture(autouse=True)
async def setup_env(monkeypatch):
    await connect_db()
    db = get_database()
    users = {"$in": [USER, ObjectId(USER)]}
    for col in ("billing_accounts", "applications", "search_profiles"):
        await db[col].delete_many({"user_id": users})
    await db.stripe_events.delete_many({"event_id": {"$regex": "^evt_test_"}})

    monkeypatch.setattr(plan_catalog, "paid_plans", AsyncMock(return_value=[STARTER, PRO]))
    fake = FakeStripe()
    monkeypatch.setattr(subscription_module, "get_stripe", lambda: fake)
    yield fake


async def _add_applications(count: int, status: str = "APPLIED", applied_at: datetime | None = None):
    now = datetime.now(timezone.utc)
    await get_database().applications.insert_many([
        {"user_id": USER, "status": status, "applied_at": applied_at or now, "updated_at": now} for _ in range(count)
    ])


@pytest.mark.anyio
async def test_new_user_gets_trial_with_total_application_limit():
    ent = await entitlement_service.get(USER)
    assert ent.source == "trial"
    assert ent.application_limit == 25

    await _add_applications(24)
    await entitlement_service.assert_can_apply(USER)
    await _add_applications(1, status="QUEUED")  # in-flight applications count too
    with pytest.raises(PlanLimitError):
        await entitlement_service.assert_can_apply(USER)


@pytest.mark.anyio
async def test_expired_trial_blocks_applying_and_new_search_profiles():
    await entitlement_service.get(USER)
    past = datetime.now(timezone.utc) - timedelta(days=1)
    await get_database().billing_accounts.update_one({"user_id": USER}, {"$set": {"trial_ends_at": past}})

    assert (await entitlement_service.get(USER)).source == "none"
    with pytest.raises(PlanLimitError):
        await entitlement_service.assert_can_apply(USER)
    with pytest.raises(PlanLimitError):
        await entitlement_service.assert_can_create_search_profile(USER)


@pytest.mark.anyio
async def test_trial_search_profile_limit():
    await entitlement_service.assert_can_create_search_profile(USER)
    await get_database().search_profiles.insert_one({"user_id": USER, "name": "One"})
    with pytest.raises(PlanLimitError):
        await entitlement_service.assert_can_create_search_profile(USER)


@pytest.mark.anyio
async def test_subscription_webhook_applies_daily_plan_limits(setup_env):
    setup_env.subscriptions_by_id["sub_1"] = _stripe_sub("sub_1", "pro")
    await handle_event({"id": "evt_test_1", "type": "customer.subscription.created", "data": {"object": {"id": "sub_1"}}})

    ent = await entitlement_service.get(USER)
    assert (ent.source, ent.plan.key, ent.application_limit, ent.search_profile_limit) == ("subscription", "pro", 30, 3)

    # Yesterday's applications don't count toward today's allowance
    await _add_applications(40, applied_at=datetime.now(timezone.utc) - timedelta(days=2))
    await _add_applications(29)
    await entitlement_service.assert_can_apply(USER)
    await _add_applications(1)
    with pytest.raises(PlanLimitError):
        await entitlement_service.assert_can_apply(USER)


@pytest.mark.anyio
async def test_duplicate_webhook_event_is_processed_once(setup_env):
    setup_env.subscriptions_by_id["sub_1"] = _stripe_sub("sub_1", "starter")
    event = {"id": "evt_test_dup", "type": "customer.subscription.updated", "data": {"object": {"id": "sub_1"}}}
    await handle_event(event)
    await handle_event(event)
    assert setup_env.v1.subscriptions.retrieve_async.await_count == 1


@pytest.mark.anyio
async def test_late_event_for_ended_subscription_keeps_live_one(setup_env):
    setup_env.subscriptions_by_id["sub_new"] = _stripe_sub("sub_new", "pro")
    setup_env.subscriptions_by_id["sub_old"] = _stripe_sub("sub_old", "starter", status="canceled")
    await subscription_service.sync_subscription("sub_new")
    await subscription_service.sync_subscription("sub_old")

    account = await get_database().billing_accounts.find_one({"user_id": USER})
    assert account["subscription"]["id"] == "sub_new"


@pytest.mark.anyio
async def test_canceled_subscription_falls_back_to_trial_then_none(setup_env):
    setup_env.subscriptions_by_id["sub_1"] = _stripe_sub("sub_1", "starter")
    await subscription_service.sync_subscription("sub_1")
    assert (await entitlement_service.get(USER)).source == "subscription"

    setup_env.subscriptions_by_id["sub_1"] = _stripe_sub("sub_1", "starter", status="canceled")
    await subscription_service.sync_subscription("sub_1")
    assert (await entitlement_service.get(USER)).source == "trial"


@pytest.mark.anyio
async def test_invoice_history_maps_plans_and_hides_drafts(setup_env):
    from app.services.billing import billing_repository

    user = {"_id": ObjectId(USER)}
    assert await subscription_service.list_invoices(user) == []  # no Stripe customer yet

    await billing_repository.get_or_create(USER)
    await billing_repository.set_customer_if_missing(USER, "cus_test")
    now = int(datetime.now(timezone.utc).timestamp())
    line = {"description": "1 x Pro", "period": {"start": now, "end": now + 86400 * 30},
            "pricing": {"price_details": {"product": "prod_pro"}}}
    page = {"data": [
        {"id": "in_1", "number": "A-0001", "status": "paid", "amount_paid": 1999, "amount_due": 1999,
         "currency": "usd", "created": now, "lines": {"data": [line]},
         "hosted_invoice_url": "https://invoice", "invoice_pdf": "https://pdf"},
        {"id": "in_draft", "status": "draft", "amount_due": 999, "created": now, "lines": {"data": []}},
    ]}
    setup_env.v1.invoices = SimpleNamespace(list_async=AsyncMock(return_value=SimpleNamespace(to_dict=lambda: page)))

    invoices = await subscription_service.list_invoices(user)
    assert [i["id"] for i in invoices] == ["in_1"]
    assert invoices[0]["plan_name"] == "Pro"
    assert invoices[0]["amount"] == 1999
