"""
The two emails this dashboard sends to its own users: an invitation, and a
password reset.

Separate from chat_insights/mailer.py, which is the SMTP layer, and from
chat_insights/alerts.py, which is about the automations. This is about
accounts, and it differs from every other mailing here in two ways that
matter.

It goes to ONE person, at an address stored on their account, rather than to a
configured list -- so it passes `to_addresses` explicitly and never touches
MAIL_PURPOSES.

And it does not come from "Chatbot-Insight". A message asking somebody to set
a password has to look like it came from the thing they are setting a password
for; arriving under the name of the weekly chat report is exactly the shape of
an email people are trained to distrust. The display name is overridden per
message (see SENDER_NAME below) while the address stays as configured, because
relays generally only accept mail from addresses they know about.

Neither email says anything about who the person is or what they can do here.
A password link that lands in the wrong inbox should give away as little as
possible.
"""
import functools
import html
import logging
import os

from chat_insights import alerts, mailer
from config import settings

logger = logging.getLogger("dashboard.user_mail")

# What the recipient sees in the From line and at the top of the message.
# Overridable for a site that brands it differently, but this is the name the
# dashboard calls itself, which is the point.
DEFAULT_SENDER_NAME = "BC Sands Content and Automation Dashboard"

BRAND_BLUE = "#004495"
BRAND_DARK = "#00224E"
BRAND_YELLOW = "#FFC72C"
INK = "#101A2B"
MUTED = "#5D6E88"
LINE = "#E3EAF3"
PAGE = "#F5F8FC"

LOGO_CID = "bcsandslogo"
LOGO_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "assets", "BC-Sands-Logo.png")


def sender_name() -> str:
    """The name beside the address in the recipient's inbox."""
    return (os.environ.get("ACCOUNT_EMAIL_FROM_NAME")
            or getattr(settings, "ACCOUNT_EMAIL_FROM_NAME", "")
            or DEFAULT_SENDER_NAME)


@functools.lru_cache(maxsize=1)
def _logo_bytes() -> bytes:
    """The logo, read once. Empty if it is not there, in which case the email
    falls back to the wordmark in text rather than showing a broken image."""
    try:
        with open(LOGO_PATH, "rb") as fh:
            return fh.read()
    except Exception:
        logger.warning("Could not read %s; the email goes out without the logo.", LOGO_PATH)
        return b""


def reset_url(token: str) -> str:
    """The absolute link, or "" when nobody has told us where this dashboard
    lives. An email with a dead link is worse than no email, so the callers
    check this before sending."""
    base = alerts.dashboard_url("/set-password")
    return f"{base}?token={token}" if base else ""


def _render(*, title: str, heading: str, lead: str, button: str, url: str,
             expiry: str, footer: str) -> tuple[str, str]:
    """(html, plain text).

    Tables, inline styles and bgcolor throughout. Outlook renders through
    Word, which ignores flexbox, most of the box model and border-radius, so
    anything laid out the way the dashboard is would arrive as one long
    column. The button is a table cell with a background colour rather than a
    styled <a>, for the same reason.
    """
    safe_url = html.escape(url, quote=True)
    logo = _logo_bytes()
    brand = (f'<img src="cid:{LOGO_CID}" width="132" alt="BC Sands"'
              f' style="display:block;border:0;height:auto;width:132px;">'
              if logo else
              '<div style="font-family:Segoe UI,Arial,sans-serif;font-size:17px;'
              'font-weight:700;color:#ffffff;">BC Sands</div>')

    body = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title></head>
