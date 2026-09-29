"""
The scheduled-job status panel: reading schtasks, translating result codes, and
being honest about tasks this account cannot see.

Windows is never actually called: every case feeds a captured CSV through a
stand-in for subprocess.run.
"""
import logging
import os
import subprocess
import sys

ROOT = r"C:\bcsands\Generic-Content-Agent"
sys.path.insert(0, ROOT)
os.chdir(ROOT)
logging.disable(logging.CRITICAL)

from fastapi.testclient import TestClient  # noqa: E402
from dashboard import app as dash, auth, task_status  # noqa: E402

failures = []


def check(ok, label, extra=""):
    print(f"  {'OK ' if ok else 'FAIL'}  {label}{(' -- ' + str(extra)) if extra else ''}")
    if not ok:
        failures.append(label)


HEADER = ('"HostName","TaskName","Next Run Time","Status","Logon Mode","Last Run Time",'
          '"Last Result","Author","Task To Run","Start In","Comment","Scheduled Task State",'
          '"Idle Time","Power Management","Run As User","Delete Task If Not Rescheduled",'
          '"Stop Task If Runs X Hours and X Mins","Schedule","Schedule Type","Start Time",'
          '"Start Date","End Date","Days","Months","Repeat: Every","Repeat: Until: Time",'
          '"Repeat: Until: Duration","Repeat: Stop If Still Running"')


def row(name, status="Ready", last="28/09/2026 1:11:00 AM", result="0",
        nxt="29/09/2026 1:11:00 AM", kind="Daily", start="1:11:00 AM", days="N/A",
        every="Disabled", run_as="SYSTEM"):
    return (f'"SERVER","{name}","{nxt}","{status}","Interactive/Background","{last}",'
            f'"{result}","SERVER\\admin","cmd.exe","N/A","","Enabled","Disabled",'
            f'"No","{run_as}","Disabled","72:00:00","Scheduling data...","{kind}","{start}",'
            f'"1/09/2026","N/A","{days}","N/A","{every}","N/A","N/A","N/A"')


def feed(csv_text: str, *, stderr: str = "", boom: Exception | None = None):
    """Replace subprocess.run with one that answers from a string, and count calls."""
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        if boom:
            raise boom
        return subprocess.CompletedProcess(command, 0, csv_text.encode("utf-8"),
                                           stderr.encode("utf-8"))

    task_status.subprocess.run = fake_run
    task_status._cache.update(at=0.0, value=None)
    return calls


