"""
Creates or updates the Apply2Hire plan Products in Stripe from scripts/stripe_plans.json.

The app reads plans straight from Stripe (see app/services/billing/plan_catalog.py), so this
script is only a repeatable way to do the dashboard setup. It is idempotent: products are
matched by `metadata.plan_key`, and a price change creates a new Price, makes it the
product's default and archives the old one (Stripe prices are immutable). Existing
subscribers keep paying their old price until you move them; their limits follow the
product metadata immediately.

    python scripts/setup_stripe_plans.py                    # dry run: shows what would change
    python scripts/setup_stripe_plans.py --apply            # make the changes (test key)
    python scripts/setup_stripe_plans.py --apply --archive-others   # also archive plans not in the file (e.g. old "ultimate")
    python scripts/setup_stripe_plans.py --apply --live     # required to touch a live-mode account
"""
import argparse
import json
import sys
from pathlib import Path

import stripe

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

DEFAULT_CONFIG = Path(__file__).resolve().parent / "stripe_plans.json"
REQUIRED = ("plan_key", "name", "unit_amount", "daily_application_limit", "search_profile_limit")


def load_config(path: Path) -> dict:
    cfg = json.loads(path.read_text(encoding="utf-8"))
    keys = set()
    for plan in cfg["plans"]:
        missing = [k for k in REQUIRED if k not in plan]
        if missing:
            raise SystemExit(f"Plan {plan.get('plan_key')!r} is missing {missing}")
        for k in ("unit_amount", "daily_application_limit", "search_profile_limit"):
            if not isinstance(plan[k], int) or plan[k] < 1:
                raise SystemExit(f"Plan {plan['plan_key']!r}: {k} must be a whole number >= 1")
        if plan["plan_key"] in keys or plan["plan_key"] == "trial":
            raise SystemExit(f"plan_key {plan['plan_key']!r} is duplicated or reserved")
        keys.add(plan["plan_key"])
    return cfg


def desired_metadata(plan: dict) -> dict:
    return {
        "plan_key": plan["plan_key"],
        "daily_application_limit": str(plan["daily_application_limit"]),
        "search_profile_limit": str(plan["search_profile_limit"]),
        "highlight": "true" if plan.get("highlight") else "false",
    }


def existing_plan_products(client: stripe.StripeClient) -> dict:
    """plan_key -> active product (default_price expanded)."""
    found, params = {}, {"active": True, "limit": 100, "expand": ["data.default_price"]}
    while True:
        page = client.v1.products.list(params).to_dict()
        for p in page["data"]:
            key = (p.get("metadata") or {}).get("plan_key", "").strip().lower()
            if key:
                found.setdefault(key, p)
        if not page.get("has_more"):
            return found
        params["starting_after"] = page["data"][-1]["id"]


def price_matches(price, plan: dict, currency: str, interval: str) -> bool:
    return (
        isinstance(price, dict) and price.get("active")
        and price.get("unit_amount") == plan["unit_amount"]
        and price.get("currency") == currency
        and (price.get("recurring") or {}).get("interval") == interval
    )


def run(client: stripe.StripeClient, cfg: dict, apply: bool, archive_others: bool) -> list[str]:
    currency, interval = cfg.get("currency", "usd"), cfg.get("interval", "month")
    existing = existing_plan_products(client)
    actions: list[str] = []

    for plan in cfg["plans"]:
        key, meta = plan["plan_key"], desired_metadata(plan)
        fields = {"name": plan["name"], "description": plan.get("description") or None, "metadata": meta}
        product = existing.get(key)

        if not product:
            actions.append(f"create product {key!r} at {plan['unit_amount']} {currency}/{interval}")
            if apply:
                client.v1.products.create({
                    **{k: v for k, v in fields.items() if v is not None},
                    "default_price_data": {
                        "currency": currency, "unit_amount": plan["unit_amount"], "recurring": {"interval": interval},
                    },
                })
            continue

        current_meta = {k: (product.get("metadata") or {}).get(k) for k in meta}
        if (product.get("name"), product.get("description"), current_meta) != (fields["name"], fields["description"], meta):
            actions.append(f"update product {key!r} ({product['id']}) name/description/metadata")
            if apply:
                # "" clears an unset description in Stripe
                client.v1.products.update(product["id"], {**fields, "description": fields["description"] or ""})

        old_price = product.get("default_price")
        if not price_matches(old_price, plan, currency, interval):
            actions.append(f"new default price for {key!r}: {plan['unit_amount']} {currency}/{interval}"
                           + (f" (archive {old_price['id']})" if isinstance(old_price, dict) else ""))
            if apply:
                new_price = client.v1.prices.create({
                    "product": product["id"], "currency": currency,
                    "unit_amount": plan["unit_amount"], "recurring": {"interval": interval},
                })
                client.v1.products.update(product["id"], {"default_price": new_price.id})
                if isinstance(old_price, dict):
                    client.v1.prices.update(old_price["id"], {"active": False})

    if archive_others:
        wanted = {p["plan_key"] for p in cfg["plans"]}
        for key, product in existing.items():
            if key not in wanted:
                actions.append(f"archive product {key!r} ({product['id']}); its subscribers keep their limits")
                if apply:
                    client.v1.products.update(product["id"], {"active": False})

    return actions


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--apply", action="store_true", help="make the changes (default is a dry run)")
    parser.add_argument("--archive-others", action="store_true", help="archive plan products not in the config")
    parser.add_argument("--live", action="store_true", help="allow a live-mode secret key")
    args = parser.parse_args()

    from app.config import settings
    key = settings.STRIPE_SECRET_KEY
    if not key:
        raise SystemExit("STRIPE_SECRET_KEY is not set")
    if key.startswith(("sk_live_", "rk_live_")) and not args.live:
        raise SystemExit("Refusing to touch a LIVE Stripe account without --live")

    cfg = load_config(args.config)
    client = stripe.StripeClient(key, max_network_retries=2)
    actions = run(client, cfg, args.apply, args.archive_others)
    mode = "APPLIED" if args.apply else "DRY RUN (pass --apply to make these changes)"
    print(f"{mode} on {'LIVE' if 'live' in key[:8] else 'test'} account:")
    for a in actions or ["nothing to change, Stripe already matches the config"]:
        print(f"  - {a}")


if __name__ == "__main__":
    main()
