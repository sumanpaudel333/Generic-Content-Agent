"""
Content and Automation Dashboard.

Run with:
    uvicorn dashboard.app:app --host 0.0.0.0 --port 8420 --reload

Then visit http://localhost:8420 (or the server's IP:8420 from another
machine on the network, if the firewall allows it).

Structure: a shell with top-level automation modules (see MODULES in
dashboard/ui.py). "Content Agent" is the first one -- the human review
checkpoint between model-generated product copy and Odoo. Adding another
automation means registering it in ui.MODULES and adding its routes here;
nav, theming, and auth are handled by the shell.

Every route except /login and /healthz requires an authenticated session
(see dashboard/auth.py and the require_auth middleware below).

No agent in this system ever writes to Odoo directly -- a person approves
each draft. Whether "Approve" also publishes immediately is controlled by
config.yaml's approval_flow.auto_publish_on_approve.
"""
import html
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import dotenv_values, load_dotenv
load_dotenv()

from fastapi import BackgroundTasks, FastAPI, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from content_seo_agent import (review_queue, small_model_client, assembler, pipeline,
                                 content_status as cs, product_images)
from content_seo_agent.assembler import assemble_html
from content_seo_agent.batch_runner import process_dataframe
from content_seo_agent.demand_link import demand_terms
from content_seo_agent.constants import Status, TaskType, Source
from content_seo_agent.daily_run import get_current_products
from connectors.odoo_connector import odoo_connector  # must import after load_dotenv()
from connectors.odoo_connector import _derive_db_name, summarise_error
from chat_insights import (alerts as chat_alerts, daily_leads as chat_daily,
                            db as chat_db, leads as chat_leads, mailer,
                            weekly_run as chat_run)
from dashboard import job_logs
from config import settings
from fastapi.staticfiles import StaticFiles
from dashboard import auth, ui, user_mail
from site_monitor import emails as monitor_emails, run as monitor_run, store as monitor_store

logger = logging.getLogger("dashboard")
app = FastAPI(title="Content and Automation Dashboard")

# Brand assets (the logo, the favicon). Served from the repo's assets/ folder
# rather than copied into the package, so there is one copy of the logo.
ASSETS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets")
if os.path.isdir(ASSETS_DIR):
    app.mount("/assets", StaticFiles(directory=ASSETS_DIR), name="assets")

CONTENT_AGENT = "/content-agent"
CHAT_INSIGHTS = "/chat-insights"
PUBLIC_PATHS = {"/login", "/healthz", "/forgot-password", "/set-password"}
# The login page carries the logo, and the browser asks for the favicon before
# anyone has signed in -- gating brand assets behind auth would leave the sign-in
# page unbranded and log a 303 for every image request.
PUBLIC_PREFIXES = ("/assets/",)

# In-process state for the manual "Fetch New Products" trigger. A single
# dashboard process is the deployment model here (README: "single-server
# Windows deployment"), so a plain in-memory flag is enough -- no need for
# a filesystem lock or external job queue. Resets on server restart, which
# is fine: it only ever reflects "is a fetch running right now."
_fetch_state: dict[str, Any] = {"running": False, "started_at": None, "last_result": None}

# Same idea for regenerating rejected drafts -- it re-runs the model per
# product (~20s each), so it can't happen inside the request.
_regen_state: dict[str, Any] = {"running": False, "started_at": None, "last_result": None,
                                 "total": 0, "done": 0}

# And for the weekly chat analysis, which is the slowest job of the three --
# one model call per conversation on a CPU-only host.
# The morning lead job, when a person starts it from the dashboard. Separate
# from _chat_state: this run takes seconds and needs no model, so there is no
# reason one should block the other.
_leads_state: dict[str, Any] = {"running": False, "started_at": None, "last_result": None}

# After this long, a run that still says it is going is assumed to have died --
# a worker restart mid-run leaves the flag set, and this button is the one
# people reach for precisely when things have gone wrong. A stuck flag must not
# be what takes the escape hatch away.
LEAD_RUN_STALE_AFTER = timedelta(minutes=15)


def _lead_run_in_progress() -> bool:
    if not _leads_state["running"]:
        return False
    started = _leads_state.get("started_at")
    if started and datetime.now(timezone.utc) - started > LEAD_RUN_STALE_AFTER:
        logger.warning("Lead digest run looked stuck since %s; letting a new one start",
                        started)
        return False
    return True

_chat_state: dict[str, Any] = {"running": False, "started_at": None, "last_result": None,
                                "total": 0, "done": 0}


@app.on_event("startup")
def _startup():
    auth.init_db()
    auth.purge_expired_sessions()
    banner = auth.ensure_bootstrap_admin()
    if banner:
        print(banner)


def _run_fetch_job():
    try:
        df, source = get_current_products()
        results = process_dataframe(
            df,
            limit=settings.DAILY_BATCH_SIZE,
            target_statuses=cs.NEEDS_DRAFTING,
            delay=settings.DAILY_RUN_DELAY_SECONDS,
            force=False,
            priority_terms=demand_terms(),
        )
        _fetch_state["last_result"] = {"source": source, "error": None, **results}
    except Exception as e:
        _fetch_state["last_result"] = {"source": None, "error": str(e)}
    finally:
        _fetch_state["last_result"]["finished_at"] = datetime.now(timezone.utc).isoformat()
        _fetch_state["running"] = False


def _validate_regen_model(model: str) -> tuple[str, str]:
    """(model, error). Empty model means the normal escalation chain.

    Checked server-side as well as in the dropdown: the picker disables a rung
    that cannot run, but a disabled option is a hint, not a guarantee, and
    starting a background job that is certain to fail wastes the reviewer's
    time twice -- once waiting, once working out why.
    """
    model = (model or "").strip()
    if not model:
        return "", ""
    choice = next((m for m in pipeline.available_models() if m["key"] == model), None)
    if not choice:
        return "", f"Unknown model “{model}”."
    if not choice["available"]:
        return "", f"{choice['label']} cannot run right now: {choice['detail']}"
    return model, ""


def _model_suffix(model: str) -> str:
    """" with Claude" for a message, or "" when the chain decides."""
    if not model:
        return ""
    label = next((m["label"] for m in pipeline.available_models() if m["key"] == model), model)
    return f" with {label}"


def _run_regen_job(row_ids: list[int], model: str = ""):
    regenerated = empty = failed = 0
    try:
        for row_id in row_ids:
            try:
                row = pipeline.regenerate_draft_for_row(row_id, model=model)
                if row and row.get("parsed_output"):
                    regenerated += 1
                else:
                    # Came back with nothing usable -- it lands in the queue
                    # flagged and un-approvable rather than silently vanishing.
                    empty += 1
            except Exception:
                failed += 1
            _regen_state["done"] += 1
        _regen_state["last_result"] = {"regenerated": regenerated, "empty": empty,
                                        "failed": failed, "total": len(row_ids),
                                        "model": model, "error": None}
    except Exception as e:
        _regen_state["last_result"] = {"regenerated": regenerated, "empty": empty,
                                        "failed": failed, "total": len(row_ids),
                                        "model": model, "error": str(e)}
    finally:
        _regen_state["last_result"]["finished_at"] = datetime.now(timezone.utc).isoformat()
        _regen_state["running"] = False


def _run_chat_job(send_email: bool):
    def progress(done, total):
        _chat_state["done"], _chat_state["total"] = done, total
    try:
        run = chat_run.run_weekly(send_email=send_email, progress=progress)
        _chat_state["last_result"] = {
            "run_id": run.get("id"), "status": run.get("status"),
            "conversations": run.get("conversation_count", 0),
            "email_status": run.get("email_status", ""),
            "email_detail": run.get("email_detail", ""),
            "error": run.get("error"),
        }
    except Exception as e:
        _chat_state["last_result"] = {"run_id": None, "status": "failed", "error": str(e)}
    finally:
        _chat_state["last_result"]["finished_at"] = datetime.now(timezone.utc).isoformat()
        _chat_state["running"] = False


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

@app.middleware("http")
async def require_auth(request: Request, call_next):
    """Gate every route behind a valid session. HTML requests get bounced to
    the login page (remembering where they were headed); anything under
    /api gets a 401 JSON body instead of a redirect it can't follow."""
    path = request.url.path
    if path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES):
        return await call_next(request)

    user = auth.get_session_user(request.cookies.get(auth.SESSION_COOKIE))
    if not user:
        if path.startswith("/api/"):
            return JSONResponse({"error": "authentication required"}, status_code=401)
        nxt = request.url.path
        if request.url.query:
            nxt += "?" + request.url.query
        return RedirectResponse(url=f"/login?{urlencode({'next': nxt})}", status_code=303)

    request.state.user = user
    return await call_next(request)


# Declared after require_auth on purpose: Starlette runs the last-declared
# middleware outermost, so this also covers the login redirects that
# require_auth returns without ever reaching a route.
@app.middleware("http")
async def security_headers(request: Request, call_next):
    """Keeps this dashboard's addresses to itself.

    Without a policy, following a link to another site sends that site the
    full URL of the page you were on, filters and all. same-origin sends it
    only within the dashboard. Routes that need stricter can set their own --
    setdefault leaves theirs alone.
    """
    response = await call_next(request)
    response.headers.setdefault("Referrer-Policy", "same-origin")
    return response


def _current_user(request: Request) -> dict:
    return getattr(request.state, "user", None) or {}


def _actor(request: Request) -> str:
    """Username to attribute an action to. Every mutating route runs behind the
    auth middleware, so there is always a session here -- the fallback exists so
    a missing one is recorded as "unknown" rather than crashing the action."""
    return _current_user(request).get("username") or "unknown"


def _can(request: Request, permission: str) -> bool:
    """Whether the signed-in user may do that. See auth.PERMISSIONS."""
    return auth.can(_current_user(request), permission)


def _denied(request: Request, permission: str, back: str) -> RedirectResponse | None:
    """None if allowed; otherwise the redirect to send them back with.

    Every job and mail route calls this. Hiding the button is a courtesy --
    it stops a reviewer reaching for something they cannot have -- but it is
    not a permission, because the form still posts to a URL anyone signed in
    can type. This is the check that actually holds.
    """
    if _can(request, permission):
        return None
    _audit(request, "permission_denied", None,
           detail=f"Tried to {permission.replace('_', ' ')} without the permission for it.")
    return _redirect(back, err="You do not have permission to do that. "
                                "Ask an admin to run it for you.")


def _audit(request: Request, action: str, row: dict | None = None, *,
            row_id: int | None = None, detail: str = "") -> None:
    """Writes one line of the trail. Never raises: an audit write that fails
    must not take down the action it was recording -- that would turn a
    bookkeeping problem into a work-stopping one."""
    try:
        review_queue.record_audit(
            _actor(request), action,
            row_id=row_id if row_id is not None else (row or {}).get("id"),
            product_id=(row or {}).get("product_id", ""),
            title=(row or {}).get("title", ""),
            detail=detail)
    except Exception:
        logger.exception("Could not write the audit entry for %s", action)


# The longest flash message kept. Messages no longer travel in the URL (see
# _redirect), so this is about reading, not transport: a toast is one glance,
# and anything longer belongs on the item or in the log.
FLASH_MAX_CHARS = 400


def _fit_flash(text: str) -> str:
    """A message short enough to read at a glance.

    Long text is almost always a traceback somebody passed straight through,
    so it is summarised rather than cut mid-word; anything else long is
    trimmed. The full detail is already in the log and on the item itself.
    """
    text = str(text or "")
    if len(text) <= FLASH_MAX_CHARS:
        return text
    if "Traceback" in text or "Fault" in text:
        head = text.split(":", 1)[0] if ":" in text[:80] else ""
        summary = summarise_error(text)
        text = f"{head}: {summary}" if head else summary
    if len(text) > FLASH_MAX_CHARS:
        text = text[:FLASH_MAX_CHARS - 1].rstrip() + "\u2026"
    return text


def _redirect(path: str, msg: str = "", err: str = "") -> RedirectResponse:
    """Redirect to `path`, with a one-shot message for the page that follows.

    The URL is exactly `path` and nothing else. The message is stored
    server-side and the browser is given a random id for it in a cookie --
    see auth.put_flash for why. The old version put the text in the query
    string, which leaked names, email addresses and once a live sign-in link
    into history and logs, and broke the page when an Odoo traceback made the
    address longer than IIS accepts.
    """
    response = RedirectResponse(url=path, status_code=303)
    if msg or err:
        kind, text = ("err", err) if err else ("ok", msg)
        flash_id = auth.put_flash(kind, _fit_flash(text))
        response.set_cookie(auth.FLASH_COOKIE, flash_id, httponly=True, samesite="lax",
                            max_age=int(auth.FLASH_LIFETIME.total_seconds()), path="/")
    return response


def _safe_next(raw: str | None) -> str:
    """Only ever redirect to a path on this site -- never to a full URL
    supplied in the query string, which would make the login page an open
    redirect."""
    if raw and raw.startswith("/") and not raw.startswith("//"):
        return raw
    return "/"


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    if auth.get_session_user(request.cookies.get(auth.SESSION_COOKIE)):
        return RedirectResponse(url="/", status_code=303)
    flash = auth.take_flash(request.cookies.get(auth.FLASH_COOKIE))
    kind, text = flash if flash else ("", "")
    return ui.login_page(notice=text if kind == "ok" else "",
                          error=text if kind == "err" else "")


@app.post("/login")
def login_submit(request: Request, username: str = Form(...), password: str = Form(...),
                  next: str = Form(default="/")):
    user = auth.authenticate(username, password)
    if not user:
        return HTMLResponse(
            ui.login_page(error="That did not match an account. Check the username "
                                 "or email address, and the password.",
                           username=username),
            status_code=401,
        )
    token = auth.create_session(user["username"])
    response = RedirectResponse(url=_safe_next(next or request.query_params.get("next")), status_code=303)
    response.set_cookie(
        auth.SESSION_COOKIE, token,
        httponly=True,          # not readable from JS -- limits XSS damage
        samesite="lax",         # blocks cookies on cross-site POSTs (CSRF mitigation)
        max_age=int(auth.SESSION_LIFETIME.total_seconds()),
        path="/",
    )
    return response


# ---------------------------------------------------------------------------
# Getting back in without a password
#
# Both of these are reachable signed out, so both are written to give nothing
# away. Asking for a link says the same thing whether or not the address is on
# an account -- otherwise the form becomes a way to find out who works here.
# ---------------------------------------------------------------------------

_FORGOT_SENT = ("If that address belongs to an account here, a link is on its way. "
                 "It works once and lasts two hours. Check the junk folder if it "
                 "does not arrive.")


@app.get("/forgot-password", response_class=HTMLResponse)
def forgot_password_page(request: Request, sent: str = ""):
    if sent:
        return HTMLResponse(ui.signed_out_page(
            heading="Check your email", intro=_FORGOT_SENT, form_html="",
            footer_html='<div class="login-alt"><a href="/login">Back to sign in</a></div>'))
    return HTMLResponse(ui.signed_out_page(
        heading="Forgotten your password?",
        intro="Type the email address on your account and we will send you a link to set "
               "a new one.",
        form_html="""
        <form method="post" action="/forgot-password">
          <div class="field">
            <label for="email">Email address</label>
            <input id="email" name="email" type="email" autocomplete="email" autofocus required>
          </div>
          <button class="login-btn" type="submit">Send me a link</button>
        </form>""",
        footer_html='<div class="login-alt"><a href="/login">Back to sign in</a></div>'))


@app.post("/forgot-password")
def forgot_password_submit(request: Request, email: str = Form(...)):
    """Always answers the same way.

    A different message for a known address would turn this form into a way to
    check who has an account, which is worth more to somebody guessing at the
    login page than it is to us.
    """
    target = auth.user_by_email(email)
    if target and target.get("active"):
        try:
            token = auth.create_reset_token(target["username"], kind=auth.KIND_RESET)
            outcome = user_mail.send_reset(target, token)
            logger.info("Reset link for %s: %s", target["username"], outcome.get("detail"))
        except Exception:
            logger.exception("Could not send a reset link for %s", target["username"])
    else:
        logger.info("Reset asked for an address with no active account; nothing sent.")
    return _redirect("/forgot-password?sent=1")


# Long enough to type a password with a phone call in the middle; short
# enough that a borrowed computer does not hold a way into somebody's account
# for the rest of the day. The link itself lasts longer, and clicking it
# again starts a fresh hour.
RESET_COOKIE_SECONDS = 3600


def _private(response):
    """Headers for a page that handles a password.

    no-store keeps it out of the browser cache, so Back after signing out does
    not show it. no-referrer is stricter than the site-wide policy: this is
    the one page whose first address contains a secret.
    """
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


def _expired_link_page() -> HTMLResponse:
    return HTMLResponse(ui.signed_out_page(
        heading="That link has expired",
        intro=("Links can only be used once, and they do not last forever. Ask for "
               "a new one, or ask an administrator to send you one."),
        form_html="",
        footer_html=('<div class="login-alt"><a href="/forgot-password">'
                     'Send me a new link</a></div>')), status_code=400)


@app.get("/set-password", response_class=HTMLResponse)
def set_password_page(request: Request, token: str = ""):
    """Where the link in an invitation or reset email lands.

    The token has to arrive in the URL -- that is the only way an email can
    deliver it. What this does is move it straight out again: a valid token
    is put in a cookie scoped to this page and the browser is redirected to
    the bare address. So it is in the address bar for a single request, not
    in the history, not in a bookmark, not in a screenshot of the page, and
    not in the Referer of anything clicked afterwards.
    """
    if token:
        if not auth.check_reset_token(token):
            return _private(_expired_link_page())
        response = RedirectResponse(url="/set-password", status_code=303)
        response.set_cookie(auth.RESET_COOKIE, token, httponly=True, samesite="lax",
                            max_age=RESET_COOKIE_SECONDS, path="/set-password")
        return _private(response)

    pending = auth.check_reset_token(request.cookies.get(auth.RESET_COOKIE, ""))
    if not pending:
        return _private(_expired_link_page())

    flash = auth.take_flash(request.cookies.get(auth.FLASH_COOKIE))
    err = flash[1] if flash and flash[0] == "err" else ""
    invite = pending["kind"] == auth.KIND_INVITE
    return _private(HTMLResponse(ui.signed_out_page(
        heading="Choose a password" if invite else "Set a new password",
        intro=(f"Welcome. Pick a password for {pending['username']} and you are in."
                if invite else
                f"Setting a new password for {pending['username']}."),
        error=err,
        form_html=f"""
        <form method="post" action="/set-password">
          <div class="field">
            <label for="new_password">New password</label>
            <input id="new_password" name="new_password" type="password" minlength="8"
                   autocomplete="new-password" autofocus required>
          </div>
          <div class="login-rules">At least 8 characters.</div>
          <div class="field">
            <label for="confirm">Type it again</label>
            <input id="confirm" name="confirm" type="password" minlength="8"
                   autocomplete="new-password" required>
          </div>
          <button class="login-btn" type="submit">
            {"Set my password and sign in" if invite else "Set my password"}</button>
        </form>""")))


@app.post("/set-password")
def set_password_submit(request: Request, new_password: str = Form(...),
                         confirm: str = Form(default="")):
    token = request.cookies.get(auth.RESET_COOKIE, "")
    if new_password != confirm:
        return _private(_redirect("/set-password", err="Those two did not match."))
    try:
        pending = auth.consume_reset_token(token, new_password)
    except ValueError as e:
        return _private(_redirect("/set-password", err=str(e)))
    if not pending:
        return _private(_expired_link_page())

    logger.info("Password set via %s link for %s", pending["kind"], pending["username"])
    response = HTMLResponse(ui.login_page(
        username=pending["username"],
        notice="Your password is set. Sign in with it now."))
    response.delete_cookie(auth.RESET_COOKIE, path="/set-password")
    return _private(response)


@app.post("/logout")
def logout(request: Request):
    auth.revoke_session(request.cookies.get(auth.SESSION_COOKIE))
    response = _redirect("/login", msg="You have been signed out.")
    response.delete_cookie(auth.SESSION_COOKIE, path="/")
    return response


@app.get("/healthz")
def healthz():
    """Unauthenticated liveness probe -- deliberately exposes nothing but
    'the process is up'. Real stats live behind auth at /api/status."""
    return {"ok": True}


# ---------------------------------------------------------------------------
# Overview
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def overview(request: Request):
    counts = review_queue.counts_by_status()
    pending = counts.get(Status.PENDING, 0)
    approved = counts.get(Status.APPROVED, 0)
    rejected = counts.get(Status.REJECTED, 0)
    unpublished = review_queue.count_rows(status=Status.APPROVED, published=False)
    published = review_queue.count_rows(status=Status.APPROVED, published=True)

    model_up = small_model_client.is_available()
    odoo_ok = odoo_connector.is_configured()
    source_label = "live product list" if settings.USE_ODOO_AS_PRODUCT_SOURCE else "exported list"

    tiles = "".join([
        ui.stat_tile("Waiting for you", pending, variant="accent", href=CONTENT_AGENT),
        ui.stat_tile("Live on the website", published, variant="ok"),
        ui.stat_tile("Did not send", unpublished, variant="warn" if unpublished else "",
                      href=f"{CONTENT_AGENT}/needs-retry"),
        ui.stat_tile("Turned down", rejected, href=f"{CONTENT_AGENT}/rejected"),
        ui.stat_tile("Description writer",
                      f'<span class="dot {"ok" if model_up else "bad"}"></span>'
                      f'{"Working" if model_up else "Not responding"}', small=True,
                      variant="ok" if model_up else "bad"),
        ui.stat_tile("Website connection",
                      f'<span class="dot {"ok" if odoo_ok else "bad"}"></span>'
                      f'{"Connected" if odoo_ok else "Not set up"}', small=True,
                      variant="ok" if odoo_ok else "bad"),
    ])

    cards = ""
    for m in ui.MODULES:
        live = m["status"] == "live"
        stats = ""
        if m["key"] == "content-agent":
            stats = f"""
            <div class="modstat">
                <div><b>{pending}</b>To check</div>
                <div><b>{approved}</b>Approved</div>
                <div><b>{published}</b>Live</div>
            </div>"""
        elif m["key"] == "chat-insights":
            latest = chat_db.latest_run()
            if latest and latest.get("stats"):
                s = latest["stats"]
                stats = f"""
                <div class="modstat">
                    <div><b>{s.get("total_conversations", 0)}</b>Chats</div>
                    <div><b>{len(s.get("failures", []))}</b>Unanswered</div>
                    <div><b>{len(s.get("leads", []))}</b>Leads</div>
                </div>"""
            else:
                ready = sum(1 for _l, ok, _d in chat_run.status_lines() if ok)
                stats = (f'<div class="modstat"><div class="meta">No reports yet '
                          f'&middot; {ready} of 3 setup steps done</div></div>')
        elif m["key"] == "site-health":
            stats = _monitor_card_stats()
        tag = f'<span class="badge {m["status"]}">{"Live" if live else "Planned"}</span>'
        open_attr = f'href="{m["path"]}"' if live else ""
        tag_name = "a" if live else "div"
        cards += f"""
        <{tag_name} class="modcard {"" if live else "planned"}" {open_attr}>
            <div class="card-header" style="margin-bottom:6px">
                <div class="modcard-icon">{ui.icon(m.get("icon", ""), 21)}</div>{tag}
            </div>
            <h3>{html.escape(m["label"])}</h3>
            <p>{html.escape(m["blurb"])}</p>
            {stats}
        </{tag_name}>"""

    body = f"""
    <div class="page-head">
      <div>
        <h1>Welcome back</h1>
        <p class="subtitle">Everything that needs your attention, in one place.
           Products are read from the <b>{html.escape(source_label)}</b>.</p>
      </div>
    </div>
    {_restart_notice(request)}
    <div class="stats">{tiles}</div>
    <h2>Automations</h2>
    <div class="modgrid">{cards}</div>
    """
    return ui.page_shell(body, title="Overview · Content and Automation", active_module="overview",
                          user=_current_user(request), request=request,
                          fetch_running=_fetch_state["running"] or _regen_state["running"])


