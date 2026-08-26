"""
Automation Control Dashboard.

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
import os
import sys
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from fastapi import BackgroundTasks, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from content_seo_agent import review_queue, small_model_client, assembler, pipeline, content_status as cs
from content_seo_agent.assembler import assemble_html
from content_seo_agent.batch_runner import process_dataframe
from content_seo_agent.constants import Status, TaskType, Source
from content_seo_agent.daily_run import get_current_products
from connectors.odoo_connector import odoo_connector  # must import after load_dotenv()
from chat_insights import db as chat_db, mailer, weekly_run as chat_run
from dashboard import job_logs
from config import settings
from dashboard import auth, ui

app = FastAPI(title="Automation Control Dashboard")

CONTENT_AGENT = "/content-agent"
CHAT_INSIGHTS = "/chat-insights"
PUBLIC_PATHS = {"/login", "/healthz"}

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
        )
        _fetch_state["last_result"] = {"source": source, "error": None, **results}
    except Exception as e:
        _fetch_state["last_result"] = {"source": None, "error": str(e)}
    finally:
        _fetch_state["last_result"]["finished_at"] = datetime.now(timezone.utc).isoformat()
        _fetch_state["running"] = False


def _run_regen_job(row_ids: list[int]):
    regenerated = empty = failed = 0
    try:
        for row_id in row_ids:
            try:
                row = pipeline.regenerate_draft_for_row(row_id)
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
                                        "failed": failed, "total": len(row_ids), "error": None}
    except Exception as e:
        _regen_state["last_result"] = {"regenerated": regenerated, "empty": empty,
                                        "failed": failed, "total": len(row_ids), "error": str(e)}
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
    if path in PUBLIC_PATHS:
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


def _current_user(request: Request) -> dict:
    return getattr(request.state, "user", None) or {}


def _redirect(path: str, msg: str = "", err: str = "") -> RedirectResponse:
    params = {}
    if msg:
        params["msg"] = msg
    if err:
        params["err"] = err
    url = f"{path}?{urlencode(params)}" if params else path
    return RedirectResponse(url=url, status_code=303)


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
    return ui.login_page(notice=request.query_params.get("notice", ""))


@app.post("/login")
def login_submit(request: Request, username: str = Form(...), password: str = Form(...),
                  next: str = Form(default="/")):
    user = auth.authenticate(username, password)
    if not user:
        return HTMLResponse(
            ui.login_page(error="Incorrect username or password.", username=username),
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


@app.post("/logout")
def logout(request: Request):
    auth.revoke_session(request.cookies.get(auth.SESSION_COOKIE))
    response = RedirectResponse(url="/login?notice=You+have+been+signed+out.", status_code=303)
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
    source_label = "Odoo (live)" if settings.USE_ODOO_AS_PRODUCT_SOURCE else "Backlog export file"

    tiles = "".join([
        ui.stat_tile("Awaiting review", pending, variant="accent", href=CONTENT_AGENT),
        ui.stat_tile("Published to Odoo", published, variant="ok"),
        ui.stat_tile("Needs retry", unpublished, variant="warn" if unpublished else "",
                      href=f"{CONTENT_AGENT}/needs-retry"),
        ui.stat_tile("Rejected", rejected),
        ui.stat_tile("Local model",
                      f'<span class="dot {"ok" if model_up else "bad"}"></span>'
                      f'{"Online" if model_up else "Offline"}', small=True,
                      variant="ok" if model_up else "bad"),
        ui.stat_tile("Odoo connection",
                      f'<span class="dot {"ok" if odoo_ok else "bad"}"></span>'
                      f'{"Connected" if odoo_ok else "Not configured"}', small=True,
                      variant="ok" if odoo_ok else "bad"),
    ])

    cards = ""
    for m in ui.MODULES:
        live = m["status"] == "live"
        stats = ""
        if m["key"] == "content-agent":
            stats = f"""
            <div class="modstat">
                <div><b>{pending}</b>Pending</div>
                <div><b>{approved}</b>Approved</div>
                <div><b>{published}</b>Published</div>
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
                          f'&middot; {ready} of 3 dependencies configured</div></div>')
        tag = f'<span class="badge {m["status"]}">{"Live" if live else "Planned"}</span>'
        open_attr = f'href="{m["path"]}"' if live else ""
        tag_name = "a" if live else "div"
        cards += f"""
        <{tag_name} class="modcard {"" if live else "planned"}" {open_attr}>
            <div class="card-header" style="margin-bottom:6px">
                <h3>{html.escape(m["label"])}</h3>{tag}
            </div>
            <p>{html.escape(m["blurb"])}</p>
            {stats}
        </{tag_name}>"""

    body = f"""
    <div class="page-head">
      <div>
        <h1>Overview</h1>
        <p class="subtitle">Status across all automations. Product source is currently
           <b>{html.escape(source_label)}</b>.</p>
      </div>
    </div>
    <div class="stats">{tiles}</div>
    <h2>Automations</h2>
    <div class="modgrid">{cards}</div>
    """
    return ui.page_shell(body, title="Overview · Automation Control", active_module="overview",
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
            <div class="meta">Delivery &amp; Pickup and disclaimer copy are appended automatically.</div>
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


def _card_html(row: dict, *, show_publish_retry: bool = False, show_reopen: bool = False) -> str:
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
        notes += ('<div class="flags"><b>No description could be generated</b> for this product '
                   '(both the local model and escalation failed), so there is nothing to approve. '
                   'Use Regenerate to try again -- a plain fetch will skip it, since it already '
                   'has a queue row.</div>')

    fail_note = ""
    if row.get("odoo_write_detail") and not row.get("published"):
        fail_note = f'<div class="dry-run-note"><b>Last attempt:</b> {html.escape(row["odoo_write_detail"])}</div>'

    editable = row["status"] == Status.PENDING and row["task_type"] == TaskType.DRAFT and has_content

    action_buttons = ""
    if row["status"] == Status.PENDING:
        reject_required = "required" if settings.APPROVAL_REQUIRE_REJECT_REASON else ""
        approve_btn = ""
        if has_content:
            label = ("Approve &amp; Publish"
                      if (row["task_type"] == TaskType.DRAFT and settings.APPROVAL_AUTO_PUBLISH)
                      else "Approve")
            approve_btn = (f'<button class="approve" type="submit" '
                            f'formaction="{CONTENT_AGENT}/queue/{row["id"]}/approve">{label}</button>')
        # A pending row with nothing generated is stranded otherwise: fetch
        # dedup skips anything that already has a queue row, so Regenerate is
        # the only way to get another draft without deleting it by hand.
        regen_btn = ""
        if not has_content and row["task_type"] == TaskType.DRAFT:
            regen_btn = (f'<button class="publish" type="submit" '
                          f'formaction="{CONTENT_AGENT}/queue/{row["id"]}/regenerate">'
                          f'&#8635; Regenerate</button>')
        action_buttons = f"""
        {approve_btn}{regen_btn}
        <input class="reject-note" type="text" name="reviewer_note"
               placeholder="Reason for rejecting (optional)" {reject_required}>
        <button class="reject" type="submit" formaction="{CONTENT_AGENT}/queue/{row['id']}/reject">Reject</button>
        """
    elif show_publish_retry and row["task_type"] == TaskType.DRAFT:
        action_buttons = f"""
        <form method="post" action="{CONTENT_AGENT}/queue/{row['id']}/publish" style="display:inline">
            <button class="publish" type="submit">Retry publish to Odoo</button>
        </form>"""
    elif show_publish_retry:
        action_buttons = '<span class="meta">Classification rows are not published.</span>'
    elif (not settings.APPROVAL_AUTO_PUBLISH and row["status"] == Status.APPROVED
            and row["task_type"] == TaskType.DRAFT and not row.get("published")):
        action_buttons = f"""
        <form method="post" action="{CONTENT_AGENT}/queue/{row['id']}/publish" style="display:inline">
            <button class="publish" type="submit">Publish to Odoo</button>
        </form>"""

    if show_reopen:
        action_buttons += f"""
        <form method="post" action="{CONTENT_AGENT}/queue/{row['id']}/reopen" style="display:inline">
            <button class="reopen" type="submit">Re-open for review</button>
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
            f'{tab("pending", "Review queue", CONTENT_AGENT, pending)}'
            f'{tab("needs-retry", "Needs retry", f"{CONTENT_AGENT}/needs-retry", retry)}'
            f'{tab("rejected", "Rejected", f"{CONTENT_AGENT}/rejected", rejected)}'
            f'{tab("history", "History", f"{CONTENT_AGENT}/history", reviewed)}'
            f'</div>')


def _regen_bar_html(pending_count: int) -> str:
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
        status = "Rejected drafts stay here until they are regenerated or dismissed."
    elif last.get("error"):
        status = f'<b>Last run failed:</b> {html.escape(last["error"])}'
    else:
        bits = [f'{last["regenerated"]} regenerated']
        if last["empty"]:
            bits.append(f'{last["empty"]} came back empty')
        if last["failed"]:
            bits.append(f'{last["failed"]} failed')
        status = "<b>Last run:</b> " + ", ".join(bits) + "."

    button = ""
    if pending_count:
        button = f"""
        <form method="post" action="{CONTENT_AGENT}/bulk/regenerate" style="margin:0"
              onsubmit="return confirm('Regenerate all {pending_count} rejected draft(s)? '
                                        + 'Each one re-runs the model, so this takes a while.')">
            <button class="btn-accent" type="submit">&#8635; Regenerate all rejected</button>
        </form>"""

    escalate_note = ("straight to the stronger model" if settings.REGENERATE_ESCALATE_FIRST
                      else f"local model at temperature {settings.REGENERATE_TEMPERATURE}")
    return f"""
    <div class="fetch-bar">
        {button}
        <span class="meta">Retries run {escalate_note}, with the rejection reason passed
            along as context. {status}</span>
    </div>"""


def _escalation_panel() -> str:
    """What the escalation chain will actually do right now. Config alone does
    not answer that -- a fallback model can be enabled but never pulled."""
    rows = ""
    for label, ok, detail in pipeline.escalation_status():
        disabled = "disabled" in detail
        if disabled:
            badge = '<span class="badge planned">Off</span>'
        elif ok:
            badge = '<span class="badge live">Ready</span>'
        else:
            badge = '<span class="badge confidence-low">Unavailable</span>'
        rows += (f'<tr><td style="width:170px"><b>{html.escape(label)}</b></td>'
                 f'<td>{badge}</td>'
                 f'<td class="meta">{html.escape(detail)}</td></tr>')
    return f"""
    <div class="panel">
      <h2>Escalation chain</h2>
      <p class="meta" style="margin:-4px 0 12px">Low-confidence output moves down this list
         until a model produces something that scores as confident. Whatever the last rung
         produced still reaches this queue, marked low confidence. Configure it in
         <code>config.yaml</code> under <code>escalation</code>.</p>
      <table class="grid">{rows}</table>
    </div>"""


def _fetch_bar_html() -> str:
    source_label = "Odoo (live)" if settings.USE_ODOO_AS_PRODUCT_SOURCE else "Backlog export file"

    if _fetch_state["running"]:
        return f"""
        <div class="fetch-bar">
            <button class="btn-accent" type="button" disabled><span class="spinner"></span> Fetching...</button>
            <span class="meta">Drafting new products from <b>{html.escape(source_label)}</b>.
                This page refreshes automatically.</span>
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
    return f"""
    <div class="fetch-bar">
        <form method="post" action="{CONTENT_AGENT}/fetch-new" style="margin:0">
            <button class="btn-accent" type="submit">&#8635; Fetch new products</button>
        </form>
        <span class="meta">Source: <b>{html.escape(source_label)}</b> &middot;
            up to {settings.DAILY_BATCH_SIZE} per run &middot; {status_text}</span>
    </div>"""


