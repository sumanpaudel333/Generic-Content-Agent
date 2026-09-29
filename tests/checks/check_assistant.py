"""
The staff assistant: what it is told, what it refuses to say, and who can use it.

The model is a function that records what it was asked, so no generation
happens here and the prompt can be inspected. Odoo is a stand-in. Both
databases are throwaway files. A probe Sales account is deleted afterwards.
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
import urllib.error

ROOT = r"C:\bcsands\Generic-Content-Agent"
sys.path.insert(0, ROOT)
os.chdir(ROOT)
logging.disable(logging.CRITICAL)

TMP = tempfile.mkdtemp(prefix="assistant_test_")
from content_seo_agent import db as queue_db  # noqa: E402
queue_db.DB_PATH = os.path.join(TMP, "review_queue.db")
queue_db.init_db()
from assistant import store as assistant_store  # noqa: E402
assistant_store.DB_PATH = os.path.join(TMP, "assistant.db")
assistant_store.init_db()

from assistant import catalogue, chat, knowledge  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from dashboard import app as dash, auth  # noqa: E402

failures = []


def check(ok, label, extra=""):
    print(f"  {'OK ' if ok else 'FAIL'}  {label}{(' -- ' + str(extra)) if extra else ''}")
    if not ok:
        failures.append(label)


def add_approved(sku, title, draft):
    with sqlite3.connect(queue_db.DB_PATH) as conn:
        conn.execute(
            "INSERT INTO review_queue (product_id, title, task_type, source, parsed_output, "
            "confidence, status, created_at, reviewed_at) VALUES (?, ?, 'draft', 'claude', ?, "
            "'high', 'approved', '2026-09-20T01:00:00', '2026-09-20T02:00:00')",
            (sku, title, json.dumps(draft)))


PROMPTS = []


def fake_model(model, messages, timeout):
    PROMPTS.append({"model": model, "messages": messages, "timeout": timeout})
    return "We sell Paving Sand (SKU PS-1) for that. Ask the yard to confirm quantities."


chat._call_model = fake_model
chat.available_models = lambda: {"bcsands-content-agent:latest": "BC Sands model",
                                 "llama3.2:3b": "General model"}


class FakeOdoo:
    def __init__(self, products=None, fail=False):
        self.products, self.fail, self.env_name = products or [], fail, "staging"
        self.configured = True

    def is_configured(self):
        return self.configured

    def list_products(self, limit=None, offset=0):
        if self.fail:
            raise RuntimeError("Odoo is not answering")
        return self.products


CATALOGUE = [
    {"product_id": "PS-1", "product_title": "Paving Sand", "product_description": ""},
    {"product_id": "VM-2", "product_title": "Vegie Organic Mix", "product_description": ""},
    {"product_id": "BM-3", "product_title": "20mm Blue Metal", "product_description": ""},
    {"product_id": "FW-4", "product_title": "Ironbark Firewood", "product_description": ""},
    {"product_id": "", "product_title": "No SKU product", "product_description": ""},
]

PROBE = f"probe_sales_{secrets.token_hex(3)}"
try:
    add_approved("PS-1", "Paving Sand", {
        "overview": "A washed sand for bedding pavers.",
        "features": ["Washed and screened", "Consistent grading"],
        "applications": ["Paver bedding", "Under slabs"]})
    add_approved("VM-2", "Vegie Organic Mix", {
        "overview": "An organic blend for vegetable gardens.",
        "features": ["Composted"], "applications": ["Raised beds"]})

    print("Finding the right products:")
    check(knowledge.terms("What sand do we sell for laying pavers?") == ["sand", "sell", "laying", "paver"],
          "the question is reduced to words worth matching (plurals folded)")
    catalogue.refresh(FakeOdoo(CATALOGUE))
    check(assistant_store.catalogue_count() == 4,
          "the product list is stored, skipping products with no SKU",
          assistant_store.catalogue_count())

    found = knowledge.find("what do we sell for laying pavers?")
    check([p["sku"] for p in found["described"]] == ["PS-1"],
          "a question about pavers finds the approved paving description",
          [p["sku"] for p in found["described"]])
    found_fire = knowledge.find("do we have ironbark firewood?")
    check(not found_fire["described"] and [p["sku"] for p in found_fire["named"]] == ["FW-4"],
          "a product with no description is still offered by name and code",
          [p["sku"] for p in found_fire["named"]])
    nothing = knowledge.find("what is the wifi password")
    check(not nothing["described"] and not nothing["named"], "an unrelated question finds nothing")

    context = knowledge.context_text(found)
    check("A washed sand for bedding pavers." in context and "SKU PS-1" in context,
          "the notes carry the approved description")
    check("Vegie Organic Mix" not in context.split("OTHER PRODUCTS")[0],
          "and not descriptions of unrelated products")
    check("BC Sands" in context and "Brookvale" in context, "plus who the business is")
    check("NO PRODUCTS MATCHED" in knowledge.context_text(nothing),
          "and says plainly when nothing matched")

    print("\nWhat it is told to do:")
    PROMPTS.clear()
    result = chat.ask("what do we sell for laying pavers?", model="bcsands-content-agent:latest")
    system = PROMPTS[0]["messages"][0]["content"]
    asked = PROMPTS[0]["messages"][-1]["content"]
    check("Answer ONLY from the notes provided" in system, "answer only from the notes")
    check(all(rule in system for rule in ("a price", "in stock", "delivery", "standard")),
          "and never price, stock, delivery or standards")
    check("A washed sand for bedding pavers." in asked and asked.endswith("laying pavers?"),
          "the notes and the question go together in one message")
    check(result["answer"].startswith("We sell Paving Sand") and not result["flagged"],
          "a clean answer comes back unflagged", result["flagged"])
    check([s["sku"] for s in result["sources"]] == ["PS-1"],
          "with the product it was built from", result["sources"])

    print("\nWhat it refuses to let through:")
    for text, expected in [("It is $45 per tonne.", "a price"),
                           ("We have 12 in stock at Brookvale.", "stock"),
                           ("Delivery is free to Cronulla.", "delivery cost"),
                           ("This complies with AS 3700.", "a standards claim"),
                           # Exactly what the fine-tuned model produced in the
                           # first real trial, spaceless code and all.
                           ("Use as a top dressing for Australian Standard AS4419 soils.",
                            "a standards claim"),
                           ("Meets AS/NZS 4454 for composts.", "a standards claim"),
                           ("It suits paver bedding. Ask the yard for quantities.", "")]:
        flagged = chat.check_answer(text)
        ok = (expected in flagged) if expected else (flagged == "")
        check(ok, f"{'flags' if expected else 'allows'}: {text}", flagged or "(clean)")
    chat._call_model = lambda m, msgs, t: "It is $45 per tonne and in stock."
    flagged_result = chat.ask("how much is paving sand?")
    check("a price" in flagged_result["flagged"] and "stock" in flagged_result["flagged"],
          "an answer that slips past the prompt is caught and marked",
          flagged_result["flagged"])
    chat._call_model = lambda m, msgs, t: "The wifi password is hunter2."
    off_topic = chat.ask("what is the wifi password")
    check("not based on anything in the catalogue" in off_topic["flagged"],
          "an answer with no matching product is marked as unsupported", off_topic["flagged"])

    print("\nWhen the model is not there:")
    def dead(model, messages, timeout):
        raise urllib.error.URLError("connection refused")
    chat._call_model = dead
    broken = chat.ask("what do we sell for laying pavers?")
    check(not broken["answer"] and "did not answer" in broken["error"],
          "a model that is down is reported, not raised", broken["error"])
    chat._call_model = fake_model

    print("\nThe product list:")
    result = catalogue.refresh(FakeOdoo(CATALOGUE, fail=True))
    check(not result["ok"] and assistant_store.catalogue_count() == 4,
          "a failed refresh keeps the list it had", result["detail"])
    unconfigured = FakeOdoo(); unconfigured.configured = False
    result = catalogue.refresh(unconfigured)
    check(not result["ok"] and "not set up" in result["detail"], "and says when Odoo is not set up")

    print("\nThe page:")
    auth.create_user(PROBE, secrets.token_urlsafe(16), role=auth.ROLE_SALES)
    admin_user = next(u for u in auth.list_users() if u["role"] == auth.ROLE_ADMIN and u["active"])
    reviewer_user = next(u for u in auth.list_users() if u["role"] == auth.ROLE_REVIEWER and u["active"])

    def client_for(name):
        c = TestClient(dash.app)
        c.cookies.set(auth.SESSION_COOKIE, auth.create_session(name))
        return c

    def flash(response):
        fid = response.cookies.get(auth.FLASH_COOKIE)
        return (auth.take_flash(fid) if fid else None) or ("", "")

    admin, reviewer, sales = (client_for(admin_user["username"]),
                              client_for(reviewer_user["username"]), client_for(PROBE))
    page = admin.get("/assistant")
    check(page.status_code == 200 and "Ask about our products" in page.text, "an admin can open it")
    check(re.search(r"cannot tell you prices, stock or delivery\s+costs", page.text),
          "the page says what it will not answer")
    check("4 products, read from Odoo" in page.text or "product list is from" in page.text,
          "and how fresh the product list is")
    for who, client in (("a reviewer", reviewer), ("Sales", sales)):
        r = client.get("/assistant", follow_redirects=False)
        check(r.status_code == 303 and r.headers.get("location") == "/", f"{who} cannot open it")
        check('href="/assistant"' not in client.get("/").text, f"and {who} has no link to it")
    check('href="/assistant"' in admin.get("/").text, "an admin does have the link")

    print("\nThe chat page:")
    check('class="chat-thread"' in page.text and 'id="composer"' in page.text,
          "it is a thread with a composer, not a form and a list")
    check('data-ask="What do we sell for laying pavers?"' in page.text,
          "with example questions to start from")
    check("Enter to send" in page.text, "and says Enter sends")

    print("\nAsking the way the page does (JSON):")
    PROMPTS.clear()
    r = admin.post("/api/assistant/ask", json={"question": "what do we sell for laying pavers?",
                                               "model": "bcsands-content-agent:latest",
                                               "conversation_id": 0})
    data = r.json()
    check(r.status_code == 200 and data["answer"].startswith("We sell Paving Sand"),
          "the answer comes back as JSON for the page to show", r.status_code)
    check(data["conversation_id"] and data["message_id"], "with ids to keep the thread going")
    check(data["sources"] and data["sources"][0]["url"].startswith("/content-agent/history?q="),
          "and a link for each description it used", data["sources"][:1])
    api_conv, api_message = data["conversation_id"], data["message_id"]

    thread_page = admin.get(f"/assistant?c={api_conv}").text
    check('class="chat-row me"' in thread_page and 'class="chat-row bot"' in thread_page,
          "a reloaded thread shows the same bubbles")

    r = admin.post("/api/assistant/rate", json={"message_id": api_message, "rating": "bad"})
    check(r.status_code == 200 and assistant_store.ratings()["bad"] == 1,
          "an answer can be marked wrong without a page reload", r.status_code)
    check(admin.post("/api/assistant/rate",
                     json={"message_id": api_message, "rating": "maybe"}).status_code == 400,
          "a made-up rating is refused")
    check(admin.post("/api/assistant/rate",
                     json={"message_id": 999999, "rating": "good"}).status_code == 404,
          "and rating something that is gone says so")

    r = sales.post("/api/assistant/ask", json={"question": "hello", "conversation_id": 0})
    check(r.status_code == 403 and "error" in r.json(),
          "Sales gets a JSON refusal, not a redirect the page cannot follow", r.status_code)
    r = TestClient(dash.app).post("/api/assistant/ask", json={"question": "hi"})
    check(r.status_code == 401, "and signed out gets 401", r.status_code)

    print("\nAsking through the plain form (no JavaScript):")
    PROMPTS.clear()
    r = admin.post("/assistant/ask", data={"question": "what do we sell for laying pavers?",
                                           "model": "bcsands-content-agent:latest",
                                           "conversation_id": "0"}, follow_redirects=False)
    thread = r.headers.get("location", "")
    check(r.status_code == 303 and thread.startswith("/assistant?c="), "the answer opens a thread",
          thread)
    page = admin.get(thread).text
    check("We sell Paving Sand" in page, "the answer is shown")
    check("Paving Sand (PS-1)" in page and "/content-agent/history?q=" in page,
          "with a link to the description it used")
    check("what do we sell for laying pavers?" in page, "and the question above it")
    conv_id = int(thread.split("=")[-1])
    messages = assistant_store.messages(conv_id)
    check([m["role"] for m in messages] == ["person", "assistant"], "both are stored")

    r = admin.post("/assistant/ask", data={"question": "and what about under slabs?",
                                           "model": "bcsands-content-agent:latest",
                                           "conversation_id": str(conv_id)}, follow_redirects=False)
    check(len(assistant_store.messages(conv_id)) == 4, "a follow-up continues the same thread")
    check(any(m["role"] == "assistant" for m in PROMPTS[-1]["messages"][1:-1]),
          "and the earlier answer is given back as context",
          [m["role"] for m in PROMPTS[-1]["messages"]])

    answer_id = [m["id"] for m in assistant_store.messages(conv_id) if m["role"] == "assistant"][0]
    r = admin.post(f"/assistant/rate/{answer_id}", data={"rating": "good"}, follow_redirects=False)
    kind, message = flash(r)
    check(kind == "ok" and assistant_store.ratings()["good"] == 1, "an answer can be marked useful",
          message)
    r = admin.post(f"/assistant/rate/{answer_id}", data={"rating": "sideways"},
                   follow_redirects=False)
    check(flash(r)[0] == "err", "and a made-up rating is refused")
    block = re.search(rf'data-rate-for="{answer_id}".*?</div>', admin.get(thread).text, re.S)
    good_button = re.search(r'<button[^>]*data-rate="good"[^>]*>', block.group(0) if block else "")
    bad_button = re.search(r'<button[^>]*data-rate="bad"[^>]*>', block.group(0) if block else "")
    check(good_button and 'aria-pressed="true"' in good_button.group(0)
          and bad_button and 'aria-pressed="false"' in bad_button.group(0),
          "the rating shows on the answer when the thread is reloaded",
          good_button and good_button.group(0)[-40:])

    print("\nWhat Sales and reviewers cannot do:")
    before = assistant_store.ratings()
    for path, data in (("/assistant/ask", {"question": "hello", "conversation_id": "0"}),
                       (f"/assistant/rate/{answer_id}", {"rating": "bad"}),
                       ("/assistant/refresh-catalogue", {})):
        r = sales.post(path, data=data, follow_redirects=False)
        check(r.status_code == 303 and r.headers.get("location") == "/", f"Sales cannot post {path}")
    check(assistant_store.ratings() == before, "and nothing of theirs was recorded")

    print("\nRefreshing the list from the page:")
    dash.assistant_catalogue.refresh = lambda connector=None: {
        "ok": True, "count": 4, "detail": "4 product(s) read from Odoo (staging)."}
    r = admin.post("/assistant/refresh-catalogue", follow_redirects=False)
    kind, message = flash(r)
    check(kind == "ok" and "4 product(s)" in message, "an admin can refresh it", message)
    check(any(e["action"] == "assistant_catalogue" for e in queue_db.list_audit(limit=20)),
          "and it is recorded in the activity log")
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
print("ALL ASSISTANT CHECKS PASS")