# ---------------------------------------------------------------------------
# Content Agent -- shared rendering helpers
# ---------------------------------------------------------------------------

def _confidence_badge(confidence: str) -> str:
    cls = "confidence-low" if confidence == "low" else "confidence-high"
    return f'<span class="badge {cls}">{html.escape(confidence or "unknown")} confidence</span>'


def _render_draft_content_html(parsed: dict) -> str:
    """Renders the draft copy (overview/features/applications) as readable
    HTML instead of a raw JSON dump, so a reviewer can judge the content
    itself rather than mentally parsing JSON."""
    if not parsed:
        return ""
    parts = []
    overview = parsed.get("overview")
    if overview:
        parts.append(f'<div class="field-label">Overview</div><p>{html.escape(str(overview))}</p>')
    for field, label in (("features", "Features"), ("applications", "Applications")):
        items = parsed.get(field)
        if items:
            lis = "".join(f"<li>{html.escape(str(i))}</li>" for i in items)
            parts.append(f'<div class="field-label">{label}</div><ul>{lis}</ul>')
    known = {"overview", "features", "applications"}
    for key, value in ((k, v) for k, v in parsed.items() if k not in known):
        parts.append(f'<div class="field-label">{html.escape(str(key))}</div><p>{html.escape(str(value))}</p>')
    return "".join(parts)


def _draft_preview_html(parsed: dict) -> str:
    content = _render_draft_content_html(parsed)
    preview = (f'<div class="preview">{content}</div>' if content
                else '<div class="meta">(no generated content)</div>')
    raw = ('<details class="raw-json"><summary>View raw JSON</summary>'
            f'<pre>{html.escape(json.dumps(parsed, indent=2, ensure_ascii=False))}</pre></details>')
    return preview + raw


def _readonly_content_html(row: dict, parsed: dict) -> str:
    """For rows that already have an assembled_html (approved at least
    once), shows that exact HTML -- the real thing sent to Odoo -- rather
    than re-deriving a preview from the raw draft JSON."""
    if row.get("assembled_html"):
        return ('<div class="meta">Final assembled HTML (as published/attempted):</div>'
                f'<div class="preview">{row["assembled_html"]}</div>')
    return _draft_preview_html(parsed)


def _section_editor_row(row_id: int, index: int, heading: str = "", items=None) -> str:
    """One custom section: a heading and its bullet points.

    The two inputs are named as parallel lists (section_heading /
    section_items), so the browser submits any number of them with the approve
    form and FastAPI reads them back in order -- no per-section indexing to
    keep in step between the markup and the route.
    """
    body = "\n".join(str(i) for i in (items or []))
    return f"""
    <div class="section-row">
      <div class="section-row-head">
        <input type="text" name="section_heading" class="section-heading"
               value="{html.escape(heading)}" maxlength="{assembler.MAX_HEADING_LENGTH}"
               placeholder="Section heading, e.g. Technical Details">
        <button type="button" class="img-btn danger" title="Remove this section"
                onclick="removeSection(this)">Remove</button>
      </div>
      <textarea name="section_items" class="edit-field section-items" rows="3"
                placeholder="One point per line">{html.escape(body)}</textarea>
    </div>"""


def _editable_draft_fields_html(row_id: int, parsed: dict, is_regulated: bool,
                                 ratio: str, quantity_detail: str) -> str:
    """Shows the FULL assembled HTML a reviewer is actually approving --
    not just the model's raw draft -- built with the exact same
    assembler.py functions used at publish time, so there's no gap between
    what's reviewed and what goes to Odoo. The delivery/disclaimer tail is
    shown but not editable (centrally controlled via config.yaml). Read-only
    by default, with an Edit toggle that swaps in textareas client-side --
    nothing reaches the server until Approve/Reject is submitted."""
    editable_html = assembler.build_editable_parts_html(parsed, is_regulated, ratio, quantity_detail)
    tail_html = assembler.build_fixed_tail_html(is_regulated)
    mix_line_html = assembler.build_mix_line_html(is_regulated, ratio, quantity_detail)

    overview = html.escape(str(parsed.get("overview", "")))
    features = html.escape("\n".join(str(f) for f in (parsed.get("features") or [])))
    applications = html.escape("\n".join(str(a) for a in (parsed.get("applications") or [])))

    # Reviewer-added sections. Features and Applications cover most products;
    # some need a heading they do not -- Technical Details, Coverage, Care --
    # so the heading itself is editable rather than fixed in code.
    section_rows = "".join(
        _section_editor_row(row_id, i, sec["heading"], sec["items"])
        for i, sec in enumerate(assembler.normalise_sections(parsed.get("sections")))
    )

    tail_block = (f'<div class="tail-block"><div class="meta" style="margin-bottom:6px">'
                   f'Appended automatically &middot; not editable here</div>{tail_html}</div>'
                   if tail_html else "")

    return f"""
    <div class="draft-content" data-row-id="{row_id}">
        <div class="view-mode">
            <div class="preview editable-preview">{editable_html}</div>
            {tail_block}
            <button type="button" class="edit-toggle-btn" onclick="toggleEdit({row_id})"
                    title="Edit this description">&#9998; Edit description</button>
        </div>
        <div class="edit-mode" style="display:none">
            <div class="field-label">Overview</div>
            <textarea name="overview" class="edit-field" data-field="overview" rows="3">{overview}</textarea>
            <div class="field-label">Features (one per line)</div>
            <textarea name="features" class="edit-field" data-field="features" rows="4">{features}</textarea>
            <div class="field-label">Applications (one per line)</div>
            <textarea name="applications" class="edit-field" data-field="applications" rows="3">{applications}</textarea>

            <div class="field-label">Extra sections</div>
            <div class="meta" style="margin:-2px 0 7px">Give the section a heading and one
               point per line -- it publishes in the same style as Features and Applications.</div>
            <div class="sections" data-sections="{row_id}">{section_rows}</div>
            <button type="button" class="edit-toggle-btn" style="margin-top:0"
                    onclick="addSection({row_id})">+ Add a section</button>

            <div class="meta" style="margin-top:9px">Delivery &amp; Pickup and disclaimer copy are appended automatically.</div>
            <div class="edit-mode-actions">
                <button type="button" class="save-edit-btn" onclick="saveEdit({row_id})">Save</button>
                <button type="button" class="cancel-edit-btn" onclick="cancelEdit({row_id})">Cancel</button>
            </div>
        </div>
        <template class="mixline-template">{mix_line_html}</template>
    </div>
    <details class="raw-json"><summary>View original raw JSON</summary>
        <pre>{html.escape(json.dumps(parsed, indent=2, ensure_ascii=False))}</pre>
    </details>
    """


def _image_strip_html(row: dict, images: list[dict], *, editable: bool) -> str:
    """The photos going out with this draft.

    Shown read-only once the row leaves the queue, so History and Needs Retry
    still answer "what was sent with this?" rather than going blank.
    """
    if not product_images.is_enabled():
        return ""
    if not images and not editable:
        return ""

    row_id = row["id"]
    # Through the same shaping the JSON endpoints use, so the tile rendered here
    # on page load and the tile rendered by the browser after an upload are
    # built from identical fields rather than drifting apart.
    images = [_image_json(i) if "original_name" in i else i for i in images]
    tiles = ""
    for img in images:
        url = img["url"]
        badges = ""
        if img["position"] == 0:
            badges += '<span class="img-tag main">Main</span>'
        if img["verified"]:
            badges += '<span class="img-tag ok" title="Confirmed present on the product">On Odoo</span>'
        elif img["published"]:
            badges += ('<span class="img-tag pending" title="Sent to Odoo, not yet verified">'
                        'Sent</span>')
        controls = ""
        if editable and not img["published"]:
            name_attr = f'data-name="{html.escape(img["name"], quote=True)}"'
            make_main = ("" if img["position"] == 0 else
                          f'<button type="button" class="img-btn" title="Use as the main product image" '
                          f'{name_attr} onclick="imgPrimary({row_id},{img["id"]},this.dataset.name)">'
                          f'Set main</button>')
            controls = (f'<div class="img-controls">{make_main}'
                         f'<button type="button" class="img-btn danger" title="Remove this image" '
                         f'{name_attr} onclick="imgDelete({row_id},{img["id"]},this.dataset.name)">'
                         f'Remove</button></div>')
        detail = (f'<div class="img-detail">{html.escape(img["detail"])}</div>'
                   if img.get("detail") and not img["verified"] else "")
        tiles += f"""
        <figure class="img-tile">
          <a href="{url}" target="_blank" rel="noopener" title="Open full size">
            <img src="{url}" alt="{html.escape(img["name"])}" loading="lazy">
          </a>
          <figcaption>
            <div class="img-name" title="{html.escape(img["name"])}">{html.escape(img["name"])}</div>
            <div class="img-meta">{html.escape(img["size"])}{badges}</div>
            {detail}{controls}
          </figcaption>
        </figure>"""

    adder = ""
    if editable:
        remaining = settings.IMAGES_MAX_PER_PRODUCT - len(images)
        if remaining > 0:
            adder = f"""
            <label class="img-add" title="JPEG, PNG, GIF or WebP -- up to {settings.IMAGES_MAX_FILE_MB:g} MB each">
              <input type="file" accept="{product_images.ACCEPT_ATTRIBUTE}" multiple
                     onchange="imgUpload({row_id}, this)" hidden>
              <span class="img-add-plus">+</span>
              <span class="img-add-text">Add photos<br><small>{remaining} left</small></span>
            </label>"""
        else:
            adder = (f'<div class="img-add full"><span class="img-add-text">Limit of '
                      f'{settings.IMAGES_MAX_PER_PRODUCT} reached</span></div>')

    note = ""
    if editable:
        note = ('<span class="meta">Uploaded here, sent to Odoo with the description when you '
                 'approve. The first image is the main product photo.</span>')

    return f"""
    <div class="img-strip" data-row-id="{row_id}">
      <div class="img-strip-head">
        <span class="img-strip-title">Product images
          <span class="tab-count" data-img-count="{row_id}">{len(images)}</span></span>
        {note}
      </div>
      <div class="img-tiles" data-img-tiles="{row_id}">{tiles}{adder}</div>
      <div class="img-error" data-img-error="{row_id}" hidden></div>
    </div>"""


def _confirm(title: str, *, body: str = "", what: str = "", ok: str = "",
              tone: str = "", count: str = "") -> str:
    """The data-confirm attributes for the shared dialog (see SHARED_JS in ui.py).

    Every action on this dashboard is confirmed before it runs. Building the
    attributes here rather than repeating five escaped strings at each button
    means adding an action is one call, and that none of them can quietly ship
    without asking.

    `title` is the question, `body` says what will actually happen (the
    consequence, not "are you sure"), `what` is the subject quoted back -- the
    product title, the username -- which matters because six cards are usually
    open at once. `tone` picks the accent: danger for anything destructive, go
    for approvals, warn for the ones that reach a customer.
    """
    bits = [f'data-confirm="{html.escape(title, quote=True)}"']
    if body:
        bits.append(f'data-confirm-body="{html.escape(body, quote=True)}"')
    if what:
        bits.append(f'data-confirm-what="{html.escape(what, quote=True)}"')
    if ok:
        bits.append(f'data-confirm-ok="{html.escape(ok, quote=True)}"')
    if tone:
        bits.append(f'data-confirm-tone="{tone}"')
    if count:
        bits.append(f'data-confirm-count="{html.escape(count, quote=True)}"')
    return " ".join(bits)


# Bulk actions count the ticked rows themselves; {n} and {s} are filled in by
# the dialog at the moment it opens, so the number is never stale.
SELECTED = ".row-check:checked"


def _readable_failure(detail: str) -> str:
    """A stored failure message, tidied for the card.

    New failures are summarised when they happen (see odoo_connector), but
    anything saved before that still holds the raw Odoo traceback -- three
    thousand characters of somebody else's call stack in the middle of a
    review card. Tidied here on the way out rather than rewritten in the
    database, so what was recorded stays exactly as it was recorded.
    """
    detail = str(detail or "")
    if "Traceback" not in detail and len(detail) <= 300:
        return detail
    head = detail.split(":", 1)[0] if ":" in detail[:80] else ""
    summary = summarise_error(detail)
    return f"{head}: {summary}" if head else summary


def _card_html(row: dict, *, show_publish_retry: bool = False, show_reopen: bool = False,
                images: list[dict] | None = None) -> str:
    parsed = row.get("parsed_output") or {}
    safety_flags = row.get("safety_flags") or []
    reasons = row.get("reasons") or []
    has_content = bool(row.get("parsed_output"))

    notes = ""
    if safety_flags:
        notes += (f'<div class="flags"><b>Needs attention:</b> '
                   f'{html.escape(", ".join(str(f) for f in safety_flags))}</div>')
    if reasons and not safety_flags:
        notes += (f'<div class="meta">Escalation reason(s): '
                   f'{html.escape("; ".join(str(r) for r in reasons))}</div>')
    if row.get("edited"):
        notes += '<div class="meta">&#9998; Edited by a reviewer before approval</div>'
    if row.get("last_rejection_note") and row["status"] == Status.PENDING:
        # Regenerated after a rejection -- show the reviewer what was wrong with
        # the previous attempt so they can check it was actually addressed.
        notes += (f'<div class="dry-run-note"><b>Regenerated after rejection.</b> '
                   f'Previous attempt was rejected because: '
                   f'{html.escape(row["last_rejection_note"])}</div>')
    if row["status"] == Status.PENDING and not has_content:
        notes += ('<div class="flags"><b>Nothing could be written</b> for this product, '
                   'so there is nothing to approve. Use Regenerate to try again -- fetching '
                   'new products will not pick this one up, because it is already here.</div>')

    fail_note = ""
    if row.get("odoo_write_detail") and not row.get("published"):
        fail_note = (f'<div class="dry-run-note"><b>Last attempt:</b> '
                     f'{html.escape(_readable_failure(row["odoo_write_detail"]))}</div>')

    editable = row["status"] == Status.PENDING and row["task_type"] == TaskType.DRAFT and has_content

    title = row["title"]
    photo_count = len(images or [])

    action_buttons = ""
    if row["status"] == Status.PENDING:
        reject_required = "required" if settings.APPROVAL_REQUIRE_REJECT_REASON else ""
        approve_btn = ""
        if has_content:
            publishes = row["task_type"] == TaskType.DRAFT and settings.APPROVAL_AUTO_PUBLISH
            label = "Approve &amp; Publish" if publishes else "Approve"
            plain = "Approve and publish" if publishes else "Approve"
            if publishes:
                body = ("The description goes onto the product as soon as you confirm, "
                        "along with any changes you made here. There is no undo -- putting "
                        "it back means editing the product yourself.")
                if photo_count:
                    body += (f" {photo_count} photo{'s' if photo_count > 1 else ''} "
                              f"go{'' if photo_count > 1 else 'es'} up with it.")
            else:
                body = ("Marks it approved, including your changes. Nothing goes onto the "
                        "product until someone sends it.")
            attrs = _confirm(f"{plain} this description?", body=body, what=title,
                              ok=plain, tone="go")
            approve_btn = (f'<button class="approve" type="submit" '
                            f'formaction="{CONTENT_AGENT}/queue/{row["id"]}/approve" '
                            f'{attrs}>{label}</button>')
        # A pending row with nothing generated is stranded otherwise: fetch
        # dedup skips anything that already has a queue row, so Regenerate is
        # the only way to get another draft without deleting it by hand.
        regen_btn = ""
        if not has_content and row["task_type"] == TaskType.DRAFT:
            attrs = _confirm("Write this description again?", what=title, ok="Rewrite",
                              body="It gets written again from scratch. Takes a minute or two, "
                                    "and it may not come out any better than this one.")
            regen_btn = (f'<button class="publish" type="submit" '
                          f'formaction="{CONTENT_AGENT}/queue/{row["id"]}/regenerate" '
                          f'{attrs}>&#8635; Regenerate</button>')
        action_buttons = f"""
        {approve_btn}{regen_btn}
        <input class="reject-note" type="text" name="reviewer_note"
               placeholder="Reason for rejecting (optional)" {reject_required}>
        <button class="reject" type="submit" formaction="{CONTENT_AGENT}/queue/{row['id']}/reject"
                {_confirm("Turn this description down?",
                           body="It moves to the Turned down list and nothing goes onto the product. Your reason is kept and used if you ask for a new one later.",
                           what=title, ok="Reject", tone="danger")}>Reject</button>
        """
    elif show_publish_retry and row["task_type"] == TaskType.DRAFT:
        action_buttons = f"""
        <form method="post" action="{CONTENT_AGENT}/queue/{row['id']}/publish" style="display:inline">
            <button class="publish" type="submit"
                    {_confirm("Try publishing to Odoo again?",
                               body="The last attempt did not get through. This sends the same approved description again.",
                               what=title, ok="Publish")}>Retry publish to Odoo</button>
        </form>"""
    elif show_publish_retry:
        action_buttons = '<span class="meta">Classification rows are not published.</span>'
    elif (not settings.APPROVAL_AUTO_PUBLISH and row["status"] == Status.APPROVED
            and row["task_type"] == TaskType.DRAFT and not row.get("published")):
        action_buttons = f"""
        <form method="post" action="{CONTENT_AGENT}/queue/{row['id']}/publish" style="display:inline">
            <button class="publish" type="submit"
                    {_confirm("Publish this to Odoo?",
                               body="The approved description goes onto the product now. There is no undo.",
                               what=title, ok="Publish", tone="go")}>Publish to Odoo</button>
        </form>"""

    if show_reopen:
        action_buttons += f"""
        <form method="post" action="{CONTENT_AGENT}/queue/{row['id']}/reopen" style="display:inline">
            <button class="reopen" type="submit"
                    {_confirm("Send this back for review?",
                               body="It goes back into the queue as it is. Anything already on the product stays there.",
                               what=title, ok="Re-open")}>Re-open for review</button>
        </form>"""

    checkbox = ""
    if row["status"] == Status.PENDING and has_content:
        checkbox = (f'<input type="checkbox" class="row-check checkbox-col" name="id" '
                     f'value="{row["id"]}" form="bulk-form" title="Select for bulk action">')

    content_html = (
        _editable_draft_fields_html(row["id"], parsed, bool(row.get("is_regulated")),
                                     row.get("ratio") or "", row.get("quantity_detail") or "")
        if editable else _readonly_content_html(row, parsed)
    )

    body = f"""
        <div class="card-header">
            <div class="card-header-left">
                {checkbox}
                <span class="title">{html.escape(row['title'])}</span>
                <span class="badge {row['task_type']}">{row['task_type']}</span>
                <span class="badge {row['source']}">{row['source']}</span>
                {_confidence_badge(row['confidence'])}
                {f'<span class="badge planned">attempt {row["attempt_count"]}</span>'
                  if (row.get("attempt_count") or 1) > 1 else ''}
            </div>
            <div class="meta">SKU {html.escape(str(row['product_id']))}</div>
        </div>
        {notes}
        {content_html}
        {_image_strip_html(row, images or [], editable=editable)}
        {fail_note}
        <div class="actions">{action_buttons}</div>
    """

    if row["status"] == Status.PENDING:
        # Approve/Reject (and any inline edits) submit together as one POST,
        # routed by the formaction on each button.
        return (f'<div class="card"><form method="post" '
                f'action="{CONTENT_AGENT}/queue/{row["id"]}/approve">{body}</form></div>')
    return f'<div class="card">{body}</div>'


def _parse_filters(request: Request) -> dict:
    q = request.query_params
    try:
        page = max(1, int(q.get("page", 1) or 1))
    except ValueError:
        page = 1
    return {
        "task_type": q.get("task_type") or None,
        "source": q.get("source") or None,
        "confidence": q.get("confidence") or None,
        "flagged_only": q.get("flagged_only") or None,
        "page": page,
    }


def _filter_bar_html(active: dict, base_path: str) -> str:
    def option(value, label, current):
        return f'<option value="{value}"{" selected" if value == current else ""}>{label}</option>'

    task_type = active.get("task_type") or ""
    source = active.get("source") or ""
    confidence = active.get("confidence") or ""
    flagged_only = active.get("flagged_only") or ""
    is_filtered = any([task_type, source, confidence, flagged_only])

    return f"""
    <form class="filter-bar" method="get" action="{base_path}">
        <label>Type
            <select name="task_type">
                {option("", "All", task_type)}
                {option(TaskType.DRAFT, "Draft", task_type)}
                {option(TaskType.CLASSIFY, "Classify", task_type)}
            </select>
        </label>
        <label>Source
            <select name="source">
                {option("", "All", source)}
                {option(Source.SMALL_MODEL, Source.LABELS[Source.SMALL_MODEL], source)}
                {option(Source.FALLBACK_MODEL, Source.LABELS[Source.FALLBACK_MODEL], source)}
                {option(Source.CLAUDE, Source.LABELS[Source.CLAUDE], source)}
            </select>
        </label>
        <label>Confidence
            <select name="confidence">
                {option("", "All", confidence)}
                {option("low", "Low", confidence)}
                {option("high", "High", confidence)}
            </select>
        </label>
        <label><input type="checkbox" name="flagged_only" value="1"
               {"checked" if flagged_only else ""}> Safety flags only</label>
        <button class="btn-primary" type="submit">Apply</button>
        {f'<a class="clear" href="{base_path}">Clear filters</a>' if is_filtered else ''}
    </form>"""