def _agent_page(request: Request, active: str, body_inner: str, title: str) -> str:
    body = f"""
    <div class="page-head">
      <div>
        <h1>Content Agent</h1>
        <p class="subtitle">{title}</p>
      </div>
    </div>
    {_agent_tabs_html(active)}
    {body_inner}
    """
    return ui.page_shell(body, title="Content Agent · Automation Control",
                          active_module="content-agent", user=_current_user(request),
                          request=request, fetch_running=_fetch_state["running"] or _regen_state["running"])


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
        cards_html = "".join(_card_html(row) for row in rows)
    elif total == 0 and not any([filters["task_type"], filters["source"],
                                  filters["confidence"], filters["flagged_only"]]):
        cards_html = ui.empty_state("Review queue is clear",
                                     "Nothing is waiting for review. Use “Fetch new products” to draft more.")
    else:
        cards_html = ui.empty_state("No matches", "No pending items match these filters.")

    bulk_bar = f"""
    <form id="bulk-form" method="post" action="{CONTENT_AGENT}/bulk/approve" style="display:contents"></form>
    <div class="bulk-bar">
        <label><input type="checkbox" class="checkbox-col" onclick="toggleAll(this)"> Select all on page</label>
        <span class="meta"><span class="sel-count" id="sel-count">0</span> selected</span>
        <div style="margin-left:auto; display:flex; gap:8px;">
            <button class="approve" type="submit" form="bulk-form" data-needs-selection
                    onclick="return confirmBulk('Approve')">Approve selected</button>
            <button class="reject" type="submit" form="bulk-form" data-needs-selection
                    formaction="{CONTENT_AGENT}/bulk/reject"
                    onclick="return confirmBulk('Reject')">Reject selected</button>
        </div>
    </div>"""

    query_state = {k: v for k, v in filters.items() if k != "page"}
    inner = (f'{_fetch_bar_html()}{_escalation_panel()}'
             f'{_filter_bar_html(filters, CONTENT_AGENT)}{bulk_bar}{cards_html}'
             f'{_pagination_html(CONTENT_AGENT, query_state, filters["page"], total, page_size)}')

    subtitle = ("Approving a draft publishes it to Odoo immediately."
                if settings.APPROVAL_AUTO_PUBLISH else
                "Approving marks a draft ready; publish is a separate step.")
    return _agent_page(request, "pending", inner,
                        f"{subtitle} Every draft is reviewed by a person before it goes live.")


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

    cards_html = ("".join(_card_html(r, show_publish_retry=True) for r in rows) if rows
                   else ui.empty_state("Nothing needs a retry",
                                        "Approved drafts that failed to reach Odoo would appear here."))

    query_state = {k: v for k, v in filters.items() if k != "page"}
    inner = (f'{_filter_bar_html(filters, f"{CONTENT_AGENT}/needs-retry")}{cards_html}'
             f'{_pagination_html(f"{CONTENT_AGENT}/needs-retry", query_state, filters["page"], total, page_size)}')
    return _agent_page(request, "needs-retry", inner,
                        "Approved drafts whose write to Odoo did not succeed. Retry them here.")


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
                <div class="meta">Rejected: {html.escape(str(row.get('reviewed_at') or '--'))}</div>
                {_draft_preview_html(row.get('parsed_output') or {})}
                <div class="actions">
                    <form method="post" action="{CONTENT_AGENT}/queue/{row['id']}/regenerate"
                          style="display:inline">
                        <button class="publish" type="submit">&#8635; Regenerate</button>
                    </form>
                    <form method="post" action="{CONTENT_AGENT}/queue/{row['id']}/reopen"
                          style="display:inline">
                        <button class="reopen" type="submit">Re-open as-is</button>
                    </form>
                </div>
            </div>"""
    else:
        cards_html = ui.empty_state(
            "Nothing rejected",
            "Drafts a reviewer turns down land here so they can be regenerated rather than forgotten.")

    bulk_form = f"""
    <form id="regen-form" method="post" action="{CONTENT_AGENT}/bulk/regenerate" style="display:contents"></form>
    <div class="bulk-bar">
        <label><input type="checkbox" class="checkbox-col" onclick="toggleAll(this)"> Select all on page</label>
        <span class="meta"><span class="sel-count" id="sel-count">0</span> selected</span>
        <div style="margin-left:auto">
            <button class="publish" type="submit" form="regen-form" data-needs-selection
                    onclick="return confirmBulk('Regenerate')">Regenerate selected</button>
        </div>
    </div>""" if rows else ""

    query_state = {k: v for k, v in filters.items() if k != "page"}
    inner = (f'{_regen_bar_html(total)}{_filter_bar_html(filters, f"{CONTENT_AGENT}/rejected")}'
             f'{bulk_form}{cards_html}'
             f'{_pagination_html(f"{CONTENT_AGENT}/rejected", query_state, filters["page"], total, page_size)}')
    return _agent_page(request, "rejected", inner,
                        "Drafts that were turned down. Regenerate them to get a fresh attempt, "
                        "or re-open one unchanged if it was rejected by mistake.")


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

    cards_html = "" if rows else ui.empty_state("No reviewed items yet",
                                                 "Approved and rejected drafts will be listed here.")
    for row in rows:
        approved = row["status"] == Status.APPROVED
        status_badge = (f'<span class="badge {"live" if approved else "confidence-low"}">'
                        f'{row["status"].upper()}</span>')
        published_html = ""
        if row.get("published"):
            published_html = '<div style="margin-top:7px"><span class="published-badge">PUBLISHED TO ODOO</span></div>'
        elif row.get("odoo_write_detail"):
            published_html = f'<div class="dry-run-note">{html.escape(row["odoo_write_detail"])}</div>'

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
            <div class="meta">Reviewed: {html.escape(str(row.get('reviewed_at') or '--'))}</div>
            {note_html}{published_html}
            <div class="actions">
                <form method="post" action="{CONTENT_AGENT}/queue/{row['id']}/reopen" style="display:inline">
                    <button class="reopen" type="submit">Re-open for review</button>
                </form>
            </div>
        </div>"""

    query_state = {k: v for k, v in filters.items() if k != "page"}
    inner = (f'{_filter_bar_html(filters, f"{CONTENT_AGENT}/history")}{cards_html}'
             f'{_pagination_html(f"{CONTENT_AGENT}/history", query_state, filters["page"], total, page_size)}')
    return _agent_page(request, "history", inner, "Everything already reviewed, and how it ended up.")


