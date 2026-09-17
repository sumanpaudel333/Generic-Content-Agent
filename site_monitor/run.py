"""
The site monitor job. The BCSands-SiteMonitor task runs it every five minutes:

    python -m site_monitor.run

One run loads each switched-on page once, records what it found, opens and
closes incidents, and sends -- or, during quiet hours, holds -- the emails
that are due. The first run after the morning summary time sends a summary
of anything held overnight.

What it does to keep off the shop's back, in one place:

  * One request per page, one page at a time, with a pause between them.
  * One shop session per route, kept and reused across runs. A new Zen Cart
    session costs the server several seconds; an existing one well under one.
  * No retry when a page fails: the next scheduled run is the confirmation.
    The only second request is a single re-check when a page is slow, so a
    cold cache or a just-renewed session is not reported as a slow site.
  * A lock, so a run that overlaps the next one is skipped, never doubled.
"""
import argparse
import logging
import os
import sys
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(REPO_ROOT, ".env"))

from site_monitor import emails, probe, rules, store  # noqa: E402
from config.localtime import to_au  # noqa: E402

logger = logging.getLogger("site_monitor.run")

LOCK_PATH = os.path.join(REPO_ROOT, "logs", "site_monitor.lock")
LOCK_STALE_SECONDS = 600
PAUSE_BETWEEN_PAGES = 1.0


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
            # Left behind by a run that died. Old enough not to be a live one.
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


def _check_target(target: dict, settings: dict, fetch) -> dict:
    param = target.get("session_param") or ""
    host = urlparse(target["url"]).hostname or ""
    # One session per route as well as per site. Through Cloudflare and direct
    # may not land on the same shop server, and a session one of them rejects
    # must not keep invalidating the other's.
    session_key = f"session:{target.get('route', 'direct')}:{host}"
    session_id = ""
    if param:
        session_id = store.get_state(session_key, "")
        if not session_id:
            session_id = probe.new_session_id()
            store.set_state(session_key, session_id)

    timeout = settings["timeout_seconds"]
    notes = []
    result = fetch(target, session_id=session_id, timeout=timeout)
    requests = 1

    # The shop sends a session it does not recognise back to the landing page.
    # One fresh session and one more try -- not a loop.
    if param and result.get("cross_host_redirect"):
        session_id = probe.new_session_id()
        store.set_state(session_key, session_id)
        result = fetch(target, session_id=session_id, timeout=timeout)
        requests += 1
        notes.append("Started a new shop session")

    outcome, detail = probe.classify(target, result, settings)
    if outcome == "slow":
        retry = fetch(target, session_id=session_id, timeout=timeout)
        requests += 1
        retry_outcome, retry_detail = probe.classify(target, retry, settings)
        if retry_outcome == "ok":
            notes.append(f"First load took {result['ms'] / 1000:.1f} s and counted as warming up")
            result, outcome, detail = retry, retry_outcome, retry_detail
        elif retry_outcome == "slow":
            result, outcome, detail = retry, retry_outcome, retry_detail

    detail = ". ".join(part.rstrip(".") for part in [detail] + notes if part)
    if detail:
        detail += "."
    return {"outcome": outcome, "detail": detail, "status": result.get("status"),
            "ms": result.get("ms"), "cert_days": result.get("cert_days"),
            "cert_expires": result.get("cert_expires") or "",
            "final_url": result.get("final_url") or "", "ip": result.get("ip") or "",
            "error_kind": result.get("error_kind") or "", "requests": requests}


def _blocked_detail(detail: str, seen_as: str, allowed: str) -> str:
    """Why Cloudflare is blocking the customer-route check, as far as can be told.

    This page is chosen because Cloudflare's rules do not match it, and its
    session is sent as a cookie because those rules do not read cookies. So a
    block means something changed at Cloudflare rather than a fault on the
    site. The office's internet address is named as well: allowing it in
    Cloudflare is the other way to let the monitor through, and it is worth
    knowing when that address has changed.
    """
    detail = (f"{detail} This page is checked because Cloudflare's rules do not match it, so a "
              "block means those rules have changed.")
    if seen_as and allowed and seen_as != allowed:
        return (f"{detail} This server also now reaches the internet from {seen_as}, not {allowed} "
                "as set in settings.")
    if seen_as:
        return (f"{detail} Either check the rules, or allow this server's internet address, "
                f"{seen_as}, in Cloudflare.")
    return f"{detail} Either check the rules, or allow the office's internet address in Cloudflare."


def _mark(event: dict, now: datetime) -> None:
    incident_id = event.get("incident_id")
    field = {"opened": "notified_at", "reminder": "last_reminder_at",
             "recovered": "recovery_notified_at"}.get(event["type"])
    if incident_id and field:
        store.mark_incident(incident_id, field, now)