def _pagination_html(base_path: str, query: dict, page: int, total: int, page_size: int) -> str:
    total_pages = max(1, (total + page_size - 1) // page_size)
    if total_pages <= 1:
        return ""

    def link(p, label):
        if p < 1 or p > total_pages:
            return f'<span class="disabled">{label}</span>'
        q = {k: v for k, v in {**query, "page": p}.items() if v not in (None, "")}
        return f'<a href="{base_path}?{urlencode(q)}">{label}</a>'

    return (f'<div class="pagination">{link(page - 1, "&laquo; Prev")}'
            f'<span class="meta">Page {page} of {total_pages} &middot; {total} item(s)</span>'
            f'{link(page + 1, "Next &raquo;")}</div>')


# Sub-tab key -> label, so the breadcrumb and the tab strip cannot drift apart.
AGENT_TAB_LABELS = {
    "pending": "To check",
    "needs-retry": "Did not send",
    "rejected": "Turned down",
    "history": "Done",
}


def _agent_tabs_html(active: str) -> str:
    counts = review_queue.counts_by_status()
    pending = counts.get(Status.PENDING, 0)
    retry = review_queue.count_rows(status=Status.APPROVED, published=False)
    rejected = counts.get(Status.REJECTED, 0)
    reviewed = counts.get(Status.APPROVED, 0) + rejected

    def tab(key, label, path, count):
        cls = "active" if key == active else ""
        badge = f'<span class="tab-count">{count}</span>' if count else ""
        return f'<a href="{path}" class="{cls}">{label}{badge}</a>'

    return (f'<div class="tabs">'
            f'{tab("pending", "To check", CONTENT_AGENT, pending)}'
            f'{tab("needs-retry", "Did not send", f"{CONTENT_AGENT}/needs-retry", retry)}'
            f'{tab("rejected", "Turned down", f"{CONTENT_AGENT}/rejected", rejected)}'
            f'{tab("history", "Done", f"{CONTENT_AGENT}/history", reviewed)}'
            f'</div>')


def _model_picker_html(name: str = "model", selected: str = "", form_id: str = "") -> str:
    """Which model to regenerate with.

    A reviewer looking at a rejected draft has just read what the last model
    produced, so they usually know which rung to reach for -- and each guess
    costs a full generation round-trip per product. Defaults to the normal
    escalation chain, so pressing Regenerate without touching this behaves
    exactly as it did before.

    Rungs that cannot run are shown disabled WITH THE REASON rather than
    hidden: "Claude is missing its API key" is worth knowing, and an option
    that silently vanishes looks like a bug.
    """
    options = ['<option value="">Choose for me (recommended)</option>']
    for model in pipeline.available_models():
        attrs = "" if model["available"] else " disabled"
        if model["key"] == selected and model["available"]:
            attrs += " selected"
        suffix = "" if model["available"] else f" -- {model['detail']}"
        options.append(f'<option value="{model["key"]}"{attrs}>'
                        f'{html.escape(model["label"])}{html.escape(suffix)}</option>')
    # form_id associates the select with a form it does not sit inside -- the
    # bulk bar's picker is outside regen-form, exactly like the row checkboxes.
    form_attr = f' form="{form_id}"' if form_id else ""
    return (f'<select name="{name}" class="model-pick"{form_attr} '
            f'title="Who should write the new description">{"".join(options)}</select>')


def _regen_bar_html(pending_count: int, can_run: bool = True) -> str:
    """Action bar for the Rejected tab -- regenerating everything at once is
    the point of the tab, so it gets a prominent control plus progress."""
    if _regen_state["running"]:
        done, total = _regen_state["done"], _regen_state["total"]
        return f"""
        <div class="fetch-bar">
            <button class="btn-accent" type="button" disabled><span class="spinner"></span> Regenerating...</button>
            <span class="meta">{done} of {total} done. This page refreshes automatically.</span>
        </div>"""

    last = _regen_state["last_result"]
    if not last:
        status = "Descriptions you turn down stay here until you ask for a new one."
    elif last.get("error"):
        status = f'<b>Last run failed:</b> {html.escape(last["error"])}'
    else:
        bits = [f'{last["regenerated"]} rewritten']
        if last["empty"]:
            bits.append(f'{last["empty"]} came back blank')
        if last["failed"]:
            bits.append(f'{last["failed"]} failed')
        status = "<b>Last run:</b> " + ", ".join(bits) + "."

    button = ""
    if pending_count and can_run:
        button = f"""
        <form method="post" action="{CONTENT_AGENT}/bulk/regenerate"
              style="margin:0; display:inline-flex; gap:6px; align-items:center">
            {_model_picker_html()}
            <button class="btn-accent" type="submit"
                    {_confirm(f"Draft all {pending_count} rejected description(s) again?",
                               body="Each one gets written again, one after another, so this takes a while. The current versions are replaced.",
                               ok="Regenerate all")}>&#8635; Regenerate all rejected</button>
        </form>"""

    escalate_note = ("go straight to the stronger writer"
                      if settings.REGENERATE_ESCALATE_FIRST else
                      "have another go with a bit more variation")
    return f"""
    <div class="fetch-bar">
        {button}
        <span class="meta">Fresh attempts {escalate_note}, and are told why you turned the
            last one down. {status}</span>
    </div>"""


# (friendly name, what it is) for each rung of the writing chain.
WRITER_LABELS = {
    "Fine-tuned model": ("First choice",
                          "Our own writer, trained on BC Sands product copy."),
    "Fallback model": ("Second choice",
                        "A general-purpose writer, used when the first one struggles."),
    "Claude escalation": ("Last resort",
                           "An outside writing service, for the hardest products."),
}


def _escalation_panel() -> str:
    """Who will actually write a description right now.

    Not the same question as what the settings ask for -- a writer can be
    switched on and still not be installed -- so this reports what would
    happen if you pressed Fetch this minute.
    """
    rows = ""
    for label, ok, detail in pipeline.escalation_status():
        friendly, blurb = WRITER_LABELS.get(label, (label, ""))
        disabled = "disabled" in detail
        if disabled:
            badge = '<span class="badge planned">Off</span>'
            note = "Switched off, so it is skipped."
        elif ok:
            badge = '<span class="badge live">Ready</span>'
            note = blurb
        else:
            badge = '<span class="badge confidence-low">Not available</span>'
            # The only case where the underlying detail is worth showing: it
            # says what to fix, and hiding it turns a fixable setup problem
            # into a mystery.
            note = f"{blurb} Cannot run right now: {detail}"
        rows += (f'<tr><td style="width:150px"><b>{html.escape(friendly)}</b></td>'
                 f'<td>{badge}</td>'
                 f'<td class="meta">{html.escape(note)}</td></tr>')
    return f"""
    <div class="panel">
      <h2>How descriptions get written</h2>
      <p class="meta" style="margin:-4px 0 12px">The first writer has a go. If what comes back
         looks weak, the next one tries instead. Whatever the last attempt produced still
         comes to this queue for you to check, marked as low confidence.</p>
      <table class="grid">{rows}</table>
    </div>"""


def _fetch_bar_html(can_run: bool = True) -> str:
    source_label = "live product list" if settings.USE_ODOO_AS_PRODUCT_SOURCE else "exported list"

    if _fetch_state["running"]:
        return f"""
        <div class="fetch-bar">
            <button class="btn-accent" type="button" disabled><span class="spinner"></span> Fetching...</button>
            <span class="meta">Writing descriptions from the <b>{html.escape(source_label)}</b>.
                This page updates on its own.</span>
        </div>"""

    last = _fetch_state["last_result"]
    if not last:
        status_text = "No manual fetch run yet. New products are also picked up by the scheduled job."
    elif last.get("error"):
        status_text = f'<b>Last fetch failed:</b> {html.escape(last["error"])}'
    else:
        status_text = (
            f'<b>Last fetch:</b> {last["processed"]} drafted &middot; '
            f'{last["escalated"]} escalated &middot; {last["safety_flagged"]} flagged &middot; '
            f'{last["failed"]} failed (of {last["total"]} attempted).'
        )
    if not can_run:
        return f"""
        <div class="fetch-bar">
            <span class="meta">New descriptions are written overnight, from the
                <b>{html.escape(source_label)}</b>, and appear here to be checked.</span>
        </div>"""

    return f"""
    <div class="fetch-bar">
        <form method="post" action="{CONTENT_AGENT}/fetch-new" style="margin:0">
            <button class="btn-accent" type="submit"
                    {_confirm("Draft the next batch of products?",
                               body=f"Writes descriptions for up to {settings.DAILY_BATCH_SIZE} more products "
                                     f"from the {source_label}. Give it several minutes. Anything already "
                                     "in the queue is left alone.",
                               ok="Fetch and draft")}>&#8635; Fetch new products</button>
        </form>
        <span class="meta">Source: <b>{html.escape(source_label)}</b> &middot;
            up to {settings.DAILY_BATCH_SIZE} per run &middot; {status_text}</span>
    </div>"""


_ENV_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")


def _config_drift() -> list[str]:
    """Differences between .env on disk and what this process loaded.

    Settings are read once, when the dashboard starts. Editing .env changes
    nothing until the next restart, and before this there was no way to tell
    from the dashboard that a restart was owed -- an Odoo database name was
    corrected in .env, the running process kept the old one, and publishing
    kept failing against a database that did not exist.

    Only names things that are safe to show. A changed username or API key is
    reported as changed, never by value.
    """
    try:
        values = dotenv_values(_ENV_FILE)
    except Exception:
        return []

    changes = []
    running_env = odoo_connector.env_name
    file_env = (values.get("ODOO_ENV") or running_env).strip().lower()
    if file_env != running_env:
        changes.append(f"Odoo environment: running {running_env}, the file now says {file_env}")

    url_key = "ODOO_URL_LIVE" if file_env == "live" else "ODOO_URL_STAGING"
    file_url = values.get(url_key) or ""
    if file_url and file_url.rstrip("/") != (odoo_connector.url or "").rstrip("/"):
        changes.append(f"Odoo address: running {odoo_connector.url or '(none)'}, "
                       f"the file now says {file_url}")

    file_db = values.get("ODOO_DB") or _derive_db_name(file_url or odoo_connector.url)
    if file_db and file_db != odoo_connector.db:
        changes.append(f"Odoo database: running {odoo_connector.db or '(none)'}, "
                       f"the file now says {file_db}")

    if "ODOO_USERNAME" in values and values["ODOO_USERNAME"] != odoo_connector.username:
        changes.append("Odoo login name has changed")
    if "ODOO_API_KEY" in values and values["ODOO_API_KEY"] != getattr(odoo_connector, "api_key", None):
        changes.append("Odoo API key has changed")
    return changes


def _restart_notice(request: Request) -> str:
    """A banner telling an admin that .env has moved on without the dashboard.

    Admin-only: a reviewer can neither restart the service nor edit .env, so
    for them it would be a worry with nothing to do about it.
    """
    if not _can(request, auth.PERM_ADMINISTER):
        return ""
    changes = _config_drift()
    if not changes:
        return ""
    items = "".join(f"<li>{html.escape(c)}</li>" for c in changes)
    return (f'<div class="flags"><b>.env has changed since the dashboard started, so these '
            f'changes are not being used yet:</b>'
            f'<ul style="margin:6px 0 6px 18px;padding:0">{items}</ul>'
            f'Restart the dashboard to apply them.</div>')


def _agent_page(request: Request, active: str, body_inner: str, title: str) -> str:
    body = f"""
    <div class="page-head">
      <div>
        <h1>Content Agent</h1>
        <p class="subtitle">{title}</p>
      </div>
    </div>
    {_restart_notice(request)}
    {_agent_tabs_html(active)}
    {body_inner}
    """
    crumbs = [("Content Agent", CONTENT_AGENT)]
    if active in AGENT_TAB_LABELS:
        crumbs.append((AGENT_TAB_LABELS[active], ""))
    return ui.page_shell(body, title="Content Agent · Content and Automation",
                          active_module="content-agent", user=_current_user(request),
                          request=request, crumbs=crumbs,
                          fetch_running=_fetch_state["running"] or _regen_state["running"])


# ---------------------------------------------------------------------------
# Content Agent -- pages
# ---------------------------------------------------------------------------

@app.get(CONTENT_AGENT, response_class=HTMLResponse)
def agent_queue(request: Request):
    filters = _parse_filters(request)
    page_size = settings.APPROVAL_PAGE_SIZE
    offset = (filters["page"] - 1) * page_size
    flagged_only = True if filters["flagged_only"] else None

    common = dict(status=Status.PENDING, task_type=filters["task_type"], source=filters["source"],
                   confidence=filters["confidence"], has_safety_flags=flagged_only)
    rows = review_queue.list_rows(**common, limit=page_size, offset=offset)
    total = review_queue.count_rows(**common)

    if rows:
        images_by_row = review_queue.list_images_for_rows([r["id"] for r in rows])
        cards_html = "".join(
            _card_html(row, images=images_by_row.get(row["id"], [])) for row in rows)
    elif total == 0 and not any([filters["task_type"], filters["source"],
                                  filters["confidence"], filters["flagged_only"]]):
        cards_html = ui.empty_state("You are all caught up",
                                     "Nothing is waiting to be checked. Use “Fetch new products” "
                                     "to write descriptions for more of the range.")
    else:
        cards_html = ui.empty_state("No matches", "No pending items match these filters.")

    bulk_bar = f"""
    <form id="bulk-form" method="post" action="{CONTENT_AGENT}/bulk/approve" style="display:contents"></form>
    <div class="bulk-bar">
        <label><input type="checkbox" class="checkbox-col" onclick="toggleAll(this)"> Select all on page</label>
        <span class="meta"><span class="sel-count" id="sel-count">0</span> selected</span>
        <div style="margin-left:auto; display:flex; gap:8px;">
            <button class="approve" type="submit" form="bulk-form" data-needs-selection
                    {_confirm("Approve {n} description{s}?",
                               body="Approved in one go, exactly as written. Any changes you typed into a card are not included when you approve several at once. "
                                    + ("They publish to Odoo immediately." if settings.APPROVAL_AUTO_PUBLISH
                                        else "They wait to be sent to the products."),
                               ok="Approve {n}", tone="go", count=SELECTED)}>Approve selected</button>
            <button class="reject" type="submit" form="bulk-form" data-needs-selection
                    formaction="{CONTENT_AGENT}/bulk/reject"
                    {_confirm("Turn down {n} description{s}?",
                               body="They move to the Turned down list with no reason recorded. Turn them down one at a time if you want to say why.",
                               ok="Reject {n}", tone="danger", count=SELECTED)}>Reject selected</button>
        </div>
    </div>"""

    query_state = {k: v for k, v in filters.items() if k != "page"}
    inner = (f'{_fetch_bar_html(_can(request, auth.PERM_RUN_JOBS))}{_escalation_panel()}'
             f'{_filter_bar_html(filters, CONTENT_AGENT)}{bulk_bar}{cards_html}'
             f'{_pagination_html(CONTENT_AGENT, query_state, filters["page"], total, page_size)}')

    subtitle = ("Approving a description sends it straight to the product page."
                if settings.APPROVAL_AUTO_PUBLISH else
                "Approving marks a description ready. Sending it to the product page "
                "is a separate step.")
    return _agent_page(request, "pending", inner,
                        f"Descriptions waiting to be checked. {subtitle}")


@app.get(f"{CONTENT_AGENT}/needs-retry", response_class=HTMLResponse)
def agent_needs_retry(request: Request):
    filters = _parse_filters(request)
    page_size = settings.APPROVAL_PAGE_SIZE
    offset = (filters["page"] - 1) * page_size
    flagged_only = True if filters["flagged_only"] else None

    common = dict(status=Status.APPROVED, published=False, task_type=filters["task_type"],
                   source=filters["source"], confidence=filters["confidence"],
                   has_safety_flags=flagged_only)
    rows = review_queue.list_rows(**common, limit=page_size, offset=offset)
    total = review_queue.count_rows(**common)

    retry_images = review_queue.list_images_for_rows([r["id"] for r in rows])
    cards_html = ("".join(_card_html(r, show_publish_retry=True,
                                      images=retry_images.get(r["id"], [])) for r in rows) if rows
                   else ui.empty_state("Nothing to fix",
                                        "Descriptions you approved that did not reach the product "
                                        "page would show up here."))

    query_state = {k: v for k, v in filters.items() if k != "page"}
    inner = (f'{_filter_bar_html(filters, f"{CONTENT_AGENT}/needs-retry")}{cards_html}'
             f'{_pagination_html(f"{CONTENT_AGENT}/needs-retry", query_state, filters["page"], total, page_size)}')
    return _agent_page(request, "needs-retry", inner,
                        "You approved these, but they did not make it to the product page. "
                        "Try sending them again.")


@app.get(f"{CONTENT_AGENT}/rejected", response_class=HTMLResponse)
def agent_rejected(request: Request):
    filters = _parse_filters(request)
    page_size = settings.APPROVAL_PAGE_SIZE
    offset = (filters["page"] - 1) * page_size
    flagged_only = True if filters["flagged_only"] else None

    common = dict(status=Status.REJECTED, task_type=filters["task_type"], source=filters["source"],
                   confidence=filters["confidence"], has_safety_flags=flagged_only)
    rows = review_queue.list_rows(**common, limit=page_size, offset=offset)
    total = review_queue.count_rows(**common)

    cards_html = ""
    if rows:
        for row in rows:
            note = row.get("reviewer_note") or row.get("last_rejection_note") or ""
            note_html = (f'<div class="dry-run-note"><b>Rejected because:</b> {html.escape(note)}</div>'
                          if note else '<div class="meta">No reason was recorded.</div>')
            attempts = row.get("attempt_count") or 1
            attempt_badge = (f'<span class="badge planned">attempt {attempts}</span>'
                              if attempts > 1 else "")
            cards_html += f"""
            <div class="card">
                <div class="card-header">
                    <div class="card-header-left">
                        <input type="checkbox" class="row-check checkbox-col" name="id"
                               value="{row['id']}" form="regen-form" title="Select to regenerate">
                        <span class="title">{html.escape(row['title'])}</span>
                        <span class="badge {row['task_type']}">{row['task_type']}</span>
                        <span class="badge {row['source']}">{row['source']}</span>
                        {attempt_badge}
                    </div>
                    <div class="meta">SKU {html.escape(str(row['product_id']))}</div>
                </div>
                {note_html}
                <div class="meta">Rejected: {html.escape(str(row.get('reviewed_at') or '--'))}
                    {f"by <b>{html.escape(row['reviewed_by'])}</b>" if row.get('reviewed_by') else ''}</div>
                {_draft_preview_html(row.get('parsed_output') or {})}
                <div class="actions">
                    <form method="post" action="{CONTENT_AGENT}/queue/{row['id']}/regenerate"
                          style="display:inline-flex; gap:6px; align-items:center">
                        {_model_picker_html()}
                        <button class="publish" type="submit"
                                {_confirm("Draft this description again?",
                                           body="It gets written again, using the writer picked beside this button and told why you turned this one down. The current version is replaced.",
                                           what=row["title"], ok="Regenerate")}>&#8635; Regenerate</button>
                    </form>
                    <form method="post" action="{CONTENT_AGENT}/queue/{row['id']}/reopen"
                          style="display:inline">
                        <button class="reopen" type="submit"
                                {_confirm("Put this back in the review queue?",
                                           body="The rejected draft returns for review unchanged -- nothing is regenerated.",
                                           what=row["title"], ok="Re-open")}>Re-open as-is</button>
                    </form>
                </div>
            </div>"""
    else:
        cards_html = ui.empty_state(
            "Nothing turned down",
            "Descriptions you reject are kept here so you can ask for a better one later.")

    bulk_form = "" if not _can(request, auth.PERM_RUN_JOBS) else f"""
    <form id="regen-form" method="post" action="{CONTENT_AGENT}/bulk/regenerate" style="display:contents"></form>
    <div class="bulk-bar">
        <label><input type="checkbox" class="checkbox-col" onclick="toggleAll(this)"> Select all on page</label>
        <span class="meta"><span class="sel-count" id="sel-count">0</span> selected</span>
        <div style="margin-left:auto; display:flex; gap:8px; align-items:center">
            {_model_picker_html(form_id="regen-form")}
            <button class="publish" type="submit" form="regen-form" data-needs-selection
                    {_confirm("Draft {n} description{s} again?",
                               body="Each one gets written again by the writer you picked, so this takes a while. The current versions are replaced.",
                               ok="Regenerate {n}", count=SELECTED)}>Regenerate selected</button>
        </div>
    </div>""" if rows else ""

    can_run_jobs = _can(request, auth.PERM_RUN_JOBS)
    query_state = {k: v for k, v in filters.items() if k != "page"}
    inner = (f'{_regen_bar_html(total, can_run_jobs)}'
             f'{_filter_bar_html(filters, f"{CONTENT_AGENT}/rejected")}'
             f'{bulk_form}{cards_html}'
             f'{_pagination_html(f"{CONTENT_AGENT}/rejected", query_state, filters["page"], total, page_size)}')
    return _agent_page(request, "rejected", inner,
                        "Descriptions you turned down. Ask for a fresh attempt, or put one "
                        "back in the queue as it is if you changed your mind.")


@app.get(f"{CONTENT_AGENT}/history", response_class=HTMLResponse)
def agent_history(request: Request):
    filters = _parse_filters(request)
    page_size = settings.APPROVAL_PAGE_SIZE
    offset = (filters["page"] - 1) * page_size
    flagged_only = True if filters["flagged_only"] else None

    rows = []
    for st in (Status.APPROVED, Status.REJECTED):
        rows.extend(review_queue.list_rows(
            status=st, task_type=filters["task_type"], source=filters["source"],
            confidence=filters["confidence"], has_safety_flags=flagged_only,
        ))
    rows.sort(key=lambda r: r.get("reviewed_at") or "", reverse=True)
    total = len(rows)
    rows = rows[offset:offset + page_size]

    cards_html = "" if rows else ui.empty_state("Nothing checked yet",
                                                 "Descriptions you approve or turn down will be "
                                                 "listed here.")
    for row in rows:
        approved = row["status"] == Status.APPROVED
        status_badge = (f'<span class="badge {"live" if approved else "confidence-low"}">'
                        f'{row["status"].upper()}</span>')
        published_html = ""
        if row.get("published"):
            published_html = '<div style="margin-top:7px"><span class="published-badge">PUBLISHED TO ODOO</span></div>'
        elif row.get("odoo_write_detail"):
            published_html = (f'<div class="dry-run-note">'
                              f'{html.escape(_readable_failure(row["odoo_write_detail"]))}</div>')

        note_html = ""
        if row.get("reviewer_note"):
            note_html += f'<div class="meta">Note: {html.escape(row["reviewer_note"])}</div>'
        if row.get("edited"):
            note_html += '<div class="meta">&#9998; Edited by a reviewer before approval</div>'

        cards_html += f"""
        <div class="card">
            <div class="card-header">
                <div class="card-header-left">
                    <span class="title">{html.escape(row['title'])}</span>
                    <span class="badge {row['task_type']}">{row['task_type']}</span>
                    {status_badge}
                </div>
                <div class="meta">SKU {html.escape(str(row['product_id']))}</div>
            </div>
            <div class="meta">Reviewed: {html.escape(str(row.get('reviewed_at') or '--'))}
                {f"by <b>{html.escape(row['reviewed_by'])}</b>" if row.get('reviewed_by') else ''}</div>
            {note_html}{published_html}
            <div class="actions">
                <form method="post" action="{CONTENT_AGENT}/queue/{row['id']}/reopen" style="display:inline">
                    <button class="reopen" type="submit"
                            {_confirm("Send this back for review?",
                                       body="It goes back into the queue as it is. Anything already on the product stays there until a new version replaces it.",
                                       what=row["title"], ok="Re-open")}>Re-open for review</button>
                </form>
            </div>
        </div>"""

    query_state = {k: v for k, v in filters.items() if k != "page"}
    inner = (f'{_filter_bar_html(filters, f"{CONTENT_AGENT}/history")}{cards_html}'
             f'{_pagination_html(f"{CONTENT_AGENT}/history", query_state, filters["page"], total, page_size)}')
    return _agent_page(request, "history", inner,
                        "Everything you have already checked, and what happened to it.")


# ---------------------------------------------------------------------------
# Content Agent -- actions
# ---------------------------------------------------------------------------

@app.post(f"{CONTENT_AGENT}/fetch-new")
def fetch_new_products(request: Request, background_tasks: BackgroundTasks):
    denied = _denied(request, auth.PERM_RUN_JOBS, CONTENT_AGENT)
    if denied:
        return denied
    """Manually triggers the same fetch-and-draft logic daily_run.py runs on
    its schedule, so a reviewer can pull new products on demand instead of
    waiting for the next scheduled run. Runs in the background; the page
    polls itself while it's going."""
    if _fetch_state["running"]:
        return _redirect(CONTENT_AGENT, err="A fetch is already running.")
    _fetch_state["running"] = True
    _fetch_state["started_at"] = datetime.now(timezone.utc).isoformat()
    background_tasks.add_task(_run_fetch_job)
    return _redirect(CONTENT_AGENT, msg="Started. New descriptions will appear here shortly.")


def _apply_draft_edits(row_id: int, overview: str | None, features: str | None,
                        applications: str | None,
                        section_headings: list[str] | None = None,
                        section_items: list[str] | None = None) -> None:
    """Persists inline reviewer edits so what gets published is what they
    actually approved, not the model's original output. No-op for classify
    rows or when nothing changed."""
    if overview is None and features is None and applications is None:
        return
    row = review_queue.get_row(row_id)
    if not row or row["task_type"] != TaskType.DRAFT:
        return

    new_parsed = dict(row.get("parsed_output") or {})
    new_parsed["overview"] = (overview or "").strip()
    new_parsed["features"] = [ln.strip() for ln in (features or "").splitlines() if ln.strip()]
    new_parsed["applications"] = [ln.strip() for ln in (applications or "").splitlines() if ln.strip()]

    # Headings and bodies arrive as two parallel lists in field order. zip stops
    # at the shorter one, so a malformed post cannot pair a heading with the
    # wrong body -- it just drops the odd one out. normalise_sections then
    # discards anything empty, which is also how a section gets deleted: clear
    # its heading or its points and it stops existing.
    sections = [{"heading": h, "items": (b or "").splitlines()}
                for h, b in zip(section_headings or [], section_items or [])]
    sections = assembler.normalise_sections(sections)
    if sections:
        new_parsed["sections"] = sections
    else:
        new_parsed.pop("sections", None)

    if new_parsed != (row.get("parsed_output") or {}):
        review_queue.update_parsed_output(row_id, new_parsed)


def _attempt_publish(row_id: int) -> dict[Any, Any] | None:
    row = review_queue.get_row(row_id)
    assembled = assemble_html(
        title=row["title"],
        draft_json=row.get("parsed_output") or {},
        is_regulated=bool(row.get("is_regulated")),
        ratio=row.get("ratio") or "",
        quantity_detail=row.get("quantity_detail") or "",
    )
    write_result = odoo_connector.write_product_description(row["product_id"], assembled)

    # The images go out in the same action as the copy -- that is the whole
    # point of staging them here. A failure on the image half does not fail the
    # publish: the description is live and correct, and the images stay staged
    # and retryable rather than blocking it.
    image_detail = ""
    if write_result["success"]:
        img = product_images.publish_for_row(row, odoo_connector)
        if not img["skipped"]:
            if img["published"]:
                image_detail = f" {img['published']} image(s) uploaded."
            if img["failed"]:
                image_detail += f" {img['failed']} image(s) failed: {img['detail']}"

    return review_queue.publish_row(
        row_id, assembled_html=assembled,
        success=write_result["success"], detail=write_result["detail"] + image_detail,
    )


@app.post(f"{CONTENT_AGENT}/queue/{{row_id}}/approve")
def approve_row(request: Request, row_id: int,
                overview: str | None = Form(default=None),
                features: str | None = Form(default=None),
                applications: str | None = Form(default=None),
                section_heading: list[str] = Form(default=[]),
                section_items: list[str] = Form(default=[])):
    before = review_queue.get_row(row_id)
    _apply_draft_edits(row_id, overview, features, applications,
                        section_heading, section_items)
    after_edit = review_queue.get_row(row_id)
    if after_edit and after_edit.get("edited") and not (before or {}).get("edited"):
        _audit(request, "edited", after_edit,
               detail="Reviewer changed the draft text before approving.")

    # Server-side guard, not just a hidden button -- a row with no generated
    # content has nothing to approve. Re-checked AFTER applying edits, so a
    # reviewer who typed content in manually can still approve.
    current = review_queue.get_row(row_id)
    if not current or not current.get("parsed_output"):
        return _redirect(CONTENT_AGENT, err="That item has no generated content, so it can't be approved.")

    row = review_queue.update_status(row_id, Status.APPROVED, actor=_actor(request))
    _audit(request, "approved", row)

    if row and row["task_type"] == TaskType.DRAFT and settings.APPROVAL_AUTO_PUBLISH:
        result = _attempt_publish(row_id)
        _audit(request, "published" if (result or {}).get("published") else "publish_failed",
               row, detail=(result or {}).get("odoo_write_detail", ""))
        if result and not result.get("published"):
            # The approval still stands; it moves to Needs Retry rather than
            # being lost or silently stuck.
            return _redirect(CONTENT_AGENT,
                              err=f"Approved, but the Odoo write failed: {result.get('odoo_write_detail', '')}")
        return _redirect(CONTENT_AGENT, msg=f"Approved and published “{row['title']}”.")

    return _redirect(CONTENT_AGENT, msg="Approved.")


# ---------------------------------------------------------------------------
# Product images -- staged here, published with the description
# ---------------------------------------------------------------------------
def _image_json(image: dict) -> dict:
    """Shape the browser needs to render one thumbnail."""
    return {
        "id": image["id"],
        "name": image["original_name"],
        "size": product_images.human_bytes(image["byte_size"]),
        "position": image["position"],
        "published": bool(image["published"]),
        "verified": bool(image["verified"]),
        "url": f"{CONTENT_AGENT}/queue/{image['row_id']}/images/{image['id']}",
        "detail": image.get("publish_detail") or "",
    }


def _images_payload(row_id: int) -> dict:
    images = [i for i in review_queue.list_images(row_id) if product_images.exists_on_disk(i)]
    return {"images": [_image_json(i) for i in images],
             "max": settings.IMAGES_MAX_PER_PRODUCT}


@app.get(f"{CONTENT_AGENT}/queue/{{row_id}}/images/list")
def list_row_images(request: Request, row_id: int):
    return JSONResponse(_images_payload(row_id))


@app.post(f"{CONTENT_AGENT}/queue/{{row_id}}/images")
async def upload_row_images(request: Request, row_id: int, files: list[UploadFile] = []):
    """Stages one or more images against a draft.

    Answers JSON because the reviewer uploads from inside the approve form --
    a normal POST here would submit or reload the page and throw away whatever
    text edits they had in progress.
    """
    row = review_queue.get_row(row_id)
    if not row:
        return JSONResponse({"error": "That item is no longer in the queue."}, status_code=404)
    if not product_images.is_enabled():
        return JSONResponse({"error": "Image upload is switched off."}, status_code=403)
    if row["status"] != Status.PENDING:
        return JSONResponse(
            {"error": "Images can only be attached while an item is still awaiting review."},
            status_code=409)

    errors = []
    added = 0
    for upload in files:
        data = await upload.read()
        # Sniffed from the bytes in product_images.stage -- the filename and the
        # browser's content-type are both supplied by the client and neither is
        # evidence of what this file actually is.
        image, err = product_images.stage(row, data, upload.filename or "image", _actor(request))
        if image:
            added += 1
        elif err:
            errors.append(err)

    if added:
        _audit(request, "images_added", row,
                detail=f"Attached {added} image(s) to the draft.")
    payload = _images_payload(row_id)
    payload["errors"] = errors
    payload["added"] = added
    return JSONResponse(payload)


@app.get(f"{CONTENT_AGENT}/queue/{{row_id}}/images/{{image_id}}")
def serve_row_image(request: Request, row_id: int, image_id: int):
    """Serves a staged image back for the thumbnail strip.

    The whole dashboard is behind the auth middleware, so this is not public.
    Content-Type comes from what the bytes were sniffed as at upload, never
    from the uploaded filename, and Content-Disposition is inline-with-a-name
    so a browser renders it rather than guessing.
    """
    image = review_queue.get_image(image_id)
    if not image or image["row_id"] != row_id:
        return Response(status_code=404)
    data = product_images.read_bytes(image)
    if data is None:
        return Response(status_code=404)
    return Response(content=data, media_type=image["content_type"], headers={
        "Cache-Control": "private, max-age=300",
        "Content-Disposition": "inline",
        "X-Content-Type-Options": "nosniff",
    })


@app.post(f"{CONTENT_AGENT}/queue/{{row_id}}/images/{{image_id}}/delete")
def delete_row_image(request: Request, row_id: int, image_id: int):
    image = review_queue.get_image(image_id)
    if not image or image["row_id"] != row_id:
        return JSONResponse({"error": "That image is no longer attached."}, status_code=404)
    ok, err = product_images.remove(image_id)
    if not ok:
        return JSONResponse({"error": err}, status_code=409)
    _audit(request, "images_removed", review_queue.get_row(row_id),
            detail=f"Removed image {image['original_name']} before publish.")
    return JSONResponse(_images_payload(row_id))


@app.post(f"{CONTENT_AGENT}/queue/{{row_id}}/images/{{image_id}}/primary")
def primary_row_image(request: Request, row_id: int, image_id: int):
    image = review_queue.get_image(image_id)
    if not image or image["row_id"] != row_id:
        return JSONResponse({"error": "That image is no longer attached."}, status_code=404)
    product_images.set_primary(row_id, image_id)
    return JSONResponse(_images_payload(row_id))


@app.post(f"{CONTENT_AGENT}/bulk/approve")
def bulk_approve_rows(request: Request, id: list[int] = Form(default=[])):
    approved = published = skipped = failed = 0
    for row_id in id:
        candidate = review_queue.get_row(row_id)
        if not candidate or not candidate.get("parsed_output"):
            skipped += 1  # nothing generated -- see approve_row's guard
            continue
        row = review_queue.update_status(row_id, Status.APPROVED, actor=_actor(request))
        # Each row gets its own entry: "approved 40 items" as a single line
        # would be useless when tracing what happened to one product.
        _audit(request, "approved", row, detail="Bulk approval.")
        approved += 1
        if row and row["task_type"] == TaskType.DRAFT and settings.APPROVAL_AUTO_PUBLISH:
            result = _attempt_publish(row_id)
            _audit(request, "published" if (result or {}).get("published") else "publish_failed",
                   row, detail=(result or {}).get("odoo_write_detail", ""))
            if result and result.get("published"):
                published += 1
            else:
                failed += 1

    parts = [f"{approved} approved"]
    if published:
        parts.append(f"{published} published")
    if failed:
        parts.append(f"{failed} failed to publish")
    if skipped:
        parts.append(f"{skipped} skipped (no content)")
    summary = ", ".join(parts) + "."
    return _redirect(CONTENT_AGENT, err=summary if failed else "", msg="" if failed else summary)


@app.post(f"{CONTENT_AGENT}/queue/{{row_id}}/reject")
def reject_row(request: Request, row_id: int, reviewer_note: str = Form(default="")):
    row = review_queue.update_status(row_id, Status.REJECTED,
                                      reviewer_note=reviewer_note, actor=_actor(request))
    _audit(request, "rejected", row, detail=reviewer_note)
    return _redirect(CONTENT_AGENT, msg="Rejected.")


@app.post(f"{CONTENT_AGENT}/bulk/reject")
def bulk_reject_rows(request: Request, id: list[int] = Form(default=[])):
    for row_id in id:
        row = review_queue.update_status(row_id, Status.REJECTED, actor=_actor(request))
        _audit(request, "rejected", row, detail="Bulk rejection.")
    return _redirect(CONTENT_AGENT, msg=f"Rejected {len(id)} item(s).")


@app.post(f"{CONTENT_AGENT}/queue/{{row_id}}/publish")
def publish_row(request: Request, row_id: int):
    row = review_queue.get_row(row_id)
    if not row or row["status"] != Status.APPROVED or row["task_type"] != TaskType.DRAFT:
        return _redirect(f"{CONTENT_AGENT}/needs-retry", err="That item can't be published.")
    result = _attempt_publish(row_id)
    _audit(request, "published" if (result or {}).get("published") else "publish_failed",
           row, detail=(result or {}).get("odoo_write_detail", ""))
    if result and result.get("published"):
        return _redirect(f"{CONTENT_AGENT}/needs-retry", msg=f"Published “{row['title']}” to Odoo.")
    detail = (result or {}).get("odoo_write_detail", "unknown error")
    return _redirect(f"{CONTENT_AGENT}/needs-retry", err=f"Publish failed: {detail}")


@app.post(f"{CONTENT_AGENT}/queue/{{row_id}}/reopen")
def reopen_row(request: Request, row_id: int):
    row = review_queue.get_row(row_id)
    _audit(request, "reopened", row,
           detail=f"Was {row['status']}." if row else "")
    review_queue.reopen_row(row_id)
    return _redirect(f"{CONTENT_AGENT}/history", msg="Re-opened for review.")


@app.post(f"{CONTENT_AGENT}/queue/{{row_id}}/regenerate")
def regenerate_row(row_id: int, background_tasks: BackgroundTasks,
                    model: str = Form(default="")):
    """Re-drafts a single rejected item. Backgrounded like the bulk version --
    one product still means a full model round-trip."""
    row = review_queue.get_row(row_id)
    # Come back to whichever tab the button was clicked from.
    back = f"{CONTENT_AGENT}/rejected" if row and row["status"] == Status.REJECTED else CONTENT_AGENT

    if _regen_state["running"]:
        return _redirect(back, err="A regeneration run is already in progress.")
    if not row:
        return _redirect(back, err="No such item.")
    if row["task_type"] != TaskType.DRAFT:
        return _redirect(back, err="Only draft rows can be regenerated (classification rows have no copy).")

    model, error = _validate_regen_model(model)
    if error:
        return _redirect(back, err=error)

    _regen_state.update({"running": True, "started_at": datetime.now(timezone.utc).isoformat(),
                          "total": 1, "done": 0})
    background_tasks.add_task(_run_regen_job, [row_id], model)
    return _redirect(back, msg=f"Regenerating “{row['title']}”{_model_suffix(model)} -- "
                                f"it returns to the review queue when done.")


@app.post(f"{CONTENT_AGENT}/bulk/regenerate")
def bulk_regenerate_rows(request: Request, background_tasks: BackgroundTasks,
                          id: list[int] = Form(default=[]),
                          model: str = Form(default="")):
    """Regenerates the selected rejected drafts, or every rejected draft when
    nothing is ticked (the "Regenerate all rejected" button posts no ids).

    Admin-only, unlike the single-row Regenerate beside each card: this one
    runs the writer once per product across the whole rejected list, which
    ties up the machine for as long as it takes and cannot be stopped from
    the browser.
    """
    denied = _denied(request, auth.PERM_RUN_JOBS, f"{CONTENT_AGENT}/rejected")
    if denied:
        return denied
    if _regen_state["running"]:
        return _redirect(f"{CONTENT_AGENT}/rejected", err="A regeneration run is already in progress.")

    if id:
        rows = [review_queue.get_row(i) for i in id]
    else:
        rows = review_queue.list_rows(status=Status.REJECTED)
    row_ids = [r["id"] for r in rows if r and r["task_type"] == TaskType.DRAFT]
    if not row_ids:
        return _redirect(f"{CONTENT_AGENT}/rejected", err="Nothing to regenerate.")

    model, error = _validate_regen_model(model)
    if error:
        return _redirect(f"{CONTENT_AGENT}/rejected", err=error)

    _regen_state.update({"running": True, "started_at": datetime.now(timezone.utc).isoformat(),
                          "total": len(row_ids), "done": 0})
    background_tasks.add_task(_run_regen_job, row_ids, model)
    return _redirect(f"{CONTENT_AGENT}/rejected",
                      msg=f"Regenerating {len(row_ids)} draft(s){_model_suffix(model)} "
                          f"in the background.")


# ---------------------------------------------------------------------------
# Legacy route redirects (pre-restructure bookmarks)
# ---------------------------------------------------------------------------

@app.get("/needs-retry")
def legacy_needs_retry():
    return RedirectResponse(url=f"{CONTENT_AGENT}/needs-retry", status_code=307)


@app.get("/approved")
def legacy_approved():
    return RedirectResponse(url=f"{CONTENT_AGENT}/needs-retry", status_code=307)


@app.get("/history")
def legacy_history():
    return RedirectResponse(url=f"{CONTENT_AGENT}/history", status_code=307)


# ---------------------------------------------------------------------------
# Chat Insights
# ---------------------------------------------------------------------------

def _chat_leads_bar() -> str:
    """The standing pointer to the lead list.

    Given the whole width and its own colour because of what is behind it: a
    customer who left a phone number and is waiting for a call. Everything
    else on this page is something to read, and this was competing with it as
    a small chip in a row of small chips.
    """
    by_status = chat_db.lead_counts_by("status")
    total, new_count = sum(by_status.values()), by_status.get("new", 0)
    if not total:
        return ""

    # The count that matters is the one nobody has picked up yet. When that is
    # zero the card still points at the list, but it stops shouting.
    waiting = (f'<span class="lead-cta-new">{new_count} waiting</span>'
                if new_count else '')
    contact_note = "Every one left a phone number or an email address."
    if not new_count:
        sub = f"All followed up. {contact_note}"
    elif new_count == total:
        sub = f"None followed up yet. {contact_note}"
    else:
        sub = (f'{new_count} of them still {"needs" if new_count == 1 else "need"} '
                f'a call. {contact_note}')

    return f"""
    <a class="lead-cta" href="{CHAT_INSIGHTS}/leads">
      <span class="lead-cta-icon">{ui.icon("phone", 22)}</span>
      <span class="lead-cta-body">
        <span class="lead-cta-title">{total} lead{"s" if total != 1 else ""}{waiting}</span>
        <span class="lead-cta-sub">{sub}</span>
      </span>
      <span class="lead-cta-go">View leads {ui.icon("arrow", 16)}</span>
    </a>"""


# Plain names for each thing this needs, and what it is for. The status list
# itself names services and model versions, which is right for the command
# line and wrong for a page a manager reads. Mapped here rather than in
# weekly_run so --dry-run keeps the detail an admin wants.
CHAT_SETUP_LABELS = {
    "Chatbase": ("Website chat", "Where the conversations are read from."),
    "Analysis model": ("Reader", "Works out what each conversation was about."),
    "Email server": ("Email", "Used to send the report out."),
    "Lead extraction": ("Contact details", "Picks names and numbers out of a chat."),
}


def _chat_status_panel() -> str:
    rows = ""
    for label, ok, detail in chat_run.status_lines():
        friendly, blurb = CHAT_SETUP_LABELS.get(
            label, (label.replace("Recipients: ", "Who gets the "), ""))
        # The underlying detail is shown only when something is not ready: it
        # says what to fix, and hiding it would turn a fixable setup problem
        # into a dead end.
        note = blurb if ok else (f"{blurb} {detail}".strip() if blurb else detail)
        rows += (f'<tr><td style="width:150px"><b>{html.escape(friendly)}</b></td>'
                 f'<td><span class="badge {"live" if ok else "planned"}">'
                 f'{"Ready" if ok else "Needs setting up"}</span></td>'
                 f'<td class="meta">{html.escape(note)}</td></tr>')
    return f"""
    <div class="panel">
      <h2>Setup</h2>
      <p class="meta" style="margin:-4px 0 12px">Everything this needs in order to run.
         Anything not ready has to be sorted before the weekly report will work.</p>
      <table class="grid">{rows}</table>
    </div>"""


def _chat_run_bar_html(can_run: bool = True) -> str:
    if _chat_state["running"]:
        done, total = _chat_state["done"], _chat_state["total"]
        progress = f"{done} of {total} chats read" if total else "Collecting this week's chats..."
        return f"""
        <div class="fetch-bar">
            <button class="btn-accent" type="button" disabled>
                <span class="spinner"></span> Running...</button>
            <span class="meta">{progress}. Each one takes a moment, and this page updates
                on its own.</span>
        </div>"""

    last = _chat_state["last_result"]
    if not last:
        status = "The weekly job also runs on a schedule; use this to run it on demand."
    elif last.get("error"):
        status = f'<b>Last run failed:</b> {html.escape(str(last["error"]))}'
    else:
        status = (f'<b>Last run:</b> {last.get("conversations", 0)} conversations, '
                   f'email {html.escape(str(last.get("email_status", "")))}.')

    if not can_run:
        note = ("A new report is produced every week and emailed out automatically."
                 if not last else status)
        return f"""
        <div class="fetch-bar">
            <span class="meta">{note}</span>
        </div>"""

    ready = all(ok for _l, ok, _d in chat_run.status_lines()[:2])  # Chatbase + model
    disabled = "" if ready else "disabled title=\"Finish the setup below first\""
    return f"""
    <div class="fetch-bar">
        <form method="post" action="{CHAT_INSIGHTS}/run" style="margin:0">
            <button class="btn-accent" type="submit" {disabled}
                    {_confirm("Run the weekly analysis and email it?",
                               body="Collects last week's chats, reads through them, then emails the report out. Takes a few minutes.",
                               ok="Run and email", tone="warn")}>&#9654; Run weekly analysis now</button>
        </form>
        <form method="post" action="{CHAT_INSIGHTS}/run" style="margin:0">
            <input type="hidden" name="send_email" value="0">
            <button class="btn-ghost" type="submit" {disabled}
                    {_confirm("Run the weekly analysis without emailing?",
                               body="Does the same work, but sends nothing. The report is kept here and you can email it afterwards.",
                               ok="Run only")}>Run without emailing</button>
        </form>
        <span class="meta">{status}</span>
    </div>"""


def _chat_email_button(run: dict, label: str = "Send by email",
                        can_send: bool = True) -> str:
    """Manual send for a report that already exists -- for a week whose
    scheduled send failed, or one that was run without emailing."""
    if not can_send:
        return ""
    if run.get("status") != chat_db.STATUS_COMPLETE or not run.get("stats"):
        return ""
    if mailer.is_configured_for("weekly_report"):
        extra = f'title="{html.escape(mailer.config_status_for("weekly_report"))}"'
    else:
        extra = f'disabled title="{html.escape(mailer.config_status_for("weekly_report"))}"'
    resend = " again" if run.get("email_status") == "sent" else ""
    attrs = _confirm(
        "Email this report again?" if resend else "Email this report?",
        ok="Send", tone="warn",
        body="It goes to the people set up to receive the weekly report, exactly as it "
              "stands now. Nothing is worked out again." + (
                  " It has already been sent once, so they get a second copy." if resend else ""))
    return (f'<form method="post" action="{CHAT_INSIGHTS}/run/{run["id"]}/email" '
            f'style="display:inline;margin:0">'
            f'<button class="btn-ghost" type="submit" style="padding:5px 12px" {extra} '
            f'{attrs}>&#9993; {html.escape(label)}{resend}</button></form>')


@app.get(CHAT_INSIGHTS, response_class=HTMLResponse)
def chat_insights_home(request: Request):
    runs = chat_db.list_runs(limit=settings.APPROVAL_PAGE_SIZE)
    latest = runs[0] if runs else None

    tiles = ""
    if latest and latest.get("stats"):
        s = latest["stats"]
        tiles = '<div class="stats">' + "".join([
            ui.stat_tile("Chats this week", s.get("total_conversations", 0), variant="accent"),
            ui.stat_tile("Chat could not answer", len(s.get("failures", [])),
                          variant="bad" if s.get("failures") else ""),
            ui.stat_tile("Possible leads", len(s.get("leads", []))),
            ui.stat_tile("Sorted by the chat", f'{s.get("resolution_rate", 0)}%', variant="ok"),
        ]) + '</div>'

    can_send_mail = _can(request, auth.PERM_SEND_MAIL)
    if runs:
        rows = ""
        for r in runs:
            email_badge = {
                "sent": '<span class="badge live">emailed</span>',
                "failed": '<span class="badge confidence-low">email failed</span>',
                "skipped": '<span class="badge planned">not emailed</span>',
            }.get(r.get("email_status") or "", '<span class="badge planned">--</span>')
            status_badge = ('<span class="badge live">complete</span>'
                             if r["status"] == chat_db.STATUS_COMPLETE
                             else f'<span class="badge confidence-low">{html.escape(r["status"])}</span>')
            rows += f"""
            <tr>
              <td><b>{html.escape(r["week_start"])}</b> to {html.escape(r["week_end"])}
                  {' <span class="badge planned">partial</span>' if r.get("truncated") else ''}</td>
              <td>{r.get("conversation_count", 0)}</td>
              <td>{status_badge}</td>
              <td>{email_badge}</td>
              <td class="meta">{html.escape(str(r.get("finished_at") or r.get("started_at") or ""))[:19]}</td>
              <td style="white-space:nowrap">
                  <a class="btn-ghost" style="padding:5px 12px;text-decoration:none;border-radius:8px"
                     href="{CHAT_INSIGHTS}/run/{r['id']}">View report</a>
                  <a class="btn-ghost" style="padding:5px 12px;text-decoration:none;border-radius:8px"
                     href="{CHAT_INSIGHTS}/run/{r['id']}/conversations">Transcripts</a>
                  {_chat_email_button(r, label="Email", can_send=can_send_mail)}
              </td>
            </tr>"""
        table = f"""
        <div class="panel"><h2>Past reports</h2>
          <table class="grid">
            <tr><th>Week</th><th>Chats</th><th>Status</th><th>Email</th><th>Finished</th><th></th></tr>
            {rows}
          </table>
        </div>"""
    else:
        table = ui.empty_state(
            "No reports yet",
            "The first report appears here after the weekly run. You can also start one "
            "now with the button above.")

    body = f"""
    <div class="page-head">
      <div>
        <h1>Chat Insights</h1>
        <p class="subtitle">What customers asked the website chat this week, which questions
           it could not answer, and who left their details wanting a call back.</p>
      </div>
    </div>
    {tiles}
    {_chat_run_bar_html(_can(request, auth.PERM_RUN_JOBS))}
    {_chat_leads_bar()}
    {table}
    {_chat_status_panel() if _can(request, auth.PERM_ADMINISTER) else ""}
    """
    return ui.page_shell(body, title="Chat Insights · Content and Automation",
                          active_module="chat-insights", user=_current_user(request),
                          request=request, crumbs=[("Chat Insights", CHAT_INSIGHTS)],
                          fetch_running=_chat_state["running"])


@app.get(CHAT_INSIGHTS + "/run/{run_id}", response_class=HTMLResponse)
def chat_insights_report(request: Request, run_id: int):
    run = chat_db.get_run(run_id)
    if not run:
        return HTMLResponse(ui.page_shell(
            ui.empty_state("Report not found", "That run does not exist."),
            title="Chat Insights", active_module="chat-insights", crumbs=[("Chat Insights", CHAT_INSIGHTS), ("Not found", "")],
            user=_current_user(request), request=request), status_code=404)

    if run.get("error"):
        inner = f'<div class="flags"><b>This run failed:</b> {html.escape(run["error"])}</div>'
    elif not run.get("report_html"):
        inner = ui.empty_state("No report stored", "This run did not produce a report.")
    else:
        # The stored report is a full HTML document; sandbox it in an iframe so
        # its inline styles cannot leak into the dashboard's own CSS.
        srcdoc = html.escape(run["report_html"], quote=True)
        inner = (f'<iframe srcdoc="{srcdoc}" style="width:100%;height:1400px;border:1px solid '
                  f'var(--line);border-radius:10px;background:#fff" title="Weekly chat report">'
                  f'</iframe>')

    status_text = "not sent yet"
    cls = "dry-run-note"
    if run.get("email_status"):
        status_text = html.escape(run["email_status"])
        if run.get("email_detail"):
            status_text += f' -- {html.escape(run["email_detail"])}'
        cls = "meta" if run["email_status"] == "sent" else "dry-run-note"
    email_note = (f'<div class="{cls}" style="display:flex;gap:12px;align-items:center;'
                   f'flex-wrap:wrap"><span>Email: {status_text}</span>'
                   f'{_chat_email_button(run, can_send=_can(request, auth.PERM_SEND_MAIL))}</div>')

    body = f"""
    <div class="page-head">
      <div>
        <h1>Week of {html.escape(run["week_start"])}</h1>
        <p class="subtitle">
           <a href="{CHAT_INSIGHTS}/run/{run_id}/conversations">{run.get("conversation_count", 0)}
           conversations</a>
           &middot; {html.escape(run["week_start"])} to {html.escape(run["week_end"])}</p>
      </div>
    </div>
    {email_note}
    {inner}
    """
    return ui.page_shell(body, title=f"Chat report {run['week_start']} · Content and Automation",
                          active_module="chat-insights", user=_current_user(request),
                          request=request, crumbs=[
                              ("Chat Insights", CHAT_INSIGHTS),
                              (f"Week of {run['week_start']}", "")])


def _sentiment_badge(analysis: dict) -> str:
    """Sentiment as a badge, reusing the confidence colours: this is the same
    good/bad signal a reviewer is already scanning for elsewhere."""
    sentiment = (analysis.get("sentiment") or "").lower()
    cls = {"negative": "confidence-low", "positive": "confidence-high"}.get(sentiment, "planned")
    return f'<span class="badge {cls}">{html.escape(sentiment or "unrated")}</span>'


@app.get(CHAT_INSIGHTS + "/run/{run_id}/conversations", response_class=HTMLResponse)
def chat_insights_conversations(request: Request, run_id: int):
    """Every conversation in a run, with what the analysis made of it.

    This is the drill-down the emailed report points at: the email is redacted,
    so anyone who needs the customer's actual words comes here for them.
    """
    run = chat_db.get_run(run_id)
    if not run:
        return HTMLResponse(ui.page_shell(
            ui.empty_state("Report not found", "That run does not exist."),
            title="Chat Insights", active_module="chat-insights", crumbs=[("Chat Insights", CHAT_INSIGHTS), ("Not found", "")],
            user=_current_user(request), request=request), status_code=404)

    conversations = chat_db.list_conversations(run_id)
    analyses = {a["conversation_id"]: a for a in chat_db.list_analyses(run_id)}

    rows = ""
    for c in conversations:
        a = analyses.get(c["id"], {})
        flags = ""
        if a.get("bot_failed"):
            flags += ' <span class="badge confidence-low">bot could not answer</span>'
        if a.get("is_lead"):
            flags += ' <span class="badge live">lead</span>'
        if c.get("negative_feedback"):
            flags += ' <span class="badge confidence-low">thumbs down</span>'
        when = datetime.fromtimestamp(int(c["created_at"] or 0), timezone.utc).strftime("%Y-%m-%d %H:%M")
        rows += f"""
        <tr>
          <td class="meta" style="white-space:nowrap">{html.escape(when)}</td>
          <td><b>{html.escape(a.get("topic") or "--")}</b>{flags}
              <div class="meta">{html.escape(a.get("summary") or "")}</div></td>
          <td>{html.escape(a.get("category") or "--")}</td>
          <td>{_sentiment_badge(a)}</td>
          <td class="meta">{c.get("message_count", 0)}</td>
          <td><a class="btn-ghost" style="padding:5px 12px;text-decoration:none;border-radius:8px"
                 href="{CHAT_INSIGHTS}/conversation/{html.escape(c["id"])}">Transcript</a></td>
        </tr>"""

    if conversations:
        table = f"""
        <div class="panel"><h2>Conversations</h2>
          <table class="grid">
            <tr><th>When</th><th>Topic</th><th>Category</th><th>Sentiment</th>
                <th>Messages</th><th></th></tr>
            {rows}
          </table>
        </div>"""
    else:
        table = ui.empty_state("No chats that week",
                                "Nobody used the website chat during this period.")

    body = f"""
    <div class="page-head">
      <div>
        <h1>Conversations, week of {html.escape(run["week_start"])}</h1>
        <p class="subtitle">{len(conversations)} conversations</p>
      </div>
    </div>
    <div class="dry-run-note">Full transcripts, unredacted. The emailed report has customer
       contact details stripped out; this page is where they are kept.</div>
    {table}
    """
    return ui.page_shell(body, title=f"Conversations {run['week_start']} · Content and Automation",
                          active_module="chat-insights", user=_current_user(request),
                          request=request, crumbs=[
                              ("Chat Insights", CHAT_INSIGHTS),
                              (f"Week of {run['week_start']}", f"{CHAT_INSIGHTS}/run/{run_id}"),
                              ("Conversations", "")])


@app.get(CHAT_INSIGHTS + "/conversation/{conversation_id}", response_class=HTMLResponse)
def chat_insights_conversation(request: Request, conversation_id: str):
    """One conversation, message by message, with its analysis alongside."""
    conv = chat_db.get_conversation(conversation_id)
    if not conv:
        return HTMLResponse(ui.page_shell(
            ui.empty_state("Conversation not found",
                            "It may belong to a run whose data has since been cleared."),
            title="Chat Insights", active_module="chat-insights", crumbs=[("Chat Insights", CHAT_INSIGHTS), ("Not found", "")],
            user=_current_user(request), request=request), status_code=404)

    analysis = chat_db.get_analysis(conversation_id) or {}

    messages = ""
    for m in conv["messages"]:
        role = "user" if (m.get("role") or "").lower() == "user" else "assistant"
        who = "Customer" if role == "user" else "Bot"
        # Chatbase records a thumbs down against the message it was given on,
        # so mark that message rather than only the conversation.
        flagged = " flagged" if (m.get("feedback") or "").lower() in ("negative", "thumbs_down") else ""
        messages += (f'<div class="chat-msg {role}{flagged}"><span class="who">{who}</span>'
                     f'{html.escape(m.get("text") or "")}</div>')
    if not messages:
        messages = '<div class="meta">No messages were stored for this conversation.</div>'

    summary_rows = ""
    for label, value in (("Topic", analysis.get("topic")),
                          ("Category", analysis.get("category")),
                          ("Summary", analysis.get("summary")),
                          ("Lead detail", analysis.get("lead_detail")),
                          ("Why it counts as a bot failure", analysis.get("failure_reason"))):
        if value:
            summary_rows += (f'<tr><td class="meta" style="white-space:nowrap">{label}</td>'
                             f'<td>{html.escape(str(value))}</td></tr>')

    badges = _sentiment_badge(analysis)
    if analysis.get("is_lead"):
        badges += ' <span class="badge live">lead</span>'
    if analysis.get("bot_failed"):
        badges += ' <span class="badge confidence-low">bot could not answer</span>'
    if analysis.get("resolved"):
        badges += ' <span class="badge confidence-high">resolved</span>'
    if not analysis:
        badges = ('<span class="badge planned">not analysed</span>'
                  ' <span class="meta">too few messages to be worth a generation</span>')

    when = datetime.fromtimestamp(int(conv.get("created_at") or 0), timezone.utc)
    # The trail depends on whether this conversation still belongs to a run --
    # once a run's data is cleared, Chat Insights is as far up as it goes.
    crumbs = [("Chat Insights", CHAT_INSIGHTS)]
    run = chat_db.get_run(conv["run_id"]) if conv.get("run_id") else None
    if run:
        crumbs.append((f"Week of {run['week_start']}", f"{CHAT_INSIGHTS}/run/{run['id']}"))
        crumbs.append(("Conversations", f"{CHAT_INSIGHTS}/run/{run['id']}/conversations"))
    crumbs.append((analysis.get("topic") or "Conversation", ""))

    body = f"""
    <div class="page-head">
      <div>
        <h1>{html.escape(analysis.get("topic") or "Conversation")}</h1>
        <p class="subtitle">{when:%Y-%m-%d %H:%M} UTC &middot;
           {html.escape(conv.get("source") or "unknown source")} &middot;
           {conv.get("message_count", 0)} messages</p>
      </div>
    </div>
    <div class="panel">
      <div style="margin-bottom:10px">{badges}</div>
      {f'<table class="grid">{summary_rows}</table>' if summary_rows else ''}
    </div>
    <div class="panel">
      <h2>Transcript</h2>
      <div class="transcript">{messages}</div>
    </div>
    """
    return ui.page_shell(body, title="Conversation · Content and Automation",
                          active_module="chat-insights", user=_current_user(request),
                          request=request, crumbs=crumbs)


LEAD_TYPE_LABELS = {
    "form_submission": "Contact form",
    "contact_shared": "Details in chat",
}

# How the hand-off to the sales team went. The first of these is the one that
# costs money: the customer was told their details were on their way and they
# were not, so they are waiting on a call nobody knows to make.
HANDOFF_LABELS = {
    "claimed_not_fired": ("Never sent", "confidence-low"),
    "no_claim": ("No hand-off", "draft"),
    "fired": ("Forwarded", "confidence-high"),
}


def _handoff_badge(lead: dict) -> str:
    label, cls = HANDOFF_LABELS.get(lead.get("handoff_state") or "", ("", ""))
    if not label:
        return ""
    title = html.escape(lead.get("handoff_detail") or "")
    return f' <span class="badge {cls}" title="{title}">{label}</span>' 


def _lead_contact_html(lead: dict) -> str:
    """Contact details as things you can act on -- a phone number you can tap
    and an address you can click, rather than text to retype."""
    bits = []
    if lead.get("contact_name"):
        bits.append(f'<b>{html.escape(lead["contact_name"])}</b>')
    if lead.get("contact_phone"):
        phone = html.escape(lead["contact_phone"])
        bits.append(f'<a href="tel:{phone.replace(" ", "")}">{phone}</a>')
    if lead.get("contact_email"):
        email = html.escape(lead["contact_email"])
        bits.append(f'<a href="mailto:{email}">{email}</a>')
    if not bits:
        return '<span class="lead-none">no contact details</span>'
    return '<div class="lead-contact">' + '<br>'.join(bits) + '</div>'


# How each outcome reads on the page, and whether it is a problem. Only one of
# these is: the other four are the mailing working as configured, and colouring
# a quiet Sunday red teaches people to ignore the colour.
DIGEST_STATUS = {
    chat_db.EMAIL_SENT: ("Sent", "live"),
    chat_db.EMAIL_FAILED: ("Not sent -- send failed", "confidence-low"),
    chat_db.EMAIL_NO_LEADS: ("No leads to send", "planned"),
    chat_db.EMAIL_NO_RECIPIENTS: ("No recipients configured", "planned"),
    chat_db.EMAIL_NOT_REQUESTED: ("Ran without emailing", "planned"),
}


def _digest_row_html(day: str, digest: dict | None, untracked: bool = False) -> str:
    """One morning, in one of three states.

    A day with no run is the row worth having -- but only once there is a run
    to compare it against. Days before the first recorded one are shown greyed
    rather than red: nothing failed then, we simply were not watching.
    """
    if not digest and untracked:
        return f"""
        <tr>
          <td class="meta" style="white-space:nowrap">{html.escape(day)}</td>
          <td><span class="badge planned">Not tracked</span></td>
          <td class="meta">--</td><td class="meta">--</td>
          <td class="meta">Before this dashboard started recording the morning email.</td>
        </tr>"""
    if not digest:
        return f"""
        <tr>
          <td class="meta" style="white-space:nowrap">{html.escape(day)}</td>
          <td><span class="badge confidence-low">Did not run</span></td>
          <td class="meta">--</td><td class="meta">--</td>
          <td class="meta">No record of this morning's job. Use
              <b>Send the morning email</b> to cover it now.</td>
        </tr>"""

    label, badge = DIGEST_STATUS.get(digest.get("email_status") or "",
                                      (digest.get("email_status") or "unknown", "planned"))
    manual = ""
    if digest.get("run_trigger") in (chat_db.TRIGGER_MANUAL, chat_db.TRIGGER_COMMAND_LINE):
        who = digest.get("triggered_by") or "someone"
        manual = f' <span class="badge role">by hand &middot; {html.escape(who)}</span>'
    waiting = ""
    if digest.get("waiting"):
        waiting = (f'<div class="meta">{digest["waiting"]} promised a callback '
                    f'the hand-off never made</div>')
    detail = digest.get("email_detail") or ""
    if digest.get("error"):
        detail = (detail + " " if detail else "") + digest["error"]
    ran = (digest.get("ran_at") or "")[:16].replace("T", " ")
    return f"""
    <tr>
      <td class="meta" style="white-space:nowrap">{html.escape(day)}
          <div class="meta">ran {html.escape(ran)}</div></td>
      <td><span class="badge {badge}">{html.escape(label)}</span>{manual}</td>
      <td><b>{digest.get("reported", 0)}</b>{waiting}</td>
      <td class="meta">{html.escape(digest.get("recipients") or "--")}</td>
      <td class="meta">{html.escape(detail)}</td>
    </tr>"""


def _lead_digest_panel(can_send: bool = True) -> str:
    """The last two weeks of morning emails, and the button to run one now.

    Worth its own panel rather than a line in the job log. The leads list above
    says who needs calling; this says whether anyone was told. A morning that
    silently did not run looks exactly like a morning with no leads unless the
    days are laid out and the gap is visible.
    """
    days = chat_daily.coverage(14)
    missing = [d["day"] for d in days if not d["digest"] and not d["untracked"]]
    recipients = mailer.recipients_for("daily_leads")

    if missing:
        # Named rather than counted: "3 mornings" sends nobody anywhere, but a
        # date is something you can go and cover.
        shown = ", ".join(missing[:4]) + (" and others" if len(missing) > 4 else "")
        banner = (f'<div class="flags"><b>No lead email went out for {shown}.</b> '
                   f'Leads from those days are still in the list above -- nobody was '
                   f'sent them. Run it now to cover the most recent day.</div>')
    elif any(d["digest"] for d in days):
        banner = ('<div class="meta">Every morning since tracking started is accounted '
                   'for.</div>')
    else:
        banner = ('<div class="meta">No morning email has run since this dashboard '
                   'started recording them. The first scheduled run will appear here.</div>')

    if recipients:
        who = ", ".join(recipients)
        confirm_body = (f"Fetches yesterday's conversations, finds the leads and emails "
                         f"the call list to {who}. Takes a few seconds. Safe to run twice "
                         f"-- they would simply get the same list again.")
    else:
        who = "nobody -- no daily_leads recipients are configured"
        confirm_body = ("No recipients are configured for the daily lead digest, so "
                         "nothing will be emailed. The run still fetches yesterday's "
                         "conversations and records the leads it finds.")

    send_button = "" if not can_send else f"""
        <form method="post" action="{CHAT_INSIGHTS}/leads/run-daily" style="margin:0">
          <button class="btn-accent" type="submit"
                  {_confirm("Send the morning lead email now?", body=confirm_body,
                             ok="Run it now", tone="warn")}>&#9993; Send the morning email</button>
        </form>"""

    rows = "".join(_digest_row_html(d["day"], d["digest"], d["untracked"]) for d in days)
    return f"""
    <div class="panel">
      <div class="page-head" style="margin-bottom:10px">
        <div>
          <h2 style="margin:0">Morning lead emails</h2>
          <p class="subtitle" style="margin:4px 0 0">One per day, covering the day
             before. Goes to {html.escape(who)}.</p>
        </div>
        {send_button}
      </div>
      {banner}
      <table class="grid">
        <tr><th>Day covered</th><th>Email</th><th>Leads</th><th>To</th><th>Detail</th></tr>
        {rows}
      </table>
    </div>"""


@app.get(CHAT_INSIGHTS + "/leads", response_class=HTMLResponse)
def chat_insights_leads(request: Request, lead_type: str = "", category: str = "",
                         status: str = "", handoff: str = "", days: int = 0):
    """Every lead found in the chats, whether or not the CRM ever heard about it.

    This list is deliberately independent of the Chatbase action that feeds
    Zapier: when that action does not fire, nothing downstream knows a lead
    existed, so the only reliable record is the one built from the conversation
    itself.
    """
    since = None
    if days:
        since = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp())

    rows_data = chat_db.list_leads(lead_type=lead_type, category=category,
                                    status=status, since=since, limit=500)
    if handoff:
        rows_data = [l for l in rows_data if (l.get("handoff_state") or "") == handoff]
    by_handoff = chat_db.lead_counts_by("handoff_state")
    by_type = chat_db.lead_counts_by("lead_type")
    by_status = chat_db.lead_counts_by("status")
    by_category = chat_db.lead_counts_by("category")

    tiles = '<div class="stats">' + "".join([
        ui.stat_tile("Leads", sum(by_type.values()), variant="accent"),
        ui.stat_tile("Never sent to sales", by_handoff.get("claimed_not_fired", 0),
                      variant="bad" if by_handoff.get("claimed_not_fired") else "",
                      href=f"{CHAT_INSIGHTS}/leads?handoff=claimed_not_fired"),
        ui.stat_tile("Forwarded OK", by_handoff.get("fired", 0), variant="ok"),
        ui.stat_tile("Not yet actioned", by_status.get("new", 0)),
    ]) + "</div>"

    def chip(label: str, key: str, value: str, count: int | None = None) -> str:
        params = {"lead_type": lead_type, "category": category, "status": status,
                   "handoff": handoff, "days": days or ""}
        params[key] = value
        query = urlencode({k: v for k, v in params.items() if v})
        current = {"lead_type": lead_type, "category": category,
                    "status": status, "handoff": handoff}
        selected = current[key] == value
        style = ("background:var(--blue-700);color:#fff;border-color:var(--blue-700)"
                  if selected else "")
        suffix = f" ({count})" if count is not None else ""
        return (f'<a class="btn-ghost" style="padding:5px 12px;text-decoration:none;'
                f'border-radius:999px;{style}" href="{CHAT_INSIGHTS}/leads?{query}">'
                f'{html.escape(label)}{suffix}</a>')

    type_chips = chip("All types", "lead_type", "") + "".join(
        chip(LEAD_TYPE_LABELS.get(t, t), "lead_type", t, n) for t, n in by_type.items())
    status_chips = chip("Any status", "status", "") + "".join(
        chip(st.title(), "status", st, n) for st, n in by_status.items())
    category_chips = chip("All categories", "category", "") + "".join(
        chip(c.replace("_", " "), "category", c, n) for c, n in by_category.items() if c)
    handoff_chips = chip("Any", "handoff", "") + "".join(
        chip(HANDOFF_LABELS.get(h, (h, ""))[0], "handoff", h, n)
        for h, n in sorted(by_handoff.items(),
                            key=lambda kv: list(HANDOFF_LABELS).index(kv[0])
                            if kv[0] in HANDOFF_LABELS else 9) if h)

    rows = ""
    for lead in rows_data:
        when = datetime.fromtimestamp(int(lead["created_at"] or 0), timezone.utc)
        lead_status = lead.get("status") or "new"
        crm = ""
        if lead.get("lead_type") == "form_submission" and lead.get("crm_status") != "present":
            # The only type where a hand-off was supposed to happen.
            crm = ' <span class="badge confidence-low">check CRM</span>'
        rows += f"""
        <tr>
          <td class="meta" style="white-space:nowrap">{when:%Y-%m-%d %H:%M}</td>
          <td><span class="badge {lead["lead_type"]}">
                {html.escape(LEAD_TYPE_LABELS.get(lead["lead_type"], lead["lead_type"]))}</span>
              {_handoff_badge(lead)}{crm}
              {f'<div class="meta" style="max-width:34ch">&ldquo;{html.escape(lead["claim_excerpt"])}&rdquo;</div>'
                if lead.get("claim_excerpt") and lead.get("handoff_state") == "claimed_not_fired" else ''}</td>
          <td>{_lead_contact_html(lead)}</td>
          <td><b>{html.escape(lead.get("topic") or "--")}</b>
              <div class="meta">{html.escape(lead.get("detail") or "")}</div></td>
          <td class="meta">{html.escape((lead.get("category") or "").replace("_", " "))}</td>
          <td><span class="badge status-{html.escape(lead_status)}">{html.escape(lead_status)}</span>
              {f'<div class="meta">{html.escape(lead.get("owner") or "")}</div>' if lead.get("owner") else ''}</td>
          <td style="white-space:nowrap">
            <a class="btn-ghost" style="padding:5px 10px;text-decoration:none;border-radius:8px"
               href="{CHAT_INSIGHTS}/conversation/{html.escape(lead["conversation_id"])}">Chat</a>
            <form method="post" action="{CHAT_INSIGHTS}/leads/{html.escape(lead["conversation_id"])}/status"
                  style="display:inline-flex;gap:4px;margin:0">
              <select name="status" style="padding:4px 6px;font-size:12px">
                {"".join(f'<option value="{s}"{" selected" if s == lead_status else ""}>{s}</option>'
                          for s in chat_db.LEAD_STATUSES)}
              </select>
              <button class="btn-ghost" type="submit" style="padding:5px 10px"
                      {_confirm("Change this lead's status?",
                                 body="Recorded against the lead with your name and the time. It does not notify anyone.",
                                 what=(lead.get("contact_name") or lead.get("contact_phone")
                                        or lead.get("contact_email") or lead["conversation_id"]),
                                 ok="Save")}>Save</button>
            </form>
          </td>
        </tr>"""

    if rows_data:
        table = f"""
        <div class="panel">
          <table class="grid">
            <tr><th>When</th><th>Type</th><th>Contact</th><th>Wants</th><th>Category</th>
                <th>Status</th><th></th></tr>
            {rows}
          </table>
        </div>"""
    elif sum(by_type.values()):
        table = ui.empty_state("No leads match these filters", "Clear a filter to see more.")
    else:
        table = ui.empty_state(
            "No leads yet",
            "A chat becomes a lead when the customer leaves a phone number or an email "
            "address. Use “Check for leads” to look through the chats already saved.")

    # Reviewers may scan; only somebody who can send mail is told that alerts
    # go out, because for anyone else they do not.
    sync_note = ("Looks through every chat saved here for a phone number or an email "
                  "address. Anything new is added to the list below.")
    if _can(request, auth.PERM_SEND_MAIL):
        sync_note += " New leads also trigger an alert email if alerts are switched on."
    sync_button = "" if not _can(request, auth.PERM_FIND_LEADS) else f"""
      <form method="post" action="{CHAT_INSIGHTS}/leads/sync" style="margin:0">
        <button class="btn-accent" type="submit"
                {_confirm("Check the saved chats for leads?", body=sync_note,
                           ok="Check now")}>&#8635; Check for leads</button>
      </form>"""

    body = f"""
    <div class="page-head">
      <div>
        <h1>Leads</h1>
        <p class="subtitle">Customers who left a phone number or an email address in the
           website chat, including the ones that never reached the sales inbox.</p>
      </div>
      {sync_button}
    </div>
    {tiles}
    <div class="panel" style="display:flex;flex-direction:column;gap:8px">
      <div style="display:flex;gap:6px;flex-wrap:wrap;align-items:center">
        <span class="meta" style="width:74px">Type</span>{type_chips}</div>
      <div style="display:flex;gap:6px;flex-wrap:wrap;align-items:center">
        <span class="meta" style="width:74px">Status</span>{status_chips}</div>
      <div style="display:flex;gap:6px;flex-wrap:wrap;align-items:center">
        <span class="meta" style="width:74px">Category</span>{category_chips}</div>
      <div style="display:flex;gap:6px;flex-wrap:wrap;align-items:center">
        <span class="meta" style="width:74px">Hand-off</span>{handoff_chips}</div>
    </div>
    {table}
    {_lead_digest_panel(_can(request, auth.PERM_SEND_MAIL))
      if _can(request, auth.PERM_VIEW_MAIL_LOG) else ""}
    """
    return ui.page_shell(body, title="Leads \u00b7 Content and Automation",
                          active_module="chat-insights", user=_current_user(request),
                          request=request, crumbs=[("Chat Insights", CHAT_INSIGHTS),
                                                    ("Leads", "")])


