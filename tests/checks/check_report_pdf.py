"""
Downloading the weekly chat report as a PDF.

One real render, because "it produced a PDF" is the whole claim and a stubbed
renderer would prove nothing. Everything else runs against throwaway
databases; a probe Sales account is deleted afterwards.
"""
import json
import logging
import os
import re
import secrets
import shutil
import sqlite3
import sys
import tempfile

ROOT = r"C:\bcsands\Generic-Content-Agent"
sys.path.insert(0, ROOT)
os.chdir(ROOT)
logging.disable(logging.CRITICAL)

TMP = tempfile.mkdtemp(prefix="report_pdf_test_")
from content_seo_agent import db as queue_db  # noqa: E402
queue_db.DB_PATH = os.path.join(TMP, "review_queue.db")
queue_db.init_db()
from chat_insights import db as chat_db  # noqa: E402
chat_db.DB_PATH = os.path.join(TMP, "chat_insights.db")
chat_db.init_db()

from fastapi.testclient import TestClient  # noqa: E402
from dashboard import app as dash, auth, pdf as report_pdf  # noqa: E402

failures = []


def check(ok, label, extra=""):
    print(f"  {'OK ' if ok else 'FAIL'}  {label}{(' -- ' + str(extra)) if extra else ''}")
    if not ok:
        failures.append(label)


REPORT_HTML = """<!DOCTYPE html><html><head><meta charset="utf-8"><title>Weekly chat report</title>
<style>body{font-family:Segoe UI,Arial;padding:28px;color:#0F172A}
h1{color:#004495} td,th{border:1px solid #ccc;padding:6px}</style></head>
<body><h1>Chat report</h1><p>Week of 7 September 2026</p>
<table><tr><th>Category</th><th>Chats</th></tr>
<tr><td>Product enquiry</td><td>144</td></tr><tr><td>Delivery</td><td>32</td></tr></table>
<p>Lead: Jane Citizen, 0412 345 678</p></body></html>"""


def add_run(week_start, week_end, report_html, status="complete"):
    with sqlite3.connect(chat_db.DB_PATH) as conn:
        cur = conn.execute(
            "INSERT INTO weekly_runs (week_start, week_end, started_at, finished_at, status, "
            "conversation_count, analysed_count, stats_json, report_html, email_status) "
            "VALUES (?, ?, '2026-09-14T01:00:00+00:00', '2026-09-14T01:10:00+00:00', ?, 40, 40, "
            "?, ?, 'sent')",
            (week_start, week_end, status, json.dumps({"total_conversations": 40}), report_html))
        return cur.lastrowid


