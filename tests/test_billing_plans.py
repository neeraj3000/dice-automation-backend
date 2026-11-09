"""
Starter / Pro / Premium pricing, driven entirely by Stripe Product metadata.

Stripe is faked at the SDK boundary; MongoDB is real (the isolated test DB from conftest.py).
Covers: catalog parsing and validation, the /billing API, each tier's daily-application and
search-profile limits end to end over HTTP, subscribers on archived plans, and the
scripts/setup_stripe_plans.py dashboard setup.
"""
import importlib
import json
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import stripe
from bson import ObjectId
from httpx import ASGITransport, AsyncClient

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))
sys.path.insert(0, str(BACKEND_DIR / "scripts"))

from app.config import settings  # noqa: E402
from app.core.security import create_access_token  # noqa: E402
from app.database import connect_db, get_database  # noqa: E402
from app.main import app  # noqa: E402
from app.services.billing import PlanLimitError, entitlement_service, plan_catalog  # noqa: E402
from app.services.billing.webhook_service import handle_event  # noqa: E402

catalog_module = importlib.import_module("app.services.billing.plan_catalog")
subscription_module = importlib.import_module("app.services.billing.subscription_service")
import setup_stripe_plans  # noqa: E402

PLANS_JSON = BACKEND_DIR / "scripts" / "stripe_plans.json"


def product(pid, key, amount, daily, profiles, *, highlight=False, price_active=True, active=True, name=None, **meta):
    return {
        "id": pid,
        "active": active,
        "name": name or key.title(),
        "description": f"{key} plan",
        "metadata": {
            "plan_key": key, "daily_application_limit": str(daily), "search_profile_limit": str(profiles),
            "highlight": "true" if highlight else "false", **meta,
        },
        "default_price": {
            "id": f"price_{pid}", "active": price_active, "type": "recurring", "unit_amount": amount,
            "currency": "usd", "recurring": {"interval": "month"},
        },
        "marketing_features": [],
    }


# What the Stripe dashboard holds after the new pricing is set up (deliberately unsorted)
NEW_TIERS = [
    product("prod_premium", "premium", 1999, 70, 5),
    product("prod_starter", "starter", 499, 10, 1),
    product("prod_pro", "pro", 1299, 30, 3, highlight=True),
]
TIER_LIMITS = {"starter": (10, 1), "pro": (30, 3), "premium": (70, 5)}


def _obj(d):
    return SimpleNamespace(to_dict=lambda: d, id=d.get("id"))


class FakeStripe:
    """The slice of StripeClient that billing uses: product listing/retrieval and subscriptions."""

    def __init__(self, products):
        self.products = {p["id"]: json.loads(json.dumps(p)) for p in products}  # tests may mutate
        self.subscriptions = {}

        async def list_products(params):
            data = [p for p in self.products.values() if p["active"]]
            return _obj({"data": data, "has_more": False})

        async def retrieve_product(pid, params=None):
            if pid not in self.products:
                raise stripe.InvalidRequestError(f"No such product: '{pid}'", "id")
            return _obj(self.products[pid])

        self.list_products = AsyncMock(side_effect=list_products)
        self.retrieve_product = AsyncMock(side_effect=retrieve_product)
        self.v1 = SimpleNamespace(
            products=SimpleNamespace(list_async=self.list_products, retrieve_async=self.retrieve_product),
            subscriptions=SimpleNamespace(retrieve_async=AsyncMock(side_effect=lambda sid: _obj(self.subscriptions[sid]))),
        )

    def subscribe(self, user_id, sub_id, product_id, status="active"):
        end = int((datetime.now(timezone.utc) + timedelta(days=30)).timestamp())
        self.subscriptions[sub_id] = {
            "id": sub_id, "status": status, "customer": f"cus_{user_id}", "metadata": {"user_id": user_id},
            "cancel_at_period_end": False, "cancel_at": None,
            "items": {"data": [{"price": {"id": f"price_{product_id}", "product": product_id}, "current_period_end": end}]},
        }


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def env(monkeypatch):
    await connect_db()
    fake = FakeStripe(NEW_TIERS)
    monkeypatch.setattr(catalog_module, "get_stripe", lambda: fake)
    monkeypatch.setattr(subscription_module, "get_stripe", lambda: fake)
    plan_catalog.invalidate()
    created_users = []
    yield SimpleNamespace(stripe=fake, users=created_users)

    db = get_database()
    ids = {"$in": created_users + [ObjectId(u) for u in created_users]}
    await db.users.delete_many({"_id": ids})
    for col in ("billing_accounts", "applications", "search_profiles"):
        await db[col].delete_many({"user_id": ids})
    await db.stripe_events.delete_many({"event_id": {"$regex": "^evt_plans_"}})
    plan_catalog.invalidate()


