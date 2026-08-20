"""
Content/SEO Agent -- review dashboard.

Run with:
    uvicorn dashboard.app:app --host 0.0.0.0 --port 8420 --reload

Then visit http://localhost:8420 (or the server's IP:8420 from another
machine on the network, if the firewall allows it).

No agent in this system ever writes to Odoo directly. This dashboard
is the human checkpoint: it lets a person approve or reject each draft.
Whether "Approve" also publishes immediately is controlled by
config.yaml's approval_flow.auto_publish_on_approve -- see the
"Publish flow" note at the bottom of this file.
"""
import html
import json
import os
import sys
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from content_seo_agent import review_queue, small_model_client
from content_seo_agent.assembler import assemble_html
from content_seo_agent.constants import Status, TaskType, Source
from connectors.odoo_connector import odoo_connector  # must import after load_dotenv()
from config import settings

app = FastAPI(title="Content/SEO Agent Dashboard")

TASK_TYPE_LABELS = {TaskType.CLASSIFY: "classify", TaskType.DRAFT: "draft"}
SOURCE_LABELS = {Source.SMALL_MODEL: "small_model", Source.CLAUDE: "claude"}


def _page_shell(body: str) -> str:
    return f"""<!DOCTYPE html>
<html>
<head>
    <title>Content/SEO Agent</title>
    <meta charset="utf-8">
    <style>
        body {{ font-family: -apple-system, Segoe UI, Arial, sans-serif; margin: 0; padding: 24px;
                background: #f5f5f4; color: #292524; }}
        h1 {{ font-size: 20px; margin-bottom: 4px; }}
        .subtitle {{ color: #78716c; font-size: 13px; margin-bottom: 20px; }}
        .status-bar {{ display: flex; gap: 12px; margin-bottom: 20px; flex-wrap: wrap; }}
        .status-pill {{ background: white; border: 1px solid #e7e5e4; border-radius: 6px;
                         padding: 6px 12px; font-size: 13px; }}
        .status-pill.ok {{ border-color: #16a34a; color: #16a34a; }}
        .status-pill.down {{ border-color: #dc2626; color: #dc2626; }}
        .status-pill.warn {{ border-color: #d97706; color: #b45309; }}
        .status-pill.warn a {{ color: inherit; }}
        .card {{ background: white; border: 1px solid #e7e5e4; border-radius: 8px;
                  padding: 16px; margin-bottom: 14px; }}
        .card-header {{ display: flex; justify-content: space-between; align-items: baseline;
                         margin-bottom: 8px; flex-wrap: wrap; gap: 8px; }}
        .card-header-left {{ display: flex; align-items: baseline; gap: 4px; }}
        .title {{ font-weight: 600; font-size: 15px; }}
        .badge {{ display: inline-block; font-size: 11px; padding: 2px 8px; border-radius: 999px;
                   margin-left: 6px; font-weight: 500; }}
        .badge.small_model {{ background: #e0f2fe; color: #075985; }}
        .badge.claude {{ background: #ede9fe; color: #5b21b6; }}
        .badge.classify {{ background: #f1f5f9; color: #475569; }}
        .badge.draft {{ background: #fef3c7; color: #92400e; }}
        .badge.confidence-low {{ background: #fef2f2; color: #991b1b; }}
        .badge.confidence-high {{ background: #f0fdf4; color: #166534; }}
        .flags {{ background: #fef2f2; border: 1px solid #fecaca; border-radius: 6px;
                   padding: 8px 10px; margin: 8px 0; font-size: 12px; color: #991b1b; }}
        .preview {{ font-size: 13px; line-height: 1.5; margin: 8px 0; }}
        .preview p {{ margin: 0 0 8px 0; }}
        .preview ul {{ margin: 0 0 8px 0; padding-left: 20px; }}
        .preview .field-label {{ font-weight: 600; font-size: 11px; text-transform: uppercase;
                                   color: #78716c; margin: 8px 0 2px 0; }}
        .edit-field {{ width: 100%; box-sizing: border-box; font-family: inherit; font-size: 13px;
                        line-height: 1.5; padding: 8px; border: 1px solid #e7e5e4; border-radius: 6px;
                        resize: vertical; background: #fffef9; margin-bottom: 6px; }}
        .edit-field:focus {{ outline: 2px solid #2563eb; outline-offset: -1px; background: white; }}
        .edit-toggle-btn {{ background: none; border: none; color: #78716c; font-size: 12px;
                              cursor: pointer; padding: 2px 0; margin-top: 2px; text-decoration: underline; }}
        .edit-toggle-btn:hover {{ color: #292524; }}
        .edit-mode-actions {{ display: flex; gap: 8px; margin-top: 4px; }}
        .save-edit-btn {{ background: #2563eb; color: white; border: none; border-radius: 6px;
                            padding: 5px 12px; font-size: 12px; cursor: pointer; font-weight: 500; }}
        .cancel-edit-btn {{ background: none; border: 1px solid #e7e5e4; color: #57534e; border-radius: 6px;
                              padding: 5px 12px; font-size: 12px; cursor: pointer; }}
        details.raw-json {{ margin-top: 6px; }}
        details.raw-json summary {{ font-size: 12px; color: #78716c; cursor: pointer; }}
        pre {{ background: #fafaf9; border: 1px solid #e7e5e4; border-radius: 6px; padding: 10px;
                font-size: 12px; overflow-x: auto; white-space: pre-wrap; }}
        .actions {{ margin-top: 10px; display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }}
        .reject-note {{ font-size: 12px; padding: 6px 8px; border: 1px solid #e7e5e4; border-radius: 6px;
                         flex: 1; min-width: 140px; }}
        button {{ border: none; border-radius: 6px; padding: 7px 16px; font-size: 13px;
                   cursor: pointer; font-weight: 500; }}
        .approve {{ background: #16a34a; color: white; }}
        .reject {{ background: #dc2626; color: white; }}
        .publish {{ background: #2563eb; color: white; }}
        .reopen {{ background: #57534e; color: white; }}
        .bulk-btn {{ background: #292524; color: white; }}
        .bulk-btn:disabled {{ background: #d6d3d1; cursor: not-allowed; }}
        .published-badge {{ background: #dbeafe; color: #1e40af; font-size: 11px; padding: 3px 10px;
                              border-radius: 999px; font-weight: 600; }}
        .dry-run-note {{ background: #fffbeb; border: 1px solid #fde68a; border-radius: 6px;
                           padding: 8px 10px; margin-top: 8px; font-size: 12px; color: #92400e; }}
        .empty {{ color: #78716c; text-align: center; padding: 40px; }}
        .tabs {{ margin-bottom: 16px; }}
        .tabs a {{ color: #57534e; text-decoration: none; margin-right: 16px; font-size: 13px; }}
        .tabs a.active {{ color: #292524; font-weight: 600; border-bottom: 2px solid #292524; }}
        .meta {{ font-size: 12px; color: #78716c; }}
        .filter-bar {{ background: white; border: 1px solid #e7e5e4; border-radius: 8px;
                        padding: 12px 16px; margin-bottom: 16px; display: flex; gap: 12px;
                        flex-wrap: wrap; align-items: center; font-size: 13px; }}
        .filter-bar select, .filter-bar label {{ font-size: 13px; }}
        .filter-bar a.clear {{ color: #78716c; text-decoration: underline; }}
        .bulk-bar {{ display: flex; gap: 8px; align-items: center; margin-bottom: 12px; font-size: 13px; }}
        .checkbox-col {{ margin-right: 8px; }}
        .pagination {{ display: flex; gap: 12px; justify-content: center; margin-top: 16px;
                        font-size: 13px; }}
        .pagination a {{ color: #292524; text-decoration: none; }}
        .pagination .disabled {{ color: #d6d3d1; }}
    </style>
    <script>
        function toggleAll(source) {{
            document.querySelectorAll('.row-check').forEach(cb => cb.checked = source.checked);
        }}

        function escapeHtml(s) {{
            const d = document.createElement('div');
            d.textContent = s;
            return d.innerHTML;
        }}

        function toggleEdit(rowId) {{
            const wrap = document.querySelector(`.draft-content[data-row-id="${{rowId}}"]`);
            wrap.querySelector('.view-mode').style.display = 'none';
            wrap.querySelector('.edit-mode').style.display = 'block';
        }}

        function cancelEdit(rowId) {{
            const wrap = document.querySelector(`.draft-content[data-row-id="${{rowId}}"]`);
            wrap.querySelectorAll('textarea').forEach(t => t.value = t.defaultValue);
            wrap.querySelector('.edit-mode').style.display = 'none';
            wrap.querySelector('.view-mode').style.display = 'block';
        }}

        function saveEdit(rowId) {{
            const wrap = document.querySelector(`.draft-content[data-row-id="${{rowId}}"]`);
            const overview = wrap.querySelector('textarea[data-field="overview"]').value.trim();
            const features = wrap.querySelector('textarea[data-field="features"]').value
                .split('\\n').map(s => s.trim()).filter(Boolean);
            const applications = wrap.querySelector('textarea[data-field="applications"]').value
                .split('\\n').map(s => s.trim()).filter(Boolean);

            let out = '';
            if (overview) out += `<div class="field-label">Overview</div><p>${{escapeHtml(overview)}}</p>`;
            if (features.length) out += `<div class="field-label">Features</div><ul>` +
                features.map(f => `<li>${{escapeHtml(f)}}</li>`).join('') + `</ul>`;
            if (applications.length) out += `<div class="field-label">Applications</div><ul>` +
                applications.map(a => `<li>${{escapeHtml(a)}}</li>`).join('') + `</ul>`;

            wrap.querySelector('.view-mode .preview').innerHTML = out || '<span class="meta">(no content)</span>';
            wrap.querySelector('.edit-mode').style.display = 'none';
            wrap.querySelector('.view-mode').style.display = 'block';
        }}
    </script>
</head>
<body>
{body}
</body>
</html>"""


