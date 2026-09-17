"""
Outbound alerts: a new lead, and a job that failed.

Both exist for the same reason -- nobody is watching. The jobs run unattended
under Task Scheduler, and the weekly report is the only thing that ever spoke
up. A lead that arrived on Thursday waited until the following Wednesday, and a
daily run that failed three days running looked exactly like a quiet week.

Alerts reuse chat_insights.mailer, so they inherit the relay, the From name and
the TLS settings already configured in .env, and they fail the same way: an
alert that cannot be sent is logged and never raises. Nothing here is allowed
to take a job down -- an unsent alert is bad, a job that dies because it could
not send one is worse.
"""
import html
import logging
import os

from chat_insights import db, mailer
from config import settings

logger = logging.getLogger("chat_insights.alerts")

LEAD_TYPE_LABELS = {
    "form_submission": "Contact form submitted",
    "contact_shared": "Contact details given in chat",
    "intent_only": "Buying intent, no contact details",
}

# Why the recipient is being told, per type. The distinction matters: a form
# submission should already have reached the CRM, so this alert is a safety net;
# for the others no hand-off exists at all and this alert is the only signal.
LEAD_TYPE_NOTES = {
    "form_submission": ("Chatbase should have sent this to the CRM through its Zapier action. "
                        "If it is not there, the action did not fire -- the details below are "
                        "the copy kept here."),
    "contact_shared": ("The customer typed their details into the chat instead of using the "
                       "contact form, so no CRM hand-off was ever triggered for this one."),
    "intent_only": ("Clear buying intent, but no contact details were given, so there is "
                    "nobody to call back. Read the transcript to judge whether it is worth "
                    "chasing another way."),
}


def dashboard_url(path: str) -> str:
    """Absolute link back into the dashboard, or "" when nobody has said where
    the dashboard lives -- in which case the alert still sends, just without
    links. Read at call time so .env wins, exactly as the mailer resolves its
    own settings."""
    base = (os.environ.get("DASHBOARD_BASE_URL") or settings.DASHBOARD_BASE_URL or "").rstrip("/")
    return f"{base}{path}" if base else ""


def _link(path: str, text: str) -> str:
    url = dashboard_url(path)
    return f'<a href="{url}">{html.escape(text)}</a>' if url else html.escape(text)


def _row(label: str, value: str) -> str:
    if not value:
        return ""
    return (f'<tr><td style="padding:4px 12px 4px 0;color:#5A6B85;white-space:nowrap">{label}</td>'
            f'<td style="padding:4px 0"><b>{html.escape(str(value))}</b></td></tr>')


def render_lead_email(lead: dict) -> tuple[str, str]:
    """(subject, html) for one lead."""
    who = lead.get("contact_name") or lead.get("contact_email") or lead.get("contact_phone")
    topic = lead.get("topic") or "Chat enquiry"
    subject = f"New lead: {topic}" + (f" -- {who}" if who else "")

    contact_rows = (_row("Name", lead.get("contact_name"))
                    + _row("Email", lead.get("contact_email"))
                    + _row("Phone", lead.get("contact_phone")))
    if not contact_rows:
        contact_rows = ('<tr><td colspan="2" style="padding:4px 0;color:#B26B00">'
                        'No contact details were given.</td></tr>')

    note = LEAD_TYPE_NOTES.get(lead.get("lead_type"), "")
    body = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>{html.escape(subject)}</title></head>
<body style="margin:0;padding:22px;background:#F2F6FB;font-family:'Segoe UI',Arial,sans-serif;color:#0F172A;">
  <table width="600" cellpadding="0" cellspacing="0"
         style="max-width:600px;width:100%;background:#fff;border:1px solid #DDE5EF;border-radius:10px;">
    <tr><td style="padding:20px 22px;border-bottom:3px solid #FFC72C;">
      <div style="font-size:11px;letter-spacing:.6px;text-transform:uppercase;color:#5A6B85;">
        {html.escape(LEAD_TYPE_LABELS.get(lead.get("lead_type"), "Lead"))}
      </div>
      <div style="font-size:18px;font-weight:600;margin-top:4px;">{html.escape(topic)}</div>
    </td></tr>
    <tr><td style="padding:18px 22px;">
      <table cellpadding="0" cellspacing="0" style="font-size:14px;">
        {contact_rows}
        {_row("Wants", lead.get("detail"))}
        {_row("Category", lead.get("category"))}
      </table>
      <p style="font-size:13px;color:#5A6B85;line-height:1.55;margin:16px 0 0;">{html.escape(note)}</p>
      <p style="font-size:13px;margin:16px 0 0;">
        {_link(f'/chat-insights/conversation/{lead["conversation_id"]}', 'Read the full transcript')}
        &nbsp;&middot;&nbsp;
        {_link('/chat-insights/leads', 'Open the lead list')}
      </p>
    </td></tr>
  </table>