# ---------------------------------------------------------------------------
# Content Agent -- actions
# ---------------------------------------------------------------------------

@app.post(f"{CONTENT_AGENT}/fetch-new")
def fetch_new_products(background_tasks: BackgroundTasks):
    """Manually triggers the same fetch-and-draft logic daily_run.py runs on
    its schedule, so a reviewer can pull new products on demand instead of
    waiting for the next scheduled run. Runs in the background; the page
    polls itself while it's going."""
    if _fetch_state["running"]:
        return _redirect(CONTENT_AGENT, err="A fetch is already running.")
    _fetch_state["running"] = True
    _fetch_state["started_at"] = datetime.now(timezone.utc).isoformat()
    background_tasks.add_task(_run_fetch_job)
    return _redirect(CONTENT_AGENT, msg="Fetch started -- new drafts will appear here shortly.")


def _apply_draft_edits(row_id: int, overview: str | None, features: str | None,
                        applications: str | None) -> None:
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
    return review_queue.publish_row(
        row_id, assembled_html=assembled,
        success=write_result["success"], detail=write_result["detail"],
    )


@app.post(f"{CONTENT_AGENT}/queue/{{row_id}}/approve")
def approve_row(row_id: int,
                overview: str | None = Form(default=None),
                features: str | None = Form(default=None),
                applications: str | None = Form(default=None)):
    _apply_draft_edits(row_id, overview, features, applications)

    # Server-side guard, not just a hidden button -- a row with no generated
    # content has nothing to approve. Re-checked AFTER applying edits, so a
    # reviewer who typed content in manually can still approve.
    current = review_queue.get_row(row_id)
    if not current or not current.get("parsed_output"):
        return _redirect(CONTENT_AGENT, err="That item has no generated content, so it can't be approved.")

    row = review_queue.update_status(row_id, Status.APPROVED)

    if row and row["task_type"] == TaskType.DRAFT and settings.APPROVAL_AUTO_PUBLISH:
        result = _attempt_publish(row_id)
        if result and not result.get("published"):
            # The approval still stands; it moves to Needs Retry rather than
            # being lost or silently stuck.
            return _redirect(CONTENT_AGENT,
                              err=f"Approved, but the Odoo write failed: {result.get('odoo_write_detail', '')}")
        return _redirect(CONTENT_AGENT, msg=f"Approved and published “{row['title']}”.")

    return _redirect(CONTENT_AGENT, msg="Approved.")