@app.post(CHAT_INSIGHTS + "/leads/sync")
def chat_insights_leads_sync(request: Request):
    """Rebuilds the lead list from stored conversations, then alerts on anything
    new. Safe to press at any time: detection fields are refreshed, but status,
    owner and notes a human set are left alone.

    Open to reviewers, because the scan itself only re-reads chats already
    held here and updates a list on the same page. The alert email is the one
    part that leaves the building, so it is sent only for somebody who may
    send mail -- and a lead that goes un-alerted stays that way, so the next
    admin scan or the overnight job still picks it up.
    """
    denied = _denied(request, auth.PERM_FIND_LEADS, f"{CHAT_INSIGHTS}/leads")
    if denied:
        return denied
    counts = chat_leads.sync()
    msg = (f"Checked every stored chat. {counts['total']} lead(s) on the list "
           f"({counts['contact_shared']} left details in the chat itself).")
    if counts.get("removed"):
        msg += f" Removed {counts['removed']} with no contact details."

    if _can(request, auth.PERM_SEND_MAIL):
        alerted = chat_alerts.send_lead_alerts()
        if alerted.get("sent"):
            msg += f" {alerted['sent']} alert(s) emailed."
    return _redirect(f"{CHAT_INSIGHTS}/leads", msg=msg)


