"""
Sending approved descriptions to Odoo in bulk.

Odoo is a stand-in that records what it was asked to write, so nothing reaches
a real shop. The review queue (and its activity log) is a throwaway database.
Signs in as an existing admin and reviewer without changing them, and uses a
probe Sales account deleted afterwards.
"""
import html as html_module
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

TMP = tempfile.mkdtemp(prefix="bulk_publish_test_")
from content_seo_agent import db as queue_db  # noqa: E402
queue_db.DB_PATH = os.path.join(TMP, "review_queue.db")
queue_db.init_db()

from fastapi.testclient import TestClient  # noqa: E402
from content_seo_agent import product_images  # noqa: E402
from dashboard import app as dash, auth  # noqa: E402

failures = []


def check(ok, label, extra=""):
    print(f"  {'OK ' if ok else 'FAIL'}  {label}{(' -- ' + str(extra)) if extra else ''}")
    if not ok:
        failures.append(label)


DRAFT = {"overview": "A washed river sand for paving.", "features": ["Screened"],
         "applications": ["Paver bedding"]}


def add_row(product_id, title, status, *, published=0, parsed=DRAFT):
    with sqlite3.connect(queue_db.DB_PATH) as conn:
        cur = conn.execute(
            "INSERT INTO review_queue (product_id, title, task_type, source, parsed_output, "
            "confidence, status, created_at, reviewed_at, published, assembled_html) "
            "VALUES (?, ?, 'draft', 'claude', ?, 'high', ?, '2026-09-20T01:00:00', "
            "'2026-09-20T02:00:00', ?, ?)",
            (product_id, title, json.dumps(parsed) if parsed is not None else None, status,
             published, "<p>old</p>" if published else None))
        return cur.lastrowid


class FakeOdoo:
    """Records writes. Fails whichever SKUs are listed in `fail`."""

    def __init__(self):
        self.writes, self.fail, self.images = [], set(), []
        self.env_name, self.db, self.url = "staging", "bcsands-staging-123", "https://staging.example.odoo.com"
        # The page's "settings changed, restart me" banner reads these too.
        self.username, self.api_key = "probe@example.com", "not-a-real-key"

    def is_configured(self):
        return True

    def supports_images(self):
        return True

    def write_product_description(self, product_id, html_description):
        self.writes.append((str(product_id), html_description))
        if str(product_id) in self.fail:
            return {"success": False, "dry_run": False,
                    "detail": f"Odoo (staging) write failed: no product found with "
                              f"Internal Reference (SKU) '{product_id}'"}
        return {"success": True, "dry_run": False, "detail": f"Written to Odoo sku={product_id}"}


fake = FakeOdoo()
dash.odoo_connector = fake
dash.PUBLISH_PAUSE_SECONDS = 0
image_calls = []


class FakeImages:
    def publish_for_row(self, row, connector):
        image_calls.append(row["id"])
        return {"attempted": 0, "published": 0, "failed": 0, "detail": "", "skipped": True}


dash.product_images = FakeImages()


def flash(response):
    fid = response.cookies.get(auth.FLASH_COOKIE)
    return (auth.take_flash(fid) if fid else None) or ("", "")


def client_for(name):
    c = TestClient(dash.app)
    c.cookies.set(auth.SESSION_COOKIE, auth.create_session(name))
    return c


def reset_state():
    fake.writes.clear()
    image_calls.clear()
    dash._publish_state.update(running=False, started_at=None, last_result=None, total=0, done=0)