async def new_user(env) -> tuple[str, dict]:
    uid = str(ObjectId())
    await get_database().users.insert_one({"_id": ObjectId(uid), "email": f"{uuid.uuid4().hex[:8]}@plans.test", "name": "Plan Tester"})
    env.users.append(uid)
    return uid, {"Authorization": f"Bearer {create_access_token(uid)}"}


async def subscribe(env, uid, product_id, sub_id=None):
    sub_id = sub_id or f"sub_{uuid.uuid4().hex[:10]}"
    env.stripe.subscribe(uid, sub_id, product_id)
    await handle_event({"id": f"evt_plans_{uuid.uuid4().hex}", "type": "customer.subscription.created", "data": {"object": {"id": sub_id}}})


async def add_applications(uid, count, applied_at=None):
    if count:
        now = datetime.now(timezone.utc)
        await get_database().applications.insert_many(
            [{"user_id": uid, "status": "APPLIED", "applied_at": applied_at or now, "updated_at": now} for _ in range(count)]
        )


def client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


# ── Plan catalog ────────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_catalog_reads_three_tiers_from_stripe_sorted_by_price(env):
    plans = await plan_catalog.paid_plans()
    assert [(p.key, p.name, p.unit_amount, p.daily_application_limit, p.search_profile_limit, p.highlight) for p in plans] == [
        ("starter", "Starter", 499, 10, 1, False),
        ("pro", "Pro", 1299, 30, 3, True),
        ("premium", "Premium", 1999, 70, 5, False),
    ]
    assert all(p.interval == "month" and p.currency == "usd" for p in plans)


@pytest.mark.anyio
async def test_catalog_is_cached_and_invalidated_by_product_webhook(env):
    await plan_catalog.paid_plans()
    await plan_catalog.paid_plans()
    assert env.stripe.list_products.await_count == 1

    # Edit Pro's limits in the dashboard: product.updated drops the cache
    env.stripe.products["prod_pro"]["metadata"]["daily_application_limit"] = "35"
    await handle_event({"id": f"evt_plans_{uuid.uuid4().hex}", "type": "product.updated", "data": {"object": {"id": "prod_pro"}}})
    pro = await plan_catalog.by_product_id("prod_pro")
    assert pro.daily_application_limit == 35
    assert env.stripe.list_products.await_count == 2


@pytest.mark.anyio
@pytest.mark.parametrize("bad", [
    {"daily_application_limit": "0"},
    {"daily_application_limit": "-5"},
    {"search_profile_limit": "abc"},
    {"search_profile_limit": ""},
    {"plan_key": "trial"},
])
async def test_catalog_skips_products_with_invalid_metadata(env, bad):
    broken = product("prod_broken", "broken", 999, 5, 1)
    broken["metadata"].update(bad)
    env.stripe.products["prod_broken"] = broken
    assert [p.key for p in await plan_catalog.paid_plans()] == ["starter", "pro", "premium"]


@pytest.mark.anyio
async def test_catalog_skips_products_without_plan_key_or_active_price(env):
    env.stripe.products["prod_other"] = {**product("prod_other", "x", 100, 1, 1), "metadata": {}}
    env.stripe.products["prod_noprice"] = product("prod_noprice", "noprice", 100, 1, 1, price_active=False)
    assert [p.key for p in await plan_catalog.paid_plans()] == ["starter", "pro", "premium"]


@pytest.mark.anyio
async def test_catalog_offers_only_one_product_per_plan_key(env):
    env.stripe.products["prod_pro_dup"] = product("prod_pro_dup", "pro", 1599, 40, 4, name="Pro (duplicate)")
    plans = await plan_catalog.paid_plans()
    assert [p.key for p in plans] == ["starter", "pro", "premium"]
    assert next(p for p in plans if p.key == "pro").product_id == "prod_pro"  # the cheaper one wins