@app.post(f"{CONTENT_AGENT}/bulk/approve")
def bulk_approve_rows(id: list[int] = Form(default=[])):
    approved = published = skipped = failed = 0
    for row_id in id:
        candidate = review_queue.get_row(row_id)
        if not candidate or not candidate.get("parsed_output"):
            skipped += 1  # nothing generated -- see approve_row's guard
            continue
        row = review_queue.update_status(row_id, Status.APPROVED)
        approved += 1
        if row and row["task_type"] == TaskType.DRAFT and settings.APPROVAL_AUTO_PUBLISH:
            result = _attempt_publish(row_id)
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
def reject_row(row_id: int, reviewer_note: str = Form(default="")):
    review_queue.update_status(row_id, Status.REJECTED, reviewer_note=reviewer_note)
    return _redirect(CONTENT_AGENT, msg="Rejected.")


@app.post(f"{CONTENT_AGENT}/bulk/reject")
def bulk_reject_rows(id: list[int] = Form(default=[])):
    for row_id in id:
        review_queue.update_status(row_id, Status.REJECTED)
    return _redirect(CONTENT_AGENT, msg=f"Rejected {len(id)} item(s).")


@app.post(f"{CONTENT_AGENT}/queue/{{row_id}}/publish")
def publish_row(row_id: int):
    row = review_queue.get_row(row_id)
    if not row or row["status"] != Status.APPROVED or row["task_type"] != TaskType.DRAFT:
        return _redirect(f"{CONTENT_AGENT}/needs-retry", err="That item can't be published.")
    result = _attempt_publish(row_id)
    if result and result.get("published"):
        return _redirect(f"{CONTENT_AGENT}/needs-retry", msg=f"Published “{row['title']}” to Odoo.")
    detail = (result or {}).get("odoo_write_detail", "unknown error")
    return _redirect(f"{CONTENT_AGENT}/needs-retry", err=f"Publish failed: {detail}")


