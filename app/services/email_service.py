"""
Outgoing email over SMTP (standard library only).

Used by the public support/contact form to deliver requests to SUPPORT_EMAIL.
"""
from __future__ import annotations

import asyncio
import smtplib
import ssl
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from html import escape

from app.config import settings

BRAND = "#104e43"
BRAND_ACCENT = "#61ebd7"


class EmailNotConfiguredError(RuntimeError):
    """Raised when SMTP settings are missing."""


def _send_sync(msg: EmailMessage) -> None:
    host, port = settings.SMTP_HOST, settings.SMTP_PORT
    context = ssl.create_default_context()
    if settings.SMTP_USE_SSL:
        server: smtplib.SMTP = smtplib.SMTP_SSL(host, port, context=context, timeout=20)
    else:
        server = smtplib.SMTP(host, port, timeout=20)
    try:
        server.ehlo()
        if settings.SMTP_USE_TLS and not settings.SMTP_USE_SSL:
            server.starttls(context=context)
            server.ehlo()
        if settings.SMTP_USERNAME:
            server.login(settings.SMTP_USERNAME, settings.SMTP_PASSWORD)
        server.send_message(msg)
    finally:
        try:
            server.quit()
        except Exception:
            pass


async def send_email(msg: EmailMessage) -> None:
    if not settings.smtp_enabled:
        raise EmailNotConfiguredError("SMTP is not configured on this server.")
    await asyncio.to_thread(_send_sync, msg)


def _row(label: str, value: str) -> str:
    return (
        '<tr>'
        f'<td style="padding:10px 0;width:120px;vertical-align:top;font-size:13px;color:#6b726f;">{escape(label)}</td>'
        f'<td style="padding:10px 0;vertical-align:top;font-size:14px;color:#161a18;font-weight:600;">{value}</td>'
        '</tr>'
    )


def build_support_email(
    *,
    name: str,
    email: str,
    subject: str,
    category: str,
    message: str,
    user_id: str | None = None,
) -> EmailMessage:
    """Compose the HTML + plain-text email sent to the support inbox."""
    now = datetime.now(timezone.utc)
    received = now.strftime("%b %d, %Y at %H:%M UTC")
    ticket = now.strftime("A2H-%y%m%d-%H%M%S")

    safe_email = escape(email)
    message_html = escape(message).replace("\n", "<br>")

    rows = [
        _row("Name", escape(name)),
        _row("Email", f'<a href="mailto:{safe_email}" style="color:{BRAND};text-decoration:none;">{safe_email}</a>'),
        _row("Category", escape(category)),
        _row("Subject", escape(subject)),
        _row("Received", escape(received)),
    ]
    if user_id:
        rows.append(_row("Account ID", escape(user_id)))

    html = f"""\
<!doctype html>
<html>
<body style="margin:0;padding:0;background:#f4f1ea;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f4f1ea;padding:32px 12px;">
    <tr><td align="center">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:600px;background:#ffffff;border-radius:16px;overflow:hidden;border:1px solid #e7e2d8;">
        <tr>
          <td style="background:{BRAND};padding:24px 32px;">
            <div style="font-size:18px;font-weight:700;color:#ffffff;letter-spacing:-0.2px;">Apply2Hire</div>
            <div style="font-size:12px;color:{BRAND_ACCENT};margin-top:4px;letter-spacing:1px;text-transform:uppercase;">New support request &middot; {escape(ticket)}</div>
          </td>
        </tr>
        <tr>
          <td style="padding:28px 32px 8px;">
            <h1 style="margin:0 0 4px;font-size:20px;color:#161a18;">{escape(subject)}</h1>
            <p style="margin:0;font-size:13px;color:#6b726f;">Reply to this email to respond to {escape(name)} directly.</p>
          </td>
        </tr>
        <tr>
          <td style="padding:12px 32px;">
            <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-top:1px solid #eee9df;border-bottom:1px solid #eee9df;">
              {''.join(rows)}
            </table>
          </td>
        </tr>
        <tr>
          <td style="padding:16px 32px 28px;">
            <div style="font-size:12px;font-weight:700;color:#6b726f;text-transform:uppercase;letter-spacing:1px;margin-bottom:10px;">Message</div>
            <div style="background:#faf7f2;border-left:3px solid {BRAND};border-radius:8px;padding:16px 18px;font-size:14px;line-height:1.6;color:#161a18;">{message_html}</div>
          </td>
        </tr>
        <tr>
          <td style="padding:16px 32px;background:#faf7f2;border-top:1px solid #eee9df;font-size:12px;color:#8a908d;">
            Sent from the Apply2Hire support form.
          </td>
        </tr>
      </table>
    </td></tr>
  </table>
</body>
</html>
"""

    text = (
        f"New support request ({ticket})\n"
        f"{'=' * 40}\n\n"
        f"Name:     {name}\n"
        f"Email:    {email}\n"
        f"Category: {category}\n"
        f"Subject:  {subject}\n"
        f"Received: {received}\n"
        + (f"Account:  {user_id}\n" if user_id else "")
        + f"\nMessage\n{'-' * 40}\n{message}\n"
    )

    from_addr = settings.SMTP_FROM_EMAIL or settings.SMTP_USERNAME
    msg = EmailMessage()
    msg["Subject"] = f"[Support] [{category}] {subject}"
    msg["From"] = formataddr((settings.SMTP_FROM_NAME, from_addr))
    msg["To"] = settings.SUPPORT_EMAIL
    msg["Reply-To"] = formataddr((name, email))
    msg["Message-ID"] = make_msgid(domain=from_addr.split("@")[-1] if "@" in from_addr else None)
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")
    return msg
