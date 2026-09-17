"""
Times as the people here read them: Australian Eastern time (Sydney), whatever
the server's own clock happens to be set to.

Everything is STORED in UTC -- ISO strings with an offset, or epoch seconds for
chats -- and converted only for display, here, in one place. Before this each
page formatted its own: some showed UTC with a "UTC" label, some sliced the raw
ISO string, some used the server's zone. The server is in Sydney today, but a
page should not change what it tells people because a machine was moved.

"Australia/Sydney" rather than a fixed +10:00: it follows daylight saving, so
clocks read what the office clock reads -- AEST in winter, AEDT in summer --
and the zone name is shown with each time so nobody has to guess which.
"""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

ZONE_NAME = "Australia/Sydney"
ZONE = ZoneInfo(ZONE_NAME)


def now() -> datetime:
    return datetime.now(ZONE)


def to_au(value) -> datetime | None:
    """A datetime in Sydney time from whatever was stored, or None.

    Accepts an aware or naive datetime, an ISO string, or epoch seconds (int,
    float or digits). Naive values are taken as UTC, which is how everything
    here is written. 0 and blanks are "no time" rather than 1 January 1970.
    """
    if value is None or value == "" or value == 0:
        return None
    try:
        if isinstance(value, datetime):
            moment = value
        elif isinstance(value, (int, float)):
            moment = datetime.fromtimestamp(float(value), timezone.utc)
        else:
            text = str(value).strip()
            if not text:
                return None
            if text.replace(".", "", 1).isdigit():
                if float(text) == 0:
                    return None
                moment = datetime.fromtimestamp(float(text), timezone.utc)
            else:
                moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (ValueError, OSError, OverflowError):
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(ZONE)


def _clock(moment: datetime) -> str:
    # %-I is not portable to Windows, so the 12-hour clock is built by hand.
    hour = moment.hour % 12 or 12
    return f"{hour}:{moment.minute:02d} {'am' if moment.hour < 12 else 'pm'}"


def au_time(value, fallback: str = "") -> str:
    """"9:05 am" """
    moment = to_au(value)
    return _clock(moment) if moment else fallback


def au_date(value, fallback: str = "") -> str:
    """"17 Sep 2026" """
    moment = to_au(value)
    return f"{moment.day} {moment:%b %Y}" if moment else fallback


def au_datetime(value, fallback: str = "--", *, zone: bool = True, year: bool = True) -> str:
    """"Thu 17 Sep 2026, 9:05 am AEST" -- the form every page uses."""
    moment = to_au(value)
    if not moment:
        return fallback
    date = f"{moment:%a} {moment.day} {moment:%b}" + (f" {moment:%Y}" if year else "")
    return f"{date}, {_clock(moment)}" + (f" {moment.tzname()}" if zone else "")


def au_day(value, fallback: str = "") -> str:
    """"7 Sep 2026" from a calendar date like "2026-09-07". A date has no time
    zone, so it is formatted as it is rather than converted."""
    try:
        day = datetime.strptime(str(value).strip()[:10], "%Y-%m-%d")
    except (TypeError, ValueError):
        return fallback or str(value or "")
    return f"{day.day} {day:%b %Y}"


def zone_label(value=None) -> str:
    """"AEST" or "AEDT", for the given moment (default: now)."""
    moment = to_au(value) if value is not None else now()
    return moment.tzname() if moment else ""
