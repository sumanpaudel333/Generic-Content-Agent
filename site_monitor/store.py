"""
SQLite storage for the site monitor, in logs/site_monitor.db.

Its own file, like every other store here, so rebuilding one can never take
another with it.

Settings are stored here as well as in config.yaml, because admins change them
from the Site health page. config.yaml supplies the starting values; anything
saved from the page wins from then on, one setting at a time.
"""
import copy
import ipaddress
import json
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from site_monitor.probe import SESSION_COOKIE_ONLY, SESSION_IN_QUERY, SESSION_MODES

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "logs", "site_monitor.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS checks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    target_key TEXT NOT NULL,
    checked_at TEXT NOT NULL,          -- UTC, ISO 8601
    outcome TEXT NOT NULL,             -- see OUTCOMES
    status INTEGER,
    ms INTEGER,
    detail TEXT,
    cert_days INTEGER,
    cert_expires TEXT,
    final_url TEXT,
    ip TEXT,
    requests INTEGER DEFAULT 1         -- what this check cost the site
);
CREATE INDEX IF NOT EXISTS idx_checks_target_time ON checks(target_key, checked_at);
CREATE TABLE IF NOT EXISTS incidents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    target_key TEXT NOT NULL,
    family TEXT NOT NULL,              -- down | error | slow | blocked
    opened_at TEXT NOT NULL,           -- the first failing check, not when it was noticed
    closed_at TEXT,
    first_outcome TEXT,
    detail TEXT,
    notified_at TEXT,
    last_reminder_at TEXT,
    recovery_notified_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_incidents_open ON incidents(target_key, closed_at);
CREATE TABLE IF NOT EXISTS held (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT,
    event_json TEXT
);
CREATE TABLE IF NOT EXISTS state (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value_json TEXT,
    updated_at TEXT,
    updated_by TEXT
);
"""

ROUTE_DIRECT = "direct"    # this server's own DNS -- straight to the shop server
ROUTE_PUBLIC = "public"    # public DNS -- through Cloudflare, as customers reach it
ROUTES = (ROUTE_DIRECT, ROUTE_PUBLIC)

OUTCOMES = ("ok", "slow", "down", "server_error", "page_error", "wrong_content",
            "blocked", "cert_invalid", "monitor_offline")
UP_OUTCOMES = ("ok", "slow")

# Chosen by testing what each route actually reaches.
#
# Direct, on the office network, the shop answers anything: those checks say
# the shop server itself works.
#
# Through Cloudflare, the WAF rules challenge any address holding a Zen Cart
# session ("zenid=") or a category path, which is most of the shop. They do
# not look at cookies, and Zen Cart takes a session from the cookie alone --
# so the customer-route check asks for a page the rules do not match and sends
# its session as a cookie. That reaches a real shop page the way a customer
# does, without going near Cloudflare's bot protection. If those rules change
# and the page is challenged, it reports "Blocked by Cloudflare", which is not
# an outage -- the alert says so.
BUILTIN_DEFAULTS = {
    "recipients": ["spaudel@bcsands.com.au"],
    "office_ip": "220.233.203.122",
    "quiet_start": "21:00",
    "quiet_end": "06:00",
    "morning_summary_at": "06:30",
    "slow_ms": 3000,
    "slow_after": 3,
    "down_after": 2,
    "issue_after": 2,
    "blocked_after": 3,
    "reminder_minutes": 60,
    "cert_warn_days": [21, 7],
    "timeout_seconds": 20,
    "retention_days": 90,
    "targets": [
        {"key": "shop_home", "label": "Online shop home page",
         "url": "https://www.bcsands.com.au/", "route": ROUTE_DIRECT,
         "session_param": "zenid", "must_contain": ["BCSands Online Shop"], "enabled": True},
        {"key": "shop_customer_route", "label": "Online shop through Cloudflare",
         "url": "https://www.bcsands.com.au/contact_us", "route": ROUTE_PUBLIC,
         "session_param": "zenid", "session_mode": SESSION_COOKIE_ONLY,
         "must_contain": ["BCSands Online Shop"], "enabled": True},
        {"key": "shop_category", "label": "Online shop category page",
         "url": "https://www.bcsands.com.au/firewood", "route": ROUTE_DIRECT,
         "session_param": "zenid", "must_contain": ["firewood"], "enabled": True},
        {"key": "landing", "label": "Landing page",
         "url": "https://landing.bcsands.com.au/", "route": ROUTE_DIRECT,
         "session_param": "", "must_contain": ["BC Sands"], "enabled": True},
    ],
}

MAX_TARGETS = 12


@contextmanager
def _connect():
    os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with _connect() as conn:
        conn.executescript(SCHEMA)


def iso(dt: datetime) -> str:
    """UTC ISO string. Everything is stored in UTC and shown in local time."""
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse(value: str) -> datetime:
    return datetime.fromisoformat(value)


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

def defaults() -> dict:
    """Built-in values, overlaid with config.yaml's site_monitor block."""
    merged = copy.deepcopy(BUILTIN_DEFAULTS)
    try:
        from config import settings as app_settings
        cfg = dict(getattr(app_settings, "SITE_MONITOR_CONFIG", {}) or {})
    except Exception:
        cfg = {}
    quiet = cfg.pop("quiet_hours", None) or {}
    if isinstance(quiet, dict):
        if quiet.get("start"):
            merged["quiet_start"] = str(quiet["start"])
        if quiet.get("end"):
            merged["quiet_end"] = str(quiet["end"])
    for key, value in cfg.items():
        if key in merged and value is not None:
            merged[key] = copy.deepcopy(value)
    try:
        return validate_settings(merged)
    except ValueError:
        # A bad config.yaml must not stop the monitor. Fall back to what ships.
        return copy.deepcopy(BUILTIN_DEFAULTS)


