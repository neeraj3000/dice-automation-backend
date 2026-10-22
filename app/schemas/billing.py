from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel


class PlanOut(BaseModel):
    key: str
    name: str
    description: str
    price_id: Optional[str]
    unit_amount: int
    currency: str
    interval: Optional[str]
    daily_application_limit: Optional[int]
    total_application_limit: Optional[int]
    search_profile_limit: int
    features: List[str]
    highlight: bool
    is_trial: bool


class UsageOut(BaseModel):
    used: int
    limit: int
    period: Optional[str]  # "day" | "trial" | None
    resets_at: Optional[datetime]


class SubscriptionOut(BaseModel):
    status: Optional[str]
    current_period_end: Optional[datetime]
    cancel_at_period_end: bool
    cancel_at: Optional[datetime]


class BillingStatusOut(BaseModel):
    source: str  # "subscription" | "trial" | "none"
    plan: Optional[PlanOut]
    subscription: Optional[SubscriptionOut]
    trial_ends_at: Optional[datetime]
    has_billing_account: bool
    applications: UsageOut
    search_profiles: UsageOut


class InvoiceOut(BaseModel):
    id: str
    number: Optional[str]
    status: Optional[str]
    plan_name: Optional[str]
    description: Optional[str]
    amount: int
    currency: str
    created: datetime
    period_start: Optional[datetime]
    period_end: Optional[datetime]
    hosted_invoice_url: Optional[str]
    invoice_pdf: Optional[str]


class CheckoutIn(BaseModel):
    price_id: str


class CheckoutConfirmIn(BaseModel):
    session_id: str


class RedirectOut(BaseModel):
    url: str