PROBE = f"probe_sales_{secrets.token_hex(3)}"
try:
    sent_rows = [add_row("SKU-100", "River Sand", "approved", published=1),
                 add_row("SKU-101", "Brickies Sand", "approved", published=1)]
    new_rows = [add_row("SKU-200", "Garden Mix", "approved"),
                add_row("SKU-201", "Blue Metal", "approved")]
    pending_row = add_row("SKU-300", "Pending Mulch", "pending")
    rejected_row = add_row("SKU-400", "Rejected Loam", "rejected")
    empty_row = add_row("SKU-500", "Approved but empty", "approved", parsed=None)

    auth.create_user(PROBE, secrets.token_urlsafe(16), role=auth.ROLE_SALES)
    admin_user = next(u for u in auth.list_users() if u["role"] == auth.ROLE_ADMIN and u["active"])
    reviewer_user = next(u for u in auth.list_users() if u["role"] == auth.ROLE_REVIEWER and u["active"])
    admin, reviewer, sales = (client_for(admin_user["username"]),
                              client_for(reviewer_user["username"]), client_for(PROBE))

    print("The Done tab:")
    page = admin.get("/content-agent/history").text
    check('action="/content-agent/bulk/publish"' in page, "an admin gets the send-to-Odoo bar")
    check("staging" in page and "bcsands-staging-123" in page,
          "which names the Odoo it writes to, so live and staging cannot be confused")
    check("Send all 5 (re-sends)" in page and "Send the 3 never sent" in page,
          "with counts for all approved and for the ones never sent")
    dialog = re.search(r'data-confirm-body="([^"]*Writes to Odoo[^"]*)"', page)
    check(dialog and "<b>" not in dialog.group(1) and "&middot;" not in dialog.group(1)
          and "staging · bcsands-staging-123" in html_module.unescape(dialog.group(1)),
          "the confirmation names the Odoo in plain words (the dialog shows text, not markup)",
          dialog and dialog.group(1)[:80])
    check(page.count('form="publish-form"') >= 5, "and a tick box on each approved description",
          page.count('form="publish-form"'))
    check("Rejected Loam" in page and page.index("Rejected Loam") > 0
          and 'value="{}"'.format(rejected_row) not in page,
          "but not on turned-down ones")
    check('action="/content-agent/bulk/publish"' not in reviewer.get("/content-agent/history").text,
          "a reviewer does not get it")

    print("\nSending the ticked ones:")
    reset_state()
    r = admin.post("/content-agent/bulk/publish",
                   data={"id": [str(new_rows[0]), str(pending_row), str(rejected_row)],
                         "scope": "selected"}, follow_redirects=False)
    kind, message = flash(r)
    check(kind == "ok" and "1 description(s)" in message, "only the approved one is sent", message)
    check([sku for sku, _ in fake.writes] == ["SKU-200"], "just that product reached Odoo",
          fake.writes and [s for s, _ in fake.writes])
    check(queue_db.get_row(new_rows[0])["published"] == 1, "and it is marked as sent")
    body = fake.writes[0][1] if fake.writes else ""
    check("A washed river sand for paving." in body and "<ul>" in body,
          "with the approved description, assembled as the product page gets it")

    print("\nSending everything never sent:")
    reset_state()
    admin.post("/content-agent/bulk/publish", data={"scope": "unpublished"}, follow_redirects=False)
    check(sorted(sku for sku, _ in fake.writes) == ["SKU-201"],
          "only the one still waiting is sent", [s for s, _ in fake.writes])
    check(dash._publish_state["last_result"]["skipped"] == 1,
          "the approved row with nothing written is skipped, not invented",
          dash._publish_state["last_result"])

    print("\nRe-sending everything (the staging-to-live case):")
    reset_state()
    r = admin.post("/content-agent/bulk/publish", data={"scope": "all"}, follow_redirects=False)
    check(sorted(sku for sku, _ in fake.writes) == ["SKU-100", "SKU-101", "SKU-200", "SKU-201"],
          "every approved description goes, including ones sent before",
          sorted(s for s, _ in fake.writes))
    resent = dict(fake.writes).get("SKU-100", "")
    check("A washed river sand for paving." in resent and "old" not in resent,
          "a re-send writes the current approved version over what was there")
    check(len(image_calls) == 4 and all(isinstance(i, int) for i in image_calls),
          "images are offered for each, and the image step skips ones already uploaded",
          len(image_calls))
    result = dash._publish_state["last_result"]
    check(result["sent"] == 4 and result["failed"] == 0, "the run reports what it did", result)

    print("\nWhen the target is wrong:")
    reset_state()
    fake.fail = {"SKU-100", "SKU-101", "SKU-200", "SKU-201"}
    for i in range(6):
        add_row(f"SKU-9{i:02d}", f"Extra {i}", "approved")
    fake.fail |= {f"SKU-9{i:02d}" for i in range(6)}
    admin.post("/content-agent/bulk/publish", data={"scope": "all"}, follow_redirects=False)
    result = dash._publish_state["last_result"]
    check(len(fake.writes) == dash.PUBLISH_ABORT_AFTER,
          f"it stops after {dash.PUBLISH_ABORT_AFTER} failures in a row instead of grinding on",
          len(fake.writes))
    check("Stopped after" in (result.get("aborted") or "") and "which Odoo" in result["aborted"],
          "and says to check which Odoo it is pointed at", result.get("aborted"))
    check(queue_db.count_rows(status=dash.Status.APPROVED, published=False) > 0,
          "the ones it could not send stay unsent, ready to retry")
    page = admin.get("/content-agent/history").text
    check("Stopped after" in page, "the page shows that too")
    fake.fail.clear()

    print("\nWhat stops it running:")
    reset_state()
    for who, client in (("a reviewer", reviewer), ("Sales", sales)):
        r = client.post("/content-agent/bulk/publish", data={"scope": "all"}, follow_redirects=False)
        check(r.status_code == 303 and not fake.writes, f"{who} cannot send anything to Odoo")
    r = admin.post("/content-agent/bulk/publish", data={"scope": "everything"}, follow_redirects=False)
    check(flash(r)[0] == "err" and not fake.writes, "an unknown choice sends nothing")
    dash._publish_state["running"] = True
    r = admin.post("/content-agent/bulk/publish", data={"scope": "all"}, follow_redirects=False)
    check(flash(r)[0] == "err" and not fake.writes, "and a second run cannot start while one is going")
    dash._publish_state["running"] = False
    fake.is_configured = lambda: False
    r = admin.post("/content-agent/bulk/publish", data={"scope": "all"}, follow_redirects=False)
    kind, message = flash(r)
    check(kind == "err" and "not set up" in message and not fake.writes,
          "with Odoo not set up, nothing is attempted", message)
    fake.is_configured = lambda: True

    print("\nIn the activity log:")
    actions = [e["action"] for e in queue_db.list_audit(limit=200)]
    check("bulk_published" in actions and actions.count("published") >= 5,
          "the run and each product are recorded", {a: actions.count(a) for a in set(actions)})
    entry = next(e for e in queue_db.list_audit(limit=200) if e["action"] == "published")
    check("Bulk send to Odoo (staging" in (entry.get("detail") or ""),
          "naming the Odoo each one went to", entry.get("detail"))

    print("\nImages really are skipped on a re-send:")
    row_id = new_rows[0]
    with sqlite3.connect(queue_db.DB_PATH) as conn:
        conn.execute("INSERT INTO product_images (row_id, product_id, original_name, stored_name, "
                     "content_type, byte_size, checksum, position, published, uploaded_at) "
                     "VALUES (?, 'SKU-200', 'a.png', 'a.png', 'image/png', 10, 'abc123', 0, 1, "
                     "'2026-09-20T01:00:00')", (row_id,))
    summary = product_images.publish_for_row(queue_db.get_row(row_id), fake)
    check(summary["skipped"] and not fake.images,
          "an image already uploaded is not uploaded again", summary)
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
print("ALL BULK PUBLISH CHECKS PASS")