PROBE = f"probe_sales_{secrets.token_hex(3)}"
try:
    print("The renderer:")
    ready, detail = report_pdf.is_available()
    check(ready, "a browser to print with is on this server", detail)
    data = report_pdf.render(REPORT_HTML)
    check(data.startswith(b"%PDF") and len(data) > 5000, "it makes a real PDF",
          f"{len(data):,} bytes, {data[:8]!r}")

    os.environ["PDF_BROWSER"] = r"C:\nope\chrome.exe"
    try:
        report_pdf.browser_path()
        check(False, "a missing PDF_BROWSER is reported", "no error raised")
    except report_pdf.PdfError as e:
        check("not there" in str(e), "a missing PDF_BROWSER is reported", e)
    os.environ.pop("PDF_BROWSER")
    real_browsers = report_pdf.BROWSERS
    report_pdf.BROWSERS = ()
    try:
        report_pdf.browser_path()
        ok = False
    except report_pdf.PdfError as e:
        ok = "Chrome or Edge" in str(e)
    finally:
        report_pdf.BROWSERS = real_browsers
    check(ok, "and so is having no browser at all")

    print("\nThe download:")
    run_id = add_run("2026-09-07", "2026-09-13", REPORT_HTML)
    empty_run = add_run("2026-08-31", "2026-09-06", "")
    auth.create_user(PROBE, secrets.token_urlsafe(16), role=auth.ROLE_SALES)
    admin_user = next(u for u in auth.list_users() if u["role"] == auth.ROLE_ADMIN and u["active"])
    reviewer_user = next(u for u in auth.list_users()
                         if u["role"] == auth.ROLE_REVIEWER and u["active"])

    def client_for(name):
        c = TestClient(dash.app)
        c.cookies.set(auth.SESSION_COOKIE, auth.create_session(name))
        return c

    def flash(response):
        fid = response.cookies.get(auth.FLASH_COOKIE)
        return (auth.take_flash(fid) if fid else None) or ("", "")

    admin, reviewer, sales = (client_for(admin_user["username"]),
                              client_for(reviewer_user["username"]), client_for(PROBE))

    page = admin.get(f"/chat-insights/run/{run_id}").text
    check(f'href="/chat-insights/run/{run_id}/report.pdf"' in page and "Download PDF" in page,
          "the report page offers the download")

    r = admin.get(f"/chat-insights/run/{run_id}/report.pdf")
    disposition = r.headers.get("content-disposition", "")
    check(r.status_code == 200 and r.headers.get("content-type") == "application/pdf",
          "it downloads as a PDF", (r.status_code, r.headers.get("content-type")))
    check(r.content.startswith(b"%PDF") and len(r.content) > 5000, "with real PDF content",
          f"{len(r.content):,} bytes")
    check(disposition.startswith("attachment;")
          and "bc-sands-chat-report-2026-09-07-to-2026-09-13.pdf" in disposition,
          "named for the week it covers", disposition)
    check(r.headers.get("cache-control") == "no-store", "and is not cached")

    print("\nWho can take one:")
    for who, client in (("a reviewer", reviewer), ("Sales", sales)):
        r = client.get(f"/chat-insights/run/{run_id}/report.pdf")
        check(r.status_code == 200 and r.content.startswith(b"%PDF"),
              f"{who} can download it, like reading it on screen", r.status_code)
    signed_out = TestClient(dash.app).get(f"/chat-insights/run/{run_id}/report.pdf",
                                          follow_redirects=False)
    check(signed_out.status_code == 303 and "/login" in signed_out.headers.get("location", ""),
          "signed out gets the login page, not a report", signed_out.status_code)

    print("\nWhen there is nothing to print:")
    r = admin.get(f"/chat-insights/run/{empty_run}/report.pdf", follow_redirects=False)
    kind, message = flash(r)
    check(r.status_code == 303 and kind == "err" and "did not produce a report" in message,
          "a run with no report says so", message)
    r = admin.get("/chat-insights/run/999999/report.pdf", follow_redirects=False)
    kind, message = flash(r)
    check(r.status_code == 303 and kind == "err" and "no longer here" in message,
          "and an unknown run does too", message)
    check("Download PDF" not in admin.get(f"/chat-insights/run/{empty_run}").text,
          "the button is not offered for a run with no report")

    print("\nWhen the browser is missing:")
    real_render = dash.report_pdf.render
    dash.report_pdf.render = lambda *a, **k: (_ for _ in ()).throw(
        report_pdf.PdfError("No browser to make the PDF with. Chrome or Edge has to be installed."))
    r = admin.get(f"/chat-insights/run/{run_id}/report.pdf", follow_redirects=False)
    kind, message = flash(r)
    check(r.status_code == 303 and kind == "err" and "No browser" in message,
          "the failure is explained, not a stack trace", message)
    real_available = dash.report_pdf.is_available
    dash.report_pdf.is_available = lambda: (False, "Chrome or Edge has to be installed.")
    check("PDF download unavailable" in admin.get(f"/chat-insights/run/{run_id}").text,
          "and the page says the download is unavailable")
    dash.report_pdf.render, dash.report_pdf.is_available = real_render, real_available

    print("\nIn the activity log:")
    entries = [e for e in queue_db.list_audit(limit=50) if e["action"] == "report_downloaded"]
    check(len(entries) == 3, "each download is recorded -- it leaves with customer details in it",
          len(entries))
    check(entries and "7 Sep 2026" in (entries[0].get("detail") or ""),
          "naming the week it covered", entries[0].get("detail") if entries else "")
finally:
    try:
        auth.delete_user(PROBE)
    except Exception as e:
        print("could not delete probe:", e)
        failures.append("probe cleanup")
    shutil.rmtree(TMP, ignore_errors=True)

print()
if failures:
    print("FAILURES:")
    for f in failures:
        print(" -", f)
    sys.exit(1)
print("ALL REPORT PDF CHECKS PASS")