# ── /billing API ────────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_plans_endpoint_returns_trial_and_tiers_with_monthly_estimate(env, monkeypatch):
    monkeypatch.setattr(settings, "BILLING_WORKING_DAYS_PER_MONTH", 22)
    async with client() as c:
        res = await c.get("/api/billing/plans")
    assert res.status_code == 200
    rows = {p["key"]: p for p in res.json()}
    assert list(rows) == ["trial", "starter", "pro", "premium"]

    assert rows["trial"]["is_trial"] and rows["trial"]["unit_amount"] == 0
    assert rows["trial"]["total_application_limit"] == settings.TRIAL_APPLICATION_LIMIT
    assert rows["trial"]["monthly_application_limit"] is None
    for key, (daily, profiles) in TIER_LIMITS.items():
        assert rows[key]["daily_application_limit"] == daily
        assert rows[key]["search_profile_limit"] == profiles
        assert rows[key]["monthly_application_limit"] == daily * 22
        assert rows[key]["working_days_per_month"] == 22
        assert rows[key]["price_id"] == f"price_prod_{key}"
    assert [rows[k]["unit_amount"] for k in TIER_LIMITS] == [499, 1299, 1999]


@pytest.mark.anyio
async def test_monthly_estimate_follows_working_days_setting(env, monkeypatch):
    monkeypatch.setattr(settings, "BILLING_WORKING_DAYS_PER_MONTH", 20)
    async with client() as c:
        rows = {p["key"]: p for p in (await c.get("/api/billing/plans")).json()}
    assert rows["premium"]["monthly_application_limit"] == 1400
    assert rows["premium"]["working_days_per_month"] == 20


@pytest.mark.anyio
@pytest.mark.parametrize("tier", ["starter", "pro", "premium"])
async def test_status_endpoint_reports_tier_limits_and_usage(env, tier):
    uid, headers = await new_user(env)
    await subscribe(env, uid, f"prod_{tier}")
    await add_applications(uid, 4)
    daily, profiles = TIER_LIMITS[tier]

    async with client() as c:
        body = (await c.get("/api/billing/status", headers=headers)).json()
    assert body["source"] == "subscription"
    assert body["plan"]["key"] == tier
    assert body["applications"] == {**body["applications"], "used": 4, "limit": daily, "period": "day"}
    assert body["applications"]["resets_at"] is not None
    assert body["search_profiles"]["limit"] == profiles


# ── Limits enforced per tier ────────────────────────────────────────────────

@pytest.mark.anyio
@pytest.mark.parametrize("tier", ["starter", "pro", "premium"])
async def test_daily_application_limit_per_tier(env, tier):
    uid, _ = await new_user(env)
    await subscribe(env, uid, f"prod_{tier}")
    daily, _ = TIER_LIMITS[tier]

    await add_applications(uid, daily + 5, applied_at=datetime.now(timezone.utc) - timedelta(days=2))  # past days don't count
    await add_applications(uid, daily - 1)
    await entitlement_service.assert_can_apply(uid)
    await add_applications(uid, 1)
    with pytest.raises(PlanLimitError) as err:
        await entitlement_service.assert_can_apply(uid)
    assert err.value.status_code == 402
    assert f"limit of {daily} applications" in err.value.detail


@pytest.mark.anyio
@pytest.mark.parametrize("tier", ["starter", "pro", "premium"])
async def test_search_profile_limit_per_tier_over_http(env, tier):
    uid, headers = await new_user(env)
    await subscribe(env, uid, f"prod_{tier}")
    _, profiles = TIER_LIMITS[tier]

    async with client() as c:
        for i in range(profiles):
            res = await c.post("/api/search-profiles", headers=headers, json={"name": f"Profile {i}", "keywords": ["python"]})
            assert res.status_code == 200, res.text
        res = await c.post("/api/search-profiles", headers=headers, json={"name": "One too many", "keywords": ["java"]})
    assert res.status_code == 402
    assert f"includes {profiles} search profile" in res.json()["detail"]
    assert await get_database().search_profiles.count_documents({"user_id": uid}) == profiles