def get_settings() -> dict:
    """The settings in force: config defaults, then anything saved on the page."""
    merged = defaults()
    init_db()
    with _connect() as conn:
        rows = conn.execute("SELECT key, value_json FROM settings").fetchall()
    for row in rows:
        if row["key"] not in merged:
            continue
        try:
            merged[row["key"]] = json.loads(row["value_json"])
        except ValueError:
            continue
    return merged


def save_settings(values: dict, updated_by: str = "") -> dict:
    """Validates everything first, then saves all of it. Raises ValueError."""
    clean = validate_settings(values)
    now = iso(datetime.now(timezone.utc))
    init_db()
    with _connect() as conn:
        for key, value in clean.items():
            conn.execute(
                "INSERT INTO settings (key, value_json, updated_at, updated_by) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json, "
                "updated_at = excluded.updated_at, updated_by = excluded.updated_by",
                (key, json.dumps(value), now, updated_by))
    return clean


_TIME = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_KEY = re.compile(r"^[a-z0-9_]{1,40}$")
_PARAM = re.compile(r"^[A-Za-z0-9_]{1,32}$")


def _slug(label: str) -> str:
    return (re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_") or "page")[:40]


def validate_settings(values: dict) -> dict:
    """Checks and normalises a full settings dict. Raises ValueError with a
    sentence an admin can act on."""
    clean: dict = {}

    recipients = values.get("recipients", [])
    if isinstance(recipients, str):
        recipients = re.split(r"[,;\s]+", recipients)
    recipients = [str(r).strip().lower() for r in recipients if str(r).strip()]
    bad = [r for r in recipients if not _EMAIL.match(r)]
    if bad:
        raise ValueError(f"Not an email address: {', '.join(bad)}")
    if not recipients:
        raise ValueError("Give at least one email address to send alerts to.")
    clean["recipients"] = list(dict.fromkeys(recipients))

    office_ip = str(values.get("office_ip", "") or "").strip()
    if office_ip:
        try:
            ipaddress.ip_address(office_ip)
        except ValueError:
            raise ValueError(f"'{office_ip}' is not an internet address like 220.233.203.122.")
    clean["office_ip"] = office_ip

    for key, label in (("quiet_start", "Quiet hours start"), ("quiet_end", "Quiet hours end"),
                       ("morning_summary_at", "Morning summary time")):
        value = str(values.get(key, "")).strip()
        if not _TIME.match(value):
            raise ValueError(f"{label} must be a time like 06:30.")
        clean[key] = value

    # A summary time inside quiet hours would never send: the job waits for
    # quiet hours to end before sending anything.
    start, end, summary_at = clean["quiet_start"], clean["quiet_end"], clean["morning_summary_at"]
    if start != end:
        inside = (start <= summary_at < end) if start < end else (summary_at >= start or summary_at < end)
        if inside:
            raise ValueError("The morning summary time must be outside quiet hours, or it would "
                             "never be sent.")

    for key, label, low, high in (("slow_ms", "Slow threshold", 500, 60000),
                                  ("slow_after", "Slow checks in a row", 1, 20),
                                  ("down_after", "Failed checks in a row", 1, 20),
                                  ("issue_after", "Problem checks in a row", 1, 20),
                                  ("blocked_after", "Blocked checks in a row", 1, 20),
                                  ("reminder_minutes", "Reminder interval", 10, 1440),
                                  ("timeout_seconds", "Timeout", 5, 60),
                                  ("retention_days", "History kept for", 7, 365)):
        try:
            number = int(str(values.get(key, "")).strip())
        except ValueError:
            raise ValueError(f"{label} must be a whole number.")
        if not low <= number <= high:
            raise ValueError(f"{label} must be between {low} and {high}.")
        clean[key] = number

    days = values.get("cert_warn_days", [])
    if isinstance(days, str):
        days = [d for d in re.split(r"[,;\s]+", days) if d]
    try:
        days = sorted({int(d) for d in days}, reverse=True)
    except (TypeError, ValueError):
        raise ValueError("Certificate warning days must be whole numbers, like 21, 7.")
    if not days or any(not 1 <= d <= 120 for d in days):
        raise ValueError("Certificate warning days must be between 1 and 120.")
    clean["cert_warn_days"] = days

    targets, seen = [], set()
    for raw in values.get("targets", []) or []:
        label = str(raw.get("label", "")).strip()
        url = str(raw.get("url", "")).strip()
        if not label and not url:
            continue                         # the blank "add a page" row
        if not label:
            raise ValueError(f"Give the page at {url} a name.")
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError(f"{label}: the address must start with https://")
        route = raw.get("route") or ROUTE_DIRECT
        if route not in ROUTES:
            raise ValueError(f"{label}: choose how the page is reached.")
        session = str(raw.get("session_param", "") or "").strip()
        if session and not _PARAM.match(session):
            raise ValueError(f"{label}: the session setting may only contain letters, "
                             "numbers and underscores.")
        session_mode = str(raw.get("session_mode", "") or SESSION_IN_QUERY).strip()
        if session_mode not in SESSION_MODES:
            raise ValueError(f"{label}: choose how the shop session is sent.")
        contains = raw.get("must_contain", [])
        if isinstance(contains, str):
            contains = contains.splitlines()
        contains = [str(c).strip() for c in contains if str(c).strip()][:10]
        key = str(raw.get("key", "") or "").strip()
        if not _KEY.match(key):
            key = _slug(label)
        base, n = key, 2
        while key in seen:
            key = f"{base}_{n}"[:40]
            n += 1
        seen.add(key)
        targets.append({"key": key, "label": label[:80], "url": url, "route": route,
                        "session_param": session, "session_mode": session_mode,
                        "must_contain": contains, "enabled": bool(raw.get("enabled"))})
    if len(targets) > MAX_TARGETS:
        raise ValueError(f"Monitor at most {MAX_TARGETS} pages. Every page adds load to the site.")
    if not any(t["enabled"] for t in targets):
        raise ValueError("Leave at least one page switched on, or there is nothing to monitor.")
    clean["targets"] = targets
    return clean


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------

def record_check(target_key: str, at: datetime, outcome: str, *, status=None, ms=None,
                 detail: str = "", cert_days=None, cert_expires: str = "",
                 final_url: str = "", ip: str = "", requests: int = 1) -> int:
    init_db()
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO checks (target_key, checked_at, outcome, status, ms, detail, cert_days, "
            "cert_expires, final_url, ip, requests) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (target_key, iso(at), outcome, status, None if ms is None else int(ms), detail,
             cert_days, cert_expires, final_url, ip, requests))
        return cur.lastrowid


