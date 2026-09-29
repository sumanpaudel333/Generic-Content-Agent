"""
Roles and what each one can reach.

Rebuilt after the original was lost from a temporary folder -- which is why
these live in the repo now. Covers the permission table, the section gates
that decide which pages a role can open at all, and a sample of the actions
each role must not be able to post.

Uses a probe account per role, deleted afterwards, and signs in as existing
admin and reviewer accounts without changing them. Nothing is emailed and no
job is started: the handful of routes that would do either are stubbed.
"""
import logging
import os
import secrets
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
os.chdir(ROOT)
logging.disable(logging.CRITICAL)

TMP = tempfile.mkdtemp(prefix="roles_check_")
from content_seo_agent import db as queue_db  # noqa: E402
queue_db.DB_PATH = os.path.join(TMP, "review_queue.db")
queue_db.init_db()

from fastapi.testclient import TestClient  # noqa: E402
from dashboard import app as dash, auth  # noqa: E402

# Nothing may reach Chatbase, a mail server, Odoo or a model from a test.
dash.chat_daily.chatbase_client.fetch_conversations = lambda *a, **k: {"conversations": []}
dash.chat_daily.LOCK_PATH = os.path.join(TMP, "leads_job.lock")
dash.chat_leads.sync = lambda *a, **k: {"total": 0, "form_submission": 0, "contact_shared": 0}
dash.chat_alerts.send_lead_alerts = lambda *a, **k: {"sent": 0}

failures = []


def check(ok, label, extra=""):
    print(f"  {'OK ' if ok else 'FAIL'}  {label}{(' -- ' + str(extra)) if extra else ''}")
    if not ok:
        failures.append(label)


def flash(response):
    flash_id = response.cookies.get(auth.FLASH_COOKIE)
    return (auth.take_flash(flash_id) if flash_id else None) or ("", "")


def client_for(username):
    c = TestClient(dash.app)
    c.cookies.set(auth.SESSION_COOKIE, auth.create_session(username))
    return c


PROBE = f"probe_sales_{secrets.token_hex(3)}"
try:
    print("The permission table:")
    P = auth.PERMISSIONS
    check(P[auth.ROLE_SALES] == {auth.PERM_VIEW_CHAT, auth.PERM_VIEW_SITE_HEALTH,
                                 auth.PERM_WORK_LEADS, auth.PERM_FIND_LEADS},
          "sales: chat insights, site health, and working leads", sorted(P[auth.ROLE_SALES]))
    check(P[auth.ROLE_REVIEWER] == {auth.PERM_VIEW_CONTENT, auth.PERM_VIEW_CHAT,
                                    auth.PERM_VIEW_SITE_HEALTH, auth.PERM_REVIEW,
                                    auth.PERM_WORK_LEADS, auth.PERM_FIND_LEADS},
          "reviewer: all of that plus the content queue", sorted(P[auth.ROLE_REVIEWER]))
    admin_only = {auth.PERM_RUN_JOBS, auth.PERM_SEND_MAIL, auth.PERM_VIEW_MAIL_LOG,
                  auth.PERM_ADMINISTER}
    check(auth.ADMIN_ONLY_PERMISSIONS == admin_only
          and all(not (granted & admin_only) for role, granted in P.items()
                  if role != auth.ROLE_ADMIN),
          "email, jobs, the mail log and settings are admin-only")
    check(P[auth.ROLE_ADMIN] >= admin_only | P[auth.ROLE_REVIEWER] | P[auth.ROLE_SALES],
          "an admin holds everything")

    auth.create_user(PROBE, secrets.token_urlsafe(16), role=auth.ROLE_SALES)
    admin_user = next(u for u in auth.list_users() if u["role"] == auth.ROLE_ADMIN and u["active"])
    reviewer_user = next(u for u in auth.list_users()
                         if u["role"] == auth.ROLE_REVIEWER and u["active"])
    admin, reviewer, sales = (client_for(admin_user["username"]),
                              client_for(reviewer_user["username"]), client_for(PROBE))

    print("\nWhich pages each role can open:")
    # path -> who may open it
    PAGES = {
        "/": {"admin", "reviewer", "sales"},
        "/content-agent": {"admin", "reviewer"},
        "/content-agent/history": {"admin", "reviewer"},
        "/chat-insights": {"admin", "reviewer", "sales"},
        "/chat-insights/leads": {"admin", "reviewer", "sales"},
        "/site-health": {"admin", "reviewer", "sales"},
        "/assistant": {"admin"},
        "/settings/users": {"admin"},
        "/settings/jobs": {"admin"},
        "/settings/audit": {"admin"},
    }
    for path, allowed in PAGES.items():
        for who, client in (("admin", admin), ("reviewer", reviewer), ("sales", sales)):
            r = client.get(path, follow_redirects=False)
            if who in allowed:
                ok = r.status_code == 200
            else:
                ok = r.status_code == 303 and r.headers.get("location") == "/"
            check(ok, f"{path:26} {who:8} {'opens' if who in allowed else 'is sent away'}",
                  "" if ok else r.status_code)

    print("\nThe sidebar only offers what the role can open:")
    check('href="/content-agent"' not in sales.get("/").text
          and 'href="/assistant"' not in sales.get("/").text
          and 'href="/chat-insights"' in sales.get("/").text,
          "sales sees Chat Insights, not the Content Agent or the Assistant")
    check('href="/assistant"' not in reviewer.get("/").text, "a reviewer has no Assistant link")
    check('href="/assistant"' in admin.get("/").text, "an admin does")

    print("\nActions each role must not be able to post:")
    REFUSALS = [
        ("sales", sales, "/content-agent/bulk/publish", {"scope": "all"}, "/"),
        ("sales", sales, "/content-agent/fetch-new", {}, "/"),
        ("sales", sales, "/chat-insights/run", {}, None),
        ("sales", sales, "/chat-insights/leads/run-daily", {}, None),
        ("sales", sales, "/site-health/settings", {"target_count": "0"}, None),
        ("sales", sales, "/assistant/ask", {"question": "hi", "conversation_id": "0"}, "/"),
        ("reviewer", reviewer, "/content-agent/fetch-new", {}, None),
        ("reviewer", reviewer, "/content-agent/bulk/publish", {"scope": "all"}, None),
        ("reviewer", reviewer, "/settings/users/create", {"username": "x"}, "/"),
        ("reviewer", reviewer, "/chat-insights/run/1/email", {}, None),
    ]
    for who, client, path, data, location in REFUSALS:
        r = client.post(path, data=data, follow_redirects=False)
        kind, message = flash(r)
        if location:
            ok = r.status_code == 303 and r.headers.get("location") == location
        else:
            ok = r.status_code == 303 and kind == "err" and "permission" in message.lower()
        check(ok, f"{who:8} cannot post {path}", "" if ok else (r.status_code, message))

    print("\nWhat sales and reviewers may do:")
    r = sales.post("/chat-insights/leads/sync", follow_redirects=False)
    check(flash(r)[0] == "ok", "sales can check for leads")
    check(reviewer.get("/content-agent").status_code == 200,
          "a reviewer still opens the review queue")
    check(admin.get("/settings/jobs").status_code == 200, "an admin still opens job history")

    print("\nSigned out:")
    stranger = TestClient(dash.app)
    r = stranger.get("/chat-insights/leads", follow_redirects=False)
    check(r.status_code == 303 and "/login" in r.headers.get("location", ""),
          "a page redirects to the login", r.status_code)
    check(stranger.post("/api/assistant/ask", json={"question": "hi"}).status_code == 401,
          "and an API call gets 401, not a redirect")
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
print("ALL ROLE CHECKS PASS")
