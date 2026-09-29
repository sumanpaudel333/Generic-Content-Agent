"""
What Windows Task Scheduler says about the jobs behind this dashboard.

Until now "did the job run?" could only be answered over a remote desktop with
scripts/job_status.ps1 -- which the README makes the first troubleshooting step.
Job history could show a log file's size and its last line, which says nothing
at all about a task that never started.

Read-only on purpose. This lists what Windows knows; it does not offer to run
or change anything. The dashboard runs as SYSTEM, and a web form that starts
SYSTEM processes is a much bigger thing than a status page.

`schtasks` rather than PowerShell: no interpreter to start, one process, about
two seconds for all 300 tasks on this server -- which is still far too slow to
do on every page render, hence the cache.
"""
import csv
import io
import logging
import subprocess
import threading
import time
from datetime import datetime

from config.localtime import zone_label

logger = logging.getLogger("dashboard.task_status")

CACHE_SECONDS = 60
QUERY_TIMEOUT_SECONDS = 25
NAME_MARKER = "BCSands"

# The tasks scripts/register_scheduled_tasks.ps1 creates, in the order they
# matter, with the job_logs key that holds their output.
EXPECTED = (
    ("BCSands-ContentAgent-Daily", "Writes product descriptions into the review queue", "daily_run"),
    ("BCSands-Leads", "Fetches chats and emails the lead call list", "daily_leads"),
    ("BCSands-ChatInsights-Weekly", "Analyses the week's chats and emails the report", "weekly_chat"),
    ("BCSands-SiteMonitor", "Checks the online shop every few minutes", "site_monitor"),
    ("BCSands-ReclaimImages", "Verifies published images, then frees the staged copies", "reclaim_images"),
    ("BCSands-Dashboard", "This dashboard", "dashboard"),
    ("BCSands-Ollama", "The local model server", "ollama"),
)

# From scripts/job_status.ps1 -- the codes that are not failures.
RESULTS = {
    0: ("Success", "live"),
    267009: ("Running now", "draft"),
    267011: ("Has never run", "planned"),
    267014: ("Last run was stopped", "confidence-low"),
    2147943726: ("Could not sign in -- check the logon type", "confidence-low"),
}

_lock = threading.Lock()
_cache: dict = {"at": 0.0, "value": None}


def describe_result(code) -> tuple[str, str]:
    """(what it means, badge class). Anything unknown is the job's exit code."""
    try:
        number = int(code)
    except (TypeError, ValueError):
        return (str(code or "unknown"), "planned")
    if number in RESULTS:
        return RESULTS[number]
    return (f"Failed -- exit code {number}", "confidence-low")


def _clean(value: str) -> str:
    value = (value or "").strip()
    return "" if value.upper() in ("N/A", "NONE", "DISABLED") else value


def describe_schedule(row: dict) -> str:
    """The trigger in words: "Daily at 04:00, 09:00" is more use than a cron line."""
    kind = _clean(row.get("Schedule Type", ""))
    start = _clean(row.get("Start Time", ""))
    days = _clean(row.get("Days", ""))
    every = _clean(row.get("Repeat: Every", ""))
    if not kind:
        return "At startup" if not start else start
    parts = [kind]
    if days and kind.lower().startswith("weekly"):
        parts.append(f"on {days}")
    if start:
        parts.append(f"at {start}")
    if every:
        parts.append(f"repeating every {every}")
    return " ".join(parts)


def _when(text: str) -> str:
    """Task Scheduler writes times in the server's own locale and zone, which is
    Australian Eastern here. Parsed for a readable form, shown as written when
    the locale is not one of these."""
    text = _clean(text)
    if not text:
        return ""
    for fmt in ("%d/%m/%Y %I:%M:%S %p", "%m/%d/%Y %I:%M:%S %p",
                "%d/%m/%Y %H:%M:%S", "%m/%d/%Y %H:%M:%S"):
        try:
            moment = datetime.strptime(text, fmt)
        except ValueError:
            continue
        hour = moment.hour % 12 or 12
        clock = f"{hour}:{moment.minute:02d} {'am' if moment.hour < 12 else 'pm'}"
        return f"{moment:%a} {moment.day} {moment:%b %Y}, {clock} {zone_label()}"
    return text


def _query() -> tuple[dict[str, dict], str]:
    """{task name: row} from schtasks, or ({}, why not)."""
    try:
        result = subprocess.run(["schtasks", "/query", "/fo", "CSV", "/v"],
                                capture_output=True, timeout=QUERY_TIMEOUT_SECONDS, check=False)
    except FileNotFoundError:
        return {}, "schtasks is not on this server, so task status cannot be read."
    except subprocess.TimeoutExpired:
        return {}, "Windows took too long to list the scheduled tasks."
    except OSError as e:
        return {}, f"The scheduled tasks could not be read: {e}"

    raw = result.stdout or b""
    for encoding in ("utf-8", "cp1252"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = raw.decode("utf-8", "replace")
    if not text.strip():
        detail = (result.stderr or b"").decode("utf-8", "replace").strip().splitlines()
        return {}, (detail[-1][:200] if detail else
                    "Windows returned nothing for the scheduled tasks.")

    found: dict[str, dict] = {}
    for row in csv.DictReader(io.StringIO(text)):
        name = (row.get("TaskName") or "").strip()
        # /v repeats the header once per folder; those come back as data rows.
        if not name or name == "TaskName" or NAME_MARKER not in name:
            continue
        found[name.lstrip("\\")] = row
    return found, ""


def tasks(force: bool = False) -> dict:
    """Every expected task with what Windows says about it.

    A task missing from the listing is reported as not visible rather than as
    missing: task objects carry their own permissions, and one registered by
    another account can be invisible to this one while running perfectly well.
    """
    with _lock:
        fresh = _cache["value"] is not None and time.time() - _cache["at"] < CACHE_SECONDS
        if fresh and not force:
            return _cache["value"]

    found, error = _query()
    rows = []
    for name, purpose, log_key in EXPECTED:
        row = found.get(name)
        if not row:
            rows.append({"name": name, "purpose": purpose, "log_key": log_key, "known": False,
                         "state": "", "schedule": "", "last_run": "", "next_run": "",
                         "result": "Not visible to this dashboard", "badge": "planned",
                         "run_as": ""})
            continue
        result_text, badge = describe_result(row.get("Last Result"))
        rows.append({
            "name": name, "purpose": purpose, "log_key": log_key, "known": True,
            "state": _clean(row.get("Status", "")) or _clean(row.get("Scheduled Task State", "")),
            "schedule": describe_schedule(row),
            "last_run": _when(row.get("Last Run Time", "")),
            "next_run": _when(row.get("Next Run Time", "")),
            "result": result_text, "badge": badge,
            "run_as": _clean(row.get("Run As User", "")),
        })
    extra = sorted(name for name in found if name not in {n for n, _p, _k in EXPECTED})
    value = {"rows": rows, "error": error, "extra": extra,
             "checked_at": datetime.now().astimezone().isoformat(timespec="seconds"),
             "missing": [r["name"] for r in rows if not r["known"]]}
    with _lock:
        _cache.update(at=time.time(), value=value)
    return value
