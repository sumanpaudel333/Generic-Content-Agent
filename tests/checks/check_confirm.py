"""Renders every dashboard page and checks each action carries a confirmation."""
import re
import sys

sys.path.insert(0, r"C:\bcsands\Generic-Content-Agent")

from fastapi.testclient import TestClient
from dashboard import app as dash, auth

client = TestClient(dash.app)

admin = next((u["username"] for u in auth.list_users() if u["role"] == "admin"), None)
print("admin user:", admin)

# Sign in the way the app does, without needing the password.
token = auth.create_session(admin)
client.cookies.set(auth.SESSION_COOKIE, token)

PAGES = [
    "/", "/content-agent", "/content-agent/needs-retry", "/content-agent/rejected",
    "/content-agent/history", "/chat-insights", "/chat-insights/leads",
    "/settings/users", "/settings/jobs", "/settings/audit", "/settings/images",
    "/site-health", "/assistant",
]

# Every button that submits or posts, matched loosely so a new one cannot hide.
BUTTON = re.compile(r"<button\b[^>]*>", re.S)

failures = []
totals = {"pages": 0, "confirmed": 0}

for path in PAGES:
    r = client.get(path)
    if r.status_code != 200:
        failures.append(f"{path}: HTTP {r.status_code}")
        continue
    html = r.text
    totals["pages"] += 1

    # Filter and search bars submit GET and change nothing, so they are not
    # actions. Drop them wholesale rather than listing their button labels --
    # a new filter must not need this check updating.
    html_actions = re.sub(r'<form[^>]*method="get"[^>]*>.*?</form>', "", html, flags=re.S)

    # The dialog markup must be on the page, or nothing can ask anything.
    if 'id="confirm-modal"' not in html:
        failures.append(f"{path}: no confirm dialog markup")

    # No native confirm() left anywhere.
    if re.search(r"onsubmit=\"return confirm\(", html) or "confirmBulk" in html:
        failures.append(f"{path}: still uses the old native confirm()")

    for tag in BUTTON.findall(html_actions):
        if 'type="submit"' not in tag and "onclick=" not in tag:
            continue                                  # not an action
        if any(k in tag for k in ("confirm-yes", "confirm-no", 'id="confirm')):
            continue                                  # the dialog's own buttons
        if any(k in tag for k in ("toggleAll(", "toggleEdit(", "addSection(", "saveEdit(",
                                   "this.parentElement.remove()", "removeSection(",
                                   "cancelEdit(", "imgDelete(", "imgPrimary(",
                                   "toggleNav(")):
            continue                                  # in-page only, or asks in JS
        if "data-confirm=" in tag:
            totals["confirmed"] += 1
            continue
        # Read-only actions say so in the markup, with the reason. Asking the
        # assistant a question, or marking its answer useful, changes nothing
        # that needs guarding -- and a dialog in front of every question would
        # teach people to click through dialogs.
        if "data-no-confirm=" in tag:
            totals["read_only"] = totals.get("read_only", 0) + 1
            continue
        failures.append(f"{path}: unconfirmed action -> {tag[:130]}")

print(f"\npages rendered: {totals['pages']}/{len(PAGES)}")
print(f"confirmed actions found: {totals['confirmed']}")
print(f"read-only buttons allowed through: {totals.get('read_only', 0)}")
if failures:
    print("\nFAILURES:")
    for f in failures:
        print(" -", f)
    sys.exit(1)
print("\nALL ACTIONS CONFIRMED")