</body></html>"""
    return subject, body


def send_lead_alerts(limit: int | None = None) -> dict:
    """Emails every lead that has not been alerted on yet, one message each.

    Marked as alerted only after the send succeeds, so a relay outage means the
    alert goes out on the next attempt rather than being lost. The flip side --
    a send that succeeds but is recorded as failed would re-alert -- is the
    error worth having: a duplicate is noise, a miss is a lost customer.
    """
    if not settings.CHAT_ALERT_LEADS:
        return {"sent": 0, "failed": 0, "pending": 0, "skipped": "alert_on_leads is off"}

    wanted = set(settings.CHAT_ALERT_LEAD_TYPES)
    # Types not being alerted on stay unalerted rather than being marked as
    # sent, so switching intent_only on later still surfaces the backlog.
    pending = [l for l in db.leads_awaiting_alert() if l.get("lead_type") in wanted]
    if limit:
        pending = pending[:limit]
    sent = failed = 0
    for lead in pending:
        subject, body = render_lead_email(lead)
        outcome = mailer.send(subject, body, purpose="lead_alerts")
        if outcome["success"]:
            db.mark_lead_alerted(lead["conversation_id"])
            sent += 1
        else:
            failed += 1
            logger.error("Lead alert failed for %s: %s",
                         lead["conversation_id"], outcome["detail"])
            # The relay is down or misconfigured; the rest would fail too.
            break
    if sent or failed:
        logger.info("Lead alerts: %s sent, %s failed, %s pending", sent, failed, len(pending))
    return {"sent": sent, "failed": failed, "pending": len(pending)}


def send_job_failure_alert(job: str, detail: str, *, log_file: str = "") -> dict:
    """Tells someone a scheduled job failed.

    Called from the jobs themselves rather than from Task Scheduler: the job
    knows what went wrong, whereas the scheduler only knows the exit code.
    """
    subject = f"Job failed: {job}"
    log_line = (f'<p style="font-size:13px;color:#5A6B85;margin:14px 0 0;">Full output: '
                f'<code>{html.escape(log_file)}</code></p>') if log_file else ""
    body = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>{html.escape(subject)}</title></head>
<body style="margin:0;padding:22px;background:#F2F6FB;font-family:'Segoe UI',Arial,sans-serif;color:#0F172A;">
  <table width="600" cellpadding="0" cellspacing="0"
         style="max-width:600px;width:100%;background:#fff;border:1px solid #F5C6C6;border-radius:10px;">
    <tr><td style="padding:20px 22px;border-bottom:3px solid #C62828;">
      <div style="font-size:11px;letter-spacing:.6px;text-transform:uppercase;color:#C62828;">
        Scheduled job failed</div>
      <div style="font-size:18px;font-weight:600;margin-top:4px;">{html.escape(job)}</div>
    </td></tr>
    <tr><td style="padding:18px 22px;">
      <pre style="background:#FDECEC;border-radius:8px;padding:12px 14px;font-size:12.5px;
                  white-space:pre-wrap;margin:0;">{html.escape(detail)[:4000]}</pre>
      {log_line}
      <p style="font-size:13px;margin:16px 0 0;">
        {_link('/settings/jobs', 'Job logs in the dashboard')}</p>
    </td></tr>
  </table>
</body></html>"""
    outcome = mailer.send(subject, body, purpose="job_failures")
    if not outcome["success"]:
        logger.error("Could not send the failure alert for %s: %s", job, outcome["detail"])
    return outcome
