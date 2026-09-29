"""
The lead detail page: everything already recorded about one lead, plus the note.

Throwaway chat and review-queue databases; a probe Sales account, deleted
afterwards. No email is sent and Chatbase is never called.
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

TMP = tempfile.mkdtemp(prefix="lead_detail_test_")
from content_seo_agent import db as queue_db  # noqa: E402
queue_db.DB_PATH = os.path.join(TMP, "review_queue.db")
queue_db.init_db()
from chat_insights import db as chat_db  # noqa: E402
chat_db.DB_PATH = os.path.join(TMP, "chat_insights.db")
chat_db.init_db()

from fastapi.testclient import TestClient  # noqa: E402
from dashboard import app as dash, auth  # noqa: E402

failures = []


def check(ok, label, extra=""):
    print(f"  {'OK ' if ok else 'FAIL'}  {label}{(' -- ' + str(extra)) if extra else ''}")
    if not ok:
        failures.append(label)


CID = "conv-lead-1"


def seed():
    chat_db.save_conversation(None, {
        "id": CID, "created_at": 1789000000, "source": "Widget", "status": "", "title": "",
        "message_count": 4, "user_message_count": 2, "negative_feedback": 0,
        "positive_feedback": 0, "form_submission": None,
        "messages": [{"role": "user", "text": "Need 2 bulk bags of sand, 0412 345 678",
                      "created_at": 1789000000}]})
    chat_db.upsert_lead({
        "conversation_id": CID, "run_id": None, "created_at": 1789000000,
        "lead_type": "contact_shared", "category": "product_enquiry", "topic": "Washed sand",
        "detail": "Wants 2 bulk bags delivered to Cronulla",
        "contact_name": "Jane Citizen", "contact_phone": "0412 345 678",
        "contact_email": "jane@example.com", "contact_source": "transcript+model",
        "handoff_state": "claimed_not_fired",
        "handoff_detail": "The bot said it had forwarded the details, but no action ran.",
        "claim_excerpt": "I've passed your details to our sales team.",
        "extracted": True, "extracted_message_count": 4})
    with sqlite3.connect(chat_db.DB_PATH) as conn:
        conn.execute("UPDATE leads SET alerted_at = '2026-09-20T01:00:00+00:00', "
                     "digested_at = '2026-09-20T03:00:00+00:00' WHERE conversation_id = ?", (CID,))


PROBE = f"probe_sales_{secrets.token_hex(3)}"
try:
    seed()
    auth.create_user(PROBE, secrets.token_urlsafe(16), role=auth.ROLE_SALES)
    admin_user = next(u for u in auth.list_users() if u["role"] == auth.ROLE_ADMIN and u["active"])

    def client_for(name):
        c = TestClient(dash.app)
        c.cookies.set(auth.SESSION_COOKIE, auth.create_session(name))
        return c

    def flash(response):
        fid = response.cookies.get(auth.FLASH_COOKIE)
        return (auth.take_flash(fid) if fid else None) or ("", "")

    sales, admin = client_for(PROBE), client_for(admin_user["username"])

    print("What the page shows:")
    page = sales.get(f"/chat-insights/leads/{CID}")
    check(page.status_code == 200 and "Jane Citizen" in page.text, "Sales can open a lead")
    text = page.text
    check("0412 345 678" in text and "jane@example.com" in text, "the contact details are there")
    check("The bot said it had forwarded the details" in text,
          "the hand-off reasoning is shown in full, not as a tooltip")
    check("I&#x27;ve passed your details" in text or "I've passed your details" in text,
          "with what the customer was actually told")
    check("typed into the chat" in text and "tidied by the model" in text,
          "where the contact details came from is in words")
    check(re.search(r"Alert emailed.*?20 Sep 2026", text, re.S)
          and re.search(r"Included in a lead email.*?20 Sep 2026", text, re.S),
          "and when it was alerted and emailed")
    check("AEST" in text and "1789000000" not in text, "times are Australian, not raw")
    check('name="note"' in text, "there is a note box")
    check(f'/chat-insights/conversation/{CID}' in text, "and a link to the chat")
    check(f'href="/chat-insights/leads/{CID}"' in sales.get("/chat-insights/leads").text,
          "the list links to it")

    print("\nWriting a note:")
    r = sales.post(f"/chat-insights/leads/{CID}/status",
                   data={"status": "contacted", "note": "Called 2pm, wants delivery Friday.",
                         "back": "detail"}, follow_redirects=False)
    kind, message = flash(r)
    check(r.status_code == 303 and r.headers.get("location") == f"/chat-insights/leads/{CID}",
          "saving returns to the lead", r.headers.get("location"))
    check(kind == "ok" and "Note saved" in message, "and says so", message)
    lead = chat_db.get_lead(CID)
    check(lead["note"] == "Called 2pm, wants delivery Friday." and lead["status"] == "contacted",
          "the note and the status are both stored", (lead["note"], lead["status"]))
    check(lead["owner"] == PROBE and lead["status_changed_at"],
          "with who changed it and when", (lead["owner"], lead["status_changed_at"]))
    check("Called 2pm, wants delivery Friday." in sales.get(f"/chat-insights/leads/{CID}").text,
          "and the note is shown on the page")
    check(any(e["action"] == "lead_note" for e in queue_db.list_audit(limit=20)),
          "writing a note is recorded in the activity log")

    print("\nThe list's quick status dropdown does not wipe the note:")
    r = sales.post(f"/chat-insights/leads/{CID}/status", data={"status": "won"},
                   follow_redirects=False)
    lead = chat_db.get_lead(CID)
    check(lead["status"] == "won" and lead["note"] == "Called 2pm, wants delivery Friday.",
          "status changed, note kept", (lead["status"], lead["note"]))
    check(r.headers.get("location") == "/chat-insights/leads",
          "and it returns to the list, not the lead")

    print("\nCare with what people type:")
    sales.post(f"/chat-insights/leads/{CID}/status",
               data={"status": "won", "note": "<script>alert(1)</script> ring back",
                     "back": "detail"}, follow_redirects=False)
    text = sales.get(f"/chat-insights/leads/{CID}").text
    check("<script>alert(1)</script>" not in text and "&lt;script&gt;" in text,
          "a note containing HTML is escaped")
    sales.post(f"/chat-insights/leads/{CID}/status",
               data={"status": "won", "note": "x" * 3000, "back": "detail"},
               follow_redirects=False)
    check(len(chat_db.get_lead(CID)["note"]) == 2000, "an over-long note is trimmed",
          len(chat_db.get_lead(CID)["note"]))
    r = sales.post(f"/chat-insights/leads/{CID}/status", data={"status": "sideways"},
                   follow_redirects=False)
    check(flash(r)[0] == "err" and chat_db.get_lead(CID)["status"] == "won",
          "a made-up status changes nothing")

    print("\nWithout permission to work leads:")
    original = auth.PERMISSIONS[auth.ROLE_SALES]
    auth.PERMISSIONS[auth.ROLE_SALES] = frozenset(original - {auth.PERM_WORK_LEADS})
    try:
        text = sales.get(f"/chat-insights/leads/{CID}").text
        check('name="note"' not in text and "Called 2pm" not in text.split("What we know")[0][:400],
              "the note box is not offered")
        check("Note" in text, "but the note is still readable")
        r = sales.post(f"/chat-insights/leads/{CID}/status",
                       data={"status": "lost", "note": "nope", "back": "detail"},
                       follow_redirects=False)
        kind, message = flash(r)
        check(kind == "err" and "permission" in message.lower()
              and chat_db.get_lead(CID)["status"] == "won", "and saving is refused", message)
    finally:
        auth.PERMISSIONS[auth.ROLE_SALES] = original

    print("\nA lead that is not there:")
    r = admin.get("/chat-insights/leads/no-such-conversation")
    check(r.status_code == 404 and "No such lead" in r.text, "says so rather than erroring",
          r.status_code)
    check("check CRM" not in admin.get("/chat-insights/leads").text,
          "and the old unclearable 'check CRM' badge is gone")
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
print("ALL LEAD DETAIL CHECKS PASS")
