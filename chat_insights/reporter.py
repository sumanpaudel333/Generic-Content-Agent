"""
Renders the weekly report as HTML (and as plain text).

Every style is inline: Outlook and most webmail clients strip <style> blocks, so
a stylesheet would arrive as unstyled text. Table-based layout for the same
reason -- flexbox/grid support in email clients is unreliable. Bars are nested
tables with a bgcolor rather than styled divs, because Outlook renders through
Word, which ignores border-radius and mishandles a div given a percentage width.

Ordered for a reader working top-down in an inbox:

  1. the numbers, with movement against last week
  2. what customers asked about -- keywords, then categories, then volume
  3. unhappy customers

Two lists that used to live here are gone. The per-lead list moved out entirely:
leads are a worklist, they go to sales every morning in their own email (see
lead_report.py), and repeating them weekly to a different audience only made
them look like something to read rather than something to action. The unanswered
questions came out with it -- the keyword table already carries that signal in
an aggregated form ("delivery: 7 conversations, 7 of them unanswered"), which is
the version that says what to fix rather than listing every instance.

The COUNTS for both stay in the headline tiles: they are the week's numbers, and
that is what this report is for.

Rendered twice by weekly_run: once in full (stored and shown in the dashboard)
and once redacted (emailed), per the PII decision.
"""
import html
from datetime import datetime, timezone

from chat_insights import alerts, analyzer, redact
from config import settings

BLUE = settings.HEADING_COLOR or "#004495"
YELLOW = "#FFC72C"
INK = "#0F172A"
MUTED = "#5A6B85"
LINE = "#DDE5EF"
BG = "#F2F6FB"

GREEN = "#0E8A4A"
RED = "#C62828"
AMBER = "#B26B00"

_MAX_ITEMS_PER_SECTION = 15
_MAX_KEYWORDS = 10


def _esc(v) -> str:
    return html.escape(str(v if v is not None else ""))


def _fmt_date(epoch) -> str:
    if not epoch:
        return "--"
    try:
        return datetime.fromtimestamp(int(epoch), tz=timezone.utc).strftime("%a %d %b")
    except (ValueError, OSError, TypeError):
        return "--"


def _delta(current, previous, *, higher_is_better: bool = True, suffix: str = "") -> str:
    """Movement against the same figure last week.

    The whole point of a weekly report is the trend, and a bare number does not
    carry one -- "12 unanswered" reads completely differently depending on
    whether last week was 3 or 30. Returns "" when there is no previous run to
    compare against, so the first report does not show a fake baseline.
    """
    if previous is None or current is None:
        return ""
    try:
        diff = round(float(current) - float(previous), 1)
    except (TypeError, ValueError):
        return ""
    if abs(diff) < 0.05:
        return (f'<div style="font-size:11px;color:{MUTED};margin-top:3px;">'
                f'&#9644; level vs last week</div>')
    up = diff > 0
    good = up if higher_is_better else not up
    colour = GREEN if good else RED
    arrow = "&#9650;" if up else "&#9660;"
    shown = f"{abs(diff):g}{suffix}"
    return (f'<div style="font-size:11px;color:{colour};margin-top:3px;font-weight:600;">'
            f'{arrow} {shown} vs last week</div>')


def _stat_cell(label: str, value, accent: str = BLUE, delta: str = "") -> str:
    """One headline tile. Returns the box only -- _tile_row does the layout."""
    return (
        f'<div style="padding:14px 16px;background:#ffffff;border:1px solid {LINE};'
        f'border-left:3px solid {accent};border-radius:8px;'
        f'font-size:13px;line-height:1.35;">'
        f'<div style="font-size:11px;text-transform:uppercase;letter-spacing:.6px;'
        f'color:{MUTED};font-weight:700;">{_esc(label)}</div>'
        f'<div style="font-size:24px;font-weight:700;color:{INK};margin-top:4px;">{value}</div>'
        f'{delta}'
        f'</div>'
    )


def _mini_stat(label: str, value) -> str:
    """Secondary figure: smaller, no accent bar. Context for the headline
    tiles, not a headline itself."""
    return (
        f'<div style="padding:10px 12px;background:#ffffff;border:1px solid {LINE};'
        f'border-radius:8px;font-size:13px;line-height:1.35;">'
        f'<div style="font-size:10.5px;text-transform:uppercase;letter-spacing:.5px;'
        f'color:{MUTED};font-weight:700;">{_esc(label)}</div>'
        f'<div style="font-size:15px;font-weight:700;color:{INK};margin-top:3px;">{value}</div>'
        f'</div>'
    )


