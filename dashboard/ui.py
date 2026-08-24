"""
Shared UI chrome for the Automation Control Dashboard.

Holds the theme, the page shell, and the top-level module navigation --
kept separate from app.py so adding a new automation module is a matter
of registering it in MODULES and adding its routes, without touching
layout or styling.

Theme is BC Sands blue (#004495, matching business.heading_color in
config.yaml) with a yellow accent.
"""
import html
from typing import Any

BRAND_BLUE = "#004495"
BRAND_YELLOW = "#FFC72C"

# ---------------------------------------------------------------------------
# Module registry -- the top-level tabs. Add an entry here (plus its routes
# in app.py) to introduce a new automation; nav, the overview grid, and
# active-state highlighting all read from this single list.
# ---------------------------------------------------------------------------
MODULES: list[dict[str, Any]] = [
    {
        "key": "content-agent",
        "label": "Content Agent",
        "path": "/content-agent",
        "status": "live",
        "blurb": "Drafts product descriptions with a local model, escalates hard "
                  "ones, and routes every draft through human review before it "
                  "reaches Odoo.",
    },
    {
        "key": "chat-insights",
        "label": "Chat Insights",
        "path": "/chat-insights",
        "status": "live",
        "blurb": "Pulls the week's Chatbase conversations, works out what the bot "
                  "could not answer, what customers asked about and who looks like a "
                  "lead, then emails the manager a report.",
    },
    {
        "key": "pricing-agent",
        "label": "Pricing Agent",
        "path": "#",
        "status": "planned",
        "blurb": "Placeholder for the next automation. Register it in "
                  "dashboard/ui.py MODULES and add its routes to light it up.",
    },
]

NAV_ITEMS = [{"key": "overview", "label": "Overview", "path": "/"}] + [
    {"key": m["key"], "label": m["label"], "path": m["path"]}
    for m in MODULES if m["status"] == "live"
]


def module_by_key(key: str) -> dict | None:
    return next((m for m in MODULES if m["key"] == key), None)


