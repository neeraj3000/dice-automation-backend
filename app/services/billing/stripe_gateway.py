"""Thin wrapper around the Stripe SDK: one shared async client, plain-dict results."""
from typing import Any, Dict, Optional

import stripe
from fastapi import HTTPException, status

from app.config import settings

_client: Optional[stripe.StripeClient] = None


def get_stripe() -> stripe.StripeClient:
    global _client
    if not settings.stripe_enabled:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Billing is not configured on this server.")
    if _client is None:
        _client = stripe.StripeClient(
            settings.STRIPE_SECRET_KEY,
            http_client=stripe.HTTPXClient(),
            max_network_retries=2,
        )
    return _client


def as_dict(obj: Any) -> Dict[str, Any]:
    """StripeObject -> dict, so the rest of the code never depends on SDK object types."""
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return obj
    return obj.to_dict()


def construct_event(payload: bytes, sig_header: Optional[str]) -> Dict[str, Any]:
    """Verifies the webhook signature and returns the event as a dict."""
    if not settings.STRIPE_WEBHOOK_SECRET:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Stripe webhook secret is not configured.")
    try:
        event = get_stripe().construct_event(payload, sig_header, settings.STRIPE_WEBHOOK_SECRET)
    except (ValueError, stripe.SignatureVerificationError):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid Stripe signature")
    return as_dict(event)
