"""
The lead email job: fetch the latest chats, find the leads, email the call list.

Runs at the times in chat_insights.leads_schedule -- by default 04:00, 09:00,
11:00, 13:00 and 16:00 every day, Australian Eastern time -- as the scheduled
task BCSands-Leads. Each run covers the chats since the previous scheduled time:
the 09:00 run reports chats that started between 04:00 and 09:00, and the 04:00
run the evening and night since 16:00 the day before, so after-hours enquiries
are in the inbox before anyone starts work.

One email per run, carrying every lead in that window, in the same call-list
template as before (with the link to each transcript). Also carried: any lead
from the day before the window that never made it into an email -- a chat that
started just before a run and only got its phone number after it, or a run
whose email failed. See db.leads_to_email. No leads, no email.

Why a run takes seconds rather than the half hour the morning job used to.
Fetching the last day or two from Chatbase is a couple of API calls, and finding
leads is text matching over a few dozen chats. The model is the slow part, and
it now only reads a chat the rules have already found a phone number or email
in, once per lead -- see leads.sync. It used to read every stored chat on every
run. Its time in any one run is capped (lead_model_budget_seconds); leads past
the cap are emailed with the rules' details and tidied next run.

A lock stops two runs overlapping -- the scheduled job, the dashboard's "Check
for leads" and "Send lead email now" all take it. A run that finds it held is
recorded as skipped, and its leads go out with the next one.
"""
import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, time as dtime, timedelta, timezone

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(REPO_ROOT, ".env"))

from chat_insights import alerts, chatbase_client, db, leads, lead_report, mailer  # noqa: E402
from config import settings  # noqa: E402
from config.localtime import ZONE, au_time, to_au  # noqa: E402

logger = logging.getLogger("chat_insights.daily_leads")

LOCK_PATH = os.path.join(REPO_ROOT, "logs", "leads_job.lock")
LOCK_STALE_SECONDS = 30 * 60
# Task Scheduler can start a moment early; a run within this of a slot belongs
# to that slot rather than the one before.
SLOT_EARLY_TOLERANCE = timedelta(minutes=10)
# A slot with no run this long after its time is reported as missed.
MISSED_AFTER = timedelta(minutes=30)
# How far back an un-emailed lead is still carried into the next email.
CARRY_OVER = timedelta(hours=24)
# Chatbase is asked for this much before the window: a chat is timestamped when
# it STARTED, and one still going at the last run has grown since.
FETCH_BACK = timedelta(days=1)


# ---------------------------------------------------------------------------
# Who started it
# ---------------------------------------------------------------------------

def _account() -> str:
    """The OS account this process is running as, as far as it can tell."""
    try:
        return os.environ.get("USERNAME") or os.environ.get("USER") or ""
    except Exception:
        return ""


def default_trigger() -> str:
    """Whether this looks like the scheduled job or a person at a terminal.

    The scheduled task runs as SYSTEM, which Windows reports as the machine
    account (BCSANDSAS08$) or plain SYSTEM. Anything else is somebody signed in
    and running it themselves, and recording that as "scheduled" would hide the
    very gap the history is meant to show.
    """
    account = _account().upper().rstrip("$")
    machine = (os.environ.get("COMPUTERNAME") or "").upper()
    if not account or account == "SYSTEM" or (machine and account == machine):
        return db.TRIGGER_SCHEDULED
    return db.TRIGGER_COMMAND_LINE


# ---------------------------------------------------------------------------
# The schedule and the windows it makes
# ---------------------------------------------------------------------------

def schedule() -> list[dtime]:
    return [dtime(int(text[:2]), int(text[3:5])) for text in settings.CHAT_LEADS_SCHEDULE]


def _slots_on(day) -> list[datetime]:
    return [datetime.combine(day, slot, tzinfo=ZONE) for slot in schedule()]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def slot_at_or_before(moment: datetime) -> datetime:
    """The latest scheduled time at or before `moment`, in Sydney time."""
    local = to_au(moment)
    for back in range(3):
        earlier = [s for s in _slots_on((local - timedelta(days=back)).date()) if s <= local]
        if earlier:
            return max(earlier)
    raise RuntimeError("chat_insights.leads_schedule has no times in it")


def slot_before(slot: datetime) -> datetime:
    return slot_at_or_before(slot - timedelta(seconds=1))