@pytest.mark.anyio
async def test_upgrading_starter_to_premium_raises_limits_immediately(env):
    uid, _ = await new_user(env)
    await subscribe(env, uid, "prod_starter", sub_id="sub_upgrade")
    await add_applications(uid, 10)
    with pytest.raises(PlanLimitError):
        await entitlement_service.assert_can_apply(uid)

    # Customer Portal plan change: same subscription, new product
    await subscribe(env, uid, "prod_premium", sub_id="sub_upgrade")
    ent = await entitlement_service.get(uid)
    assert (ent.plan.key, ent.application_limit, ent.search_profile_limit) == ("premium", 70, 5)
    await entitlement_service.assert_can_apply(uid)


# ── Archived (no longer sold) plans ─────────────────────────────────────────

@pytest.mark.anyio
async def test_subscriber_on_archived_plan_keeps_its_limits(env):
    # An old plan (e.g. Ultimate) archived in the dashboard but still subscribed to
    env.stripe.products["prod_ultimate"] = product("prod_ultimate", "ultimate", 9999, 200, 10, active=False, price_active=False)
    uid, headers = await new_user(env)
    await subscribe(env, uid, "prod_ultimate")

    assert "ultimate" not in [p.key for p in await plan_catalog.paid_plans()]  # not offered to new buyers
    ent = await entitlement_service.get(uid)
    assert (ent.source, ent.plan.key, ent.application_limit, ent.search_profile_limit) == ("subscription", "ultimate", 200, 10)

    account = await get_database().billing_accounts.find_one({"user_id": uid})
    assert account["subscription"]["daily_application_limit"] == 200  # stored fallback is populated too
    async with client() as c:
        assert (await c.get("/api/billing/status", headers=headers)).json()["plan"]["key"] == "ultimate"


@pytest.mark.anyio
async def test_archived_plan_lookups_are_cached(env):
    env.stripe.products["prod_old"] = product("prod_old", "old", 999, 15, 2, active=False)
    await plan_catalog.resolve_product("prod_old")
    await plan_catalog.resolve_product("prod_old")
    assert env.stripe.retrieve_product.await_count == 1


@pytest.mark.anyio
async def test_deleted_product_resolves_to_none_instead_of_erroring(env):
    assert await plan_catalog.resolve_product("prod_gone") is None
    assert await plan_catalog.resolve_product(None) is None


# ── scripts/setup_stripe_plans.py ───────────────────────────────────────────

class FakeSyncStripe:
    """Synchronous StripeClient slice used by the setup script; records every write."""

    def __init__(self, products=()):
        self.products = {p["id"]: json.loads(json.dumps(p)) for p in products}
        self.writes = []
        self._n = 0

        def list_(params):
            data = [p for p in self.products.values() if p["active"]]
            return _obj({"data": data, "has_more": False})

        def create_product(params):
            self._n += 1
            pid = f"prod_new{self._n}"
            pd = params.pop("default_price_data")
            self.products[pid] = {**params, "id": pid, "active": True, "default_price": {
                "id": f"price_{pid}", "active": True, "type": "recurring", "unit_amount": pd["unit_amount"],
                "currency": pd["currency"], "recurring": pd["recurring"]}}
            self.writes.append(("create_product", params["metadata"]["plan_key"]))
            return _obj(self.products[pid])

        def update_product(pid, params):
            p = self.products[pid]
            if "default_price" in params:
                p["default_price"] = self._prices[params["default_price"]]
            for k in ("name", "description", "metadata", "active"):
                if k in params:
                    p[k] = params[k]
            self.writes.append(("update_product", pid, sorted(params)))
            return _obj(p)

        self._prices = {}

        def create_price(params):
            self._n += 1
            price = {"id": f"price_n{self._n}", "active": True, "type": "recurring", "unit_amount": params["unit_amount"],
                     "currency": params["currency"], "recurring": params["recurring"]}
            self._prices[price["id"]] = price
            self.writes.append(("create_price", params["product"], params["unit_amount"]))
            return _obj(price)

        def update_price(pid, params):
            self.writes.append(("archive_price", pid))
            return _obj({"id": pid, **params})

        self.v1 = SimpleNamespace(
            products=SimpleNamespace(list=list_, create=create_product, update=update_product),
            prices=SimpleNamespace(create=create_price, update=update_price),
        )