# ---------------------------------------------------------------------------
# Theme
# ---------------------------------------------------------------------------
THEME_CSS = """
:root {
    --blue-900:#002A5E; --blue-800:#003370; --blue-700:#004495; --blue-600:#0A5FBF;
    --blue-500:#1E7AE0; --blue-100:#DCE9F8; --blue-50:#EFF5FC;
    --yellow-600:#E0A500; --yellow-500:#FFC72C; --yellow-300:#FFDE7A; --yellow-100:#FFF3CE;
    --ink:#0F172A; --muted:#5A6B85; --line:#DDE5EF; --bg:#F2F6FB; --card:#FFFFFF;
    --green:#0E8A4A; --green-bg:#E6F6ED; --red:#C62828; --red-bg:#FDECEC;
    --amber:#B26B00; --amber-bg:#FFF6E3;
    --radius:10px; --shadow:0 1px 2px rgba(15,32,60,.06), 0 4px 14px rgba(15,32,60,.06);
}
* { box-sizing:border-box; }
body { font-family:'Segoe UI',-apple-system,system-ui,Arial,sans-serif; margin:0;
       background:var(--bg); color:var(--ink); font-size:14px; line-height:1.5; }
a { color:var(--blue-600); }

/* ---------- App bar ---------- */
.appbar { background:linear-gradient(135deg,var(--blue-800) 0%,var(--blue-700) 55%,var(--blue-600) 100%);
          color:#fff; box-shadow:0 2px 10px rgba(0,42,94,.22); position:sticky; top:0; z-index:50; }
.appbar-inner { max-width:1280px; margin:0 auto; padding:0 24px; display:flex; align-items:center;
                gap:28px; min-height:60px; flex-wrap:wrap; }
.brand { display:flex; align-items:center; gap:11px; font-weight:700; font-size:16px;
         letter-spacing:.2px; color:#fff; text-decoration:none; white-space:nowrap; }
.brand-mark { width:30px; height:30px; border-radius:8px; background:var(--yellow-500);
              color:var(--blue-800); display:grid; place-items:center; font-size:15px; font-weight:800; }
.brand-sub { font-weight:400; font-size:11px; opacity:.72; display:block; margin-top:-2px; letter-spacing:.3px; }
.nav { display:flex; gap:4px; margin-left:auto; flex-wrap:wrap; }
.nav a { color:rgba(255,255,255,.82); text-decoration:none; font-size:13.5px; font-weight:500;
         padding:8px 14px; border-radius:8px; border-bottom:2px solid transparent; transition:.15s; }
.nav a:hover { background:rgba(255,255,255,.12); color:#fff; }
.nav a.active { color:#fff; background:rgba(255,255,255,.14); border-bottom-color:var(--yellow-500); }
.usermenu { display:flex; align-items:center; gap:10px; padding-left:18px;
            border-left:1px solid rgba(255,255,255,.2); }
.avatar { width:30px; height:30px; border-radius:50%; background:var(--yellow-500); color:var(--blue-800);
          display:grid; place-items:center; font-weight:700; font-size:12.5px; }
.uname { font-size:13px; line-height:1.2; }
.urole { font-size:10.5px; opacity:.72; text-transform:uppercase; letter-spacing:.5px; }
.logout-btn { background:rgba(255,255,255,.14); color:#fff; border:1px solid rgba(255,255,255,.24);
              padding:6px 12px; font-size:12.5px; border-radius:7px; cursor:pointer; }
.logout-btn:hover { background:rgba(255,255,255,.24); }

/* ---------- Layout ---------- */
.wrap { max-width:1280px; margin:0 auto; padding:26px 24px 60px; }
.page-head { display:flex; justify-content:space-between; align-items:flex-start; gap:16px;
             flex-wrap:wrap; margin-bottom:18px; }
h1 { font-size:21px; margin:0 0 4px; letter-spacing:-.2px; }
h2 { font-size:16px; margin:0 0 12px; }
.subtitle { color:var(--muted); font-size:13px; margin:0; max-width:70ch; }

/* ---------- Sub-tabs ---------- */
.tabs { display:flex; gap:6px; margin-bottom:18px; border-bottom:1px solid var(--line); flex-wrap:wrap; }
.tabs a { padding:9px 15px; font-size:13.5px; color:var(--muted); text-decoration:none; font-weight:500;
          border-bottom:2.5px solid transparent; margin-bottom:-1px; transition:.15s; }
.tabs a:hover { color:var(--blue-700); }
.tabs a.active { color:var(--blue-700); border-bottom-color:var(--yellow-500); font-weight:600; }
.tab-count { background:var(--blue-100); color:var(--blue-700); font-size:11px; font-weight:700;
             padding:1px 7px; border-radius:999px; margin-left:6px; }
.tabs a.active .tab-count { background:var(--yellow-500); color:var(--blue-800); }

/* ---------- Stat tiles ---------- */
.stats { display:grid; grid-template-columns:repeat(auto-fit,minmax(158px,1fr)); gap:12px; margin-bottom:20px; }
.stat { background:var(--card); border:1px solid var(--line); border-radius:var(--radius);
        padding:13px 15px; box-shadow:var(--shadow); border-left:3px solid var(--blue-600); }
.stat.accent { border-left-color:var(--yellow-500); }
.stat.ok { border-left-color:var(--green); }
.stat.bad { border-left-color:var(--red); }
.stat.warn { border-left-color:var(--amber); }
.stat-label { font-size:11px; text-transform:uppercase; letter-spacing:.6px; color:var(--muted); font-weight:600; }
.stat-value { font-size:23px; font-weight:700; margin-top:3px; letter-spacing:-.5px; }
.stat-value.sm { font-size:14px; font-weight:600; margin-top:5px; }
.stat a { text-decoration:none; color:inherit; }
.dot { width:8px; height:8px; border-radius:50%; display:inline-block; margin-right:6px; }
.dot.ok { background:var(--green); } .dot.bad { background:var(--red); }

/* ---------- Cards ---------- */
.card { background:var(--card); border:1px solid var(--line); border-radius:var(--radius);
        padding:16px 18px; margin-bottom:13px; box-shadow:var(--shadow); transition:.15s; }
.card:hover { box-shadow:0 2px 4px rgba(15,32,60,.07), 0 8px 24px rgba(15,32,60,.09); }
.card-header { display:flex; justify-content:space-between; align-items:flex-start; gap:10px;
               margin-bottom:9px; flex-wrap:wrap; }
.card-header-left { display:flex; align-items:center; gap:7px; flex-wrap:wrap; }
.title { font-weight:650; font-size:14.5px; }
.panel { background:var(--card); border:1px solid var(--line); border-radius:var(--radius);
         padding:16px 18px; margin-bottom:16px; box-shadow:var(--shadow); }

/* ---------- Module cards (overview) ---------- */
.modgrid { display:grid; grid-template-columns:repeat(auto-fit,minmax(290px,1fr)); gap:14px; }
.modcard { background:var(--card); border:1px solid var(--line); border-radius:var(--radius);
           padding:18px; box-shadow:var(--shadow); display:flex; flex-direction:column;
           text-decoration:none; color:inherit; transition:.16s; border-top:3px solid var(--blue-600); }
.modcard:hover { transform:translateY(-2px); box-shadow:0 4px 8px rgba(15,32,60,.08),0 12px 30px rgba(15,32,60,.11); }
.modcard.planned { border-top-color:var(--line); opacity:.72; }
.modcard.planned:hover { transform:none; }
.modcard h3 { margin:0 0 6px; font-size:15.5px; }
.modcard p { margin:0 0 14px; font-size:13px; color:var(--muted); flex:1; }
.modstat { display:flex; gap:16px; padding-top:12px; border-top:1px solid var(--line); }
.modstat div { font-size:12px; color:var(--muted); }
.modstat b { display:block; font-size:17px; color:var(--ink); font-weight:700; }

/* ---------- Badges ---------- */
.badge { display:inline-block; font-size:10.5px; padding:2.5px 9px; border-radius:999px;
         font-weight:600; letter-spacing:.2px; }
.badge.small_model { background:var(--blue-100); color:var(--blue-700); }
.badge.claude { background:#EDE7F9; color:#5B21B6; }
.badge.classify { background:#EEF2F7; color:#475569; }
.badge.draft { background:var(--yellow-100); color:var(--yellow-600); }
.badge.confidence-low { background:var(--red-bg); color:var(--red); }
.badge.confidence-high { background:var(--green-bg); color:var(--green); }
.badge.live { background:var(--green-bg); color:var(--green); }
.badge.planned { background:#EEF2F7; color:var(--muted); }
.badge.role { background:var(--blue-100); color:var(--blue-700); }
.published-badge { background:var(--green-bg); color:var(--green); font-size:10.5px; padding:3px 10px;
                   border-radius:999px; font-weight:700; letter-spacing:.3px; }

/* ---------- Notices ---------- */
.flags { background:var(--red-bg); border:1px solid #F5C6C6; border-left:3px solid var(--red);
         border-radius:8px; padding:9px 12px; margin:9px 0; font-size:12.5px; color:#8E1F1F; }
.dry-run-note { background:var(--amber-bg); border:1px solid var(--yellow-300); border-left:3px solid var(--yellow-500);
                border-radius:8px; padding:9px 12px; margin-top:9px; font-size:12.5px; color:var(--amber); }
.meta { font-size:12px; color:var(--muted); }
.empty { color:var(--muted); text-align:center; padding:48px 20px; background:var(--card);
         border:1px dashed var(--line); border-radius:var(--radius); }
.empty-title { font-weight:600; color:var(--ink); margin-bottom:5px; font-size:14px; }

/* ---------- Toast ---------- */
.toast { position:fixed; right:22px; bottom:22px; z-index:200; max-width:400px;
         padding:13px 16px; border-radius:var(--radius); font-size:13.5px; color:#fff;
         box-shadow:0 8px 30px rgba(15,32,60,.26); animation:slidein .25s ease-out; }
.toast.ok { background:var(--green); } .toast.err { background:var(--red); }
.toast button { background:transparent; border:none; color:rgba(255,255,255,.85); cursor:pointer;
                font-size:15px; margin-left:10px; padding:0; }
@keyframes slidein { from { transform:translateY(14px); opacity:0; } to { transform:none; opacity:1; } }

/* ---------- Content preview ---------- */
.preview { font-size:13px; line-height:1.6; margin:9px 0; }
.preview p { margin:0 0 9px; } .preview ul { margin:0 0 9px; padding-left:20px; } .preview li { margin-bottom:3px; }
.preview .field-label { font-weight:700; font-size:10.5px; text-transform:uppercase; letter-spacing:.6px;
                        color:var(--blue-600); margin:11px 0 3px; }
.tail-block { background:var(--blue-50); border:1px solid var(--blue-100); border-radius:8px;
              padding:11px 13px; margin-top:9px; color:var(--muted); font-size:12.5px; }
.tail-block p { margin:0 0 7px; }
details.raw-json { margin-top:8px; }
details.raw-json summary { font-size:12px; color:var(--muted); cursor:pointer; user-select:none; }
details.raw-json summary:hover { color:var(--blue-600); }
pre { background:#F7F9FC; border:1px solid var(--line); border-radius:8px; padding:11px;
      font-size:11.5px; overflow-x:auto; white-space:pre-wrap; margin:7px 0 0; }

/* ---------- Forms & buttons ---------- */
button { border:none; border-radius:8px; padding:8px 16px; font-size:13px; cursor:pointer;
         font-weight:600; font-family:inherit; transition:.15s; }
button:hover { filter:brightness(1.07); } button:active { transform:translateY(1px); }
button:disabled { opacity:.55; cursor:not-allowed; filter:none; transform:none; }
.approve { background:var(--green); color:#fff; }
.reject { background:var(--red); color:#fff; }
.publish { background:var(--blue-600); color:#fff; }
.reopen { background:#EEF2F7; color:var(--muted); border:1px solid var(--line); }
.btn-primary { background:var(--blue-700); color:#fff; }
.btn-accent { background:var(--yellow-500); color:var(--blue-800); }
.btn-ghost { background:var(--card); color:var(--blue-700); border:1px solid var(--line); }
.btn-ghost:hover { border-color:var(--blue-600); }
.bulk-btn { background:var(--blue-700); color:#fff; }
input[type=text], input[type=password], select, textarea {
    font-family:inherit; font-size:13px; padding:8px 11px; border:1px solid var(--line);
    border-radius:8px; background:#fff; color:var(--ink); }
input:focus, select:focus, textarea:focus { outline:2px solid var(--blue-500); outline-offset:-1px; border-color:transparent; }
label { font-size:12.5px; color:var(--muted); font-weight:500; }
.actions { margin-top:12px; display:flex; gap:8px; align-items:center; flex-wrap:wrap;
           padding-top:12px; border-top:1px solid var(--line); }
.reject-note { flex:1; min-width:150px; font-size:12.5px; }
.edit-field { width:100%; font-size:13px; line-height:1.6; padding:9px 11px; resize:vertical;
              background:#FFFDF5; border:1px solid var(--yellow-300); margin-bottom:8px; }
.edit-toggle-btn { background:var(--blue-50); border:1px solid var(--blue-100); color:var(--blue-700);
                   font-size:12px; cursor:pointer; padding:5px 11px; border-radius:7px;
                   margin-top:8px; font-weight:600; }
.edit-toggle-btn:hover { background:var(--blue-100); }
.edit-mode-actions { display:flex; gap:8px; margin-top:6px; }
.save-edit-btn { background:var(--blue-600); color:#fff; padding:6px 14px; font-size:12.5px; }
.cancel-edit-btn { background:#fff; border:1px solid var(--line); color:var(--muted); padding:6px 14px; font-size:12.5px; }

/* ---------- Bars ---------- */
.filter-bar { background:var(--card); border:1px solid var(--line); border-radius:var(--radius);
              padding:12px 16px; margin-bottom:15px; display:flex; gap:14px; flex-wrap:wrap;
              align-items:center; box-shadow:var(--shadow); }
.filter-bar label { display:flex; align-items:center; gap:6px; }
.filter-bar a.clear { color:var(--muted); font-size:12.5px; }
.fetch-bar { background:linear-gradient(100deg,var(--blue-50),#fff 60%); border:1px solid var(--blue-100);
             border-left:3px solid var(--yellow-500); border-radius:var(--radius); padding:13px 16px;
             margin-bottom:15px; display:flex; gap:14px; align-items:center; flex-wrap:wrap; }
.bulk-bar { display:flex; gap:10px; align-items:center; margin-bottom:13px; font-size:13px;
            background:var(--card); border:1px solid var(--line); border-radius:var(--radius);
            padding:10px 15px; box-shadow:var(--shadow); position:sticky; top:70px; z-index:20; }
.bulk-bar.has-selection { border-color:var(--yellow-500); background:var(--yellow-100); }
.sel-count { font-weight:700; color:var(--blue-700); }
.checkbox-col { margin-right:4px; width:15px; height:15px; accent-color:var(--blue-700); cursor:pointer; }
.spinner { width:13px; height:13px; border:2px solid var(--blue-100); border-top-color:var(--blue-600);
           border-radius:50%; display:inline-block; animation:spin .7s linear infinite; vertical-align:-2px; }
@keyframes spin { to { transform:rotate(360deg); } }

/* ---------- Table ---------- */
table.grid { width:100%; border-collapse:collapse; font-size:13px; }
table.grid th { text-align:left; font-size:11px; text-transform:uppercase; letter-spacing:.6px;
                color:var(--muted); padding:9px 10px; border-bottom:2px solid var(--line); }
table.grid td { padding:10px; border-bottom:1px solid var(--line); }
table.grid tr:last-child td { border-bottom:none; }

/* ---------- Pagination ---------- */
.pagination { display:flex; gap:8px; justify-content:center; align-items:center; margin-top:20px; font-size:13px; }
.pagination a { color:var(--blue-700); text-decoration:none; padding:7px 13px; border-radius:8px;
                border:1px solid var(--line); background:var(--card); font-weight:500; }
.pagination a:hover { border-color:var(--blue-600); background:var(--blue-50); }
.pagination .disabled { color:#B8C4D4; padding:7px 13px; border:1px solid var(--line);
                        border-radius:8px; background:#F7F9FC; }

/* ---------- Login ---------- */
.login-bg { min-height:100vh; display:grid; place-items:center; padding:24px;
            background:linear-gradient(135deg,var(--blue-800) 0%,var(--blue-700) 50%,var(--blue-600) 100%); }
.login-card { background:#fff; border-radius:14px; padding:34px; width:100%; max-width:388px;
              box-shadow:0 20px 60px rgba(0,25,60,.34); }
.login-brand { display:flex; align-items:center; gap:11px; margin-bottom:6px; }
.login-card h1 { font-size:19px; margin:0; }
.login-card .subtitle { margin-bottom:22px; font-size:12.5px; }
.field { margin-bottom:15px; }
.field label { display:block; margin-bottom:5px; font-weight:600; color:var(--ink); font-size:12.5px; }
.field input { width:100%; padding:10px 12px; font-size:14px; }
.login-btn { width:100%; padding:11px; font-size:14px; background:var(--blue-700); color:#fff; margin-top:4px; }
.login-err { background:var(--red-bg); border:1px solid #F5C6C6; color:#8E1F1F; padding:10px 12px;
             border-radius:8px; font-size:12.5px; margin-bottom:15px; }

@media (max-width:720px) {
    .appbar-inner { padding:10px 16px; gap:12px; } .nav { margin-left:0; width:100%; }
    .wrap { padding:18px 16px 50px; } .bulk-bar { position:static; }
}
"""