real_run = subprocess.run
try:
    print("Result codes, in English:")
    for code, expected in ((0, "Success"), (267009, "Running now"), (267011, "Has never run"),
                           (267014, "Last run was stopped"),
                           (2147943726, "Could not sign in"), (9009, "exit code 9009")):
        label, badge = task_status.describe_result(code)
        check(expected in label, f"{code} reads as \u201c{label}\u201d")
    check(task_status.describe_result(9009)[1] == "confidence-low",
          "an unknown code is shown as a failure")
    check(task_status.describe_result(0)[1] == "live", "and success is not")

    print("\nSchedules, in words:")
    check(task_status.describe_schedule(
        {"Schedule Type": "Daily", "Start Time": "4:00:00 AM", "Days": "N/A",
         "Repeat: Every": "Disabled"}) == "Daily at 4:00:00 AM", "a daily task")
    check(task_status.describe_schedule(
        {"Schedule Type": "Weekly", "Start Time": "2:00:00 AM", "Days": "MON",
         "Repeat: Every": "Disabled"}) == "Weekly on MON at 2:00:00 AM", "a weekly task")
    check("repeating every 0 Hour(s), 5 Minute(s)" in task_status.describe_schedule(
        {"Schedule Type": "One Time Only", "Start Time": "12:00:00 AM", "Days": "N/A",
         "Repeat: Every": "0 Hour(s), 5 Minute(s)"}), "a repeating task says how often")
    check(task_status.describe_schedule({"Schedule Type": "", "Start Time": ""}) == "At startup",
          "and a service with no schedule")

    print("\nTimes:")
    check(task_status._when("28/09/2026 1:11:00 AM").startswith("Mon 28 Sep 2026, 1:11 am"),
          "a local time is made readable", task_status._when("28/09/2026 1:11:00 AM"))
    check(task_status._when("N/A") == "", "N/A is nothing, not a date")
    check(task_status._when("sometime tuesday") == "sometime tuesday",
          "an unexpected format is shown as written, not dropped")

    print("\nReading the task list:")
    csv_text = "\n".join([HEADER,
                          row("\\BCSands-ContentAgent-Daily"),
                          row("\\BCSands-Leads", kind="Daily", start="4:00:00 AM"),
                          row("\\BCSands-Dashboard", status="Running", result="267009",
                              nxt="N/A", kind="", start=""),
                          row("\\Microsoft\\Windows\\SomethingElse"),
                          HEADER,  # /v repeats the header per folder
                          row("\\BCSands-SiteMonitor", every="0 Hour(s), 5 Minute(s)")])
    calls = feed(csv_text)
    state = task_status.tasks()
    by_name = {r["name"]: r for r in state["rows"]}
    check(not state["error"], "no error when Windows answers", state["error"])
    check(by_name["BCSands-ContentAgent-Daily"]["known"]
          and by_name["BCSands-ContentAgent-Daily"]["result"] == "Success",
          "a listed task carries its result")
    check(by_name["BCSands-Dashboard"]["result"] == "Running now",
          "the dashboard shows as running, not failed")
    check(by_name["BCSands-SiteMonitor"]["schedule"].endswith("0 Hour(s), 5 Minute(s)"),
          "the site monitor's repeat is read", by_name["BCSands-SiteMonitor"]["schedule"])
    check(not by_name["BCSands-ReclaimImages"]["known"]
          and by_name["BCSands-ReclaimImages"]["result"] == "Not visible to this dashboard",
          "a task that is not listed is called not visible, not missing")
    check(state["missing"] == ["BCSands-ChatInsights-Weekly", "BCSands-ReclaimImages",
                               "BCSands-Ollama"],
          "and every one of them named -- this listing has four of the seven",
          state["missing"])
    check(all("SomethingElse" not in r["name"] for r in state["rows"]) and not state["extra"],
          "other people's tasks are left out")
    check(len(calls) == 1, "one call to Windows", len(calls))

    print("\nThe cache:")
    task_status.tasks()
    task_status.tasks()
    check(len(calls) == 1, "repeat reads come from the cache -- the query takes ~2 s", len(calls))
    task_status.tasks(force=True)
    check(len(calls) == 2, "and it can be forced")

    print("\nWhen Windows will not answer:")
    feed("", boom=FileNotFoundError())
    check("not on this server" in task_status.tasks()["error"], "schtasks missing is explained")
    feed("", boom=subprocess.TimeoutExpired("schtasks", 25))
    check("too long" in task_status.tasks()["error"], "a slow query is explained")
    feed("", stderr="ERROR: Access is denied.")
    state = task_status.tasks()
    check("Access is denied" in state["error"], "a refusal is passed through", state["error"])
    check(all(not r["known"] for r in state["rows"]) and len(state["rows"]) == 7,
          "and every task still gets a row saying it could not be read")

    print("\nOn the page:")
    feed(csv_text)
    admin_user = next(u for u in auth.list_users() if u["role"] == auth.ROLE_ADMIN and u["active"])
    reviewer_user = next(u for u in auth.list_users()
                         if u["role"] == auth.ROLE_REVIEWER and u["active"])

    def client_for(name):
        c = TestClient(dash.app)
        c.cookies.set(auth.SESSION_COOKIE, auth.create_session(name))
        return c

    page = client_for(admin_user["username"]).get("/settings/jobs").text
    check("Scheduled jobs" in page and "BCSands-ContentAgent-Daily" in page,
          "the job page lists the tasks")
    check("Running now" in page and "Not visible to this dashboard" in page,
          "with their results, including the ones it cannot see")
    check("permissions difference" in page,
          "and explains that not visible is not the same as missing")
    check("?log=daily_run" in page, "each task links to its own log")
    check("Run" not in page.split("Scheduled jobs")[1].split("</table>")[0].replace("Running", ""),
          "there is no button to start a task from the web")
    r = client_for(reviewer_user["username"]).get("/settings/jobs", follow_redirects=False)
    check(r.status_code == 303 and r.headers.get("location") == "/",
          "a reviewer cannot see it at all", r.status_code)
finally:
    task_status.subprocess.run = real_run
    task_status._cache.update(at=0.0, value=None)

print()
if failures:
    print("FAILURES:")
    for f in failures:
        print(" -", f)
    sys.exit(1)
print("ALL TASK STATUS CHECKS PASS")