def test_plans_config_matches_the_new_pricing():
    cfg = setup_stripe_plans.load_config(PLANS_JSON)
    assert [(p["plan_key"], p["unit_amount"], p["daily_application_limit"], p["search_profile_limit"]) for p in cfg["plans"]] == [
        ("starter", 499, 10, 1), ("pro", 1299, 30, 3), ("premium", 1999, 70, 5),
    ]
    assert (cfg["currency"], cfg["interval"]) == ("usd", "month")


def test_setup_script_dry_run_changes_nothing():
    fake = FakeSyncStripe()
    actions = setup_stripe_plans.run(fake, setup_stripe_plans.load_config(PLANS_JSON), apply=False, archive_others=False)
    assert len(actions) == 3 and all(a.startswith("create product") for a in actions)
    assert fake.writes == []


def test_setup_script_creates_products_the_app_can_read_and_is_idempotent():
    fake = FakeSyncStripe()
    cfg = setup_stripe_plans.load_config(PLANS_JSON)
    setup_stripe_plans.run(fake, cfg, apply=True, archive_others=False)
    assert [w[1] for w in fake.writes] == ["starter", "pro", "premium"]

    # What the script created parses into the exact plans the app should offer
    parsed = {p.key: p for p in map(catalog_module._plan_from_product, fake.products.values())}
    assert {k: (p.unit_amount, p.daily_application_limit, p.search_profile_limit) for k, p in parsed.items()} == {
        "starter": (499, 10, 1), "pro": (1299, 30, 3), "premium": (1999, 70, 5),
    }
    assert parsed["pro"].highlight and not parsed["starter"].highlight

    fake.writes.clear()
    assert setup_stripe_plans.run(fake, cfg, apply=True, archive_others=False) == []
    assert fake.writes == []


def test_setup_script_migrates_old_pricing():
    # Old dashboard: Starter $9.99, Pro $19.99, Premium $49.99 (85/day), Ultimate $99.99
    old = [
        product("prod_s", "starter", 999, 10, 1),
        product("prod_p", "pro", 1999, 30, 3, highlight=True),
        product("prod_m", "premium", 4999, 85, 5),
        product("prod_u", "ultimate", 9999, 200, 10),
    ]
    for p in old:
        p["description"] = None
    fake = FakeSyncStripe(old)
    setup_stripe_plans.run(fake, setup_stripe_plans.load_config(PLANS_JSON), apply=True, archive_others=True)

    assert ("create_price", "prod_s", 499) in fake.writes
    assert ("create_price", "prod_p", 1299) in fake.writes
    assert ("create_price", "prod_m", 1999) in fake.writes
    for pid in ("prod_s", "prod_p", "prod_m"):
        assert ("archive_price", f"price_{pid}") in fake.writes
    assert fake.products["prod_m"]["metadata"]["daily_application_limit"] == "70"
    assert fake.products["prod_u"]["active"] is False  # Ultimate archived, not deleted
    assert not any(w[0] == "create_product" for w in fake.writes)  # existing products reused

    live = [catalog_module._plan_from_product(p) for p in fake.products.values() if p["active"]]
    assert sorted((p.key, p.unit_amount, p.daily_application_limit) for p in live) == [
        ("premium", 1999, 70), ("pro", 1299, 30), ("starter", 499, 10),
    ]


@pytest.mark.parametrize("bad_plan", [
    {"plan_key": "x", "name": "X", "unit_amount": 0, "daily_application_limit": 1, "search_profile_limit": 1},
    {"plan_key": "trial", "name": "T", "unit_amount": 100, "daily_application_limit": 1, "search_profile_limit": 1},
    {"plan_key": "x", "name": "X", "unit_amount": 100, "daily_application_limit": 1},
])
def test_setup_script_rejects_invalid_config(tmp_path, bad_plan):
    path = tmp_path / "plans.json"
    path.write_text(json.dumps({"plans": [bad_plan]}))
    with pytest.raises(SystemExit):
        setup_stripe_plans.load_config(path)