@app.post(f"{CONTENT_AGENT}/queue/{{row_id}}/reopen")
def reopen_row(row_id: int):
    review_queue.reopen_row(row_id)
    return _redirect(f"{CONTENT_AGENT}/history", msg="Re-opened for review.")


@app.post(f"{CONTENT_AGENT}/queue/{{row_id}}/regenerate")
def regenerate_row(row_id: int, background_tasks: BackgroundTasks):
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

    _regen_state.update({"running": True, "started_at": datetime.now(timezone.utc).isoformat(),
                          "total": 1, "done": 0})
    background_tasks.add_task(_run_regen_job, [row_id])
    return _redirect(back, msg=f"Regenerating “{row['title']}” -- it returns to the review queue when done.")


@app.post(f"{CONTENT_AGENT}/bulk/regenerate")
def bulk_regenerate_rows(background_tasks: BackgroundTasks, id: list[int] = Form(default=[])):
    """Regenerates the selected rejected drafts, or every rejected draft when
    nothing is ticked (the "Regenerate all rejected" button posts no ids)."""
    if _regen_state["running"]:
        return _redirect(f"{CONTENT_AGENT}/rejected", err="A regeneration run is already in progress.")

    if id:
        rows = [review_queue.get_row(i) for i in id]
    else:
        rows = review_queue.list_rows(status=Status.REJECTED)
    row_ids = [r["id"] for r in rows if r and r["task_type"] == TaskType.DRAFT]
    if not row_ids:
        return _redirect(f"{CONTENT_AGENT}/rejected", err="Nothing to regenerate.")

    _regen_state.update({"running": True, "started_at": datetime.now(timezone.utc).isoformat(),
                          "total": len(row_ids), "done": 0})
    background_tasks.add_task(_run_regen_job, row_ids)
    return _redirect(f"{CONTENT_AGENT}/rejected",
                      msg=f"Regenerating {len(row_ids)} draft(s) in the background.")


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

