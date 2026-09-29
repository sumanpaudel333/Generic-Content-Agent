"""
Searching the review queue by SKU, product name or row id.

Throwaway review queue; signs in as an existing admin without changing them.
"""
import json
import logging
import os
import re
import shutil
import sqlite3
import sys
import tempfile

ROOT = r"C:\bcsands\Generic-Content-Agent"
sys.path.insert(0, ROOT)
os.chdir(ROOT)
logging.disable(logging.CRITICAL)

TMP = tempfile.mkdtemp(prefix="queue_search_test_")
from content_seo_agent import db as queue_db  # noqa: E402
queue_db.DB_PATH = os.path.join(TMP, "review_queue.db")
queue_db.init_db()

from fastapi.testclient import TestClient  # noqa: E402
from dashboard import app as dash, auth  # noqa: E402

failures = []


def check(ok, label, extra=""):
    print(f"  {'OK ' if ok else 'FAIL'}  {label}{(' -- ' + str(extra)) if extra else ''}")
    if not ok:
        failures.append(label)


DRAFT = {"overview": "A description.", "features": ["One"], "applications": ["Two"]}


def add(sku, title, status="pending", published=0):
    with sqlite3.connect(queue_db.DB_PATH) as conn:
        cur = conn.execute(
            "INSERT INTO review_queue (product_id, title, task_type, source, parsed_output, "
            "confidence, status, created_at, reviewed_at, published) VALUES (?, ?, 'draft', "
            "'claude', ?, 'high', ?, '2026-09-20T01:00:00', '2026-09-20T02:00:00', ?)",
            (sku, title, json.dumps(DRAFT), status, published))
        return cur.lastrowid


try:
    river = add("BCS-100", "Washed River Sand")
    brickies = add("BCS-101", "Brickies Sand")
    mulch = add("BCS-200", "Forest Blend Mulch")
    blend = add("SAND_50", "50% Sand Blend")          # literal _ and % in the data
    approved = add("BCS-300", "Paving Sand", status="approved", published=1)
    rejected = add("BCS-400", "Rejected Sand", status="rejected")

    print("Matching:")
    titles = lambda rows: sorted(r["title"] for r in rows)
    check(titles(queue_db.list_rows(q="sand")) == ["50% Sand Blend", "Brickies Sand",
                                                   "Paving Sand", "Rejected Sand",
                                                   "Washed River Sand"],
          "part of a product name matches, whatever the status",
          titles(queue_db.list_rows(q="sand")))
    check(titles(queue_db.list_rows(q="BCS-10")) == ["Brickies Sand", "Washed River Sand"],
          "part of a SKU matches", titles(queue_db.list_rows(q="BCS-10")))
    check(titles(queue_db.list_rows(q="bcs-100")) == ["Washed River Sand"],
          "matching ignores case")
    # A digit also matches any SKU containing it, which is right for a search
    # box; what matters is that the row with that id is always in the results.
    check(mulch in [r["id"] for r in queue_db.list_rows(q=str(mulch))],
          "a number matches the queue row id -- what the activity log links with",
          [r["id"] for r in queue_db.list_rows(q=str(mulch))])
    check([r["id"] for r in queue_db.list_rows(q=str(rejected))] == [rejected],
          "and on its own when no SKU contains that digit",
          [r["id"] for r in queue_db.list_rows(q=str(rejected))])
    check(queue_db.list_rows(q="nothing here") == [], "a miss returns nothing")

    print("\nCharacters that mean something to SQL:")
    check(titles(queue_db.list_rows(q="%")) == ["50% Sand Blend"],
          "a literal % matches only the product with one in its name",
          titles(queue_db.list_rows(q="%")))
    check(titles(queue_db.list_rows(q="_")) == ["50% Sand Blend"],
          "a literal _ matches only the SKU with one, not every row",
          titles(queue_db.list_rows(q="_")))
    check(queue_db.list_rows(q="\\") == [], "a backslash matches nothing and does not error")
    check(titles(queue_db.list_rows(q="SAND_5")) == ["50% Sand Blend"], "and _ inside a SKU works")

    print("\nWith the filters and the counts:")
    check(titles(queue_db.list_rows(status="pending", q="sand"))
          == ["50% Sand Blend", "Brickies Sand", "Washed River Sand"],
          "search and status combine")
    check(queue_db.count_rows(status="pending", q="sand") == 3
          and queue_db.count_rows(q="sand") == 5,
          "the count agrees with the rows returned",
          (queue_db.count_rows(status="pending", q="sand"), queue_db.count_rows(q="sand")))
    check(queue_db.count_rows(status="approved", published=False, q="sand") == 0
          and queue_db.count_rows(status="approved", published=True, q="sand") == 1,
          "and still respects published")
    check(len(queue_db.list_rows(q="sand", limit=2)) == 2
          and len(queue_db.list_rows(q="sand", limit=2, offset=4)) == 1,
          "paging a search works")

    print("\nOn the pages:")
    admin_user = next(u for u in auth.list_users() if u["role"] == auth.ROLE_ADMIN and u["active"])
    admin = TestClient(dash.app)
    admin.cookies.set(auth.SESSION_COOKIE, auth.create_session(admin_user["username"]))

    page = admin.get("/content-agent?q=brickies").text
    check("Brickies Sand" in page and "Washed River Sand" not in page,
          "the queue shows only what matched")
    check('name="q" value="brickies"' in page, "the box keeps what was typed")
    check("Clear filters" in page, "and offers to clear it")
    for path, expected in (("/content-agent/needs-retry?q=sand", "no"),
                           ("/content-agent/rejected?q=rejected", "Rejected Sand"),
                           ("/content-agent/history?q=paving", "Paving Sand")):
        text = admin.get(path).text
        ok = (expected in text) if expected != "no" else ("Nothing matching" in text)
        check(ok, f"{path.split('?')[0]} searches too")

    page = admin.get(f"/content-agent/history?q={approved}").text
    check("Paving Sand" in page and "Rejected Sand" not in page,
          "the activity log's link by row id lands on that one row")

    page = admin.get("/content-agent?q=zzz-not-a-product").text
    check("Nothing matching" in page and "zzz-not-a-product" in page,
          "a search with no matches names what was searched for")

    page = admin.get('/content-agent?q=%3E%3Cscript%3Ealert(1)%3C/script%3E').text
    check("<script>alert(1)</script>" not in page and "&lt;script&gt;" in page,
          "a search term with HTML in it is escaped everywhere it is shown")

    for i in range(30):
        add(f"BULK-{i:03d}", f"Bulk Bag Sand {i}")
    page = admin.get("/content-agent?q=bulk+bag").text
    check("q=bulk+bag" in page or "q=bulk%20bag" in page,
          "the search survives into the paging links",
          re.findall(r'href="[^"]*page=2[^"]*"', page)[:1])
    second = admin.get("/content-agent?q=bulk+bag&page=2").text
    check("Bulk Bag Sand" in second and "Washed River Sand" not in second,
          "and page two still shows only what matched")
    check(queue_db.count_rows(status="pending", q="bulk bag") == 30,
          "the count behind the paging is the search's own count",
          queue_db.count_rows(status="pending", q="bulk bag"))
finally:
    shutil.rmtree(TMP, ignore_errors=True)

print()
if failures:
    print("FAILURES:")
    for f in failures:
        print(" -", f)
    sys.exit(1)
print("ALL QUEUE SEARCH CHECKS PASS")
