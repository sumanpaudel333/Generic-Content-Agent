"""
Turns check results into incidents and notifications.

Kept apart from the checking and the sending: every decision about whether
somebody gets an email is made here, from what is stored, so it can be tested
with made-up outcomes rather than a flaky website.

The rules, in one place:

  * A problem has to repeat before anyone is told. One failed check is a blip.
    Down needs `down_after` in a row, a wrong page or server error
    `issue_after`, slow `slow_after`, a Cloudflare block `blocked_after`.
  * One email when a problem starts and one when it ends. Checking every five
    minutes never repeats an alert. Only "down" sends a reminder, every
    `reminder_minutes`, because a site that stays down is worth being told
    about more than once.
  * A recovery email only follows a problem somebody was told about, and only
    when the page is properly back. Going from slow to down closes the slow
    incident quietly and opens the down one.
  * Checks this server could not make, because it could not reach the
    internet at all, are recorded but never count toward anything.
"""
import json
from datetime import datetime, timedelta
from urllib.parse import urlparse

from site_monitor import store

FAMILY = {"down": "down", "server_error": "error", "page_error": "error",
          "wrong_content": "error", "cert_invalid": "error",
          "slow": "slow", "blocked": "blocked"}


def threshold(family: str, settings: dict) -> int:
    return {"down": settings["down_after"], "error": settings["issue_after"],
            "slow": settings["slow_after"], "blocked": settings["blocked_after"]}[family]


def in_quiet_hours(local_now: datetime, start: str, end: str) -> bool:
    """Whether local_now falls in [start, end), which may run past midnight."""
    now = local_now.strftime("%H:%M")
    if start == end:
        return False
    if start < end:
        return start <= now < end
    return now >= start or now < end


def _event(kind: str, target: dict, incident: dict | None, now: datetime,
           latest: dict | None, settings: dict, **extra) -> dict:
    latest = latest or {}
    incident = incident or {}
    event = {"type": kind, "target_key": target["key"], "target_label": target["label"],
             "target_url": target["url"], "family": incident.get("family", ""),
             "incident_id": incident.get("id"), "opened_at": incident.get("opened_at", ""),
             "closed_at": store.iso(now) if kind == "recovered" else "",
             "detail": latest.get("detail") or incident.get("detail") or "",
             "status": latest.get("status"), "ms": latest.get("ms"),
             "checked_at": latest.get("checked_at") or store.iso(now),
             "slow_ms": settings["slow_ms"]}
    event.update(extra)
    return event


def evaluate_target(target: dict, now: datetime, settings: dict) -> list[dict]:
    """Opens and closes this page's incidents from its latest checks, and
    returns the notifications that are now due."""
    key = target["key"]
    recent = store.recent_checks(key, 60)
    if not recent or recent[0]["outcome"] == "monitor_offline":
        return []
    real = [c for c in recent if c["outcome"] != "monitor_offline"]
    latest = real[0]
    current = latest["outcome"]
    family = FAMILY.get(current)
    events = []

    for incident in store.open_incidents(key):
        if incident["family"] == family:
            continue
        store.close_incident(incident["id"], now)
        if current == "ok" and incident["notified_at"]:
            events.append(_event("recovered", target, incident, now, latest, settings,
                                 detail=incident.get("detail") or ""))

    if not family:
        return events

    incident = store.open_incident(key, family)
    if incident is None:
        streak = []
        for check in real:
            if FAMILY.get(check["outcome"]) != family:
                break
            streak.append(check)
        if len(streak) >= threshold(family, settings):
            incident = store.create_incident(key, family, streak[-1]["checked_at"],
                                             streak[-1]["outcome"], latest["detail"] or "")
            events.append(_event("opened", target, incident, now, latest, settings,
                                 streak=len(streak)))
        return events

    store.update_incident_detail(incident["id"], latest["detail"] or incident["detail"] or "")
    if not incident["notified_at"]:
        # Opened on an earlier run but the email never went -- the mail server
        # was down, say. Try again rather than let it go unannounced.
        events.append(_event("opened", target, incident, now, latest, settings))
    elif family == "down":
        last = store.parse(incident["last_reminder_at"] or incident["notified_at"])
        if now - last >= timedelta(minutes=settings["reminder_minutes"]):
            events.append(_event("reminder", target, incident, now, latest, settings))
    return events


def cert_events(target: dict, check: dict, now: datetime, settings: dict) -> list[dict]:
    """A warning when a certificate crosses one of the warning thresholds.

    Tracked per hostname and per expiry date, so two pages on the same site
    warn once between them, and a renewed certificate starts afresh.
    """
    days, expires = check.get("cert_days"), check.get("cert_expires")
    if days is None or not expires:
        return []
    host = urlparse(target["url"]).hostname or ""
    state_key = f"cert_warned:{host}:{expires}"
    try:
        warned = set(json.loads(store.get_state(state_key, "[]")))
    except ValueError:
        warned = set()
    due = [d for d in settings["cert_warn_days"] if days <= d and d not in warned]
    if not due:
        return []
    store.set_state(state_key, json.dumps(sorted(warned | set(due))))
    return [{"type": "cert_expiring", "target_key": target["key"],
             "target_label": target["label"], "target_url": target["url"], "host": host,
             "cert_days": days, "cert_expires": expires, "checked_at": store.iso(now),
             "family": "cert", "detail": "", "incident_id": None}]