def scheduled_window(now: datetime | None = None) -> tuple[datetime, datetime]:
    """(start, end) for a scheduled run: from the previous slot to this one.

    A run the scheduler started late -- the server was down at 09:00 and it ran
    at 10:20 -- still reports 04:00 to 09:00. Chats after 09:00 wait for 11:00.
    """
    end = slot_at_or_before(to_au(now or _utc_now()) + SLOT_EARLY_TOLERANCE)
    return slot_before(end), end


def catch_up_window(now: datetime | None = None) -> tuple[datetime, datetime]:
    """(start, end) for a run a person started: since the last scheduled time,
    up to now. Anything earlier that was never emailed is carried in anyway."""
    local = to_au(now or _utc_now())
    return slot_at_or_before(local), local


def window_for_slot(slot_text: str, day_offset: int = 0,
                    now: datetime | None = None) -> tuple[datetime, datetime]:
    """The window a given slot covers: "09:00" today (0) or yesterday (-1)."""
    local = to_au(now or _utc_now())
    hour, minute = int(slot_text[:2]), int(slot_text[3:5])
    end = datetime.combine((local + timedelta(days=day_offset)).date(),
                           dtime(hour, minute), tzinfo=ZONE)
    return slot_before(end), end


def window_label(start: datetime, end: datetime) -> tuple[str, str]:
    """("9:00 am to 11:00 am", "Thursday 17 September 2026, 9:00 am to 11:00 am AEST")."""
    s, e = to_au(start), to_au(end)
    short = f"{au_time(s)} to {au_time(e)}"
    if s.date() == e.date():
        full = f"{e:%A} {e.day} {e:%B %Y}, {au_time(s)} to {au_time(e)} {e.tzname()}"
    else:
        full = (f"{s:%a} {s.day} {s:%b} {au_time(s)} to "
                f"{e:%a} {e.day} {e:%b %Y} {au_time(e)} {e.tzname()}")
    return short, full


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# The lock
# ---------------------------------------------------------------------------