def _chat_status_panel() -> str:
    rows = ""
    for label, ok, detail in chat_run.status_lines():
        rows += (f'<tr><td style="width:150px"><b>{html.escape(label)}</b></td>'
                 f'<td><span class="badge {"live" if ok else "planned"}">'
                 f'{"Ready" if ok else "Not configured"}</span></td>'
                 f'<td class="meta">{html.escape(detail)}</td></tr>')
    return f"""
    <div class="panel">
      <h2>Setup</h2>
      <table class="grid">{rows}</table>
    </div>"""


def _chat_run_bar_html() -> str:
    if _chat_state["running"]:
        done, total = _chat_state["done"], _chat_state["total"]
        progress = f"{done} of {total} conversations analysed" if total else "Fetching conversations..."
        return f"""
        <div class="fetch-bar">
            <button class="btn-accent" type="button" disabled>
                <span class="spinner"></span> Running...</button>
            <span class="meta">{progress}. Each one takes a moment on this hardware --
                this page refreshes automatically.</span>
        </div>"""

    last = _chat_state["last_result"]
    if not last:
        status = "The weekly job also runs on a schedule; use this to run it on demand."
    elif last.get("error"):
        status = f'<b>Last run failed:</b> {html.escape(str(last["error"]))}'
    else:
        status = (f'<b>Last run:</b> {last.get("conversations", 0)} conversations, '
                   f'email {html.escape(str(last.get("email_status", "")))}.')

    ready = all(ok for _l, ok, _d in chat_run.status_lines()[:2])  # Chatbase + model
    disabled = "" if ready else "disabled title=\"Configure Chatbase and the model first\""
    return f"""
    <div class="fetch-bar">
        <form method="post" action="{CHAT_INSIGHTS}/run" style="margin:0">
            <button class="btn-accent" type="submit" {disabled}>&#9654; Run weekly analysis now</button>
        </form>
        <form method="post" action="{CHAT_INSIGHTS}/run" style="margin:0">
            <input type="hidden" name="send_email" value="0">
            <button class="btn-ghost" type="submit" {disabled}>Run without emailing</button>
        </form>
        <span class="meta">{status}</span>
    </div>"""


def _chat_email_button(run: dict, label: str = "Send by email") -> str:
    """Manual send for a report that already exists -- for a week whose
    scheduled send failed, or one that was run without emailing."""
    if run.get("status") != chat_db.STATUS_COMPLETE or not run.get("stats"):
        return ""
    if mailer.is_configured():
        extra = f'title="Send to {html.escape(", ".join(mailer.recipients()))}"'
    else:
        extra = f'disabled title="{html.escape(mailer.config_status())}"'
    resend = " again" if run.get("email_status") == "sent" else ""
    return (f'<form method="post" action="{CHAT_INSIGHTS}/run/{run["id"]}/email" '
            f'style="display:inline;margin:0">'
            f'<button class="btn-ghost" type="submit" style="padding:5px 12px" {extra}>'
            f'&#9993; {html.escape(label)}{resend}</button></form>')


@app.get(CHAT_INSIGHTS, response_class=HTMLResponse)
def chat_insights_home(request: Request):
    runs = chat_db.list_runs(limit=settings.APPROVAL_PAGE_SIZE)
    latest = runs[0] if runs else None

    tiles = ""
    if latest and latest.get("stats"):
        s = latest["stats"]
        tiles = '<div class="stats">' + "".join([
            ui.stat_tile("Conversations", s.get("total_conversations", 0), variant="accent"),
            ui.stat_tile("Bot couldn't answer", len(s.get("failures", [])),
                          variant="bad" if s.get("failures") else ""),
            ui.stat_tile("Possible leads", len(s.get("leads", []))),
            ui.stat_tile("Resolution rate", f'{s.get("resolution_rate", 0)}%', variant="ok"),
        ]) + '</div>'

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
                  {_chat_email_button(r, label="Email")}
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
            "Run the weekly analysis to produce the first report, or wait for the scheduled job.")

    body = f"""
    <div class="page-head">
      <div>
        <h1>Chat Insights</h1>
        <p class="subtitle">Weekly analysis of Chatbase conversations -- what the bot could not
           answer, what customers asked about, and who looks like a lead.</p>
      </div>
    </div>
    {tiles}
    {_chat_run_bar_html()}
    {table}
    {_chat_status_panel()}
    """
    return ui.page_shell(body, title="Chat Insights · Automation Control",
                          active_module="chat-insights", user=_current_user(request),
                          request=request,
                          fetch_running=_chat_state["running"])