<body style="margin:0;padding:0;background:{PAGE};">
<!-- Preheader: the grey line of text a client shows beside the subject. -->
<div style="display:none;max-height:0;overflow:hidden;opacity:0;">{html.escape(lead[:110])}</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"
       style="background:{PAGE};padding:26px 12px;">
  <tr><td align="center">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"
           style="max-width:540px;background:#ffffff;border:1px solid {LINE};
                  border-radius:12px;overflow:hidden;">

      <!-- Brand band. The logo is a transparent PNG with a dark wordmark, so
           it sits on a white chip rather than straight on the blue. -->
      <tr><td bgcolor="{BRAND_DARK}"
              style="background:{BRAND_DARK};padding:22px 28px;">
        <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%">
          <tr>
            <td align="left" style="vertical-align:middle;">
              <table role="presentation" cellpadding="0" cellspacing="0" border="0">
                <tr><td bgcolor="#ffffff" style="background:#ffffff;border-radius:8px;
                        padding:7px 11px;">{brand}</td></tr>
              </table>
            </td>
            <td align="right" style="vertical-align:middle;
                       font-family:Segoe UI,Arial,sans-serif;font-size:11px;
                       letter-spacing:.6px;text-transform:uppercase;
                       color:rgba(255,255,255,.72);">Content&nbsp;and&nbsp;Automation</td>
          </tr>
        </table>
      </td></tr>
      <tr><td bgcolor="{BRAND_YELLOW}" height="3"
              style="background:{BRAND_YELLOW};height:3px;line-height:3px;font-size:0;">&nbsp;</td></tr>

      <tr><td style="padding:28px 28px 0;">
        <div style="font-family:Segoe UI,Arial,sans-serif;font-size:20px;font-weight:700;
                    color:{INK};line-height:1.3;">{html.escape(heading)}</div>
      </td></tr>
      <tr><td style="padding:10px 28px 0;">
        <div style="font-family:Segoe UI,Arial,sans-serif;font-size:14.5px;line-height:1.62;
                    color:{INK};">{html.escape(lead)}</div>
      </td></tr>

      <tr><td style="padding:24px 28px 4px;">
        <table role="presentation" cellpadding="0" cellspacing="0" border="0">
          <tr><td bgcolor="{BRAND_BLUE}" style="background:{BRAND_BLUE};border-radius:8px;">
            <a href="{safe_url}"
               style="display:inline-block;padding:13px 30px;font-family:Segoe UI,Arial,sans-serif;
                      font-size:14.5px;font-weight:600;color:#ffffff;text-decoration:none;">
              {html.escape(button)}</a>
          </td></tr>
        </table>
      </td></tr>

      <tr><td style="padding:16px 28px 0;">
        <div style="font-family:Segoe UI,Arial,sans-serif;font-size:12.5px;line-height:1.6;
                    color:{MUTED};">{html.escape(expiry)}</div>
      </td></tr>

      <tr><td style="padding:18px 28px 0;">
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"
               style="background:{PAGE};border:1px solid {LINE};border-radius:8px;">
          <tr><td style="padding:11px 13px;">
            <div style="font-family:Segoe UI,Arial,sans-serif;font-size:11.5px;color:{MUTED};
                        padding-bottom:4px;">If the button does not work, paste this into
              your browser:</div>
            <div style="font-family:Consolas,Courier New,monospace;font-size:11.5px;
                        color:{BRAND_BLUE};word-break:break-all;">{safe_url}</div>
          </td></tr>
        </table>
      </td></tr>

      <tr><td style="padding:20px 28px 26px;">
        <div style="border-top:1px solid {LINE};padding-top:14px;
                    font-family:Segoe UI,Arial,sans-serif;font-size:11.5px;line-height:1.6;
                    color:{MUTED};">{html.escape(footer)}</div>
        <div style="font-family:Segoe UI,Arial,sans-serif;font-size:11px;color:{MUTED};
                    padding-top:10px;">BC Sands Building &amp; Landscape Supplies &middot;
          sent automatically by the Content and Automation Dashboard</div>
      </td></tr>
    </table>
  </td></tr>
</table>
</body></html>"""

    text = (f"{title}\n{'=' * len(title)}\n\n"
             f"{heading}\n\n{lead}\n\n{button}:\n{url}\n\n{expiry}\n\n{footer}\n\n"
             f"BC Sands Building & Landscape Supplies\n"
             f"Sent automatically by the Content and Automation Dashboard.\n")
    return body, text


def _send(user: dict, subject: str, html_body: str, text_body: str) -> dict:
    address = (user.get("email") or "").strip()
    if not address:
        return {"success": False, "skipped": True,
                "detail": "That account has no email address on it."}
    if not mailer.is_configured():
        return {"success": False, "skipped": True,
                "detail": "Email is not set up on this server, so nothing was sent."}
    logo = _logo_bytes()
    outcome = mailer.send(
        subject, html_body,
        to_addresses=[address],
        text_body=text_body,
        from_name=sender_name(),
        inline_images={LOGO_CID: logo} if logo else None,
    )
    logger.info("Account email to %s: %s", address, outcome.get("detail"))
    return outcome


TITLE = "BC Sands Content and Automation Dashboard"


def send_invite(user: dict, token: str, *, invited_by: str = "") -> dict:
    """Asks somebody to set their first password."""
    url = reset_url(token)
    if not url:
        return {"success": False, "skipped": True,
                "detail": "This server does not know its own web address, so the "
                           "link could not be built. Copy the link instead."}
    who = f" by {invited_by}" if invited_by else ""
    name = user.get("display_name") or user["username"]
    html_body, text_body = _render(
        title=TITLE,
        heading="You have been given access",
        lead=(f"Hello {name}. An account has been set up for you{who} on the BC Sands "
               f"Content and Automation Dashboard. Choose a password to get started. "
               f"Your username is {user['username']}."),
        button="Choose a password",
        url=url,
        expiry="This link works once, and stops working after seven days. Ask for a "
                "new one if it expires.",
        footer="If you were not expecting this, you can ignore it -- the account "
                "cannot be used until somebody sets a password with this link.")
    return _send(user, f"Your account on the {TITLE}", html_body, text_body)


def send_reset(user: dict, token: str, *, requested_by_admin: bool = False) -> dict:
    """Lets somebody set a new password."""
    url = reset_url(token)
    if not url:
        return {"success": False, "skipped": True,
                "detail": "This server does not know its own web address, so the "
                           "link could not be built. Copy the link instead."}
    name = user.get("display_name") or user["username"]
    if requested_by_admin:
        lead = (f"Hello {name}. An administrator has sent you a link to set a new "
                 f"password for your account on the BC Sands Content and Automation "
                 f"Dashboard. Your username is {user['username']}.")
    else:
        lead = (f"Hello {name}. A password reset was requested for your account on the "
                 f"BC Sands Content and Automation Dashboard. Your username is "
                 f"{user['username']}.")
    html_body, text_body = _render(
        title=TITLE,
        heading="Set a new password",
        lead=lead,
        button="Set a new password",
        url=url,
        expiry="This link works once, and stops working after two hours.",
        footer="If you did not ask for this, ignore this email and your password "
                "stays as it is. Tell an administrator if you get these unexpectedly.")
    return _send(user, f"Set a new password - {TITLE}", html_body, text_body)