def _confidence_badge(confidence: str) -> str:
    cls = "confidence-low" if confidence == "low" else "confidence-high"
    return f'<span class="badge {cls}">confidence: {html.escape(confidence or "unknown")}</span>'


def _render_draft_content_html(parsed: dict) -> str:
    """Renders the actual draft copy (overview/features/applications) as
    readable HTML instead of a raw JSON dump, so a reviewer can judge the
    content itself rather than mentally parsing JSON. Shared by the
    read-only preview and the "view" side of the inline editor."""
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

    known_fields = {"overview", "features", "applications"}
    extra = {k: v for k, v in parsed.items() if k not in known_fields}
    for key, value in extra.items():
        parts.append(f'<div class="field-label">{html.escape(str(key))}</div><p>{html.escape(str(value))}</p>')

    return "".join(parts)


def _draft_preview_html(parsed: dict) -> str:
    content = _render_draft_content_html(parsed)
    preview = f'<div class="preview">{content}</div>' if content else '<div class="meta">(no parsed output)</div>'
    raw = (
        '<details class="raw-json"><summary>View raw JSON</summary>'
        f'<pre>{html.escape(json.dumps(parsed, indent=2, ensure_ascii=False))}</pre></details>'
    )
    return preview + raw


def _filter_bar_html(active: dict, base_path: str) -> str:
    def option(value, label, current):
        selected = " selected" if value == current else ""
        return f'<option value="{value}"{selected}>{label}</option>'

    task_type = active.get("task_type", "")
    source = active.get("source", "")
    confidence = active.get("confidence", "")
    flagged_only = active.get("flagged_only", "")

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
                {option(Source.SMALL_MODEL, "Small model", source)}
                {option(Source.CLAUDE, "Claude", source)}
            </select>
        </label>
        <label>Confidence
            <select name="confidence">
                {option("", "All", confidence)}
                {option("low", "Low", confidence)}
                {option("high", "High", confidence)}
            </select>
        </label>
        <label><input type="checkbox" name="flagged_only" value="1" {"checked" if flagged_only else ""}>
            Safety flags only</label>
        <button type="submit" class="bulk-btn">Apply</button>
        <a class="clear" href="{base_path}">Clear filters</a>
    </form>
    """


def _pagination_html(base_path: str, query: dict, page: int, total: int, page_size: int) -> str:
    total_pages = max(1, (total + page_size - 1) // page_size)
    if total_pages <= 1:
        return ""

    def link(p, label):
        if p < 1 or p > total_pages:
            return f'<span class="disabled">{label}</span>'
        q = {**query, "page": p}
        qs = "&".join(f"{k}={v}" for k, v in q.items() if v not in (None, ""))
        return f'<a href="{base_path}?{qs}">{label}</a>'

    return (
        f'<div class="pagination">{link(page - 1, "&laquo; Prev")} '
        f'<span class="meta">Page {page} of {total_pages}</span> '
        f'{link(page + 1, "Next &raquo;")}</div>'
    )


def _editable_draft_fields_html(row_id: int, parsed: dict) -> str:
    """Read-only preview by default (matching non-editable rows), with a
    small Edit toggle that swaps in textareas client-side -- no page
    reload, nothing sent to the server until Approve/Reject is actually
    submitted. The textareas stay present (just hidden) in view mode so
    an edit made and then re-hidden via Save still submits correctly."""
    content = _render_draft_content_html(parsed)
    overview = html.escape(str(parsed.get("overview", "")))
    features = html.escape("\n".join(str(f) for f in (parsed.get("features") or [])))
    applications = html.escape("\n".join(str(a) for a in (parsed.get("applications") or [])))
    return f"""
    <div class="draft-content" data-row-id="{row_id}">
        <div class="view-mode">
            <div class="preview">{content or '<span class="meta">(no content)</span>'}</div>
            <button type="button" class="edit-toggle-btn" onclick="toggleEdit({row_id})"
                    title="Edit description">&#9998; Edit</button>
        </div>
        <div class="edit-mode" style="display:none">
            <div class="field-label">Overview</div>
            <textarea name="overview" class="edit-field" data-field="overview" rows="3">{overview}</textarea>
            <div class="field-label">Features (one per line)</div>
            <textarea name="features" class="edit-field" data-field="features" rows="4">{features}</textarea>
            <div class="field-label">Applications (one per line)</div>
            <textarea name="applications" class="edit-field" data-field="applications" rows="3">{applications}</textarea>
            <div class="edit-mode-actions">
                <button type="button" class="save-edit-btn" onclick="saveEdit({row_id})">Save</button>
                <button type="button" class="cancel-edit-btn" onclick="cancelEdit({row_id})">Cancel</button>
            </div>
        </div>
    </div>
    <details class="raw-json"><summary>View original raw JSON</summary>
        <pre>{html.escape(json.dumps(parsed, indent=2, ensure_ascii=False))}</pre>
    </details>
    """


def _card_html(row: dict, *, show_publish_retry: bool = False, show_reopen: bool = False) -> str:
    parsed = row.get("parsed_output") or {}
    safety_flags = row.get("safety_flags") or []
    reasons = row.get("reasons") or []

    flags_html = ""
    if safety_flags:
        flags_html = (
            f'<div class="flags">Needs attention: '
            f'{html.escape(", ".join(str(f) for f in safety_flags))}</div>'
        )

    reasons_html = ""
    if reasons and not safety_flags:
        reasons_html = (
            f'<div class="meta">Escalation reason(s): '
            f'{html.escape("; ".join(str(r) for r in reasons))}</div>'
        )
    if row.get("edited"):
        reasons_html += '<div class="meta">Edited by reviewer before approval</div>'

    fail_note = ""
    if row.get("odoo_write_detail") and not row.get("published"):
        fail_note = f'<div class="dry-run-note">Last attempt: {html.escape(row["odoo_write_detail"])}</div>'

    editable = row["status"] == Status.PENDING and row["task_type"] == TaskType.DRAFT

    action_buttons = ""
    if row["status"] == Status.PENDING:
        approve_label = "Approve & Publish" if (row["task_type"] == TaskType.DRAFT and settings.APPROVAL_AUTO_PUBLISH) else "Approve"
        reject_required = "required" if settings.APPROVAL_REQUIRE_REJECT_REASON else ""
        action_buttons = f"""
        <button class="approve" type="submit" formaction="/queue/{row['id']}/approve">{approve_label}</button>
        <input class="reject-note" type="text" name="reviewer_note"
               placeholder="Reason (optional)" {reject_required}>
        <button class="reject" type="submit" formaction="/queue/{row['id']}/reject">Reject</button>
        """
    elif show_publish_retry and row["task_type"] == TaskType.DRAFT:
        action_buttons = f"""
        <form method="post" action="/queue/{row['id']}/publish" style="display:inline">
            <button class="publish" type="submit">Retry Publish to Odoo</button>
        </form>
        """
    elif show_publish_retry:
        action_buttons = '<span class="meta">(classification rows are not published)</span>'
    elif not settings.APPROVAL_AUTO_PUBLISH and row["status"] == Status.APPROVED and row["task_type"] == TaskType.DRAFT and not row.get("published"):
        action_buttons = f"""
        <form method="post" action="/queue/{row['id']}/publish" style="display:inline">
            <button class="publish" type="submit">Publish to Odoo</button>
        </form>
        """

    if show_reopen:
        action_buttons += f"""
        <form method="post" action="/queue/{row['id']}/reopen" style="display:inline">
            <button class="reopen" type="submit">Re-open for review</button>
        </form>
        """

    checkbox = ""
    if row["status"] == Status.PENDING:
        checkbox = f'<input type="checkbox" class="row-check checkbox-col" name="id" value="{row["id"]}" form="bulk-form">'

    content_html = _editable_draft_fields_html(row['id'], parsed) if editable else _draft_preview_html(parsed)
    body = f"""
        <div class="card-header">
            <div class="card-header-left">
                {checkbox}
                <span class="title">{html.escape(row['title'])}</span>
                <span class="badge {row['task_type']}">{row['task_type']}</span>
                <span class="badge {row['source']}">{row['source']}</span>
                {_confidence_badge(row['confidence'])}
            </div>
            <div class="meta">{row['product_id']}</div>
        </div>
        {flags_html}
        {reasons_html}
        {content_html}
        {fail_note}
        <div class="actions">{action_buttons}</div>
    """

    if row["status"] == Status.PENDING:
        # Approve/Reject (and any inline edits) submit together as one POST,
        # via the formaction trick on each button -- see the bulk-approve
        # buttons above for the same pattern.
        return f'<div class="card"><form method="post" action="/queue/{row["id"]}/approve">{body}</form></div>'
    return f'<div class="card">{body}</div>'


def _parse_filters(request: Request) -> dict:
    q = request.query_params
    return {
        "task_type": q.get("task_type") or None,
        "source": q.get("source") or None,
        "confidence": q.get("confidence") or None,
        "flagged_only": q.get("flagged_only") or None,
        "page": max(1, int(q.get("page", 1) or 1)),
    }


def _status_bar_html() -> str:
    counts = review_queue.counts_by_status()
    pending = counts.get(Status.PENDING, 0)
    approved = counts.get(Status.APPROVED, 0)
    rejected = counts.get(Status.REJECTED, 0)
    unpublished = review_queue.count_rows(status=Status.APPROVED, published=False)

    model_up = small_model_client.is_available()
    status_class = "ok" if model_up else "down"
    status_text = "Small model: online" if model_up else "Small model: unreachable (falling back to Claude only)"

    odoo_status_class = "ok" if odoo_connector.is_configured() else "down"
    odoo_status_text = "Odoo: configured" if odoo_connector.is_configured() else "Odoo: not configured (publish will dry-run)"

    unpublished_pill = ""
    if unpublished:
        unpublished_pill = f'<div class="status-pill warn"><a href="/needs-retry">Unpublished: {unpublished}</a></div>'

    return f"""
    <div class="status-bar">
        <div class="status-pill {status_class}">{status_text}</div>
        <div class="status-pill {odoo_status_class}">{odoo_status_text}</div>
        <div class="status-pill">Pending: {pending}</div>
        <div class="status-pill">Approved: {approved}</div>
        <div class="status-pill">Rejected: {rejected}</div>
        {unpublished_pill}
    </div>
    """


def _tabs_html(active: str) -> str:
    counts = review_queue.counts_by_status()
    pending = counts.get(Status.PENDING, 0)
    return f"""
    <div class="tabs">
        <a href="/" class="{'active' if active == 'pending' else ''}">Pending ({pending})</a>
        <a href="/needs-retry" class="{'active' if active == 'needs-retry' else ''}">Needs Retry</a>
        <a href="/history" class="{'active' if active == 'history' else ''}">History</a>
    </div>
    """


@app.get("/", response_class=HTMLResponse)
def dashboard_home(request: Request):
    filters = _parse_filters(request)
    page_size = settings.APPROVAL_PAGE_SIZE
    offset = (filters["page"] - 1) * page_size

    flagged_only = bool(filters["flagged_only"])
    rows = review_queue.list_rows(
        status=Status.PENDING,
        task_type=filters["task_type"],
        source=filters["source"],
        confidence=filters["confidence"],
        has_safety_flags=True if flagged_only else None,
        limit=page_size,
        offset=offset,
    )

    total = review_queue.count_rows(
        status=Status.PENDING,
        task_type=filters["task_type"],
        source=filters["source"],
        confidence=filters["confidence"],
        has_safety_flags=True if flagged_only else None,
    )

    cards_html = ""
    if not rows:
        cards_html = '<div class="empty">No pending items match these filters.</div>'
    else:
        cards_html = "".join(_card_html(row) for row in rows)

    bulk_bar = """
    <form id="bulk-form" method="post" action="/bulk/approve" style="display:contents">
    </form>
    <div class="bulk-bar">
        <input type="checkbox" onclick="toggleAll(this)"> Select all
        <button class="bulk-btn" type="submit" form="bulk-form">Approve Selected</button>
        <button class="bulk-btn" type="submit" form="bulk-form" formaction="/bulk/reject">Reject Selected</button>
    </div>
    """

    query_state = {k: v for k, v in filters.items() if k != "page"}
    pagination = _pagination_html("/", query_state, filters["page"], total, page_size)

    body = f"""
    <h1>Content/SEO Agent -- Review Queue</h1>
    <div class="subtitle">{"Approving a draft publishes it to Odoo immediately." if settings.APPROVAL_AUTO_PUBLISH else "Approving a draft no longer auto-publishes -- use the Publish button once approved."} Classification rows are approve-only (nothing to publish).</div>
    {_status_bar_html()}
    {_tabs_html("pending")}
    {_filter_bar_html(filters, "/")}
    {bulk_bar}
    {cards_html}
    {pagination}
    """
    return _page_shell(body)


@app.get("/needs-retry", response_class=HTMLResponse)
def dashboard_needs_retry(request: Request):
    filters = _parse_filters(request)
    page_size = settings.APPROVAL_PAGE_SIZE
    offset = (filters["page"] - 1) * page_size

    flagged_only = bool(filters["flagged_only"])
    rows = review_queue.list_rows(
        status=Status.APPROVED, published=False,
        task_type=filters["task_type"], source=filters["source"], confidence=filters["confidence"],
        has_safety_flags=True if flagged_only else None,
        limit=page_size, offset=offset,
    )
    total = review_queue.count_rows(status=Status.APPROVED, published=False,
                                     task_type=filters["task_type"], source=filters["source"],
                                     confidence=filters["confidence"],
                                     has_safety_flags=True if flagged_only else None)

    cards_html = ""
    if not rows:
        cards_html = ('<div class="empty">Nothing here. Approving a draft publishes it '
                      f'{"immediately -- " if settings.APPROVAL_AUTO_PUBLISH else "-- "}'
                      'this tab only shows approved items that are not yet published.</div>')
    else:
        cards_html = "".join(_card_html(row, show_publish_retry=True) for row in rows)

    query_state = {k: v for k, v in filters.items() if k != "page"}
    pagination = _pagination_html("/needs-retry", query_state, filters["page"], total, page_size)

    body = f"""
    <h1>Content/SEO Agent -- Review Queue</h1>
    {_tabs_html("needs-retry")}
    {_filter_bar_html(filters, "/needs-retry")}
    {cards_html}
    {pagination}
    """
    return _page_shell(body)


@app.get("/approved")
def dashboard_approved_redirect():
    """Old route name -- kept as a redirect for any existing bookmarks/links."""
    return RedirectResponse(url="/needs-retry", status_code=307)


@app.post("/queue/{row_id}/publish")
def publish_row(row_id: int):
    row = review_queue.get_row(row_id)
    if not row or row["status"] != Status.APPROVED or row["task_type"] != TaskType.DRAFT:
        return RedirectResponse(url="/needs-retry", status_code=303)
    _attempt_publish(row_id)
    return RedirectResponse(url="/needs-retry", status_code=303)


@app.get("/history", response_class=HTMLResponse)
def dashboard_history(request: Request):
    filters = _parse_filters(request)
    page_size = settings.APPROVAL_PAGE_SIZE
    offset = (filters["page"] - 1) * page_size

    all_reviewed_statuses = [Status.APPROVED, Status.REJECTED]
    rows = []
    flagged_only = bool(filters["flagged_only"])
    for st in all_reviewed_statuses:
        rows.extend(review_queue.list_rows(
            status=st, task_type=filters["task_type"], source=filters["source"],
            confidence=filters["confidence"],
            has_safety_flags=True if flagged_only else None,
        ))
    rows.sort(key=lambda r: r.get("reviewed_at") or "", reverse=True)
    total = len(rows)
    rows = rows[offset:offset + page_size]

    cards_html = '<div class="empty">No reviewed items yet.</div>' if not rows else ""
    for row in rows:
        status_color = "#16a34a" if row["status"] == Status.APPROVED else "#dc2626"
        published_html = ""
        if row.get("published"):
            published_html = '<div style="margin-top:6px"><span class="published-badge">PUBLISHED TO ODOO</span></div>'
        elif row.get("odoo_write_detail"):
            published_html = f'<div class="dry-run-note">{html.escape(row["odoo_write_detail"])}</div>'

        note_html = ""
        if row.get("reviewer_note"):
            note_html = f'<div class="meta">Note: {html.escape(row["reviewer_note"])}</div>'
        if row.get("edited"):
            note_html += '<div class="meta">Edited by reviewer before approval</div>'

        cards_html += f"""
        <div class="card">
            <div class="card-header">
                <div>
                    <span class="title">{html.escape(row['title'])}</span>
                    <span class="badge {row['task_type']}">{row['task_type']}</span>
                </div>
                <div class="meta" style="color:{status_color}; font-weight:600;">
                    {row['status'].upper()}
                </div>
            </div>
            <div class="meta">Reviewed: {row.get('reviewed_at', '')}</div>
            {note_html}
            {published_html}
            <div class="actions">
                <form method="post" action="/queue/{row['id']}/reopen" style="display:inline">
                    <button class="reopen" type="submit">Re-open for review</button>
                </form>
            </div>
        </div>
        """

    query_state = {k: v for k, v in filters.items() if k != "page"}
    pagination = _pagination_html("/history", query_state, filters["page"], total, page_size)

    body = f"""
    <h1>Content/SEO Agent -- Review Queue</h1>
    {_tabs_html("history")}
    {_filter_bar_html(filters, "/history")}
    {cards_html}
    {pagination}
    """
    return _page_shell(body)


def _apply_draft_edits(row_id: int, overview: str | None, features: str | None, applications: str | None) -> None:
    """If the reviewer edited the draft text inline before approving, persist
    the edited version so what gets published is what they actually approved
    -- not the model's original output. A no-op for classify rows (no edit
    fields in their form) or if nothing actually changed."""
    if overview is None and features is None and applications is None:
        return
    row = review_queue.get_row(row_id)
    if not row or row["task_type"] != TaskType.DRAFT:
        return

    new_parsed = dict(row.get("parsed_output") or {})
    new_parsed["overview"] = (overview or "").strip()
    new_parsed["features"] = [line.strip() for line in (features or "").splitlines() if line.strip()]
    new_parsed["applications"] = [line.strip() for line in (applications or "").splitlines() if line.strip()]

    if new_parsed != (row.get("parsed_output") or {}):
        review_queue.update_parsed_output(row_id, new_parsed)


@app.post("/queue/{row_id}/approve")
def approve_row(
    row_id: int,
    overview: str | None = Form(default=None),
    features: str | None = Form(default=None),
    applications: str | None = Form(default=None),
):
    _apply_draft_edits(row_id, overview, features, applications)
    row = review_queue.update_status(row_id, Status.APPROVED)

    # For draft rows, approval immediately attempts publish when
    # approval_flow.auto_publish_on_approve is true (the default). If the
    # Odoo write fails, the approval still stands; the row shows up on the
    # Needs Retry tab with a retry button rather than the approval being
    # lost or silently stuck. When auto-publish is disabled, publishing
    # becomes an explicit separate action.
    if row and row["task_type"] == TaskType.DRAFT and settings.APPROVAL_AUTO_PUBLISH:
        _attempt_publish(row_id)

    return RedirectResponse(url="/", status_code=303)


@app.post("/bulk/approve")
def bulk_approve_rows(id: list[int] = Form(default=[])):
    for row_id in id:
        row = review_queue.update_status(row_id, Status.APPROVED)
        if row and row["task_type"] == TaskType.DRAFT and settings.APPROVAL_AUTO_PUBLISH:
            _attempt_publish(row_id)
    return RedirectResponse(url="/", status_code=303)


def _attempt_publish(row_id: int) -> dict[Any, Any] | None:
    row = review_queue.get_row(row_id)
    draft_json = row.get("parsed_output") or {}
    assembled = assemble_html(
        title=row["title"],
        draft_json=draft_json,
        is_regulated=bool(row.get("is_regulated")),
        ratio=row.get("ratio") or "",
        quantity_detail=row.get("quantity_detail") or "",
    )
    write_result = odoo_connector.write_product_description(row["product_id"], assembled)
    return review_queue.publish_row(
        row_id,
        assembled_html=assembled,
        success=write_result["success"],
        detail=write_result["detail"],
    )


@app.post("/queue/{row_id}/reject")
def reject_row(row_id: int, reviewer_note: str = Form(default="")):
    review_queue.update_status(row_id, Status.REJECTED, reviewer_note=reviewer_note)
    return RedirectResponse(url="/", status_code=303)


@app.post("/bulk/reject")
def bulk_reject_rows(id: list[int] = Form(default=[])):
    for row_id in id:
        review_queue.update_status(row_id, Status.REJECTED)
    return RedirectResponse(url="/", status_code=303)


@app.post("/queue/{row_id}/reopen")
def reopen_row(row_id: int):
    review_queue.reopen_row(row_id)
    return RedirectResponse(url="/history", status_code=303)


@app.get("/api/status")
def api_status():
    """JSON health/stats endpoint -- for scripted checks or a future 'Agent Status' view."""
    return {
        "small_model_available": small_model_client.is_available(),
        "queue_counts": review_queue.counts_by_status(),
    }


# ---------------------------------------------------------------------------
# Publish flow: approving a draft either auto-publishes immediately, or (if
# approval_flow.auto_publish_on_approve is false in config.yaml) just marks
# it approved and a separate "Publish" button appears. Either way it goes
# through assemble_html() (injecting disclaimer/mix ratio/delivery copy by
# code, never from the model) -> writes via connectors/odoo_connector.py.
#
# If ODOO_URL/ODOO_DB/ODOO_USERNAME/ODOO_API_KEY aren't set in .env, the
# connector automatically dry-runs: no write is attempted, and the
# History tab shows why. This lets the whole flow be tested safely
# before real Odoo credentials are added.
# ---------------------------------------------------------------------------