SHARED_JS = """
function toggleAll(src){
    document.querySelectorAll('.row-check').forEach(cb => cb.checked = src.checked);
    updateSelCount();
}
function updateSelCount(){
    const n = document.querySelectorAll('.row-check:checked').length;
    const el = document.getElementById('sel-count');
    if(el) el.textContent = n;
    const bar = document.querySelector('.bulk-bar');
    if(bar) bar.classList.toggle('has-selection', n > 0);
    document.querySelectorAll('[data-needs-selection]').forEach(b => b.disabled = n === 0);
}
function confirmBulk(action){
    const n = document.querySelectorAll('.row-check:checked').length;
    if(n === 0){ return false; }
    return confirm(`${action} ${n} selected item${n>1?'s':''}?`);
}
function escapeHtml(s){ const d=document.createElement('div'); d.textContent=s; return d.innerHTML; }
function toggleEdit(id){
    const w=document.querySelector(`.draft-content[data-row-id="${id}"]`);
    w.querySelector('.view-mode').style.display='none';
    w.querySelector('.edit-mode').style.display='block';
    const ta=w.querySelector('textarea[data-field="overview"]'); if(ta) ta.focus();
}
function cancelEdit(id){
    const w=document.querySelector(`.draft-content[data-row-id="${id}"]`);
    w.querySelectorAll('textarea').forEach(t=>t.value=t.defaultValue);
    w.querySelector('.edit-mode').style.display='none';
    w.querySelector('.view-mode').style.display='block';
}
function saveEdit(id){
    const w=document.querySelector(`.draft-content[data-row-id="${id}"]`);
    const ov=w.querySelector('textarea[data-field="overview"]').value.trim();
    const ft=w.querySelector('textarea[data-field="features"]').value.split('\\n').map(s=>s.trim()).filter(Boolean);
    const ap=w.querySelector('textarea[data-field="applications"]').value.split('\\n').map(s=>s.trim()).filter(Boolean);
    const tpl=w.querySelector('.mixline-template');
    const mix=tpl?tpl.innerHTML.trim():'';
    let out='';
    if(ov) out+=`<div class="field-label">Overview</div><p>${escapeHtml(ov)}</p>`;
    if(ft.length||mix){ out+=`<div class="field-label">Features</div><ul>`+(mix?`<li>${mix}</li>`:'')+
        ft.map(f=>`<li>${escapeHtml(f)}</li>`).join('')+`</ul>`; }
    if(ap.length) out+=`<div class="field-label">Applications</div><ul>`+
        ap.map(a=>`<li>${escapeHtml(a)}</li>`).join('')+`</ul>`;
    w.querySelector('.editable-preview').innerHTML = out || '<span class="meta">(no content)</span>';
    w.querySelector('.edit-mode').style.display='none';
    w.querySelector('.view-mode').style.display='block';
    w.classList.add('locally-edited');
}
document.addEventListener('DOMContentLoaded', function(){
    document.querySelectorAll('.row-check').forEach(cb => cb.addEventListener('change', updateSelCount));
    updateSelCount();
    const t=document.querySelector('.toast');
    if(t) setTimeout(()=>{ t.style.transition='opacity .4s'; t.style.opacity='0';
                            setTimeout(()=>t.remove(),400); }, 6000);
    // While a fetch is running, refresh periodically so the reviewer sees
    // it finish (and the new drafts appear) without manually reloading.
    if(document.body.dataset.fetchRunning === '1'){ setTimeout(()=>location.reload(), 5000); }
});
"""