def recent_checks(target_key: str, limit: int = 60) -> list[dict]:
    """Newest first."""
    init_db()
    with _connect() as conn:
        rows = conn.execute("SELECT * FROM checks WHERE target_key = ? ORDER BY id DESC LIMIT ?",
                            (target_key, limit)).fetchall()
    return [dict(r) for r in rows]


def latest_check(target_key: str) -> dict | None:
    rows = recent_checks(target_key, 1)
    return rows[0] if rows else None


def checks_since(target_key: str, since: datetime) -> list[dict]:
    """Oldest first, for charts."""
    init_db()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT checked_at, outcome, ms, status FROM checks "
            "WHERE target_key = ? AND checked_at >= ? ORDER BY id",
            (target_key, iso(since))).fetchall()
    return [dict(r) for r in rows]


def uptime(target_key: str, since: datetime) -> float | None:
    """Percentage of checks that found the page up. Checks this server could
    not make are left out rather than counted as the site being down."""
    init_db()
    with _connect() as conn:
        row = conn.execute(
            "SELECT SUM(CASE WHEN outcome IN ('ok', 'slow') THEN 1 ELSE 0 END) AS up, "
            "SUM(CASE WHEN outcome != 'monitor_offline' THEN 1 ELSE 0 END) AS counted "
            "FROM checks WHERE target_key = ? AND checked_at >= ?",
            (target_key, iso(since))).fetchone()
    if not row or not row["counted"]:
        return None
    return 100.0 * (row["up"] or 0) / row["counted"]


