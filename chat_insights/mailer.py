"""
Sends the weekly report by email. Stdlib only (smtplib + email.message).

Configured for Microsoft 365 (smtp.office365.com:587, STARTTLS) but the host and
port are config values, so any SMTP relay works.

IMPORTANT operational note: Microsoft disables SMTP AUTH (basic auth) by default
on most 365 tenants. If sending fails with 535, the mailbox needs SMTP AUTH
enabled by a tenant admin and an app password -- no change to this code will fix
that. send() reports the failure verbatim so the cause is visible rather than
buried.

A send failure never fails the weekly run: the report is already stored and
viewable in the dashboard, exactly as an Odoo publish failure leaves an approved
draft intact.
"""
import logging
import os
import smtplib
import ssl
from email.message import EmailMessage

from config import settings

logger = logging.getLogger("chat_insights.mailer")

SMTP_TIMEOUT_SECONDS = 30


def username() -> str:
    return os.environ.get("SMTP_USERNAME", "")


def password() -> str:
    return os.environ.get("SMTP_PASSWORD", "")


def sender() -> str:
    # Fall back to the auth username, which is the common case for 365.
    return settings.CHAT_SMTP_FROM or username()


def is_configured() -> bool:
    return bool(settings.CHAT_SMTP_HOST and username() and password()
                and settings.CHAT_REPORT_RECIPIENTS and sender())


def config_status() -> str:
    if is_configured():
        return (f"Email: configured ({sender()} -> "
                f"{', '.join(settings.CHAT_REPORT_RECIPIENTS)})")
    missing = []
    if not settings.CHAT_SMTP_HOST:
        missing.append("chat_insights.smtp_host")
    if not username():
        missing.append("SMTP_USERNAME (.env)")
    if not password():
        missing.append("SMTP_PASSWORD (.env)")
    if not settings.CHAT_REPORT_RECIPIENTS:
        missing.append("chat_insights.report_recipients")
    if not sender():
        missing.append("chat_insights.smtp_from")
    return "Email: not configured -- missing " + ", ".join(missing)


def send(subject: str, html_body: str, *, recipients: list[str] | None = None,
         smtp_factory=None) -> dict:
    """Sends the report. Returns {"success": bool, "detail": str}.

    `smtp_factory` is injectable so tests exercise the whole path without
    touching a real mail server."""
    recipients = recipients or settings.CHAT_REPORT_RECIPIENTS
    if not is_configured():
        return {"success": False, "detail": config_status()}

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = sender()
    message["To"] = ", ".join(recipients)
    message.set_content(
        "This report is formatted in HTML. If you are reading this, your mail client "
        "could not display it -- the same report is available in the Automation Control "
        "dashboard under Chat Insights."
    )
    message.add_alternative(html_body, subtype="html")

    try:
        if smtp_factory is not None:
            server = smtp_factory()
            with server:
                server.send_message(message)
        else:
            context = ssl.create_default_context()
            with smtplib.SMTP(settings.CHAT_SMTP_HOST, settings.CHAT_SMTP_PORT,
                               timeout=SMTP_TIMEOUT_SECONDS) as server:
                server.ehlo()
                server.starttls(context=context)
                server.ehlo()
                server.login(username(), password())
                server.send_message(message)
        return {"success": True, "detail": f"Sent to {', '.join(recipients)}"}
    except smtplib.SMTPAuthenticationError as e:
        logger.error("SMTP auth failed: %s", e)
        return {"success": False, "detail": (
            f"SMTP authentication failed ({e}). Microsoft 365 disables SMTP AUTH by "
            f"default -- it must be enabled for this mailbox, and an app password used.")}
    except Exception as e:
        logger.error("SMTP send failed: %s", e)
        return {"success": False, "detail": f"Send failed: {e}"}