def _esc(s: Any) -> str:
    return html.escape(str(s if s is not None else ""))


def initials(name: str) -> str:
    parts = [p for p in str(name).replace(".", " ").replace("_", " ").split() if p]
    if not parts:
        return "?"
    return (parts[0][0] + (parts[1][0] if len(parts) > 1 else "")).upper()


def toast_html(request) -> str:
    """Renders a one-shot flash message from ?msg= / ?err= on the URL.
    Query-param based (rather than server-side session state) so it works
    cleanly with the POST-redirect-GET pattern every action here uses."""
    msg = request.query_params.get("msg")
    err = request.query_params.get("err")
    if not msg and not err:
        return ""
    kind = "err" if err else "ok"
    text = _esc(err or msg)
    return (f'<div class="toast {kind}">{text}'
            f'<button onclick="this.parentElement.remove()" title="Dismiss">&times;</button></div>')


def _nav_html(active_module: str, user: dict | None) -> str:
    links = "".join(
        f'<a href="{i["path"]}" class="{"active" if i["key"] == active_module else ""}">{_esc(i["label"])}</a>'
        for i in NAV_ITEMS
    )
    if user and user.get("role") == "admin":
        links += (f'<a href="/settings/users" class="{"active" if active_module == "settings" else ""}">'
                   f'Settings</a>')

    user_block = ""
    if user:
        name = user.get("display_name") or user["username"]
        user_block = f"""
        <div class="usermenu">
            <div class="avatar">{_esc(initials(name))}</div>
            <div>
                <div class="uname">{_esc(name)}</div>
                <div class="urole">{_esc(user.get("role", ""))}</div>
            </div>
            <form method="post" action="/logout" style="margin:0">
                <button class="logout-btn" type="submit">Sign out</button>
            </form>
        </div>"""

    return f"""
    <header class="appbar">
      <div class="appbar-inner">
        <a class="brand" href="/">
          <span class="brand-mark">A</span>
          <span>Automation Control
            <span class="brand-sub">BC SANDS</span>
          </span>
        </a>
        <nav class="nav">{links}</nav>
        {user_block}
      </div>
    </header>"""