def _tile_row(tiles: list[str], *, columns: int = 4, tile_width: int = 152) -> str:
    """Lays tiles out so they reflow on a phone.

    Four tiles in four table cells is what this used to be, and on a 375px
    screen the fourth one fell off the edge -- which matters, because a weekly
    summary is exactly the kind of mail that gets read on a phone.

    Media queries are not an option: several clients strip <style> blocks, so a
    breakpoint would only work in some inboxes. This is the fluid-hybrid
    approach instead. Each tile is an inline-block with a max width, so they sit
    four across at 680px and wrap to two (or one) as the screen narrows, with no
    stylesheet involved. Outlook ignores display:inline-block on a div, so it
    gets a real table through the MSO conditional -- invisible to every other
    client, and the only way to keep Outlook from stacking all four full width.
    """
    width_pct = round(100 / columns)
    parts = [
        '<!--[if mso]><table role="presentation" width="100%" cellpadding="0" '
        'cellspacing="0" border="0"><tr><![endif]-->'
    ]
    for tile in tiles:
        parts.append(
            f'<!--[if mso]><td width="{width_pct}%" valign="top" style="padding:3px;">'
            f'<![endif]-->'
            f'<div style="display:inline-block;vertical-align:top;width:100%;'
            f'max-width:{tile_width}px;padding:3px;box-sizing:border-box;">{tile}</div>'
            f'<!--[if mso]></td><![endif]-->'
        )
    parts.append('<!--[if mso]></tr></table><![endif]-->')
    # font-size:0 on the wrapper kills the whitespace gaps between inline-blocks;
    # each tile sets its own sizes back.
    return (f'<div style="font-size:0;line-height:0;">{"".join(parts)}</div>')


def _bar(pct: int, colour: str) -> str:
    """A progress bar built from nested tables.

    The obvious version -- a div with a percentage width and a background --
    renders as a full-width block in Outlook, which turns every bar into 100%.
    A table cell with a bgcolor and a width attribute is the one construction
    that behaves the same in Outlook, Gmail and Apple Mail.
    """
    filled = max(min(int(pct), 100), 2)
    return (
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        f'style="border-collapse:collapse;"><tr>'
        f'<td width="{filled}%" bgcolor="{colour}" height="9" '
        f'style="background:{colour};height:9px;font-size:0;line-height:0;border-radius:4px;">&nbsp;</td>'
        f'<td width="{100 - filled}%" bgcolor="{BG}" height="9" '
        f'style="background:{BG};height:9px;font-size:0;line-height:0;">&nbsp;</td>'
        f'</tr></table>'
    )


def _failure_kind_rows(kinds: dict) -> str:
    """Why the bot could not answer, split by what would fix it.

    A summary, not a list. The per-conversation list was removed on purpose --
    this is the same information in the form that says what to do, and it stays
    three rows however bad the week was.
    """
    if not kinds:
        return ""
    total = sum(kinds.values()) or 1
    accents = {"no_content": RED, "weak_context": AMBER,
               "deflected": BLUE, "other": MUTED}
    rows = ""
    for kind in analyzer.FAILURE_KINDS:
        count = kinds.get(kind, 0)
        if not count:
            continue
        accent = accents.get(kind, MUTED)
        share = round(100 * count / total)
        rows += (
            f'<tr>'
            f'<td style="padding:10px 12px;border-top:1px solid {LINE};" width="42%">'
            f'<div style="font-size:13.5px;font-weight:700;color:{accent};">'
            f'{count} &middot; {_esc(analyzer.FAILURE_KIND_LABELS[kind])}</div></td>'
            f'<td style="padding:10px 12px;border-top:1px solid {LINE};" width="26%">'
            f'{_bar(share, accent)}'
            f'<div style="font-size:11px;color:{MUTED};margin-top:3px;">{share}%</div></td>'
            f'<td style="padding:10px 12px;font-size:12.5px;color:{INK};line-height:1.45;'
            f'border-top:1px solid {LINE};">{_esc(analyzer.FAILURE_KIND_ACTIONS[kind])}</td>'
            f'</tr>'
        )
    return (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
            f'style="background:#fff;border:1px solid {LINE};border-radius:8px;'
            f'border-collapse:separate;">{rows}</table>')