def _run_lead_digest(username: str):
    try:
        result = chat_daily.run_daily(trigger=chat_db.TRIGGER_MANUAL, triggered_by=username)
        _leads_state["last_result"] = result
    except Exception as e:
        # run_daily swallows the failures it expects, so anything reaching here
        # is unexpected -- and still gets recorded, because a manual run that
        # vanished without trace is exactly what this feature exists to stop.
        logger.exception("Manual lead digest failed")
        _leads_state["last_result"] = {"error": str(e)}
        try:
            chat_db.record_lead_digest({
                "day_end": datetime.now().astimezone().strftime("%Y-%m-%d"),
                "ran_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "run_trigger": chat_db.TRIGGER_MANUAL, "triggered_by": username,
                "email_status": chat_db.EMAIL_FAILED, "error": str(e),
            })
        except Exception:
            logger.exception("Could not record the failed run")
    finally:
        _leads_state["running"] = False


@app.post(CHAT_INSIGHTS + "/leads/run-daily")
def chat_insights_leads_run_daily(request: Request, background_tasks: BackgroundTasks):
    """Runs this morning's lead job on demand.

    The reason this button exists: when the scheduled task does not fire, the
    people who need the leads are the ones who cannot do anything about it.
    Before this they had to wait for someone with a server login. It does the
    same work as the scheduled run and is recorded the same way, marked as
    manual and against whoever pressed it.
    """
    denied = _denied(request, auth.PERM_SEND_MAIL, f"{CHAT_INSIGHTS}/leads")
    if denied:
        return denied
    if _lead_run_in_progress():
        return _redirect(f"{CHAT_INSIGHTS}/leads",
                          err="The lead digest is already running. Refresh in a moment.")
    user = _current_user(request)
    _audit(request, "lead_digest_run", None, detail="Manual run of the daily lead email.")
    _leads_state.update({"running": True, "last_result": None,
                          "started_at": datetime.now(timezone.utc)})
    background_tasks.add_task(_run_lead_digest, user.get("username", ""))
    return _redirect(f"{CHAT_INSIGHTS}/leads",
                      msg="Morning lead email started. Refresh in a few seconds to see the result.")