def _acquire_lock() -> bool:
    os.makedirs(os.path.dirname(LOCK_PATH), exist_ok=True)
    for _ in range(2):
        try:
            fd = os.open(LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w") as handle:
                handle.write(f"{os.getpid()} {time.time():.0f}")
            return True
        except FileExistsError:
            try:
                age = time.time() - os.path.getmtime(LOCK_PATH)
            except OSError:
                continue
            if age < LOCK_STALE_SECONDS:
                return False
            # Left by a run that died; old enough not to be a live one.
            try:
                os.remove(LOCK_PATH)
            except OSError:
                return False
    return False


def _release_lock() -> None:
    try:
        os.remove(LOCK_PATH)
    except OSError:
        pass


def lock_busy() -> bool:
    try:
        return time.time() - os.path.getmtime(LOCK_PATH) < LOCK_STALE_SECONDS
    except OSError:
        return False


# ---------------------------------------------------------------------------
# One run
# ---------------------------------------------------------------------------

def run_leads(*, send_email: bool = True, fetch: bool = True, trigger: str = "",
              triggered_by: str = "", now: datetime | None = None,
              window: tuple[datetime, datetime] | None = None, slot: str = "",
              test: bool = False, test_recipients: list[str] | None = None,
              record: bool = True, use_lock: bool = True, fetcher=None, sender=None) -> dict:
    """Fetch, find the leads, email the call list for one window. Never raises
    for a Chatbase or email problem: the leads are stored and visible in the
    dashboard either way, and the error is recorded on the run.

    `test` sends only to `test_recipients` (or nobody -- a preview), marks no
    lead as emailed and is recorded as a test, so it changes nothing the real
    runs rely on. `fetcher` and `sender` exist so tests never reach Chatbase or
    a mail server.
    """
    trigger = trigger or default_trigger()
    triggered_by = triggered_by or (_account() if trigger == db.TRIGGER_COMMAND_LINE else "")
    now_utc = (now or _utc_now()).astimezone(timezone.utc)
    if window is None:
        if trigger == db.TRIGGER_SCHEDULED:
            start, end = scheduled_window(now_utc)
            slot = end.strftime("%H:%M")
        else:
            start, end = catch_up_window(now_utc)
    else:
        start, end = window
    start, end = to_au(start), to_au(end)
    short_label, full_label = window_label(start, end)

    result = {
        "slot": slot, "window_start_at": _iso(start), "window_end_at": _iso(end),
        "window_start_date": start.strftime("%Y-%m-%d"), "window_end_date": end.strftime("%Y-%m-%d"),
        "day_label": short_label, "day_date": full_label,
        "fetched": 0, "scanned": 0, "leads": {}, "reported": 0, "waiting": 0,
        "refined": 0, "deferred": 0, "carried": 0, "lead_ids": [],
        "email_status": db.EMAIL_NOT_REQUESTED, "email_detail": "", "error": "",
        "subject": "", "recipients": [], "trigger": trigger, "triggered_by": triggered_by,
        "test": bool(test), "skipped": False, "timings": {}, "duration_ms": 0,
        "preview_html": "",
    }

    if use_lock and not _acquire_lock():
        result["skipped"] = True
        result["error"] = ("Another lead run was still going, so this one did not start. "
                           "Its leads go out with the next run.")
        logger.warning(result["error"])
        if record:
            _record(result)
        return result

    started = time.perf_counter()
    timings = result["timings"]
    try:
        if fetch:
            t0 = time.perf_counter()
            try:
                fetched = (fetcher or chatbase_client.fetch_conversations)(
                    start.astimezone(timezone.utc) - FETCH_BACK, now_utc)
                for conv in fetched["conversations"]:
                    db.save_conversation(None, conv)
                result["fetched"] = len(fetched["conversations"])
            except Exception as e:
                # The email still goes out from what is already stored: those
                # leads have not stopped needing a call because Chatbase is down.
                result["error"] = f"Chatbase fetch failed: {e}"
                logger.exception("Chatbase fetch failed; continuing with stored conversations")
            timings["fetch"] = round(time.perf_counter() - t0, 1)

        t0 = time.perf_counter()
        carry_since = start.astimezone(timezone.utc) - CARRY_OVER
        recent = db.list_conversations_since(int(carry_since.timestamp()))
        counts = leads.sync(conversations=recent,
                            model_budget_seconds=settings.CHAT_LEAD_MODEL_BUDGET_SECONDS)
        model_seconds = counts.get("model_seconds", 0.0)
        result.update(leads=counts, scanned=counts.get("scanned", len(recent)),
                      refined=counts.get("refined", 0), deferred=counts.get("deferred", 0))
        timings["model"] = model_seconds
        timings["scan"] = round(max(0.0, time.perf_counter() - t0 - model_seconds), 1)

        reported = db.leads_to_email(int(start.timestamp()), int(end.timestamp()),
                                     int(carry_since.timestamp()))
        result["reported"] = len(reported)
        result["waiting"] = sum(1 for l in reported if l.get("handoff_state") == "claimed_not_fired")
        result["lead_ids"] = [l["conversation_id"] for l in reported]
        window_start_epoch = int(start.timestamp())
        result["carried"] = len(lead_report.split_carried(reported, window_start_epoch)[1])

        grouped = lead_report.group(reported)
        subject = lead_report.render_subject(grouped, day_label=short_label,
                                             carried=result["carried"])
        html_body = lead_report.render_report(reported, day_label=short_label, day_date=full_label,
                                              window_start_epoch=window_start_epoch)
        text_body = lead_report.render_text_report(reported, day_label=short_label,
                                                   day_date=full_label,
                                                   window_start_epoch=window_start_epoch)
        result["subject"] = subject
        if test:
            result["preview_html"] = html_body

        if send_email:
            t0 = time.perf_counter()
            recipients = list(test_recipients or []) if test else mailer.recipients_for("daily_leads")
            result["recipients"] = recipients
            send = sender or mailer.send
            if not reported and not settings.CHAT_LEADS_EMAIL_WHEN_EMPTY:
                result["email_status"] = db.EMAIL_NO_LEADS
                result["email_detail"] = "No leads in this window, so no email."
            elif test and not recipients:
                result["email_detail"] = "Test run: preview only, not emailed."
            else:
                if test:
                    outcome = send(f"[TEST] {subject}", html_body, to_addresses=recipients,
                                   text_body=text_body)
                else:
                    outcome = send(subject, html_body, purpose="daily_leads", text_body=text_body)
                # "Nobody configured" is a mailing switched off, not a fault.
                result["email_status"] = (db.EMAIL_SENT if outcome.get("success")
                                          else db.EMAIL_NO_RECIPIENTS if outcome.get("skipped")
                                          else db.EMAIL_FAILED)
                result["email_detail"] = outcome.get("detail", "")
                if outcome.get("success") and not test:
                    db.mark_leads_digested(result["lead_ids"])
            timings["email"] = round(time.perf_counter() - t0, 1)
            logger.info("Lead email %s: %s", result["email_status"], result["email_detail"])
        elif test:
            result["email_detail"] = "Test run: preview only, not emailed."
    except Exception as e:
        result["error"] = (result["error"] + " " if result["error"] else "") + f"The run failed: {e}"
        logger.exception("Lead run failed")
    finally:
        elapsed = time.perf_counter() - started
        timings["total"] = round(elapsed, 1)
        result["duration_ms"] = int(elapsed * 1000)
        if record:
            _record(result)
        if use_lock:
            _release_lock()
    return result


# The name the dashboard and older scripts used.
run_daily = run_leads


def check_for_leads(*, triggered_by: str = "", fetcher=None, use_lock: bool = True) -> dict:
    """The dashboard's "Check for leads": fetch new chats and find the leads,
    without emailing and without being recorded as an email run."""
    return run_leads(send_email=False, trigger=db.TRIGGER_MANUAL, triggered_by=triggered_by,
                     record=False, use_lock=use_lock, fetcher=fetcher)


def _record(result: dict) -> int | None:
    """Files the run in lead_digests. Never raises: bookkeeping must not fail a
    job whose real work is already done."""
    try:
        return db.record_lead_digest({
            "day_start": result.get("window_start_date", ""),
            "day_end": result.get("window_end_date", ""),
            "day_label": result.get("day_date", ""),
            "ran_at": datetime.now(ZONE).isoformat(timespec="seconds"),
            "run_trigger": result.get("trigger") or db.TRIGGER_SCHEDULED,
            "triggered_by": result.get("triggered_by") or "",
            "fetched": result.get("fetched", 0),
            "reported": result.get("reported", 0),
            "waiting": result.get("waiting", 0),
            "email_status": result.get("email_status", ""),
            "email_detail": result.get("email_detail", ""),
            "recipients": ", ".join(result.get("recipients") or []),
            "subject": result.get("subject") or "",
            "error": result.get("error") or "",
            "slot": result.get("slot") or "",
            "window_start_at": result.get("window_start_at", ""),
            "window_end_at": result.get("window_end_at", ""),
            "is_test": 1 if result.get("test") else 0,
            "refined": result.get("refined", 0),
            "deferred": result.get("deferred", 0),
            "carried": result.get("carried", 0),
            "duration_ms": result.get("duration_ms", 0),
            "timings": json.dumps(result.get("timings") or {}),
        })
    except Exception:
        logger.exception("Could not record this run in lead_digests")
        return None


# ---------------------------------------------------------------------------
# Did every slot run?
# ---------------------------------------------------------------------------

def slot_coverage(days: int = 2, now: datetime | None = None) -> list[dict]:
    """Every scheduled slot from the start of `days` days ago up to now, newest
    first, each with the run that covered it and one of:

      ran        a run is recorded for it
      due        its time has just passed; the run may still be going
      missed     no run, and long enough ago that there should have been one
      untracked  before the first slot-based run was ever recorded

    "Missed" is the state worth showing: leads from that window reached nobody
    until the next run carried them in.
    """
    local_now = to_au(now or _utc_now())
    slots = []
    for back in range(days):
        slots += [s for s in _slots_on((local_now - timedelta(days=back)).date()) if s <= local_now]
    slots.sort(reverse=True)
    if not slots:
        return []
    first_recorded = db.earliest_slot_run()
    first = to_au(first_recorded) if first_recorded else None
    runs = db.lead_runs_between(_iso(min(slots) - timedelta(minutes=1)),
                                _iso(local_now + timedelta(days=1)))
    by_end: dict[str, dict] = {}
    for run in runs:
        if run.get("is_test") or not run.get("slot"):
            continue
        by_end.setdefault(run["window_end_at"], run)
    out = []
    for slot in slots:
        run = by_end.get(_iso(slot))
        if run:
            state = "ran"
        elif first is None or slot < first:
            state = "untracked"
        elif local_now - slot < MISSED_AFTER:
            state = "due"
        else:
            state = "missed"
        out.append({"slot": slot, "window_start": slot_before(slot), "run": run, "state": state})
    return out


def missed_slots(days: int = 2, now: datetime | None = None) -> list[dict]:
    return [c for c in slot_coverage(days, now) if c["state"] == "missed"]


def other_runs(days: int = 2, now: datetime | None = None, *, tests: bool = False) -> list[dict]:
    """Runs a person started (catch-ups, or tests when `tests`), newest first."""
    local_now = to_au(now or _utc_now())
    since = datetime.combine((local_now - timedelta(days=days - 1)).date(), dtime(0), tzinfo=ZONE)
    runs = db.lead_runs_between(_iso(since), _iso(local_now + timedelta(days=1)))
    return [r for r in runs
            if bool(r.get("is_test")) == tests and (tests or not r.get("slot"))]


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------

def _previous_run_failed() -> bool:
    """Whether the run before this one also failed -- so the job failure alert
    goes out once per problem, not five times a day while it lasts."""
    runs = [r for r in db.list_lead_digests(limit=6) if not r.get("is_test")]
    if len(runs) < 2:
        return False
    previous = runs[1]
    return bool(previous.get("error")) or previous.get("email_status") == db.EMAIL_FAILED


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description="Lead capture and call-list email.")
    parser.add_argument("--no-email", action="store_true",
                        help="Find and store leads without sending the email")
    parser.add_argument("--no-fetch", action="store_true",
                        help="Skip Chatbase; use conversations already stored")
    parser.add_argument("--slot", metavar="HH:MM",
                        help="Report the window of this scheduled time (today's, or yesterday's "
                             "if it has not come yet), e.g. to re-send a missed run")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be sent, without fetching, sending or recording")
    args = parser.parse_args(argv)

    trigger = default_trigger()
    window, slot = None, ""
    if args.slot:
        window = window_for_slot(args.slot, 0)
        if window[1] > datetime.now(ZONE):
            window = window_for_slot(args.slot, -1)
        slot = window[1].strftime("%H:%M")

    if args.dry_run:
        start, end = window or (scheduled_window() if trigger == db.TRIGGER_SCHEDULED
                                else catch_up_window())
        short_label, full_label = window_label(start, end)
        carry_since = start.astimezone(timezone.utc) - CARRY_OVER
        reported = db.leads_to_email(int(start.timestamp()), int(end.timestamp()),
                                     int(carry_since.timestamp()))
        carried = len(lead_report.split_carried(reported, int(start.timestamp()))[1])
        print(f"Window: {full_label}")
        print("Subject: " + lead_report.render_subject(lead_report.group(reported),
                                                       day_label=short_label, carried=carried))
        print()
        print(lead_report.render_text_report(reported, day_label=short_label, day_date=full_label,
                                             window_start_epoch=int(start.timestamp())))
        print()
        print(mailer.config_status_for("daily_leads"))
        return 0

    result = run_leads(send_email=not args.no_email, fetch=not args.no_fetch,
                       trigger=trigger, window=window, slot=slot)
    if result["skipped"]:
        print(result["error"])
        return 0

    t = result["timings"]
    print(f"Window: {result['day_date']}")
    print(f"Fetched {result['fetched']} chat(s), scanned {result['scanned']}")
    print(f"Leads: {result['leads']}")
    print(f"Model tidied {result['refined']} lead(s) in {t.get('model', 0)} s"
          + (f"; {result['deferred']} left for the next run" if result["deferred"] else ""))
    print(f"In this email: {result['reported']} lead(s) ({result['carried']} carried from earlier), "
          f"{result['waiting']} expecting a callback we promised")
    print(f"Email: {result['email_status']} {result['email_detail']}")
    if result["recipients"]:
        print(f"Recipients: {', '.join(result['recipients'])}")
    print(f"Took {t.get('total', 0)} s (fetch {t.get('fetch', '-')}, scan {t.get('scan', '-')}, "
          f"model {t.get('model', '-')}, email {t.get('email', '-')})")

    problem = result["error"] or (
        f"The lead email could not be sent: {result['email_detail']}"
        if result["email_status"] == db.EMAIL_FAILED else "")
    if problem:
        print(f"WARNING: {problem}")
        if not _previous_run_failed():
            try:
                alerts.send_job_failure_alert("Lead emails", problem)
            except Exception:
                logger.exception("Could not send the failure alert")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