def _keyword_rows(keywords: list[dict], *, redacted: bool = False) -> str:
    """What customers actually typed, and how often the bot could not help.

    The second column is the reason this section exists. A term that comes up
    often is interesting; a term that comes up often AND that the bot keeps
    failing on is a content gap with demand already attached to it.
    """
    if not keywords:
        return (f'<div style="font-size:13px;color:{MUTED};background:#fff;'
                f'border:1px dashed {LINE};border-radius:8px;padding:16px;text-align:center;">'
                f'Not enough conversations this week to pick out recurring terms.</div>')

    shown = keywords[:_MAX_KEYWORDS]
    peak = max(k["conversations"] for k in shown)
    rows = (f'<tr>'
            f'<td style="padding:8px 12px;font-size:10.5px;text-transform:uppercase;'
            f'letter-spacing:.5px;color:{MUTED};font-weight:700;">Term</td>'
            f'<td style="padding:8px 12px;font-size:10.5px;text-transform:uppercase;'
            f'letter-spacing:.5px;color:{MUTED};font-weight:700;" width="34%">Conversations</td>'
            f'<td style="padding:8px 12px;font-size:10.5px;text-transform:uppercase;'
            f'letter-spacing:.5px;color:{MUTED};font-weight:700;text-align:right;">'
            f'Bot failed</td></tr>')
    for k in shown:
        term = redact.redact(k["term"]) if redacted else k["term"]
        width = round(100 * k["conversations"] / peak) if peak else 2
        unanswered = k.get("unanswered", 0)
        if unanswered:
            share = round(100 * unanswered / k["conversations"])
            flag = (f'<span style="color:{RED};font-weight:700;">{unanswered}</span>'
                     f'<span style="color:{MUTED};"> of {k["conversations"]} ({share}%)</span>')
        else:
            flag = f'<span style="color:{MUTED};">--</span>'
        rows += (
            f'<tr>'
            f'<td style="padding:8px 12px;font-size:13px;color:{INK};font-weight:600;'
            f'border-top:1px solid {LINE};">{_esc(term)}</td>'
            f'<td style="padding:8px 12px;border-top:1px solid {LINE};">'
            f'{_bar(width, BLUE)}'
            f'<div style="font-size:11px;color:{MUTED};margin-top:3px;">'
            f'{k["conversations"]} ({k["share"]}% of chats)</div></td>'
            f'<td style="padding:8px 12px;font-size:12px;text-align:right;'
            f'border-top:1px solid {LINE};">{flag}</td>'
            f'</tr>'
        )
    return (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
            f'style="background:#fff;border:1px solid {LINE};border-radius:8px;'
            f'border-collapse:separate;">{rows}</table>')


def _section(title: str, subtitle: str, inner: str) -> str:
    return (
        f'<tr><td style="padding:26px 0 0 0;">'
        f'<h2 style="font-size:16px;color:{BLUE};margin:0 0 2px 0;">{_esc(title)}</h2>'
        f'<div style="font-size:12px;color:{MUTED};margin-bottom:10px;">{_esc(subtitle)}</div>'
        f'{inner}</td></tr>'
    )


def _item_list(items: list[dict], *, empty_text: str, show: str = "summary",
                redacted: bool = False) -> str:
    if not items:
        return (f'<div style="font-size:13px;color:{MUTED};background:#fff;border:1px dashed {LINE};'
                f'border-radius:8px;padding:16px;text-align:center;">{_esc(empty_text)}</div>')

    rows = ""
    for item in items[:_MAX_ITEMS_PER_SECTION]:
        detail = item.get(show) or item.get("summary") or ""
        if redacted:
            detail = redact.redact(detail)
        extra = ""
        reason = item.get("reason")
        if reason and show != "reason":
            extra = (f'<div style="font-size:12px;color:#B26B00;margin-top:3px;">'
                      f'{_esc(reason)}</div>')
        # Link straight to the conversation. The emailed report is redacted and
        # summarised, so the reader's next question is always "what did they
        # actually say?" -- without a link that means finding the week, opening
        # the transcript list and hunting for the row.
        transcript = alerts.dashboard_url(f'/chat-insights/conversation/{item.get("id", "")}')
        link = (f' &nbsp;<a href="{_esc(transcript)}" style="color:{BLUE};'
                 f'text-decoration:none;font-weight:600;">Read the full conversation &rarr;</a>'
                 ) if transcript and item.get("id") else ""
        rows += (
            f'<tr><td style="padding:11px 14px;border-bottom:1px solid {LINE};">'
            f'<div style="font-size:13px;font-weight:600;color:{INK};">'
            f'{_esc(redact.redact(item.get("topic", "")) if redacted else item.get("topic", ""))}</div>'
            f'<div style="font-size:12.5px;color:{MUTED};margin-top:2px;">{_esc(detail)}</div>'
            f'{extra}'
            f'<div style="font-size:11px;color:{MUTED};margin-top:4px;">'
            f'{_fmt_date(item.get("created_at"))}{link}</div>'
            f'</td></tr>'
        )

    more = ""
    if len(items) > _MAX_ITEMS_PER_SECTION:
        more = (f'<tr><td style="padding:9px 14px;font-size:12px;color:{MUTED};">'
                f'+ {len(items) - _MAX_ITEMS_PER_SECTION} more -- see the dashboard for the full list.'
                f'</td></tr>')

    return (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
            f'style="background:#fff;border:1px solid {LINE};border-radius:8px;">'
            f'{rows}{more}</table>')


