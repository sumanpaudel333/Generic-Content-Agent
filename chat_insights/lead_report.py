"""
The daily lead email.

Goes to the sales team every morning covering the previous day's chats. It is a
call list, not a report: names, numbers, what they asked about, and which ones
are already expecting to hear from us.

Deliberately free of machinery. Nothing here mentions actions, tool calls,
Zapier, webhooks or hand-off states -- whether the chatbot's forwarding step
fired is our problem to fix, and a salesperson holding a phone cannot act on it.
It changes exactly one thing they care about, so that is all it says:

    "Expecting your call"      the customer was told someone would ring them
    "New enquiry"              they left details, nobody promised anything
    "Already sent through"     came through the normal way as well

The diagnostic detail behind those labels is kept on the lead record and shown
in the dashboard, where someone debugging the chatbot will look for it.

Email-client constraints as per chat_insights/reporter.py: inline styles, table
layout, no media queries.
"""
import html
from datetime import datetime, timezone

from chat_insights import alerts, handoff
from config import settings
from config.localtime import au_time, to_au

BLUE = settings.HEADING_COLOR or "#004495"
YELLOW = "#FFC72C"
INK = "#0F172A"
MUTED = "#5A6B85"
LINE = "#DDE5EF"
BG = "#F2F6FB"
RED = "#C62828"
RED_BG = "#FDECEC"
GREEN = "#0E8A4A"
AMBER = "#B26B00"
AMBER_BG = "#FFF6E3"

# (state, accent, tint, heading, one-line instruction)
# Wording is the test: if a line would not make sense read aloud to someone
# about to pick up the phone, it does not belong here.
SECTIONS = [
    (handoff.CLAIMED_NOT_FIRED, RED, RED_BG, "Expecting our call",
     "These customers were told someone would call them back. Please ring them first."),
    (handoff.NO_CLAIM, AMBER, AMBER_BG, "New enquiries",
     "They left their details in the chat."),
    (handoff.FIRED, GREEN, "#E6F6ED", "Already sent through",
     "These also came through the usual way, so they might be already contacted. Please verify it."),
]


def _esc(value) -> str:
    return html.escape(str(value if value is not None else ""))


def _local(epoch):
    """Chat timestamps are stored as UTC epochs; the sales team reads Australian
    Eastern time, whatever zone the server is set to."""
    return to_au(epoch)


def _fmt_time(epoch) -> str:
    """"9:05 am, Thu 17 Sep" -- an email now covers a few hours rather than one
    day, and the 04:00 run spans midnight, so the day is shown too."""
    when = _local(epoch)
    if not when:
        return ""
    return f"{au_time(when)}, {when:%a} {when.day} {when:%b}"


def _field(label: str, value_html: str, *, muted: bool = False) -> str:
    """One labelled row. A fixed four-field shape -- Name, Phone, Email,
    Inquiry -- so every card is read the same way and a missing field is
    visibly missing rather than silently absent."""
    colour = MUTED if muted else INK
    return (f'<tr>'
            f'<td valign="top" style="padding:3px 12px 3px 0;font-size:12px;color:{MUTED};'
            f'white-space:nowrap;width:64px;">{_esc(label)}</td>'
            f'<td valign="top" style="padding:3px 0;font-size:14px;color:{colour};'
            f'line-height:1.45;">{value_html}</td>'
            f'</tr>')


def _phone_html(phone: str) -> str:
    """tel: link -- this is read on a phone by someone about to make the call,
    and retyping a number is where that stops happening."""
    digits = "".join(ch for ch in phone if ch.isdigit() or ch == "+")
    return (f'<a href="tel:{_esc(digits)}" style="color:{BLUE};text-decoration:none;'
            f'font-weight:700;">{_esc(phone)}</a>')


def _email_html(email: str) -> str:
    return (f'<a href="mailto:{_esc(email)}" style="color:{BLUE};text-decoration:none;'
            f'font-weight:600;word-break:break-all;">{_esc(email)}</a>')


