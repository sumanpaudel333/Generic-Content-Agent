"""
Content/SEO Agent -- review dashboard.

Run with:
    uvicorn dashboard.app:app --host 0.0.0.0 --port 8420 --reload

Then visit http://localhost:8420 (or the server's IP:8420 from another
machine on the network, if the firewall allows it).

No agent in this system ever writes to Odoo directly. This dashboard
is the human checkpoint: it lets a person approve or reject each draft.
Approving a row here does NOT publish it -- that's a deliberate,
separate action (not yet built) so a click here can never accidentally
push something live. See "Not yet built" note at the bottom of this
file for what that publish step will need.
"""
import html
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, RedirectResponse

from content_seo_agent import review_queue, small_model_client
from content_seo_agent.assembler import assemble_html
from connectors.odoo_connector import odoo_connector

app = FastAPI(title="Content/SEO Agent Dashboard")


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
        .card {{ background: white; border: 1px solid #e7e5e4; border-radius: 8px;
                  padding: 16px; margin-bottom: 14px; }}
        .card-header {{ display: flex; justify-content: space-between; align-items: baseline;
                         margin-bottom: 8px; flex-wrap: wrap; gap: 8px; }}
        .title {{ font-weight: 600; font-size: 15px; }}
        .badge {{ display: inline-block; font-size: 11px; padding: 2px 8px; border-radius: 999px;
                   margin-left: 6px; font-weight: 500; }}
        .badge.small_model {{ background: #e0f2fe; color: #075985; }}
        .badge.claude {{ background: #ede9fe; color: #5b21b6; }}
        .badge.classify {{ background: #f1f5f9; color: #475569; }}
        .badge.draft {{ background: #fef3c7; color: #92400e; }}
        .flags {{ background: #fef2f2; border: 1px solid #fecaca; border-radius: 6px;
                   padding: 8px 10px; margin: 8px 0; font-size: 12px; color: #991b1b; }}
        pre {{ background: #fafaf9; border: 1px solid #e7e5e4; border-radius: 6px; padding: 10px;
                font-size: 12px; overflow-x: auto; white-space: pre-wrap; }}
        .actions {{ margin-top: 10px; display: flex; gap: 8px; }}
        button {{ border: none; border-radius: 6px; padding: 7px 16px; font-size: 13px;
                   cursor: pointer; font-weight: 500; }}
        .approve {{ background: #16a34a; color: white; }}
        .reject {{ background: #dc2626; color: white; }}
        .publish {{ background: #2563eb; color: white; }}
        .published-badge {{ background: #dbeafe; color: #1e40af; font-size: 11px; padding: 3px 10px;
                              border-radius: 999px; font-weight: 600; }}
        .dry-run-note {{ background: #fffbeb; border: 1px solid #fde68a; border-radius: 6px;
                           padding: 8px 10px; margin-top: 8px; font-size: 12px; color: #92400e; }}
        .empty {{ color: #78716c; text-align: center; padding: 40px; }}
        .tabs {{ margin-bottom: 16px; }}
        .tabs a {{ color: #57534e; text-decoration: none; margin-right: 16px; font-size: 13px; }}
        .tabs a.active {{ color: #292524; font-weight: 600; border-bottom: 2px solid #292524; }}
        .meta {{ font-size: 12px; color: #78716c; }}
    </style>
</head>
<body>
{body}
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
def dashboard_home():
    counts = review_queue.counts_by_status()
    pending = counts.get("pending", 0)
    approved = counts.get("approved", 0)
    rejected = counts.get("rejected", 0)

    model_up = small_model_client.is_available()
    status_class = "ok" if model_up else "down"
    status_text = "Small model: online" if model_up else "Small model: unreachable (falling back to Claude only)"

    odoo_status_class = "ok" if odoo_connector.is_configured() else "down"
    odoo_status_text = "Odoo: configured" if odoo_connector.is_configured() else "Odoo: not configured (publish will dry-run)"

    rows = review_queue.list_rows(status="pending")

    cards_html = ""
    if not rows:
        cards_html = '<div class="empty">No pending items. Run the pipeline to generate drafts.</div>'
    else:
        for row in rows:
            parsed = row.get("parsed_output") or {}
            safety_flags = row.get("safety_flags") or []
            reasons = row.get("reasons") or []

            flags_html = ""
            if safety_flags:
                flags_html = (
                    f'<div class="flags">⚠ Needs attention: '
                    f'{html.escape(", ".join(str(f) for f in safety_flags))}</div>'
                )

            reasons_html = ""
            if reasons and not safety_flags:
                reasons_html = (
                    f'<div class="meta">Escalation reason(s): '
                    f'{html.escape("; ".join(str(r) for r in reasons))}</div>'
                )

            cards_html += f"""
            <div class="card">
                <div class="card-header">
                    <div>
                        <span class="title">{html.escape(row['title'])}</span>
                        <span class="badge {row['task_type']}">{row['task_type']}</span>
                        <span class="badge {row['source']}">{row['source']}</span>
                    </div>
                    <div class="meta">{row['product_id']} &middot; confidence: {row['confidence']}</div>
                </div>
                {flags_html}
                {reasons_html}
                <pre>{html.escape(json.dumps(parsed, indent=2, ensure_ascii=False))}</pre>
                <div class="actions">
                    <form method="post" action="/queue/{row['id']}/approve" style="display:inline">
                        <button class="approve" type="submit">{"Approve & Publish" if row['task_type'] == 'draft' else "Approve"}</button>
                    </form>
                    <form method="post" action="/queue/{row['id']}/reject" style="display:inline">
                        <button class="reject" type="submit">Reject</button>
                    </form>
                </div>
            </div>
            """

    body = f"""
    <h1>Content/SEO Agent -- Review Queue</h1>
    <div class="subtitle">Approving a draft publishes it to Odoo immediately. Classification rows are approve-only (nothing to publish).</div>
    <div class="status-bar">
        <div class="status-pill {status_class}">{status_text}</div>
        <div class="status-pill {odoo_status_class}">{odoo_status_text}</div>
        <div class="status-pill">Pending: {pending}</div>
        <div class="status-pill">Approved: {approved}</div>
        <div class="status-pill">Rejected: {rejected}</div>
    </div>
    <div class="tabs">
        <a href="/" class="active">Pending ({pending})</a>
        <a href="/approved">Needs Retry</a>
        <a href="/history">History</a>
    </div>
    {cards_html}
    """
    return _page_shell(body)


@app.get("/approved", response_class=HTMLResponse)
def dashboard_approved():
    rows = review_queue.list_rows(status="approved")
    unpublished = [r for r in rows if not r.get("published")]

    cards_html = ""
    if not unpublished:
        cards_html = '<div class="empty">Nothing here. Approving a draft publishes it immediately -- ' \
                      'this tab only shows items where that publish attempt failed and needs a retry.</div>'
    else:
        for row in unpublished:
            parsed = row.get("parsed_output") or {}
            publish_btn = ""
            if row["task_type"] == "draft":
                publish_btn = f"""
                <form method="post" action="/queue/{row['id']}/publish" style="display:inline">
                    <button class="publish" type="submit">Retry Publish to Odoo</button>
                </form>
                """
            else:
                publish_btn = '<span class="meta">(classification rows are not published)</span>'

            fail_note = ""
            if row.get("odoo_write_detail"):
                fail_note = f'<div class="dry-run-note">Last attempt: {html.escape(row["odoo_write_detail"])}</div>'

            cards_html += f"""
            <div class="card">
                <div class="card-header">
                    <div>
                        <span class="title">{html.escape(row['title'])}</span>
                        <span class="badge {row['task_type']}">{row['task_type']}</span>
                        <span class="badge {row['source']}">{row['source']}</span>
                    </div>
                    <div class="meta">{row['product_id']}</div>
                </div>
                <pre>{html.escape(json.dumps(parsed, indent=2, ensure_ascii=False))}</pre>
                {fail_note}
                <div class="actions">{publish_btn}</div>
            </div>
            """

    counts = review_queue.counts_by_status()
    body = f"""
    <h1>Content/SEO Agent -- Review Queue</h1>
    <div class="tabs">
        <a href="/">Pending ({counts.get('pending', 0)})</a>
        <a href="/approved" class="active">Needs Retry</a>
        <a href="/history">History</a>
    </div>
    {cards_html}
    """
    return _page_shell(body)


@app.post("/queue/{row_id}/publish")
def publish_row(row_id: int):
    row = review_queue.get_row(row_id)
    if not row or row["status"] != "approved" or row["task_type"] != "draft":
        return RedirectResponse(url="/approved", status_code=303)
    _attempt_publish(row_id)
    return RedirectResponse(url="/approved", status_code=303)


@app.get("/history", response_class=HTMLResponse)
def dashboard_history():
    rows = review_queue.list_rows()
    reviewed = [r for r in rows if r["status"] != "pending"]

    cards_html = '<div class="empty">No reviewed items yet.</div>' if not reviewed else ""
    for row in reviewed:
        status_color = "#16a34a" if row["status"] == "approved" else "#dc2626"
        published_html = ""
        if row.get("published"):
            published_html = '<div style="margin-top:6px"><span class="published-badge">PUBLISHED TO ODOO</span></div>'
        elif row.get("odoo_write_detail"):
            published_html = f'<div class="dry-run-note">{html.escape(row["odoo_write_detail"])}</div>'

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
            {published_html}
        </div>
        """

    counts = review_queue.counts_by_status()
    body = f"""
    <h1>Content/SEO Agent -- Review Queue</h1>
    <div class="tabs">
        <a href="/">Pending ({counts.get('pending', 0)})</a>
        <a href="/approved">Approved</a>
        <a href="/history" class="active">History</a>
    </div>
    {cards_html}
    """
    return _page_shell(body)


@app.post("/queue/{row_id}/approve")
def approve_row(row_id: int):
    row = review_queue.update_status(row_id, "approved")

    # For draft rows, approval immediately attempts publish -- per
    # design decision, "Approve" and "Publish" are one action from the
    # reviewer's side. If the Odoo write fails, the approval still
    # stands; the row shows up on the Approved tab with a retry button
    # rather than the approval being lost or silently stuck.
    if row and row["task_type"] == "draft":
        _attempt_publish(row_id)

    return RedirectResponse(url="/", status_code=303)


def _attempt_publish(row_id: int) -> dict:
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
def reject_row(row_id: int):
    review_queue.update_status(row_id, "rejected")
    return RedirectResponse(url="/", status_code=303)


@app.get("/api/status")
def api_status():
    """JSON health/stats endpoint -- for scripted checks or a future 'Agent Status' view."""
    return {
        "small_model_available": small_model_client.is_available(),
        "queue_counts": review_queue.counts_by_status(),
    }


# ---------------------------------------------------------------------------
# Publish flow: Approved tab -> "Publish to Odoo" -> assembles the final
# HTML via assembler.py (injecting disclaimer/mix ratio/delivery copy by
# code, never from the model) -> writes via connectors/odoo_connector.py.
#
# If ODOO_URL/ODOO_DB/ODOO_USERNAME/ODOO_API_KEY aren't set in .env, the
# connector automatically dry-runs: no write is attempted, and the
# History tab shows why. This lets the whole flow be tested safely
# before real Odoo credentials are added.
# ---------------------------------------------------------------------------