def _bar_rows(counts: dict, total: int, redacted: bool = False) -> str:
    if not counts:
        return ""
    rows = ""
    for key, count in counts.items():
        pct = round(100 * count / total) if total else 0
        rows += (
            f'<tr>'
            f'<td style="padding:7px 12px;font-size:13px;color:{INK};width:45%;">'
            f'{analyzer.category_label(key)}</td>'
            f'<td style="padding:7px 12px;width:40%;">{_bar(pct, BLUE)}</td>'
            f'<td style="padding:7px 12px;font-size:12.5px;color:{MUTED};text-align:right;">'
            f'{count} ({pct}%)</td></tr>'
        )
    return (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
            f'style="background:#fff;border:1px solid {LINE};border-radius:8px;">{rows}</table>')


def render_report(stats: dict, week_start: str, week_end: str, *, redacted: bool = False,
                   truncated: bool = False, model_note: str = "",
                   previous: dict | None = None, dashboard_url: str = "") -> str:
    total = stats.get("total_conversations", 0)
    prev = previous or {}
    failures = stats.get("failures", [])
    leads = stats.get("leads", [])
    keywords = stats.get("keywords", [])

    warning = ""
    if truncated:
        warning = (
            f'<tr><td style="padding:14px 16px;background:#FFF6E3;border:1px solid {YELLOW};'
            f'border-radius:8px;font-size:13px;color:{AMBER};">'
            f'<b>Partial week.</b> Paging stopped at the configured limit, so some '
            f'conversations may be missing from these numbers.</td></tr>'
            f'<tr><td style="height:12px;"></td></tr>'
        )

    # --- headline numbers, with movement against last week -------------------
    prev_failures = len(prev.get("failures", [])) if prev else None
    prev_leads = len(prev.get("leads", [])) if prev else None
    prev_total = prev.get("total_conversations") if prev else None
    prev_rate = prev.get("resolution_rate") if prev else None
    headline = _tile_row([
        _stat_cell("Conversations", total, BLUE, _delta(total, prev_total)),
        _stat_cell("Resolution rate", str(stats.get("resolution_rate", 0)) + "%", GREEN,
                    _delta(stats.get("resolution_rate"), prev_rate, suffix="pts")),
        _stat_cell("Possible leads", len(leads), YELLOW, _delta(len(leads), prev_leads)),
        _stat_cell("Bot could not help", len(failures), RED,
                    _delta(len(failures), prev_failures, higher_is_better=False)),
    ], columns=4, tile_width=152)

    # --- the supporting numbers, on the same screen as the headline ----------
    sentiments = stats.get("sentiments", {}) or {}
    sources = stats.get("sources", {}) or {}
    top_source = next(iter(sources), "--")
    resolved_label = f'{stats.get("resolved_count", 0)} of {total}'
    busiest_label = f'{stats.get("busiest_day", "--")} ({stats.get("busiest_day_count", 0)})'
    secondary = _tile_row([
        _mini_stat("Messages", stats.get("total_messages", 0)),
        _mini_stat("Avg per chat", stats.get("avg_messages", 0)),
        _mini_stat("Resolved", resolved_label),
        _mini_stat("Busiest day", busiest_label),
    ], columns=4, tile_width=152) + _tile_row([
        _mini_stat("Positive", sentiments.get("positive", 0)),
        _mini_stat("Negative", sentiments.get("negative", 0)),
        _mini_stat("Thumbs-down", stats.get("negative_feedback_messages", 0)),
        _mini_stat("Top channel", _esc(top_source)),
    ], columns=4, tile_width=152)

    # --- volume by day -------------------------------------------------------
    per_day = stats.get("per_day", {})
    volume_rows = ""
    if per_day:
        peak = max(per_day.values())
        for day, count in per_day.items():
            width = max(round(100 * count / peak), 2) if peak else 2
            label = _fmt_date(datetime.strptime(day, "%Y-%m-%d").replace(
                tzinfo=timezone.utc).timestamp())
            volume_rows += (
                f'<tr><td style="padding:6px 12px;font-size:13px;color:{INK};width:30%;">{label}</td>'
                f'<td style="padding:6px 12px;width:55%;">{_bar(width, YELLOW)}</td>'
                f'<td style="padding:6px 12px;font-size:12.5px;color:{MUTED};text-align:right;">'
                f'{count}</td></tr>'
            )
    volume_table = (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
                     f'border="0" style="background:#fff;border:1px solid {LINE};border-radius:8px;">'
                     f'{volume_rows}</table>') if volume_rows else ""

    # --- inbox preview text --------------------------------------------------
    # The snippet a mail client shows beside the subject in the message list.
    # Without one it grabs whatever text comes first -- which used to be the
    # word "Conversations" -- so the reader learned nothing before opening it.
    preheader = (f'{total} conversations, {stats.get("resolution_rate", 0)}% resolved, '
                  f'{len(leads)} lead(s), {len(failures)} the bot could not answer.')
    if keywords:
        preheader += f' Top term: {keywords[0]["term"]}.'

    link_row = ""
    if dashboard_url:
        link_row = (
            f'<tr><td style="padding:20px 0 0 0;" align="center">'
            f'<a href="{_esc(dashboard_url)}" '
            f'style="display:inline-block;background:{BLUE};color:#ffffff;text-decoration:none;'
            f'font-size:13px;font-weight:600;padding:11px 22px;border-radius:8px;">'
            f'Open the full report</a></td></tr>'
        )

    footer_note = (
        f'Put together automatically from the week\'s website chats. '
        f'{_esc(model_note)} '
        + ("Customer contact details have been removed from this email. Full, unredacted "
            "transcripts are in the Content and Automation dashboard, under Chat Insights -- "
            "open the week's report and follow the Transcripts link."
            if redacted else "")
    )

    keyword_blurb = ("What customers typed, and how often the bot could not help with it. "
                      "A term high on both counts is a content gap with demand behind it.")

    # Only when there were failures. On a clean week the tile already says zero
    # and a section explaining nothing is noise.
    failure_kinds = stats.get("failure_kinds") or {}
    failure_kinds_section = _section(
        "Why the bot could not answer",
        "The same failures split by what would fix them -- three different jobs, "
        "not one number.",
        _failure_kind_rows(failure_kinds)) if failure_kinds else ""
    volume_blurb = (f'{total} conversations, {stats.get("avg_messages", 0)} messages each on '
                     f'average. Busiest: {stats.get("busiest_day", "--")} '
                     f'({stats.get("busiest_day_count", 0)}).')

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Chat report {_esc(week_start)}</title></head>
<body style="margin:0;padding:0;background:{BG};font-family:'Segoe UI',Arial,sans-serif;color:{INK};">
<div style="display:none;font-size:1px;color:{BG};line-height:1px;max-height:0;max-width:0;opacity:0;overflow:hidden;">
{_esc(preheader)}
</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background:{BG};padding:22px 0;">
<tr><td align="center">
<table role="presentation" width="680" cellpadding="0" cellspacing="0" border="0" style="max-width:680px;width:100%;">

  <tr><td style="background:{BLUE};padding:20px 24px;border-radius:10px 10px 0 0;">
    <div style="color:#fff;font-size:19px;font-weight:700;">Weekly Chat Report</div>
    <div style="color:rgba(255,255,255,.8);font-size:13px;margin-top:3px;">
      {_esc(settings.BUSINESS_NAME)} &middot; {_esc(week_start)} to {_esc(week_end)}
    </div>
  </td></tr>

  <tr><td style="background:#fff;padding:22px 24px;border:1px solid {LINE};border-top:none;
                 border-radius:0 0 10px 10px;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
      {warning}
      <tr><td>{headline}</td></tr>
      <tr><td style="height:8px;font-size:0;line-height:0;">&nbsp;</td></tr>
      <tr><td>{secondary}</td></tr>

      {_section("Popular keywords", keyword_blurb, _keyword_rows(keywords, redacted=redacted))}

      {failure_kinds_section}

      {_section("What customers asked about",
                 "Conversations grouped by topic.",
                 _bar_rows(stats.get("categories", {}), total, redacted))}

      {_section("Volume by day", volume_blurb, volume_table)}

      {_section("Unhappy customers",
                 "Negative sentiment or an explicit thumbs-down.",
                 _item_list(stats.get("unhappy", []),
                             empty_text="No negative feedback this week.", redacted=redacted))}

      {link_row}

      <tr><td style="padding:24px 0 0 0;">
        <div style="font-size:11.5px;color:{MUTED};border-top:1px solid {LINE};padding-top:12px;">
          {footer_note}
        </div>
      </td></tr>
    </table>
  </td></tr>