def _lead_card(lead: dict, accent: str) -> str:
    name = (lead.get("contact_name") or "").strip()
    phone = (lead.get("contact_phone") or "").strip()
    email = (lead.get("contact_email") or "").strip()
    # What they want, falling back to the conversation's topic -- the enquiry
    # line should never be blank, or the card gives no reason to call.
    inquiry = (lead.get("detail") or "").strip() or (lead.get("topic") or "").strip()

    missing = f'<span style="color:{MUTED};">&mdash;</span>'
    rows = (
        _field("Name", _esc(name) if name else missing, muted=not name)
        + _field("Phone", _phone_html(phone) if phone else missing, muted=not phone)
        + _field("Email", _email_html(email) if email else missing, muted=not email)
        + _field("Inquiry", _esc(inquiry) if inquiry else missing, muted=not inquiry)
    )

    time_label = _fmt_time(lead.get("created_at"))
    when = (f'<div style="font-size:12px;color:{MUTED};margin-bottom:9px;">{_esc(time_label)}</div>'
             if time_label else "")

    # The transcript link is the card's escape hatch: everything the extraction
    # did not pull out -- company name, delivery suburb, quantities, what the
    # bot actually said -- is one click away in the full conversation.
    transcript = alerts.dashboard_url(
        f'/chat-insights/conversation/{lead["conversation_id"]}')
    link = ""
    if transcript:
        link = (f'<div style="margin-top:11px;">'
                 f'<a href="{_esc(transcript)}" '
                 f'style="display:inline-block;background:{BG};border:1px solid {LINE};'
                 f'border-radius:6px;padding:6px 12px;font-size:12.5px;font-weight:600;'
                 f'color:{BLUE};text-decoration:none;">Read the full conversation &rarr;</a></div>')

    return f"""
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"
           style="background:#ffffff;border:1px solid {LINE};border-left:3px solid {accent};
                  border-radius:8px;margin-bottom:10px;">
      <tr><td style="padding:13px 15px;">
        {when}
        <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%">
          {rows}
        </table>
        {link}
      </td></tr>
    </table>"""


def _empty_state(day_label: str) -> str:
    return (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
            f'style="background:#ffffff;border:1px dashed {LINE};border-radius:8px;">'
            f'<tr><td style="padding:26px 20px;text-align:center;font-size:14px;color:{MUTED};">'
            f'<b style="color:{INK};display:block;font-size:15px;margin-bottom:4px;">'
            f'No leads {_esc(day_label)}.</b>'
            f'Nobody left contact details in the chat.'
            f'</td></tr></table>')


