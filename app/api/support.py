"""
Support API – public contact form that emails the support inbox.
"""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr, Field, field_validator

from app.core.deps import get_current_user_optional, rate_limit
from app.services.email_service import EmailNotConfiguredError, build_support_email, send_email

router = APIRouter(prefix="/support", tags=["Support"])

Category = Literal["General", "Technical issue", "Billing", "Account", "Feature request", "Other"]


class ContactIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    email: EmailStr
    subject: str = Field(min_length=3, max_length=150)
    category: Category = "General"
    message: str = Field(min_length=10, max_length=5000)
    # Honeypot: hidden in the UI, real users leave it empty
    website: str = ""

    @field_validator("name", "subject", "message")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("must not be empty")
        return v

    @field_validator("name", "subject")
    @classmethod
    def _single_line(cls, v: str) -> str:
        # Block header injection through name/subject
        return " ".join(v.split())


@router.post("/contact", dependencies=[Depends(rate_limit(5, 600))])
async def contact_support(body: ContactIn, user: dict | None = Depends(get_current_user_optional)):
    if body.website:
        # Bot filled the honeypot: pretend success, send nothing
        return {"ok": True}

    user_id = None
    if user:
        user_id = str(user.get("_id") or user.get("id") or "") or None

    msg = build_support_email(
        name=body.name,
        email=str(body.email),
        subject=body.subject,
        category=body.category,
        message=body.message,
        user_id=user_id,
    )
    try:
        await send_email(msg)
    except EmailNotConfiguredError:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Support email is not configured. Please email us directly.")
    except Exception as e:
        print(f"[Dice-Automation] [support] Failed to send support email: {e}")
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "We couldn't send your message right now. Please try again shortly.")
    return {"ok": True}