@app.get(CHAT_INSIGHTS + "/run/{run_id}", response_class=HTMLResponse)
def chat_insights_report(request: Request, run_id: int):
    run = chat_db.get_run(run_id)
    if not run:
        return HTMLResponse(ui.page_shell(
            ui.empty_state("Report not found", "That run does not exist."),
            title="Chat Insights", active_module="chat-insights",
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
                   f'{_chat_email_button(run)}</div>')

    body = f"""
    <div class="page-head">
      <div>
        <h1>Week of {html.escape(run["week_start"])}</h1>
        <p class="subtitle">
           <a href="{CHAT_INSIGHTS}/run/{run_id}/conversations">{run.get("conversation_count", 0)}
           conversations</a>
           &middot; {html.escape(run["week_start"])} to {html.escape(run["week_end"])}
           &middot; <a href="{CHAT_INSIGHTS}">back to all reports</a></p>
      </div>
    </div>
    {email_note}
    {inner}
    """
    return ui.page_shell(body, title=f"Chat report {run['week_start']} · Automation Control",
                          active_module="chat-insights", user=_current_user(request),
                          request=request)


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
            title="Chat Insights", active_module="chat-insights",
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
        table = ui.empty_state("No conversations stored",
                                "This run recorded no conversations for the week.")

    body = f"""
    <div class="page-head">
      <div>
        <h1>Conversations, week of {html.escape(run["week_start"])}</h1>
        <p class="subtitle">{len(conversations)} conversations
           &middot; <a href="{CHAT_INSIGHTS}/run/{run_id}">back to the report</a></p>
      </div>
    </div>
    <div class="dry-run-note">Full transcripts, unredacted. The emailed report has customer
       contact details stripped out; this page is where they are kept.</div>
    {table}
    """
    return ui.page_shell(body, title=f"Conversations {run['week_start']} · Automation Control",
                          active_module="chat-insights", user=_current_user(request),
                          request=request)


@app.get(CHAT_INSIGHTS + "/conversation/{conversation_id}", response_class=HTMLResponse)
def chat_insights_conversation(request: Request, conversation_id: str):
    """One conversation, message by message, with its analysis alongside."""
    conv = chat_db.get_conversation(conversation_id)
    if not conv:
        return HTMLResponse(ui.page_shell(
            ui.empty_state("Conversation not found",
                            "It may belong to a run whose data has since been cleared."),
            title="Chat Insights", active_module="chat-insights",
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
    back = (f'{CHAT_INSIGHTS}/run/{conv["run_id"]}/conversations' if conv.get("run_id")
            else CHAT_INSIGHTS)

    body = f"""
    <div class="page-head">
      <div>
        <h1>{html.escape(analysis.get("topic") or "Conversation")}</h1>
        <p class="subtitle">{when:%Y-%m-%d %H:%M} UTC &middot;
           {html.escape(conv.get("source") or "unknown source")} &middot;
           {conv.get("message_count", 0)} messages &middot;
           <a href="{back}">back to the conversation list</a></p>
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
    return ui.page_shell(body, title="Conversation · Automation Control",
                          active_module="chat-insights", user=_current_user(request),
                          request=request)


@app.post(CHAT_INSIGHTS + "/run/{run_id}/email")
def chat_insights_email(run_id: int):
    """Sends an already-generated report on demand. Synchronous on purpose: one
    small message, and the whole point of the button is to see whether the mail
    server accepted it."""
    back = f"{CHAT_INSIGHTS}/run/{run_id}"
    if not mailer.is_configured():
        return _redirect(back, err=mailer.config_status())
    outcome = chat_run.email_report(run_id)
    if outcome["success"]:
        return _redirect(back, msg=outcome["detail"])
    return _redirect(back, err=outcome["detail"])


@app.post(CHAT_INSIGHTS + "/run")
def chat_insights_run(background_tasks: BackgroundTasks, send_email: str = Form(default="1")):
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
    return _current_user(request).get("role") == auth.ROLE_ADMIN


@app.get("/settings/jobs", response_class=HTMLResponse)
def settings_jobs(request: Request, log: str = "", lines: int = job_logs.DEFAULT_TAIL_LINES):
    """What the scheduled jobs actually did, without an RDP session.

    Admin-only: these logs carry customer questions from the chat analysis and
    the full text of anything that failed, which is more than a reviewer needs.
    """
    user = _current_user(request)
    if not _require_admin(request):
        return HTMLResponse(ui.page_shell(
            ui.empty_state("Admins only", "You need an admin account to view job logs."),
            title="Job logs", active_module="settings", user=user, request=request),
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
                f"{label}: no log file yet",
                "The task that writes it has not run successfully. Check Windows Task "
                "Scheduler -- a task that fails to start never writes a line.")
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
      <div><h1>Job logs</h1>
      <p class="subtitle">Output from the scheduled tasks. A job that never wrote a log
         never started -- that is a Task Scheduler problem, not an application one.</p></div>
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
    return ui.page_shell(body, title="Job logs · Automation Control",
                          active_module="settings", user=user, request=request)


@app.get("/settings/users", response_class=HTMLResponse)
def settings_users(request: Request):
    user = _current_user(request)
    if not _require_admin(request):
        return HTMLResponse(ui.page_shell(
            ui.empty_state("Admins only", "You need an admin account to manage users."),
            title="Settings", active_module="settings", user=user, request=request), status_code=403)

    rows = ""
    for u in auth.list_users():
        is_self = u["username"] == user["username"]
        active_label = "Active" if u["active"] else "Disabled"
        toggle_label = "Disable" if u["active"] else "Enable"
        toggle_btn = (
            f'<form method="post" action="/settings/users/{u["username"]}/toggle" style="display:inline">'
            f'<button class="reopen" type="submit">{toggle_label}</button></form>'
        ) if not is_self else '<span class="meta">--</span>'
        rows += f"""
        <tr>
            <td><b>{html.escape(u["username"])}</b>{' <span class="meta">(you)</span>' if is_self else ''}</td>
            <td><span class="badge role">{html.escape(u["role"])}</span></td>
            <td><span class="badge {'live' if u['active'] else 'planned'}">{active_label}</span></td>
            <td class="meta">{html.escape(str(u.get("last_login_at") or "never"))}</td>
            <td>
              <form method="post" action="/settings/users/{u["username"]}/password"
                    style="display:inline-flex; gap:6px;">
                <input type="password" name="new_password" placeholder="New password"
                       minlength="8" required style="width:150px">
                <button class="btn-ghost" type="submit">Reset</button>
              </form>
              {toggle_btn}
            </td>
        </tr>"""

    body = f"""
    <div class="page-head">
      <div><h1>Users</h1>
      <p class="subtitle">Accounts that can sign in to this dashboard.
         Passwords are stored as salted PBKDF2 hashes and can only be reset, never viewed.</p></div>
    </div>
    <div class="panel">
      <h2>Add a user</h2>
      <form method="post" action="/settings/users/create"
            style="display:flex; gap:10px; flex-wrap:wrap; align-items:flex-end;">
        <div><label>Username<br><input type="text" name="username" required></label></div>
        <div><label>Password<br><input type="password" name="password" minlength="8" required></label></div>
        <div><label>Role<br>
          <select name="role">
            <option value="reviewer">Reviewer</option>
            <option value="admin">Admin</option>
          </select></label></div>
        <button class="btn-primary" type="submit">Create user</button>
      </form>
    </div>
    <div class="panel">
      <h2>Existing users</h2>
      <table class="grid">
        <tr><th>Username</th><th>Role</th><th>Status</th><th>Last sign-in</th><th>Actions</th></tr>
        {rows}
      </table>
    </div>
    <div class="panel">
      <h2>Change my password</h2>
      <form method="post" action="/account/password" style="display:flex; gap:10px; align-items:flex-end;">
        <div><label>Current<br><input type="password" name="current_password" required></label></div>
        <div><label>New<br><input type="password" name="new_password" minlength="8" required></label></div>
        <button class="btn-primary" type="submit">Update password</button>
      </form>
    </div>
    """
    return ui.page_shell(body, title="Users · Automation Control", active_module="settings",
                          user=user, request=request)


@app.post("/settings/users/create")
def create_user_route(request: Request, username: str = Form(...), password: str = Form(...),
                       role: str = Form(default=auth.ROLE_REVIEWER)):
    if not _require_admin(request):
        return _redirect("/settings/users", err="Admins only.")
    try:
        auth.create_user(username, password, role=role)
    except ValueError as e:
        return _redirect("/settings/users", err=str(e))
    return _redirect("/settings/users", msg=f"Created user “{username}”.")


@app.post("/settings/users/{username}/password")
def reset_user_password(request: Request, username: str, new_password: str = Form(...)):
    if not _require_admin(request):
        return _redirect("/settings/users", err="Admins only.")
    try:
        auth.set_password(username, new_password)
    except ValueError as e:
        return _redirect("/settings/users", err=str(e))
    auth.revoke_all_sessions(username)  # force re-login with the new password
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
    return _redirect("/settings/users",
                      msg=f"{'Disabled' if target['active'] else 'Enabled'} “{username}”.")


@app.post("/account/password")
def change_own_password(request: Request, current_password: str = Form(...),
                         new_password: str = Form(...)):
    user = _current_user(request)
    if not auth.authenticate(user["username"], current_password):
        return _redirect("/settings/users", err="Current password is incorrect.")
    try:
        auth.set_password(user["username"], new_password)
    except ValueError as e:
        return _redirect("/settings/users", err=str(e))
    return _redirect("/settings/users", msg="Your password was updated.")


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

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