def group(leads: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for lead in leads:
        out.setdefault(lead.get("handoff_state") or handoff.NO_CLAIM, []).append(lead)
    return out


CARRIED_TITLE = "From earlier -- not emailed yet"


def split_carried(leads: list[dict], window_start_epoch: int | None) -> tuple[list[dict], list[dict]]:
    """(leads from chats inside the window, leads carried in from before it).

    A lead is carried in when its chat started before the window but it was
    never emailed -- it only got contact details after its own run, or its run
    failed or did not happen. Shown apart so an email for "1:00 pm to 3:00 pm"
    does not appear to contain a 9:28 am chat for no reason.
    """
    if window_start_epoch is None:
        return list(leads), []
    inside = [l for l in leads if (l.get("created_at") or 0) >= window_start_epoch]
    carried = [l for l in leads if (l.get("created_at") or 0) < window_start_epoch]
    return inside, carried


def render_subject(grouped: dict[str, list[dict]], *, day_label: str = "yesterday",
                   carried: int = 0) -> str:
    waiting = len(grouped.get(handoff.CLAIMED_NOT_FIRED, []))
    total = sum(len(v) for v in grouped.values())
    earlier = f", {carried} from earlier" if carried else ""
    if not total:
        return f"Chat leads {day_label} -- none"
    if waiting:
        return (f"Chat leads {day_label} -- {total} to call "
                f"({waiting} expecting a callback{earlier})")
    return f"Chat leads {day_label} -- {total} to call" + (f" ({carried} from earlier)" if carried else "")


def _carried_block(carried: list[dict], window_start_epoch: int | None) -> str:
    if not carried:
        return ""
    since = _fmt_time(window_start_epoch) if window_start_epoch else "this email's time"
    accents = {state: accent for state, accent, _t, _h, _i in SECTIONS}
    cards = "".join(_lead_card(lead, accents.get(lead.get("handoff_state"), AMBER))
                    for lead in carried)
    return f"""
        <tr><td style="padding:4px 0 0 0;">
          <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"
                 style="background:#EEF2F7;border-radius:8px;margin-bottom:10px;">
            <tr><td style="padding:10px 14px;">
              <div style="font-size:15px;font-weight:700;color:{MUTED};">
                {CARRIED_TITLE} &nbsp;<span style="font-size:13px;">({len(carried)})</span></div>
              <div style="font-size:13px;color:{INK};margin-top:3px;">These chats started before
                {_esc(since)}, but had not been emailed to anyone yet. Please check whether they
                have already been called.</div>
            </td></tr>
          </table>
          {cards}
        </td></tr>"""


def render_report(leads: list[dict], *, day_label: str = "yesterday",
                   day_date: str = "", window_start_epoch: int | None = None) -> str:
    all_leads = leads
    leads, carried = split_carried(all_leads, window_start_epoch)
    grouped = group(leads)
    total = len(all_leads)
    waiting = len(group(all_leads).get(handoff.CLAIMED_NOT_FIRED, []))

    banner = ""
    if waiting:
        banner = f"""
        <tr><td style="padding:0 0 15px 0;">
          <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"
                 style="background:{RED_BG};border:1px solid #F5C6C6;border-left:4px solid {RED};
                        border-radius:8px;">
            <tr><td style="padding:13px 16px;font-size:14.5px;font-weight:700;color:{RED};">
              {waiting} {'customer is' if waiting == 1 else 'customers are'} waiting on a
              callback we promised.
            </td></tr>
          </table>
        </td></tr>"""

    sections = ""
    for state, accent, tint, title, instruction in SECTIONS:
        items = grouped.get(state, [])
        if not items:
            continue
        cards = "".join(_lead_card(lead, accent) for lead in items)
        sections += f"""
        <tr><td style="padding:4px 0 0 0;">
          <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"
                 style="background:{tint};border-radius:8px;margin-bottom:10px;">
            <tr><td style="padding:10px 14px;">
              <div style="font-size:15px;font-weight:700;color:{accent};">
                {title} &nbsp;<span style="font-size:13px;">({len(items)})</span></div>
              <div style="font-size:13px;color:{INK};margin-top:3px;">{instruction}</div>
            </td></tr>
          </table>
          {cards}
        </td></tr>"""

    sections += _carried_block(carried, window_start_epoch)

    if not sections:
        sections = f'<tr><td style="padding:4px 0 0 0;">{_empty_state(day_label)}</td></tr>'

    list_link = alerts.dashboard_url("/chat-insights/leads")
    footer = (f'<a href="{_esc(list_link)}" style="color:{BLUE};">Mark these off as you call them</a>'
               if list_link else "Mark these off in the dashboard as you call them.")

    preheader = (f'{total} to call. {waiting} expecting a callback.' if waiting
                  else (f'{total} to call.' if total else f'No leads {day_label}.'))

    heading_date = day_date or day_label.title()

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Chat leads {_esc(heading_date)}</title></head>
<body style="margin:0;padding:0;background:{BG};font-family:'Segoe UI',Arial,sans-serif;color:{INK};">
<div style="display:none;font-size:1px;color:{BG};line-height:1px;max-height:0;max-width:0;opacity:0;overflow:hidden;">
{_esc(preheader)}
</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"
       style="background:{BG};padding:22px 0;">
<tr><td align="center">
<table role="presentation" width="620" cellpadding="0" cellspacing="0" border="0"
       style="max-width:620px;width:100%;">

  <tr><td style="background:{BLUE};padding:18px 22px;border-radius:10px 10px 0 0;">
    <div style="color:#ffffff;font-size:18px;font-weight:700;">Chat leads to call</div>
    <div style="color:rgba(255,255,255,.82);font-size:13px;margin-top:3px;">
      {_esc(settings.BUSINESS_NAME)} &middot; {_esc(heading_date)}
    </div>
  </td></tr>

  <tr><td style="background:#ffffff;padding:20px 22px;border:1px solid {LINE};border-top:none;
                 border-radius:0 0 10px 10px;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
      {banner}
      {sections}
      <tr><td style="padding:18px 0 0 0;">
        <div style="font-size:11.5px;color:{MUTED};border-top:1px solid {LINE};padding-top:11px;">
          {footer}
        </div>
      </td></tr>
    </table>
  </td></tr>

</table>
</td></tr></table>
</body></html>"""


def render_text_report(leads: list[dict], *, day_label: str = "yesterday",
                        day_date: str = "", window_start_epoch: int | None = None) -> str:
    """Plain-text alternative -- what mail search indexes, and what survives a
    forward with a question on top."""
    lines = [f"CHAT LEADS TO CALL -- {settings.BUSINESS_NAME}",
             day_date or day_label.title(), ""]

    if not leads:
        lines += [f"No leads {day_label}. Nobody left contact details in the chat.", ""]
        return "\n".join(lines)

    inside, carried = split_carried(leads, window_start_epoch)
    grouped = group(inside)
    since = _fmt_time(window_start_epoch) if window_start_epoch else "this email's time"
    blocks = [(state, title, instruction, grouped.get(state, []))
              for state, _accent, _tint, title, instruction in SECTIONS]
    blocks.append(("carried", CARRIED_TITLE,
                   f"These chats started before {since}, but had not been emailed to anyone "
                   "yet. Please check whether they have already been called.", carried))
    for state, title, instruction, items in blocks:
        if not items:
            continue
        lines.append(f"{title.upper()} ({len(items)})")
        lines.append(f"  {instruction}")
        lines.append("")
        for lead in items:
            when = _fmt_time(lead.get("created_at"))
            lines.append(f"  {when}" if when else "  --")
            lines.append(f"    Name    : {lead.get('contact_name') or '-'}")
            lines.append(f"    Phone   : {lead.get('contact_phone') or '-'}")
            lines.append(f"    Email   : {lead.get('contact_email') or '-'}")
            inquiry = (lead.get("detail") or "").strip() or (lead.get("topic") or "-")
            lines.append(f"    Inquiry : {inquiry}")
            transcript = alerts.dashboard_url(
                f"/chat-insights/conversation/{lead['conversation_id']}")
            if transcript:
                lines.append(f"    Full conversation: {transcript}")
            lines.append("")
        lines.append("")

    list_link = alerts.dashboard_url("/chat-insights/leads")
    if list_link:
        lines.append(f"Mark these off as you call them: {list_link}")
    return "\n".join(lines)