def _dispatch(events: list, now: datetime, settings: dict, send, report: dict) -> None:
    # Quiet hours and the summary time are Australian Eastern, like every time
    # shown on the dashboard -- not whatever zone the server is set to.
    local_now = to_au(now)
    quiet = rules.in_quiet_hours(local_now, settings["quiet_start"], settings["quiet_end"])
    summary_pending = store.count_held() > 0
    for event in events:
        if event["type"] == "reminder" and (quiet or summary_pending):
            # Overnight, the morning summary says it is still down. Sending a
            # reminder minutes before that summary would only say it twice.
            continue
        if quiet:
            store.hold(event, now)
            report["held"].append(event)
            _mark(event, now)
            continue
        subject, html_body, text_body = emails.render_event(event)
        outcome = send(subject, html_body, text_body, settings["recipients"])
        report["sent"].append({"type": event["type"], "subject": subject, "outcome": outcome})
        if outcome.get("success") or outcome.get("skipped"):
            _mark(event, now)
        else:
            # Left unmarked on purpose: the next run tries this email again.
            logger.error("Site monitor email did not send: %s", outcome.get("detail"))


def _morning_summary(now: datetime, settings: dict, send, report: dict) -> None:
    local_now = to_au(now)
    if rules.in_quiet_hours(local_now, settings["quiet_start"], settings["quiet_end"]):
        return
    if local_now.strftime("%H:%M") < settings["morning_summary_at"]:
        return
    today = local_now.date().isoformat()
    if store.get_state("last_summary_date") == today:
        return
    held = store.take_held()
    if held:
        targets = {t["key"]: t for t in settings["targets"]}
        subject, html_body, text_body = emails.render_summary(held, store.open_incidents(), targets)
        outcome = send(subject, html_body, text_body, settings["recipients"])
        report["summary"] = {"events": len(held), "subject": subject, "outcome": outcome}
        if not (outcome.get("success") or outcome.get("skipped")):
            for event in held:
                store.hold(event, now)
            logger.error("Morning summary did not send: %s", outcome.get("detail"))
            return
    store.set_state("last_summary_date", today)


def run_once(*, now: datetime | None = None, fetch=None, send=None, control=None, egress=None,
             pause: float = PAUSE_BETWEEN_PAGES, use_lock: bool = True,
             settings: dict | None = None) -> dict:
    """One monitoring pass. Everything that talks to the outside world can be
    swapped out, so the tests never touch the real site or send real mail."""
    fetch = fetch or probe.fetch
    send = send or emails.send
    control = control or probe.control_ok
    egress = egress or probe.egress_ip
    if use_lock and not _acquire_lock():
        logger.info("Another site monitor run is still going; skipping this one.")
        return {"skipped": True}
    try:
        now = now or datetime.now(timezone.utc)
        settings = settings or store.get_settings()
        targets = [t for t in settings["targets"] if t.get("enabled")]
        report = {"skipped": False, "checks": [], "sent": [], "held": [], "summary": None,
                  "offline": False, "requests": 0}
        internet_ok = None
        seen_as = None
        events: list = []

        for index, target in enumerate(targets):
            if index and pause:
                time.sleep(pause)
            check = _check_target(target, settings, fetch)
            report["requests"] += check["requests"]

            # An outside page that cannot be reached might mean the office has
            # lost its internet rather than the page being down. Ask once per
            # run, only when it matters. The shop server on the office network
            # does not depend on the internet, so it is never excused this way.
            external = not check["ip"] or not probe.is_private(check["ip"])
            if check["outcome"] == "down" and external:
                if internet_ok is None:
                    internet_ok = bool(control())
                if not internet_ok:
                    check["outcome"] = "monitor_offline"
                    check["detail"] = ("This server could not reach the internet, so the page "
                                       "could not be checked.")
                    report["offline"] = True

            if check["outcome"] == "blocked" and target.get("route") == store.ROUTE_PUBLIC:
                if seen_as is None:
                    seen_as = egress() or ""
                check["detail"] = _blocked_detail(check["detail"], seen_as,
                                                  settings.get("office_ip", ""))

            store.record_check(target["key"], now, check["outcome"], status=check["status"],
                               ms=check["ms"], detail=check["detail"],
                               cert_days=check["cert_days"], cert_expires=check["cert_expires"],
                               final_url=check["final_url"], ip=check["ip"],
                               requests=check["requests"])
            report["checks"].append({"target": target["key"], **check})
            events += rules.evaluate_target(target, now, settings)
            events += rules.cert_events(target, check, now, settings)

        store.set_state("last_run_at", store.iso(now))
        store.set_state("last_run_offline", "1" if report["offline"] else "0")
        _dispatch(events, now, settings, send, report)
        _morning_summary(now, settings, send, report)
        store.purge(settings["retention_days"], now)
        return report
    finally:
        if use_lock:
            _release_lock()


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    argparse.ArgumentParser(
        description="Check the online shop once and send any alerts that are due.").parse_args(argv)
    try:
        report = run_once()
    except Exception as e:
        logger.exception("Site monitor run failed")
        try:
            from chat_insights import alerts
            alerts.send_job_failure_alert("Site monitor", str(e))
        except Exception:
            logger.exception("Could not send the job failure alert")
        return 1
    if report.get("skipped"):
        print("Skipped: another run is still going.")
        return 0
    for check in report["checks"]:
        ms = "-" if check["ms"] is None else f"{check['ms']:,}"
        print(f"{check['target']:24} {check['outcome']:15} {ms:>7} ms  {check['detail']}")
    print(f"requests to the site: {report['requests']}  emails sent: {len(report['sent'])}  "
          f"held: {len(report['held'])}" + ("  summary sent" if report["summary"] else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
