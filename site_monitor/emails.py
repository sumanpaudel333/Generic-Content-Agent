"""
The emails the site monitor sends: an alert when a problem starts, a reminder
while a page stays down, a note when it recovers, a certificate warning, and a
morning summary of anything that happened during quiet hours.

Built the same way as the account emails in dashboard/user_mail.py -- tables,
inline styles and bgcolor so Outlook lays them out properly, the logo embedded
rather than linked, and a plain-text copy alongside. They go out under their
own sender name, so an alert about the shop is never mistaken for the chat
report.
"""
import html
from datetime import datetime, timezone

from chat_insights import alerts, mailer
from config.localtime import au_datetime, au_time

SENDER_NAME = "BC Sands Site Monitor"

RED = "#C62828"
AMBER = "#B26B00"
GREEN = "#0E8A4A"
BLUE = "#004495"
SLATE = "#475569"
DARK = "#00224E"
YELLOW = "#FFC72C"
INK = "#101A2B"
MUTED = "#5D6E88"
LINE = "#E3EAF3"
PAGE = "#F5F8FC"

FAMILY_WORDS = {"down": "not responding", "error": "showing a problem",
                "slow": "slow", "blocked": "blocked by Cloudflare"}


def _logo() -> tuple[str, bytes]:
    try:
        from dashboard.user_mail import LOGO_CID, _logo_bytes
        return LOGO_CID, _logo_bytes()
    except Exception:
        return "", b""


def site_health_url() -> str:
    return alerts.dashboard_url("/site-health")


def local_time(value: str) -> str:
    """"Thu 17 Sep, 9:05 am AEST" -- Australian Eastern time, whatever the server's zone."""
    return au_datetime(value, fallback=value or "", year=False)


def short_time(value: str) -> str:
    """"9:05 am" """
    return au_time(value)


def duration(start: str, end: str = "") -> str:
    try:
        began = datetime.fromisoformat(start)
        ended = datetime.fromisoformat(end) if end else datetime.now(timezone.utc)
    except (TypeError, ValueError):
        return ""
    minutes = max(0, int((ended - began).total_seconds() // 60))
    if minutes < 60:
        return f"{minutes} min"
    hours, mins = divmod(minutes, 60)
    if hours < 24:
        return f"{hours} h {mins} min" if mins else f"{hours} h"
    days, hours = divmod(hours, 24)
    return f"{days} d {hours} h" if hours else f"{days} d"


def _response(event: dict) -> str:
    parts = []
    if event.get("status"):
        parts.append(f"HTTP {event['status']}")
    if event.get("ms") is not None:
        parts.append(f"{event['ms'] / 1000:.1f} s")
    return ", ".join(parts)


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _page(*, accent: str, tag: str, headline: str, intro: str, rows: list,
          extra_html: str = "", footer: str = "") -> str:
    cid, logo = _logo()
    brand = (f'<img src="cid:{cid}" width="120" alt="BC Sands" '
             f'style="display:block;border:0;height:auto;width:120px;">' if logo else
             '<div style="font-family:Segoe UI,Arial,sans-serif;font-size:16px;font-weight:700;'
             'color:#ffffff;">BC Sands</div>')
    table_rows = "".join(
        f'<tr><td style="padding:7px 12px 7px 0;font-family:Segoe UI,Arial,sans-serif;'
        f'font-size:12.5px;color:{MUTED};vertical-align:top;white-space:nowrap;">{html.escape(k)}</td>'
        f'<td style="padding:7px 0;font-family:Segoe UI,Arial,sans-serif;font-size:13.5px;'
        f'color:{INK};vertical-align:top;word-break:break-word;">{html.escape(str(v))}</td></tr>'
        for k, v in rows)
    link = site_health_url()
    button = "" if not link else f"""
      <tr><td style="padding:6px 28px 4px;">
        <table role="presentation" cellpadding="0" cellspacing="0" border="0">
          <tr><td bgcolor="{BLUE}" style="background:{BLUE};border-radius:8px;">
            <a href="{html.escape(link, quote=True)}" style="display:inline-block;padding:11px 24px;
               font-family:Segoe UI,Arial,sans-serif;font-size:14px;font-weight:600;color:#ffffff;
               text-decoration:none;">Open Site health</a>
          </td></tr>
        </table>
      </td></tr>"""
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(headline)}</title></head>
<body style="margin:0;padding:0;background:{PAGE};">
<div style="display:none;max-height:0;overflow:hidden;opacity:0;">{html.escape(intro[:110])}</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"
       style="background:{PAGE};padding:24px 12px;">
  <tr><td align="center">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"
           style="max-width:560px;background:#ffffff;border:1px solid {LINE};border-radius:12px;">
      <tr><td bgcolor="{DARK}" style="background:{DARK};padding:18px 28px;">
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"><tr>
          <td style="vertical-align:middle;">
            <table role="presentation" cellpadding="0" cellspacing="0" border="0"><tr>
              <td bgcolor="#ffffff" style="background:#ffffff;border-radius:8px;padding:6px 10px;">{brand}</td>
            </tr></table>
          </td>
          <td align="right" style="vertical-align:middle;font-family:Segoe UI,Arial,sans-serif;
              font-size:11px;letter-spacing:.6px;text-transform:uppercase;color:#C9D6E8;">Site&nbsp;monitor</td>
        </tr></table>
      </td></tr>
      <tr><td bgcolor="{accent}" height="4" style="background:{accent};height:4px;line-height:4px;font-size:0;">&nbsp;</td></tr>
      <tr><td style="padding:24px 28px 0;">
        <div style="font-family:Segoe UI,Arial,sans-serif;font-size:11px;font-weight:700;
                    letter-spacing:.8px;text-transform:uppercase;color:{accent};">{html.escape(tag)}</div>
        <div style="font-family:Segoe UI,Arial,sans-serif;font-size:20px;font-weight:700;color:{INK};
                    line-height:1.3;padding-top:4px;">{html.escape(headline)}</div>
        <div style="font-family:Segoe UI,Arial,sans-serif;font-size:14px;line-height:1.6;color:{INK};
                    padding-top:8px;">{html.escape(intro)}</div>
      </td></tr>
      <tr><td style="padding:16px 28px 8px;">
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"
               style="border-top:1px solid {LINE};">{table_rows}</table>
      </td></tr>
      {extra_html}
      {button}
      <tr><td style="padding:18px 28px 24px;">
        <div style="border-top:1px solid {LINE};padding-top:12px;font-family:Segoe UI,Arial,sans-serif;
                    font-size:11.5px;line-height:1.6;color:{MUTED};">{html.escape(footer or
            "Sent by the BC Sands Content and Automation Dashboard, which checks the online shop every five minutes.")}</div>
      </td></tr>
    </table>
  </td></tr>