</table>
</td></tr></table>
</body></html>"""


def render_text_report(stats: dict, week_start: str, week_end: str, *,
                        redacted: bool = False, dashboard_url: str = "") -> str:
    """Plain-text version, sent as the message's alternative part.

    Worth having for more than clients that cannot render HTML: it is what a
    mail search actually indexes, and it is what gets quoted when somebody
    forwards the report with a question on top of it.
    """
    total = stats.get("total_conversations", 0)
    failures = stats.get("failures", [])
    leads = stats.get("leads", [])

    def clean(value) -> str:
        text = str(value or "")
        return redact.redact(text) if redacted else text

    lines = [
        f"WEEKLY CHAT REPORT -- {settings.BUSINESS_NAME}",
        f"{week_start} to {week_end}",
        "",
        "AT A GLANCE",
        f"  Conversations       {total}",
        f"  Resolution rate     {stats.get('resolution_rate', 0)}%",
        f"  Possible leads      {len(leads)}",
        f"  Bot could not help  {len(failures)}",
        f"  Messages            {stats.get('total_messages', 0)}"
        f" ({stats.get('avg_messages', 0)} per chat)",
        f"  Busiest day         {stats.get('busiest_day', '--')}"
        f" ({stats.get('busiest_day_count', 0)})",
        "",
    ]

    keywords = stats.get("keywords", [])
    if keywords:
        lines.append("POPULAR KEYWORDS")
        for k in keywords[:_MAX_KEYWORDS]:
            note = f", {k['unanswered']} unanswered" if k.get("unanswered") else ""
            lines.append(f"  {clean(k['term'])} -- {k['conversations']} conversations{note}")
        lines.append("")

    failure_kinds = stats.get("failure_kinds") or {}
    if failure_kinds:
        lines.append("WHY THE BOT COULD NOT ANSWER")
        for kind in analyzer.FAILURE_KINDS:
            count = failure_kinds.get(kind, 0)
            if not count:
                continue
            lines.append(f"  {count} -- {analyzer.FAILURE_KIND_LABELS[kind]}")
            lines.append(f"       {analyzer.FAILURE_KIND_ACTIONS[kind]}")
        lines.append("")

    categories = stats.get("categories", {}) or {}
    if categories:
        lines.append("WHAT CUSTOMERS ASKED ABOUT")
        for key, count in categories.items():
            pct = round(100 * count / total) if total else 0
            lines.append(f"  {html.unescape(analyzer.category_label(key))} -- {count} ({pct}%)")
        lines.append("")

    if dashboard_url:
        lines.append(f"Full report: {dashboard_url}")
    if redacted:
        lines.append("Customer contact details have been removed from this email. "
                      "Full transcripts are in the dashboard under Chat Insights.")
    return "\n".join(lines)


def render_subject(stats: dict, week_start: str, week_end: str,
                    previous: dict | None = None) -> str:
    """Subject carries the week's numbers and, once there is a week to compare
    against, the direction of travel -- so a row of these in an inbox reads as a
    trend without opening any of them."""
    failures = len(stats.get("failures", []))
    total = stats.get("total_conversations", 0)
    resolution = stats.get("resolution_rate", 0)

    trend = ""
    if previous:
        prev_failures = len(previous.get("failures", []))
        diff = failures - prev_failures
        if diff > 0:
            trend = f" (unanswered up {diff})"
        elif diff < 0:
            trend = f" (unanswered down {abs(diff)})"

    # Leads are no longer named here: they are their own daily mailing, to a
    # different audience, and a weekly count of them steered attention at a
    # list this report does not contain.
    return (f"Weekly chat report {week_start} to {week_end} -- "
            f"{total} chats, {resolution}% resolved, {failures} unanswered{trend}")