# ---------------------------------------------------------------------------
# Incidents
# ---------------------------------------------------------------------------

def create_incident(target_key: str, family: str, opened_at: str, first_outcome: str,
                    detail: str) -> dict:
    init_db()
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO incidents (target_key, family, opened_at, first_outcome, detail) "
            "VALUES (?, ?, ?, ?, ?)", (target_key, family, opened_at, first_outcome, detail))
        row = conn.execute("SELECT * FROM incidents WHERE id = ?", (cur.lastrowid,)).fetchone()
    return dict(row)


def open_incident(target_key: str, family: str) -> dict | None:
    init_db()
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM incidents WHERE target_key = ? AND family = ? AND closed_at IS NULL "
            "ORDER BY id DESC LIMIT 1", (target_key, family)).fetchone()
    return dict(row) if row else None


def open_incidents(target_key: str | None = None) -> list[dict]:
    init_db()
    with _connect() as conn:
        if target_key is None:
            rows = conn.execute("SELECT * FROM incidents WHERE closed_at IS NULL ORDER BY id").fetchall()
        else:
            rows = conn.execute("SELECT * FROM incidents WHERE target_key = ? AND closed_at IS NULL "
                                "ORDER BY id", (target_key,)).fetchall()
    return [dict(r) for r in rows]


def close_incident(incident_id: int, at: datetime) -> None:
    init_db()
    with _connect() as conn:
        conn.execute("UPDATE incidents SET closed_at = ? WHERE id = ?", (iso(at), incident_id))


_INCIDENT_MARKS = {"notified_at", "last_reminder_at", "recovery_notified_at"}


def mark_incident(incident_id: int, field: str, at: datetime) -> None:
    if field not in _INCIDENT_MARKS:
        raise ValueError(field)
    init_db()
    with _connect() as conn:
        conn.execute(f"UPDATE incidents SET {field} = ? WHERE id = ?", (iso(at), incident_id))


def update_incident_detail(incident_id: int, detail: str) -> None:
    init_db()
    with _connect() as conn:
        conn.execute("UPDATE incidents SET detail = ? WHERE id = ?", (detail, incident_id))


def recent_incidents(limit: int = 20) -> list[dict]:
    init_db()
    with _connect() as conn:
        rows = conn.execute("SELECT * FROM incidents ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Notifications held during quiet hours
# ---------------------------------------------------------------------------

def hold(event: dict, at: datetime) -> None:
    init_db()
    with _connect() as conn:
        conn.execute("INSERT INTO held (created_at, event_json) VALUES (?, ?)",
                     (iso(at), json.dumps(event)))


def count_held() -> int:
    init_db()
    with _connect() as conn:
        return conn.execute("SELECT COUNT(*) AS n FROM held").fetchone()["n"]


def take_held() -> list[dict]:
    """Every held notification, oldest first, removed as it is read."""
    init_db()
    with _connect() as conn:
        rows = conn.execute("SELECT id, event_json FROM held ORDER BY id").fetchall()
        conn.execute("DELETE FROM held")
    events = []
    for row in rows:
        try:
            events.append(json.loads(row["event_json"]))
        except ValueError:
            continue
    return events


# ---------------------------------------------------------------------------
# Small persistent values: the shop sessions, the last summary date, ...
# ---------------------------------------------------------------------------

def get_state(key: str, default: str = "") -> str:
    init_db()
    with _connect() as conn:
        row = conn.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_state(key: str, value: str) -> None:
    init_db()
    with _connect() as conn:
        conn.execute("INSERT INTO state (key, value) VALUES (?, ?) "
                     "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))


def purge(retention_days: int, now: datetime) -> None:
    cutoff = iso(now - timedelta(days=retention_days))
    init_db()
    with _connect() as conn:
        conn.execute("DELETE FROM checks WHERE checked_at < ?", (cutoff,))
        conn.execute("DELETE FROM incidents WHERE closed_at IS NOT NULL AND closed_at < ?", (cutoff,))