</table>
</body></html>"""


def _text(headline: str, intro: str, rows: list, extra: str = "") -> str:
    lines = [headline, "=" * len(headline), "", intro, ""]
    lines += [f"{k}: {v}" for k, v in rows]
    if extra:
        lines += ["", extra]
    link = site_health_url()
    if link:
        lines += ["", f"Site health: {link}"]
    lines += ["", "Sent by the BC Sands Content and Automation Dashboard."]
    return "\n".join(lines) + "\n"


def render_event(event: dict) -> tuple[str, str, str]:
    """(subject, html, text) for one notification."""
    kind = event["type"]
    family = event.get("family", "")
    label = event.get("target_label", "")
    since = local_time(event.get("opened_at", ""))
    rows = [("Page", label), ("Address", event.get("target_url", ""))]

    if kind == "cert_expiring":
        days, host = event["cert_days"], event.get("host", "")
        subject = f"CERTIFICATE: {host} expires in {_plural(days, 'day')}"
        headline = f"The certificate for {host} expires in {_plural(days, 'day')}"
        intro = (f"It runs out on {event['cert_expires']}. After that, browsers refuse to open the "
                 "site and show a security warning instead, until the certificate is renewed.")
        rows.append(("Expires", event["cert_expires"]))
        accent, tag = AMBER, "Certificate"
    elif kind == "recovered":
        took = duration(event.get("opened_at", ""), event.get("closed_at", ""))
        subject = f"RECOVERED: {label} after {took}"
        headline = f"{label} is back to normal"
        intro = (f"It was {FAMILY_WORDS.get(family, 'having a problem')} for {took}, from {since} "
                 f"to {local_time(event.get('closed_at', ''))}.")
        rows += [("What it was", event.get("detail") or FAMILY_WORDS.get(family, "")),
                 ("Now", _response(event) or "Loading normally")]
        accent, tag = GREEN, "Recovered"
    elif kind == "reminder":
        took = duration(event.get("opened_at", ""), event.get("checked_at", ""))
        subject = f"STILL DOWN: {label} for {took}"
        headline = f"{label} is still not responding"
        intro = (f"It has been down for {took}, since {since}. You will be reminded while it stays "
                 "down, and told as soon as it recovers.")
        rows += [("What the monitor sees", event.get("detail", "")), ("Down since", since)]
        accent, tag = RED, "Still down"
    else:
        streak = event.get("streak")
        in_a_row = f" on {streak} checks in a row" if streak else ""
        if family == "down":
            subject = f"DOWN: {label} since {short_time(event.get('opened_at', ''))}".rstrip()
            headline = f"{label} is not responding"
            intro = f"The monitor could not load this page{in_a_row}, starting {since}."
            accent, tag = RED, "Down"
        elif family == "slow":
            limit = (event.get("slow_ms") or 3000) / 1000
            subject = (f"SLOW: {label} ({event['ms'] / 1000:.1f} s)" if event.get("ms") is not None
                       else f"SLOW: {label}")
            headline = f"{label} is slow"
            intro = f"It took longer than {limit:g} s to load{in_a_row}, starting {since}."
            accent, tag = AMBER, "Slow"
        elif family == "blocked":
            subject = f"BLOCKED: Cloudflare is blocking the monitor on {label}"
            headline = "Cloudflare is blocking the monitor"
            intro = ("This is not an outage. Cloudflare showed its bot check instead of the page, so "
                     "the monitor cannot tell whether customers can reach it. The Cloudflare rule "
                     "that lets the office's internet address through is missing or no longer "
                     "matches. The details below say which address Cloudflare saw.")
            accent, tag = SLATE, "Blocked"
        else:
            subject = f"PROBLEM: {label}"
            headline = f"{label} is showing a problem"
            intro = f"The page answered, but something is wrong{in_a_row}, starting {since}."
            accent, tag = AMBER, "Problem"
        rows += [("What the monitor sees", event.get("detail", "")), ("Since", since),
                 ("Last response", _response(event))]

    rows = [(k, v) for k, v in rows if v]
    return (subject, _page(accent=accent, tag=tag, headline=headline, intro=intro, rows=rows),
            _text(headline, intro, rows))


def render_summary(held: list[dict], still_open: list[dict],
                   targets: dict[str, dict]) -> tuple[str, str, str]:
    """The morning email: what happened during quiet hours, and what is still wrong."""
    problems = [e for e in held if e["type"] in ("opened", "cert_expiring")]
    fixed = [e for e in held if e["type"] == "recovered"]
    n_open = len(still_open)
    if n_open:
        subject = (f"Overnight site report: {_plural(len(problems), 'problem')}, "
                   f"{n_open} still open")
        accent, tag = RED, "Needs attention"
        headline = f"{_plural(n_open, 'problem')} still open this morning"
    else:
        subject = f"Overnight site report: {_plural(len(problems), 'problem')}, all cleared"
        accent, tag = GREEN, "All clear"
        headline = "Everything is working this morning"
    intro = (f"While alerts were paused overnight the monitor recorded "
             f"{_plural(len(problems), 'problem')} and {_plural(len(fixed), 'recovery')}.")

    def describe(event: dict) -> str:
        if event["type"] == "cert_expiring":
            return f"Certificate for {event.get('host', '')} expires in {_plural(event['cert_days'], 'day')}"
        if event["type"] == "recovered":
            took = duration(event.get("opened_at", ""), event.get("closed_at", ""))
            return f"Recovered after {took}"
        return f"{FAMILY_WORDS.get(event.get('family', ''), 'Problem').capitalize()}: {event.get('detail', '')}".rstrip(": ")

    rows = [(short_time(e.get("checked_at", "")) + "  " + e.get("target_label", ""), describe(e))
            for e in held if e["type"] != "reminder"]
    open_rows = []
    for incident in still_open:
        target = targets.get(incident["target_key"], {})
        open_rows.append((target.get("label", incident["target_key"]),
                          f"{FAMILY_WORDS.get(incident['family'], 'problem')} since "
                          f"{local_time(incident['opened_at'])}"))
    extra_html = ""
    extra_text = ""
    if open_rows:
        items = "".join(f'<li style="margin:0 0 6px;">{html.escape(k)}: {html.escape(v)}</li>'
                        for k, v in open_rows)
        extra_html = (f'<tr><td style="padding:4px 28px 6px;font-family:Segoe UI,Arial,sans-serif;'
                      f'font-size:13.5px;color:{INK};"><b>Still open now</b>'
                      f'<ul style="margin:8px 0 0;padding-left:18px;">{items}</ul></td></tr>')
        extra_text = "Still open now:\n" + "\n".join(f"  - {k}: {v}" for k, v in open_rows)
    return (subject,
            _page(accent=accent, tag=tag, headline=headline, intro=intro, rows=rows,
                  extra_html=extra_html),
            _text(headline, intro, rows, extra_text))


def render_test(recipients: list[str]) -> tuple[str, str, str]:
    headline = "Site monitor alerts are reaching you"
    intro = ("This is a test from the Site health page. Real alerts arrive from the same sender, "
             "look like this, and say which page has a problem.")
    rows = [("Sent to", ", ".join(recipients)),
            ("Sent at", au_datetime(datetime.now(timezone.utc), year=False))]
    return ("Test: BC Sands site monitor alerts",
            _page(accent=BLUE, tag="Test", headline=headline, intro=intro, rows=rows),
            _text(headline, intro, rows))


def send(subject: str, html_body: str, text_body: str, recipients: list[str]) -> dict:
    cid, logo = _logo()
    return mailer.send(subject, html_body, to_addresses=list(recipients), text_body=text_body,
                       from_name=SENDER_NAME,
                       inline_images={cid: logo} if cid and logo else None)
