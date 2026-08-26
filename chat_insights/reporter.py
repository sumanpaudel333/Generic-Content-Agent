"""
Renders the weekly report as HTML.

Every style is inline: Outlook and most webmail clients strip <style> blocks, so
a stylesheet would arrive as unstyled text. Table-based layout for the same
reason -- flexbox/grid support in email clients is unreliable.

Rendered twice by weekly_run: once in full (stored and shown in the dashboard)
and once redacted (emailed), per the PII decision.
"""
import html
from datetime import datetime, timezone

from chat_insights import analyzer, redact
from config import settings

BLUE = settings.HEADING_COLOR or "#004495"
YELLOW = "#FFC72C"
INK = "#0F172A"
MUTED = "#5A6B85"
LINE = "#DDE5EF"
BG = "#F2F6FB"

_MAX_ITEMS_PER_SECTION = 15


def _esc(v) -> str:
    return html.escape(str(v if v is not None else ""))


def _fmt_date(epoch) -> str:
    if not epoch:
        return "--"
    try:
        return datetime.fromtimestamp(int(epoch), tz=timezone.utc).strftime("%a %d %b")
    except (ValueError, OSError, TypeError):
        return "--"


def _stat_cell(label: str, value, accent: str = BLUE) -> str:
    return (
        f'<td style="padding:14px 16px;background:#fff;border:1px solid {LINE};'
        f'border-left:3px solid {accent};border-radius:8px;" valign="top">'
        f'<div style="font-size:11px;text-transform:uppercase;letter-spacing:.6px;'
        f'color:{MUTED};font-weight:700;">{_esc(label)}</div>'
        f'<div style="font-size:24px;font-weight:700;color:{INK};margin-top:4px;">{value}</div>'
        f'</td>'
    )


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
        rows += (
            f'<tr><td style="padding:11px 14px;border-bottom:1px solid {LINE};">'
            f'<div style="font-size:13px;font-weight:600;color:{INK};">'
            f'{_esc(redact.redact(item.get("topic", "")) if redacted else item.get("topic", ""))}</div>'
            f'<div style="font-size:12.5px;color:{MUTED};margin-top:2px;">{_esc(detail)}</div>'
            f'{extra}'
            f'<div style="font-size:11px;color:{MUTED};margin-top:4px;">'
            f'{_fmt_date(item.get("created_at"))}</div>'
            f'</td></tr>'
        )

    more = ""
    if len(items) > _MAX_ITEMS_PER_SECTION:
        more = (f'<tr><td style="padding:9px 14px;font-size:12px;color:{MUTED};">'
                f'+ {len(items) - _MAX_ITEMS_PER_SECTION} more -- see the dashboard for the full list.'
                f'</td></tr>')

    return (f'<table width="100%" cellpadding="0" cellspacing="0" '
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
            f'<td style="padding:7px 12px;width:40%;">'
            f'<div style="background:{BG};border-radius:4px;height:9px;">'
            f'<div style="background:{BLUE};width:{max(pct, 2)}%;height:9px;border-radius:4px;"></div>'
            f'</div></td>'
            f'<td style="padding:7px 12px;font-size:12.5px;color:{MUTED};text-align:right;">'
            f'{count} ({pct}%)</td></tr>'
        )
    return (f'<table width="100%" cellpadding="0" cellspacing="0" '
            f'style="background:#fff;border:1px solid {LINE};border-radius:8px;">{rows}</table>')