def page_shell(body: str, *, title: str = "Automation Control Dashboard",
                active_module: str = "", user: dict | None = None,
                request=None, fetch_running: bool = False) -> str:
    toast = toast_html(request) if request is not None else ""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_esc(title)}</title>
<style>{THEME_CSS}</style>
<script>{SHARED_JS}</script>
</head>
<body data-fetch-running="{'1' if fetch_running else '0'}">
{_nav_html(active_module, user)}
<main class="wrap">
{body}
</main>
{toast}
</body>
</html>"""


def login_page(error: str = "", username: str = "", notice: str = "") -> str:
    err_html = f'<div class="login-err">{_esc(error)}</div>' if error else ""
    notice_html = (f'<div class="login-err" style="background:var(--blue-50);border-color:var(--blue-100);'
                    f'color:var(--blue-700)">{_esc(notice)}</div>') if notice else ""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sign in &middot; Automation Control</title>
<style>{THEME_CSS}</style>
</head>
<body>
<div class="login-bg">
  <div class="login-card">
    <div class="login-brand">
      <span class="brand-mark" style="width:34px;height:34px;font-size:17px">A</span>
      <div>
        <h1>Automation Control</h1>
        <div class="meta">BC Sands</div>
      </div>
    </div>
    <p class="subtitle">Sign in to manage automations and review queues.</p>
    {err_html}{notice_html}
    <form method="post" action="/login">
      <div class="field">
        <label for="username">Username</label>
        <input id="username" name="username" type="text" value="{_esc(username)}"
               autocomplete="username" autofocus required>
      </div>
      <div class="field">
        <label for="password">Password</label>
        <input id="password" name="password" type="password" autocomplete="current-password" required>
      </div>
      <button class="login-btn" type="submit">Sign in</button>
    </form>
  </div>
</div>
</body>
</html>"""


def stat_tile(label: str, value: Any, *, variant: str = "", href: str = "", small: bool = False) -> str:
    val_cls = "stat-value sm" if small else "stat-value"
    inner = f'<div class="stat-label">{_esc(label)}</div><div class="{val_cls}">{value}</div>'
    if href:
        inner = f'<a href="{href}">{inner}</a>'
    return f'<div class="stat {variant}">{inner}</div>'


def empty_state(title: str, detail: str = "") -> str:
    return (f'<div class="empty"><div class="empty-title">{_esc(title)}</div>'
            f'<div>{_esc(detail)}</div></div>')