@app.post(CHAT_INSIGHTS + "/leads/{conversation_id}/status")
def chat_insights_lead_status(request: Request, conversation_id: str,
                               status: str = Form(...)):
    try:
        chat_db.set_lead_status(conversation_id, status,
                                 owner=_current_user(request).get("username", ""))
    except ValueError as e:
        return _redirect(f"{CHAT_INSIGHTS}/leads", err=str(e))
    return _redirect(f"{CHAT_INSIGHTS}/leads", msg=f"Lead marked {status}.")


@app.post(CHAT_INSIGHTS + "/run/{run_id}/email")
def chat_insights_email(request: Request, run_id: int):
    """Sends an already-generated report on demand. Synchronous on purpose: one
    small message, and the whole point of the button is to see whether the mail
    server accepted it."""
    back = f"{CHAT_INSIGHTS}/run/{run_id}"
    denied = _denied(request, auth.PERM_SEND_MAIL, back)
    if denied:
        return denied
    if not mailer.is_configured_for("weekly_report"):
        return _redirect(back, err=mailer.config_status_for("weekly_report"))
    outcome = chat_run.email_report(run_id)
    if outcome["success"]:
        return _redirect(back, msg=outcome["detail"])
    return _redirect(back, err=outcome["detail"])


@app.post(CHAT_INSIGHTS + "/run")
def chat_insights_run(request: Request, background_tasks: BackgroundTasks,
                       send_email: str = Form(default="1")):
    denied = _denied(request, auth.PERM_RUN_JOBS, CHAT_INSIGHTS)
    if denied:
        return denied
    if _chat_state["running"]:
        return _redirect(CHAT_INSIGHTS, err="A chat analysis run is already in progress.")

    chatbase_ok, model_ok = [ok for _l, ok, _d in chat_run.status_lines()[:2]]
    if not chatbase_ok or not model_ok:
        return _redirect(CHAT_INSIGHTS,
                          err="Chatbase and the analysis model must both be configured first.")

    _chat_state.update({"running": True, "started_at": datetime.now(timezone.utc).isoformat(),
                         "total": 0, "done": 0, "last_result": None})
    background_tasks.add_task(_run_chat_job, send_email != "0")
    return _redirect(CHAT_INSIGHTS, msg="Weekly analysis started -- this page will update as it runs.")


# ---------------------------------------------------------------------------
# Settings -- user administration
# ---------------------------------------------------------------------------

def _require_admin(request: Request) -> bool:
    return _can(request, auth.PERM_ADMINISTER)


@app.get("/settings/jobs", response_class=HTMLResponse)
def settings_jobs(request: Request, log: str = "", lines: int = job_logs.DEFAULT_TAIL_LINES):
    """What the scheduled jobs actually did, without an RDP session.

    Admin-only: these logs carry customer questions from the chat analysis and
    the full text of anything that failed, which is more than a reviewer needs.
    """
    user = _current_user(request)
    if not _require_admin(request):
        return HTMLResponse(ui.page_shell(
            ui.empty_state("Admins only", "You need an admin account to see this page."),
            title="Job logs", active_module="settings", user=user, request=request,
            crumbs=[("Administration", ui.SETTINGS_ROOT), ("Job history", "")]),
            status_code=403)

    entries = job_logs.catalogue()
    selected = log if log in job_logs.CATALOGUE else ""
    # Default to the newest log that actually exists, so the page opens on
    # something useful rather than an empty pane.
    if not selected:
        written = [e for e in entries if e["exists"]]
        if written:
            selected = max(written, key=lambda e: e["modified"])["key"]

    rows = ""
    for e in entries:
        if e["exists"]:
            size = f'{e["size"]/1024:.1f} KB' if e["size"] >= 1024 else f'{e["size"]} bytes'
            state = f'<span class="badge live">written</span>'
            when = e["modified"].strftime("%Y-%m-%d %H:%M UTC")
        else:
            size, when = "--", "--"
            state = '<span class="badge planned">never written</span>'
        active = ' style="background:var(--blue-100)"' if e["key"] == selected else ""
        rows += f"""
        <tr{active}>
          <td><b>{html.escape(e["label"])}</b>
              <div class="meta">{html.escape(e["written_by"])}</div></td>
          <td>{state}</td>
          <td class="meta">{size}</td>
          <td class="meta">{html.escape(when)}</td>
          <td><a class="btn-ghost" style="padding:5px 12px;text-decoration:none;border-radius:8px"
                 href="/settings/jobs?log={e["key"]}&lines={lines}">View</a></td>
        </tr>"""

    viewer = ""
    if selected:
        label = job_logs.CATALOGUE[selected][0]
        result = job_logs.tail(selected, lines)
        if result["missing"]:
            viewer = ui.empty_state(
                f"Nothing recorded for {label}",
                "This job has not finished a run yet, so there is nothing to show.")
        else:
            note = ('<span class="meta">showing the end of the file only</span>'
                    if result["truncated"] else "")
            body_text = html.escape(result["text"]) or "(the file is empty)"
            viewer = f"""
            <div class="panel">
              <div class="card-header" style="margin-bottom:10px">
                <h2>{html.escape(label)} <span class="meta">last {lines} lines</span></h2>
                <div style="display:flex;gap:8px;align-items:center">
                  {note}
                  <a class="btn-ghost" style="padding:5px 12px;text-decoration:none;border-radius:8px"
                     href="/settings/jobs?log={selected}&lines={min(lines * 5, job_logs.MAX_TAIL_LINES)}">More lines</a>
                  <a class="btn-ghost" style="padding:5px 12px;text-decoration:none;border-radius:8px"
                     href="/settings/jobs?log={selected}&lines={lines}">Refresh</a>
                </div>
              </div>
              <pre class="logview">{body_text}</pre>
            </div>"""

    body = f"""
    <div class="page-head">
      <div><h1>Job history</h1>
      <p class="subtitle">A record of what the automatic overnight jobs did. If a job has
         nothing here at all, it never started.</p></div>
    </div>
    <div class="panel">
      <h2>Logs</h2>
      <table class="grid">
        <tr><th>Job</th><th>State</th><th>Size</th><th>Last written</th><th></th></tr>
        {rows}
      </table>
    </div>
    {viewer}
    """
    return ui.page_shell(body, title="Job history · Content and Automation",
                          active_module="settings", user=user, request=request,
                          crumbs=[("Administration", ui.SETTINGS_ROOT), ("Job history", "")])


ACTION_LABELS = {
    "approved": "Approved",
    "rejected": "Rejected",
    "edited": "Edited the description",
    "published": "Published to Odoo",
    "publish_failed": "Publish failed",
    "reopened": "Re-opened",
    "regenerated": "Regenerated",
    "user_created": "Created a user",
    "user_disabled": "Disabled a user",
    "user_enabled": "Enabled a user",
    "password_reset": "Reset a password",
    "images_added": "Attached images",
    "images_removed": "Removed an image",
    "images_reclaimed": "Reclaimed staged images",
}
ACTION_BADGES = {
    "approved": "confidence-high", "published": "live",
    "rejected": "confidence-low", "publish_failed": "confidence-low",
}