def render_report(stats: dict, week_start: str, week_end: str, *, redacted: bool = False,
                   truncated: bool = False, model_note: str = "") -> str:
    total = stats.get("total_conversations", 0)

    warning = ""
    if truncated:
        warning = (
            f'<tr><td style="padding:14px 16px;background:#FFF6E3;border:1px solid {YELLOW};'
            f'border-radius:8px;font-size:13px;color:#B26B00;margin-bottom:12px;">'
            f'<b>Partial week.</b> Paging stopped at the configured limit, so some '
            f'conversations may be missing from these numbers.</td></tr>'
            f'<tr><td style="height:12px;"></td></tr>'
        )

    stats_table = (
        f'<table width="100%" cellpadding="0" cellspacing="6"><tr>'
        f'{_stat_cell("Conversations", total, BLUE)}'
        f'{_stat_cell("Bot could not help", len(stats.get("failures", [])), "#C62828")}'
        f'{_stat_cell("Possible leads", len(stats.get("leads", [])), YELLOW)}'
        f'{_stat_cell("Resolution rate", str(stats.get("resolution_rate", 0)) + "%", "#0E8A4A")}'
        f'</tr></table>'
    )

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
                f'<td style="padding:6px 12px;width:55%;">'
                f'<div style="background:{BG};border-radius:4px;height:9px;">'
                f'<div style="background:{YELLOW};width:{width}%;height:9px;border-radius:4px;"></div>'
                f'</div></td>'
                f'<td style="padding:6px 12px;font-size:12.5px;color:{MUTED};text-align:right;">'
                f'{count}</td></tr>'
            )
    volume_table = (f'<table width="100%" cellpadding="0" cellspacing="0" '
                     f'style="background:#fff;border:1px solid {LINE};border-radius:8px;">'
                     f'{volume_rows}</table>') if volume_rows else ""

    footer_note = (
        f'Generated automatically from Chatbase conversations. '
        f'{_esc(model_note)} '
        + ("Customer contact details have been removed from this email. Full, unredacted "
            "transcripts are in the Automation Control dashboard, under Chat Insights -- "
            "open the week's report and follow the Transcripts link."
            if redacted else "")
    )

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Chat report {_esc(week_start)}</title></head>
<body style="margin:0;padding:0;background:{BG};font-family:'Segoe UI',Arial,sans-serif;color:{INK};">
<table width="100%" cellpadding="0" cellspacing="0" style="background:{BG};padding:22px 0;">
<tr><td align="center">
<table width="680" cellpadding="0" cellspacing="0" style="max-width:680px;width:100%;">

  <tr><td style="background:{BLUE};padding:20px 24px;border-radius:10px 10px 0 0;">
    <div style="color:#fff;font-size:19px;font-weight:700;">Weekly Chat Report</div>
    <div style="color:rgba(255,255,255,.8);font-size:13px;margin-top:3px;">
      {_esc(settings.BUSINESS_NAME)} &middot; {_esc(week_start)} to {_esc(week_end)}
    </div>
  </td></tr>

  <tr><td style="background:#fff;padding:22px 24px;border:1px solid {LINE};border-top:none;
                 border-radius:0 0 10px 10px;">
    <table width="100%" cellpadding="0" cellspacing="0">
      {warning}
      <tr><td>{stats_table}</td></tr>

      {_section("Questions the bot could not answer",
                 "The highest-value list here -- each one is a content gap or a customer who left unhelped.",
                 _item_list(stats.get("failures", []), show="reason",
                             empty_text="The bot answered everything this week.", redacted=redacted))}

      {_section("What customers asked about",
                 "Conversations grouped by topic.",
                 _bar_rows(stats.get("categories", {}), total, redacted))}

      {_section("Possible sales leads",
                 "Conversations showing real buying intent -- worth a human follow-up.",
                 _item_list(stats.get("leads", []), show="lead_detail",
                             empty_text="No clear buying intent detected this week.", redacted=redacted))}

      {_section("Unhappy customers",
                 "Negative sentiment or an explicit thumbs-down.",
                 _item_list(stats.get("unhappy", []),
                             empty_text="No negative feedback this week.", redacted=redacted))}

      {_section("Volume by day",
                 f'{total} conversations, {stats.get("avg_messages", 0)} messages each on average. '
                 f'Busiest: {stats.get("busiest_day", "--")} ({stats.get("busiest_day_count", 0)}).',
                 volume_table)}

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


def render_subject(stats: dict, week_start: str, week_end: str) -> str:
    failures = len(stats.get("failures", []))
    leads = len(stats.get("leads", []))
    return (f"Weekly chat report {week_start} to {week_end} -- "
            f"{stats.get('total_conversations', 0)} chats, {failures} unanswered, {leads} leads")