@app.get("/settings/audit", response_class=HTMLResponse)
def settings_audit(request: Request, actor: str = "", action: str = "", page: int = 1):
    """Who did what, and when.

    Content approved here goes out to a live public site, and until now a row
    recorded the decision and the time but not the person. Append-only: nothing
    in this dashboard edits or deletes an entry.
    """
    user = _current_user(request)
    if not _require_admin(request):
        return HTMLResponse(ui.page_shell(
            ui.empty_state("Admins only", "You need an admin account to see this page."),
            title="Audit trail", active_module="settings", user=user, request=request,
            crumbs=[("Administration", ui.SETTINGS_ROOT), ("Activity log", "")]),
            status_code=403)

    page = max(1, page)
    page_size = settings.APPROVAL_PAGE_SIZE
    entries = review_queue.list_audit(actor=actor, action=action,
                                       limit=page_size, offset=(page - 1) * page_size)
    total = review_queue.count_audit(actor=actor, action=action)

    def option(value: str, label: str, selected: str) -> str:
        return (f'<option value="{html.escape(value)}"'
                f'{" selected" if value == selected else ""}>{html.escape(label)}</option>')

    filters = f"""
    <form class="filter-bar" method="get" action="/settings/audit">
      <label>Person
        <select name="actor">
          {option("", "Anyone", actor)}
          {"".join(option(a, a, actor) for a in review_queue.audit_actors())}
        </select>
      </label>
      <label>Action
        <select name="action">
          {option("", "Anything", action)}
          {"".join(option(a, ACTION_LABELS.get(a, a), action)
                    for a in review_queue.AUDIT_ACTIONS)}
        </select>
      </label>
      <button class="btn-ghost" type="submit">Filter</button>
      <a class="btn-ghost" style="padding:7px 14px;text-decoration:none;border-radius:8px"
         href="/settings/audit">Clear</a>
    </form>"""

    rows = ""
    for e in entries:
        when = (e.get("at") or "")[:19].replace("T", " ")
        badge_cls = ACTION_BADGES.get(e.get("action"), "planned")
        target = html.escape(e.get("title") or "")
        if e.get("row_id"):
            target = (f'<a href="{CONTENT_AGENT}/history?q={e["row_id"]}">{target}</a>'
                       if target else f'#{e["row_id"]}')
        rows += f"""
        <tr>
          <td class="meta" style="white-space:nowrap">{html.escape(when)} UTC</td>
          <td><b>{html.escape(e.get("actor") or "")}</b></td>
          <td><span class="badge {badge_cls}">
              {html.escape(ACTION_LABELS.get(e.get("action"), e.get("action") or ""))}</span></td>
          <td>{target}<div class="meta">{html.escape(e.get("product_id") or "")}</div></td>
          <td class="meta">{html.escape(e.get("detail") or "")}</td>
        </tr>"""

    if entries:
        table = f"""
        <div class="panel">
          <table class="grid">
            <tr><th>When</th><th>Who</th><th>Action</th><th>Item</th><th>Detail</th></tr>
            {rows}
          </table>
        </div>"""
    elif total == 0 and not (actor or action):
        table = ui.empty_state(
            "Nothing here yet",
            "Entries appear as people approve, turn down, edit and publish descriptions.")
    else:
        table = ui.empty_state("No matches", "No entries match those filters.")

    pages = max(1, (total + page_size - 1) // page_size)
    pager = ""
    if pages > 1:
        query = urlencode({k: v for k, v in
                            {"actor": actor, "action": action}.items() if v})
        prefix = f"/settings/audit?{query}&" if query else "/settings/audit?"
        back = (f'<a class="btn-ghost" style="padding:6px 12px;text-decoration:none;'
                 f'border-radius:8px" href="{prefix}page={page-1}">Newer</a>') if page > 1 else ""
        fwd = (f'<a class="btn-ghost" style="padding:6px 12px;text-decoration:none;'
                f'border-radius:8px" href="{prefix}page={page+1}">Older</a>') if page < pages else ""
        pager = (f'<div style="display:flex;gap:8px;align-items:center;margin-top:12px">'
                 f'{back}{fwd}<span class="meta">Page {page} of {pages}</span></div>')

    body = f"""
    <div class="page-head">
      <div><h1>Activity log</h1>
      <p class="subtitle">{total} thing{"s" if total != 1 else ""} people have done here.
         Every description approved, turned down, edited or sent live, and who did it.</p></div>
    </div>
    {filters}
    {table}
    {pager}
    """
    return ui.page_shell(body, title="Activity log \u00b7 Content and Automation",
                          active_module="settings", user=user, request=request,
                          crumbs=[("Administration", ui.SETTINGS_ROOT), ("Activity log", "")])


@app.get("/settings/images", response_class=HTMLResponse)
def settings_images(request: Request):
    """Staged image storage: what is here, what has landed, what can go.

    Admin-only because the actions on it delete files, and because it is the
    place a stuck upload gets diagnosed.
    """
    user = _current_user(request)
    if not _require_admin(request):
        return HTMLResponse(ui.page_shell(
            ui.empty_state("Admins only", "You need an admin account to see this page."),
            title="Images", active_module="settings", user=user, request=request,
            crumbs=[("Administration", ui.SETTINGS_ROOT), ("Photo storage", "")]), status_code=403)

    stats = review_queue.image_stats()
    on_disk = product_images.disk_usage()

    tiles = '<div class="stats">' + "".join([
        ui.stat_tile("Awaiting review", stats["staged"],
                      variant="accent" if stats["staged"] else ""),
        ui.stat_tile("Sent, not verified", stats["unverified"],
                      variant="warn" if stats["unverified"] else ""),
        ui.stat_tile("Verified on Odoo", stats["verified"], variant="ok"),
        ui.stat_tile("Reclaimed", stats["reclaimed"]),
        ui.stat_tile("On disk", product_images.human_bytes(on_disk), small=True),
    ]) + '</div>' 

    # Anything Odoo accepted but that did not read back is the case worth
    # showing by name -- it is the only state where a reviewer thinks the job
    # is done and it is not.
    problems = ""
    stranded = db_images_missing()
    if stranded:
        rows = ""
        for img in stranded[:50]:
            rows += (f'<tr><td><b>{html.escape(img["original_name"])}</b>'
                      f'<div class="meta">SKU {html.escape(str(img["product_id"]))} '
                      f'&middot; queue row {img["row_id"]}</div></td>'
                      f'<td class="meta">'
                      f'{html.escape(str(img.get("published_at") or "")[:19].replace("T", " "))}</td>'
                      f'<td class="meta">{html.escape(img.get("publish_detail") or "")}</td></tr>')
        problems = f"""
        <div class="panel">
          <h2>Sent, but not showing on the product</h2>
          <p class="subtitle" style="margin:-4px 0 12px">These photos were sent, but they are
             not on the product when we look again. Nothing is deleted while a photo is in
             this state. Try sending the description again, or check the product yourself.</p>
          <table class="grid">
            <tr><th>Image</th><th>Sent</th><th>What the check found</th></tr>{rows}
          </table>
        </div>"""

    retention = (f"Staged copies are deleted {settings.IMAGES_RETAIN_DAYS} day(s) after an image "
                  f"is confirmed on the product." if settings.IMAGES_RETAIN_DAYS
                  else "Staged copies are deleted as soon as an image is confirmed on the product.")

    body = f"""
    <div class="page-head">
      <div><h1>Photo storage</h1>
      <p class="subtitle">Product photos held here between being uploaded and appearing on
         the product page. {html.escape(retention)}</p></div>
    </div>
    {tiles}
    <div class="fetch-bar">
      <form method="post" action="/settings/images/verify" style="margin:0">
        <button class="btn-ghost" type="submit"
                {_confirm("Check these images against Odoo?",
                           body="Checks each product to confirm its photos actually arrived. Nothing is deleted by this -- it only marks which photos are safe to clear later.",
                           ok="Verify")}>&#10003; Verify now</button>
      </form>
      <form method="post" action="/settings/images/cleanup" style="margin:0">
        <button class="btn-accent" type="submit"
                {_confirm("Delete the staged copies?",
                           body="Clears the copies held here for photos already confirmed on their products. The products keep theirs. Nothing still waiting or unconfirmed is touched.",
                           ok="Delete files", tone="danger")}>&#128465; Reclaim disk space</button>
      </form>
      <span class="meta">Both run automatically via the <b>Image cleanup</b> scheduled task
         (<code>python -m scripts.reclaim_images</code>). These buttons run the same thing now.
         Verify reads products back off Odoo; only images it confirms become eligible for
         deletion.</span>
    </div>
    {problems}
    """
    return ui.page_shell(body, title="Photo storage · Content and Automation",
                          active_module="settings", user=user, request=request,
                          crumbs=[("Administration", ui.SETTINGS_ROOT), ("Photo storage", "")])


def db_images_missing() -> list[dict]:
    """Published, checked, and not found on the product."""
    return [i for i in review_queue.images_awaiting_verification()
            if i.get("publish_detail") and "not on the product" in i["publish_detail"]]


@app.post("/settings/images/verify")
def settings_images_verify(request: Request):
    if not _require_admin(request):
        return _redirect("/settings/images", err="Admins only.")
    result = product_images.verify_published(odoo_connector)
    if not result["checked"] and not result["unknown"]:
        return _redirect("/settings/images", msg="Nothing was waiting to be verified.")
    parts = [f"{result['verified']} confirmed"]
    if result["missing"]:
        parts.append(f"{result['missing']} not found on the product")
    if result["unknown"]:
        parts.append(f"{result['unknown']} could not be checked (Odoo unreachable)")
    summary = "Verify: " + ", ".join(parts) + "."
    return _redirect("/settings/images",
                      err=summary if (result["missing"] or result["unknown"]) else "",
                      msg="" if (result["missing"] or result["unknown"]) else summary)


@app.post("/settings/images/cleanup")
def settings_images_cleanup(request: Request):
    if not _require_admin(request):
        return _redirect("/settings/images", err="Admins only.")
    result = product_images.reclaim()
    if not result["removed"] and not result["orphans"]:
        return _redirect("/settings/images",
                          msg="Nothing to reclaim -- no verified image has passed its retention window yet.")
    _audit(request, "images_reclaimed",
            detail=f"Reclaimed {result['removed']} staged image file(s), "
                    f"{product_images.human_bytes(result['freed_bytes'])}.")
    note = (f"Reclaimed {result['removed']} staged file(s), "
             f"{product_images.human_bytes(result['freed_bytes'])} freed.")
    if result["orphans"]:
        note += f" Swept {result['orphans']} orphaned file(s)."
    return _redirect("/settings/images", msg=note)


@app.get("/settings/users", response_class=HTMLResponse)
def settings_users(request: Request):
    user = _current_user(request)
    if not _require_admin(request):
        return HTMLResponse(ui.page_shell(
            ui.empty_state("Admins only", "You need an admin account to see this page."),
            title="People · Content and Automation", active_module="settings",
            user=user, request=request,
            crumbs=[("Administration", ui.SETTINGS_ROOT), ("People", "")]), status_code=403)

    me = user["username"]
    mail_ready = mailer.is_configured()

    cards = ""
    for u in auth.list_users():
        name = u["username"]
        is_self = name == me
        email = u.get("email") or ""
        invite = auth.pending_invite(name)
        signed_in = u.get("last_login_at")

        # One line that says where this person is up to, because "never" in a
        # Last sign-in column does not distinguish "invited on Tuesday and has
        # not been in yet" from "set up months ago and forgotten about".
        if not u["active"]:
            state = '<span class="badge planned">No access</span>'
        elif invite and not signed_in:
            state = '<span class="badge draft">Invited, not signed in yet</span>'
        elif not signed_in:
            state = '<span class="badge confidence-low">Never signed in</span>'
        else:
            state = (f'<span class="badge live">Active</span>'
                      f'<span class="meta" style="margin-left:8px">last in '
                      f'{html.escape(str(signed_in))[:16].replace("T", " ")}</span>')

        role_options = "".join(
            f'<option value="{r}"{" selected" if r == u["role"] else ""}>'
            f'{"Admin" if r == auth.ROLE_ADMIN else "Reviewer"}</option>'
            for r in auth.ROLES)
        role_field = (f'<select name="role" disabled title="You cannot change your own role">'
                       f'{role_options}</select>' if is_self else
                       f'<select name="role">{role_options}</select>')

        # Sending a link needs somewhere to send it to.
        if not email:
            link_btn = ('<span class="meta">Add an email address to send them a '
                         'sign-in link.</span>')
        else:
            verb = "Resend invitation" if (invite and not signed_in) else (
                    "Send an invitation" if not signed_in else "Send a reset link")
            link_body = (f"Emails {email} a link to set their own password. "
                          + ("Their current password keeps working until they use it."
                              if signed_in else
                              "They cannot sign in until they use it."))
            if not mail_ready:
                link_body = ("Email is not set up on this server, so nothing will be "
                              "sent -- you will be given the link to pass on yourself.")
            link_btn = (
                f'<form method="post" action="/settings/users/{name}/send-link" '
                f'style="display:inline;margin:0">'
                f'<button class="btn-ghost" type="submit" '
                f'{_confirm(verb + "?", body=link_body, what=email, ok="Send it", tone="warn")}>'
                f'&#9993; {verb}</button></form>')

        toggle_label = "Turn off access" if u["active"] else "Turn access back on"
        toggle_btn = '<span class="meta">This is you.</span>' if is_self else (
            f'<form method="post" action="/settings/users/{name}/toggle" '
            f'style="display:inline;margin:0">'
            f'<button class="reopen" type="submit" '
            f'{_confirm(("Turn off access for this person?" if u["active"] else "Give this person access again?"), what=name, ok=toggle_label, tone="danger" if u["active"] else "go", body=("They are signed out straight away and cannot sign back in. Nothing they have done is deleted, and you can turn it back on at any time." if u["active"] else "They can sign in again from now on, with the password they had before."))}>'
            f'{toggle_label}</button></form>')

        # Deleting asks for the username to be typed as well as confirmed.
        # Everything else on this card can be undone by pressing the opposite
        # button; this cannot, and the cards sit close together.
        delete_btn = "" if is_self else (
            f'<form method="post" action="/settings/users/{name}/delete" '
            f'class="person-delete">'
            f'<input type="text" name="confirm_username" placeholder="Type {html.escape(name, quote=True)} to delete" '
            f'autocomplete="off" spellcheck="false" required>'
            f'<button class="reject" type="submit" '
            f'{_confirm("Delete this account for good?", what=name, ok="Delete", tone="danger", body="This cannot be undone. They are signed out, their account is gone, and the username becomes free for somebody else. What they approved and turned down stays in the activity log under their name. If they are just leaving, turn off access instead -- it keeps the name reserved.")}>'
            f'Delete</button></form>')

        cards += f"""
        <div class="person">
          <div class="person-head">
            <div class="avatar lg">{html.escape(ui.initials(u.get("display_name") or name))}</div>
            <div class="person-id">
              <div class="person-name">{html.escape(u.get("display_name") or name)}
                {' <span class="meta">(you)</span>' if is_self else ''}</div>
              <div class="meta">{html.escape(name)}
                {' &middot; ' + html.escape(email) if email else ' &middot; no email address'}</div>
            </div>
            <div class="person-state">{state}</div>
          </div>

          <form method="post" action="/settings/users/{name}/update" class="person-edit">
            <div><label>Full name<br>
              <input type="text" name="display_name" maxlength="60"
                     value="{html.escape(u.get("display_name") or "", quote=True)}"></label></div>
            <div><label>Email address<br>
              <input type="email" name="email" placeholder="name@bcsands.com.au"
                     value="{html.escape(email, quote=True)}"></label></div>
            <div><label>Role<br>{role_field}</label></div>
            <button class="btn-primary" type="submit"
                    {_confirm("Save these changes?",
                               body="Changes to the name and email address take effect straight away. A change of role applies the next time they load a page.",
                               what=name, ok="Save")}>Save</button>
          </form>

          <div class="person-actions">
            {link_btn}
            <form method="post" action="/settings/users/{name}/password"
                  style="display:inline-flex;gap:6px;margin:0">
              <input type="password" name="new_password" placeholder="Set a password instead"
                     minlength="8" required style="width:190px">
              <button class="btn-ghost" type="submit"
                      {_confirm("Set this password yourself?",
                                 body="Use this when they have no email address. Their current password stops working at once and they are signed out. You will have to tell them the new one, and it cannot be read back here afterwards.",
                                 what=name, ok="Set password", tone="danger")}>Set</button>
            </form>
            {toggle_btn}
          </div>
          {f'<div class="person-danger">{delete_btn}</div>' if delete_btn else ''}
        </div>"""

    mail_note = ("" if mail_ready else
                  '<div class="dry-run-note"><b>Email is not set up on this server.</b> '
                  'Invitations and reset links cannot be sent, so the dashboard will show '
                  'you the link to pass on by hand instead.</div>')

    body = f"""
    <div class="page-head">
      <div><h1>People</h1>
      <p class="subtitle">Who can sign in here. Nobody's password can be looked up, so if
         somebody forgets theirs, send them a link and they set their own.</p></div>
    </div>
    {mail_note}
    <div class="panel">
      <h2>Add somebody</h2>
      <p class="meta" style="margin:-4px 0 12px">Give them an email address and they get an
         invitation to choose their own password. Type a password instead only if they have
         no email address.</p>
      <form method="post" action="/settings/users/create" class="person-edit wide">
        <div><label>Username<br><input type="text" name="username" required
             placeholder="jsmith" autocomplete="off"></label></div>
        <div><label>Full name<br><input type="text" name="display_name"
             placeholder="Jane Smith" maxlength="60"></label></div>
        <div><label>Email address<br><input type="email" name="email"
             placeholder="jane@bcsands.com.au"></label></div>
        <div><label>Role<br>
          <select name="role">
            <option value="reviewer">Reviewer</option>
            <option value="admin">Admin</option>
          </select></label></div>
        <div><label>Password <span class="meta">(optional)</span><br>
          <input type="password" name="password" minlength="8"
                 placeholder="Leave blank to invite" autocomplete="new-password"></label></div>
        <button class="btn-primary" type="submit"
                {_confirm("Add this person?",
                           body="If you gave an email address and left the password blank, they are sent an invitation to choose their own. If you typed a password, they can sign in with it straight away and you will need to tell them what it is.",
                           ok="Add them", tone="go")}>Add them</button>
      </form>
    </div>

    <div class="panel">
      <h2>Everyone with access</h2>
      <div class="people">{cards}</div>
    </div>

    <div class="panel">
      <h2>Change my password</h2>
      <form method="post" action="/account/password" style="display:flex; gap:10px; align-items:flex-end; flex-wrap:wrap;">
        <div><label>Current<br><input type="password" name="current_password"
             autocomplete="current-password" required></label></div>
        <div><label>New<br><input type="password" name="new_password" minlength="8"
             autocomplete="new-password" required></label></div>
        <button class="btn-primary" type="submit"
                {_confirm("Change your password?",
                           body="You stay signed in here, but the old password stops working everywhere else.",
                           ok="Change it")}>Update password</button>
      </form>
    </div>
    """
    return ui.page_shell(body, title="People · Content and Automation", active_module="settings",
                          crumbs=[("Administration", ui.SETTINGS_ROOT), ("People", "")],
                          user=user, request=request)


@app.post("/settings/users/create")
def create_user_route(request: Request, username: str = Form(...),
                       display_name: str = Form(default=""),
                       email: str = Form(default=""),
                       password: str = Form(default=""),
                       role: str = Form(default=auth.ROLE_REVIEWER)):
    """Adds an account.

    An email address and no password means an invitation: they get a link and
    choose their own. That is the better way round -- a password an admin types
    has to reach the person somehow, and every way of doing that is worse than
    a link only they can use. Typing one is still allowed, for somebody who has
    no email address.
    """
    if not _require_admin(request):
        return _redirect("/settings/users", err="Admins only.")
    email = (email or "").strip()
    if not password and not email:
        return _redirect("/settings/users",
                          err="Give an email address to send an invitation to, or type "
                              "a password for them.")
    try:
        auth.create_user(username, password, role=role,
                          display_name=display_name, email=email)
    except ValueError as e:
        return _redirect("/settings/users", err=str(e))

    _audit(request, "user_created",
           detail=f"{username} as {role}" + (f", email {email}" if email else ""))
    if password:
        return _redirect("/settings/users",
                          msg=f"Created {username}. Tell them the password yourself.")
    return _issue_link(request, username, kind=auth.KIND_INVITE, created=True)


def _issue_link(request: Request, username: str, *, kind: str, created: bool = False):
    """Makes a sign-in link, emails it, and says what happened.

    When the email cannot go -- no address on the account, no mail server, no
    configured web address for this dashboard -- the link is put on screen for
    the admin to pass on instead. An invitation that silently fails to send
    leaves an account nobody can use and nobody knows about.
    """
    target = auth.get_user(username)
    if not target:
        return _redirect("/settings/users", err="No such user.")
    actor = _current_user(request).get("username", "")
    token = auth.create_reset_token(username, kind=kind, issued_by=actor)

    if kind == auth.KIND_INVITE:
        outcome = user_mail.send_invite(target, token, invited_by=actor)
    else:
        outcome = user_mail.send_reset(target, token, requested_by_admin=True)

    made = f"Created {username}. " if created else ""
    _audit(request, "invite_sent" if kind == auth.KIND_INVITE else "reset_link_sent",
           detail=f"for {username}: {outcome.get('detail', '')}")
    if outcome.get("success"):
        return _redirect("/settings/users",
                          msg=f"{made}Sent a sign-in link to {target['email']}.")

    link = user_mail.reset_url(token)
    if link:
        # Handed over rather than thrown away: the token is already issued, and
        # this is the only moment it can be read.
        return _redirect("/settings/users",
                          err=f"{made}The email could not be sent "
                              f"({outcome.get('detail', '')}) Give them this link "
                              f"instead: {link}")
    return _redirect("/settings/users",
                      err=f"{made}No link could be made: {outcome.get('detail', '')}")


@app.post("/settings/users/{username}/send-link")
def send_user_link(request: Request, username: str):
    """Emails somebody a link to set a password -- an invitation if they have
    never signed in, a reset if they have."""
    if not _require_admin(request):
        return _redirect("/settings/users", err="Admins only.")
    target = auth.get_user(username)
    if not target:
        return _redirect("/settings/users", err="No such user.")
    kind = auth.KIND_RESET if target.get("last_login_at") else auth.KIND_INVITE
    return _issue_link(request, username, kind=kind)


@app.post("/settings/users/{username}/update")
def update_user_route(request: Request, username: str,
                       display_name: str = Form(default=""),
                       email: str = Form(default=""),
                       role: str = Form(default="")):
    """Edits a person's name, address and role."""
    if not _require_admin(request):
        return _redirect("/settings/users", err="Admins only.")
    target = auth.get_user(username)
    if not target:
        return _redirect("/settings/users", err="No such user.")
    if role and role != target["role"] and username == _current_user(request).get("username"):
        return _redirect("/settings/users",
                          err="You cannot change your own role. Ask another admin.")
    try:
        auth.update_user(username, display_name=display_name, email=email,
                          role=role or None)
    except ValueError as e:
        return _redirect("/settings/users", err=str(e))
    _audit(request, "user_updated",
           detail=f"{username}: name={display_name!r} email={email!r} "
                   f"role={role or target['role']}")
    return _redirect("/settings/users", msg=f"Saved {username}.")


@app.post("/settings/users/{username}/password")
def reset_user_password(request: Request, username: str, new_password: str = Form(...)):
    if not _require_admin(request):
        return _redirect("/settings/users", err="Admins only.")
    try:
        auth.set_password(username, new_password)
    except ValueError as e:
        return _redirect("/settings/users", err=str(e))
    auth.revoke_all_sessions(username)  # force re-login with the new password
    _audit(request, "password_reset", detail=f"for {username}")
    return _redirect("/settings/users", msg=f"Password reset for “{username}”. Their sessions were signed out.")


@app.post("/settings/users/{username}/toggle")
def toggle_user_active(request: Request, username: str):
    if not _require_admin(request):
        return _redirect("/settings/users", err="Admins only.")
    if username == _current_user(request).get("username"):
        return _redirect("/settings/users", err="You can't disable your own account.")
    target = auth.get_user(username)
    if not target:
        return _redirect("/settings/users", err="No such user.")
    # Don't allow locking everyone out of user administration.
    if target["active"] and target["role"] == auth.ROLE_ADMIN and auth.count_admins() <= 1:
        return _redirect("/settings/users", err="That's the only active admin -- promote another first.")
    auth.set_active(username, not target["active"])
    _audit(request, "user_disabled" if target["active"] else "user_enabled",
           detail=username)
    return _redirect("/settings/users",
                      msg=f"{'Disabled' if target['active'] else 'Enabled'} “{username}”.")


@app.post("/settings/users/{username}/delete")
def delete_user_route(request: Request, username: str, confirm_username: str = Form(default="")):
    """Removes an account outright.

    The typed-name confirmation is on top of the usual dialog on purpose. This
    is the one action on the People page that cannot be undone by pressing the
    opposite button, and the cards sit close together -- typing the name is
    cheap, and it makes deleting the wrong person out of a list something you
    have to do deliberately rather than something you can misclick into.
    """
    if not _require_admin(request):
        return _redirect("/settings/users", err="Admins only.")
    if username == _current_user(request).get("username"):
        return _redirect("/settings/users", err="You cannot delete your own account.")
    if confirm_username.strip().lower() != username.strip().lower():
        return _redirect("/settings/users",
                          err=f"Type {username} exactly to confirm the deletion.")
    try:
        removed = auth.delete_user(username)
    except ValueError as e:
        return _redirect("/settings/users", err=str(e))
    _audit(request, "user_deleted",
           detail=f"{username} ({removed.get('role', '')}"
                   + (f", {removed['email']}" if removed.get("email") else "") + ")")
    return _redirect("/settings/users",
                      msg=f"Deleted {username}. What they did is still in the activity log.")


@app.post("/account/password")
def change_own_password(request: Request, current_password: str = Form(...),
                         new_password: str = Form(...)):
    user = _current_user(request)
    if not auth.authenticate(user["username"], current_password):
        return _redirect("/settings/users", err="Current password is incorrect.")
    try:
        # keep_sessions, because the session doing this is the person's own:
        # being signed out of the tab you just used to change your password
        # reads as the change having failed. Every OTHER way a password
        # changes -- an admin setting one, a reset link -- does sign them out,
        # which is the point of those.
        auth.set_password(user["username"], new_password, keep_sessions=True)
    except ValueError as e:
        return _redirect("/settings/users", err=str(e))
    _audit(request, "own_password_changed", detail=user["username"])
    return _redirect("/settings/users",
                      msg="Your password was updated. You are still signed in here; "
                          "anywhere else will need the new one.")


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Site health
#
# Reviewers and admins both see the page. Changing settings, running a check
# and sending a test email are admin-only, like every other job and mail
# control on the dashboard.
# ---------------------------------------------------------------------------
MONITOR_PATH = "/site-health"
_monitor_state: dict[str, Any] = {"running": False, "started_at": None}
MONITOR_STALE_AFTER = timedelta(minutes=10)

MONITOR_OUTCOMES = {
    "ok": ("Up", "live"),
    "slow": ("Slow", "draft"),
    "down": ("Down", "confidence-low"),
    "server_error": ("Server error", "confidence-low"),
    "page_error": ("Page error", "confidence-low"),
    "wrong_content": ("Wrong page", "confidence-low"),
    "blocked": ("Blocked by Cloudflare", "planned"),
    "cert_invalid": ("Certificate problem", "confidence-low"),
    "monitor_offline": ("Could not check", "planned"),
}
MONITOR_FAMILIES = {"down": "Down", "error": "Problem", "slow": "Slow",
                    "blocked": "Blocked by Cloudflare"}
MONITOR_ROUTES = {"direct": "Direct to the server",
                  "public": "Through Cloudflare, as customers reach it"}


def _monitor_card_stats() -> str:
    try:
        current = monitor_store.get_settings()
        enabled = [t for t in current["targets"] if t.get("enabled")]
        latest = [monitor_store.latest_check(t["key"]) for t in enabled]
        open_count = len(monitor_store.open_incidents())
    except Exception:
        logger.exception("Could not read site monitor state for the overview")
        return ""
    checked = [c for c in latest if c]
    if not checked:
        return ('<div class="modstat"><div class="meta">No checks yet &middot; '
                'waiting for the first run</div></div>')
    up = sum(1 for c in checked if c["outcome"] in ("ok", "slow"))
    last = max(c["checked_at"] for c in checked)
    return (f'<div class="modstat"><div><b>{up}/{len(enabled)}</b>Pages up</div>'
            f'<div><b>{open_count}</b>Open problems</div>'
            f'<div><b>{html.escape(monitor_emails.short_time(last))}</b>Last check</div></div>')


def _sparkline(points: list[dict], slow_ms: int) -> str:
    """Load times over the last day, with the slow threshold drawn in and any
    failed check marked. Inline SVG: one small chart per page, no library."""
    usable = [p for p in points if p["outcome"] != "monitor_offline" and p["ms"] is not None]
    if len(usable) < 2:
        return '<div class="meta">A chart appears once there are a few checks.</div>'
    width, height, pad = 300, 56, 4
    top = max([p["ms"] for p in usable] + [slow_ms * 1.25])
    step = (width - 2 * pad) / (len(usable) - 1)

    def y(ms):
        return height - pad - (min(ms, top) / top) * (height - 2 * pad)

    line = " ".join(f"{pad + i * step:.1f},{y(p['ms']):.1f}" for i, p in enumerate(usable))
    dots = "".join(f'<circle cx="{pad + i * step:.1f}" cy="{y(p["ms"]):.1f}" r="2.5" fill="#C62828"/>'
                   for i, p in enumerate(usable) if p["outcome"] not in ("ok", "slow"))
    slow_y = y(slow_ms)
    return (f'<svg viewBox="0 0 {width} {height}" width="100%" height="{height}" '
            f'preserveAspectRatio="none" role="img" aria-label="Load times over the last 24 hours">'
            f'<line x1="0" x2="{width}" y1="{slow_y:.1f}" y2="{slow_y:.1f}" stroke="#E0A500" '
            f'stroke-dasharray="4 3" stroke-width="1" vector-effect="non-scaling-stroke"/>'
            f'<polyline points="{line}" fill="none" stroke="#0A5FBF" stroke-width="1.6" '
            f'vector-effect="non-scaling-stroke"/>{dots}</svg>')


def _uptime_text(value) -> str:
    return "No data yet" if value is None else f"{value:.1f}%"


def _monitor_settings_form(current: dict) -> str:
    targets = list(current["targets"]) + [{"key": "", "label": "", "url": "", "route": "direct",
                                           "session_param": "", "must_contain": [],
                                           "enabled": False}]
    rows = ""
    for i, t in enumerate(targets):
        options = "".join(
            f'<option value="{r}"{" selected" if r == t.get("route") else ""}>{html.escape(label)}</option>'
            for r, label in MONITOR_ROUTES.items())
        placeholder = "" if t.get("label") else "Add a page"
        rows += f"""
          <tr>
            <td><input type="hidden" name="t{i}_key" value="{html.escape(t.get('key', ''), quote=True)}">
                <input type="checkbox" name="t{i}_enabled" {"checked" if t.get("enabled") else ""}
                       title="Check this page"></td>
            <td><input type="text" name="t{i}_label" maxlength="80" placeholder="{placeholder}"
                       value="{html.escape(t.get('label', ''), quote=True)}"></td>
            <td><input type="url" name="t{i}_url" placeholder="https://"
                       value="{html.escape(t.get('url', ''), quote=True)}"></td>
            <td><select name="t{i}_route">{options}</select></td>
            <td><textarea name="t{i}_contains" rows="2" placeholder="One per line">{html.escape(chr(10).join(t.get('must_contain') or []))}</textarea></td>
            <td><input type="text" name="t{i}_session" maxlength="32" style="width:90px"
                       value="{html.escape(t.get('session_param', ''), quote=True)}"></td>
          </tr>"""
    return f"""
    <div class="panel">
      <h2>Settings</h2>
      <p class="meta" style="margin:-4px 0 14px">Only admins can see and change these. Changes apply
         from the next check.</p>
      <form method="post" action="{MONITOR_PATH}/settings">
        <input type="hidden" name="target_count" value="{len(targets)}">
        <div class="person-edit wide">
          <div style="flex:1 1 100%"><label>Send alerts to<br>
            <textarea name="recipients" rows="2" placeholder="One email address per line">{html.escape(chr(10).join(current['recipients']))}</textarea></label></div>
          <div><label>Quiet from<br><input type="time" name="quiet_start" value="{current['quiet_start']}" required></label></div>
          <div><label>Quiet until<br><input type="time" name="quiet_end" value="{current['quiet_end']}" required></label></div>
          <div><label>Morning summary at<br><input type="time" name="morning_summary_at" value="{current['morning_summary_at']}" required></label></div>
          <div><label>Office internet address<br><input type="text" name="office_ip" placeholder="220.233.203.122"
               value="{html.escape(current.get('office_ip', ''), quote=True)}"></label></div>
        </div>
        <p class="meta" style="margin:8px 0 0">The office internet address is the one the Cloudflare
           rule lets through. It is only used to explain a "Blocked by Cloudflare" alert.</p>
        <div class="person-edit wide" style="margin-top:12px">
          <div><label>Slow when over (ms)<br><input type="number" name="slow_ms" min="500" max="60000" value="{current['slow_ms']}" required></label></div>
          <div><label>Slow checks in a row<br><input type="number" name="slow_after" min="1" max="20" value="{current['slow_after']}" required></label></div>
          <div><label>Failed checks in a row<br><input type="number" name="down_after" min="1" max="20" value="{current['down_after']}" required></label></div>
          <div><label>Problem checks in a row<br><input type="number" name="issue_after" min="1" max="20" value="{current['issue_after']}" required></label></div>
          <div><label>Cloudflare blocks in a row<br><input type="number" name="blocked_after" min="1" max="20" value="{current['blocked_after']}" required></label></div>
          <div><label>Remind while down every (min)<br><input type="number" name="reminder_minutes" min="10" max="1440" value="{current['reminder_minutes']}" required></label></div>
          <div><label>Certificate warnings (days before)<br><input type="text" name="cert_warn_days" value="{', '.join(str(d) for d in current['cert_warn_days'])}" required></label></div>
          <div><label>Give up on a page after (s)<br><input type="number" name="timeout_seconds" min="5" max="60" value="{current['timeout_seconds']}" required></label></div>
          <div><label>Keep history for (days)<br><input type="number" name="retention_days" min="7" max="365" value="{current['retention_days']}" required></label></div>
        </div>
        <h2 style="margin-top:20px">Pages</h2>
        <p class="meta" style="margin:-4px 0 10px">Each page switched on is one request every five
           minutes. Keep the list short. "Session" is the shop's session parameter (zenid); the
           monitor keeps one session per route and reuses it, because a new one is slow for the
           shop server.</p>
        <table class="grid">
          <tr><th>On</th><th>Name</th><th>Address</th><th>Reached</th><th>Must contain</th><th>Session</th></tr>
          {rows}
        </table>
        <div style="margin-top:14px">
          <button class="btn-primary" type="submit"
                  {_confirm("Save site monitor settings?", body="They apply from the next check, within five minutes. Changing the alert list changes who is emailed about problems.", ok="Save settings")}>Save settings</button>
        </div>
      </form>
    </div>"""


@app.get(MONITOR_PATH, response_class=HTMLResponse)
def site_health(request: Request):
    user = _current_user(request)
    crumbs = [("Site health", "")]
    if not _can(request, auth.PERM_REVIEW):
        return HTMLResponse(ui.page_shell(
            ui.empty_state("No access", "Your account cannot see this page."),
            title="Site health \u00b7 Content and Automation", active_module="site-health",
            user=user, request=request, crumbs=crumbs), status_code=403)

    current = monitor_store.get_settings()
    now = datetime.now(timezone.utc)
    can_admin = _can(request, auth.PERM_ADMINISTER)
    can_run = _can(request, auth.PERM_RUN_JOBS)
    can_mail = _can(request, auth.PERM_SEND_MAIL)
    enabled = [t for t in current["targets"] if t.get("enabled")]
    switched_off = [t for t in current["targets"] if not t.get("enabled")]

    cards, uptime_rows = "", ""
    for t in enabled:
        latest = monitor_store.latest_check(t["key"])
        if latest:
            state, badge = MONITOR_OUTCOMES.get(latest["outcome"], (latest["outcome"], "planned"))
            response = []
            if latest["status"]:
                response.append(f"HTTP {latest['status']}")
            if latest["ms"] is not None:
                response.append(f"{latest['ms'] / 1000:.2f} s")
            meta = (f'{html.escape(", ".join(response))} &middot; checked '
                    f'{html.escape(monitor_emails.local_time(latest["checked_at"]))}')
            detail = (f'<div class="meta" style="margin-top:4px">{html.escape(latest["detail"])}</div>'
                      if latest.get("detail") else "")
        else:
            state, badge, meta, detail = "Not checked yet", "planned", "Waiting for the first check", ""
        day = monitor_store.uptime(t["key"], now - timedelta(days=1))
        chart = _sparkline(monitor_store.checks_since(t["key"], now - timedelta(days=1)),
                           current["slow_ms"])
        route = MONITOR_ROUTES.get(t.get("route"), t.get("route", ""))
        cards += f"""
        <div class="modcard">
          <div class="card-header" style="margin-bottom:6px">
            <div class="modcard-icon">{ui.icon("pulse", 21)}</div>
            <span class="badge {badge}">{html.escape(state)}</span>
          </div>
          <h3>{html.escape(t["label"])}</h3>
          <p class="meta" style="margin:0 0 2px;flex:none;word-break:break-all">{html.escape(t["url"])}
             &middot; {html.escape(route)}</p>
          <p class="meta" style="margin:0;flex:none">{meta}</p>
          {detail}
          <div style="margin-top:10px">{chart}</div>
          <div class="modstat"><div><b>{_uptime_text(day)}</b>Up, last 24 hours</div></div>
        </div>"""
        uptime_rows += (f'<tr><td><b>{html.escape(t["label"])}</b></td>'
                        f'<td>{_uptime_text(day)}</td>'
                        f'<td>{_uptime_text(monitor_store.uptime(t["key"], now - timedelta(days=7)))}</td>'
                        f'<td>{_uptime_text(monitor_store.uptime(t["key"], now - timedelta(days=30)))}</td></tr>')

    started = _monitor_state.get("started_at")
    running = bool(_monitor_state["running"] and started and now - started < MONITOR_STALE_AFTER)
    last_run = monitor_store.get_state("last_run_at", "")
    last_text = (f"Last check {html.escape(monitor_emails.local_time(last_run))}."
                 if last_run else "No checks have run yet.")
    buttons = ""
    if running:
        buttons += ('<button class="btn-accent" type="button" disabled>'
                    '<span class="spinner"></span> Checking...</button>')
    elif can_run:
        buttons += (f'<form method="post" action="{MONITOR_PATH}/check" style="margin:0">'
                    f'<button class="btn-accent" type="submit" '
                    f'{_confirm("Check the pages now?", body="Loads each monitored page once, the same as the scheduled check. Any alert that becomes due is sent.", ok="Check now")}>'
                    f'&#8635; Check now</button></form>')
    if can_mail:
        buttons += (f'<form method="post" action="{MONITOR_PATH}/test-email" style="margin:0">'
                    f'<button class="btn-ghost" type="submit" '
                    f'{_confirm("Send a test alert?", body="Sends a sample alert to everyone on the alert list, so you can see that it arrives and how it looks.", ok="Send test", tone="warn")}>'
                    f'&#9993; Send a test email</button></form>')
    who = (f"Alerts go to {html.escape(', '.join(current['recipients']))}." if can_admin
           else "Alerts are emailed to the people an admin has chosen.")
    action_bar = f"""
    <div class="fetch-bar">
      {buttons}
      <span class="meta">{last_text} Pages are checked every five minutes. {who}
        From {current['quiet_start']} to {current['quiet_end']} alerts wait, and a summary goes out
        at {current['morning_summary_at']}.</span>
    </div>"""

    offline = ""
    if monitor_store.get_state("last_run_offline") == "1":
        offline = ('<div class="dry-run-note"><b>The last check could not reach the internet from '
                   'this server.</b> Pages outside the office network were not checked, and nothing '
                   'was counted against them.</div>')

    labels = {t["key"]: t["label"] for t in current["targets"]}
    incident_rows = ""
    for incident in monitor_store.recent_incidents(20):
        ongoing = not incident["closed_at"]
        incident_rows += (
            f'<tr><td><b>{html.escape(labels.get(incident["target_key"], incident["target_key"]))}</b></td>'
            f'<td><span class="badge {"confidence-low" if ongoing else "planned"}">'
            f'{html.escape(MONITOR_FAMILIES.get(incident["family"], incident["family"]))}</span>'
            f'<div class="meta">{html.escape(incident.get("detail") or "")}</div></td>'
            f'<td class="meta">{html.escape(monitor_emails.local_time(incident["opened_at"]))}</td>'
            f'<td class="meta">{"Still going" if ongoing else html.escape(monitor_emails.local_time(incident["closed_at"]))}</td>'
            f'<td class="meta">{html.escape(monitor_emails.duration(incident["opened_at"], incident["closed_at"] or ""))}</td></tr>')
    incidents_html = (
        f'<table class="grid"><tr><th>Page</th><th>Problem</th><th>Started</th><th>Ended</th>'
        f'<th>Lasted</th></tr>{incident_rows}</table>' if incident_rows else
        ui.empty_state("No problems recorded",
                       "Anything that goes wrong with a monitored page is listed here."))

    off_html = ""
    if switched_off:
        items = "".join(
            f'<li><b>{html.escape(t["label"])}</b> <span class="meta">'
            f'{html.escape(MONITOR_ROUTES.get(t["route"], t["route"]))} &middot; '
            f'{html.escape(t["url"])}</span></li>' for t in switched_off)
        off_html = (f'<div class="panel"><h2>Switched off</h2>'
                    f'<p class="meta" style="margin:-4px 0 8px">These pages are not being checked.</p>'
                    f'<ul style="margin:0;padding-left:18px">{items}</ul></div>')

    body = f"""
    <div class="page-head">
      <div>
        <h1>Site health</h1>
        <p class="subtitle">Checks the online shop every five minutes and emails when a page goes
           down, slows down or shows a problem.</p>
      </div>
    </div>
    {offline}
    {action_bar}
    <div class="modgrid">{cards}</div>
    <div class="panel" style="margin-top:16px">
      <h2>Uptime</h2>
      <table class="grid"><tr><th>Page</th><th>Last 24 hours</th><th>Last 7 days</th>
        <th>Last 30 days</th></tr>{uptime_rows}</table>
    </div>
    <div class="panel">
      <h2>Recent problems</h2>
      {incidents_html}
    </div>
    {off_html}
    {_monitor_settings_form(current) if can_admin else ""}
    """
    return ui.page_shell(body, title="Site health \u00b7 Content and Automation",
                         active_module="site-health", user=user, request=request,
                         crumbs=crumbs, fetch_running=running)


@app.post(MONITOR_PATH + "/settings")
async def site_health_settings(request: Request):
    denied = _denied(request, auth.PERM_ADMINISTER, MONITOR_PATH)
    if denied:
        return denied
    form = await request.form()
    try:
        count = max(0, min(int(form.get("target_count") or 0), 40))
    except ValueError:
        count = 0
    targets = [{"key": form.get(f"t{i}_key", ""), "label": form.get(f"t{i}_label", ""),
                "url": form.get(f"t{i}_url", ""), "route": form.get(f"t{i}_route", "direct"),
                "must_contain": form.get(f"t{i}_contains", ""),
                "session_param": form.get(f"t{i}_session", ""),
                "enabled": form.get(f"t{i}_enabled") in ("on", "true", "1")}
               for i in range(count)]
    values = {name: form.get(name, "") for name in (
        "recipients", "office_ip", "quiet_start", "quiet_end", "morning_summary_at", "slow_ms",
        "slow_after", "down_after", "issue_after", "blocked_after", "reminder_minutes",
        "cert_warn_days", "timeout_seconds", "retention_days")}
    values["targets"] = targets
    try:
        saved = monitor_store.save_settings(values, updated_by=_actor(request))
    except ValueError as e:
        return _redirect(MONITOR_PATH, err=str(e))
    on = sum(1 for t in saved["targets"] if t["enabled"])
    _audit(request, "site_monitor_settings",
           detail=f"{on} page(s) on, alerts to {', '.join(saved['recipients'])}")
    return _redirect(MONITOR_PATH, msg="Site monitor settings saved. They apply from the next check.")


def _run_monitor_job():
    try:
        monitor_run.run_once()
    except Exception:
        logger.exception("Manual site monitor check failed")
    finally:
        _monitor_state["running"] = False


@app.post(MONITOR_PATH + "/check")
def site_health_check(request: Request, background_tasks: BackgroundTasks):
    denied = _denied(request, auth.PERM_RUN_JOBS, MONITOR_PATH)
    if denied:
        return denied
    now = datetime.now(timezone.utc)
    started = _monitor_state.get("started_at")
    if _monitor_state["running"] and started and now - started < MONITOR_STALE_AFTER:
        return _redirect(MONITOR_PATH, err="A check is already running. Refresh in a moment.")
    _monitor_state.update({"running": True, "started_at": now})
    _audit(request, "site_monitor_check", detail="Manual check from the Site health page.")
    background_tasks.add_task(_run_monitor_job)
    return _redirect(MONITOR_PATH, msg="Checking now. Refresh in a few seconds to see the results.")


@app.post(MONITOR_PATH + "/test-email")
def site_health_test_email(request: Request):
    denied = _denied(request, auth.PERM_SEND_MAIL, MONITOR_PATH)
    if denied:
        return denied
    current = monitor_store.get_settings()
    subject, html_body, text_body = monitor_emails.render_test(current["recipients"])
    outcome = monitor_emails.send(subject, html_body, text_body, current["recipients"])
    _audit(request, "site_monitor_test_email", detail=outcome.get("detail", ""))
    if outcome.get("success"):
        return _redirect(MONITOR_PATH,
                         msg=f"Test email sent to {', '.join(current['recipients'])}.")
    return _redirect(MONITOR_PATH, err=f"The test email did not send: {outcome.get('detail', '')}")


@app.get("/api/status")
def api_status():
    """JSON health/stats endpoint for scripted checks. Behind auth -- use
    /healthz for an unauthenticated liveness probe."""
    return {
        "small_model_available": small_model_client.is_available(),
        "odoo_configured": odoo_connector.is_configured(),
        "queue_counts": review_queue.counts_by_status(),
        "unpublished_approved": review_queue.count_rows(status=Status.APPROVED, published=False),
        "fetch_source": "odoo_live" if settings.USE_ODOO_AS_PRODUCT_SOURCE else "static_file_fallback",
        "fetch_state": _fetch_state,
    }
